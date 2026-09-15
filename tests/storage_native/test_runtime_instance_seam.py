"""One real RuntimeInstance seam against the isolated ProcessLance helper.

The test is intentionally outside the normal contract suite: its interpreter
must provide the approved isolated LanceDB installation.  No model or HTTP
route is used; both embedding ports are deterministic TEST dependencies.
"""
from __future__ import annotations

from dataclasses import replace
import importlib.util
from pathlib import Path
import sqlite3

import pytest

from scope_recall.contracts import InstanceBinding, TrustedContext
from scope_recall.core.recall_policy import RecallPolicy
from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig
from scope_recall.runtime.instance import (
    RuntimeInstanceConfig,
    VectorRuntimeConfig,
    build_runtime_instance,
    default_vector_factory,
)
from v11_support import source_event


pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("lancedb") is None,
    reason="approved NativePY LanceDB is required for this seam",
)


class DeterministicEmbedding:
    def embed_query(self, text: str, *, remaining_seconds: float):
        assert text and remaining_seconds > 0
        return (1.0, 0.0)

    def embed_source(self, source, *, remaining_seconds: float):
        assert source.event["content"] and remaining_seconds > 0
        return (1.0, 0.0)


def _request(query: str):
    return {
        "protocol_version": "1.1",
        "request_id": "TEST-runtime-native",
        "query": query,
        "mode": "current",
        "max_items": 6,
        "budget_tokens": 1200,
    }


def _delete_request(ref: str):
    return {
        "protocol_version": "1.1",
        "target_refs": [ref],
        "mode": "delete",
        "expected_revisions": {ref: 1},
    }


def test_runtime_instance_native_source_query_delete_purge(tmp_path: Path):
    binding = InstanceBinding(
        "TEST-runtime-native-agent",
        "TEST-runtime-native-installation",
        tmp_path / "truth",
        frozenset({"TEST-scope"}),
        True,
    )
    config = RuntimeInstanceConfig(
        binding=binding,
        session_id="worker-session-B",
        allowed_scope_ids=binding.scope_ids,
        request_seconds=45.0,
        drain_seconds=120.0,
        max_items=32,
        lease_seconds=60.0,
        auxiliary=AuxiliaryRuntimeConfig.from_mapping(
            {"external_embedding": False, "external_consolidation": False}
        ),
        vector=VectorRuntimeConfig(
            backend="lancedb",
            storage_dir=tmp_path / "vectors",
            table_name="TEST-runtime-vectors",
            dimensions=2,
            test_injection_override=True,
        ),
    )
    instance = build_runtime_instance(config, vector_factory=default_vector_factory)
    context_a = TrustedContext(binding, "human-session-A", binding.scope_ids, "human_direct")
    context_b = config.context()
    try:
        # Construction and SQLite status do not create/open the companion.
        instance.core.initialize()
        assert instance.status().sources == 0
        assert not (config.vector.storage_dir / "lancedb").exists()
        disabled_result = instance.recall(_request("完全没有词项重合的语义问题"))
        assert disabled_result.items == ()
        assert not (config.vector.storage_dir / "lancedb").exists()

        instance.auxiliary = replace(
            instance.auxiliary,
            query_embedding=DeterministicEmbedding(),
            source_embedding=DeterministicEmbedding(),
        )
        source_receipt = instance.core.record_event(
            context_a,
            source_event(
                source_event_key="TEST-runtime-native/source",
                content="海报四周压低明度，核心图案保留高光。",
            ),
            scope_id="TEST-scope",
            remaining_seconds=10,
        )
        source_ref = source_receipt.event_refs[0].ref
        # The seam focuses on source projection.  Consolidation is explicitly
        # acknowledged in this test; no model call is fabricated.
        with sqlite3.connect(instance.core.storage.path) as conn:
            conn.execute("UPDATE work_items SET state='done' WHERE work_type='consolidate'")
            conn.commit()

        # Only explicit background drain may create the first native index.
        drain = instance.drain()
        assert drain.completed == 1
        assert (config.vector.storage_dir / "lancedb").exists()
        instance.core.recall_pipeline.policy = RecallPolicy(vector_threshold=0.5)
        result = instance.recall(_request("什么设计手法吸引注意焦点？"))
        assert any(item.ref == source_ref for item in result.items)

        authorization = instance.core.record_event(
            context_a,
            source_event(
                source_event_key="TEST-runtime-native/delete-auth",
                content=f"删除 {source_ref}",
            ),
            scope_id="TEST-scope",
            remaining_seconds=10,
        )
        with sqlite3.connect(instance.core.storage.path) as conn:
            conn.execute("UPDATE work_items SET state='done' WHERE work_type IN ('embed','consolidate')")
            conn.commit()
        deleted = instance.core.forget(context_a, _delete_request(source_ref), remaining_seconds=10)
        assert deleted["layers"]["vector_active"] == "inventory_pending"
        purged = instance.drain()
        assert purged.completed == 1
        assert instance._vector_store.count_rows() == 0
        with instance.core.storage.read(context_b) as tx:
            receipt = tx.deletions.receipt(deleted["operation_id"])
        assert receipt["active_content_removed"] is True
        assert receipt["layers"]["vector_active"] == "removed"
        assert authorization.durability == "persisted"
    finally:
        instance.close()
