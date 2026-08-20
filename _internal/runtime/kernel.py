"""Query and command facades. No SQL and no Hermes lifecycle."""

from __future__ import annotations

from typing import Any

from .ports import MemoryCommandPort, MemoryQueryPort
from ... import memory_ops, write_kernel
from ...memory_queries import (
    benchmark_queries,
    context_payload,
    explain_query,
    export_memories,
    hygiene_report,
    inspect_memory,
    probe_entity,
    profile_payload,
    related_entities,
    stats_payload,
)


class QueryKernel:
    """Thin query/diagnostic facade used by the Hermes adapter."""

    def context(self, port: MemoryQueryPort, **kwargs: Any) -> dict[str, Any]:
        return context_payload(port, **kwargs)

    def profile(self, port: MemoryQueryPort, **kwargs: Any) -> dict[str, Any]:
        return profile_payload(port, **kwargs)

    def inspect(self, port: MemoryQueryPort, **kwargs: Any) -> dict[str, Any]:
        return inspect_memory(port, **kwargs)

    def explain(self, port: MemoryQueryPort, **kwargs: Any) -> dict[str, Any]:
        return explain_query(port, **kwargs)

    def stats(self, port: MemoryQueryPort) -> dict[str, Any]:
        return stats_payload(port)

    def probe(self, port: MemoryQueryPort, **kwargs: Any) -> dict[str, Any]:
        return probe_entity(port, **kwargs)

    def related(self, port: MemoryQueryPort, **kwargs: Any) -> dict[str, Any]:
        return related_entities(port, **kwargs)

    def export(self, port: MemoryQueryPort, **kwargs: Any) -> dict[str, Any]:
        return export_memories(port, **kwargs)

    def hygiene(self, port: MemoryQueryPort, **kwargs: Any) -> dict[str, Any]:
        return hygiene_report(port, **kwargs)

    def benchmark(self, port: MemoryQueryPort, **kwargs: Any) -> dict[str, Any]:
        return benchmark_queries(port, **kwargs)


class _LegacyPersistCommandPort:
    """Isolated-host fallback for FakeProvider and external hosts.

    Production Provider and Tooling writes use the assembled composition
    ``command_port``. This wrapper exists only so hosts without a
    composition can still intercept ``store_now`` / ``_store_now``.
    CommandKernel must not select it for a live Provider.
    """

    def __init__(self, host: Any) -> None:
        self._host = host

    def write_target(self) -> object:
        return self._host

    def query_connection(self) -> Any:
        raise RuntimeError("legacy command port has no query connection")

    def query_lock(self) -> Any:
        raise RuntimeError("legacy command port has no query lock")

    def rollback_conn_after_error(self, context: str) -> None:
        fn = getattr(self._host, "rollback_conn_after_error", None)
        if callable(fn):
            fn(context)
            return
        fn = getattr(self._host, "_rollback_conn_after_error", None)
        if callable(fn):
            fn(context)

    def store_now(self, *args: Any, **kwargs: Any) -> Any:
        return self.execute_store_now(*args, **kwargs)

    def execute_store_now(self, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self._host, "store_now", None)
        if callable(fn):
            return fn(*args, **kwargs)
        fn = getattr(self._host, "_store_now", None)
        if not callable(fn):
            raise AttributeError("store_now")
        return fn(*args, **kwargs)

    def execute_update_memory(self, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self._host, "_update_memory", None)
        if not callable(fn):
            raise AttributeError("update_memory")
        return fn(*args, **kwargs)

    def execute_merge_memories(self, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self._host, "_merge_memories", None)
        if not callable(fn):
            raise AttributeError("merge_memories")
        return fn(*args, **kwargs)

    def execute_archive_memories(self, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self._host, "_archive_memories", None)
        if not callable(fn):
            raise AttributeError("archive_memories")
        return fn(*args, **kwargs)

    def execute_feedback_memory(self, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self._host, "_feedback_memory", None)
        if not callable(fn):
            raise AttributeError("feedback_memory")
        return fn(*args, **kwargs)

    def execute_govern_memories(self, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self._host, "_govern_memories", None)
        if not callable(fn):
            raise AttributeError("govern_memories")
        return fn(*args, **kwargs)

    def execute_delete_memories(self, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self._host, "_delete_memories", None)
        if not callable(fn):
            raise AttributeError("delete_memories")
        return fn(*args, **kwargs)

    def execute_dedupe_memories(self, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self._host, "_dedupe_memories", None)
        if not callable(fn):
            raise AttributeError("dedupe_memories")
        return fn(*args, **kwargs)

    def execute_repair_vector(self, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self._host, "_repair_vector", None)
        if not callable(fn):
            raise AttributeError("repair_vector")
        return fn(*args, **kwargs)

    def command_update_memory(self, *args: Any, **kwargs: Any) -> Any:
        return self.execute_update_memory(*args, **kwargs)

    def command_merge_memories(self, *args: Any, **kwargs: Any) -> Any:
        return self.execute_merge_memories(*args, **kwargs)

    def command_archive_memories(self, *args: Any, **kwargs: Any) -> Any:
        return self.execute_archive_memories(*args, **kwargs)

    def command_delete_memories(self, *args: Any, **kwargs: Any) -> Any:
        return self.execute_delete_memories(*args, **kwargs)

    def command_feedback_memory(self, *args: Any, **kwargs: Any) -> Any:
        return self.execute_feedback_memory(*args, **kwargs)

    def command_govern_memories(self, *args: Any, **kwargs: Any) -> Any:
        return self.execute_govern_memories(*args, **kwargs)

    def command_dedupe_memories(self, *args: Any, **kwargs: Any) -> Any:
        return self.execute_dedupe_memories(*args, **kwargs)

    def command_repair_vector(self, *args: Any, **kwargs: Any) -> Any:
        return self.execute_repair_vector(*args, **kwargs)

    def fact_owned_memory_ids(self, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self._host, "fact_owned_memory_ids", None)
        if not callable(fn):
            raise AttributeError("fact_owned_memory_ids")
        return fn(*args, **kwargs)

    def clean_text(self, text: object) -> str:
        fn = getattr(self._host, "clean_text", None)
        if callable(fn):
            return str(fn(text) or "")
        return str(text or "")

    def config_view(self) -> dict[str, object]:
        fn = getattr(self._host, "config_view", None)
        if callable(fn):
            raw = fn()
            return dict(raw) if isinstance(raw, dict) else {}
        return {}

    def config_value(self, key: str, default: object = None) -> object:
        fn = getattr(self._host, "config_value", None)
        if callable(fn):
            return fn(key, default)
        return self.config_view().get(key, default)

    def query_scope_view(self) -> Any:
        fn = getattr(self._host, "query_scope_view", None)
        if callable(fn):
            return fn()
        return {}

    def scope_id(self) -> str:
        return str((self.query_scope_view() or {}).get("scope_id") or "")

    def shared_scope_id(self) -> str:
        return str((self.query_scope_view() or {}).get("shared_scope_id") or "")

    def shared_pool_scope_id(self) -> str:
        return str((self.query_scope_view() or {}).get("shared_pool_scope_id") or "")

    def writable_scope_ids(self) -> list[str]:
        return [
            str(item)
            for item in ((self.query_scope_view() or {}).get("writable_scope_ids") or [])
            if str(item)
        ]

    def vector_status_view(self) -> Any:
        fn = getattr(self._host, "vector_status_view", None)
        if callable(fn):
            return fn()
        return {}

    def has_positive_write_authority(self) -> bool:
        fn = getattr(self._host, "has_positive_write_authority", None)
        return bool(fn()) if callable(fn) else False

    def writer_lifecycle_lock(self) -> object:
        fn = getattr(self._host, "writer_lifecycle_lock", None)
        if callable(fn):
            return fn()
        return object()


