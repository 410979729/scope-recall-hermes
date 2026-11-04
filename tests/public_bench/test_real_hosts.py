"""Contracts preventing synthetic evidence, ambient homes and incomplete metrics from passing M6."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from real_host_io import MARKER, Children, clean_root, environment, new_root
from real_host_runtime import route_template
from real_host_scale import scale_metrics
from real_host_session import Session, answer_text, injected, recalled
from real_hosts import IDS, arguments, record
from scenario_report import brief, merge_real

TEST_CHAT_ENDPOINT = "http://127.0.0.1:9/v1/chat/completions"


def specs():
    """Read the single authoritative fixture used by both scenario runners."""
    return json.loads(Path(__file__).with_name("scenarios_fixture.json").read_text(encoding="utf-8"))


def test_ids_are_exactly_the_remaining_real_machine_subchecks():
    required = {row["id"] for row in specs()["checks"] if "M6" in row["method"] or row["id"] == "S9.frozen"}
    assert set(IDS) == required


@pytest.mark.parametrize("key", IDS)
def test_deterministic_verdict_never_passes_a_real_machine_check(tmp_path, key):
    run = SimpleNamespace(specs=specs(), args=SimpleNamespace(dry_run=True), root=tmp_path, rows=[])
    record(run, key, True, dict(flow_pass=True, scales=[dict(scale=100)]), 1)
    assert run.rows[0]["status"] == "m6"
    assert run.rows[0]["actual"]["flow_pass"] is True


def test_environment_passes_only_declared_credentials_and_redirects_homes(tmp_path, monkeypatch):
    monkeypatch.setenv("UNRELATED_PRIVATE_CONFIG", "TEST-secret-omitted")
    monkeypatch.setenv("TEST_ROUTE_CREDENTIAL", "TEST-value")
    env = environment(tmp_path, credential_names=["TEST_ROUTE_CREDENTIAL"])
    assert env["TEST_ROUTE_CREDENTIAL"] == "TEST-value"
    assert "UNRELATED_PRIVATE_CONFIG" not in env
    for name in ("HOME", "USERPROFILE", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "TEMP", "UV_CACHE_DIR"):
        assert Path(env[name]).is_relative_to(tmp_path)


def test_host_account_environment_lends_only_the_operator_sign_in_and_proxy(tmp_path, monkeypatch):
    """Host CLIs may use the operator's own login; caches, temp files and unrelated settings stay isolated."""
    from real_host_io import host_account_environment

    profile = tmp_path / "TEST-operator-profile"
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("UNRELATED_PRIVATE_CONFIG", "TEST-secret-omitted")
    isolated = environment(tmp_path / "isolated")
    env = host_account_environment(isolated)
    assert env["USERPROFILE"] == str(profile)
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:9"
    assert "CODEX_HOME" not in env and "CLAUDE_CONFIG_DIR" not in env
    assert "UNRELATED_PRIVATE_CONFIG" not in env
    for name in ("TEMP", "TMP", "UV_CACHE_DIR", "PIP_CACHE_DIR", "XDG_CONFIG_HOME"):
        assert env[name] == isolated[name]
    assert isolated["USERPROFILE"] != str(profile)


def test_codex_overrides_are_valid_toml_and_contain_only_ephemeral_wiring(tmp_path):
    args = SimpleNamespace(dry_run=False, codex="TEST-codex", codex_model=None)
    run = SimpleNamespace(args=args, credential_names=["TEST_SR_GEMINI_KEY"])
    session = Session(run, tmp_path, {host: tmp_path / (host + "-home") for host in ("codex", "claude-code")})
    command = session.command("codex", "TEST held-out question")
    assert "--dangerously-bypass-hook-trust" in command
    assert "--ignore-user-config" in command
    configs = [tomllib.loads(command[index + 1]) for index, part in enumerate(command) if part == "-c"]
    hooks = next(row["hooks"] for row in configs if "hooks" in row)
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"}
    mcp = next(row["mcp_servers"] for row in configs if "mcp_servers" in row)
    assert mcp["scope-recall"]["env_vars"] == ["TEST_SR_GEMINI_KEY"]
    assert all("--env-file" not in json.dumps(row) for row in configs)
    assert all(str(tmp_path) in row["hooks"][0]["command"] for groups in hooks.values() for row in groups)


