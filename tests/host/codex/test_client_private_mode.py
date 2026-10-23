"""Private capture, late records and metadata-only remote preflight on the client boundaries."""

import json
from contextlib import closing
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from scope_recall.adapters.clients import remote_client, remote_server
from scope_recall.adapters.clients.config import load_shared_client
from scope_recall.adapters.clients.handler import CodexHookHandler
from scope_recall.adapters.clients.identity import resolve_runtime_audience, trusted_context
from scope_recall.adapters.hermes.shared_entries import attach_shared_record, client_entry_record, read_shared_payload
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.management import MemoryManagement
from scope_recall.core.restore import InstallationMaintenance

from tests.host.codex.test_remote import NOW, _client, store


def _cached_rules(config, value):
    """Seed only the server's metadata answer, as a preceding hook would."""
    config["state_dir"].mkdir(parents=True, exist_ok=True)
    (config["state_dir"] / "private-rules.json").write_text(json.dumps({"private_rules": value}), encoding="utf-8")


def _fake_transport(monkeypatch, *, private_rules=False):
    """Count physical requests and simulate outages without contacting a server."""
    transport = SimpleNamespace(away=False, connections=0, calls=[], private_rules=private_rules)

    class Connection:
        def __init__(self, *args, **kwargs):
            self.sock = SimpleNamespace(settimeout=lambda value: None)

        def connect(self):
            transport.connections += 1
            if transport.away:
                raise ConnectionRefusedError("TEST server unreachable")

        def request(self, _method, path, **kwargs):
            self.path, self.body = path, json.loads(kwargs["body"])
            transport.calls.append((path, self.body))

        def getresponse(self):
            if self.path.endswith("/private/check"):
                answer = dict(
                    mode="full",
                    capture_blocked=True,
                    recall_blocked=True,
                    decisions=[True] * len(self.body["times"]),
                    private_rules=transport.private_rules,
                )
            else:
                answer = dict(result={}, private_rules=transport.private_rules)
            return SimpleNamespace(status=200, read=lambda: json.dumps(answer).encode())

        def close(self):
            pass

    monkeypatch.setattr(remote_client.http.client, "HTTPConnection", Connection)
    return transport


@pytest.mark.parametrize("cached", [False, None], ids=["stored-false", "fresh"])
def test_remote_without_known_rules_spools_offline_and_replays_on_next_hook(tmp_path, monkeypatch, cached):
    config = _client(tmp_path, "codex", 1)
    if cached is not None:
        _cached_rules(config, cached)
    transport = _fake_transport(monkeypatch)
    transport.away = True
    payload = dict(hook_event_name="Stop", session_id="TEST-session-1", last_assistant_message="TEST offline body")
    remote_client.run_hook(config, json.dumps(payload).encode())
    kept = list(remote_client._spool_dir(config).glob("*.json"))
    assert len(kept) == 1, "an ordinary offline hook must remain queued"
    assert json.loads(kept[0].read_text(encoding="utf-8"))["payload"] == payload
    remote_client.run_hook(config, json.dumps({**payload, "last_assistant_message": "TEST cooldown body"}).encode())
    assert len(list(remote_client._spool_dir(config).glob("*.json"))) == 2
    assert transport.connections == 1, "the cooldown keeps hooks without reconnecting"
    transport.away = False
    monkeypatch.setattr(remote_client, "_server_away", lambda config: False)
    monkeypatch.setattr(remote_client, "_start_flush", lambda config: remote_client.flush_spool(config))
    remote_client.run_hook(
        config, json.dumps(dict(hook_event_name="SessionStart", session_id="TEST-session-1")).encode()
    )
    assert [path for path, _body in transport.calls] == ["/hook"] * 3
    assert [body["payload"].get("last_assistant_message") for _path, body in transport.calls[1:]] == [
        "TEST offline body",
        "TEST cooldown body",
    ]
    assert list(remote_client._spool_dir(config).glob("*.json")) == []


