from __future__ import annotations

from pathlib import Path
import json
import sqlite3
from types import SimpleNamespace

import pytest

from p18_hermes_a2a_transport import HermesA2ATransport, HermesHTTPResponse, HermesTransportConfig
from p18_journey_host_bridge import (
    CodexJourneyHostBridge,
    CodexAppServerSessionControl,
    HermesJourneyHostBridge,
    HermesRoutingSessionControl,
    HermesRoutingSessionObserver,
    JourneyHostBridgeError,
    _query,
)


class _Budget:
    def reserve(self, *args):
        return "TEST-reservation"

    def finish(self, *args):
        return "TEST-finished"


class _Sessions:
    def __init__(self):
        self.calls = []

    def new_session(self, alias):
        self.calls.append(("new_session", alias))
        return {"session_id": f"TEST-session-{alias}", "context_id": f"TEST-context-{alias}", "owned_process": True}

    def quiesce(self):
        self.calls.append(("quiesce",))

    def resume(self):
        self.calls.append(("resume",))

    def quiesce_workers(self):
        self.calls.append(("quiesce_workers",))


def _hermes_transport(tmp_path: Path) -> HermesA2ATransport:
    root = tmp_path / "TEST-hermes"
    root.mkdir()
    config = HermesTransportConfig(
        endpoint="http://127.0.0.1:19921",
        expected_agent_card_identity={"name": "TEST", "version": "1"},
        isolation_root=root,
        allow_diagnostic_fixture=True,
    )

    def exchange(method, url, body, timeout):
        return HermesHTTPResponse(200, b"{}", {"content-type": "application/json"})

    return HermesA2ATransport(config, _Budget(), exchange=exchange)


def test_formal_bridge_rejects_fixture_or_unverified_config_before_dispatch(tmp_path: Path):
    root = tmp_path / "TEST-output"
    root.mkdir()
    with pytest.raises(JourneyHostBridgeError, match="formal_config_not_ready"):
        HermesJourneyHostBridge(
            transport=_hermes_transport(tmp_path),
            formal_config_path=tmp_path / "TEST-config.json",
            output_root=root,
            session_control=_Sessions(),
            source_refs_provider=lambda operation_id, session_id: ("TEST-source@1",),
        )


def test_session_controls_are_explicit_and_session_ids_cannot_be_reused():
    bridge = object.__new__(HermesJourneyHostBridge)
    bridge.host_id = "hermes_a2a"
    bridge.session_control = _Sessions()
    bridge._sessions = {}
    bridge._contexts = {}
    bridge._owned_process = {}
    assert bridge.new_session("A")["session_id"] == "TEST-session-A"
    bridge.quiesce()
    bridge.quiesce_workers()
    bridge.resume()
    assert ("quiesce",) in bridge.session_control.calls
    assert ("quiesce_workers",) in bridge.session_control.calls
    assert ("resume",) in bridge.session_control.calls
    with pytest.raises(JourneyHostBridgeError, match="session_alias_reused"):
        bridge.new_session("A")


def test_codex_requires_observed_thread_and_attachments_require_encoder(tmp_path: Path):
    bridge = object.__new__(CodexJourneyHostBridge)
    bridge.host_id = "codex_windows_appserver_native_hooks_v2"
    bridge.session_observer = None
    with pytest.raises(JourneyHostBridgeError, match="codex_thread_observation_missing"):
        bridge._observe("A", "TEST-thread-1", {"association": {"thread_id": "TEST-other"}})
    with pytest.raises(JourneyHostBridgeError, match="attachments_require_explicit_host_encoder"):
        _query({"query": "TEST query", "attachments": [{"asset": {"path": "a", "sha256": "b"}}]}, tmp_path, None)


def test_codex_session_control_uses_thread_start_without_turn(monkeypatch, tmp_path: Path):
    calls = []
    sent = []

    class Peer:
        def __init__(self, process, fixture):
            self.process = process
            self.request_id = 0

        def start(self):
            calls.append(("start",))

        def next_id(self):
            self.request_id += 1
            return self.request_id

        def send(self, payload):
            sent.append(dict(payload))
            calls.append(("send", payload["method"]))

        def wait_response(self, request_id, deadline, notification):
            method = calls[-1][1]
            if method == "thread/start":
                return {"id": request_id, "result": {"thread": {"id": "TEST-real-thread"}}}
            if method == "hooks/list":
                return {"id": request_id, "result": {"data": []}}
            return {"id": request_id, "result": {}}

        def close(self):
            calls.append(("close",))

    process = SimpleNamespace()
    monkeypatch.setattr("p18_journey_host_bridge._spawn", lambda config: process)
    monkeypatch.setattr("p18_journey_host_bridge._JsonlPeer", Peer)
    transport = SimpleNamespace(config=SimpleNamespace(cwd=tmp_path, model="gpt-5.6-luna", timeout_seconds=5.0))
    control = CodexAppServerSessionControl(transport)
    # This test exercises protocol/lifecycle semantics only.  Process tree
    # ownership is covered by the production watcher; avoid requiring a real
    # Windows Job Object for this offline peer test.
    monkeypatch.setattr(control, "watch_process", lambda process: calls.append(("watch", process)))
    receipt = control.new_session("A")
    assert receipt == {"session_id": "TEST-real-thread", "owned_process": True}
    assert [item for item in calls if item[0] == "send"] == [("send", "initialize"), ("send", "initialized"), ("send", "hooks/list"), ("send", "thread/start")]
    thread_start = next(item for item in sent if item["method"] == "thread/start")
    assert thread_start["params"]["ephemeral"] is False
    assert ("close",) not in calls
    assert transport._started_peers["TEST-real-thread"][0] is control._peers["TEST-real-thread"]
    assert not any(item == ("send", "turn/start") for item in calls)
    control.close()
    assert ("close",) in calls


