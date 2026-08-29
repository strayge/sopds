"""Deterministic converter capability registration and lookup."""

from collections.abc import Iterable
from dataclasses import dataclass

from sopds.conversion.contracts import (
    ConversionCapability,
    Converter,
    UnsupportedConversionError,
    normalize_format,
)
from sopds.conversion.policy import OUTPUT_POLICY, OutputDecision


@dataclass(frozen=True, slots=True)
class RegisteredCapability:
    capability: ConversionCapability
    converter: Converter


class ConverterRegistry:
    """Immutable registry that rejects ambiguous source-target ownership."""

    def __init__(self, converters: Iterable[Converter] = ()) -> None:
        registrations: dict[tuple[str, str], RegisteredCapability] = {}
        for converter in converters:
            for capability in converter.capabilities:
                key = (capability.source_format, capability.target_format)
                output = OUTPUT_POLICY.choice(capability.target_format)
                if (
                    OUTPUT_POLICY.decision(*key) is not OutputDecision.CONVERT
                    or capability.target_media_type != output.media_type
                    or capability.target_extension != output.extension
                ):
                    raise ValueError(
                        "Converter capability does not match the canonical output policy"
                    )
                if key in registrations:
                    raise ValueError(
                        "Duplicate converter capability "
                        f"{capability.source_format}->{capability.target_format}"
                    )
                registrations[key] = RegisteredCapability(capability, converter)
        self._registrations = registrations

    def resolve(self, source_format: str, target_format: str) -> RegisteredCapability:
        key = (normalize_format(source_format), normalize_format(target_format))
        try:
            return self._registrations[key]
        except KeyError:
            raise UnsupportedConversionError("Requested conversion is unsupported") from None

    def capabilities(self) -> tuple[ConversionCapability, ...]:
        return tuple(self._registrations[key].capability for key in sorted(self._registrations))

    def __len__(self) -> int:
        return len(self._registrations)


__all__ = ["ConverterRegistry", "RegisteredCapability", "normalize_format"]
