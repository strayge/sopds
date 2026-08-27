"""Database-free XML rendering for the OPDS 1.2 presentation adapter."""

import hashlib
import json
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

from sopds.acquisition.service import media_type_for
from sopds.catalog.contracts import BookSummary, NavigationItem

ATOM = "http://www.w3.org/2005/Atom"
DC = "http://purl.org/dc/terms/"
OPENSEARCH = "http://a9.com/-/spec/opensearch/1.1/"
ACQUISITION_REL = "http://opds-spec.org/acquisition/open-access"
SUBSECTION_REL = "subsection"
NAVIGATION_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQUISITION_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"
OPENSEARCH_TYPE = "application/opensearchdescription+xml"

ET.register_namespace("", ATOM)
ET.register_namespace("dc", DC)
ET.register_namespace("opensearch", OPENSEARCH)


def clean(value: object) -> str:
    """Replace code points forbidden by XML 1.0 instead of failing a catalog response."""
    text = str(value)
    return "".join(
        character
        if character in "\t\n\r"
        or "\x20" <= character <= "\ud7ff"
        or "\ue000" <= character <= "\ufffd"
        or "\U00010000" <= character <= "\U0010ffff"
        else "\ufffd"
        for character in text
    )


def rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def stable_id(name: str, state: object | None = None) -> str:
    suffix = name
    if state is not None:
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        suffix += ":" + hashlib.sha256(payload.encode()).hexdigest()[:24]
    return f"urn:sopds:{suffix}"


def _element(parent: ET.Element, tag: str, text: object | None = None, **attrs: str) -> ET.Element:
    node = ET.SubElement(
        parent, f"{{{ATOM}}}{tag}", {key: clean(value) for key, value in attrs.items()}
    )
    if text is not None:
        node.text = clean(text)
    return node


def _link(parent: ET.Element, rel: str, href: str, media_type: str) -> None:
    _element(parent, "link", rel=rel, href=href, type=media_type)


def _feed(
    *,
    feed_id: str,
    title: str,
    updated_at: datetime,
    self_url: str,
    start_url: str,
    up_url: str | None,
    kind: str,
    search_url: str,
    next_url: str | None,
) -> ET.Element:
    root = ET.Element(f"{{{ATOM}}}feed")
    _element(root, "id", feed_id)
    _element(root, "title", title)
    _element(root, "updated", rfc3339(updated_at))
    author = _element(root, "author")
    _element(author, "name", "SOPDS")
    _link(root, "self", self_url, kind)
    _link(root, "start", start_url, NAVIGATION_TYPE)
    if up_url is not None:
        _link(root, "up", up_url, NAVIGATION_TYPE)
    _link(root, "search", search_url, OPENSEARCH_TYPE)
    if next_url is not None:
        _link(root, "next", next_url, kind)
    return root


def navigation_feed(
    *,
    feed_id: str,
    title: str,
    updated_at: datetime,
    self_url: str,
    start_url: str,
    up_url: str | None,
    search_url: str,
    entries: tuple[tuple[str, str, str | None, str, str], ...],
    next_url: str | None = None,
) -> bytes:
    root = _feed(
        feed_id=feed_id,
        title=title,
        updated_at=updated_at,
        self_url=self_url,
        start_url=start_url,
        up_url=up_url,
        kind=NAVIGATION_TYPE,
        search_url=search_url,
        next_url=next_url,
    )
    for entry_id, entry_title, entry_content, href, media_type in entries:
        entry = _element(root, "entry")
        _element(entry, "id", entry_id)
        _element(entry, "title", entry_title)
        _element(entry, "updated", rfc3339(updated_at))
        if entry_content is not None:
            _element(entry, "content", entry_content, type="text")
        _link(entry, SUBSECTION_REL, href, media_type)
    return cast(bytes, ET.tostring(root, encoding="utf-8", xml_declaration=True))


def display_author_name(value: str) -> str:
    """Present INPX name components without exposing their storage delimiters."""
    if "," not in value:
        return value
    return " ".join(component.strip() for component in value.split(",") if component.strip())


