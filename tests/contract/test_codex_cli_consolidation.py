"""Focused CLI subscription/worker tests. Native wire proof is opt-in, offline."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from scope_recall.adapters import codex_cli as cli
from scope_recall.adapters.models import AuxiliaryModelError
from scope_recall.contracts import InstanceBinding
from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig, build_auxiliary_runtime
from scope_recall.runtime.instance import RuntimeInstanceConfig, build_runtime_instance
from scope_recall.runtime.model_budget import default_budget_policy, initialize_auxiliary_budget_ledger, pre_request_refusals
from scope_recall.runtime.subscription_budget import SubscriptionBudgetLedger, SubscriptionBudgetPolicy
from v11_support import source_event


def route():
    return cli.CodexCliRouteConfig(Path(sys.executable), next(iter(cli.VERIFIED_BINARIES)))


def ledger(tmp_path, **policy):
    path = tmp_path / "auxiliary-budget.sqlite3"
    initialize_auxiliary_budget_ledger(path, default_budget_policy())
    return SubscriptionBudgetLedger(path, SubscriptionBudgetPolicy(**policy))


def events(text="{}", inputs=100, outputs=20):
    return b"\n".join(json.dumps(e).encode() for e in [
        {"type": "thread.started", "thread_id": "TEST"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": {"input_tokens": inputs, "cached_input_tokens": 0, "output_tokens": outputs}},
    ])


def fake_catalog(monkeypatch):
    monkeypatch.setattr(cli, "_prepare_catalog", lambda r, p, **kw: p / "catalog.json")
    monkeypatch.setattr(cli, "_prepare_state", lambda r, p, **kw: None)


def test_success_stdin_isolation_and_per_pass_budget(tmp_path, monkeypatch):
    fake_catalog(monkeypatch)
    seen = []
    def run(command, *, cwd, prompt, seconds, diagnostics=None):
        seen.append((command, cwd, prompt))
        assert cwd.is_dir() and cwd != tmp_path
        assert command[-1] == "-" and "synthetic-only" not in " ".join(command)
        assert "--ephemeral" in command and "--ignore-user-config" in command
        assert "read-only" in command and 'approval_policy="never"' in command
        return 0, events()
    monkeypatch.setattr(cli, "_run", run)
    account = ledger(tmp_path)
    adapter = cli.CodexCliConsolidationAdapter(route(), ledger=account)
    assert adapter.propose([dict(role="user", content="synthetic-only")], remaining_seconds=10) == "{}"
    with pytest.raises(AuxiliaryModelError, match="budget_exhausted"):
        adapter.propose([dict(role="user", content="synthetic-only")], remaining_seconds=10)
    assert len(seen) == 1 and not seen[0][1].exists()
    assert b"synthetic-only" in seen[0][2]
    assert account.status()["input_tokens"] == 100
    assert account.status()["charge_micro_usd"] is None
    assert adapter.for_pass().propose([dict(role="user", content="next pass")], remaining_seconds=10) == "{}"


@pytest.mark.parametrize("mode", ["exit", "malformed", "timeout", "overspend"])
def test_failed_attempts_remain_accounted_and_overspend_fences(tmp_path, monkeypatch, mode):
    fake_catalog(monkeypatch)
    def run(*args, **kwargs):
        if mode == "timeout":
            raise AuxiliaryModelError("timeout")
        if mode == "exit":
            return 7, events()
        if mode == "malformed":
            return 0, b"not JSON"
        return 0, events(inputs=40000)
    monkeypatch.setattr(cli, "_run", run)
    account = ledger(tmp_path)
    adapter = cli.CodexCliConsolidationAdapter(route(), ledger=account)
    with pytest.raises(AuxiliaryModelError):
        adapter.propose([dict(role="user", content="synthetic")], remaining_seconds=10)
    status = account.status()
    assert status["daily_calls"] == 1
    assert status["input_tokens"] == (100 if mode == "exit" else 40000 if mode == "overspend" else 32768)
    if mode == "overspend":
        assert status["meter_breach"] is True
        with pytest.raises(AuxiliaryModelError, match="budget_exhausted"):
            adapter.for_pass().propose([dict(role="user", content="synthetic")], remaining_seconds=10)
        assert account.status()["daily_calls"] == 1


def test_daily_caps_unknown_usage_and_restart(tmp_path):
    account = ledger(tmp_path)
    for _ in range(4):
        request_id = account.reserve(cli.MODEL, timeout_seconds=1)
        account.finish(request_id, "timeout", None, timeout_seconds=1)
    reopened = SubscriptionBudgetLedger(account.path, account.policy)
    with pytest.raises(ValueError, match="budget_exhausted"):
        reopened.reserve(cli.MODEL, timeout_seconds=1)
    assert reopened.status()["input_tokens"] == 131072
    assert reopened.status()["output_tokens"] == 32768
    token_limited = ledger(tmp_path / "tokens", daily_calls=4, daily_input_tokens=32768)
    token_limited.reserve(cli.MODEL, timeout_seconds=1)
    with pytest.raises(ValueError, match="budget_exhausted"):
        token_limited.reserve(cli.MODEL, timeout_seconds=1)


def test_timeout_reaps_only_owned_subprocess_tree(tmp_path):
    child_pid = tmp_path / "child.pid"
    code = ("import subprocess,sys,time,pathlib; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"pathlib.Path({str(child_pid)!r}).write_text(str(p.pid)); time.sleep(60)")
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        started = time.monotonic()
        with pytest.raises(AuxiliaryModelError, match="timeout"):
            cli._run([sys.executable, "-c", code], cwd=tmp_path, prompt=b"synthetic", seconds=1)
        assert time.monotonic() - started < 6
        assert sentinel.poll() is None
        pid = int(child_pid.read_text())
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                status = ctypes.c_ulong()
                ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(status))
                ctypes.windll.kernel32.CloseHandle(handle)
                assert status.value != 259
        else:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        sentinel.kill()
        sentinel.wait(timeout=3)


def test_runtime_core_schema_lease_and_pass_reset(tmp_path, monkeypatch):
    fake_catalog(monkeypatch)
    account = ledger(tmp_path)
    config = AuxiliaryRuntimeConfig.from_mapping(dict(
        external_embedding=False, external_consolidation=True, ledger_path=str(account.path),
        consolidation=dict(kind="codex_cli", executable=sys.executable,
                           executable_sha256=route().executable_sha256)))
    assert isinstance(build_auxiliary_runtime(config).consolidation, cli.CodexCliConsolidationAdapter)
    assert not pre_request_refusals(config)
    binding = InstanceBinding("TEST-agent", "TEST-cli", tmp_path / "data", frozenset({"TEST-scope"}), True)
    instance = build_runtime_instance(RuntimeInstanceConfig(binding, "TEST-session", binding.scope_ids,
                                                           auxiliary=config, max_items=4))
    instance.core.initialize()
    ctx = instance.config.context()
    try:
        saved = instance.core.record_event(ctx, source_event(content="Synthetic project selects blue.",
                                                            source_event_key="TEST-cli/1"),
                                           scope_id="TEST-scope", remaining_seconds=5)
        ref = saved.event_refs[0]
        payload = dict(protocol_version="1.1", source_refs=[f"{ref.ref}@{ref.revision}"],
                       claim_proposals=[], resume_proposals=[], reference_proposals=[])
        monkeypatch.setattr(cli, "_run", lambda *a, **kw: (0, events(json.dumps(payload))))
        receipt = instance.drain(max_items=4)
        assert receipt.completed == 1
        with closing(sqlite3.connect(instance.core.storage.path)) as db:
            state = db.execute("SELECT state,lease_owner FROM work_items WHERE work_type='consolidate'").fetchone()
        assert state == ("done", None)
        # A fresh pass still runs, while an invalid schema never commits claims.
        instance.core.record_event(ctx, source_event(content="Synthetic second source chooses green.",
                                                    source_event_key="TEST-cli/2"),
                                   scope_id="TEST-scope", remaining_seconds=5)
        monkeypatch.setattr(cli, "_run", lambda *a, **kw: (0, events('{"invalid":true}')))
        bad = instance.drain(max_items=4)
        assert any(item.error_code for item in bad.items)
        assert account.status()["daily_calls"] == 2
        with closing(sqlite3.connect(instance.core.storage.path)) as db:
            assert db.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM work_items WHERE lease_owner IS NOT NULL").fetchone()[0] == 0
    finally:
        instance.close()


@pytest.mark.parametrize("mode", ["timeout", "rate_limit", "success"])
def test_bounded_diagnostics_keep_stage_without_body_or_retry(tmp_path, monkeypatch, mode):
    fake_catalog(monkeypatch)
    original_run = cli._run
    calls = []
    sentinel = "TEST_PRIVATE_BODY_NOT_DIAGNOSTICS"
    wire = (json.dumps({"type": "thread.started", "thread_id": sentinel}) + "\n" +
            json.dumps({"type": "turn.started"}) + "\n" +
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": sentinel + " HTTP 429"}}) + "\n")
    stderr = ("HTTP 429 Too Many Requests " if mode == "rate_limit" else "") + sentinel
    if mode == "success":
        wire = events(sentinel).decode() + "\n"
    code = ("import sys,time; sys.stdin.buffer.read(); "
            f"sys.stdout.write({wire!r}); sys.stdout.flush(); "
            f"sys.stderr.write({(stderr + chr(10))!r}); sys.stderr.flush(); "
            + ("time.sleep(60)" if mode != "success" else ""))
    def run(command, **kwargs):
        calls.append(1)
        return original_run([sys.executable, "-c", code], **kwargs)
    monkeypatch.setattr(cli, "_run", run)
    account = ledger(tmp_path)
    adapter = cli.CodexCliConsolidationAdapter(route(), ledger=account)
    if mode == "success":
        assert adapter.propose([dict(role="user", content=sentinel)], remaining_seconds=5) == sentinel
    else:
        with pytest.raises(AuxiliaryModelError, match="timeout" if mode == "timeout" else "codex_turn_failed"):
            adapter.propose([dict(role="user", content=sentinel)], remaining_seconds=5)
    diag = adapter.last_diagnostics["model"]
    assert sentinel not in json.dumps(adapter.last_diagnostics)
    assert "turn.started" in diag["event_types"]
    assert diag["cleanup_process_state"] == "exited"
    if mode != "success":
        assert diag["phase"] == "process_wait"
        assert diag["process_state"] == "still_running"
    if mode == "rate_limit":
        assert diag["http_statuses"] == [429]
        assert diag["error_categories"] == ["rate_limited"]
        assert diag["elapsed_seconds"] < 3
    else:
        assert diag["http_statuses"] == []
    assert len(calls) == 1 and account.status()["daily_calls"] == 1
    with pytest.raises(AuxiliaryModelError, match="budget_exhausted"):
        adapter.propose([dict(role="user", content=sentinel)], remaining_seconds=5)
    assert len(calls) == 1


def test_native_cli_wire_has_zero_tools(tmp_path, monkeypatch):
    """Real pinned CLI, local fake HTTP only, isolated home and no OAuth."""
    executable = os.environ.get("SCOPE_RECALL_CODEX_CLI")
    if not executable:
        pytest.skip("set SCOPE_RECALL_CODEX_CLI for the offline native boundary proof")
    (tmp_path / "codex-home").mkdir()
    # Deliberately invalid user configuration must not be loaded. No credentials
    # are created here; this is the loopback-only native boundary proof.
    (tmp_path / "codex-home" / "config.toml").write_text("[invalid user config", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            assert not self.headers.get("Authorization")
            body = json.loads(raw)
            requests.append(body)
            item = {"id": "msg_test", "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "{}"}]}
            frames = [
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": {"id": "resp_test", "status": "completed",
                 "output": [item], "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                                              "input_tokens_details": {"cached_tokens": 0}}}},
            ]
            data = b"".join(("event: " + frame["type"] + "\ndata: " + json.dumps(frame) + "\n\n").encode() for frame in frames)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        configured = replace(route(), executable=Path(executable))
        catalog = cli._prepare_catalog(configured, tmp_path, seconds=8)
        cli._prepare_state(configured, tmp_path, seconds=5)
        # Native initialization belongs to an empty, credential-free home, not
        # the caller's login/history directory (whose config above is invalid).
        assert not (tmp_path / "bootstrap-home" / "auth.json").exists()
        state_db, = (tmp_path / "state").glob("state_*.sqlite")
        with closing(sqlite3.connect(state_db.as_uri() + "?mode=ro", uri=True)) as db:
            assert db.execute("SELECT status FROM backfill_state WHERE id=1").fetchone() == ("complete",)
            assert db.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0
        command = cli._command(configured, tmp_path, catalog)
        command[-1:-1] = ["-c", f'model_providers.codex_subscription.base_url="http://127.0.0.1:{server.server_port}/v1"',
                          "-c", "model_providers.codex_subscription.requires_openai_auth=false",
                          "--disable", "enable_request_compression"]
        code, raw = cli._run(command, cwd=tmp_path, prompt=b"Return {}", seconds=15)
        # Do not include stdout/stderr or request prompt in assertion diagnostics.
        assert len(requests) == 1, f"request_count={len(requests)}, exit={code}"
        additional = [item for item in requests[0].get("input", []) if item.get("type") == "additional_tools"]

        assert requests[0].get("tools", []) == []
        assert all(item.get("tools") == [] for item in additional)
        assert requests[0].get("model") == cli.MODEL
        assert code == 0
        assert cli._decode(raw, code)[2] == "codex_success"
        print("native_wire: model=gpt-5.6-luna tools=[] requests=1 authorization=absent exit=0")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
