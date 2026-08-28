"""Shared orchestration and HTTP-safe metadata for original acquisition."""

import re
import unicodedata
from urllib.parse import quote

from sopds.acquisition.contracts import (
    AcquiredOriginal,
    AcquisitionNotFoundError,
    AcquisitionRepository,
    AcquisitionTarget,
    OriginalDescription,
    OriginalStore,
)

_MEDIA_TYPES = {
    "fb2": "application/x-fictionbook+xml",
    "epub": "application/epub+zip",
    "mobi": "application/x-mobipocket-ebook",
    "azw3": "application/vnd.amazon.ebook",
    "pdf": "application/pdf",
    "djvu": "image/vnd.djvu",
    "txt": "text/plain; charset=utf-8",
}
_UNSAFE_FILENAME = re.compile(r"[/\\\"]")
_UNSAFE_EXTENSION = re.compile(r"[^A-Za-z0-9_-]")


def _safe_filename_character(character: str) -> str:
    if _UNSAFE_FILENAME.fullmatch(character) or unicodedata.category(character) in {"Cc", "Cf"}:
        return "_"
    return character


class AcquisitionService:
    """Resolve one active snapshot and transfer ownership of its opened stream."""

    def __init__(self, repository: AcquisitionRepository, store: OriginalStore) -> None:
        self._repository = repository
        self._store = store

    async def _target(
        self,
        public_id: str,
        expected_generation_id: int | None,
    ) -> AcquisitionTarget:
        if not public_id or len(public_id) > 64 or "\x00" in public_id:
            raise AcquisitionNotFoundError("Original is unavailable")
        target = (
            await self._repository.acquisition_target(public_id)
            if expected_generation_id is None
            else await self._repository.acquisition_target(
                public_id,
                expected_generation_id=expected_generation_id,
            )
        )
        if target is None:
            raise AcquisitionNotFoundError("Original is unavailable")
        return target

    async def describe(
        self,
        public_id: str,
        *,
        expected_generation_id: int | None = None,
    ) -> OriginalDescription:
        target = await self._target(public_id, expected_generation_id)
        revision = await self._store.describe(target)
        return OriginalDescription(
            public_id=target.public_id,
            title=target.title,
            source_format=target.original_format,
            content_length=target.expected_size,
            revision=revision,
        )

    async def acquire(
        self,
        public_id: str,
        *,
        expected_generation_id: int | None = None,
    ) -> AcquiredOriginal:
        target = await self._target(public_id, expected_generation_id)
        stream = await self._store.open(target)
        return AcquiredOriginal(
            filename=safe_download_filename(target.title, target.original_format),
            media_type=media_type_for(target.original_format),
            content_length=target.expected_size,
            source_format=target.original_format,
            source_revision=stream.source_revision,
            stream=stream,
        )

    async def shutdown(self) -> None:
        await self._store.shutdown()


def media_type_for(original_format: str) -> str:
    return _MEDIA_TYPES.get(original_format.casefold().lstrip("."), "application/octet-stream")


def safe_download_filename(title: str, original_format: str) -> str:
    """Preserve readable Unicode while removing header and path metacharacters."""
    stem = "".join(_safe_filename_character(character) for character in title)
    stem = " ".join(stem.split()).strip(" .")
    if not stem or stem in {".", ".."}:
        stem = "book"
    extension = _UNSAFE_EXTENSION.sub("", original_format.lstrip("."))
    if not extension:
        extension = "bin"
    return f"{stem}.{extension}"


def content_disposition(filename: str) -> str:
    """Provide interoperable ASCII and RFC 5987 UTF-8 filename parameters."""
    safe_filename = "".join(_safe_filename_character(character) for character in filename)
    stem, separator, extension = safe_filename.rpartition(".")
    if not separator:
        stem, extension = safe_filename, ""
    fallback_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    fallback_stem = "".join(_safe_filename_character(character) for character in fallback_stem)
    fallback_stem = fallback_stem.strip(" .") or "book"
    fallback_extension = _UNSAFE_EXTENSION.sub(
        "", unicodedata.normalize("NFKD", extension).encode("ascii", "ignore").decode()
    )
    fallback = f"{fallback_stem}.{fallback_extension}" if fallback_extension else fallback_stem
    encoded = quote(safe_filename.encode("utf-8"), safe="!#$&+-.^_`|~")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"