@pytest.mark.parametrize("encoding", ["ascii", "cp936"])
def test_hook_tee_forwards_unicode_bytes_without_using_the_host_locale(tmp_path, monkeypatch, encoding):
    import io
    import subprocess

    import real_host_probe

    body = json.dumps(
        dict(hook_event_name="UserPromptSubmit", session_id="TEST-session", prompt="TEST-azalea 配色是琥珀色。"),
        ensure_ascii=False,
    ).encode("utf-8")
    stdin = io.TextIOWrapper(io.BytesIO(body), encoding=encoding, errors="surrogateescape")
    stdout = io.TextIOWrapper(io.BytesIO(), encoding=encoding)
    stderr = io.TextIOWrapper(io.BytesIO(), encoding=encoding)
    expected = '{"hookSpecificOutput":{"additionalContext":"TEST-琥珀色"}}'.encode()
    diagnostic = "TEST-诊断\r\n".encode()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    def product(command, **kwargs):
        assert "scope_recall.adapters.clients.hook_entry" in command
        assert kwargs == dict(input=body, capture_output=True, timeout=20)
        ingress = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
        assert ingress["phase"] == "received" and ingress["exit_code"] is None
        return subprocess.CompletedProcess(command, 0, expected, diagnostic)

    monkeypatch.setattr(real_host_probe.subprocess, "run", product)
    assert (
        real_host_probe.hook(SimpleNamespace(evidence=tmp_path, python=sys.executable, home=tmp_path, host="codex"))
        == 0
    )
    assert stdout.buffer.getvalue() == expected
    assert stderr.buffer.getvalue() == diagnostic
    receipt = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert receipt["phase"] == "completed" and receipt["exit_code"] == 0
    assert receipt["output"] == expected.decode("utf-8")
    assert receipt["stderr"] == diagnostic.decode("utf-8")


def test_hook_tee_retains_timeout_as_a_failure(tmp_path, monkeypatch, capsys):
    import io
    import subprocess

    import real_host_probe

    body = b'{"hook_event_name":"UserPromptSubmit","session_id":"TEST-timeout"}'
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(body), encoding="ascii"))

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 20)

    monkeypatch.setattr(real_host_probe.subprocess, "run", timeout)
    assert (
        real_host_probe.hook(SimpleNamespace(evidence=tmp_path, python=sys.executable, home=tmp_path, host="codex"))
        == 1
    )
    assert capsys.readouterr().out == "{}"
    receipt = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert receipt["phase"] == "failed" and receipt["exit_code"] == 1
    assert receipt["stderr"] == "TimeoutExpired"


def test_answer_parser_excludes_prompts_tool_echoes_and_hook_diagnostics():
    output = "\n".join(
        json.dumps(row)
        for row in [
            dict(type="item.completed", item=dict(type="tool_result", text="TEST expected")),
            dict(type="item.completed", item=dict(type="agent_message", text="TEST actual")),
        ]
    )
    assert answer_text("codex", output) == ("TEST actual", None)
    reply = dict(exit_code=0, answer="TEST expected", hooks=[])
    assert not recalled(reply, "TEST expected")
    reply["hooks"] = [
        dict(
            event="UserPromptSubmit",
            exit_code=0,
            output=json.dumps(dict(hookSpecificOutput=dict(additionalContext="TEST expected"))),
        )
    ]
    assert recalled(reply, "TEST expected")
    reply["hooks"][0]["exit_code"] = 1
    assert not injected(reply, "TEST expected")


def test_cleanup_requires_new_direct_child_marker_and_matching_token(tmp_path):
    root = new_root(tmp_path)
    token = json.loads((root / MARKER).read_text())["token"]
    (root / "TEST-owned-file.txt").write_text("TEST")
    with pytest.raises(ValueError, match="ownership mismatch"):
        clean_root(root, tmp_path, "TEST-wrong-owner")
    with pytest.raises(ValueError, match="direct owned"):
        clean_root(tmp_path, tmp_path, token)
    clean_root(root, tmp_path, token)
    assert not root.exists()