def test_hermes_session_control_reads_real_routing_row_and_rejects_context_as_session(tmp_path: Path):
    root = tmp_path / "TEST-hermes-state"
    root.mkdir()
    db_path = root / "state.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE gateway_routing(scope TEXT, session_key TEXT, entry_json TEXT, updated_at INTEGER)")
        db.execute(
            "INSERT INTO gateway_routing VALUES (?, ?, ?, ?)",
            ("owner_private", "telegram:TEST-chat", json.dumps({"session_id": "TEST-session-1", "context_id": "TEST-context-1", "session_key": "telegram:TEST-chat", "platform": "telegram"}), 1),
        )
        db.commit()
    control = HermesRoutingSessionControl(
        state_db=db_path,
        routes={"A": {"scope": "owner_private", "session_key": "telegram:TEST-chat", "context_id": "TEST-context-1"}},
        config_root=root,
    )
    pending = control.new_session("A")
    assert pending == {
        "session_id": None,
        "pending_context_id": "TEST-context-1",
        "context_id": "TEST-context-1",
        "owned_process": False,
    }
    observed = control.observe("A")
    assert observed["session_id"] == "TEST-session-1"
    assert observed["session_id"] != observed["context_id"]
    assert HermesRoutingSessionObserver(control)("A", "TEST-session-1", {"context_id": "TEST-context-1"})["session_id"] == "TEST-session-1"
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE gateway_routing SET entry_json = ?", (json.dumps({"session_id": "TEST-context-1", "context_id": "TEST-context-1", "session_key": "telegram:TEST-chat", "platform": "telegram"}),))
        db.commit()
    with pytest.raises(JourneyHostBridgeError, match="hermes_gateway_session_mismatch"):
        control.observe("A")


def test_hermes_session_control_returns_pending_context_before_host_creates_route(tmp_path: Path):
    root = tmp_path / "TEST-hermes-pending"
    root.mkdir()
    db_path = root / "state.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE gateway_routing(scope TEXT, session_key TEXT, entry_json TEXT, updated_at INTEGER)")
        db.commit()
    control = HermesRoutingSessionControl(
        state_db=db_path,
        routes={"A": {"session_key": "telegram:TEST-new", "context_id": "TEST-context-new", "scope": "owner_private"}},
        config_root=root,
    )
    pending = control.new_session("A")
    assert pending == {
        "session_id": None,
        "pending_context_id": "TEST-context-new",
        "context_id": "TEST-context-new",
        "owned_process": False,
    }


def test_hermes_alias_reuse_is_rejected_but_same_context_waits_for_idle(monkeypatch, tmp_path: Path):
    root = tmp_path / "TEST-hermes-idle"
    root.mkdir()
    db_path = root / "state.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE gateway_routing(scope TEXT, session_key TEXT, entry_json TEXT, updated_at INTEGER)")
        db.execute(
            "INSERT INTO gateway_routing VALUES (?, ?, ?, ?)",
            ("owner_private", "telegram:TEST-A", json.dumps({"session_id": "TEST-session-A", "context_id": "TEST-context-shared", "session_key": "telegram:TEST-A"}), 1),
        )
        db.commit()
    clock = {"now": 10.0, "sleeps": []}

    def monotonic():
        return clock["now"]

    def sleep(seconds):
        clock["sleeps"].append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr("p18_journey_host_bridge.time.monotonic", monotonic)
    monkeypatch.setattr("p18_journey_host_bridge.time.sleep", sleep)
    control = HermesRoutingSessionControl(
        state_db=db_path,
        routes={
            "A": {"scope": "owner_private", "session_key": "telegram:TEST-A", "context_id": "TEST-context-shared"},
            "B": {"scope": "owner_private", "session_key": "telegram:TEST-B", "context_id": "TEST-context-shared"},
        },
        config_root=root,
        idle_seconds=1,
    )
    assert control.new_session("A")["pending_context_id"] == "TEST-context-shared"
    assert control.observe("A")["session_id"] == "TEST-session-A"
    with pytest.raises(JourneyHostBridgeError, match="session_alias_reused"):
        control.new_session("A")
    second = control.new_session("B")
    assert second["session_id"] is None
    assert second["pending_context_id"] == "TEST-context-shared"
    assert clock["sleeps"]


