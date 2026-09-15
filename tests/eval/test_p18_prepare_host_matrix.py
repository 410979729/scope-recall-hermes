from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import p18_prepare_host_matrix as matrix


def _row(host: str = "hermes_a2a", arm: str = "A", pair: int = 1, condition: int = 1) -> dict[str, object]:
    return {
        "unit_id": f"{host}-{arm}-query-{pair:02d}-c{condition}",
        "kind": "host_query",
        "ordinal": (pair - 1) * 2 + condition,
        "source_sequence": "imported_history_then_new_session_query",
        "source_records": [
            {
                "event_id": f"PUBLIC-{host}-{arm}-{pair}-{condition}",
                "sequence": 1,
                "source_type": "human_direct",
                "speaker_role": "user",
                "text": "public synthetic source",
                "occurred_at": "2026-01-01T00:00:00Z",
            }
        ],
        "model_input": {
            "history": [{"source_type": "human_direct", "speaker_role": "user", "text": "public synthetic source", "occurred_at": "2026-01-01T00:00:00Z"}],
            "query": {"text": "public synthetic query"},
        },
    }


def _rows_for(host: str, arm: str) -> list[dict[str, object]]:
    return [_row(host, arm, pair, condition) for pair in range(1, 41) for condition in (1, 2)]


def _fake_factory(entry: Path, **_kwargs: object) -> dict[str, object]:
    path = entry / "host-binding" / "host-binding.json"
    path.parent.mkdir()
    payload = {"status": "PREPARED", "host_id": "fake", "arm_id": "A", "binding_manifest_path": str(path)}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def test_unit_rows_joins_private_identity_without_exposing_it_to_model_input(tmp_path: Path) -> None:
    plan = tmp_path / "TEST-plan"
    units = plan / "units" / "hermes_a2a"
    units.mkdir(parents=True)
    planner_rows: list[dict[str, object]] = []
    associations: list[dict[str, object]] = []
    for pair in range(1, 41):
        for condition in (1, 2):
            row = _row("hermes_a2a", "A", pair, condition)
            source_records = row.pop("source_records")
            row["source_sequence"] = "source_seed_then_new_session_query"
            planner_rows.append(row)
            associations.append({
                "unit_id": row["unit_id"],
                "kind": "host_query",
                "host_id": "hermes_a2a",
                "arm_id": "A",
                "event_ids": [item["event_id"] for item in source_records],
                "event_sequences": [item["sequence"] for item in source_records],
            })
    (units / "A.jsonl").write_text(
        "\n".join(json.dumps(row) for row in planner_rows) + "\n", encoding="utf-8"
    )
    (plan / "private-association.json").write_text(json.dumps({
        "visibility": "independent_executor_private",
        "associations": associations,
    }), encoding="utf-8")

    observed = matrix._unit_rows(plan, "hermes_a2a", "A")

    assert len(observed) == 80
    assert observed[0]["source_records"][0]["event_id"] == "PUBLIC-hermes_a2a-A-1-1"
    assert observed[0]["source_records"][0]["sequence"] == 1
    assert set(observed[0]["model_input"]["history"][0]) == {
        "source_type", "speaker_role", "text", "occurred_at"
    }


def test_matrix_plan_keeps_d_archive_and_one_mapping_per_condition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(matrix, "_unit_rows", lambda _root, host, arm: _rows_for(host, arm))
    hermes_exe = tmp_path / "hermes.exe"
    hermes_exe.write_bytes(b"hermes-test")
    source = tmp_path / "TEST-hermes-source"
    source.mkdir()
    result = matrix.prepare_host_query_matrix(
        plan_root=tmp_path / "TEST-plan",
        output_root=tmp_path / "TEST-matrix",
        hermes_executable=hermes_exe,
        hermes_version="0.21.0",
        hermes_source_root=source,
    )
    assert result["status"] == "PLAN_ONLY"
    assert result["binding_count"] == 640
    assert len(result["group_maps"]) == 8
    assert all(item["unit_count"] == 80 for item in result["group_maps"].values())
    d_plan = next(Path(result["output_root"]).glob("bindings/h/D/*/arm-plan.json"))
    plan = json.loads(d_plan.read_text(encoding="utf-8"))
    archive = Path(plan["storage"]["archive_path"])
    assert json.loads(archive.read_text(encoding="utf-8").splitlines()[0])["history"] == _row("hermes_a2a", "D")["source_records"]
    assert plan["storage"]["archive_sha256"] == matrix._sha256(archive)
    mapping = Path(result["output_root"]) / result["group_maps"]["hermes_cli_local_input_v1/D"]["path"]
    payload = json.loads(mapping.read_text(encoding="utf-8"))
    assert len(payload) == 80
    assert all(set(ref) == {"path", "sha256"} for ref in payload.values())
    assert (Path(result["output_root"]) / Path(payload[next(iter(payload))]["path"])).is_file()


def test_matrix_prepare_delegates_to_existing_factory_and_rejects_reuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(matrix, "_unit_rows", lambda _root, host, arm: _rows_for(host, arm))
    hermes_exe = tmp_path / "hermes.exe"
    hermes_exe.write_bytes(b"hermes-test")
    source = tmp_path / "TEST-hermes-source"
    source.mkdir()
    result = matrix.prepare_host_query_matrix(
        plan_root=tmp_path / "TEST-plan",
        output_root=tmp_path / "TEST-matrix",
        hermes_executable=hermes_exe,
        hermes_version="0.21.0",
        hermes_source_root=source,
        codex_executable=hermes_exe,
        codex_version="test-codex",
        prepare=True,
        factory=_fake_factory,
    )
    assert result["status"] == "PREPARED"
    assert result["binding_count"] == 640
    assert all(item["status"] == "PREPARED" for item in result["bindings"].values())
    with pytest.raises(matrix.HostMatrixError, match="new and empty"):
        matrix.prepare_host_query_matrix(
            plan_root=tmp_path / "TEST-plan",
            output_root=Path(result["output_root"]),
            hermes_executable=hermes_exe,
            hermes_version="0.21.0",
            hermes_source_root=source,
        )


