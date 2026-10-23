"""Private capture, late records and metadata-only remote preflight on the client boundaries."""

import json
from contextlib import closing
from datetime import datetime, timedelta
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
    latest = []

    def batch(_config, _body, times, _timeout):
        latest.append(_body["private_latest_at"])
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
    assert latest == [NOW, NOW, NOW]


def test_dsh_preflight_translates_millisecond_record_times():
    moment = int(datetime.fromisoformat(NOW.replace("Z", "+00:00")).timestamp() * 1000)
    times = remote_client._private_times(
        dict(payload=dict(record=[dict(id="TEST-line", role="user", text="TEST private", time=moment)])), None
    )
    assert times and datetime.fromisoformat(times[0].replace("Z", "+00:00")).timestamp() * 1000 == moment


def test_read_only_remote_sends_only_a_question_and_keeps_no_spool(tmp_path, monkeypatch):
    config = _client(tmp_path, "codex", 1)
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
        remote_server.private_check(server, dict(session_id="TEST-clock-session", times=[raw], latest_at=latest))
        remote_server.private_check(server, dict(session_id="TEST-clock-session", times=[latest], latest_at=latest))
        assert [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in seen] == [
            at - timedelta(seconds=60),
            at,
        ]
    finally:
        hook.close()
