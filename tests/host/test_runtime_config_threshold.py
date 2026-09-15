from __future__ import annotations

from scope_recall.contracts import InstanceBinding
from scope_recall.runtime.instance import RuntimeInstanceConfig, build_runtime_instance


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