@pytest.mark.parametrize("cached", [False, None], ids=["stored-false", "fresh"])
def test_remote_first_content_answer_enables_strict_checks_for_next_process(tmp_path, monkeypatch, cached):
    config = _client(tmp_path, "codex", 1)
    if cached is not None:
        _cached_rules(config, cached)
    transport = _fake_transport(monkeypatch, private_rules=True)
    payload = dict(hook_event_name="Stop", session_id="TEST-session-1", last_assistant_message="TEST body")
    remote_client.run_hook(config, json.dumps(payload).encode())
    assert [path for path, _body in transport.calls] == ["/hook"], "ordinary hooks need no metadata round trips"
    assert json.loads((config["state_dir"] / "private-rules.json").read_text(encoding="utf-8")) == {
        "private_rules": True
    }
    config = remote_client.load_client_config(config["config"])
    remote_client.run_hook(config, json.dumps(payload).encode())
    assert [path for path, _body in transport.calls] == ["/hook", "/private/check"]
    assert list(remote_client._spool_dir(config).glob("*.json")) == []


def test_remote_known_rules_keep_nothing_during_an_outage(tmp_path, monkeypatch):
    config = _client(tmp_path, "codex", 1)
    _cached_rules(config, True)
    transport = _fake_transport(monkeypatch, private_rules=True)
    transport.away = True
    remote_client.run_hook(
        config,
        json.dumps(
            dict(hook_event_name="Stop", session_id="TEST-session-1", last_assistant_message="TEST private body")
        ).encode(),
    )
    assert transport.connections == 1
    assert transport.calls == []
    assert list(remote_client._spool_dir(config).glob("*.json")) == []


def test_remote_metadata_answer_overwrites_cached_rule_presence(tmp_path, monkeypatch):
    config = _client(tmp_path, "codex", 1)
    _cached_rules(config, True)
    _fake_transport(monkeypatch, private_rules=False)
    remote_client._private_batch(config, dict(payload=dict(session_id="TEST-session-1")), [NOW], 1)
    assert json.loads((config["state_dir"] / "private-rules.json").read_text(encoding="utf-8")) == {
        "private_rules": False
    }


def test_flush_uses_the_rule_answer_received_from_the_preceding_record(tmp_path, monkeypatch):
    config = _client(tmp_path, "codex", 1)
    _cached_rules(config, False)
    transport = _fake_transport(monkeypatch, private_rules=True)
    for number in range(2):
        remote_client._spool(
            config,
            dict(hook_event_name="Stop", session_id="TEST-session-1", last_assistant_message=f"TEST queued {number}"),
            NOW,
        )
    assert remote_client.flush_spool(config) == 1
    assert [path for path, _body in transport.calls] == ["/hook", "/private/check"]
    assert list(remote_client._spool_dir(config).glob("*.json")) == []


class PrivateClock:
    def __init__(self):
        self.now = NOW

    def utc_now(self):
        return self.now

    def monotonic(self):
        return 100.0

    def advance(self):
        self.now = (datetime.fromisoformat(self.now.replace("Z", "+00:00")) + timedelta(hours=1)).isoformat()


@pytest.fixture
def client_store(store):
    root, homes = store
    owner = next(row for row in read_shared_payload(root)["entries"][0]["audiences"] if row["kind"] == "owner_private")
    for host in ("workbuddy", "dsh"):
        home = root.parent / f"TEST-{host}-home"
        attach_shared_record(
            root,
            client_entry_record(
                host=host,
                home=home,
                entry_id=f"agent-{host}",
                display_name="agent1",
                attached_at=NOW,
                allowed_scope_ids=owner["allowed_scope_ids"],
                writable_scope_ids=owner["writable_scope_ids"],
                capture_scope_id=owner["capture_scope_id"],
            ),
            now=NOW,
        )
        homes[host] = home
    return root, homes


def _bound(client_store, host):
    _root, homes = client_store
    config = load_shared_client(homes[host], host)
    clock = PrivateClock()
    core = MemoryCore(CoreConfig(config.to_binding()), clock=clock)
    audience = resolve_runtime_audience(config, "C:/work/app")
    ctx = trusted_context(config, audience, session_id="TEST-session-1", project_root="C:/work/app")
    owner = MemoryManagement(core, InstallationMaintenance(ctx))
    hook = CodexHookHandler(config, core=core, clock=clock)
    return hook, core, ctx, clock, owner


def _rows(core, ctx):
    with core.storage.read(ctx) as tx:
        return [tuple(row) for row in tx._check().execute("SELECT content,extra_json FROM source_events")]


