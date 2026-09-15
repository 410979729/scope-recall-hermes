"""Public offline configuration tests; no real host, API, or semantic score."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from p18_journey_execution import JourneyExecutionError
from p18_owned_host_lifecycle import OwnedHermesProcess, WorkerPause
from p18_run_journey import attachment_path_input, run_journey


def test_attachment_uses_exact_actual_file_and_no_expected_input(tmp_path):
    workspace = tmp_path / "TEST-workspace"
    workspace.mkdir()
    path = workspace / "v1" / "image.svg"
    path.parent.mkdir()
    path.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="3"/></svg>')
    row = {"workspace_path": "v1/image.svg", "asset": {"path": "private.svg", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}
    result = attachment_path_input({"query":"PUBLIC TEST inspect the file", "attachments":[row]},workspace)
    assert str(path.resolve()) in result and "private.svg" not in result
    path.write_bytes(b"changed")
    with pytest.raises(JourneyExecutionError,match="hash_mismatch"):
        attachment_path_input({"query":"PUBLIC TEST", "attachments":[row]},workspace)
    row["workspace_path"] = "../outside.svg"
    with pytest.raises(JourneyExecutionError,match="workspace_attachment"):
        attachment_path_input({"query":"PUBLIC TEST", "attachments":[row]},workspace)


def test_worker_pause_is_real_existing_advisory_lock(tmp_path):
    from scope_recall.file_lock import advisory_file_lock
    path = tmp_path / "TEST-data"
    path.mkdir()
    pause = WorkerPause(path)
    pause.acquire()
    from concurrent.futures import ThreadPoolExecutor
    def other_owner():
        with advisory_file_lock(path / "runtime-worker.lock",timeout_seconds=0):
            return True
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(other_owner)
        with pytest.raises(TimeoutError):
            future.result(timeout=1)
    pause.close()
    with advisory_file_lock(path / "runtime-worker.lock",timeout_seconds=0):
        pass


def test_existing_health_log_is_preserved_for_next_owned_launch(tmp_path):
    root=tmp_path/"TEST-owned"
    home=root/"home"
    home.mkdir(parents=True)
    archive=root/"archive"
    archive.mkdir()
    previous=archive/"cli-meter-1.log"
    previous.write_bytes(b"PUBLIC zero-model health evidence")
    binding={"arm_id":"A","roots":{"binding_root":str(root),"home_path":str(home),"database_path":str(home/"unused.db")}}
    owner=OwnedHermesProcess(binding,formal_config_path=root/"formal.json",context_id="TEST-health")
    assert owner.generation==1
    assert previous.read_bytes()==b"PUBLIC zero-model health evidence"


def test_owned_quiesce_uses_only_retained_children(monkeypatch):
    stopped = []
    monkeypatch.setattr("probes.hermes.p11_start_a2a_test._terminate_owned",lambda child: stopped.append(child))
    owner = object.__new__(OwnedHermesProcess)
    gateway,bridge = object(),object()
    owner.gateway,owner.bridge = gateway,bridge
    closed=[]
    owner.jobs=[SimpleNamespace(close=lambda:closed.append("job"))]
    owner.logs=[SimpleNamespace(close=lambda:closed.append("log"))]
    owner.quiesce()
    assert stopped == [gateway,bridge] and closed == ["job","log"]
    assert owner.gateway is None and owner.bridge is None


def test_preflight_binds_fault_to_frozen_action_without_starting_host(tmp_path,monkeypatch):
    root=tmp_path/"TEST-bundle"
    arm=root/"arm"
    workspace=arm/"workspace"
    workspace.mkdir(parents=True)
    binding={"host_id":"codex_windows_desktop","arm_id":"A","roots":{"binding_root":str(arm)},
             "launch_contract":{"working_directory":str(workspace)}}
    binding_path=root/"binding.json"
    binding_path.write_text(json.dumps(binding),encoding="utf-8")
    op={"host_id":"codex_windows_appserver_native_hooks_v2","arm_id":"A","unit":{"journey_id":"J08"},"fault":"sqlite_unavailable"}
    ready=SimpleNamespace(formal_execution_allowed=True,details={"operations":{"turn":op},"ledger_path":str(root/"ledger.sqlite3")})
    monkeypatch.setattr("p18_run_journey.verify_formal_run_config",lambda _:ready)
    actions=[{"kind":"sqlite_unavailable"},{"kind":"host_turn","operation_id":"turn"}]
    monkeypatch.setattr("p18_run_journey.load_journey",lambda *_:(root/"private",{"actions":actions}))
    monkeypatch.setattr("p18_run_journey.OwnedHermesProcess",lambda *_a,**_k:pytest.fail("preflight started host"))
    kwargs=dict(host_binding_path=binding_path,formal_config_path=root/"formal.json",journey_bundle_path=root/"private/journeys.json",
                journey_id="J08",output_root=arm/"output")
    result=run_journey(**kwargs)
    assert result["status"]=="PREFLIGHT_ONLY" and result["model_calls"]==0
    op.pop("fault")
    with pytest.raises(JourneyExecutionError,match="fault_map"):
        run_journey(**kwargs)


@pytest.mark.parametrize("host_id,route",[("hermes_a2a","go"),("codex_windows_appserver_native_hooks_v2","codex")])
def test_runner_retains_exact_request_bytes_and_host_ledger_route(tmp_path,host_id,route):
    from p18_formal_runner import _append_formal_transport_operation
    root=tmp_path/"TEST-formal"
    operation=root/"operations/op"
    operation.mkdir(parents=True)
    captured=[]
    writer=SimpleNamespace(root=root,append_operation=lambda record:captured.append(record) or root/"op.json")
    request={"jsonrpc":"2.0","id":"request","method":"message/send","params":{"text":"PUBLIC non-ASCII 文"}}
    raw=json.dumps(request,ensure_ascii=False,separators=(",", ":")).encode("utf-8")
    result={"formal_evaluation":True,"fixture_mode":False,"transport_status":"COMPLETED","status":"COMPLETED",
            "request":request,"request_sha256":hashlib.sha256(raw).hexdigest(),"task_id":"turn","context_id":"context",
            "answer_text":"PUBLIC answer","public_output":"PUBLIC answer","process":{"pid":123},
            "association":{"thread_id":"session","turn_id":"turn"},"transport":"PUBLIC-protocol-unit"}
    response=operation/"response.json"
    response.write_bytes(json.dumps(result).encode())
    item={"operation_id":"op","session_id":"session","owned_process":True,"source_capture_refs":[],
          "usage":{"status":"unknown","input_tokens":None,"output_tokens":None,"ledger_path":"ledger.sqlite3",
                   "entries":[{"id":1 if route=="go" else "op","request":{"path":"model.bin","sha256":"a"*64}}]}}
    readiness=SimpleNamespace(details={"config_path":str(root/"formal.json"),"source":{"commit":"PUBLIC"}})
    _append_formal_transport_operation(writer,readiness,frozen={"host_id":host_id,"arm_id":"A","request_id":"request",
        "unit":{"kind":"round","journey_id":"J01","round_id":"1","query_id":None}},
        item=item,result=result,response_path=response,latency_ms=1,transport=SimpleNamespace())
    association=json.loads((operation/"association.json").read_bytes())
    assert association["ledger_entries"][0]["route"]==route
    if route=="go":
        assert (operation/"host-request.json").read_bytes()==raw
        assert association["host_request"]["sha256"]==result["request_sha256"]
    # This test exercises serialization with a recording sink; it is not a
    # FormalEvidenceWriter or a genuine host-completion receipt.
    assert captured[0]["usage"]["status"]=="unknown"


def test_ensure_frozen_hermes_authorization_binds_hash_checked_file(tmp_path, monkeypatch):
    from p18_owned_host_lifecycle import (
        _HERMES_AUTH_PATH,
        _HERMES_AUTH_SHA,
        ensure_frozen_hermes_attempt_authorization,
    )
    fixture = tmp_path / "TEST-hermes-attempt-authorization.json"
    fixture.write_text(json.dumps({"kind": "TEST-fixture", "secret": False}, sort_keys=True), encoding="utf-8")
    digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
    monkeypatch.setattr("p18_owned_host_lifecycle.FROZEN_HERMES_ATTEMPT_AUTHORIZATION", fixture)
    monkeypatch.setattr("p18_owned_host_lifecycle.FROZEN_HERMES_ATTEMPT_AUTHORIZATION_SHA256", digest)
    monkeypatch.delenv(_HERMES_AUTH_PATH, raising=False)
    monkeypatch.delenv(_HERMES_AUTH_SHA, raising=False)
    env = {}
    ensure_frozen_hermes_attempt_authorization(env)
    assert env[_HERMES_AUTH_SHA] == digest
    assert Path(env[_HERMES_AUTH_PATH]) == fixture
    env[_HERMES_AUTH_PATH] = str(tmp_path / "already-set.json")
    env[_HERMES_AUTH_SHA] = "custom"
    ensure_frozen_hermes_attempt_authorization(env)
    assert env[_HERMES_AUTH_PATH] == str(tmp_path / "already-set.json")
    assert env[_HERMES_AUTH_SHA] == "custom"


def test_ensure_frozen_hermes_authorization_missing_file_leaves_env(tmp_path, monkeypatch):
    from p18_owned_host_lifecycle import (
        _HERMES_AUTH_PATH,
        _HERMES_AUTH_SHA,
        ensure_frozen_hermes_attempt_authorization,
    )
    missing = tmp_path / "TEST-missing-hermes-attempt-authorization.json"
    monkeypatch.setattr("p18_owned_host_lifecycle.FROZEN_HERMES_ATTEMPT_AUTHORIZATION", missing)
    monkeypatch.setattr("p18_owned_host_lifecycle.FROZEN_HERMES_ATTEMPT_AUTHORIZATION_SHA256", "a" * 64)
    monkeypatch.delenv(_HERMES_AUTH_PATH, raising=False)
    monkeypatch.delenv(_HERMES_AUTH_SHA, raising=False)
    env = {}
    ensure_frozen_hermes_attempt_authorization(env)
    assert env == {}


def test_ensure_frozen_hermes_authorization_rejects_hash_mismatch(tmp_path, monkeypatch):
    from p18_owned_host_lifecycle import (
        OwnedHostError,
        _HERMES_AUTH_PATH,
        _HERMES_AUTH_SHA,
        ensure_frozen_hermes_attempt_authorization,
    )
    monkeypatch.delenv(_HERMES_AUTH_PATH, raising=False)
    monkeypatch.delenv(_HERMES_AUTH_SHA, raising=False)
    fake = tmp_path / "budget-authorization-20260908-hermes-turn-v1.json"
    fake.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("p18_owned_host_lifecycle.FROZEN_HERMES_ATTEMPT_AUTHORIZATION", fake)
    with pytest.raises(OwnedHostError, match="hash_mismatch"):
        ensure_frozen_hermes_attempt_authorization({})
