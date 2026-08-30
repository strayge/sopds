"""Focused coverage for request locale negotiation and catalog preparation."""

import os
from pathlib import Path

import pytest
from babel.messages.catalog import Catalog
from babel.messages.extract import DEFAULT_KEYWORDS, extract_from_dir
from babel.messages.frontend import parse_mapping_cfg
from babel.messages.pofile import PoFileError, read_po, write_po
from starlette.requests import Request

from sopds.catalog.contracts import CatalogInputError
from sopds.imports.status import ImportState, ImportTrigger
from sopds.web.i18n import (
    N_,
    catalog_browser_messages,
    catalog_error_message,
    compile_catalogs_if_needed,
    import_state_label,
    import_trigger_label,
    known_html_message,
    request_translation_context,
    resolve_locale,
    selection_browser_messages,
    translation_context,
)


def _request(
    *,
    cookie: str | tuple[str, ...] | None = None,
    accept_language: str | tuple[str, ...] | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if isinstance(cookie, str):
        cookie = (cookie,)
    headers.extend((b"cookie", value.encode()) for value in cookie or ())
    if isinstance(accept_language, str):
        accept_language = (accept_language,)
    headers.extend((b"accept-language", value.encode()) for value in accept_language or ())
    return Request({"type": "http", "headers": headers})


def _po(
    *messages: str,
    language: str = "ru",
    plural_forms: str = (
        "nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : "
        "n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);"
    ),
) -> str:
    header = f"""msgid ""
msgstr ""
"Project-Id-Version: SOPDS tests\\n"
"Language: {language}\\n"
"Plural-Forms: {plural_forms}\\n"
"MIME-Version: 1.0\\n"
"Content-Type: text/plain; charset=utf-8\\n"
"Content-Transfer-Encoding: 8bit\\n"
"""
    return header + "\n" + "\n\n".join(messages) + "\n"


def _catalog_path(directory: Path) -> Path:
    path = directory / "ru" / "LC_MESSAGES" / "messages.po"
    path.parent.mkdir(parents=True)
    return path


def test_exact_cookie_takes_precedence_over_accept_language() -> None:
    assert resolve_locale(_request(cookie="sopds_ui_language=en", accept_language="ru")) == "en"
    assert resolve_locale(_request(cookie="sopds_ui_language=ru", accept_language="en")) == "ru"


@pytest.mark.parametrize(
    ("cookie", "accept_language", "expected"),
    [
        ("EN", "ru", "ru"),
        ("ru-RU", "en", "en"),
        (" ru", "en", "en"),
        ("ru ", "en", "en"),
        ('"ru"', "en", "en"),
        ("de", "ru", "ru"),
        ("", "ru", "ru"),
    ],
)
def test_invalid_cookie_falls_through_to_header(
    cookie: str, accept_language: str, expected: str
) -> None:
    request = _request(
        cookie=f"sopds_ui_language={cookie}",
        accept_language=accept_language,
    )

    assert resolve_locale(request) == expected


def test_cookie_delimiter_whitespace_does_not_change_the_named_value() -> None:
    request = _request(cookie="other=value; \t sopds_ui_language=ru", accept_language="en")

    assert resolve_locale(request) == "ru"


@pytest.mark.parametrize(
    ("cookie", "expected"),
    [
        ("sopds_ui_language= ru; sopds_ui_language=ru", "en"),
        (("sopds_ui_language= ru", "sopds_ui_language=ru"), "en"),
        ("sopds_ui_language=ru; sopds_ui_language=en", "ru"),
        (("sopds_ui_language=ru", "sopds_ui_language=en"), "ru"),
    ],
)
def test_first_named_cookie_occurrence_controls_resolution(
    cookie: str | tuple[str, ...], expected: str
) -> None:
    assert resolve_locale(_request(cookie=cookie, accept_language="en")) == expected


def test_header_uses_first_supported_family_and_ignores_quality_values() -> None:
    assert resolve_locale(_request(accept_language="fr, ru-RU;q=0, en;q=1")) == "ru"
    assert resolve_locale(_request(accept_language="EN-gb;q=0.1, ru;q=1")) == "en"


def test_repeated_header_fields_preserve_negotiation_order() -> None:
    assert resolve_locale(_request(accept_language=("de", "ru-RU, en"))) == "ru"
    assert resolve_locale(_request(accept_language=("en-US", "ru"))) == "en"


def test_supported_script_and_region_tags_are_accepted() -> None:
    assert resolve_locale(_request(accept_language="ru-Cyrl-RU")) == "ru"
    assert resolve_locale(_request(accept_language="en-Latn-US")) == "en"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("*", "en"),
        ("de, *;q=1", "en"),
        ("ru_RU, en-US", "en"),
        ("ru--RU, en", "en"),
        ("ru-x, en", "en"),
        ("en-u-foo-u-bar, ru", "ru"),
        ("en-1901-1901, ru", "ru"),
        ("", "en"),
    ],
)
def test_malformed_wildcard_and_unsupported_headers_fall_back_cleanly(
    header: str, expected: str
) -> None:
    assert resolve_locale(_request(accept_language=header)) == expected


