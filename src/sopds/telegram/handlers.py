"""Telegram message and callback handlers over database-free service contracts."""

import asyncio
import logging
import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from sopds.acquisition.contracts import (
    AcquiredOriginal,
    Acquisition,
    AcquisitionError,
    AcquisitionMemberNotFoundError,
    AcquisitionNotFoundError,
    AcquisitionUnavailableError,
    AsyncByteStream,
    OriginalDescription,
)
from sopds.catalog.contracts import Catalog, CatalogPage, CatalogRequest
from sopds.conversion.contracts import (
    ConversionShutdownError,
    ConversionSourceError,
    ConversionTimeoutError,
    ConverterExecutionError,
    InvalidConversionOutputError,
    SourceChangedError,
    SourceUnavailableError,
    UnsupportedConversionError,
)
from sopds.conversion.policy import OUTPUT_POLICY, OutputDecision, OutputPolicy
from sopds.conversion.service import ConversionService
from sopds.telegram.formatting import (
    button_label,
    detail_text,
    results_text,
    safe_filename,
    sanitize,
    source_format_label,
    truncate,
)
from sopds.telegram.state import CallbackStateStore, DownloadState, PageState
from sopds.telegram.upload import StreamingInputFile

_LOGGER = logging.getLogger(__name__)
_PUBLIC_ID = re.compile(r"[A-Za-z0-9._~-]{1,62}\Z")
_UPLOAD_LIMIT = 50 * 1024 * 1024


