"""Public offline freeze -> actual query-preflight integration regression.

The formal verifier, matrix producer, query runner, CLI constructor, candidate
byte checks, empty Core databases and raw archive checks are real. Only the
installed Hermes executable/source attestation is a fixture; no process or
network dispatch is permitted. These synthetic inputs are not P18 evidence.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

import p18_formal_evidence as evidence
import p18_hermes_cli_transport as cli
import p18_prepare_host_matrix as matrix
import p18_run_host_queries as runner
from p18_formal_freeze import build_frozen_config
from p18_score_report import authoritative_query_pair_indices
from test_p18_formal_freeze import _freeze_inputs
from test_p18_run_host_queries import _sealed_identity_rows


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _public_matrix(tmp_path, monkeypatch):
    import scope_recall
    from scope_recall.core.recall_policy import SPACE_ID
    from scope_recall.runtime.instance import RuntimeInstanceConfig, build_runtime_instance

    inputs = _freeze_inputs(tmp_path, monkeypatch)
    # Native Windows path-length behavior is covered by the dedicated matrix
    # guard test.  This integration fixture deliberately uses pytest's long
    # randomized temp root and only exercises freeze/group wiring.
    monkeypatch.setattr(matrix, "_check_c_native_vector_path", lambda _manifest, _arm_id: "checked")
    repo = Path(__file__).resolve().parents[2]
    # The shared pytest namespace alias has no __file__; point the real
    # candidate byte comparison at this checkout, not a synthetic package.
    monkeypatch.setattr(scope_recall, "__file__", str(repo / "__init__.py"), raising=False)
    package_bytes = (repo / "__init__.py").read_bytes()
    (inputs["candidate_source_root"] / "__init__.py").write_bytes(package_bytes)
    with zipfile.ZipFile(inputs["candidate_wheel"], "w") as wheel:
        wheel.writestr("scope_recall/__init__.py", package_bytes)
    _json(inputs["candidate_receipt"], {
        "source_commit": inputs["source_commit"], "wheel_sha256": _sha(inputs["candidate_wheel"]),
        "package_files_checked": 1, "mismatches": [],
        "package_files": [{"path": "__init__.py", "sha256": hashlib.sha256(package_bytes).hexdigest()}],
    })
    executable = tmp_path / "PUBLIC-host.exe"
    executable.write_bytes(b"PUBLIC TEST executable identity; never executable")
    method = json.loads(inputs["method_artifact"].read_text(encoding="utf-8"))
    method["method"].update(executable_sha256=_sha(executable), executable_version="PUBLIC")
    _json(inputs["method_artifact"], method)
    monkeypatch.setattr(evidence, "METHOD_SHA256", _sha(inputs["method_artifact"]))
    executor_plan = tmp_path / "public-executor-plan"
    pair_indices = authoritative_query_pair_indices()
    for host in ("hermes_a2a", "codex_windows_desktop"):
        for arm in "ABCD":
            rows = _sealed_identity_rows(pair_indices=pair_indices, host=host, arm=arm)
            _jsonl(executor_plan / "units" / host / f"{arm}.jsonl", rows)
            planner = [{key: value for key, value in row.items() if key != "source_records"} for row in rows]
            for row in planner:
                row["source_sequence"] = "source_seed_then_new_session_query"
            _jsonl(inputs["plan_root"] / "units" / host / f"{arm}.jsonl", planner)
    source = tmp_path / "PUBLIC-hermes-source"
    source.mkdir()

    def factory(entry, **_kwargs):
        plan = json.loads((entry / "arm-plan.json").read_text(encoding="utf-8"))
        host, arm = plan["host_id"], plan["arm_id"]
        root = inputs["output_dir"] / "TEST-b" / ("h" if host == evidence.HERMES_METHOD_ID else "c") / arm / entry.name.rsplit("-query-", 1)[-1]
        home, workspace, data = root / "home", root / "workspace", root / "data"
        for path in (home, workspace, data):
            path.mkdir(parents=True)
        binding = {
            "host_id": host, "arm_id": arm, "status": "PREPARED",
            "fixed_host": {"executable_path": str(executable), "version": "PUBLIC",
                           "runtime_python_path": sys.executable},
            "roots": {"binding_root": str(root), "home_path": str(home),
                      "database_path": str(data / "memory.sqlite3")},
            "launch_contract": {"working_directory": str(workspace), "post_turn_settle_seconds": 60},
            "loader": {},
        }
        if arm == "B" and host == "codex_windows_desktop":
            binding["status"] = "UNSUPPORTED"
        if arm == "C":
            if host == evidence.HERMES_METHOD_ID:
                from scope_recall.adapters.hermes.installation import (
                    build_installation_manifest,
                    manifest_payload,
                    write_installation_manifest,
                )
                # Runner bind is fixed: platform=cli, agent_identity=default,
                # agent_workspace=hermes. Trusted constructor writes home/scope-recall.
                installed_manifest = build_installation_manifest(
                    home, agent_id="default", platform="cli", user_id="local",
                    agent_workspace="hermes", test_mode=True,
                )
                written = write_installation_manifest(installed_manifest)
                installed = installed_manifest.to_binding()
                binding["roots"]["database_path"] = str(installed.data_directory / "memory.sqlite3")
                binding["installation_manifest"] = {
                    "manifest_path": str(written), "manifest_sha256": _sha(written),
                    "payload": manifest_payload(installed_manifest),
                }
                runtime = {"binding": {"agent_id": installed.agent_id,
                    "installation_id": installed.installation_id,
                    "data_directory": str(installed.data_directory),
                    "scope_ids": sorted(installed.scope_ids), "test_mode": installed.test_mode},
                    "session_id": "TEST", "allowed_scope_ids": sorted(installed.scope_ids),
                    "vector": {"storage_dir": str(installed.data_directory / "vectors" / SPACE_ID),
                               "table_name": "TEST_P18", "dimensions": 3072}}
            else:
                from scope_recall.adapters.codex.config import install_codex_scope_recall
                installation, _core = install_codex_scope_recall(root, project_root=workspace, agent_id="TEST")
                binding["roots"]["installation_config_path"] = str(installation.config_path)
                runtime = {"binding": {"agent_id": installation.agent_id, "installation_id": installation.installation_id,
                    "data_directory": str(installation.data_directory), "scope_ids": sorted(installation.scope_ids),
                    "test_mode": True},
                    "session_id": "TEST", "allowed_scope_ids": sorted(installation.scope_ids),
                    "vector": {"storage_dir": str(installation.data_directory / "vectors" / SPACE_ID),
                               "table_name": "TEST_P18", "dimensions": 3072}}
            runtime_path = root / "runtime.json"
            _json(runtime_path, runtime)
            binding["roots"]["runtime_config_path"] = str(runtime_path)
            instance = build_runtime_instance(RuntimeInstanceConfig.from_mapping(runtime))
            try:
                instance.core.initialize()  # Real Core schema, no inserted sources/claims.
            finally:
                instance.close()
        if host == evidence.HERMES_METHOD_ID:
            config_path = root / "config.json"
            _json(config_path, {
                "model": {"default": "deepseek-v4-flash", "provider": "PUBLIC",
                          "max_tokens": 4096, "context_length": 131072, "streaming": False},
                "custom_providers": [{"name": "PUBLIC", "base_url": "http://127.0.0.1:29991/v1",
                                     "key_env": "SCOPE_RECALL_TEST_LOCAL_BRIDGE_TOKEN"}],
                "fallback_model": [], "agent": {"api_max_retries": 0, "max_turns": 3},
                "platform_toolsets": {"cli": ["terminal", "file", "memory"]},
                "compression": {"enabled": False}, "terminal": {"cwd": str(workspace)},
            })
            binding["roots"]["config_path"] = str(config_path)
            binding["launch_contract"]["config_artifact"] = {"path": str(config_path), "sha256": _sha(config_path)}
            if arm == "C":
                site = root / "site"
                installed = site / "scope_recall" / "__init__.py"
                installed.parent.mkdir(parents=True)
                installed.write_bytes(package_bytes)
                binding["loader"]["module_search_path"] = str(site)
                binding["source"] = {"candidate_wheel": {"path": str(inputs["candidate_wheel"]),
                                                          "sha256": _sha(inputs["candidate_wheel"])}}
        if arm == "D":
            binding["loader"]["archive"] = {"path": plan["storage"]["archive_path"],
                                               "sha256": plan["storage"]["archive_sha256"]}
        path = entry / "binding.json"
        binding["binding_manifest_path"] = str(path)
        _json(path, binding)
        return binding

    def forbidden(*_args, **_kwargs):
        pytest.fail("offline freeze preflight attempted host dispatch")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(runner, "_execute_cli_condition", forbidden)
    monkeypatch.setattr(runner, "_execute_codex_condition", forbidden)
    monkeypatch.setattr(cli, "verify_binding", lambda _binding: None)
    inputs["output_dir"] = tmp_path / "TEST-formal"
    result = matrix.prepare_host_query_matrix(
        plan_root=executor_plan, output_root=inputs["output_dir"],
        hermes_executable=executable, hermes_version="PUBLIC", hermes_source_root=source,
        codex_executable=executable, codex_version="PUBLIC", candidate_wheel=inputs["candidate_wheel"],
        candidate_sha256=_sha(inputs["candidate_wheel"]), prepare=True, factory=factory,
    )
    inputs["host_matrix_root"] = inputs["output_dir"]
    return inputs, result


def test_freeze_output_reaches_all_eight_actual_group_preflights(tmp_path, monkeypatch):
    inputs, matrix_result = _public_matrix(tmp_path, monkeypatch)
    root = inputs["output_dir"]
    # Snapshot original public binding/input files: the freeze must preserve
    # bytes and absolute roots instead of copying/relocating the matrix.
    retained = {path: _sha(path) for path in root.rglob("*") if path.is_file()}
    ledger_stat = inputs["ledger"].stat()
    receipt = build_frozen_config(**inputs)
    assert receipt["status"] == "READY", receipt["readiness"]["reasons"]
    assert receipt["operation_count"] == 648  # 640 query fixtures + 8 public journey turns
    config = json.loads((root / "formal-run-config.json").read_text(encoding="utf-8"))
    assert set(config["host_query_groups"]) == set(matrix_result["group_maps"])
    for index, group in enumerate(config["host_query_groups"].values()):
        group_path = root / group["config_path"]
        assert group_path.parent == root
        outcome = runner.run_host_queries(formal_config_path=group_path, output_root=root / f"TEST-direct-{index}")
        assert outcome["status"] == "PREFLIGHT_ONLY" and outcome["query_conditions"] == 80
        assert not outcome["semantic_pass"]
        assert not (root / f"TEST-direct-{index}").exists()
        frozen = json.loads(group_path.read_text(encoding="utf-8"))
        assert len(frozen["operations"]) == 80
        assert all(op["host_id"] == group["method_id"] for op in frozen["operations"].values())
        assert frozen["ledger_path"] == str(inputs["ledger"].resolve())
    assert all(path.is_file() and _sha(path) == sha for path, sha in retained.items())
    after = inputs["ledger"].stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (ledger_stat.st_ino, ledger_stat.st_size, ledger_stat.st_mtime_ns)

    # Exercise the real consumer after a wrong group reference, rather than
    # assuming the producer's own success receipt proves consumability.
    a = root / config["host_query_groups"]["codex_windows_desktop/A"]["config_path"]
    raw = json.loads(a.read_text(encoding="utf-8"))
    raw["host_query_bindings"] = config["host_query_groups"]["codex_windows_desktop/B"]["host_query_bindings"]
    _json(a, raw)
    with pytest.raises(runner.HostQueryError, match="one_actual_binding"):
        runner.run_host_queries(formal_config_path=a, output_root=root / "TEST-wrong-group")


def test_freeze_cannot_report_ready_for_changed_actual_binding(tmp_path, monkeypatch):
    inputs, result = _public_matrix(tmp_path, monkeypatch)
    root = inputs["output_dir"]
    group = result["group_maps"]["hermes_cli_local_input_v1/A"]
    mapping_path = root / group["path"]
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    reference = next(iter(mapping.values()))
    path = root / reference["path"]
    binding = json.loads(path.read_text(encoding="utf-8"))
    binding["host_id"] = "hermes_a2a"  # Legacy unit IDs do not authorize an A2A transport.
    _json(path, binding)
    reference["sha256"] = _sha(path)
    _json(mapping_path, mapping)
    group["sha256"] = _sha(mapping_path)
    _json(root / "matrix-summary.json", result)
    receipt = build_frozen_config(**inputs)
    assert receipt["status"] == "NOT_READY"
    assert any("condition_host_arm_binding_mismatch" in reason for reason in receipt["readiness"]["reasons"])