def test_cleanup_refuses_symlink_without_touching_target(tmp_path):
    root = new_root(tmp_path)
    target = tmp_path / "TEST-kept.txt"
    target.write_text("TEST-kept")
    token = json.loads((root / MARKER).read_text())["token"]
    try:
        (root / "TEST-link").symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError, match="links"):
        clean_root(root, tmp_path, token)
    assert target.read_text() == "TEST-kept"


def test_merge_rejects_foreign_fixture_and_never_promotes_dry_run(tmp_path):
    spec = next(row for row in specs()["checks"] if row["id"] == "S1.host")
    current = dict(
        version="TEST-version",
        fixture_sha256="TEST-hash",
        checks=[dict(**spec, status="m6", classification="real_machine", actual=dict(missing="host"))],
    )
    evidence = dict(
        version="TEST-version",
        fixture_sha256="TEST-hash",
        mode="dry-run",
        root=str(tmp_path),
        checks=[dict(**spec, status="m6", classification="real_machine", actual=dict(flow_pass=True))],
    )
    merged = merge_real(current, [evidence])
    assert merged["checks"][0]["status"] == "m6"
    evidence["mode"] = "real"
    evidence["checks"][0]["status"] = "pass"
    evidence["checks"][0]["classification"] = None
    assert merge_real(current, [evidence])["checks"][0]["status"] == "pass"
    accepted = merge_real(current, [evidence])
    evidence["mode"] = "dry-run"
    evidence["checks"][0]["status"] = "m6"
    preserved = merge_real(accepted, [evidence])["checks"][0]
    assert preserved["status"] == "pass"
    assert preserved["actual"]["real_host_result"]["mode"] == "real"
    evidence["fixture_sha256"] = "TEST-other"
    with pytest.raises(ValueError, match="identical fixture"):
        merge_real(current, [evidence])
    assert current["checks"][0]["status"] == "m6"


def test_frozen_metrics_fail_on_gaps_unseen_messages_and_any_outstanding_channel():
    window = dict(samples=[dict(visible=True, gaps=[])])
    for name in ("capture", "auto", "retrieval", "start"):
        window[name] = dict(p95_ms=1, samples_ms=[1])
    assert all(scale_metrics(window, window, window, dict(depth=1), dict(depth=0), dict(depth=0)).values())
    window["samples"][0].update(visible=False, gaps=["vector_unavailable"])
    metrics = scale_metrics(window, window, window, dict(depth=1), dict(depth=2), dict(depth=1))
    assert not metrics["channels"] and not metrics["fresh"] and not metrics["backlog"]


def test_route_recipe_validates_and_names_environment_variables_only():
    from scope_recall.maintenance.setup_configuration import validate_models

    recipe = validate_models(route_template(TEST_CHAT_ENDPOINT, "TEST-chat-model"))
    assert recipe["auxiliary"]["consolidation"]["allow_insecure_endpoint"] is True
    assert recipe["auxiliary"]["embedding"]["model"] == "gemini-embedding-2"
    assert recipe["auxiliary"]["consolidation"]["credential_env"] == "TEST_SR_CHAT_KEY"
    assert recipe["vector"]["backend"] == "sqlite"
    assert recipe["vector_threshold"] == 0.70


def test_template_budget_prices_paid_embedding_and_unbilled_local_chat():
    from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig

    policy = AuxiliaryRuntimeConfig.from_mapping(
        route_template(TEST_CHAT_ENDPOINT, "TEST-chat-model")["auxiliary"]
    ).budget
    assert policy.cap_micro_usd == 250_000
    assert policy.batch == "TEST-local-chat-unbilled"
    assert policy.pricing["TEST-chat-model"].charge_micro_usd(1_000_000, 1_000_000) == 0
    assert policy.pricing["gemini-embedding-2"].charge_micro_usd(1_000_000, 0) == 200_000