def test_group_map_is_accepted_by_actual_runner_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import p18_run_host_queries as runner

    monkeypatch.setattr(matrix, "_unit_rows", lambda _root, host, arm: _rows_for(host, arm))
    hermes_exe = tmp_path / "hermes.exe"
    hermes_exe.write_bytes(b"hermes-test")
    codex_exe = tmp_path / "codex.exe"
    codex_exe.write_bytes(b"codex-test")
    source = tmp_path / "TEST-hermes-source"
    source.mkdir()
    formal_root = tmp_path / "TEST-formal"

    def codex_factory(entry: Path, **_kwargs: object) -> dict[str, object]:
        binding_root = entry / "binding-root"
        home = binding_root / "home"
        workspace = binding_root / "workspace"
        database = binding_root / "memory.sqlite3"
        home.mkdir(parents=True)
        workspace.mkdir()
        payload = {
            "host_id": "codex_windows_desktop",
            "arm_id": "A",
            "status": "PREPARED",
            "fixed_host": {"executable_path": str(codex_exe), "version": "PUBLIC"},
            "roots": {"binding_root": str(binding_root), "home_path": str(home), "database_path": str(database)},
            "launch_contract": {"working_directory": str(workspace)},
        }
        path = entry / "host-binding" / "binding.json"
        path.parent.mkdir()
        path.write_text(json.dumps(payload), encoding="utf-8")
        return {"status": "PREPARED", "binding_manifest_path": str(path)}

    result = matrix.prepare_host_query_matrix(
        plan_root=tmp_path / "TEST-plan",
        output_root=formal_root,
        hermes_executable=hermes_exe,
        hermes_version="0.21.0",
        hermes_source_root=source,
        codex_executable=codex_exe,
        codex_version="PUBLIC",
        prepare=True,
        factory=codex_factory,
    )
    from p18_score_report import IDENTITY_MAP_SHA256, authoritative_query_pair_indices
    from test_p18_run_host_queries import _sealed_identity_rows

    rows = _sealed_identity_rows(pair_indices=authoritative_query_pair_indices(), host="codex_windows_desktop", arm="A")
    units_path = formal_root / "units.jsonl"
    units_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    method_path = formal_root / "method.json"
    method_path.write_text(json.dumps({"method": {"executable_sha256": matrix._sha256(codex_exe), "executable_version": "PUBLIC"}}), encoding="utf-8")
    group = result["group_maps"]["codex_windows_desktop/A"]
    config_path = formal_root / "formal.json"
    config_path.write_text(json.dumps({
        "host_query_inputs": {"path": "units.jsonl", "sha256": matrix._sha256(units_path)},
        "host_query_bindings": {"path": group["path"], "sha256": group["sha256"]},
        "identity_map_sha256": IDENTITY_MAP_SHA256,
    }), encoding="utf-8")
    ready = SimpleNamespace(
        formal_execution_allowed=True,
        details={"operations": runner.host_query_operation_map(rows), "method_path": str(method_path)},
    )
    monkeypatch.setattr(runner, "verify_formal_run_config", lambda _: ready)
    monkeypatch.setattr(runner, "_execute_codex_condition", lambda *_: pytest.fail("run=False dispatched host"))
    observed = runner.run_host_queries(formal_config_path=config_path, output_root=formal_root / "TEST-run")
    assert observed["status"] == "PREFLIGHT_ONLY"
    assert observed["query_conditions"] == 80
    assert observed["independent_query_pairs"] == 40
    assert not (formal_root / "TEST-run").exists()

    wrong = result["group_maps"]["codex_windows_desktop/B"]
    config_path.write_text(json.dumps({
        "host_query_inputs": {"path": "units.jsonl", "sha256": matrix._sha256(units_path)},
        "host_query_bindings": {"path": wrong["path"], "sha256": wrong["sha256"]},
        "identity_map_sha256": IDENTITY_MAP_SHA256,
    }), encoding="utf-8")
    with pytest.raises(runner.HostQueryError, match="one_actual_binding"):
        runner.run_host_queries(formal_config_path=config_path, output_root=formal_root / "TEST-run-2")


def test_c_native_path_guard_uses_existing_vector_store_check() -> None:
    if os.name != "nt":
        pytest.skip("Win32 native path guard is platform-specific")
    from scope_recall.lance_process_store import ProcessLanceVectorStore

    long_root = Path(r"F:\SCOPERECALL更新项目\worktrees\scope-recall-runtime-integration\.execution\TEST-LUNA-MATRIX-FIX-v1\conditions\codex_windows_desktop\C\codex_windows_desktop-C-query-40-c2\binding-root\hermes-home\scope-recall\vectors\scope-recall.embedding.v1")
    short_root = Path(r"F:\T\P18\C\vectors\scope-recall.embedding.v1")
    long_error = ProcessLanceVectorStore(long_root, table_name="TEST_P18_C", dimensions=3072).native_path_error()
    short_error = ProcessLanceVectorStore(short_root, table_name="TEST_P18_C", dimensions=3072).native_path_error()
    assert long_error is not None and "native_vector_path_too_long" in long_error
    assert short_error is None