@pytest.mark.parametrize("host", ["codex", "dsh"])
def test_server_hooks_report_rules_even_for_other_sessions_and_closed_intervals(client_store, host):
    hook, _core, _ctx, clock, owner = _bound(client_store, host)
    server = remote_server.RemoteServerConfig(hook.config.home, host, "127.0.0.1", 1, "0" * 64)
    body = dict(payload=dict(hook_event_name="Stop", session_id="TEST-other-session", cwd="C:/work/app"))
    try:
        answer = remote_server.handle_request(server, body)
        assert answer["private_rules"] is False
        if host == "dsh":
            assert answer["result"]["private_rules"] is False
        owner.private_on(target="session", session="TEST-unrelated")
        assert remote_server.handle_request(server, body)["private_rules"] is True
        clock.advance()
        owner.private_off(target="session", session="TEST-unrelated")
        assert remote_server.handle_request(server, body)["private_rules"] is True
        assert (
            remote_server.private_check(server, dict(session_id="TEST-other-session", times=[]))["private_rules"]
            is True
        )
    finally:
        hook.close()


def test_local_dsh_hook_answers_teach_the_native_spool_about_rule_history(client_store):
    hook, _core, _ctx, _clock, owner = _bound(client_store, "dsh")
    payload = dict(hook_event_name="Stop", session_id="TEST-other-session", cwd="C:/work/app")
    try:
        assert hook.handle_bytes(json.dumps(payload).encode())["private_rules"] is False
        owner.private_on(target="session", session="TEST-unrelated")
        assert hook.handle_bytes(json.dumps(payload).encode())["private_rules"] is True
    finally:
        hook.close()


@pytest.mark.parametrize("host", ["codex", "claude-code", "workbuddy", "dsh"])
@pytest.mark.parametrize("mode", ["full", "read-only"])
def test_hooks_drop_private_words_and_attachment_refs(client_store, host, mode):
    hook, core, ctx, _clock, owner = _bound(client_store, host)
    owner.private_on(target="project", project="c:\\WORK\\APP", mode=mode)
    base = dict(session_id="TEST-session-1", turn_id="TEST-turn", prompt_id="TEST-turn", cwd="C:/work/app")
    requests = [
        dict(
            hook_event_name="UserPromptSubmit",
            prompt="TEST private words",
            attachments=[
                dict(
                    filename="TEST-file.txt",
                    mediaType="text/plain",
                    sha256="a" * 64,
                    byte_length=12,
                    reference="artifact:TEST-private",
                )
            ],
        ),
        dict(hook_event_name="Stop", last_assistant_message="TEST private reply"),
        dict(
            hook_event_name="PostToolUse",
            tool_name="TEST-tool",
            tool_use_id="TEST-tool-1",
            tool_response="TEST private tool",
        ),
        dict(hook_event_name="Interrupt"),
        dict(hook_event_name="SessionEnd"),
    ]
    try:
        answer = hook.handle_payload({**base, **requests[0]})
        assert "not being remembered" in answer["hookSpecificOutput"]["additionalContext"]
        for request in requests[1:]:
            hook.handle_payload({**base, **request})
        assert _rows(core, ctx) == []
        with core.storage.read(ctx) as tx:
            assert tx._check().execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 0
    finally:
        hook.close()


def test_transcript_backfill_after_off_uses_each_original_instant(client_store):
    from scope_recall.adapters.clients.record_reader import RecordLines
    from scope_recall.adapters.clients.transcript import Said

    hook, core, ctx, clock, owner = _bound(client_store, "claude-code")
    owner.private_on(target="session", session=ctx.session_id)
    clock.advance()
    owner.private_off(target="session", session=ctx.session_id)
    lines = RecordLines(
        0,
        [
            (1, Said("TEST-private", "user", "TEST private backfill", NOW)),
            (2, Said("TEST-public", "user", "TEST public backfill", clock.now)),
        ],
    )
    try:
        hook.handle_payload(
            dict(hook_event_name="SessionEnd", session_id="TEST-session-1", cwd="C:/work/app"), record=lines
        )
        assert lines.through == 2
        words = str(_rows(core, ctx))
        assert "TEST private backfill" not in words
        assert "TEST public backfill" in words
    finally:
        hook.close()


def test_server_discards_late_spool_event_by_its_original_time(client_store, monkeypatch):
    hook, core, ctx, clock, owner = _bound(client_store, "codex")
    owner.private_on(target="session", session=ctx.session_id)
    clock.advance()
    owner.private_off(target="session", session=ctx.session_id)

    def from_home(*args, **kwargs):
        hook.clock = kwargs["event_clock"]
        return hook

    monkeypatch.setattr(CodexHookHandler, "from_home", from_home)
    answer = remote_server.handle_request(
        type("Config", (), {"home": hook.config.home, "host": "codex"})(),
        dict(
            observed_at=NOW,
            payload=dict(
                hook_event_name="Stop",
                session_id="TEST-session-1",
                turn_id="TEST-turn",
                cwd="C:/work/app",
                tool_use_id="TEST-tool",
                tool_name="TEST-tool",
                last_assistant_message="TEST late private tool",
            ),
        ),
    )
    assert answer["retry"] is False
    assert _rows(core, ctx) == []


