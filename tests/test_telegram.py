"""Network-free tests for Telegram authorization, state, rendering, and streaming."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, cast, override
from unittest.mock import AsyncMock, patch

import pytest
from telegram import (
    Bot,
    CallbackQuery,
    Chat,
    InaccessibleMessage,
    InputFile,
    Message,
    MessageEntity,
    Update,
    User,
)
from telegram.ext import Application, CommandHandler

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
from sopds.telegram.middleware import TelegramUpdateProcessor
from sopds.telegram.runner import TelegramRunner
from sopds.telegram.state import CallbackStateStore, DownloadState, PageState
from sopds.telegram.upload import StagedInputFile


def _message(chat_id: int, text: str = "search") -> Message:
    return Message(
        message_id=1,
        date=datetime(1970, 1, 1, tzinfo=UTC),
        chat=Chat(id=chat_id, type="private" if chat_id > 0 else "group"),
        from_user=User(id=2, is_bot=False, first_name="User"),
        text=text,
    )


async def test_update_processor_filters_private_group_callback_and_unknown_updates() -> None:
    processor = TelegramUpdateProcessor((10, -20))
    handled: list[int] = []

    async def handle(update: Update) -> None:
        handled.append(update.update_id)

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
    updates = (
        allowed_private,
        allowed_group,
        denied,
        Update(update_id=4, callback_query=callback),
        Update(update_id=5),
    )
    for update in updates:
        await processor.process_update(update, handle(update))

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


async def test_staged_input_file_uses_disk_and_closes_source_before_upload() -> None:
    stream = _Stream()
    upload = await StagedInputFile.create(cast(AsyncByteStream, stream), "book.fb2")

    assert upload.input_file is not None
    content = upload.input_file.input_file_content
    assert not isinstance(content, bytes)
    assert content.read() == b"onetwo"
    assert stream.closed == 1

    await upload.aclose()
    assert content.closed
    assert stream.closed == 1


async def test_staged_input_file_closes_after_read_failure() -> None:
    stream = _Stream(fail=True)

    with pytest.raises(OSError, match="read failed"):
        await StagedInputFile.create(cast(AsyncByteStream, stream), "book.fb2")

    assert stream.closed == 1


async def test_staging_closes_source_when_temporary_file_creation_fails() -> None:
    stream = _Stream()

    with (
        patch("sopds.telegram.upload.tempfile.TemporaryFile", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        await StagedInputFile.create(cast(AsyncByteStream, stream), "book.fb2")

    assert stream.closed == 1


async def test_staging_waits_for_source_cleanup_when_cancelled() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingCloseStream(_Stream):
        @override
        async def aclose(self) -> None:
            self.closed += 1
            started.set()
            await release.wait()

    stream = BlockingCloseStream()
    task = asyncio.create_task(StagedInputFile.create(cast(AsyncByteStream, stream), "book.fb2"))
    await started.wait()
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed == 1


async def test_unauthorized_callback_does_not_answer() -> None:
    processor = TelegramUpdateProcessor((10,))
    callback = CallbackQuery(
        id="callback",
        from_user=User(id=2, is_bot=False, first_name="User"),
        chat_instance="instance",
        message=_message(-20),
        data="d:book",
    )
    answer = AsyncMock()

    async def handler() -> None:
        await answer()

    update = Update(update_id=1, callback_query=callback)
    await processor.process_update(update, handler())
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
        self.documents: list[tuple[InputFile, str]] = []
        self.document_payloads: list[bytes] = []

    async def reply_text(self, text: str, *, reply_markup: object | None = None) -> None:
        self.answers.append((text, reply_markup))

    async def edit_text(self, text: str, *, reply_markup: object | None = None) -> None:
        self.edits.append((text, reply_markup))

    async def reply_document(self, document: InputFile, *, caption: str) -> None:
        self.documents.append((document, caption))
        if self.fail_document:
            raise OSError("send failed")
        self.document_started.set()
        await self.document_release.wait()
        content = document.input_file_content
        self.document_payloads.append(content if isinstance(content, bytes) else content.read())


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
    bot._bot_user = User(id=123456, is_bot=True, first_name="SOPDS", username="sopds_bot")
    command = CommandHandler("start", AsyncMock())

    def update(text: str) -> Update:
        entity = MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(text))
        message = Message(
            message_id=1,
            date=datetime(1970, 1, 1, tzinfo=UTC),
            chat=Chat(id=10, type="private"),
            from_user=User(id=2, is_bot=False, first_name="User"),
            text=text,
            entities=(entity,),
        )
        message.set_bot(bot)
        return Update(update_id=1, message=message)

    assert command.check_update(update("/start@other_bot")) is None
    assert command.check_update(update("/start@sopds_bot")) is not None


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


async def test_processor_closes_admission_and_cancels_active_update() -> None:
    processor = TelegramUpdateProcessor((10,))
    started = asyncio.Event()
    invoked = 0

    async def handler() -> None:
        nonlocal invoked
        invoked += 1
        started.set()
        await asyncio.Event().wait()

    update = Update(update_id=1, message=_message(10))
    active = asyncio.create_task(processor.process_update(update, handler()))
    await started.wait()
    processor.stop_accepting()
    await processor.process_update(update, handler())
    await processor.cancel_and_wait()

    assert invoked == 1
    assert active.cancelled()


class _FakeBot:
    def __init__(self) -> None:
        self.shutdown_calls = 0

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class _FakeUpdater:
    def __init__(self, *, polling_failures: int = 0, runtime_polling_failures: int = 0) -> None:
        self.polling_failures = polling_failures
        self.runtime_polling_failures = runtime_polling_failures
        self.polling_calls = 0
        self.stop_calls = 0
        self.shutdown_calls = 0
        self.polling_started = asyncio.Event()
        self.polling_retried = asyncio.Event()
        self.second_polling_started = asyncio.Event()
        self.polling_arguments: dict[str, object] = {}
        self.polling_arguments_history: list[dict[str, object]] = []
        self.runtime_failure_requested = asyncio.Event()
        self._polling_stop = asyncio.Event()
        self.running = False
        self._Updater__polling_task: asyncio.Task[None] | None = None

    async def start_polling(self, **kwargs: object) -> asyncio.Queue[object]:
        self.polling_arguments = kwargs
        self.polling_arguments_history.append(kwargs)
        self.polling_calls += 1
        self.polling_started.set()
        if self.polling_calls >= 2:
            self.second_polling_started.set()
        if self.polling_calls <= self.polling_failures:
            raise RuntimeError("polling failed")
        self.polling_retried.set()
        self.running = True
        self._polling_stop = asyncio.Event()

        async def poll() -> None:
            if self.runtime_polling_failures:
                self.runtime_polling_failures -= 1
                await self.runtime_failure_requested.wait()
                raise RuntimeError("runtime polling failed")
            await self._polling_stop.wait()

        self._Updater__polling_task = asyncio.create_task(poll())
        return asyncio.Queue()

    async def stop(self) -> None:
        self.stop_calls += 1
        self.running = False
        self._polling_stop.set()
        polling_task = self._Updater__polling_task
        if polling_task is not None:
            await polling_task
        self._Updater__polling_task = None

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class _FakeApplication:
    def __init__(
        self,
        processor: TelegramUpdateProcessor,
        *,
        initialization_failures: int = 0,
        polling_failures: int = 0,
        runtime_polling_failures: int = 0,
    ) -> None:
        self.processor = processor
        self.bot = _FakeBot()
        self.updater = _FakeUpdater(
            polling_failures=polling_failures,
            runtime_polling_failures=runtime_polling_failures,
        )
        self.initialization_failures = initialization_failures
        self.initialize_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.shutdown_calls = 0
        self.handlers: list[object] = []
        self.running = False
        self.initialize_attempted = asyncio.Event()

    def add_handler(self, handler: object) -> None:
        self.handlers.append(handler)

    async def initialize(self) -> None:
        self.initialize_calls += 1
        self.initialize_attempted.set()
        if self.initialize_calls <= self.initialization_failures:
            raise OSError("offline")
        await self.processor.initialize()

    async def start(self) -> None:
        self.start_calls += 1
        self.running = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self.running = False

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        await self.processor.shutdown()
        await self.updater.shutdown()
        await self.bot.shutdown()


def _telegram_config() -> TelegramConfig:
    return TelegramConfig.model_validate(
        {"enabled": True, "token": "123456:secret", "allowed_chat_ids": [10, -20]}
    )


def _runner_with_application(
    application: _FakeApplication,
    processor: TelegramUpdateProcessor,
    *,
    catalog: Catalog | None = None,
    acquisition: Acquisition | None = None,
    conversion: ConversionService | None = None,
) -> TelegramRunner:
    return TelegramRunner(
        _telegram_config(),
        catalog or cast(Catalog, _FakeCatalog([])),
        acquisition or cast(Acquisition, _FakeAcquisition(1)),
        conversion,
        application=cast(Application[Any, Any, Any, Any, Any, Any], application),
        processor=processor,
    )


async def test_runner_builds_ptb_application_without_network_access() -> None:
    runner = TelegramRunner(
        _telegram_config(),
        cast(Catalog, _FakeCatalog([])),
        cast(Acquisition, _FakeAcquisition(1)),
    )

    assert runner.processor.max_concurrent_updates == 4
    assert sum(len(group) for group in runner.application.handlers.values()) == 3
    await runner.shutdown()


async def test_runner_wires_handlers_into_application() -> None:
    processor = TelegramUpdateProcessor((10, -20))
    application = _FakeApplication(processor)
    catalog = cast(Catalog, _FakeCatalog([]))
    acquisition = cast(Acquisition, _FakeAcquisition(1))
    conversion = cast(ConversionService, _FakeConversion())

    with patch("sopds.telegram.runner.TelegramHandlers") as handlers_type:
        runner = _runner_with_application(
            application,
            processor,
            catalog=catalog,
            acquisition=acquisition,
            conversion=conversion,
        )

    handlers_type.assert_called_once_with(
        catalog,
        acquisition,
        runner.state,
        conversion,
        OUTPUT_POLICY,
    )
    handlers_type.return_value.register.assert_called_once_with(application)
    await runner.shutdown()


async def test_runner_uses_bounded_polling_and_idempotent_shutdown() -> None:
    processor = TelegramUpdateProcessor((10, -20))
    application = _FakeApplication(processor)
    runner = _runner_with_application(application, processor)

    runner.start()
    await asyncio.wait_for(application.updater.polling_started.wait(), timeout=1)

    arguments = application.updater.polling_arguments
    assert arguments["allowed_updates"] == ["message", "callback_query"]
    assert arguments["drop_pending_updates"] is True
    assert arguments["bootstrap_retries"] == 0
    assert callable(arguments["error_callback"])

    await runner.shutdown()
    await runner.shutdown()

    assert application.initialize_calls == 1
    assert application.start_calls == 1
    assert application.updater.stop_calls == 1
    assert application.stop_calls == 1
    assert application.shutdown_calls == 1
    assert application.bot.shutdown_calls == 1


@pytest.mark.parametrize("failure_phase", ["initialize", "polling"])
async def test_runner_retries_failed_start_without_blocking_http_lifecycle(
    failure_phase: str,
) -> None:
    processor = TelegramUpdateProcessor((10, -20))
    application = _FakeApplication(
        processor,
        initialization_failures=1 if failure_phase == "initialize" else 0,
        polling_failures=1 if failure_phase == "polling" else 0,
    )
    runner = _runner_with_application(application, processor)

    async def immediate_retry(delay: float) -> bool:
        assert delay == 1.0
        return False

    runner._retry_delay = immediate_retry  # type: ignore[method-assign]
    runner.start()
    await asyncio.wait_for(application.updater.polling_retried.wait(), timeout=1)

    assert application.initialize_calls == 2
    assert application.bot.shutdown_calls == 1

    await runner.shutdown()
    assert application.bot.shutdown_calls == 2


async def test_runner_retries_terminal_polling_failure_without_dropping_updates_again() -> None:
    processor = TelegramUpdateProcessor((10, -20))
    application = _FakeApplication(processor, runtime_polling_failures=1)
    runner = _runner_with_application(application, processor)

    async def immediate_retry(delay: float) -> bool:
        assert delay == 1.0
        return False

    runner._retry_delay = immediate_retry  # type: ignore[method-assign]
    runner.start()
    await application.updater.polling_started.wait()
    application.updater.runtime_failure_requested.set()
    await asyncio.wait_for(application.updater.second_polling_started.wait(), timeout=1)

    assert [
        arguments["drop_pending_updates"]
        for arguments in application.updater.polling_arguments_history
    ] == [True, False]

    await runner.shutdown()


async def test_runner_shutdown_interrupts_blocked_initialization() -> None:
    processor = TelegramUpdateProcessor((10, -20))
    application = _FakeApplication(processor)
    blocked = asyncio.Event()

    async def blocked_initialize() -> None:
        application.initialize_calls += 1
        blocked.set()
        await asyncio.Event().wait()

    application.initialize = blocked_initialize  # type: ignore[method-assign]
    runner = _runner_with_application(application, processor)
    runner.start()
    await blocked.wait()

    await asyncio.wait_for(runner.shutdown(), timeout=0.2)

    assert application.bot.shutdown_calls == 1


async def test_runner_shutdown_wakes_failed_start_retry() -> None:
    processor = TelegramUpdateProcessor((10, -20))
    application = _FakeApplication(processor, initialization_failures=10)
    runner = _runner_with_application(application, processor)

    runner.start()
    await application.initialize_attempted.wait()
    await asyncio.wait_for(runner.shutdown(), timeout=0.2)

    assert application.initialize_calls == 1
    assert application.bot.shutdown_calls >= 1
    assert application.updater.polling_calls == 0


async def test_processor_cancels_active_and_discards_queued_update() -> None:
    processor = TelegramUpdateProcessor((10,), max_concurrent_updates=1)
    first_started = asyncio.Event()
    service_calls = 0

    async def active_service() -> None:
        first_started.set()
        await asyncio.Event().wait()

    async def queued_service() -> None:
        nonlocal service_calls
        service_calls += 1

    update = Update(update_id=1, message=_message(10))
    active = asyncio.create_task(processor.process_update(update, active_service()))
    await first_started.wait()
    queued = asyncio.create_task(processor.process_update(update, queued_service()))
    await asyncio.sleep(0)

    processor.stop_accepting()
    await processor.cancel_and_wait()
    await queued

    assert active.cancelled()
    assert service_calls == 0


async def test_runner_shutdown_cancels_active_upload_and_closes_stream() -> None:
    processor = TelegramUpdateProcessor((10, -20))
    application = _FakeApplication(processor)
    acquisition = _FakeAcquisition(1)
    handlers = TelegramHandlers(
        cast(Catalog, _FakeCatalog([])), cast(Acquisition, acquisition), CallbackStateStore()
    )
    runner = _runner_with_application(
        application,
        processor,
        acquisition=cast(Acquisition, acquisition),
    )
    message = _FakeMessage(block_document=True)
    update = Update(update_id=1, message=_message(10))

    async def upload() -> None:
        await handlers.on_callback(cast(CallbackQuery, _FakeCallback("x:book-0", message)))

    task = asyncio.create_task(processor.process_update(update, upload()))
    await message.document_started.wait()

    await runner.shutdown()

    assert task.done()
    assert task.cancelled()
    assert acquisition.stream.closed == 1
    assert application.bot.shutdown_calls == 1
