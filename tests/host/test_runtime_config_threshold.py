from __future__ import annotations

from dataclasses import replace
import json

import pytest

from scope_recall.adapters.lance import LanceIndexWriter, LanceVectorRecord
from scope_recall.adapters.runtime_wiring import attach_trusted_host_runtime
from scope_recall.contracts import InstanceBinding
from scope_recall.core.recall_policy import EMBEDDING_SPACE, SPACE_ID
from scope_recall.runtime.instance import RuntimeInstanceConfig, build_runtime_instance, default_vector_factory
from v11_support import source_event


def test_runtime_mapping_wires_frozen_vector_threshold(tmp_path):
    binding = InstanceBinding(
        "TEST-threshold-agent",
        "TEST-threshold-installation",
        tmp_path / "data",
        frozenset({"TEST-threshold-scope"}),
        True,
    )
    config = RuntimeInstanceConfig.from_mapping(
        {
            "binding": {
                "agent_id": binding.agent_id,
                "installation_id": binding.installation_id,
                "data_directory": str(binding.data_directory),
                "scope_ids": sorted(binding.scope_ids),
                "test_mode": True,
            },
            "session_id": "TEST-threshold-session",
            "allowed_scope_ids": sorted(binding.scope_ids),
            "auxiliary": {"external_embedding": False, "external_consolidation": False},
            "vector_threshold": 0.653189984350642,
        }
    )
    instance = build_runtime_instance(config)
    try:
        assert instance.core.recall_pipeline.policy.vector_threshold == 0.653189984350642
    finally:
        instance.close()


class _QueryEmbedding:
    """Deterministic stand-in for the route's HTTP embedder; no model is called."""

    def __init__(self, dimensions: int) -> None:
        self.vector = (1.0,) + (0.0,) * (dimensions - 1)

    def embed_query(self, text: str, *, remaining_seconds: float):
        assert text and remaining_seconds > 0
        return self.vector


_NAMED_ROUTES = {
    # Stating the shipped values is still a named route: its request encoding
    # differs from the frozen descriptor's, so its space digest does too.
    "stated-gemini-defaults": {
        "model": EMBEDDING_SPACE["model"], "endpoint": EMBEDDING_SPACE["endpoint"],
        "dimensions": EMBEDDING_SPACE["dimensions"], "dialect": "gemini",
    },
    "openai-dialect-provider": {
        "model": "TEST-embedding", "endpoint": "https://example.invalid/v1/embeddings",
        "dimensions": 8, "dialect": "openai",
    },
}


@pytest.mark.parametrize("route_name", sorted(_NAMED_ROUTES))
def test_named_embedding_route_admits_its_own_vector_hits(tmp_path, route_name):
    """A host attaching a runtime config that names an embedding route recalls through its vectors.

    ``attach_trusted_host_runtime`` is the wiring Hermes, the Codex hooks and the
    Codex MCP server share.  While admission compared against the shipped space,
    every hit of such a route was refused as ``vector_old_or_mismatched_space``.
    """
    route = _NAMED_ROUTES[route_name]
    data = tmp_path / "d"
    data.mkdir()
    binding = InstanceBinding("TEST-agent", "TEST-installation", data, frozenset({"TEST-scope"}), True)
    raw = {
        "binding": {"agent_id": binding.agent_id, "installation_id": binding.installation_id,
                    "data_directory": str(data), "scope_ids": ["TEST-scope"], "test_mode": True},
        "session_id": "TEST-session",
        "allowed_scope_ids": ["TEST-scope"],
        "auxiliary": {"external_embedding": False, "external_consolidation": False,
                      "embedding": {"credential_env": "TEST_EMBED_KEY", **route}},
        "vector_threshold": 0.5,
    }
    space_id = RuntimeInstanceConfig.from_mapping(raw).embedding_space_id()
    assert space_id != SPACE_ID
    raw["vector"] = {"backend": "sqlite-bruteforce", "storage_dir": str(data / "vectors" / space_id),
                     "table_name": "TEST_vectors", "dimensions": route["dimensions"]}
    (data / "runtime-config.json").write_text(json.dumps(raw), encoding="utf-8")

    host = attach_trusted_host_runtime(config_path=None, expected_binding=binding,
                                       session_id="TEST-session", allowed_scope_ids=binding.scope_ids)
    try:
        assert host.configured, host.capability_gaps
        runtime = host.runtime
        assert host.core.recall_pipeline.policy.embedding_space_id == space_id
        context = runtime.config.context()
        host.core.initialize()
        event = host.core.record_event(
            context,
            source_event(source_event_key="TEST-named-route/source", content="海报四周压低明度，核心图案保留高光。"),
            scope_id="TEST-scope", remaining_seconds=10,
        ).event_refs[0]
        query = _QueryEmbedding(route["dimensions"])
        # The projection a drain writes: the configured space's partition, nothing else.
        store = default_vector_factory(runtime.config.vector)
        store.open()
        try:
            LanceIndexWriter(store).upsert_records([LanceVectorRecord(
                "event", event.ref, event.revision, "TEST-named-route-vector", space_id, query.vector,
                "TEST-scope", binding.agent_id, binding.installation_id,
            )])
        finally:
            store.close()
        runtime.auxiliary = replace(runtime.auxiliary, query_embedding=query)

        # No lexical overlap with the source: only the vector channel can admit it.
        request = {"protocol_version": "1.1", "request_id": "TEST-named-route", "query": "zzz unrelated words",
                   "mode": "current", "max_items": 6, "budget_tokens": 1200}
        result = host.core.recall(context, request, deadline_seconds=10)
        assert [candidate.ref for candidate in result.candidates if candidate.source == "vector"] == [event.ref]
        packet = host.core.recall_packet(context, request, deadline_seconds=10)
        assert event.ref in [item["ref"] for item in packet["items"]]
        assert "vector_old_or_mismatched_space" not in packet["gaps"]
    finally:
        host.close()