@pytest.mark.parametrize("state", [None, dict(mode="full", capture_blocked=True, recall_blocked=True)])
def test_remote_full_or_unverified_sends_no_content_and_keeps_no_spool(tmp_path, monkeypatch, state):
    config = _client(tmp_path, "codex", 1)
    _cached_rules(config, True)
    post = Mock(return_value={})
    monkeypatch.setattr(remote_client, "_private_check", lambda *args: state)
    monkeypatch.setattr(remote_client, "_post", post)
    remote_client.run_hook(
        config,
        json.dumps(
            dict(
                hook_event_name="Stop",
                session_id="TEST-session-1",
                turn_id="TEST-turn",
                tool_use_id="TEST-tool",
                tool_response="TEST private",
            )
        ).encode(),
    )
    assert post.call_count == 0
    assert list(remote_client._spool_dir(config).glob("*.json")) == []


def test_spool_flush_checks_private_state_before_sending(tmp_path, monkeypatch):
    config = _client(tmp_path, "codex", 1)
    _cached_rules(config, True)
    remote_client._spool(
        config, dict(hook_event_name="Stop", session_id="TEST-session-1", last_assistant_message="TEST private"), NOW
    )
    post = Mock(return_value={})
    monkeypatch.setattr(
        remote_client, "_private_check", lambda *args: dict(mode="full", capture_blocked=True, recall_blocked=True)
    )
    monkeypatch.setattr(remote_client, "_post", post)
    remote_client.flush_spool(config)
    assert post.call_count == 0
    assert list(remote_client._spool_dir(config).glob("*.json")) == []


def test_metadata_preflight_checks_every_record_in_bounded_batches(monkeypatch):
    checked = []

    def batch(_config, _body, times, _timeout):
        checked.extend(times)
        return dict(mode="none", capture_blocked=NOW in times, recall_blocked=False)

    monkeypatch.setattr(remote_client, "_private_batch", batch)
    lines = [[index, dict(occurred_at=f"2026-09-26T00:{index // 60:02}:{index % 60:02}Z")] for index in range(130)]
    lines.append([131, dict(occurred_at=NOW)])
    state = remote_client._private_check(
        {}, dict(payload=dict(session_id="TEST-session"), record=dict(lines=lines)), 10
    )
    assert state["capture_blocked"] is True
    assert len(checked) == 131


def test_dsh_preflight_translates_millisecond_record_times():
    moment = int(datetime.fromisoformat(NOW.replace("Z", "+00:00")).timestamp() * 1000)
    times = remote_client._private_times(
        dict(payload=dict(record=[dict(id="TEST-line", role="user", text="TEST private", time=moment)])), None
    )
    assert times and datetime.fromisoformat(times[0].replace("Z", "+00:00")).timestamp() * 1000 == moment


def test_read_only_remote_sends_only_a_question_and_keeps_no_spool(tmp_path, monkeypatch):
    config = _client(tmp_path, "codex", 1)
    _cached_rules(config, True)
    post = Mock(return_value={"retry": True})
    monkeypatch.setattr(
        remote_client,
        "_private_check",
        lambda *args: dict(mode="read-only", capture_blocked=True, recall_blocked=False),
    )
    monkeypatch.setattr(remote_client, "_post", post)
    remote_client.run_hook(
        config,
        json.dumps(
            dict(
                hook_event_name="UserPromptSubmit",
                session_id="TEST-session-1",
                turn_id="TEST-turn",
                prompt="TEST recall question",
                attachments=[dict(svg="TEST private attachment")],
            )
        ).encode(),
    )
    assert post.call_count == 1
    body = post.call_args.args[1]
    assert body["payload"]["prompt"] == "TEST recall question"
    assert "attachments" not in body["payload"]
    assert list(remote_client._spool_dir(config).glob("*.json")) == []


