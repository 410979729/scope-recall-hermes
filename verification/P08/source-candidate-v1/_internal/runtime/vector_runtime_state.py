"""Explicit compatibility view over legacy Provider-owned vector state.

Program 1A keeps the physical vector algorithms unchanged while replacing
hundreds of scattered Provider-private reads with one owned adapter.  Every
legacy field is listed explicitly so this boundary is searchable, reviewable,
and removable; there is deliberately no generic ``__getattr__`` dispatch.
"""

from __future__ import annotations

from typing import Any


class ProviderVectorRuntimeState:
    """Public state/capability names backed by one legacy Provider host."""

    def __init__(self, host: Any) -> None:
        self._host = host

    def binds(self, host: Any) -> bool:
        return self._host is host

    @property
    def config(self) -> Any:
        return getattr(self._host, "_config", None)

    @property
    def db_path(self) -> Any:
        return getattr(self._host, "_db_path", None)

    @property
    def embedder(self) -> Any:
        return getattr(self._host, "_embedder", None)

    @embedder.setter
    def embedder(self, value: Any) -> None:
        self._host._embedder = value

    @property
    def lock(self) -> Any:
        return getattr(self._host, "_lock", None)

    @property
    def next_outbox_retention_at(self) -> Any:
        return getattr(self._host, "_next_outbox_retention_at", 0.0)

    @next_outbox_retention_at.setter
    def next_outbox_retention_at(self, value: Any) -> None:
        self._host._next_outbox_retention_at = value

    @property
    def outbox_retention_contention_skips(self) -> Any:
        return getattr(self._host, "_outbox_retention_contention_skips", 0)

    @outbox_retention_contention_skips.setter
    def outbox_retention_contention_skips(self, value: Any) -> None:
        self._host._outbox_retention_contention_skips = value

    def require_conn(self) -> Any:
        return self._host._require_conn()

    @property
    def retrieval_config(self) -> Any:
        return getattr(self._host, "_retrieval_config", {})

    @property
    def scope_id(self) -> Any:
        return getattr(self._host, "_scope_id", "")

    @property
    def storage_dir(self) -> Any:
        return getattr(self._host, "_storage_dir", None)

    @property
    def vector_auto_recoverable(self) -> Any:
        return getattr(self._host, "_vector_auto_recoverable", False)

    @vector_auto_recoverable.setter
    def vector_auto_recoverable(self, value: Any) -> None:
        self._host._vector_auto_recoverable = value

    @property
    def vector_backend(self) -> Any:
        return getattr(self._host, "_vector_backend", "")

    @vector_backend.setter
    def vector_backend(self, value: Any) -> None:
        self._host._vector_backend = value

    @property
    def vector_config(self) -> Any:
        return getattr(self._host, "_vector_config", {})

    @property
    def vector_debt_counts(self) -> Any:
        return getattr(self._host, "_vector_debt_counts", None)

    @vector_debt_counts.setter
    def vector_debt_counts(self, value: Any) -> None:
        self._host._vector_debt_counts = value

    @property
    def vector_duplicate_row_count(self) -> Any:
        return getattr(self._host, "_vector_duplicate_row_count", 0)

    @vector_duplicate_row_count.setter
    def vector_duplicate_row_count(self, value: Any) -> None:
        self._host._vector_duplicate_row_count = value

    @property
    def vector_enabled(self) -> Any:
        return getattr(self._host, "_vector_enabled", False)

    @vector_enabled.setter
    def vector_enabled(self, value: Any) -> None:
        self._host._vector_enabled = value

    @property
    def vector_generation_id(self) -> Any:
        return getattr(self._host, "_vector_generation_id", "")

    @vector_generation_id.setter
    def vector_generation_id(self, value: Any) -> None:
        self._host._vector_generation_id = value

    @property
    def vector_lock(self) -> Any:
        return getattr(self._host, "_vector_lock", None)

    @property
    def vector_message(self) -> Any:
        return getattr(self._host, "_vector_message", "")

    @vector_message.setter
    def vector_message(self, value: Any) -> None:
        self._host._vector_message = value

    @property
    def vector_ready(self) -> Any:
        return getattr(self._host, "_vector_ready", False)

    @vector_ready.setter
    def vector_ready(self, value: Any) -> None:
        self._host._vector_ready = value

    @property
    def vector_reason_code(self) -> Any:
        return getattr(self._host, "_vector_reason_code", "")

    @vector_reason_code.setter
    def vector_reason_code(self, value: Any) -> None:
        self._host._vector_reason_code = value

    @property
    def vector_reconciliation(self) -> Any:
        return getattr(self._host, "_vector_reconciliation", None)

    @vector_reconciliation.setter
    def vector_reconciliation(self, value: Any) -> None:
        self._host._vector_reconciliation = value

    @property
    def vector_repair_required(self) -> Any:
        return getattr(self._host, "_vector_repair_required", False)

    @vector_repair_required.setter
    def vector_repair_required(self, value: Any) -> None:
        self._host._vector_repair_required = value

    @property
    def vector_replay_degraded(self) -> Any:
        return getattr(self._host, "_vector_replay_degraded", False)

    @vector_replay_degraded.setter
    def vector_replay_degraded(self, value: Any) -> None:
        self._host._vector_replay_degraded = value

    @property
    def vector_row_count(self) -> Any:
        return getattr(self._host, "_vector_row_count", 0)

    @vector_row_count.setter
    def vector_row_count(self, value: Any) -> None:
        self._host._vector_row_count = value

    @property
    def vector_status(self) -> Any:
        return getattr(self._host, "_vector_status", "")

    @vector_status.setter
    def vector_status(self, value: Any) -> None:
        self._host._vector_status = value

    @property
    def vector_storage_dir(self) -> Any:
        return getattr(self._host, "_vector_storage_dir", None)

    @vector_storage_dir.setter
    def vector_storage_dir(self, value: Any) -> None:
        self._host._vector_storage_dir = value

    @property
    def vector_store(self) -> Any:
        return getattr(self._host, "_vector_store", None)

    @vector_store.setter
    def vector_store(self, value: Any) -> None:
        self._host._vector_store = value

    def vector_text(self, summary: Any, content: Any) -> Any:
        return self._host._vector_text(summary, content)

    @property
    def vector_unique_id_count(self) -> Any:
        return getattr(self._host, "_vector_unique_id_count", 0)

    @vector_unique_id_count.setter
    def vector_unique_id_count(self, value: Any) -> None:
        self._host._vector_unique_id_count = value

    @property
    def vector_usable_for_query(self) -> Any:
        return getattr(self._host, "_vector_usable_for_query", False)

    @vector_usable_for_query.setter
    def vector_usable_for_query(self, value: Any) -> None:
        self._host._vector_usable_for_query = value


def bind_provider_vector_runtime(host: Any) -> ProviderVectorRuntimeState:
    """Return an idempotent explicit state view for legacy vector algorithms."""

    if isinstance(host, ProviderVectorRuntimeState):
        return host
    cache_name = "_scope_recall_vector_runtime_state"
    cached = getattr(host, cache_name, None)
    if isinstance(cached, ProviderVectorRuntimeState) and cached.binds(host):
        return cached
    state = ProviderVectorRuntimeState(host)
    try:
        setattr(host, cache_name, state)
    except (AttributeError, TypeError):
        # Slot-only test doubles still receive a correct uncached adapter.
        pass
    return state
