"""Provider-compat tool port adapter. Tooling must not import or hold the Hermes class."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any, Mapping

from .ports import FactToolPort, MemoryCommandPort, ToolRuntimePort


def _call(host: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    fn = getattr(host, name, None)
    if not callable(fn):
        raise AttributeError(name)
    return fn(*args, **kwargs)


def _optional_call(host: Any, name: str, *args: Any, default: Any = None, **kwargs: Any) -> Any:
    fn = getattr(host, name, None)
    if not callable(fn):
        return default
    return fn(*args, **kwargs)


def _assembled_command_port(host: Any) -> MemoryCommandPort | None:
    """Return the already assembled composition command_port, if present."""

    composition = getattr(host, "_composition", None)
    return getattr(composition, "command_port", None) if composition is not None else None


class ProviderToolRuntimeAdapter:
    """Central compat face over the current Provider. D2 may replace the thin doors.

    Production writes use the composition ``command_port`` injected at bind
    time (or looked up on the host composition). Isolated FakeProvider /
    external hosts without a composition fall back to the legacy persist
    wrapper so ``store_now`` / ``_store_now`` hooks still intercept.
    """

    def __init__(self, host: Any, *, command_port: MemoryCommandPort | None = None) -> None:
        self._host = host
        self._bound_command_port = command_port

    def _resolve_command_port(self) -> MemoryCommandPort:
        if self._bound_command_port is not None:
            return self._bound_command_port
        assembled = _assembled_command_port(self._host)
        if assembled is not None:
            return assembled
        from .kernel import bind_memory_command_port

        return bind_memory_command_port(self._host)

    def _command_kernel(self) -> Any:
        from .kernel import COMMAND_KERNEL

        return COMMAND_KERNEL

    def query_connection(self) -> Any:
        fn = getattr(self._host, "query_connection", None)
        if callable(fn):
            return fn()
        fn = getattr(self._host, "_require_conn", None)
        if callable(fn):
            return fn()
        raise RuntimeError("query connection is unavailable")

    def query_lock(self) -> Any:
        fn = getattr(self._host, "query_lock", None)
        if callable(fn):
            return fn()
        lock = getattr(self._host, "_lock", None)
        return lock if lock is not None else nullcontext()

    def query_scope_view(self) -> dict[str, Any]:
        fn = getattr(self._host, "query_scope_view", None)
        if callable(fn):
            payload = fn()
            return dict(payload) if isinstance(payload, Mapping) else {}
        return {
            "scope_id": str(getattr(self._host, "_scope_id", "") or ""),
            "shared_scope_id": str(getattr(self._host, "_shared_scope_id", "") or ""),
            "accessible_scope_ids": list(getattr(self._host, "_accessible_scope_ids", []) or []),
            "writable_scope_ids": list(getattr(self._host, "_writable_scope_ids", []) or []),
            "shared_pool_scope_id": str(getattr(self._host, "_shared_pool_scope_id", "") or ""),
        }

    def vector_status_view(self) -> Mapping[str, Any]:
        payload = _optional_call(self._host, "vector_status_view", default={})
        return dict(payload) if isinstance(payload, Mapping) else {}

    def retrieval_status_view(self) -> Mapping[str, Any]:
        payload = _optional_call(self._host, "retrieval_status_view", default={})
        return dict(payload) if isinstance(payload, Mapping) else {}

    def runtime_status_view(self) -> Mapping[str, Any]:
        payload = _optional_call(self._host, "runtime_status_view", default={})
        return dict(payload) if isinstance(payload, Mapping) else {}

    def recall_service_view(self) -> Any:
        fn = getattr(self._host, "recall_service_view", None)
        if callable(fn):
            return fn()
        return getattr(self._host, "_recall_service", None)

    def clean_text(self, text: Any) -> str:
        fn = getattr(self._host, "_clean_text", None)
        if callable(fn):
            return str(fn(text) or "")
        return str(text or "")

    def session_id(self) -> str:
        return str(getattr(self._host, "_session_id", "") or "")

    def scope_object(self) -> Any:
        return getattr(self._host, "_scope", None)

    def scope_id(self) -> str:
        return str(self.query_scope_view().get("scope_id") or "")

    def shared_scope_id(self) -> str:
        return str(self.query_scope_view().get("shared_scope_id") or "")

    def shared_pool_scope_id(self) -> str:
        return str(self.query_scope_view().get("shared_pool_scope_id") or "")

    def writable_scope_ids(self) -> list[str]:
        return [str(item) for item in (self.query_scope_view().get("writable_scope_ids") or [])]

    def accessible_scope_ids(self) -> list[str]:
        return [str(item) for item in (self.query_scope_view().get("accessible_scope_ids") or [])]

    def scope_id_for_mode(self, scope_mode: str) -> str:
        if scope_mode == "shared_pool":
            return self.shared_pool_scope_id()
        if scope_mode == "shared":
            return self.shared_scope_id()
        return self.scope_id()

    def scope_mode_for(self, target: str, source: str = "") -> str:
        fn = getattr(self._host, "_scope_mode_for", None)
        if callable(fn):
            return str(fn(target, source))
        return "local"

    def config_view(self) -> dict[str, Any]:
        raw = getattr(self._host, "_config", {})
        return dict(raw) if isinstance(raw, Mapping) else {}

    def shared_pool_enabled(self) -> bool:
        return bool(getattr(self._host, "_shared_pool_enabled", False))

    def shared_pool_write_enabled(self) -> bool:
        return bool(getattr(self._host, "_shared_pool_write_enabled", False))

    def config_value(self, key: str, default: Any = None) -> Any:
        fn = getattr(self._host, "_config_value", None)
        if callable(fn):
            return fn(key, default)
        return self.config_view().get(key, default)

    def normalize_query(self, query: str, char_limit: int) -> str:
        fn = getattr(self._host, "_normalize_query", None)
        if callable(fn):
            return str(fn(query, char_limit) or "")
        return str(query or "")[: max(0, int(char_limit))]

    def retrieval_config_view(self) -> dict[str, Any]:
        raw = getattr(self._host, "_retrieval_config", {}) or {}
        return dict(raw) if isinstance(raw, Mapping) else {}

    def vector_store_view(self) -> Any:
        return getattr(self._host, "_vector_store", None)

    def writer_lifecycle_lock(self) -> Any:
        from ...write_kernel import _writer_lifecycle_lock

        return _writer_lifecycle_lock(self._host)

    def has_positive_write_authority(self) -> bool:
        from ...write_kernel import has_positive_write_authority

        return has_positive_write_authority(self._host)

    def rollback_conn_after_error(self, context: str) -> Any:
        return _optional_call(self._host, "_rollback_conn_after_error", context)

    def recover_sqlite_connection_after_error(self, context: str) -> Mapping[str, Any]:
        payload = _optional_call(
            self._host, "_recover_sqlite_connection_after_error", context, default={}
        )
        return payload if isinstance(payload, Mapping) else {}

    def store_now(self, *args: Any, **kwargs: Any) -> Any:
        return self._command_kernel().store(self._resolve_command_port(), *args, **kwargs)

    def update_memory(self, *args: Any, **kwargs: Any) -> Any:
        return self._command_kernel().update(self._resolve_command_port(), *args, **kwargs)

    def merge_memories(self, *args: Any, **kwargs: Any) -> Any:
        return self._command_kernel().merge(self._resolve_command_port(), *args, **kwargs)

    def archive_memories(self, *args: Any, **kwargs: Any) -> Any:
        return self._command_kernel().archive(self._resolve_command_port(), *args, **kwargs)

    def delete_memories(self, *args: Any, **kwargs: Any) -> Any:
        return self._command_kernel().delete(self._resolve_command_port(), *args, **kwargs)

    def feedback_memory(self, *args: Any, **kwargs: Any) -> Any:
        return self._command_kernel().feedback(self._resolve_command_port(), *args, **kwargs)

    def fact_owned_memory_ids(self, *args: Any, **kwargs: Any) -> Any:
        return _call(self._host, "_fact_owned_memory_ids", *args, **kwargs)

    def dedupe_memories(self, *args: Any, **kwargs: Any) -> Any:
        return self._command_kernel().dedupe(self._resolve_command_port(), *args, **kwargs)

    def govern_memories(self, *args: Any, **kwargs: Any) -> Any:
        return self._command_kernel().govern(self._resolve_command_port(), *args, **kwargs)

    def repair_vector(self, *args: Any, **kwargs: Any) -> Any:
        return self._command_kernel().repair(self._resolve_command_port(), *args, **kwargs)

    def hygiene_report(self, *args: Any, **kwargs: Any) -> Any:
        return _call(self._host, "_hygiene_report", *args, **kwargs)

    def stats_payload(self, *args: Any, **kwargs: Any) -> Any:
        return _call(self._host, "_stats_payload", *args, **kwargs)

    def inspect_memory(self, *args: Any, **kwargs: Any) -> Any:
        return _call(self._host, "_inspect_memory", *args, **kwargs)

    def explain_query(self, *args: Any, **kwargs: Any) -> Any:
        return _call(self._host, "_explain_query", *args, **kwargs)

    def export_memories(self, *args: Any, **kwargs: Any) -> Any:
        return _call(self._host, "_export_memories", *args, **kwargs)

    def context_payload(self, *args: Any, **kwargs: Any) -> Any:
        return _call(self._host, "_context_payload", *args, **kwargs)

    def profile_payload(self, *args: Any, **kwargs: Any) -> Any:
        return _call(self._host, "_profile_payload", *args, **kwargs)

    def probe_entity(self, *args: Any, **kwargs: Any) -> Any:
        return _call(self._host, "_probe_entity", *args, **kwargs)

    def related_entities(self, *args: Any, **kwargs: Any) -> Any:
        return _call(self._host, "_related_entities", *args, **kwargs)

    def benchmark_queries(self, *args: Any, **kwargs: Any) -> Any:
        return _call(self._host, "_benchmark_queries", *args, **kwargs)

    def run_reflection(self, args: Mapping[str, Any]) -> Any:
        from ...reflection_tooling import run_reflection_tool

        return run_reflection_tool(self, args=dict(args))

    def mark_vector_needs_repair(self, reason: str) -> None:
        from ...vector_runtime import mark_vector_needs_repair

        mark_vector_needs_repair(self._host, reason)

    def hermes_home_path(self) -> Any:
        from pathlib import Path

        return getattr(self._host, "_hermes_home", Path.home() / ".hermes")

    def reflection_transport(self) -> Any:
        return getattr(self._host, "_reflection_transport", None)

    def evidence_runtime(self) -> Any:
        return self._host


def bind_tool_runtime_port(
    obj: Any, *, command_port: MemoryCommandPort | None = None
) -> ToolRuntimePort:
    if isinstance(obj, ProviderToolRuntimeAdapter):
        if command_port is not None and obj._bound_command_port is None:
            obj._bound_command_port = command_port
        return obj
    existing = getattr(getattr(obj, "_composition", None), "tool_port", None)
    if isinstance(existing, ProviderToolRuntimeAdapter) and existing._host is obj:
        if command_port is not None and existing._bound_command_port is None:
            existing._bound_command_port = command_port
        return existing
    return ProviderToolRuntimeAdapter(obj, command_port=command_port)


def bind_fact_tool_port(obj: Any) -> FactToolPort:
    return bind_tool_runtime_port(obj)