class TelegramHandlers:
    def __init__(
        self,
        catalog: Catalog,
        acquisition: Acquisition,
        state: CallbackStateStore,
        conversion: ConversionService | None = None,
        output_policy: OutputPolicy = OUTPUT_POLICY,
    ) -> None:
        self._catalog = catalog
        self._acquisition = acquisition
        self._state = state
        self._conversion = conversion
        self._output_policy = output_policy

    def router(self) -> Router:
        router = Router(name="telegram")
        router.message.register(self.on_start_command, Command("start"))
        router.message.register(self.on_plain_text, F.text & ~F.text.startswith("/"))
        router.callback_query.register(self.on_callback)
        return router

    async def on_start_command(self, message: Message) -> None:
        await message.answer(f"i'm here. your id is {message.chat.id}")

    async def on_plain_text(self, message: Message) -> None:
        text = message.text
        if text is None:
            return
        query = text.strip()
        if query:
            await self._search_message(message, query)

    async def on_callback(self, callback: CallbackQuery) -> None:
        data = callback.data
        if data is None:
            await callback.answer()
            return
        prefix, separator, value = data.partition(":")
        if not separator:
            await callback.answer()
            return
        if prefix == "p":
            await self._page(callback, value)
            return
        if prefix == "c":
            await self._converted_callback(callback, value)
            return

        await callback.answer()
        if prefix not in {"d", "x"} or not _valid_public_id(value):
            return
        if prefix == "d":
            await self._detail(callback, value)
        else:
            await self._download_original(callback, value)

    async def _search_message(self, message: Message, query: str) -> None:
        try:
            page = await self._catalog.browse(CatalogRequest(query=query, page_size=10))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.warning(f"Telegram search failed failure_type={type(error).__name__}")
            await message.answer("Search is temporarily unavailable.")
            return
        markup = await self._result_markup(message.chat.id, query, page)
        await message.answer(results_text(page.books), reply_markup=markup)

    async def _detail(self, callback: CallbackQuery, public_id: str) -> None:
        message = callback.message
        if message is None or isinstance(message, InaccessibleMessage):
            return
        try:
            book = await self._catalog.details(public_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.warning(f"Telegram detail failed failure_type={type(error).__name__}")
            await message.answer("Book details are temporarily unavailable.")
            return
        if book is None:
            await message.answer("Book is no longer available.")
            return
        buttons = [
            InlineKeyboardButton(
                text=source_format_label(book.original_format), callback_data=f"x:{public_id}"
            )
        ]
        if book.downloadable and self._conversion is not None:
            for choice in self._output_policy.choices():
                if self._output_policy.decision(
                    book.original_format, choice.key
                ) is not OutputDecision.CONVERT or not self._conversion.supports(
                    book.original_format, choice.key
                ):
                    continue
                token = await self._state.put_download(
                    message.chat.id, DownloadState(public_id, choice.key)
                )
                callback_data = f"c:{token}"
                if len(callback_data.encode()) > 64:
                    raise AssertionError("Callback state token exceeds Telegram's limit")
                buttons.append(InlineKeyboardButton(text=choice.label, callback_data=callback_data))
        markup = InlineKeyboardMarkup(inline_keyboard=[buttons]) if book.downloadable else None
        await message.answer(detail_text(book), reply_markup=markup)

    async def _page(self, callback: CallbackQuery, token: str) -> None:
        message = callback.message
        if (
            not token
            or len(token) > 40
            or message is None
            or isinstance(message, InaccessibleMessage)
        ):
            await callback.answer()
            return
        state = await self._state.get(token, message.chat.id)
        if state is None:
            await callback.answer("This search page has expired.", show_alert=True)
            return
        await callback.answer()
        try:
            page = await self._catalog.browse(
                CatalogRequest(query=state.query, cursor=state.cursor, page_size=10)
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.warning(f"Telegram pagination failed failure_type={type(error).__name__}")
            await message.answer("Search is temporarily unavailable.")
            return
        markup = await self._result_markup(message.chat.id, state.query, page)
        await message.edit_text(results_text(page.books), reply_markup=markup)

    async def _converted_callback(self, callback: CallbackQuery, token: str) -> None:
        message = callback.message
        if (
            not token
            or len(token) > 40
            or message is None
            or isinstance(message, InaccessibleMessage)
        ):
            await callback.answer()
            return
        state = await self._state.get_download(token, message.chat.id)
        if state is None or not _valid_public_id(state.public_id) or self._conversion is None:
            await callback.answer("This format choice is unavailable or expired.", show_alert=True)
            return
        try:
            choice = self._output_policy.choice(state.target_format)
        except ValueError:
            await callback.answer("This format choice is unavailable or expired.", show_alert=True)
            return
        if choice.key == "original":
            await callback.answer("This format choice is unavailable or expired.", show_alert=True)
            return
        await callback.answer()
        await self._download_conversion(message, state.public_id, choice.key)

    async def _download_original(self, callback: CallbackQuery, public_id: str) -> None:
        message = callback.message
        if message is None or isinstance(message, InaccessibleMessage):
            return
        try:
            description = await self._acquisition.describe(public_id)
            if description.content_length > _UPLOAD_LIMIT:
                await message.answer("This file is too large to send through Telegram.")
                return
            acquired = await self._acquisition.acquire(public_id)
            if acquired.content_length > _UPLOAD_LIMIT:
                await self._close_stream(acquired.stream, acquired.filename)
                await message.answer("This file is too large to send through Telegram.")
                return
            mismatch = _description_mismatch(description, acquired)
            if mismatch is not None:
                _LOGGER.warning(
                    f"Telegram acquisition integrity check failed surface=telegram "
                    f"phase=acquisition failure_type={mismatch}"
                )
                await self._close_stream(acquired.stream, acquired.filename)
                await message.answer("The original file is currently unavailable.")
                return
            await self._send_document(
                message,
                acquired.stream,
                acquired.filename,
                description.title,
            )
        except asyncio.CancelledError:
            raise
        except AcquisitionError as error:
            if not isinstance(
                error,
                (
                    AcquisitionNotFoundError,
                    AcquisitionUnavailableError,
                    AcquisitionMemberNotFoundError,
                ),
            ):
                _LOGGER.warning(
                    f"Telegram acquisition failed surface=telegram phase=acquisition "
                    f"failure_type={type(error).__name__}"
                )
            await message.answer("The original file is currently unavailable.")
        except Exception as error:
            _LOGGER.warning(
                f"Telegram acquisition failed surface=telegram phase=acquisition "
                f"failure_type={type(error).__name__}"
            )
            await message.answer("The original file is currently unavailable.")

    async def _download_conversion(
        self, message: Message, public_id: str, target_format: str
    ) -> None:
        conversion = self._conversion
        if conversion is None:
            await message.answer("This format is unavailable.")
            return
        try:
            book = await self._catalog.details(public_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.warning(
                f"Telegram conversion metadata failed surface=telegram phase=metadata "
                f"failure_type={type(error).__name__}"
            )
            await message.answer("The source file is currently unavailable.")
            return
        if book is None or not book.downloadable:
            await message.answer("The source file is currently unavailable.")
            return
        if not conversion.supports(book.original_format, target_format):
            await message.answer("This format is unavailable.")
            return
        try:
            result = await conversion.convert(public_id, target_format)
        except asyncio.CancelledError:
            raise
        except SourceUnavailableError:
            await message.answer("The source file is currently unavailable.")
            return
        except UnsupportedConversionError:
            await message.answer("This format is unavailable.")
            return
        except ConversionTimeoutError as error:
            _LOGGER.warning(
                f"Telegram conversion timed out surface=telegram phase=conversion "
                f"failure_type={type(error).__name__}"
            )
            await message.answer("Conversion timed out.")
            return
        except (
            ConversionSourceError,
            ConverterExecutionError,
            InvalidConversionOutputError,
            SourceChangedError,
            ConversionShutdownError,
        ) as error:
            _LOGGER.warning(
                f"Telegram conversion failed surface=telegram phase=conversion "
                f"failure_type={type(error).__name__}"
            )
            await message.answer("Conversion failed.")
            return
        except Exception as error:
            _LOGGER.warning(
                f"Telegram conversion failed surface=telegram phase=conversion "
                f"failure_type={type(error).__name__}"
            )
            await message.answer("Conversion failed.")
            return

        if result.content_length > _UPLOAD_LIMIT:
            await self._close_stream(result.stream, result.filename)
            await message.answer("This file is too large to send through Telegram.")
            return
        await self._send_document(
            message,
            result.stream,
            result.filename,
            book.title,
        )

    async def _send_document(
        self,
        message: Message,
        stream: AsyncByteStream,
        filename: str,
        title: str,
    ) -> None:
        upload = StreamingInputFile(stream, safe_filename(filename))
        try:
            await message.answer_document(
                upload,
                caption=truncate(sanitize(title), 1_024) or "Book",
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.warning(
                f"Telegram upload failed surface=telegram phase=upload "
                f"failure_type={type(error).__name__}"
            )
            await message.answer("Telegram could not send this file.")
        finally:
            try:
                await upload.aclose()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _LOGGER.warning(
                    f"Telegram upload cleanup failed surface=telegram phase=cleanup "
                    f"failure_type={type(error).__name__}"
                )

    async def _close_stream(self, stream: AsyncByteStream, filename: str) -> None:
        upload = StreamingInputFile(stream, safe_filename(filename))
        try:
            await upload.aclose()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.warning(
                f"Telegram artifact cleanup failed surface=telegram phase=cleanup "
                f"failure_type={type(error).__name__}"
            )

    async def _result_markup(
        self,
        chat_id: int,
        query: str,
        page: CatalogPage,
    ) -> InlineKeyboardMarkup | None:
        rows = [
            [
                InlineKeyboardButton(
                    text=button_label(book.title), callback_data=f"d:{book.public_id}"
                )
            ]
            for book in page.books
            if _valid_public_id(book.public_id)
        ]
        if page.next_cursor is not None:
            token = await self._state.put(chat_id, PageState(query=query, cursor=page.next_cursor))
            rows.append([InlineKeyboardButton(text="Next page", callback_data=f"p:{token}")])
        return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _normalized_source_format(value: str) -> str:
    return value.strip().removeprefix(".").casefold()


def _description_mismatch(
    description: OriginalDescription, acquired: AcquiredOriginal
) -> str | None:
    if acquired.content_length != description.content_length:
        return "size_mismatch"
    if acquired.source_revision != description.revision or _normalized_source_format(
        acquired.source_format
    ) != _normalized_source_format(description.source_format):
        return "metadata_mismatch"
    return None


def _valid_public_id(value: str) -> bool:
    return _PUBLIC_ID.fullmatch(value) is not None and len(value.encode()) + 2 <= 64