def test_source_refs_use_dispatch_watermark_and_actual_session(tmp_path: Path):
    db_path = tmp_path / "TEST-source-events.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE source_events(event_id TEXT, source_revision INTEGER, session_id TEXT)")
        db.execute("INSERT INTO source_events VALUES (?, ?, ?)", ("TEST-old", 1, "TEST-session"))
        db.commit()
    provider = __import__("p18_journey_host_bridge", fromlist=["JourneySourceRefsProvider"]).JourneySourceRefsProvider(db_path)
    provider.begin()
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO source_events VALUES (?, ?, ?)", ("TEST-new", 2, "TEST-session"))
        db.execute("INSERT INTO source_events VALUES (?, ?, ?)", ("TEST-other", 1, "TEST-other-session"))
        db.commit()
    assert provider("op-1", "TEST-session") == ("TEST-new@2",)
    assert provider("op-1", "TEST-other-session") == ("TEST-other@1",)


def test_source_refs_require_begin_before_dispatch(tmp_path: Path):
    db_path = tmp_path / "TEST-source-events.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE source_events(event_id TEXT, source_revision INTEGER, session_id TEXT)")
        db.commit()
    provider = __import__("p18_journey_host_bridge", fromlist=["JourneySourceRefsProvider"]).JourneySourceRefsProvider(db_path)
    with pytest.raises(JourneyHostBridgeError, match="source_capture_watermark_unavailable"):
        provider("op-1", "TEST-session")


@pytest.mark.parametrize("fault", [None, "sqlite_unavailable"])
def test_persist_binds_real_Hermes_task_id_and_only_declared_fault_allows_gap(tmp_path, monkeypatch, fault):
    bridge = object.__new__(HermesJourneyHostBridge)
    bridge.formal_config_path = tmp_path / "formal.json"
    bridge._owned_process = {"real-session": True}
    bridge._contexts = {}
    bridge._usage = lambda *_: {"status": "unknown"}
    bridge._operation = lambda _: {"fault": fault}
    bridge.writer = bridge.readiness = bridge.transport = SimpleNamespace()
    observed = {}
    def append(*_args, **kwargs):
        observed.update(kwargs)
        return tmp_path / "operation.json"
    monkeypatch.setattr("p18_journey_host_bridge._append_formal_transport_operation", append)
    raw = tmp_path / "raw.bin"
    result = bridge._persist_turn(operation_id="op", query="PUBLIC query", session_id="real-session",
        source_refs=(), result={"task_id":"actual-task", "status":"COMPLETED", "delivery_gaps":[{"kind":"capture"}]},
        logical_raw=raw, transport_raw=raw, started=__import__("time").perf_counter())
    assert observed["item"]["turn_id"] == result["turn_id"] == "actual-task"
    assert observed["item"]["delivery"]["status"] == "failed"
    assert bool(observed["result"].get("errors")) is (fault is None)
    # Recording sink tests bindings only, never real host or formal admission.


@pytest.mark.parametrize("observation_fails", [False, True])
def test_attachment_gap_and_charged_observation_failure_are_retained(tmp_path, observation_fails):
    bridge = object.__new__(CodexJourneyHostBridge)
    bridge._operation = lambda _: {"arm_id":"C"}
    bridge._sessions = {"s":"actual-session"}
    bridge._pending_aliases = set()
    bridge.attachment_encoder = lambda natural, workspace: natural["query"]
    bridge.transport = SimpleNamespace(budget=SimpleNamespace(formal_usage=lambda:{"status":"unknown","entries":[{"id":"charged"}]}))
    class Sources:
        def begin(self):
            pass
        def __call__(self, *_):
            return ("actual-source@1",)
    bridge.source_refs_provider = Sources()
    bridge._raw_path = lambda _: (tmp_path/"raw",tmp_path/"raw")
    bridge._dispatch = lambda *_: {"status":"COMPLETED"}
    def observe(*_):
        if observation_fails:
            raise JourneyHostBridgeError("public-routing-gap")
    bridge._observe = observe
    bridge._persist_turn = lambda **kwargs: kwargs
    result = bridge.execute_turn(operation_id="op",session_alias="s",session_id="actual-session",
        model_input={"query":"PUBLIC file query","attachments":[{"workspace_path":"v1/test.svg","asset":{"sha256":"a"*64}}]},workspace=tmp_path)
    assert result["result"]["formal_usage"]["entries"] == [{"id":"charged"}]
    if observation_fails:
        assert result["result"]["errors"][0]["kind"] == "session_observation"
    else:
        assert result["result"]["attachment_entry"]["managed_attachment"] == "NOT_VERIFIED"
        assert result["result"]["capability_gaps"] == ["managed_attachment_retention_and_purge_not_verified"]
