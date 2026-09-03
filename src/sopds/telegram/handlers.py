"""Telegram message and callback handlers over database-free service contracts."""

import asyncio
import logging
import re
from typing import Any, cast

from telegram import (
    CallbackQuery,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
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
from sopds.catalog.contracts import Catalog, CatalogBook, CatalogRequest, SearchField
from sopds.catalog.search import normalize_text
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
from sopds.conversion.policy import OUTPUT_POLICY, OutputPolicy
from sopds.conversion.service import ConversionService
from sopds.telegram.formatting import (
    catalog_id_from_command,
    detail_text,
    results_text,
    safe_filename,
    sanitize,
    source_format_label,
    truncate,
)
from sopds.telegram.state import CallbackStateStore, DownloadState, PageState
from sopds.telegram.upload import StagedInputFile, close_stream

_LOGGER = logging.getLogger(__name__)
_PUBLIC_ID = re.compile(r"[A-Za-z0-9._~-]{1,62}\Z")
_BOOK_ID_PATTERN = r"[1-9][0-9]{0,18}"
_BOOK_COMMAND = re.compile(rf"/b{_BOOK_ID_PATTERN}\Z")
_AUTHOR_COMMAND = re.compile(rf"/a({_BOOK_ID_PATTERN})\Z")
_SERIES_COMMAND = re.compile(rf"/s({_BOOK_ID_PATTERN})\Z")
_LINKED_SEARCH_COMMAND = re.compile(rf"/[as]{_BOOK_ID_PATTERN}\Z")
_UPLOAD_LIMIT = 50 * 1024 * 1024
_SEARCH_LIMIT = 100
_SEARCH_PAGE_SIZE = 10
_NATURAL_PARTS = re.compile(r"(\d+)")


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

    def register(self, application: Application[Any, Any, Any, Any, Any, Any]) -> None:
        application.add_handler(CommandHandler("start", self._dispatch_start))
        application.add_handler(MessageHandler(filters.Regex(_BOOK_COMMAND), self._dispatch_book))
        application.add_handler(
            MessageHandler(filters.Regex(_LINKED_SEARCH_COMMAND), self._dispatch_linked_search)
        )
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._dispatch_plain_text)
        )
        application.add_handler(CallbackQueryHandler(self._dispatch_callback))

    async def _dispatch_start(
        self, update: Update, context: CallbackContext[Any, Any, Any, Any]
    ) -> None:
        del context
        if update.message is not None:
            await self.on_start_command(update.message)

    async def _dispatch_plain_text(
        self, update: Update, context: CallbackContext[Any, Any, Any, Any]
    ) -> None:
        del context
        if update.message is not None:
            await self.on_plain_text(update.message)

    async def _dispatch_book(
        self, update: Update, context: CallbackContext[Any, Any, Any, Any]
    ) -> None:
        del context
        if update.message is not None:
            await self.on_book_command(update.message)

    async def _dispatch_linked_search(
        self, update: Update, context: CallbackContext[Any, Any, Any, Any]
    ) -> None:
        del context
        if update.message is not None:
            await self.on_linked_search_command(update.message)

    async def _dispatch_callback(
        self, update: Update, context: CallbackContext[Any, Any, Any, Any]
    ) -> None:
        del context
        if update.callback_query is not None:
            await self.on_callback(update.callback_query)

    async def on_start_command(self, message: Message) -> None:
        await message.reply_text(f"i'm here. your id is {message.chat.id}")

    async def on_plain_text(self, message: Message) -> None:
        text = message.text
        if text is None:
            return
        query = text.strip()
        if query:
            await self._search_message(message, query)

    async def on_book_command(self, message: Message) -> None:
        book_id = catalog_id_from_command(message.text or "", "b")
        if book_id is not None:
            await self._send_detail_by_id(message, book_id)

    async def on_linked_search_command(self, message: Message) -> None:
        text = message.text or ""
        author_match = _AUTHOR_COMMAND.fullmatch(text)
        series_match = _SERIES_COMMAND.fullmatch(text)
        match = author_match or series_match
        if match is None:
            return
        entity_id = catalog_id_from_command(text, "a" if author_match is not None else "s")
        if entity_id is None:
            return
        try:
            query = (
                await self._catalog.author_name_by_id(entity_id)
                if author_match is not None
                else await self._catalog.series_name_by_id(entity_id)
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.warning(f"Telegram linked search failed failure_type={type(error).__name__}")
            await message.reply_text("Search is temporarily unavailable.")
            return
        if query is None:
            await message.reply_text("Search link is no longer available.")
            return
        search_field = SearchField.AUTHOR if author_match is not None else SearchField.SERIES
        await self._search_message(message, query, search_field)

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

    async def _search_message(
        self, message: Message, query: str, search_field: SearchField = SearchField.ALL
    ) -> None:
        try:
            books, page, page_count = await self._search_page(query, search_field, 0)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.warning(f"Telegram search failed failure_type={type(error).__name__}")
            await message.reply_text("Search is temporarily unavailable.")
            return
        markup = await self._result_markup(message.chat.id, query, search_field, page, page_count)
        await message.reply_text(
            results_text(books), reply_markup=markup, parse_mode=ParseMode.HTML
        )

    async def _detail(self, callback: CallbackQuery, public_id: str) -> None:
        message = callback.message
        if message is None or isinstance(message, InaccessibleMessage):
            return
        await self._send_detail(cast(Message, message), public_id)

    async def _send_detail(self, message: Message, public_id: str) -> None:
        try:
            book = await self._catalog.details(public_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.warning(f"Telegram detail failed failure_type={type(error).__name__}")
            await message.reply_text("Book details are temporarily unavailable.")
            return
        await self._reply_with_detail(message, book)

    async def _send_detail_by_id(self, message: Message, book_id: int) -> None:
        try:
            book = await self._catalog.details_by_id(book_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.warning(f"Telegram detail failed failure_type={type(error).__name__}")
            await message.reply_text("Book details are temporarily unavailable.")
            return
        await self._reply_with_detail(message, book)

    async def _reply_with_detail(self, message: Message, book: CatalogBook | None) -> None:
        if book is None:
            await message.reply_text("Book is no longer available.")
            return
        public_id = book.public_id
        buttons = [
            InlineKeyboardButton(
                text=source_format_label(book.original_format), callback_data=f"x:{public_id}"
            )
        ]
        if book.downloadable and self._conversion is not None:
            for choice in self._output_policy.available_conversions(
                book.original_format, self._conversion.supports
            ):
                token = await self._state.put_download(
                    message.chat.id, DownloadState(public_id, choice.key)
                )
                callback_data = f"c:{token}"
                if len(callback_data.encode()) > 64:
                    raise AssertionError("Callback state token exceeds Telegram's limit")
                buttons.append(InlineKeyboardButton(text=choice.label, callback_data=callback_data))
        markup = InlineKeyboardMarkup(inline_keyboard=[buttons]) if book.downloadable else None
        await message.reply_text(detail_text(book), reply_markup=markup, parse_mode=ParseMode.HTML)

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
        message = cast(Message, message)
        state = await self._state.get(token, message.chat.id)
        if state is None:
            await callback.answer("This search page has expired.", show_alert=True)
            return
        await callback.answer()
        try:
            books, page, page_count = await self._search_page(
                state.query, state.search_field, state.page
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.warning(f"Telegram pagination failed failure_type={type(error).__name__}")
            await message.reply_text("Search is temporarily unavailable.")
            return
        markup = await self._result_markup(
            message.chat.id, state.query, state.search_field, page, page_count
        )
        await message.edit_text(results_text(books), reply_markup=markup, parse_mode=ParseMode.HTML)

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
        message = cast(Message, message)
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
        message = cast(Message, message)
        try:
            description = await self._acquisition.describe(public_id)
            if description.content_length > _UPLOAD_LIMIT:
                await message.reply_text("This file is too large to send through Telegram.")
                return
            acquired = await self._acquisition.acquire(public_id)
            if acquired.content_length > _UPLOAD_LIMIT:
                await self._close_stream(acquired.stream)
                await message.reply_text("This file is too large to send through Telegram.")
                return
            mismatch = _description_mismatch(description, acquired)
            if mismatch is not None:
                _LOGGER.warning(
                    f"Telegram acquisition integrity check failed surface=telegram "
                    f"phase=acquisition failure_type={mismatch}"
                )
                await self._close_stream(acquired.stream)
                await message.reply_text("The original file is currently unavailable.")
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
            await message.reply_text("The original file is currently unavailable.")
        except Exception as error:
            _LOGGER.warning(
                f"Telegram acquisition failed surface=telegram phase=acquisition "
                f"failure_type={type(error).__name__}"
            )
            await message.reply_text("The original file is currently unavailable.")

    async def _download_conversion(
        self, message: Message, public_id: str, target_format: str
    ) -> None:
        conversion = self._conversion
        if conversion is None:
            await message.reply_text("This format is unavailable.")
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
            await message.reply_text("The source file is currently unavailable.")
            return
        if book is None or not book.downloadable:
            await message.reply_text("The source file is currently unavailable.")
            return
        if not conversion.supports(book.original_format, target_format):
            await message.reply_text("This format is unavailable.")
            return
        try:
            result = await conversion.convert(public_id, target_format)
        except asyncio.CancelledError:
            raise
        except SourceUnavailableError:
            await message.reply_text("The source file is currently unavailable.")
            return
        except UnsupportedConversionError:
            await message.reply_text("This format is unavailable.")
            return
        except ConversionTimeoutError as error:
            _LOGGER.warning(
                f"Telegram conversion timed out surface=telegram phase=conversion "
                f"failure_type={type(error).__name__}"
            )
            await message.reply_text("Conversion timed out.")
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
            await message.reply_text("Conversion failed.")
            return
        except Exception as error:
            _LOGGER.warning(
                f"Telegram conversion failed surface=telegram phase=conversion "
                f"failure_type={type(error).__name__}"
            )
            await message.reply_text("Conversion failed.")
            return

        if result.content_length > _UPLOAD_LIMIT:
            await self._close_stream(result.stream)
            await message.reply_text("This file is too large to send through Telegram.")
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
        upload: StagedInputFile | None = None
        try:
            upload = await StagedInputFile.create(stream, safe_filename(filename))
            if upload.input_file is None:
                raise AssertionError("Staged Telegram upload has no input file")
            await message.reply_document(
                upload.input_file,
                caption=truncate(sanitize(title), 1_024) or "Book",
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.warning(
                f"Telegram upload failed surface=telegram phase=upload "
                f"failure_type={type(error).__name__}"
            )
            await message.reply_text("Telegram could not send this file.")
        finally:
            if upload is not None:
                try:
                    await upload.aclose()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    _LOGGER.warning(
                        f"Telegram upload cleanup failed surface=telegram phase=cleanup "
                        f"failure_type={type(error).__name__}"
                    )

    async def _close_stream(self, stream: AsyncByteStream) -> None:
        try:
            await close_stream(stream)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.warning(
                f"Telegram artifact cleanup failed surface=telegram phase=cleanup "
                f"failure_type={type(error).__name__}"
            )

    async def _search_page(
        self, query: str, search_field: SearchField, requested_page: int
    ) -> tuple[tuple[CatalogBook, ...], int, int]:
        result = await self._catalog.browse(
            CatalogRequest(query=query, search_field=search_field, page_size=_SEARCH_LIMIT)
        )
        ordered = tuple(sorted(result.books, key=_telegram_book_sort_key))
        page_count = max(1, (len(ordered) + _SEARCH_PAGE_SIZE - 1) // _SEARCH_PAGE_SIZE)
        page = min(max(requested_page, 0), page_count - 1)
        offset = page * _SEARCH_PAGE_SIZE
        return ordered[offset : offset + _SEARCH_PAGE_SIZE], page, page_count

    async def _result_markup(
        self,
        chat_id: int,
        query: str,
        search_field: SearchField,
        page: int,
        page_count: int,
    ) -> InlineKeyboardMarkup | None:
        controls: list[InlineKeyboardButton] = []
        if page > 0:
            token = await self._state.put(
                chat_id,
                PageState(query=query, page=page - 1, search_field=search_field),
            )
            controls.append(InlineKeyboardButton(text="←", callback_data=f"p:{token}"))
        if page + 1 < page_count:
            token = await self._state.put(
                chat_id,
                PageState(query=query, page=page + 1, search_field=search_field),
            )
            controls.append(InlineKeyboardButton(text="→", callback_data=f"p:{token}"))
        return InlineKeyboardMarkup(inline_keyboard=[controls]) if controls else None


def _natural_sort_key(value: str) -> tuple[str, ...]:
    parts: list[str] = []
    for part in _NATURAL_PARTS.split(normalize_text(value)):
        if part.isdecimal():
            number = str(int(part))
            parts.append(f"1{len(number):04d}:{number}:{len(part):04d}")
        else:
            parts.append(f"0{part}")
    return tuple(parts)


def _telegram_book_sort_key(
    book: CatalogBook,
) -> tuple[
    bool,
    str,
    bool,
    str,
    bool,
    tuple[str, ...],
    tuple[str, ...],
    str,
]:
    first_author = " ".join(book.authors[0].replace(",", " ").split()) if book.authors else ""
    author_sort = normalize_text(first_author)
    series_sort = normalize_text(book.series or "")
    series_number = book.series_number or "" if series_sort else ""
    return (
        not bool(author_sort),
        author_sort,
        not bool(series_sort),
        series_sort,
        not bool(series_number),
        _natural_sort_key(series_number),
        _natural_sort_key(book.title),
        book.public_id,
    )


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
