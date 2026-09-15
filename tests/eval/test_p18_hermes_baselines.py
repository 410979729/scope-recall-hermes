from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from p18_hermes_baselines import (
    AUTOMATIC_MEMORY_BUDGET_TOKENS,
    EXPLICIT_INSPECTION_BUDGET_TOKENS,
    HermesBaselineError,
    run_archive_search,
    run_arm,
    run_exact_578b_provider,
    run_native_memory,
)


def test_archive_arm_loads_raw_public_history_and_searches_without_core(tmp_path: Path) -> None:
    receipt = run_archive_search(tmp_path / "arm-D")

    assert receipt["status"] == "PASS"
    assert receipt["records_seen"] == 2
    assert receipt["recall_count"] == 2
    assert receipt["core_schema_created"] is False
    assert receipt["claims_created"] is False
    assert receipt["source_text_preserved"] is True
    archive = Path(receipt["archive"])
    rows = [json.loads(line) for line in archive.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["source_event_key"] == "P18-public-synthetic/001"
    assert rows[0]["text"] == "TEST source event"
    assert receipt["matched_records"][0]["source_ref"] == rows[0]["source_event_key"]
    assert receipt["matched_records"][0]["body"] == rows[0]["text"]
    assert receipt["recall_tokens_used"] <= AUTOMATIC_MEMORY_BUDGET_TOKENS


def test_public_budget_and_arm_boundary_are_explicit(tmp_path: Path) -> None:
    assert AUTOMATIC_MEMORY_BUDGET_TOKENS == 1200
    assert EXPLICIT_INSPECTION_BUDGET_TOKENS == 4000
    with pytest.raises(HermesBaselineError, match="only arms"):
        run_arm("C", tmp_path / "arm-C")


def test_nonempty_baseline_home_fails_closed(tmp_path: Path) -> None:
    home = tmp_path / "existing"
    home.mkdir()
    (home / "foreign.txt").write_text("foreign", encoding="utf-8")
    with pytest.raises(HermesBaselineError, match="new and empty"):
        run_archive_search(home)


def test_native_arm_reads_frozen_memory_context_without_lexical_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = Path(r"F:\SCOPERECALL更新项目\TEST-Hermes-runtime-v1\hermes-source-79445")
    if not (frozen / "tools" / "memory_tool_store.py").is_file():
        pytest.skip("frozen Hermes source is not provisioned")
    monkeypatch.syspath_prepend(str(frozen))
    import p18_hermes_baselines as baselines

    monkeypatch.setattr(baselines, "_simple_search", lambda *_args: (_ for _ in ()).throw(AssertionError("A used D search")))
    receipt = run_native_memory(tmp_path / "arm-A")
    assert receipt["status"] == "PASS"
    assert receipt["query_driver"] == "MemoryStore.format_for_system_prompt(memory)"
    assert receipt["native_context_chars"] > 0


def test_archive_arm_truncates_matched_bodies_to_automatic_budget(tmp_path: Path) -> None:
    fixture = tmp_path / "long-public.jsonl"
    fixture.write_text(
        json.dumps(
            {
                "history": [{"source_type": "human_direct", "speaker_role": "user", "text": "token " * 1400}],
                "query": {"text": "token"},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    receipt = run_archive_search(tmp_path / "arm-D-long", fixture_path=fixture)
    record = receipt["matched_records"][0]
    assert record["truncated"] is True
    assert record["token_count"] == AUTOMATIC_MEMORY_BUDGET_TOKENS
    assert receipt["recall_tokens_used"] == AUTOMATIC_MEMORY_BUDGET_TOKENS


def test_formal_baseline_admission_stops_before_output_when_gate_is_not_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import p18_formal_evidence

    monkeypatch.setattr(
        p18_formal_evidence,
        "verify_formal_run_config",
        lambda _path: SimpleNamespace(formal_execution_allowed=False, reasons=("g2_not_passed",), details={}),
    )
    with pytest.raises(HermesBaselineError, match="formal_config_not_ready"):
        from p18_hermes_baselines import run_core_baseline

        run_core_baseline("D", tmp_path / "core-D.jsonl", tmp_path / "formal.json", tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_exact_578b_provider_uses_test_owned_temp_for_writes(tmp_path: Path) -> None:
    receipt = run_exact_578b_provider(tmp_path / "arm-B")
    if receipt.get("reason") == "exact_578b_source_or_hermes_source_missing":
        pytest.skip("exact archived baseline is not provisioned")
    assert receipt["status"] == "PASS"
    assert receipt["stored_successes"] == receipt["records_seen"] == 2
    assert receipt["flush"] is True
    assert receipt["shutdown"] == "PASS"
    assert receipt["child_temp"].endswith("arm-B\\child-temp")
