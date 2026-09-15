"""Offline isolation of the P18 Hermes meter ledger. No host or model calls."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

from p18_hermes_operation_budget import HermesOperationBudget
from p18_journey_execution import JourneyExecutionError
from p18_owned_host_lifecycle import OwnedHermesProcess
from p18_run_journey import bound_p11_bridge_ledger, cli_host
from probes.hermes.p11_a2a_bridge import Bridge, FORMAL_BATCH_NAME
from probes.hermes.p11_a2a_testkit import (
    LEDGER,
    active_bridge_ledger,
    resolve_hash_bound_formal_ledger,
    resolve_hash_bound_runtime_budget,
)


_BUDGET = {
    "approved_models": ["deepseek-v4-flash"],
    "batch": "YUHENG_HOST_R2_HR2_C",
    "cap_micro_usd": 10000000,
    "default_reserve_input": 32768,
    "default_reserve_output": 4096,
    "max_request_bytes": 786432,
    "pricing": {"deepseek-v4-flash": {"input_usd_per_million": "0.44", "output_usd_per_million": "1.32"}},
    "total_call_cap": 512,
    "total_input_cap": 8000000,
    "total_output_cap": 16000000,
}


def _freeze(path: Path) -> dict:
    stat = path.stat()
    return {
        "schema": "scope-recall.p18-original-ledger.v1",
        "canonical_path": str(path.resolve()),
        "device": stat.st_dev,
        "inode": stat.st_ino,
    }


def _bundle(tmp_path: Path):
    root = tmp_path / "TEST-formal"
    root.mkdir()
    ledger = root / "auxiliary-ledger.sqlite3"
    ledger.write_bytes(b"")
    formal = root / "formal-query.json"
    formal.write_text(
        json.dumps({"ledger_path": ledger.name, "ledger_binding": _freeze(ledger)}),
        encoding="utf-8",
    )
    runtime = root / "runtime-config.json"
    runtime.write_text(
        json.dumps({"auxiliary": {"ledger_path": str(ledger.resolve()), "budget": _BUDGET}}),
        encoding="utf-8",
    )
    return formal, ledger, runtime


def test_old_p11_without_formal_config_keeps_shared_ledger():
    assert active_bridge_ledger() == LEDGER.resolve()
    bridge = Bridge(Path("TEST-old-p11-state"), 29991, formal_active_operation=True, formal_freeze_sha256="a" * 64)
    assert Path(bridge.ledger.path).resolve() == LEDGER.resolve()
    assert bridge.ledger.policy.batch == FORMAL_BATCH_NAME


def test_hash_bound_formal_ledger_matches_cli_host_and_bridge_budget(tmp_path, monkeypatch):
    formal, ledger, runtime = _bundle(tmp_path)
    freeze = hashlib.sha256(formal.read_bytes()).hexdigest()
    readiness = SimpleNamespace(details={"ledger_path": str(ledger)})
    assert bound_p11_bridge_ledger(formal, readiness.details["ledger_path"]) == ledger.resolve()
    captured = {}

    class Budget(HermesOperationBudget):
        def __init__(self, state, ledger_path, **kwargs):
            super().__init__(state, ledger_path, **kwargs)
            captured["budget"] = self

    class Transport:
        def __init__(self, binding, formal_config_path, budget):
            captured["transport_budget"] = budget
            self.owner = SimpleNamespace(command=None)

    monkeypatch.setattr("p18_hermes_operation_budget.HermesOperationBudget", Budget)
    monkeypatch.setattr("p18_hermes_cli_transport.HermesCLITransport", Transport)
    monkeypatch.setattr("p18_journey_host_bridge.HermesCLISessionControl", lambda transport: SimpleNamespace(observe=None))
    monkeypatch.setattr(
        "p18_journey_host_bridge.HermesCLIJourneyHostBridge",
        lambda **kwargs: captured.setdefault("bridge", kwargs) or kwargs["transport"],
    )
    binding = {"arm_id": "C", "roots": {"binding_root": str(tmp_path / "TEST-formal"), "database_path": str(tmp_path / "TEST-formal" / "db")}}
    cli_host(binding, formal, tmp_path / "TEST-formal" / "out", readiness)
    runtime_sha = hashlib.sha256(runtime.read_bytes()).hexdigest()
    meter = Bridge(
        tmp_path / "TEST-formal" / "state",
        29991,
        formal_active_operation=True,
        formal_freeze_sha256=freeze,
        formal_config_path=formal,
        runtime_config_path=runtime,
        runtime_config_sha256=runtime_sha,
    )
    assert captured["budget"].ledger_path == ledger.resolve()
    assert Path(meter.ledger.path).resolve() == ledger.resolve()
    assert meter.ledger.policy.batch == "YUHENG_HOST_R2_HR2_C"
    assert meter.ledger.policy.cap_micro_usd == 10_000_000
    assert meter.ledger.policy.total_call_cap == 512
    assert Path(meter.ledger.path).resolve() != LEDGER.resolve()


def _raises(exc_type, match, fn):
    try:
        fn()
    except exc_type as exc:
        if match and not re.search(match, str(exc)):
            raise AssertionError(f"{exc_type.__name__} {exc!r} did not match {match!r}") from exc
        return
    raise AssertionError(f"{exc_type.__name__} was not raised")


def test_readiness_conflict_and_unbound_or_out_of_bound_ledger_rejected(tmp_path):
    formal, ledger, runtime = _bundle(tmp_path)
    _raises(JourneyExecutionError, "existing_P11_bridge_ledger_binding_mismatch", lambda: bound_p11_bridge_ledger(formal, LEDGER))
    _raises(ValueError, "hash mismatch", lambda: resolve_hash_bound_formal_ledger(formal, "0" * 64))
    _raises(ValueError, "hash must be supplied together", lambda: active_bridge_ledger(formal_config_path=formal, formal_freeze_sha256=None))
    outside = Path(r"F:/SCOPERECALL更新项目/worktrees/scope-recall-v1.1/.execution/not-a-formal-config.json")
    _raises(ValueError, "absolute TEST formal config", lambda: resolve_hash_bound_formal_ledger(outside, "a" * 64))
    freeze = hashlib.sha256(formal.read_bytes()).hexdigest()
    _raises(
        ValueError,
        "hash-bound runtime budget",
        lambda: Bridge(
            tmp_path / "TEST-formal" / "state",
            29991,
            formal_active_operation=True,
            formal_freeze_sha256=freeze,
            formal_config_path=formal,
        ),
    )
    _raises(
        ValueError,
        "does not match formal ledger",
        lambda: resolve_hash_bound_runtime_budget(runtime, hashlib.sha256(runtime.read_bytes()).hexdigest(), LEDGER),
    )


def test_owned_meter_command_forwards_hash_bound_formal_and_runtime(tmp_path):
    formal, ledger, runtime = _bundle(tmp_path)
    root = tmp_path / "TEST-formal"
    home = root / "home"
    home.mkdir()
    (root / "archive").mkdir(exist_ok=True)
    binding = {
        "arm_id": "C",
        "roots": {
            "binding_root": str(root),
            "home_path": str(home),
            "database_path": str(root / "memory.sqlite3"),
            "runtime_config_path": str(runtime),
        },
    }
    owner = OwnedHermesProcess(binding, formal_config_path=formal, context_id="TEST-local")
    command = owner._meter_bridge_command(port=30001)
    freeze = hashlib.sha256(formal.read_bytes()).hexdigest()
    runtime_sha = hashlib.sha256(runtime.read_bytes()).hexdigest()
    assert "--formal-config" in command and str(formal.resolve()) in command
    assert "--formal-freeze-sha256" in command and freeze in command
    assert "--runtime-config" in command and str(runtime.resolve()) in command
    assert runtime_sha in command
    assert "29991" not in command or command[command.index("--port") + 1] == "30001"
    assert LEDGER.name not in " ".join(command)
    assert not any(child.poll() is not None for child in (owner.gateway, owner.bridge) if child is not None)
