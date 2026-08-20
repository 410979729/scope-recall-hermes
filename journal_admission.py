"""Compatibility shim. Implementation: _internal.journal.admission."""
from __future__ import annotations

from ._internal.journal.admission import (
    JOURNAL_TARGETS,
    LOW_VALUE_NOTIFICATION_RE,
    LOW_VALUE_LOG_RE,
    LOW_VALUE_PROGRESS_RE,
    EPHEMERAL_RELEASE_STATE_RE,
    TRANSIENT_PHASE_GATE_RE,
    HIGH_VALUE_DURABLE_SIGNAL_RE,
    _has_high_value_durable_signal,
    _low_value_promotion_reason,
    _candidate_rejection_reason,
    _candidate_allowed,
)

__all__ = ['JOURNAL_TARGETS', 'LOW_VALUE_NOTIFICATION_RE', 'LOW_VALUE_LOG_RE', 'LOW_VALUE_PROGRESS_RE', 'EPHEMERAL_RELEASE_STATE_RE', 'TRANSIENT_PHASE_GATE_RE', 'HIGH_VALUE_DURABLE_SIGNAL_RE', '_has_high_value_durable_signal', '_low_value_promotion_reason', '_candidate_rejection_reason', '_candidate_allowed']
