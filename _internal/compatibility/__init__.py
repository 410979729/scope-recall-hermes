"""Explicit compatibility inventory for architecture migrations."""

from .registry import (
    COMPATIBILITY_REGISTRY,
    PROGRAM_1A_COMPATIBILITY_IDS,
    CompatibilityShim,
    validate_compatibility_registry,
)

__all__ = [
    "COMPATIBILITY_REGISTRY",
    "PROGRAM_1A_COMPATIBILITY_IDS",
    "CompatibilityShim",
    "validate_compatibility_registry",
]