def test_template_explicit_embedding_space_has_a_usable_floor(tmp_path):
    from real_host_runtime import library
    from scope_recall.maintenance.doctor import _check_vector_threshold
    from scope_recall.maintenance.doctor_report import DoctorReport
    from scope_recall.runtime.worker_entry import load_config

    run = SimpleNamespace(
        root=tmp_path,
        stores=[],
        args=SimpleNamespace(dry_run=False),
        routes=route_template(TEST_CHAT_ENDPOINT, "TEST-chat-model"),
    )
    scenes = library(run, "TEST-threshold", models=True)
    try:
        config = load_config(scenes.store / "runtime-config.json")
        report = DoctorReport(host="codex", status="degraded")
        _check_vector_threshold(report, config.binding, scenes.store)
        assert "vector_threshold_unconfigured" not in report.capability_gaps
        assert config.recall_policy().vector_threshold == 0.70
        assert report.checks[-1] == dict(name="vector_threshold", result="configured", detail="0.7")
    finally:
        scenes.close()


@pytest.mark.parametrize("schema", [1108, 1109, 1110, 1111, 1112])
def test_historical_library_copies_each_schema_family_without_changing_source(tmp_path, schema):
    import hashlib

    from real_host_checks import historical_library
    from sample_store import create

    from tests.v11_support import downgrade_store

    sample = tmp_path / "TEST-original"
    create(sample)
    snapshot = sample / "pre-upgrade.sqlite3"
    downgrade_store(snapshot, schema)
    before = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    with sqlite3.connect(snapshot) as conn:
        expected = conn.execute(
            "SELECT event_id,source_revision,content FROM source_events ORDER BY event_id"
        ).fetchall()
    _, store = historical_library(SimpleNamespace(root=tmp_path), sample, "TEST-relocated")
    with sqlite3.connect(store / "memory.sqlite3") as conn:
        assert conn.execute("SELECT installation_kind FROM instance_meta").fetchone()[0] == "shared"
        assert (
            conn.execute("SELECT event_id,source_revision,content FROM source_events ORDER BY event_id").fetchall()
            == expected
        )
        assert conn.execute("SELECT count(*) FROM entries").fetchone()[0] == 2
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == before


def test_release_matrix_excludes_extra_backend_samples(tmp_path):
    from real_host_probe import release_samples

    for name in ("3.1.0", "3.16.1-pypi", "historical-lancedb", "historical-sqlite"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "sample.json").write_text("{}")
    assert [path.parent.name for path in release_samples(tmp_path)] == ["3.1.0", "3.16.1-pypi"]


def test_merge_accepts_only_lf_crlf_equivalence_of_the_complete_fixture(tmp_path):
    import hashlib

    fixture = Path(__file__).with_name("scenarios_fixture.json").read_bytes().replace(b"\r\n", b"\n")
    spec = next(row for row in specs()["checks"] if row["id"] == "S1.host")
    current = dict(
        version="TEST",
        fixture_sha256=hashlib.sha256(fixture).hexdigest(),
        checks=[dict(**spec, status="m6", actual=dict(missing="host"))],
    )
    evidence = dict(
        version="TEST",
        fixture_sha256=hashlib.sha256(fixture.replace(b"\n", b"\r\n")).hexdigest(),
        mode="dry-run",
        root=str(tmp_path),
        checks=[dict(**spec, status="m6", actual={})],
    )
    assert merge_real(current, [evidence])["checks"][0]["status"] == "m6"


def test_existing_list_evidence_still_renders_after_real_result_merging():
    row = dict(id="S6.session", status="pass", actual=[dict(host="TEST", delivered_tokens=0)])
    assert "TEST" in brief(row)


def test_cli_rejects_unknown_subcheck_and_missing_release_inputs(tmp_path):
    base = ["--scratch", str(tmp_path), "--output", str(tmp_path / "result.json")]
    with pytest.raises(SystemExit):
        arguments([*base, "--only", "S12.unknown", "--dry-run"])
    with pytest.raises(SystemExit):
        arguments([*base, "--only", "S2.restart"])


def test_redaction_removes_credential_values_from_child_evidence(tmp_path):
    children = Children(dict(TEST_SR_GEMINI_KEY="TEST-private-value"), tmp_path)
    assert children.redact("TEST-private-value") == "[REDACTED]"