def test_locale_and_translations_remain_bound_to_each_request() -> None:
    russian = request_translation_context(_request(accept_language="ru"))
    english = request_translation_context(_request(accept_language="en"))
    russian_again = request_translation_context(_request(accept_language="ru"))

    assert (russian.locale, english.locale, russian_again.locale) == ("ru", "en", "ru")
    assert english.translations is not russian.translations
    assert russian_again.translations is russian.translations
    assert N_("deferred") == "deferred"


def test_public_html_presenters_translate_only_known_values() -> None:
    russian = request_translation_context(_request(accept_language="ru"))

    assert catalog_error_message(russian, CatalogInputError("Invalid catalog search")) == (
        "Некорректный поиск по каталогу"
    )
    assert catalog_error_message(russian, CatalogInputError("private diagnostic")) == (
        "Не удалось выполнить запрос к каталогу"  # noqa: RUF001
    )
    assert known_html_message(russian, "Service is shutting down") == ("Сервис завершает работу")
    assert import_state_label(russian, ImportState.INTERRUPTED) == "Прервано"
    assert import_trigger_label(russian, ImportTrigger.SCHEDULED) == "По расписанию"
    with pytest.raises(ValueError, match="Unknown HTML message"):
        known_html_message(russian, "private diagnostic")


def test_compact_browser_payloads_are_allowlisted_and_have_russian_plural_categories() -> None:
    english_catalog = catalog_browser_messages(
        request_translation_context(_request(accept_language="en"))
    )
    russian = request_translation_context(_request(accept_language="ru"))
    catalog = catalog_browser_messages(russian)
    selection = selection_browser_messages(russian)

    assert english_catalog["filteredLoaded"] == {
        "one": "{count} book loaded out of {total}.",
        "few": "{count} books loaded out of {total}.",
        "many": "{count} books loaded out of {total}.",
        "other": "{count} books loaded out of {total}.",
    }
    assert catalog["unknownAuthor"] == "Неизвестный автор"
    assert catalog["filteredLoaded"] == {
        "one": "Загружена {count} книга из {total}.",
        "few": "Загружено {count} книги из {total}.",
        "many": "Загружено {count} книг из {total}.",
        "other": "Загружено {count} книги из {total}.",
    }
    assert selection["selectionLimit"] == "Можно выбрать не более 10 000 книг."
    assert "Browse and manage the SOPDS private library catalog." not in catalog.values()
    assert "Browse and manage the SOPDS private library catalog." not in selection.values()


def test_russian_server_plural_rules_cover_one_few_and_many() -> None:
    russian = request_translation_context(_request(accept_language="ru"))

    assert [
        russian.ngettext("%(count)s book loaded.", "%(count)s books loaded.", count)
        % {"count": f"{count:,}".replace(",", " ")}
        for count in (1, 2, 5, 11, 21, 1_002)
    ] == [
        "Загружена 1 книга.",
        "Загружено 2 книги.",
        "Загружено 5 книг.",
        "Загружено 11 книг.",
        "Загружена 21 книга.",
        "Загружено 1 002 книги.",
    ]


