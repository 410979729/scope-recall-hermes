"""Query-time vector status snapshot and batch embed-query variants.

RuntimeComposition owns one instance. Provider keeps one-line delegates so
legacy callers and instance monkeypatches still resolve. These two methods
copy the accepted Provider bodies: list_records failures stay empty records,
embed stays gated on adapter readiness, and neither opens a transaction.
"""

from __future__ import annotations

from typing import Any, List

from .ports import RuntimeAdapterPort


class RuntimeVectorView:
    """Composition-held owner of the two remaining Provider vector/embed views."""

    def __init__(self, adapter: RuntimeAdapterPort) -> None:
        self.adapter = adapter

    def vector_status_view(self) -> dict[str, Any]:
        adapter: Any = self.adapter
        store = getattr(adapter, "_vector_store", None)
        embedder = getattr(adapter, "_embedder", None)
        describe = getattr(embedder, "describe", None)
        vector_config = dict(getattr(adapter, "_vector_config", None) or {})
        records: dict[str, Any] = {}
        list_records = getattr(store, "list_records", None) if store is not None else None
        if callable(list_records):
            try:
                raw_records = list_records()
            except Exception:
                raw_records = {}
            if isinstance(raw_records, dict):
                records = {
                    str(key): dict(value) if isinstance(value, dict) else value
                    for key, value in raw_records.items()
                    if key
                }
        return {
            "status": str(getattr(adapter, "_vector_status", "") or ""),
            "path": str(getattr(store, "db_path", "") or "") if store is not None else "",
            "table": str(getattr(store, "table_name", "") or "") if store is not None else "",
            "embedder": describe() if callable(describe) else {},
            "row_count": int(getattr(adapter, "_vector_row_count", 0) or 0),
            "unique_id_count": int(getattr(adapter, "_vector_unique_id_count", 0) or 0),
            "duplicate_row_count": int(getattr(adapter, "_vector_duplicate_row_count", 0) or 0),
            "enabled": bool(getattr(adapter, "_vector_enabled", False)),
            "ready": bool(getattr(adapter, "_vector_ready", False)),
            "message": str(getattr(adapter, "_vector_message", "") or ""),
            "backend": str(getattr(adapter, "_vector_backend", "") or ""),
            "sync_mode": str(vector_config.get("sync_mode") or "incremental"),
            "fallback_embedder": dict(vector_config.get("fallback_embedder") or {}),
            "records": records,
        }

    def embed_query_variants(self, queries: List[str]) -> List[List[float]]:
        """Batch query embeddings when the active vector generation is ready."""

        adapter: Any = self.adapter
        if not adapter._vector_ready or adapter._embedder is None:
            return []
        return adapter._embedder.embed_queries(queries)