def test_mcp_project_full_stops_recall_and_read_tools_before_outbound(installed):
    from types import SimpleNamespace

    from scope_recall.adapters.clients.mcp_server import CodexMCPServer
    from scope_recall.contracts import ContractError, TrustedContext

    config, core, clock, root = installed
    ctx = TrustedContext(config.to_binding(), "TEST-session-1", config.scope_ids, "human_direct")
    owner = MemoryManagement(core, InstallationMaintenance(ctx))
    owner.private_on(target="project", project=str(root))
    server = CodexMCPServer(config, workspace=root, core=core)
    request = SimpleNamespace(request_context=SimpleNamespace(meta=None))
    vectors = Mock(side_effect=AssertionError("TEST private tool must not embed"))
    core.recall_pipeline.search = vectors
    answer = server.recall(request, "1.1", "TEST question", "current", 6)
    assert answer["result"]["items"] == []
    assert vectors.call_count == 0
    for method in ("profile", "entity", "trace"):
        with pytest.raises(ContractError) as refused:
            extra = {"action": "probe"} if method == "entity" else {}
            getattr(server, method)(request, "1.1", "TEST-subject", **extra)
        assert refused.value.field == "private_mode"


def test_preflight_batches_share_the_receive_protocol_clock_correction(client_store, monkeypatch):
    hook, core, ctx, _clock, _owner_api = _bound(client_store, "codex")
    server = remote_server.RemoteServerConfig(
        home=hook.config.home, host="codex", listen="127.0.0.1", port=1, token_sha256="0" * 64
    )
    at = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
    monkeypatch.setattr(remote_server, "_now", lambda: at)
    seen = []
    monkeypatch.setattr(MemoryCore, "private_check", lambda self, context, times: seen.extend(times) or {})
    raw = (at + timedelta(seconds=90)).isoformat()
    latest = (at + timedelta(seconds=150)).isoformat()
    try:
        first = remote_server.private_check(server, dict(session_id="TEST-clock-session", times=[], sent_at=latest))
        first["clock"]["received_at"] = latest
        remote_server.private_check(
            server, dict(session_id="TEST-clock-session", times=[raw, latest], clock=first["clock"])
        )
        assert [datetime.fromisoformat(value["earliest"].replace("Z", "+00:00")) for value in seen] == [
            at - timedelta(seconds=60),
            at,
        ]
        assert all(value["earliest"] == value["latest"] for value in seen)
    finally:
        hook.close()


def test_local_hook_uses_child_cwd_for_private_rules(installed):
    from scope_recall.contracts import TrustedContext

    config, core, clock, root = installed
    child = root / "TEST-private"
    child.mkdir()
    ctx = TrustedContext(config.to_binding(), "TEST-session-1", config.scope_ids, "human_direct")
    owner = MemoryManagement(core, InstallationMaintenance(ctx))
    owner.private_on(target="project", project=str(child))
    hook = CodexHookHandler(config, core=core, clock=clock)
    try:
        answer = hook.handle_payload(
            dict(
                hook_event_name="UserPromptSubmit",
                session_id=ctx.session_id,
                turn_id="TEST-turn",
                prompt="TEST private body",
                cwd=str(child),
            )
        )
        assert "not being remembered" in answer["hookSpecificOutput"]["additionalContext"]
        assert _rows(core, ctx) == []
    finally:
        hook.close()


def test_remote_rechecks_after_spooling_before_content_send(tmp_path, monkeypatch):
    config = _client(tmp_path, "codex", 1)
    _cached_rules(config, True)
    states = iter(
        [
            dict(mode="none", capture_blocked=False, recall_blocked=False),
            dict(mode="full", capture_blocked=True, recall_blocked=True),
        ]
    )
    monkeypatch.setattr(remote_client, "_private_check", lambda *args: next(states))
    post = Mock(return_value={})
    monkeypatch.setattr(remote_client, "_post", post)
    remote_client.run_hook(
        config,
        json.dumps(
            dict(hook_event_name="Stop", session_id="TEST-session-1", last_assistant_message="TEST body")
        ).encode(),
    )
    assert post.call_count == 0
    assert list(remote_client._spool_dir(config).glob("*.json")) == []


def test_resident_warm_embedding_obeys_global_full(client_store, monkeypatch):
    from types import SimpleNamespace

    hook, core, ctx, _clock, owner = _bound(client_store, "codex")
    warm = Mock()
    hook._host_runtime = SimpleNamespace(_runtime=SimpleNamespace(warm_query_embedding=warm))
    owner.private_on(target="global")
    hook.warm_embedding(1)
    assert warm.call_count == 0
    from scope_recall.runtime.instance import RuntimeInstance, _LazyVectorPort

    embed = Mock(return_value=[1.0])
    monkeypatch.setattr(_LazyVectorPort, "_query_embedder", lambda self: embed)
    runtime = SimpleNamespace(
        core=core, _ensure_open=lambda: None, config=SimpleNamespace(vector=object(), context=lambda: ctx)
    )
    assert RuntimeInstance.warm_query_embedding(runtime, 1) is False
    assert embed.call_count == 0


