"""Offline reproduction of the real v7 auxiliary-worker wiring failure.

This file intentionally reads the public TEST-v7 fixture and exercises only the
official installed/runtime composition with a capture seam.  It never starts a
gateway, opens a network transport, or writes the v7 database.
"""

from __future__ import annotations

import json
from pathlib import Path

from scope_recall.adapters.runtime_wiring import attach_trusted_host_runtime
from scope_recall.runtime.instance import RuntimeInstanceConfig, build_runtime_instance


V7 = Path(r"F:\SCOPERECALL更新项目\TEST-P11-candidate-v7")
RUNTIME_CONFIG = V7 / "hermes-home" / "scope-recall" / "runtime-config.json"


def _v7_raw_config() -> dict:
    value = json.loads(RUNTIME_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_real_v7_runtime_config_has_embedding_but_no_vector_composition() -> None:
    """The v7 config enables Gemini embedding but omits the vector runtime."""
    raw = _v7_raw_config()
    auxiliary = raw["auxiliary"]

    assert auxiliary["external_embedding"] is True
    assert auxiliary["embedding"]["credential_env"] == "SCOPE_RECALL_GEMINI_EMBEDDING_API_KEY"
    assert raw.get("vector") is None


def test_real_v7_runtime_is_auto_attached_not_basic_fallback() -> None:
    """The provider's implicit same-directory discovery resolves the v7 runtime."""
    config = RuntimeInstanceConfig.from_mapping(_v7_raw_config())
    runtime = attach_trusted_host_runtime(
        config_path=None,
        expected_binding=config.binding,
        session_id=config.session_id,
        allowed_scope_ids=config.allowed_scope_ids,
    )

    try:
        assert runtime.configured is True
        assert runtime._config_path == RUNTIME_CONFIG.resolve()
        assert runtime.capability_gaps == ()
        assert runtime.runtime is not None
        assert runtime.runtime.auxiliary is not None
        assert runtime.runtime.auxiliary.source_embedding is not None
        assert runtime.runtime.auxiliary.query_embedding is not None
    finally:
        runtime.close()


def test_offline_drain_reproduces_embed_none_while_consolidation_is_wired() -> None:
    """Official RuntimeInstance forwards no embed port when v7 has no vector config.

    The fake drain sink is the only seam: it prevents SQLite/model/network work
    while observing the exact arguments that the real worker would receive.
    """
    instance = build_runtime_instance(RuntimeInstanceConfig.from_mapping(_v7_raw_config()))
    observed: dict[str, object] = {}

    def fake_drain_worker(context, **kwargs):
        observed.update(kwargs)
        return {"status": "captured"}

    instance.core.drain_worker = fake_drain_worker
    try:
        result = instance.drain(consolidation=object())
    finally:
        instance.close()

    assert result == {"status": "captured"}
    assert observed["consolidation"] is not None
    assert observed["embed"] is None
