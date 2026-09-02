"""Network-free tests for Telegram authorization, state, rendering, and streaming."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import cast, override
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    Chat,
    InaccessibleMessage,
    Message,
    TelegramObject,
    Update,
    User,
)

from sopds.acquisition.contracts import (
    AcquiredOriginal,
    Acquisition,
    AcquisitionUnavailableError,
    AsyncByteStream,
    OriginalDescription,
    SourceRevision,
)
from sopds.catalog.contracts import (
    Catalog,
    CatalogBook,
    CatalogPage,
    CatalogRequest,
)
from sopds.config import TelegramConfig
from sopds.conversion.contracts import (
    ConversionResult,
    ConversionSourceError,
    ConversionTimeoutError,
    SourceUnavailableError,
    UnsupportedConversionError,
)
from sopds.conversion.policy import OUTPUT_POLICY
from sopds.conversion.service import ConversionService
from sopds.telegram.formatting import (
    button_label,
    results_text,
    sanitize,
    truncate,
    utf16_length,
)
from sopds.telegram.handlers import TelegramHandlers
from sopds.telegram.middleware import ActiveUpdateTracker, AllowlistMiddleware
from sopds.telegram.runner import TelegramRunner
from sopds.telegram.state import CallbackStateStore, DownloadState, PageState
from sopds.telegram.upload import StreamingInputFile


def _message(chat_id: int, text: str = "search") -> Message:
    return Message(
        message_id=1,
        date=datetime(1970, 1, 1, tzinfo=UTC),
        chat=Chat(id=chat_id, type="private" if chat_id > 0 else "group"),
        from_user=User(id=2, is_bot=False, first_name="User"),
        text=text,
    )


async def test_allowlist_filters_private_group_callback_and_unknown_updates() -> None:
    middleware = AllowlistMiddleware((10, -20))
    handled: list[int] = []

    async def handler(event: object, data: dict[str, object]) -> None:
        del data
        handled.append(cast(Update, event).update_id)

    allowed_private = Update(update_id=1, message=_message(10))
    allowed_group = Update(update_id=2, message=_message(-20))
    denied = Update(update_id=3, message=_message(11))
    callback = CallbackQuery(
        id="callback",
        from_user=User(id=2, is_bot=False, first_name="User"),
        chat_instance="instance",
        message=_message(-20),
        data="d:book",
    )
    await middleware(handler, allowed_private, {})
    await middleware(handler, allowed_group, {})
    await middleware(handler, denied, {})
    await middleware(handler, Update(update_id=4, callback_query=callback), {})
    await middleware(handler, Update(update_id=5), {})

    assert handled == [1, 2, 4]


async def test_callback_state_ttl_lru_binding_opacity_and_concurrency() -> None:
    now = 100.0
    store = CallbackStateStore(ttl_seconds=10, max_entries=2, clock=lambda: now)
    first = await store.put(1, PageState("secret title", "signed cursor"))
    second = await store.put(-2, PageState("other", "cursor 2"))
    assert "secret" not in first
    assert len(f"p:{first}".encode()) < 64
    assert await store.get(first, -2) is None
    assert await store.get(first, 1) == PageState("secret title", "signed cursor")

    third = await store.put(3, PageState("third", "cursor 3"))
    assert await store.get(second, -2) is None
    assert await store.get(third, 3) is not None
    tokens = await asyncio.gather(
        *(store.put(4, PageState(str(index), str(index))) for index in range(20))
    )
    assert len(set(tokens)) == 20
    now = 111.0
    assert await store.get(tokens[-1], 4) is None


async def test_download_callback_state_is_typed_chat_bound_expiring_and_compact() -> None:
    now = 10.0
    store = CallbackStateStore(ttl_seconds=5, clock=lambda: now)
    token = await store.put_download(10, DownloadState("x" * 62, "azw3"))

    assert len(f"c:{token}".encode()) <= 64
    assert await store.get(token, 10) is None
    assert await store.get_download(token, 11) is None
    assert await store.get_download(token, 10) == DownloadState("x" * 62, "azw3")

    now = 15.0
    assert await store.get_download(token, 10) is None


def test_plain_text_formatting_normalizes_controls_and_bounds_output() -> None:
    hostile = "\uff1cb\uff1etitle\uff1c/b\uff1e\x00\u200b" + "x" * 5_000
    cleaned = sanitize(hostile)
    assert cleaned.startswith("<b>title</b>")
    assert "\x00" not in cleaned
    assert "\u200b" not in cleaned
    assert len(cleaned) <= 512
    assert len(button_label(hostile)) <= 64

    books = tuple(
        CatalogBook(
            public_id=f"book-{index}",
            title=hostile,
            authors=(hostile,),
            series=None,
            series_number=None,
            language="en",
            original_format="fb2",
        )
        for index in range(10)
    )
    assert len(results_text(books)) <= 4_096


class _Stream:
    def __init__(self, *, fail: bool = False) -> None:
        self.closed = 0
        self.fail = fail

    async def _iterate(self) -> AsyncIterator[bytes]:
        yield b"one"
        if self.fail:
            raise OSError("read failed")
        yield b"two"

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iterate()

    async def aclose(self) -> None:
        self.closed += 1


async def test_streaming_input_file_yields_chunks_and_closes_once() -> None:
    stream = _Stream()
    upload = StreamingInputFile(cast(AsyncByteStream, stream), "book.fb2")
    chunks = [chunk async for chunk in upload.read(cast(Bot, object()))]
    await upload.aclose()
    assert chunks == [b"one", b"two"]
    assert stream.closed == 1


async def test_streaming_input_file_closes_after_read_failure() -> None:
    stream = _Stream(fail=True)
    upload = StreamingInputFile(cast(AsyncByteStream, stream), "book.fb2")
    try:
        _ = [chunk async for chunk in upload.read(cast(Bot, object()))]
    except OSError:
        pass
    else:
        raise AssertionError("stream failure was not propagated")
    assert stream.closed == 1


async def test_streaming_close_has_one_persistent_task_under_repeated_cancellation() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingCloseStream(_Stream):
        @override
        async def aclose(self) -> None:
            self.closed += 1
            started.set()
            await release.wait()

    stream = BlockingCloseStream()
    upload = StreamingInputFile(cast(AsyncByteStream, stream), "book.fb2")
    first = asyncio.create_task(upload.aclose())
    second = asyncio.create_task(upload.aclose())
    await started.wait()
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert stream.closed == 1


async def test_unauthorized_callback_does_not_answer() -> None:
    middleware = AllowlistMiddleware((10,))
    callback = CallbackQuery(
        id="callback",
        from_user=User(id=2, is_bot=False, first_name="User"),
        chat_instance="instance",
        message=_message(-20),
        data="d:book",
    )
    answer = AsyncMock()

    async def handler(event: object, data: dict[str, object]) -> None:
        del event, data
        await answer()

    await middleware(handler, Update(update_id=1, callback_query=callback), {})
    answer.assert_not_awaited()


@dataclass
class _Chat:
    id: int


class _FakeMessage:
    def __init__(
        self,
        text: str | None = None,
        chat_id: int = 10,
        *,
        fail_document: bool = False,
        block_document: bool = False,
    ) -> None:
        self.text = text
        self.chat = _Chat(chat_id)
        self.fail_document = fail_document
        self.document_started = asyncio.Event()
        self.document_release = asyncio.Event()
        if not block_document:
            self.document_release.set()
        self.answers: list[tuple[str, object | None]] = []
        self.edits: list[tuple[str, object | None]] = []
        self.documents: list[tuple[StreamingInputFile, str]] = []

    async def answer(self, text: str, *, reply_markup: object | None = None) -> None:
        self.answers.append((text, reply_markup))

    async def edit_text(self, text: str, *, reply_markup: object | None = None) -> None:
        self.edits.append((text, reply_markup))

    async def answer_document(self, document: StreamingInputFile, *, caption: str) -> None:
        self.documents.append((document, caption))
        if self.fail_document:
            raise OSError("send failed")
        self.document_started.set()
        await self.document_release.wait()
        _ = [chunk async for chunk in document.read(cast(Bot, object()))]


class _FakeCallback:
    def __init__(self, data: str | None, message: object | None) -> None:
        self.data = data
        self.message = message
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


class _FakeCatalog:
    def __init__(
        self,
        pages: list[CatalogPage],
        detail: CatalogBook | None = None,
        *,
        browse_error: Exception | None = None,
    ) -> None:
        self.pages = pages
        self.detail = detail
        self.browse_error = browse_error
        self.requests: list[CatalogRequest] = []
        self.detail_ids: list[str] = []

    async def browse(self, request: CatalogRequest) -> CatalogPage:
        self.requests.append(request)
        if self.browse_error is not None:
            raise self.browse_error
        return self.pages.pop(0)

    async def details(self, public_id: str) -> CatalogBook | None:
        self.detail_ids.append(public_id)
        return self.detail


class _FakeAcquisition:
    def __init__(
        self,
        size: int,
        stream: _Stream | None = None,
        acquired_size: int | None = None,
        *,
        acquired_revision: SourceRevision | None = None,
        acquired_format: str = "fb2",
    ) -> None:
        self.size = size
        self.stream = stream or _Stream()
        self.acquired_size = size if acquired_size is None else acquired_size
        self.acquired_revision = acquired_revision or SourceRevision(1, 2, 3)
        self.acquired_format = acquired_format
        self.describe_calls = 0
        self.acquire_calls = 0

    async def describe(self, public_id: str) -> OriginalDescription:
        self.describe_calls += 1
        return OriginalDescription(
            public_id=public_id,
            title="<Book>\x00",
            source_format="fb2",
            content_length=self.size,
            revision=SourceRevision(1, 2, 3),
        )

    async def acquire(self, public_id: str) -> AcquiredOriginal:
        self.acquire_calls += 1
        return AcquiredOriginal(
            filename="../book.fb2",
            media_type="application/octet-stream",
            content_length=self.acquired_size,
            stream=cast(AsyncByteStream, self.stream),
            source_format=self.acquired_format,
            source_revision=self.acquired_revision,
        )


def _summary(index: int) -> CatalogBook:
    return CatalogBook(
        public_id=f"book-{index}",
        title=f"Book {index}",
        authors=("Author",),
        series=None,
        series_number=None,
        language="en",
        original_format="fb2",
    )


def _detail(source_format: str = "fb2") -> CatalogBook:
    return CatalogBook(
        public_id="book-0",
        title="Book 0",
        authors=("Author",),
        genres=(("sf", "Science fiction"),),
        series=None,
        series_number=None,
        size=10,
        libid=None,
        published_date=date(2020, 1, 1),
        language="en",
        original_format=source_format,
        rating=None,
        keywords=None,
    )


class _FakeConversion:
    def __init__(
        self,
        *,
        supported: set[tuple[str, str]] | None = None,
        content_length: int = 9,
        error: Exception | None = None,
    ) -> None:
        self.supported = supported or {("fb2", "epub"), ("fb2", "azw3")}
        self.content_length = content_length
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self.stream = _Stream()

    def supports(self, source_format: str, target_format: str) -> bool:
        source = source_format.strip().removeprefix(".").casefold()
        return (source, target_format.casefold()) in self.supported

    async def convert(self, public_id: str, target_format: str) -> ConversionResult:
        self.calls.append((public_id, target_format))
        if self.error is not None:
            raise self.error
        return ConversionResult(
            filename=f"Converted.{target_format}",
            media_type=(
                "application/epub+zip"
                if target_format == "epub"
                else "application/vnd.amazon.ebook"
            ),
            content_length=self.content_length,
            stream=cast(AsyncByteStream, self.stream),
        )


async def test_handlers_start_plain_text_search_detail_and_pagination() -> None:
    first = CatalogPage(tuple(_summary(index) for index in range(10)), "cursor")
    second = CatalogPage((_summary(10),), None)
    catalog = _FakeCatalog([first, second], _detail())
    handlers = TelegramHandlers(
        cast(Catalog, catalog),
        cast(Acquisition, _FakeAcquisition(1)),
        CallbackStateStore(),
    )

    start_message = _FakeMessage("/start", chat_id=-20)
    await handlers.on_start_command(cast(Message, start_message))
    assert start_message.answers == [("i'm here. your id is -20", None)]

    search_message = _FakeMessage("terms")
    await handlers.on_plain_text(cast(Message, search_message))
    assert catalog.requests[0].query == "terms"
    assert catalog.requests[0].page_size == 10
    assert len(search_message.answers) == 1
    markup = search_message.answers[0][1]
    assert markup is not None
    keyboard = markup.inline_keyboard  # type: ignore[attr-defined]
    assert len(keyboard) == 11
    assert all(len(button.callback_data.encode()) <= 64 for row in keyboard for button in row)

    detail_message = _FakeMessage()
    detail_callback = _FakeCallback("d:book-0", detail_message)
    await handlers.on_callback(cast(CallbackQuery, detail_callback))
    assert detail_callback.answers == [(None, False)]
    assert len(detail_message.answers) == 1
    detail_markup = detail_message.answers[0][1]
    assert detail_markup is not None
    assert len(detail_markup.inline_keyboard) == 1  # type: ignore[attr-defined]
    assert detail_markup.inline_keyboard[0][0].text == "FB2"  # type: ignore[attr-defined]

    page_data = keyboard[-1][0].callback_data
    page_message = _FakeMessage()
    page_callback = _FakeCallback(page_data, page_message)
    await handlers.on_callback(cast(CallbackQuery, page_callback))
    assert page_callback.answers == [(None, False)]
    assert catalog.requests[-1].cursor == "cursor"
    assert len(page_message.edits) == 1


async def test_expired_pagination_alerts_without_catalog_use() -> None:
    catalog = _FakeCatalog([])
    handlers = TelegramHandlers(
        cast(Catalog, catalog),
        cast(Acquisition, _FakeAcquisition(1)),
        CallbackStateStore(),
    )
    callback = _FakeCallback("p:expired", _FakeMessage())
    await handlers.on_callback(cast(CallbackQuery, callback))
    assert callback.answers == [("This search page has expired.", True)]
    assert catalog.requests == []


@pytest.mark.parametrize("size", [50 * 1024 * 1024, 50 * 1024 * 1024 + 1])
async def test_download_limit_checks_before_open(size: int) -> None:
    acquisition = _FakeAcquisition(size)
    handlers = TelegramHandlers(
        cast(Catalog, _FakeCatalog([])),
        cast(Acquisition, acquisition),
        CallbackStateStore(),
    )
    message = _FakeMessage()
    callback = _FakeCallback("x:book-0", message)
    await handlers.on_callback(cast(CallbackQuery, callback))
    assert callback.answers == [(None, False)]
    if size == 50 * 1024 * 1024:
        assert acquisition.acquire_calls == 1
        assert len(message.documents) == 1
        assert acquisition.stream.closed == 1
    else:
        assert acquisition.acquire_calls == 0
        assert message.documents == []
        assert "too large" in message.answers[0][0]
        assert "http" not in message.answers[0][0].lower()


async def test_cancelled_send_closes_stream_before_propagating() -> None:
    acquisition = _FakeAcquisition(1)
    handlers = TelegramHandlers(
        cast(Catalog, _FakeCatalog([])),
        cast(Acquisition, acquisition),
        CallbackStateStore(),
    )
    message = _FakeMessage(block_document=True)
    task = asyncio.create_task(
        handlers.on_callback(cast(CallbackQuery, _FakeCallback("x:book-0", message)))
    )
    await message.document_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert acquisition.stream.closed == 1


async def test_send_failure_closes_stream_and_reports_upload_failure() -> None:
    acquisition = _FakeAcquisition(1)
    handlers = TelegramHandlers(
        cast(Catalog, _FakeCatalog([])),
        cast(Acquisition, acquisition),
        CallbackStateStore(),
    )
    message = _FakeMessage(fail_document=True)
    await handlers.on_callback(cast(CallbackQuery, _FakeCallback("x:book-0", message)))
    assert acquisition.stream.closed == 1
    assert message.answers[-1][0] == "Telegram could not send this file."


class _UnavailableAcquisition:
    async def describe(self, public_id: str) -> OriginalDescription:
        del public_id
        raise AcquisitionUnavailableError

    async def acquire(self, public_id: str) -> AcquiredOriginal:
        del public_id
        raise AssertionError("acquire must not follow a failed description")


async def test_acquisition_failure_is_user_safe() -> None:
    handlers = TelegramHandlers(
        cast(Catalog, _FakeCatalog([])),
        cast(Acquisition, _UnavailableAcquisition()),
        CallbackStateStore(),
    )
    message = _FakeMessage()
    await handlers.on_callback(cast(CallbackQuery, _FakeCallback("x:book-0", message)))
    assert message.answers == [("The original file is currently unavailable.", None)]


async def test_download_rechecks_size_after_open_and_closes() -> None:
    acquisition = _FakeAcquisition(1, acquired_size=50 * 1024 * 1024 + 1)
    handlers = TelegramHandlers(
        cast(Catalog, _FakeCatalog([])),
        cast(Acquisition, acquisition),
        CallbackStateStore(),
    )
    message = _FakeMessage()
    await handlers.on_callback(cast(CallbackQuery, _FakeCallback("x:book-0", message)))
    assert acquisition.acquire_calls == 1
    assert acquisition.stream.closed == 1
    assert message.documents == []


@pytest.mark.parametrize(
    ("source_format", "expected_labels"),
    [
        (".FB2", ["FB2", "EPUB", "AZW3"]),
        ("epub", ["EPUB", "AZW3"]),
        ("azw3", ["AZW3"]),
        ("pdf", ["PDF"]),
    ],
)
async def test_detail_keyboard_uses_source_label_and_registered_nonduplicate_formats(
    source_format: str, expected_labels: list[str]
) -> None:
    conversion = _FakeConversion(supported={("fb2", "epub"), ("fb2", "azw3"), ("epub", "azw3")})
    handlers = TelegramHandlers(
        cast(Catalog, _FakeCatalog([], _detail(source_format))),
        cast(Acquisition, _FakeAcquisition(1)),
        CallbackStateStore(),
        cast(ConversionService, conversion),
        OUTPUT_POLICY,
    )
    message = _FakeMessage()

    await handlers.on_callback(cast(CallbackQuery, _FakeCallback("d:book-0", message)))

    markup = message.answers[0][1]
    assert markup is not None
    buttons = markup.inline_keyboard[0]  # type: ignore[attr-defined]
    assert [button.text for button in buttons] == expected_labels
    assert all(len((button.callback_data or "").encode()) <= 64 for button in buttons)
    assert conversion.calls == []


async def test_converted_callback_uploads_result_without_source_size_preflight() -> None:
    state = CallbackStateStore()
    token = await state.put_download(10, DownloadState("book-0", "epub"))
    acquisition = _FakeAcquisition(100 * 1024 * 1024)
    conversion = _FakeConversion()
    handlers = TelegramHandlers(
        cast(Catalog, _FakeCatalog([], _detail())),
        cast(Acquisition, acquisition),
        state,
        cast(ConversionService, conversion),
    )
    message = _FakeMessage()

    await handlers.on_callback(cast(CallbackQuery, _FakeCallback(f"c:{token}", message)))

    assert conversion.calls == [("book-0", "epub")]
    assert acquisition.describe_calls == acquisition.acquire_calls == 0
    assert len(message.documents) == 1
    document, caption = message.documents[0]
    assert document.filename == "Converted.epub"
    assert caption == "Book 0"
    assert conversion.stream.closed == 1


async def test_converted_output_limit_uses_artifact_size_and_closes_immediately() -> None:
    state = CallbackStateStore()
    token = await state.put_download(10, DownloadState("book-0", "azw3"))
    conversion = _FakeConversion(content_length=50 * 1024 * 1024 + 1)
    handlers = TelegramHandlers(
        cast(Catalog, _FakeCatalog([], _detail())),
        cast(Acquisition, _FakeAcquisition(1)),
        state,
        cast(ConversionService, conversion),
    )
    message = _FakeMessage()

    await handlers.on_callback(cast(CallbackQuery, _FakeCallback(f"c:{token}", message)))

    assert conversion.stream.closed == 1
    assert message.documents == []
    assert message.answers == [("This file is too large to send through Telegram.", None)]


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (SourceUnavailableError(), "The source file is currently unavailable."),
        (UnsupportedConversionError(), "This format is unavailable."),
        (ConversionTimeoutError(), "Conversion timed out."),
        (ConversionSourceError(), "Conversion failed."),
    ],
)
async def test_converted_failures_have_distinct_safe_messages(
    error: Exception, message: str
) -> None:
    state = CallbackStateStore()
    token = await state.put_download(10, DownloadState("book-0", "epub"))
    conversion = _FakeConversion(error=error)
    handlers = TelegramHandlers(
        cast(Catalog, _FakeCatalog([], _detail())),
        cast(Acquisition, _FakeAcquisition(1)),
        state,
        cast(ConversionService, conversion),
    )
    telegram_message = _FakeMessage()

    await handlers.on_callback(cast(CallbackQuery, _FakeCallback(f"c:{token}", telegram_message)))

    assert telegram_message.answers == [(message, None)]
    assert telegram_message.documents == []


async def test_expired_or_cross_chat_conversion_token_is_rejected() -> None:
    state = CallbackStateStore()
    token = await state.put_download(11, DownloadState("book-0", "epub"))
    handlers = TelegramHandlers(
        cast(Catalog, _FakeCatalog([], _detail())),
        cast(Acquisition, _FakeAcquisition(1)),
        state,
        cast(ConversionService, _FakeConversion()),
    )
    callback = _FakeCallback(f"c:{token}", _FakeMessage(chat_id=10))

    await handlers.on_callback(cast(CallbackQuery, callback))

    assert callback.answers == [("This format choice is unavailable or expired.", True)]


async def test_converted_upload_failure_and_cancellation_close_artifacts() -> None:
    async def run(message: _FakeMessage) -> tuple[_FakeConversion, asyncio.Task[None]]:
        state = CallbackStateStore()
        token = await state.put_download(10, DownloadState("book-0", "epub"))
        conversion = _FakeConversion()
        handlers = TelegramHandlers(
            cast(Catalog, _FakeCatalog([], _detail())),
            cast(Acquisition, _FakeAcquisition(1)),
            state,
            cast(ConversionService, conversion),
        )
        task = asyncio.create_task(
            handlers.on_callback(cast(CallbackQuery, _FakeCallback(f"c:{token}", message)))
        )
        return conversion, task

    failed_conversion, failed_task = await run(_FakeMessage(fail_document=True))
    await failed_task
    assert failed_conversion.stream.closed == 1

    blocked_message = _FakeMessage(block_document=True)
    cancelled_conversion, cancelled_task = await run(blocked_message)
    await blocked_message.document_started.wait()
    cancelled_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_task
    assert cancelled_conversion.stream.closed == 1


async def test_command_filter_rejects_mentions_for_another_bot() -> None:
    bot = Bot("123456:secret")
    bot._me = User(id=123456, is_bot=True, first_name="SOPDS", username="sopds_bot")
    command = Command("start")

    assert await command(_message(10, "/start@other_bot"), bot) is False
    assert await command(_message(10, "/start@sopds_bot"), bot) is not False

    await bot.session.close()


@pytest.mark.parametrize(
    "data",
    [None, "malformed", "unknown:value", "d:", "x:bad id", "p:", "p:" + "x" * 41],
)
async def test_every_invalid_authorized_callback_is_answered_once(data: str | None) -> None:
    callback = _FakeCallback(data, _FakeMessage())
    handlers = TelegramHandlers(
        cast(Catalog, _FakeCatalog([])),
        cast(Acquisition, _FakeAcquisition(1)),
        CallbackStateStore(),
    )

    await handlers.on_callback(cast(CallbackQuery, callback))

    assert callback.answers == [(None, False)]


@pytest.mark.parametrize("data", ["d:book-0", "x:book-0", "p:token"])
async def test_inaccessible_callback_is_answered_once(data: str) -> None:
    inaccessible = InaccessibleMessage(chat=Chat(id=10, type="private"), message_id=1)
    callback = _FakeCallback(data, inaccessible)
    handlers = TelegramHandlers(
        cast(Catalog, _FakeCatalog([])),
        cast(Acquisition, _FakeAcquisition(1)),
        CallbackStateStore(),
    )

    await handlers.on_callback(cast(CallbackQuery, callback))

    assert callback.answers == [(None, False)]


async def test_pagination_failure_uses_message_after_single_acknowledgement() -> None:
    store = CallbackStateStore()
    token = await store.put(10, PageState("query", "cursor"))
    message = _FakeMessage()
    callback = _FakeCallback(f"p:{token}", message)
    handlers = TelegramHandlers(
        cast(Catalog, _FakeCatalog([], browse_error=RuntimeError("offline"))),
        cast(Acquisition, _FakeAcquisition(1)),
        store,
    )

    await handlers.on_callback(cast(CallbackQuery, callback))

    assert callback.answers == [(None, False)]
    assert message.answers == [("Search is temporarily unavailable.", None)]


def test_telegram_limits_count_utf16_code_units() -> None:
    assert truncate("a" * 62 + "😀x", 64) == "a" * 62 + "…"
    assert utf16_length(truncate("😀" * 3_000, 4_096)) <= 4_096
    assert utf16_length(button_label("😀" * 100)) <= 64
    assert utf16_length(sanitize("😀" * 1_000)) <= 512


@pytest.mark.parametrize(
    ("revision", "source_format"),
    [(SourceRevision(1, 2, 4), "fb2"), (SourceRevision(1, 2, 3), "epub")],
)
async def test_download_rejects_changed_source_identity_and_closes(
    revision: SourceRevision,
    source_format: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    acquisition = _FakeAcquisition(1, acquired_revision=revision, acquired_format=source_format)
    handlers = TelegramHandlers(
        cast(Catalog, _FakeCatalog([])),
        cast(Acquisition, acquisition),
        CallbackStateStore(),
    )
    message = _FakeMessage()

    await handlers.on_callback(cast(CallbackQuery, _FakeCallback("x:book-0", message)))

    assert acquisition.stream.closed == 1
    assert message.documents == []
    assert message.answers == [("The original file is currently unavailable.", None)]
    assert "failure_type=metadata_mismatch" in caplog.text
    assert "failure_type=size_mismatch" not in caplog.text


async def test_download_classifies_only_length_difference_as_size_mismatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    acquisition = _FakeAcquisition(2, acquired_size=1)
    handlers = TelegramHandlers(
        cast(Catalog, _FakeCatalog([])),
        cast(Acquisition, acquisition),
        CallbackStateStore(),
    )

    await handlers.on_callback(cast(CallbackQuery, _FakeCallback("x:book-0", _FakeMessage())))

    assert acquisition.stream.closed == 1
    assert "failure_type=size_mismatch" in caplog.text


async def test_tracker_closes_admission_and_cancels_active_update() -> None:
    tracker = ActiveUpdateTracker()
    started = asyncio.Event()
    invoked = 0

    async def handler(event: object, data: dict[str, object]) -> None:
        del event, data
        nonlocal invoked
        invoked += 1
        started.set()
        await asyncio.Event().wait()

    active = asyncio.create_task(tracker(handler, cast(TelegramObject, object()), {}))
    await started.wait()
    tracker.stop_accepting()
    await tracker(handler, cast(TelegramObject, object()), {})
    await tracker.cancel_and_wait()

    assert invoked == 1
    assert active.cancelled()


class _FakeSession:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _FakeBot:
    def __init__(self, *, delete_failures: int = 0) -> None:
        self.session = _FakeSession()
        self.delete_calls = 0
        self.delete_failures = delete_failures
        self.delete_attempted = asyncio.Event()

    async def delete_webhook(self, *, drop_pending_updates: bool) -> None:
        assert drop_pending_updates is True
        self.delete_calls += 1
        self.delete_attempted.set()
        if self.delete_calls <= self.delete_failures:
            raise OSError("offline")


class _FakeMiddlewareManager:
    def __init__(self) -> None:
        self.values: list[object] = []

    def outer_middleware(self, middleware: object) -> None:
        self.values.append(middleware)


class _FakeObserver:
    def __init__(self) -> None:
        self.outer = _FakeMiddlewareManager()

    def outer_middleware(self, middleware: object) -> None:
        self.outer.outer_middleware(middleware)


class _FakeDispatcher:
    def __init__(self, *, polling_failures: int = 0) -> None:
        self.update = _FakeObserver()
        self._handle_update_tasks: set[asyncio.Task[object]] = set()
        self.polling_started = asyncio.Event()
        self.polling_retried = asyncio.Event()
        self.polling_stopped = asyncio.Event()
        self.polling_arguments: dict[str, object] = {}
        self.polling_failures = polling_failures
        self.polling_calls = 0
        self.router_count = 0

    def include_router(self, router: object) -> None:
        del router
        self.router_count += 1

    async def start_polling(self, bot: object, **kwargs: object) -> None:
        del bot
        self.polling_arguments = kwargs
        self.polling_calls += 1
        self.polling_started.set()
        if self.polling_calls <= self.polling_failures:
            raise RuntimeError("polling failed")
        self.polling_retried.set()
        await self.polling_stopped.wait()

    async def stop_polling(self) -> None:
        self.polling_stopped.set()


async def test_runner_wires_conversion_and_output_policy_into_handlers() -> None:
    bot = _FakeBot()
    dispatcher = _FakeDispatcher()
    config = TelegramConfig.model_validate(
        {"enabled": True, "token": "123456:secret", "allowed_chat_ids": [10]}
    )
    catalog = cast(Catalog, _FakeCatalog([]))
    acquisition = cast(Acquisition, _FakeAcquisition(1))
    conversion = cast(ConversionService, _FakeConversion())

    with patch("sopds.telegram.runner.TelegramHandlers") as handlers_type:
        runner = TelegramRunner(
            config,
            catalog,
            acquisition,
            conversion,
            OUTPUT_POLICY,
            bot=cast(Bot, bot),
            dispatcher=cast(Dispatcher, dispatcher),
        )

    handlers_type.assert_called_once_with(
        catalog,
        acquisition,
        runner.state,
        conversion,
        OUTPUT_POLICY,
    )
    await runner.shutdown()


async def test_runner_drops_pending_once_and_uses_bounded_polling() -> None:
    bot = _FakeBot()
    dispatcher = _FakeDispatcher()
    config = TelegramConfig.model_validate(
        {"enabled": True, "token": "123456:secret", "allowed_chat_ids": [10, -20]}
    )
    runner = TelegramRunner(
        config,
        cast(Catalog, _FakeCatalog([])),
        cast(Acquisition, _FakeAcquisition(1)),
        bot=cast(Bot, bot),
        dispatcher=cast(Dispatcher, dispatcher),
    )
    runner.start()
    await asyncio.wait_for(dispatcher.polling_started.wait(), timeout=1)
    assert bot.delete_calls == 1
    assert dispatcher.polling_arguments == {
        "allowed_updates": ["message", "callback_query"],
        "handle_signals": False,
        "close_bot_session": False,
        "handle_as_tasks": True,
        "tasks_concurrency_limit": 4,
    }

    await runner.shutdown()
    await runner.shutdown()
    assert bot.session.close_calls == 1


async def test_runner_deletes_webhook_once_across_fatal_polling_retry() -> None:
    bot = _FakeBot()
    dispatcher = _FakeDispatcher(polling_failures=1)
    config = TelegramConfig.model_validate(
        {"enabled": True, "token": "123456:secret", "allowed_chat_ids": [10]}
    )
    runner = TelegramRunner(
        config,
        cast(Catalog, _FakeCatalog([])),
        cast(Acquisition, _FakeAcquisition(1)),
        bot=cast(Bot, bot),
        dispatcher=cast(Dispatcher, dispatcher),
    )
    runner.start()
    await asyncio.wait_for(dispatcher.polling_retried.wait(), timeout=2)

    assert dispatcher.polling_calls == 2
    assert bot.delete_calls == 1
    await runner.shutdown()


async def test_runner_resets_polling_delay_after_successful_recovery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = _FakeBot()

    class CyclingDispatcher(_FakeDispatcher):
        @override
        async def start_polling(self, bot: object, **kwargs: object) -> None:
            del bot, kwargs
            self.polling_calls += 1
            if self.polling_calls in {1, 3}:
                raise RuntimeError("polling failed")

    dispatcher = CyclingDispatcher()
    config = TelegramConfig.model_validate(
        {"enabled": True, "token": "123456:secret", "allowed_chat_ids": [10]}
    )
    runner = TelegramRunner(
        config,
        cast(Catalog, _FakeCatalog([])),
        cast(Acquisition, _FakeAcquisition(1)),
        bot=cast(Bot, bot),
        dispatcher=cast(Dispatcher, dispatcher),
    )
    delays: list[float] = []

    async def record_retry(delay: float) -> bool:
        delays.append(delay)
        if len(delays) == 3:
            runner._stop.set()
            return True
        return False

    runner._retry_delay = record_retry  # type: ignore[method-assign]
    caplog.set_level("INFO", logger="sopds.telegram.runner")
    await runner._run()

    assert delays == [1.0, 1.0, 1.0]
    assert "Telegram polling recovered" in caplog.text
    assert "Telegram polling cycle ended" in caplog.text
    assert caplog.text.count("Telegram polling stopped") == 1


async def test_runner_retries_initial_webhook_delete_and_shutdown_wakes_retry() -> None:
    bot = _FakeBot(delete_failures=10)
    dispatcher = _FakeDispatcher()
    config = TelegramConfig.model_validate(
        {"enabled": True, "token": "123456:secret", "allowed_chat_ids": [10]}
    )
    runner = TelegramRunner(
        config,
        cast(Catalog, _FakeCatalog([])),
        cast(Acquisition, _FakeAcquisition(1)),
        bot=cast(Bot, bot),
        dispatcher=cast(Dispatcher, dispatcher),
    )
    runner.start()
    await bot.delete_attempted.wait()

    await asyncio.wait_for(runner.shutdown(), timeout=0.2)

    assert bot.delete_calls == 1
    assert bot.session.close_calls == 1
    assert not dispatcher.polling_started.is_set()


async def test_runner_retries_delete_before_polling() -> None:
    bot = _FakeBot(delete_failures=1)
    dispatcher = _FakeDispatcher()
    config = TelegramConfig.model_validate(
        {"enabled": True, "token": "123456:secret", "allowed_chat_ids": [10]}
    )
    runner = TelegramRunner(
        config,
        cast(Catalog, _FakeCatalog([])),
        cast(Acquisition, _FakeAcquisition(1)),
        bot=cast(Bot, bot),
        dispatcher=cast(Dispatcher, dispatcher),
    )
    runner.start()
    await asyncio.wait_for(dispatcher.polling_started.wait(), timeout=2)

    assert bot.delete_calls == 2
    await runner.shutdown()


async def test_runner_cancels_aiogram_task_paused_before_tracker_entry() -> None:
    bot = _FakeBot()
    dispatcher = _FakeDispatcher()
    config = TelegramConfig.model_validate(
        {"enabled": True, "token": "123456:secret", "allowed_chat_ids": [10]}
    )
    runner = TelegramRunner(
        config,
        cast(Catalog, _FakeCatalog([])),
        cast(Acquisition, _FakeAcquisition(1)),
        bot=cast(Bot, bot),
        dispatcher=cast(Dispatcher, dispatcher),
    )
    release = asyncio.Event()
    service_calls = 0

    async def aiogram_task() -> None:
        nonlocal service_calls
        await release.wait()

        async def service(event: object, data: dict[str, object]) -> None:
            nonlocal service_calls
            del event, data
            service_calls += 1

        await runner.tracker(service, cast(TelegramObject, object()), {})

    task = asyncio.create_task(aiogram_task())
    dispatcher._handle_update_tasks.add(cast(asyncio.Task[object], task))
    await asyncio.sleep(0)

    await runner.shutdown()
    release.set()

    assert task.done()
    assert task.cancelled()
    assert service_calls == 0
    assert bot.session.close_calls == 1


async def test_runner_shutdown_cancels_active_upload_and_closes_stream() -> None:
    bot = _FakeBot()
    dispatcher = _FakeDispatcher()
    config = TelegramConfig.model_validate(
        {"enabled": True, "token": "123456:secret", "allowed_chat_ids": [10]}
    )
    acquisition = _FakeAcquisition(1)
    handlers = TelegramHandlers(
        cast(Catalog, _FakeCatalog([])), cast(Acquisition, acquisition), CallbackStateStore()
    )
    runner = TelegramRunner(
        config,
        cast(Catalog, _FakeCatalog([])),
        cast(Acquisition, acquisition),
        bot=cast(Bot, bot),
        dispatcher=cast(Dispatcher, dispatcher),
    )
    message = _FakeMessage(block_document=True)

    async def upload(event: object, data: dict[str, object]) -> None:
        del event, data
        await handlers.on_callback(cast(CallbackQuery, _FakeCallback("x:book-0", message)))

    task = asyncio.create_task(runner.tracker(upload, cast(TelegramObject, object()), {}))
    dispatcher._handle_update_tasks.add(cast(asyncio.Task[object], task))
    await message.document_started.wait()

    await runner.shutdown()

    assert task.done()
    assert acquisition.stream.closed == 1
