"""Canonical reader-facing output formats and source-target decisions."""

from dataclasses import dataclass
from enum import Enum

from sopds.conversion.contracts import normalize_format


class OutputDecision(Enum):
    """Describe whether an output is original, pass-through, converted, or unavailable."""

    ORIGINAL = "original"
    PASSTHROUGH = "passthrough"
    CONVERT = "convert"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class OutputChoice:
    key: str
    label: str
    media_type: str | None
    extension: str | None


class OutputPolicy:
    """Keep capability presentation independent from converter execution and registration."""

    def __init__(self) -> None:
        choices = (
            OutputChoice("original", "Original", None, None),
            OutputChoice("epub", "EPUB", "application/epub+zip", "epub"),
            OutputChoice("azw3", "AZW3", "application/vnd.amazon.ebook", "azw3"),
        )
        self._choices = {choice.key: choice for choice in choices}
        self._ordered_choices = choices
        self._conversions = frozenset(
            {
                ("fb2", "epub"),
                ("fb2", "azw3"),
                ("epub", "azw3"),
            }
        )

    def choice(self, target_format: str) -> OutputChoice:
        try:
            key = normalize_format(target_format)
            return self._choices[key]
        except KeyError, ValueError:
            raise ValueError("Invalid output format") from None

    def choices(self) -> tuple[OutputChoice, ...]:
        return self._ordered_choices

    def decision(self, source_format: str, target_format: str) -> OutputDecision:
        try:
            source = normalize_format(source_format)
            target = self.choice(target_format).key
        except ValueError:
            return OutputDecision.UNSUPPORTED
        if target == "original":
            return OutputDecision.ORIGINAL
        if source == target and source in {"epub", "azw3"}:
            return OutputDecision.PASSTHROUGH
        if (source, target) in self._conversions:
            return OutputDecision.CONVERT
        return OutputDecision.UNSUPPORTED

    def targets_for(self, source_format: str) -> tuple[OutputChoice, ...]:
        """Return every supported choice, including represented same-format pass-throughs."""
        return tuple(
            choice
            for choice in self._ordered_choices
            if self.decision(source_format, choice.key) is not OutputDecision.UNSUPPORTED
        )


OUTPUT_POLICY = OutputPolicy()

__all__ = ["OUTPUT_POLICY", "OutputChoice", "OutputDecision", "OutputPolicy"]
