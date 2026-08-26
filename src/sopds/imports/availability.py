"""Contain archive availability probes within the configured archive root."""

from pathlib import Path


def archive_is_available(root: Path, relative_path: str) -> bool:
    """Reject path traversal and symlink escapes before probing archive availability."""
    try:
        resolved_root = root.resolve()
        candidate = (resolved_root / relative_path).resolve()
        return candidate.is_relative_to(resolved_root) and candidate.is_file()
    except OSError:
        return False


def archive_availability(root: Path, relative_paths: set[str]) -> dict[str, bool]:
    return {relative: archive_is_available(root, relative) for relative in relative_paths}


def archive_availability_rows(root: Path, archives: list[tuple[int, str]]) -> dict[int, bool]:
    return {
        archive_id: archive_is_available(root, relative_path)
        for archive_id, relative_path in archives
    }
