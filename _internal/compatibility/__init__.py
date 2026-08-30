"""Explicit compatibility inventory for architecture migrations."""

from .registry import (
    ALL_COMPATIBILITY_IDS,
    COMPATIBILITY_REGISTRY,
    PROGRAM_1A_COMPATIBILITY_IDS,
    PROGRAM_1B_COMPATIBILITY_IDS,
    CompatibilityShim,
    validate_compatibility_registry,
)

__all__ = [
    "ALL_COMPATIBILITY_IDS",
    "COMPATIBILITY_REGISTRY",
    "PROGRAM_1A_COMPATIBILITY_IDS",
    "PROGRAM_1B_COMPATIBILITY_IDS",
    "CompatibilityShim",
    "validate_compatibility_registry",
]