@pytest.mark.parametrize("hours", [-2, 2])
def test_remote_calibrates_both_directions_and_replays_the_same_time(client_store, monkeypatch, hours):
    hook, core, ctx, clock, owner = _bound(client_store, "codex")
    owner.private_on(target="session", session=ctx.session_id)
    server_at = datetime.fromisoformat(clock.now.replace("Z", "+00:00")) + timedelta(minutes=30)
    client_at = server_at + timedelta(hours=hours)
    monkeypatch.setattr(remote_server, "_now", lambda: server_at)
    original = MemoryCore.private_check
    monkeypatch.setattr(MemoryCore, "private_check", lambda self, context, times: original(core, context, times))
    config = remote_server.RemoteServerConfig(hook.config.home, "codex", "127.0.0.1", 1, "0" * 64)
    clock.advance()
    owner.private_off(target="session", session=ctx.session_id)
    first = remote_server.private_check(
        config, dict(session_id="TEST-session-1", times=[client_at.isoformat()], sent_at=client_at.isoformat())
    )
    assert first["capture_blocked"] is True
    calibration = first.get("clock")
    assert isinstance(calibration, dict)
    calibration["received_at"] = client_at.isoformat()
    monkeypatch.setattr(remote_server, "_now", lambda: server_at + timedelta(days=1))
    replay = remote_server.private_check(
        config, dict(session_id="TEST-session-1", times=[client_at.isoformat()], clock=calibration)
    )
    assert replay["capture_blocked"] is True
    observed, _record, _lead = remote_server.client_times_and_lead(
        dict(observed_at=client_at.isoformat(), clock=calibration)
    )
    assert datetime.fromisoformat(observed.replace("Z", "+00:00")) == server_at
    hook.close()


def test_remote_mixed_record_drops_private_lines_and_advances_cursor(tmp_path, monkeypatch):
    from scope_recall.adapters.clients import transcript

    config = _client(tmp_path, "claude-code", 1)
    _cached_rules(config, True)
    public = "2026-09-26T14:00:00Z"
    lines = [
        [10, transcript.said_to_wire(transcript.Said("TEST-1", "user", "TEST private", NOW))],
        [20, transcript.said_to_wire(transcript.Said("TEST-2", "user", "TEST public", public))],
    ]
    cursor = Mock()
    monkeypatch.setattr(remote_client, "_record_part", lambda *args: (dict(start=0, lines=lines), cursor))
    monkeypatch.setattr(remote_client, "_now", lambda: public)

    def batch(_config, _body, times, _timeout):
        return dict(
            mode="none",
            capture_blocked=NOW in times,
            recall_blocked=False,
            decisions=[stamp == NOW for stamp in times],
            full_decisions=[stamp == NOW for stamp in times],
        )

    monkeypatch.setattr(remote_client, "_private_batch", batch)
    sent = []
    monkeypatch.setattr(
        remote_client, "_post", lambda _config, body, _timeout: sent.append(body) or dict(through=20, result={})
    )
    remote_client.run_hook(config, json.dumps(dict(hook_event_name="SessionEnd", session_id="TEST-session-1")).encode())
    assert len(sent) == 1
    assert "TEST private" not in json.dumps(sent)
    assert sent[0]["record"]["lines"][0] == [10, None]
    assert sent[0]["record"]["lines"][1][1]["text"] == "TEST public"
    cursor.save.assert_called_once_with(20)


def test_junction_and_real_directory_share_the_rule_identity(installed):
    import os
    import subprocess

    from scope_recall.contracts import TrustedContext

    config, core, clock, root = installed
    real, alias = root / "TEST-real", root / "TEST-alias"
    real.mkdir()
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(real)], check=True, capture_output=True)
    else:
        alias.symlink_to(real, target_is_directory=True)
    ctx = TrustedContext(config.to_binding(), "TEST-session-1", config.scope_ids, "human_direct")
    owner = MemoryManagement(core, InstallationMaintenance(ctx))
    hook = CodexHookHandler(config, core=core, clock=clock)
    try:
        for rule, cwd in [(alias, real), (real, alias)]:
            owner.private_on(target="project", project=str(rule))
            answer = hook.handle_payload(
                dict(
                    hook_event_name="UserPromptSubmit",
                    session_id=ctx.session_id,
                    turn_id=str(cwd),
                    prompt="TEST private body",
                    cwd=str(cwd),
                )
            )
            assert "not being remembered" in answer["hookSpecificOutput"]["additionalContext"]
            assert _rows(core, ctx) == []
            owner.private_off(target="project", project=str(real))
    finally:
        hook.close()


