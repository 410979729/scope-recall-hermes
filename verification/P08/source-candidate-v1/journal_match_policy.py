"""Compatibility shim. Implementation: _internal.journal.match_policy."""
from __future__ import annotations

from ._internal.journal.match_policy import (
    _WORKFLOW_CONTINUATION_TOKENS,
    _workflow_continuation_tokens,
    _is_workflow_continuation,
    _metadata_entities,
)

__all__ = ['_WORKFLOW_CONTINUATION_TOKENS', '_workflow_continuation_tokens', '_is_workflow_continuation', '_metadata_entities']