def item_entries(
    kind: str,
    items: tuple[NavigationItem, ...],
    destination_urls: tuple[str, ...],
    media_type: str = ACQUISITION_TYPE,
    count_kind: str | None = None,
) -> tuple[tuple[str, str, str | None, str, str], ...]:
    count_labels = {
        "authors": ("author", "authors"),
        "series": ("series", "series"),
        "titles": ("book", "books"),
    }

    def content(item: NavigationItem) -> str | None:
        if item.count is None:
            return None
        singular, plural = count_labels.get(
            item.count_kind or count_kind or kind, ("entry", "entries")
        )
        return f"{item.count} {singular if item.count == 1 else plural}"

    return tuple(
        (
            stable_id("navigation", [kind, item.value]),
            display_author_name(item.label) if kind == "authors" else item.label,
            content(item),
            href,
            media_type,
        )
        for item, href in zip(items, destination_urls, strict=True)
    )


def acquisition_feed(
    *,
    feed_id: str,
    title: str,
    updated_at: datetime,
    self_url: str,
    start_url: str,
    up_url: str,
    search_url: str,
    next_url: str | None,
    books: tuple[BookSummary, ...],
    book_urls: tuple[str, ...],
    download_urls: tuple[str, ...],
) -> bytes:
    root = _feed(
        feed_id=feed_id,
        title=title,
        updated_at=updated_at,
        self_url=self_url,
        start_url=start_url,
        up_url=up_url,
        kind=ACQUISITION_TYPE,
        search_url=search_url,
        next_url=next_url,
    )
    for book, alternate_url, download_url in zip(books, book_urls, download_urls, strict=True):
        entry = _element(root, "entry")
        _element(entry, "id", stable_id("book", book.public_id))
        _element(entry, "title", book.title)
        _element(entry, "updated", rfc3339(book.updated_at))
        authors = book.authors or ("Unknown author",)
        for name in authors:
            author = _element(entry, "author")
            _element(author, "name", display_author_name(name))
        for code, label in book.genres:
            _element(entry, "category", term=code, label=label)
        if book.language:
            node = ET.SubElement(entry, f"{{{DC}}}language")
            node.text = clean(book.language)
        if book.published_date:
            node = ET.SubElement(entry, f"{{{DC}}}issued")
            node.text = book.published_date.isoformat()
        if book.libid:
            node = ET.SubElement(entry, f"{{{DC}}}identifier")
            node.text = clean(book.libid)
        node = ET.SubElement(entry, f"{{{DC}}}format")
        node.text = clean(media_type_for(book.original_format))
        details: list[str] = []
        if book.series:
            series = book.series
            if book.series_number:
                series += f" #{book.series_number}"
            details.append(f"Series: {series}")
        details.append(f"Size: {book.size} bytes")
        if book.rating is not None:
            details.append(f"Rating: {book.rating}")
        if book.keywords:
            details.append(f"Keywords: {book.keywords}")
        _element(entry, "content", "\n".join(details), type="text")
        _link(entry, "alternate", alternate_url, "text/html")
        _element(
            entry,
            "link",
            rel=ACQUISITION_REL,
            href=download_url,
            type=media_type_for(book.original_format),
            length=str(book.size),
        )
    return cast(bytes, ET.tostring(root, encoding="utf-8", xml_declaration=True))


def open_search(base_path: str) -> bytes:
    root = ET.Element(f"{{{OPENSEARCH}}}OpenSearchDescription")
    for name, value in (
        ("ShortName", "SOPDS"),
        ("Description", "Search the SOPDS book catalog"),
        ("InputEncoding", "UTF-8"),
        ("OutputEncoding", "UTF-8"),
    ):
        child = ET.SubElement(root, f"{{{OPENSEARCH}}}{name}")
        child.text = value
    ET.SubElement(
        root,
        f"{{{OPENSEARCH}}}Url",
        {
            "type": clean(ACQUISITION_TYPE),
            "template": clean(f"{base_path}/opds/books/?q={{searchTerms}}"),
        },
    )
    return cast(bytes, ET.tostring(root, encoding="utf-8", xml_declaration=True))


def query_url(base_path: str, path: str, values: dict[str, str | None]) -> str:
    present = {key: value for key, value in values.items() if value not in (None, "")}
    query = urlencode(present)
    return f"{base_path}{path}" + (f"?{query}" if query else "")