def test_remote_clock_range_refuses_a_private_event_delayed_past_the_end(client_store, monkeypatch):
    hook, core, ctx, clock, owner = _bound(client_store, "codex")
    owner.private_on(target="session", session=ctx.session_id)
    start = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
    clock.now = (start + timedelta(seconds=10)).isoformat()
    owner.private_off(target="session", session=ctx.session_id)
    sent = (start + timedelta(seconds=9.9)).isoformat()
    received = (start + timedelta(seconds=10.1)).isoformat()
    calibration = dict(client_at=sent, server_at=received, received_at=(start + timedelta(seconds=10.3)).isoformat())
    original = MemoryCore.private_check
    monkeypatch.setattr(
        MemoryCore, "private_check", lambda self, context, times, **kwargs: original(core, context, times, **kwargs)
    )
    config = remote_server.RemoteServerConfig(hook.config.home, "codex", "127.0.0.1", 1, "0" * 64)
    try:
        result = remote_server.private_check(config, dict(session_id="TEST-session-1", times=[sent], clock=calibration))
        assert result["capture_blocked"] is True
    finally:
        hook.close()


def test_remote_historical_record_cannot_borrow_the_upload_clock(client_store, monkeypatch):
    hook, core, ctx, clock, owner = _bound(client_store, "dsh")
    owner.private_on(target="session", session=ctx.session_id)
    clock.advance()
    owner.private_off(target="session", session=ctx.session_id)
    upload = clock.now
    calibration = dict(client_at=upload, server_at=upload, received_at=upload)
    original = MemoryCore.private_check
    monkeypatch.setattr(
        MemoryCore, "private_check", lambda self, context, times, **kwargs: original(core, context, times, **kwargs)
    )
    server = remote_server.RemoteServerConfig(hook.config.home, "dsh", "127.0.0.1", 1, "0" * 64)
    old = (datetime.fromisoformat(NOW.replace("Z", "+00:00")) - timedelta(hours=2)).isoformat()
    body = dict(
        observed_at=upload,
        clock=calibration,
        payload=dict(
            hook_event_name="Stop",
            session_id="TEST-session-1",
            cwd="C:/work/app",
            record=[
                dict(
                    id="TEST-private",
                    role="assistant",
                    text="TEST private history",
                    time=datetime.fromisoformat(old).timestamp() * 1000,
                )
            ],
        ),
    )
    try:
        metadata = remote_client.private_metadata(body)
        state = remote_server.private_check(server, metadata)
        assert state["capture_blocked"] is True
        state["event_decisions"] = dict(zip(metadata["times"], state["decisions"], strict=True))
        filtered, _state = remote_client.eligible_body(body, state)
        assert "TEST private history" not in json.dumps(filtered)
    finally:
        hook.close()


def test_private_dsh_reply_decision_removes_every_copy():
    stamp = datetime.fromisoformat(NOW.replace("Z", "+00:00")).timestamp() * 1000
    row = dict(id="TEST-private", role="assistant", text="TEST duplicate private reply", time=stamp)
    body = dict(
        observed_at="2026-09-26T14:00:00Z",
        payload=dict(hook_event_name="Stop", record=[row], reply_id=row["id"], last_assistant_message=row["text"]),
    )
    moment = remote_client._private_times(body, None)[0]
    filtered, _state = remote_client.eligible_body(
        body, dict(mode="none", event_decisions={moment: True, body["observed_at"]: False})
    )
    assert "TEST duplicate private reply" not in json.dumps(filtered)


def test_metadata_wire_has_only_the_protocol_fields(tmp_path, monkeypatch):
    from types import SimpleNamespace

    config = _client(tmp_path, "codex", 1)
    requests = []

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            pass

        def request(self, _method, _path, **kwargs):
            requests.append(json.loads(kwargs["body"]))

        def getresponse(self):
            return SimpleNamespace(
                status=200, read=lambda: b'{"mode":"none","capture_blocked":false,"recall_blocked":false}'
            )

        def close(self):
            pass

    monkeypatch.setattr(remote_client.http.client, "HTTPConnection", Connection)
    remote_client._private_batch(config, dict(payload=dict(session_id="TEST-session-1")), [NOW], 1)
    assert len(requests) == 1
    assert set(requests[0]) == {
        "session_id",
        "project",
        "project_unverified",
        "sent_at",
        "rule_marker",
        "clock",
        "times",
        "clocks",
    }