def test_russian_catalog_covers_every_extracted_source_message(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    with (repo_root / "babel.cfg").open(encoding="utf-8") as config_file:
        method_map, options_map = parse_mapping_cfg(  # type: ignore[no-untyped-call]
            config_file, filename="babel.cfg"
        )

    extracted_catalog = Catalog()
    keywords = {**DEFAULT_KEYWORDS, "N_": None}
    for filename, line_number, message_id, comments, context in extract_from_dir(
        repo_root,
        method_map=method_map,
        options_map=options_map,
        keywords=keywords,
    ):
        extracted_catalog.add(
            message_id,
            locations=[(filename, line_number)],
            auto_comments=comments,
            context=context,
        )

    pot_path = tmp_path / "messages.pot"
    with pot_path.open("wb") as pot_file:
        write_po(pot_file, extracted_catalog)
    with pot_path.open("rb") as pot_file:
        source_catalog = read_po(pot_file, abort_invalid=True)
    russian_path = repo_root / "src/sopds/web/translations/ru/LC_MESSAGES/messages.po"
    with russian_path.open("rb") as russian_file:
        russian_catalog = read_po(russian_file, abort_invalid=True)

    source_identities = {(message.context, message.id) for message in source_catalog if message.id}
    russian_identities = {
        (message.context, message.id) for message in russian_catalog if message.id
    }
    missing = source_identities - russian_identities
    stale = russian_identities - source_identities

    assert not missing, f"Russian catalog is missing active messages: {sorted(map(repr, missing))}"
    assert not stale, f"Russian catalog has stale active messages: {sorted(map(repr, stale))}"


def test_compiler_handles_missing_fresh_stale_and_forced_catalogs(tmp_path: Path) -> None:
    po_path = _catalog_path(tmp_path)
    po_path.write_text(
        _po(
            'msgid "Hello"\nmsgstr "Привет"',
            'msgid "book"\nmsgid_plural "books"\nmsgstr[0] "книга"\n'
            'msgstr[1] "книги"\nmsgstr[2] "книг"',
        ),
        encoding="utf-8",
    )
    mo_path = po_path.with_suffix(".mo")

    assert compile_catalogs_if_needed(translations_directory=tmp_path) == (mo_path,)
    assert mo_path.is_file()
    first_mtime = mo_path.stat().st_mtime_ns
    assert compile_catalogs_if_needed(translations_directory=tmp_path) == ()
    assert mo_path.stat().st_mtime_ns == first_mtime

    russian = translation_context("ru", tmp_path)
    assert russian.gettext("Hello") == "Привет"
    assert [russian.ngettext("book", "books", count) for count in (1, 2, 5, 11, 21)] == [
        "книга",
        "книги",
        "книг",
        "книг",
        "книга",
    ]

    po_path.write_text(
        _po(
            'msgid "Hello"\nmsgstr "Здравствуйте"',
            'msgid "book"\nmsgid_plural "books"\nmsgstr[0] "книга"\n'
            'msgstr[1] "книги"\nmsgstr[2] "книг"',
        ),
        encoding="utf-8",
    )
    os.utime(mo_path, ns=(1, 1))
    assert compile_catalogs_if_needed(translations_directory=tmp_path) == (mo_path,)
    assert translation_context("ru", tmp_path).gettext("Hello") == "Здравствуйте"

    assert compile_catalogs_if_needed(force=True, translations_directory=tmp_path) == (mo_path,)
    assert not tuple(mo_path.parent.glob(".messages.mo.*.tmp"))


def test_compiler_preserves_existing_mo_when_atomic_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    po_path = _catalog_path(tmp_path)
    po_path.write_text(_po('msgid "Hello"\nmsgstr "Привет"'), encoding="utf-8")
    mo_path = po_path.with_suffix(".mo")
    compile_catalogs_if_needed(translations_directory=tmp_path)
    original_mo = mo_path.read_bytes()
    os.utime(mo_path, ns=(1, 1))

    def fail_write(*_args: object) -> None:
        raise OSError("write failed")

    monkeypatch.setattr("sopds.web.i18n.write_mo", fail_write)
    with pytest.raises(OSError, match="write failed"):
        compile_catalogs_if_needed(translations_directory=tmp_path)

    assert mo_path.read_bytes() == original_mo
    assert not tuple(mo_path.parent.glob(".messages.mo.*.tmp"))


@pytest.mark.parametrize(
    "message",
    [
        '#, fuzzy\nmsgid "Hello"\nmsgstr "Привет"',
        'msgid "Hello"\nmsgstr ""',
        'msgid "book"\nmsgid_plural "books"\nmsgstr[0] "книга"\nmsgstr[1] "книги"\nmsgstr[2] ""',
    ],
)
def test_compiler_rejects_incomplete_or_fuzzy_messages(tmp_path: Path, message: str) -> None:
    _catalog_path(tmp_path).write_text(_po(message), encoding="utf-8")

    with pytest.raises(ValueError):
        compile_catalogs_if_needed(translations_directory=tmp_path)

    assert not (tmp_path / "ru" / "LC_MESSAGES" / "messages.mo").exists()


def test_compiler_rejects_fuzzy_catalog_header(tmp_path: Path) -> None:
    _catalog_path(tmp_path).write_text(
        "#, fuzzy\n" + _po('msgid "Hello"\nmsgstr "Привет"'),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Fuzzy translation"):
        compile_catalogs_if_needed(translations_directory=tmp_path)

    assert not (tmp_path / "ru" / "LC_MESSAGES" / "messages.mo").exists()


@pytest.mark.parametrize(
    ("language", "plural_forms", "translations"),
    [
        ("en", "nplurals=2; plural=(n != 1);", ('msgstr[0] "one"', 'msgstr[1] "many"')),
        ("ru", "nplurals=2; plural=(n != 1);", ('msgstr[0] "одна"', 'msgstr[1] "много"')),
        (
            "ru",
            "nplurals=3; plural=(n == 1 ? 0 : n == 2 ? 1 : 2);",
            ('msgstr[0] "одна"', 'msgstr[1] "несколько"', 'msgstr[2] "много"'),
        ),
    ],
)
def test_compiler_rejects_non_russian_plural_metadata(
    tmp_path: Path,
    language: str,
    plural_forms: str,
    translations: tuple[str, ...],
) -> None:
    plural_message = "\n".join(('msgid "item"', 'msgid_plural "items"', *translations))
    _catalog_path(tmp_path).write_text(
        _po(plural_message, language=language, plural_forms=plural_forms),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Russian catalog metadata"):
        compile_catalogs_if_needed(translations_directory=tmp_path)

    assert not (tmp_path / "ru" / "LC_MESSAGES" / "messages.mo").exists()


def test_compiler_propagates_po_parse_errors(tmp_path: Path) -> None:
    _catalog_path(tmp_path).write_text('msgid "unterminated\n', encoding="utf-8")

    with pytest.raises(PoFileError):
        compile_catalogs_if_needed(translations_directory=tmp_path)