def test_small_scale_cannot_replace_the_two_frozen_scales(tmp_path):
    run = SimpleNamespace(specs=specs(), args=SimpleNamespace(dry_run=False), root=tmp_path, rows=[])
    record(run, "S9.frozen", True, dict(complete_frozen=False, scales=[dict(scale=100)]), 1)
    assert run.rows[0]["status"] == "m6"


def test_promotion_requires_correct_fact_value_rather_than_quoted_source():
    from real_host_quality import promoted_facts

    payload = dict(kind="fact", subject="TEST-promotion0", value_text="银色", evidence_spans=[dict(quote="琥珀色")])
    scenes = SimpleNamespace(rows=lambda *args: [dict(payload_json=json.dumps(payload))])
    assert not promoted_facts(scenes, "TEST-promotion0")
    payload.update(kind="preference", value_text="琥珀色")
    assert not promoted_facts(scenes, "TEST-promotion0")
    payload["kind"] = "fact"
    assert promoted_facts(scenes, "TEST-promotion0")


def test_every_session_first_latency_is_checked():
    window = dict(samples=[dict(visible=True, gaps=[])])
    for name in ("capture", "auto", "retrieval", "start"):
        window[name] = dict(p95_ms=1, samples_ms=[1, 3000] if name == "auto" else [1])
    assert not scale_metrics(window, window, window, dict(depth=0), dict(depth=0), dict(depth=0))["first"]


def test_batch_query_embedding_is_measured():
    from real_host_scale import TimedEmbedding

    embedding = TimedEmbedding(SimpleNamespace(embed_queries=lambda queries, **kwargs: [(1,)] * len(queries)))
    assert embedding.embed_queries(["TEST query"]) == [(1,)]
    assert len(embedding.samples) == 1


def test_doctor_must_explain_the_actual_work_failure():
    from real_host_quality import doctor_explains

    observed = [dict(last_error_code="TEST-model-unavailable", work_type="consolidate")]
    report = dict(
        model_workload_per_100_turns=[dict(model="TEST")],
        status="degraded",
        capability_gaps=["host_registration_incomplete"],
    )
    assert not doctor_explains(report, observed)
    report["pending_error_counts"] = {"TEST-model-unavailable": 1}
    assert doctor_explains(report, observed)


def test_raw_probe_uses_the_actual_claude_prompt_identity(tmp_path):
    from real_host_probe import raw_check
    from scenario_store import ScenarioStore

    scenes = ScenarioStore(tmp_path / "TEST-raw-probe")
    try:
        assert (
            raw_check(
                SimpleNamespace(home=scenes.root / "claude-code-home", host="claude-code", output=tmp_path / "raw.json")
            )
            == 0
        )
    finally:
        scenes.close()


@pytest.mark.parametrize("kind", ["model", "embedding", "vector"])
def test_fault_configuration_reaches_the_fault_without_invalidating_binding(tmp_path, kind):
    from real_host_quality import outage_configs, restore_outage
    from real_host_runtime import library
    from scope_recall.runtime.instance_config import RuntimeInstanceConfig

    routes = route_template(TEST_CHAT_ENDPOINT, "TEST-chat-model")
    run = SimpleNamespace(root=tmp_path, stores=[], args=SimpleNamespace(dry_run=False), routes=routes)
    scenes = library(run, "TEST-fault-config", models=True)
    try:
        raw = json.loads((scenes.store / "runtime-config.json").read_text(encoding="utf-8"))
        vector = Path(raw["vector"]["storage_dir"]) / "vectors.sqlite3"
        vector.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(vector) as conn:
            conn.execute("CREATE TABLE TEST_fixture(value TEXT)")
        original, backups = outage_configs(scenes, kind, "http://127.0.0.1:9/v1/chat/completions")
        for path in original:
            RuntimeInstanceConfig.from_mapping(json.loads(path.read_text(encoding="utf-8")))
        if kind == "vector":
            assert vector.read_bytes().startswith(b"TEST-corrupt")
        restore_outage(scenes, original, backups)
        with sqlite3.connect(vector) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        scenes.close()
