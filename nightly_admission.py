"""Compatibility shim. Implementation: _internal.nightly.admission."""
from __future__ import annotations

from ._internal.nightly.admission import (
    NIGHTLY_TARGETS,
    candidate_is_allowed,
)

__all__ = ['NIGHTLY_TARGETS', 'candidate_is_allowed']
