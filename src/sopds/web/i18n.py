"""Request-bound localization and translation catalog preparation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, cast

from babel.messages.catalog import Catalog, Message
from babel.messages.mofile import write_mo
from babel.messages.plurals import get_plural
from babel.messages.pofile import read_po
from babel.support import NullTranslations, Translations
from starlette.requests import Request

from sopds.imports.status import ImportState, ImportTrigger

LocaleCode = Literal["en", "ru"]

SUPPORTED_LOCALES: tuple[LocaleCode, ...] = ("en", "ru")
DEFAULT_LOCALE: LocaleCode = "en"
LANGUAGE_COOKIE = "sopds_ui_language"
TRANSLATIONS_DIRECTORY = Path(__file__).parent / "translations"
_DOMAIN = "messages"
_LANGUAGE_COOKIE_BYTES = LANGUAGE_COOKIE.encode("ascii")
_COOKIE_LOCALES: dict[bytes, LocaleCode] = {b"en": "en", b"ru": "ru"}
_RUSSIAN_PLURAL = get_plural("ru")


def N_(message: str) -> str:
    """Mark deferred messages for extraction without translating them eagerly."""
    return message


_CATALOG_ERROR_MESSAGES = frozenset(
    {
        N_("Catalog changed while loading; retry the request"),
        N_("Invalid catalog search"),
        N_("Invalid search field"),
        N_("Invalid catalog filter"),
        N_("Invalid catalog cursor"),
    }
)
_GENERIC_CATALOG_ERROR = N_("The catalog request could not be completed")
_KNOWN_HTML_MESSAGES = frozenset(
    {
        N_("This page has expired. Reload it and try again."),
        N_("Selected-books request is too large"),
        N_("Invalid selected-books request"),
        N_("Invalid archive preset"),
        N_("Invalid archive format"),
        N_("Invalid archive request"),
        N_("Invalid selected book IDs"),
        N_("Invalid public book ID"),
        N_("Too many selected books"),
        N_("Selected books exceed the source-size limit"),
        N_("No selected books are available for download"),
        N_("Catalog changed while loading; retry the request"),
        N_("The selected-books preview is unavailable"),
        N_("Service is shutting down"),
        N_("The selected books archive could not be created"),
        N_("Catalog import is starting"),
        N_("No catalog changes found"),
        N_("Manual import is starting"),
        N_("Import check is starting"),
        N_("Force import is starting"),
        N_("An import or database maintenance operation is already running"),
        N_("Database VACUUM completed"),
        N_("VACUUM skipped because catalog work is running"),
        N_(
            "This book is larger than the 64 MiB web reader limit. "
            "You can still download the original file."
        ),
    }
)
_IMPORT_STATE_MESSAGES = {
    ImportState.RUNNING: N_("running"),
    ImportState.SUCCEEDED: N_("succeeded"),
    ImportState.FAILED: N_("failed"),
    ImportState.INTERRUPTED: N_("interrupted"),
}
_IMPORT_TRIGGER_MESSAGES = {
    ImportTrigger.SCHEDULED: N_("scheduled"),
    ImportTrigger.MANUAL: N_("manual"),
}

_CATALOG_BROWSER_MESSAGES = {
    "unknownAuthor": N_("Unknown author"),
    "manyAuthors": N_("Many authors (6+)"),
    "booksWithoutSeries": N_("Books without series"),
    "moreAuthors": N_("+{count} more"),
    "selectBook": N_("Select {title}"),
    "selectAllAuthor": N_("Select all books by {label}"),
    "selectAllSeries": N_("Select all books in series {label}"),
    "read": N_("Read"),
    "details": N_("Details"),
    "missed": N_("Missed"),
    "hidden": N_("Hidden"),
    "authors": N_("Authors: "),
    "series": N_("Series: "),
    "bookMetadata": N_("Book metadata"),
    "downloadOriginal": N_("Download original {format} file for {title}"),
    "moreDownloadFormats": N_("More download formats for {title}"),
    "downloadConversion": N_("Download {format} conversion for {title}"),
    "filteredOverflow": N_("0 of {total} loaded · More match — refine search"),
    "sortBy": N_("Sort by "),
    "title": N_("Title"),
    "author": N_("Author"),
    "seriesColumn": N_("Series"),
    "number": N_("Number"),
    "ascending": N_("Ascending"),
    "descending": N_("Descending"),
    "sortAscending": N_("Sort ascending"),
    "sortDescending": N_("Sort descending"),
    "noLoadedBooksMatch": N_("No loaded books match"),
    "emptyOverflow": N_(
        "Additional catalog matches were not loaded. Refine the catalog search to search beyond this loaded set."
    ),
    "emptyFiltered": N_("Clear a local filter or try a broader phrase."),
    "loadedCatalogBooks": N_("Loaded catalog books"),
    "select": N_("Select"),
    "actions": N_("Actions"),
    "sortedAscending": N_(", sorted ascending"),
    "sortedDescending": N_(", sorted descending"),
}

_READER_BROWSER_MESSAGES = {
    "pages": N_("Pages"),
    "scroll": N_("Scroll"),
    "switchToPagesView": N_("Switch to pages view"),
    "switchToScrollView": N_("Switch to scroll view"),
    "previousPage": N_("Previous page"),
    "nextPage": N_("Next page"),
    "genericOpenError": N_("The book could not be opened in the web reader."),
}

_SELECTION_BROWSER_MESSAGES = {
    "savedSelectionRepaired": N_("Saved selection was repaired."),
    "selectionUnavailable": N_("Book selection is unavailable in this browser."),
    "couldNotSaveSelection": N_("Could not save the book selection."),
    "selectionLimit": N_("The selection is limited to 10 000 books."),
    "original": N_("Original"),
    "size": N_("Size"),
    "sourceSize": N_("Source size"),
    "couldNotLoadPreview": N_("Could not load the selection preview."),
    "noBooksSelected": N_("No books selected"),
    "selectBooksForZip": N_("Select downloadable books from the catalog to build a ZIP."),
    "browseCatalog": N_("Browse the catalog"),
    "unknownAuthor": N_("Unknown author"),
    "manyAuthors": N_("Many authors (6+)"),
    "moreAuthors": N_("+{count} more"),
    "booksWithoutSeries": N_("Books without series"),
    "unknownSelection": N_("Unknown selection"),
    "selectAllAuthor": N_("Select all books by {label}"),
    "selectAllSeries": N_("Select all books in series {label}"),
    "selectedBooks": N_("Selected books"),
    "select": N_("Select"),
    "author": N_("Author"),
    "title": N_("Title"),
    "series": N_("Series"),
    "status": N_("Status"),
    "actions": N_("Actions"),
    "sortedAscending": N_(", sorted ascending"),
    "sortedDescending": N_(", sorted descending"),
    "downloadable": N_("Downloadable"),
    "unsupported": N_("Unsupported"),
    "unavailable": N_("Unavailable"),
    "unknown": N_("Unknown"),
    "loadingSelection": N_("Loading selection…"),
    "loadingPreview": N_("Loading preview…"),
    "previewNeedsAttention": N_("Preview needs attention."),
    "couldNotRefreshPreview": N_("Could not refresh the selection preview."),
}


def _browser_messages(
    context: TranslationContext,
    sources: dict[str, str],
) -> dict[str, str]:
    return {key: context.gettext(source) for key, source in sources.items()}


def catalog_browser_messages(context: TranslationContext) -> dict[str, object]:
    """Return only the reviewed strings consumed by the catalog script."""
    messages: dict[str, object] = {**_browser_messages(context, _CATALOG_BROWSER_MESSAGES)}
    messages["filteredLoaded"] = {
        "one": context.pgettext(
            "catalog filtered count: one", "{count} book loaded out of {total}."
        ),
        "few": context.pgettext(
            "catalog filtered count: few", "{count} books loaded out of {total}."
        ),
        "many": context.pgettext(
            "catalog filtered count: many", "{count} books loaded out of {total}."
        ),
        "other": context.pgettext(
            "catalog filtered count: other", "{count} books loaded out of {total}."
        ),
    }
    return messages


def selection_browser_messages(context: TranslationContext) -> dict[str, str]:
    """Return only the reviewed strings consumed by the selection script."""
    return _browser_messages(context, _SELECTION_BROWSER_MESSAGES)


def reader_browser_messages(context: TranslationContext) -> dict[str, str]:
    """Return only the reviewed strings changed dynamically by the reader."""
    return _browser_messages(context, _READER_BROWSER_MESSAGES)


def _is_ascii_alphanumeric(value: str) -> bool:
    return value.isascii() and value.isalnum()


def _supported_locale_from_tag(language_tag: str) -> LocaleCode | None:
    """Accept ordinary supported BCP 47 tags without admitting duplicate parts."""
    subtags = language_tag.split("-")
    if not subtags or subtags[0].lower() not in SUPPORTED_LOCALES:
        return None
    if any(not subtag or not _is_ascii_alphanumeric(subtag) for subtag in subtags):
        return None

    locale = cast(LocaleCode, subtags[0].lower())
    index = 1
    if index < len(subtags) and len(subtags[index]) == 4 and subtags[index].isalpha():
        index += 1
    if index < len(subtags):
        region = subtags[index]
        if (len(region) == 2 and region.isalpha()) or (len(region) == 3 and region.isdigit()):
            index += 1

    variants: set[str] = set()
    while index < len(subtags):
        variant = subtags[index]
        is_variant = 5 <= len(variant) <= 8 or (len(variant) == 4 and variant[0].isdigit())
        if not is_variant:
            break
        normalized_variant = variant.lower()
        if normalized_variant in variants:
            return None
        variants.add(normalized_variant)
        index += 1

    extension_singletons: set[str] = set()
    while index < len(subtags) and len(subtags[index]) == 1:
        singleton = subtags[index].lower()
        if singleton == "x":
            break
        if singleton in extension_singletons:
            return None
        extension_singletons.add(singleton)
        index += 1
        extension_start = index
        while index < len(subtags) and 2 <= len(subtags[index]) <= 8:
            index += 1
        if index == extension_start:
            return None

    if index < len(subtags) and subtags[index].lower() == "x":
        index += 1
        if index == len(subtags):
            return None
        while index < len(subtags) and 1 <= len(subtags[index]) <= 8:
            index += 1

    return locale if index == len(subtags) else None


def _locale_from_cookie_headers(request: Request) -> LocaleCode | None:
    for header_name, header_value in request.headers.raw:
        if header_name.lower() != b"cookie":
            continue
        for index, cookie_pair in enumerate(header_value.split(b";")):
            if index:
                cookie_pair = cookie_pair.lstrip(b" \t")
            name, separator, value = cookie_pair.partition(b"=")
            if name == _LANGUAGE_COOKIE_BYTES:
                return _COOKIE_LOCALES.get(value) if separator else None
    return None


def resolve_locale(request: Request) -> LocaleCode:
    """Keep language negotiation exact, deterministic, and local to one request."""
    if cookie_locale := _locale_from_cookie_headers(request):
        return cookie_locale

    for header_value in request.headers.getlist("accept-language"):
        for item in header_value.split(","):
            language_tag = item.split(";", 1)[0].strip()
            if locale := _supported_locale_from_tag(language_tag):
                return locale
    return DEFAULT_LOCALE


@dataclass(frozen=True, slots=True)
class TranslationContext:
    """Expose translations as render-local callables rather than Jinja global state."""

    locale: LocaleCode
    translations: NullTranslations

    def gettext(self, message: str) -> str:
        return self.translations.gettext(message)

    def ngettext(self, singular: str, plural: str, number: int) -> str:
        return self.translations.ngettext(singular, plural, number)

    def pgettext(self, context: str, message: str) -> str:
        return cast(str, self.translations.pgettext(context, message))

    def npgettext(self, context: str, singular: str, plural: str, number: int) -> str:
        return self.translations.npgettext(context, singular, plural, number)


@cache
def get_translations(
    locale: LocaleCode,
    translations_directory: Path = TRANSLATIONS_DIRECTORY,
) -> NullTranslations:
    """Cache immutable catalogs while leaving request locale selection uncached."""
    if locale == DEFAULT_LOCALE:
        return NullTranslations()
    return Translations.load(
        dirname=translations_directory,
        locales=[locale],
        domain=_DOMAIN,
    )


def translation_context(
    locale: LocaleCode,
    translations_directory: Path = TRANSLATIONS_DIRECTORY,
) -> TranslationContext:
    return TranslationContext(locale, get_translations(locale, translations_directory))


def request_translation_context(request: Request) -> TranslationContext:
    return translation_context(resolve_locale(request))


def catalog_error_message(context: TranslationContext, error: Exception) -> str:
    """Translate only catalog failures approved for public HTML presentation."""
    source = str(error)
    if source not in _CATALOG_ERROR_MESSAGES:
        source = _GENERIC_CATALOG_ERROR
    return context.gettext(source)


def known_html_message(context: TranslationContext, source: str) -> str:
    """Keep route-generated HTML translation limited to reviewed source messages."""
    if source not in _KNOWN_HTML_MESSAGES:
        raise ValueError(f"Unknown HTML message: {source!r}")
    return context.gettext(source)


def import_state_label(context: TranslationContext, state: ImportState) -> str:
    return context.gettext(_IMPORT_STATE_MESSAGES[state])


def import_trigger_label(context: TranslationContext, trigger: ImportTrigger) -> str:
    return context.gettext(_IMPORT_TRIGGER_MESSAGES[trigger])


def _validate_message(message: Message, *, plural_count: int) -> None:
    if message.fuzzy:
        raise ValueError(f"Fuzzy translation is not allowed: {message.id!r}")
    if not message.id:
        return
    if isinstance(message.id, tuple):
        if not isinstance(message.string, tuple) or len(message.string) != plural_count:
            raise ValueError(f"All Russian plural forms are required: {message.id!r}")
        if any(not value.strip() for value in message.string):
            raise ValueError(f"All Russian plural forms are required: {message.id!r}")
    elif not isinstance(message.string, str) or not message.string.strip():
        raise ValueError(f"Translation is required: {message.id!r}")


def _read_validated_catalog(po_path: Path) -> Catalog:
    with po_path.open("rb") as po_file:
        catalog = read_po(po_file, domain=_DOMAIN, abort_invalid=True)
    if (
        catalog.locale is None
        or catalog.locale.language != "ru"
        or catalog.num_plurals != _RUSSIAN_PLURAL.num_plurals
        or "".join(catalog.plural_expr.split()) != "".join(_RUSSIAN_PLURAL.plural_expr.split())
    ):
        raise ValueError("Russian catalog metadata must declare Russian plural rules")
    for message in catalog:
        _validate_message(message, plural_count=_RUSSIAN_PLURAL.num_plurals)
    return catalog


def _compile_catalog(po_path: Path, mo_path: Path) -> None:
    catalog = _read_validated_catalog(po_path)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w+b",
            prefix=f".{mo_path.name}.",
            suffix=".tmp",
            dir=mo_path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            write_mo(temporary_file, catalog)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary_path.replace(mo_path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def compile_catalogs_if_needed(
    *,
    force: bool = False,
    translations_directory: Path = TRANSLATIONS_DIRECTORY,
) -> tuple[Path, ...]:
    """Compile missing or stale Russian catalogs and invalidate loaded copies."""
    po_path = translations_directory / "ru" / "LC_MESSAGES" / f"{_DOMAIN}.po"
    mo_path = po_path.with_suffix(".mo")
    if not po_path.is_file():
        raise FileNotFoundError(po_path)

    should_compile = force or not mo_path.exists()
    if not should_compile:
        should_compile = po_path.stat().st_mtime_ns > mo_path.stat().st_mtime_ns
    if not should_compile:
        return ()

    _compile_catalog(po_path, mo_path)
    get_translations.cache_clear()
    return (mo_path,)