def test_remote_content_connection_rechecks_privacy_after_connect(tmp_path, monkeypatch):
    from types import SimpleNamespace

    config = _client(tmp_path, "codex", 1)
    _cached_rules(config, True)
    sent = []

    class Connection:
        def __init__(self, *args, **kwargs):
            self.sock = SimpleNamespace(settimeout=lambda value: None)

        def connect(self):
            pass

        def request(self, *args, **kwargs):
            sent.append("TEST private body")

        def close(self):
            pass

    monkeypatch.setattr(remote_client.http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(
        remote_client, "_private_check", lambda *args: dict(mode="full", capture_blocked=True, recall_blocked=True)
    )
    assert (
        remote_client._post(
            config,
            dict(
                observed_at=NOW,
                payload=dict(
                    session_id="TEST-session-1", hook_event_name="Stop", last_assistant_message="TEST private body"
                ),
            ),
            2,
        )
        is None
    )
    assert sent == []


@pytest.mark.parametrize("mode", ["none", "read-only"])
def test_server_never_recalls_an_uncalibrated_private_prompt_after_off(client_store, monkeypatch, mode):
    hook, core, ctx, clock, owner = _bound(client_store, "codex")
    owner.private_on(target="session", session=ctx.session_id)
    clock.advance()
    owner.private_off(target="session", session=ctx.session_id)
    if mode == "read-only":
        owner.private_on(target="session", session=ctx.session_id, mode=mode)
    original = MemoryCore.private_check
    monkeypatch.setattr(
        MemoryCore, "private_check", lambda self, context, times, **kwargs: original(core, context, times, **kwargs)
    )
    reached = []

    def recall(*args, **kwargs):
        reached.append("TEST query")
        raise AssertionError("TEST private prompt reached recall")

    monkeypatch.setattr(CodexHookHandler, "from_home", recall)
    server = remote_server.RemoteServerConfig(hook.config.home, "codex", "127.0.0.1", 1, "0" * 64)
    try:
        result = remote_server.handle_request(
            server,
            dict(
                observed_at=NOW,
                payload=dict(
                    hook_event_name="UserPromptSubmit",
                    session_id="TEST-session-1",
                    cwd="C:/work/app",
                    prompt="TEST private prompt",
                ),
            ),
        )
        assert result["reason"] == "private_mode"
        assert reached == []
    finally:
        hook.close()


@pytest.mark.parametrize("private", [True, False])
def test_read_only_sends_only_questions_that_were_never_full_private(client_store, tmp_path, monkeypatch, private):
    hook, core, ctx, clock, owner = _bound(client_store, "codex")
    owner.private_on(target="session", session=ctx.session_id)
    clock.advance()
    owner.private_off(target="session", session=ctx.session_id)
    owner.private_on(target="session", session=ctx.session_id, mode="read-only")
    original = MemoryCore.private_check
    monkeypatch.setattr(
        MemoryCore, "private_check", lambda self, context, times, **kwargs: original(core, context, times, **kwargs)
    )
    server = remote_server.RemoteServerConfig(hook.config.home, "codex", "127.0.0.1", 1, "0" * 64)
    monkeypatch.setattr(
        remote_client,
        "_private_batch",
        lambda config, body, times, seconds: remote_server.private_check(
            server, remote_client.private_metadata(body, times)
        ),
    )
    sent = []
    monkeypatch.setattr(remote_client, "_post", lambda config, body, seconds: sent.append(body) or dict(result={}))
    at = NOW if private else clock.now
    body = dict(
        observed_at=at,
        payload=dict(
            hook_event_name="UserPromptSubmit", session_id="TEST-session-1", cwd="C:/work/app", prompt="TEST question"
        ),
    )
    if not private:
        body["clock"] = dict(client_at=at, server_at=at, received_at=at)
    try:
        client = _client(tmp_path, "codex", 1)
        _cached_rules(client, True)
        remote_client._send_checked(client, body, remote_client.time.monotonic() + 10)
        assert len(sent) == (0 if private else 1)
    finally:
        hook.close()
