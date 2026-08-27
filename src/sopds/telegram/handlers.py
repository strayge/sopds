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
    OriginalDescription,
)
from sopds.catalog.contracts import Catalog, CatalogPage, CatalogRequest
from sopds.telegram.formatting import (
    button_label,
    detail_text,
    results_text,
    safe_filename,
    sanitize,
    truncate,
)
from sopds.telegram.state import CallbackStateStore, PageState
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
    ) -> None:
        self._catalog = catalog
        self._acquisition = acquisition
        self._state = state

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

        await callback.answer()
        if prefix not in {"d", "x"} or not _valid_public_id(value):
            return
        if prefix == "d":
            await self._detail(callback, value)
        else:
            await self._download(callback, value)

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
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Download original", callback_data=f"x:{public_id}")]
            ]
        )
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

    async def _download(self, callback: CallbackQuery, public_id: str) -> None:
        message = callback.message
        if message is None or isinstance(message, InaccessibleMessage):
            return
        upload: StreamingInputFile | None = None
        primary: BaseException | None = None
        try:
            description = await self._acquisition.describe(public_id)
            if description.content_length > _UPLOAD_LIMIT:
                await message.answer("This file is too large to send through Telegram.")
                return
            acquired = await self._acquisition.acquire(public_id)
            if acquired.content_length > _UPLOAD_LIMIT:
                await acquired.stream.aclose()
                await message.answer("This file is too large to send through Telegram.")
                return
            mismatch = _description_mismatch(description, acquired)
            if mismatch is not None:
                _LOGGER.warning(
                    f"Telegram acquisition integrity check failed surface=telegram "
                    f"phase=acquisition failure_type={mismatch}"
                )
                await acquired.stream.aclose()
                await message.answer("The original file is currently unavailable.")
                return
            upload = StreamingInputFile(acquired.stream, safe_filename(acquired.filename))
            await message.answer_document(
                upload,
                caption=truncate(sanitize(description.title), 1_024) or "Book",
            )
        except asyncio.CancelledError as error:
            primary = error
            raise
        except AcquisitionError as error:
            primary = error
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
            primary = error
            _LOGGER.warning(
                f"Telegram upload failed surface=telegram phase=upload "
                f"failure_type={type(error).__name__}"
            )
            await message.answer("The original file is currently unavailable.")
        finally:
            if upload is not None:
                try:
                    await upload.aclose()
                except BaseException:
                    if primary is None:
                        raise
                    _LOGGER.warning(
                        f"Telegram upload cleanup failed surface=telegram phase=cleanup "
                        f"failure_type={type(primary).__name__}"
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