def bind_memory_command_port(obj: Any) -> MemoryCommandPort:
    """Wrap isolated-host persist hooks. Production uses composition.command_port."""

    if isinstance(obj, _LegacyPersistCommandPort):
        return obj
    return _LegacyPersistCommandPort(obj)


class CommandKernel:
    """Owns memory_ops write boundaries. Callers pass MemoryCommandPort only."""

    def store(
        self,
        port: MemoryCommandPort,
        *args: Any,
        store_memory_now: Any = None,
        **kwargs: Any,
    ) -> Any:
        if store_memory_now is None and isinstance(port, _LegacyPersistCommandPort):
            return port.execute_store_now(*args, **kwargs)
        fn = memory_ops.store_memory_now if store_memory_now is None else store_memory_now
        target = port.write_target()
        with write_kernel.hold_positive_write_authority(target):
            try:
                return fn(target, *args, **kwargs)
            except Exception:
                port.rollback_conn_after_error("store_now")
                raise

    def update(self, port: MemoryCommandPort, *args: Any, **kwargs: Any) -> Any:
        if isinstance(port, _LegacyPersistCommandPort):
            return port.execute_update_memory(*args, **kwargs)
        target = port.write_target()
        return memory_ops.update_memory(target, *args, **kwargs)

    def merge(self, port: MemoryCommandPort, *args: Any, **kwargs: Any) -> Any:
        if isinstance(port, _LegacyPersistCommandPort):
            return port.execute_merge_memories(*args, **kwargs)
        target = port.write_target()
        return memory_ops.merge_memories(target, *args, **kwargs)

    def archive(self, port: MemoryCommandPort, ids: list[str], **kwargs: Any) -> Any:
        if isinstance(port, _LegacyPersistCommandPort):
            return port.execute_archive_memories(ids, **kwargs)
        target = port.write_target()
        return memory_ops.archive_memories(target, ids, **kwargs)

    def feedback(self, port: MemoryCommandPort, **kwargs: Any) -> Any:
        if isinstance(port, _LegacyPersistCommandPort):
            return port.execute_feedback_memory(**kwargs)
        return memory_ops.feedback_memory(port.write_target(), **kwargs)

    def govern(self, port: MemoryCommandPort, **kwargs: Any) -> Any:
        if isinstance(port, _LegacyPersistCommandPort):
            return port.execute_govern_memories(**kwargs)
        return memory_ops.govern_memories(port.write_target(), **kwargs)

    def delete(self, port: MemoryCommandPort, ids: list[str]) -> Any:
        if isinstance(port, _LegacyPersistCommandPort):
            return port.execute_delete_memories(ids)
        target = port.write_target()
        return memory_ops.delete_memories(target, ids)

    def dedupe(self, port: MemoryCommandPort, **kwargs: Any) -> Any:
        if isinstance(port, _LegacyPersistCommandPort):
            return port.execute_dedupe_memories(**kwargs)
        return memory_ops.dedupe_memories(port.write_target(), **kwargs)

    def repair(self, port: MemoryCommandPort) -> Any:
        if isinstance(port, _LegacyPersistCommandPort):
            return port.execute_repair_vector()
        return memory_ops.repair_vector(port.write_target())


class RuntimeKernel(QueryKernel, CommandKernel):
    """Compatibility facade used by existing KERNEL imports."""


KERNEL = RuntimeKernel()
COMMAND_KERNEL = KERNEL
