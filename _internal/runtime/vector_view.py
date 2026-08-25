"""Query-time vector status snapshot and batch embed-query variants.

RuntimeComposition owns one instance. Provider keeps one-line delegates so
legacy callers and instance monkeypatches still resolve. Status reports only
cached aggregates; it never lists records or deserializes vectors. Embed stays
gated on adapter readiness, and neither method opens a transaction.
"""

from __future__ import annotations

from typing import Any, List

from ...http_utils import safe_endpoint_display
from .ports import RuntimeAdapterPort


class RuntimeVectorView:
    """Composition-held owner of the two remaining Provider vector/embed views."""

    def __init__(self, adapter: RuntimeAdapterPort) -> None:
        self.adapter = adapter

    def vector_status_view(self) -> dict[str, Any]:
        """Return cached vector status and aggregate counts only.

        This view must not call ``list_records`` or deserialize physical
        vectors. Full ID/cardinality audit belongs to Doctor and explicit
        repair, not stats or ordinary status reads.
        """

        adapter: Any = self.adapter
        store = getattr(adapter, "_vector_store", None)
        embedder = getattr(adapter, "_embedder", None)
        describe = getattr(embedder, "describe", None)
        vector_config = dict(getattr(adapter, "_vector_config", None) or {})
        embedder_description = describe() if callable(describe) else {}
        if isinstance(embedder_description, dict):
            embedder_description = dict(embedder_description)
            if embedder_description.get("base_url"):
                embedder_description["base_url"] = safe_endpoint_display(
                    str(embedder_description["base_url"])
                )
        else:
            embedder_description = {}
        fallback_description = dict(vector_config.get("fallback_embedder") or {})
        if fallback_description.get("base_url"):
            fallback_description["base_url"] = safe_endpoint_display(
                str(fallback_description["base_url"])
            )
        return {
            "status": str(getattr(adapter, "_vector_status", "") or ""),
            "path": str(getattr(store, "db_path", "") or "") if store is not None else "",
            "table": str(getattr(store, "table_name", "") or "") if store is not None else "",
            "embedder": embedder_description,
            "row_count": int(getattr(adapter, "_vector_row_count", 0) or 0),
            "unique_id_count": int(getattr(adapter, "_vector_unique_id_count", 0) or 0),
            "duplicate_row_count": int(getattr(adapter, "_vector_duplicate_row_count", 0) or 0),
            "enabled": bool(getattr(adapter, "_vector_enabled", False)),
            "ready": bool(getattr(adapter, "_vector_ready", False)),
            "message": str(getattr(adapter, "_vector_message", "") or ""),
            "backend": str(getattr(adapter, "_vector_backend", "") or ""),
            "sync_mode": str(vector_config.get("sync_mode") or "incremental"),
            "fallback_embedder": fallback_description,
            "records": {},
        }

    def embed_query_variants(self, queries: List[str]) -> List[List[float]]:
        """Batch query embeddings when the active vector generation is ready."""

        adapter: Any = self.adapter
        if not adapter._vector_ready or adapter._embedder is None:
            return []
        return adapter._embedder.embed_queries(queries)
