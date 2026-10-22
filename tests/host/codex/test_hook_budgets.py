"""Hooks defer work when a slow store or runtime consumes their remaining time."""

from __future__ import annotations

import io
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from scope_recall.adapters.clients import CodexHookHandler, hook_entry, install_codex_scope_recall
from scope_recall.adapters.clients import handler as handler_module
from scope_recall.adapters.clients.handler import _TOTAL_BUDGET_S
from scope_recall.core.capture_inbox import replay_inbox

from tests.host.codex.test_runtime_wiring import _runtime_payload, _write_runtime_config
from tests.store_clock import HOOK_MARGIN_SECONDS, assert_hook_budget


@pytest.fixture
def codex_install(tmp_path):
    root, project = tmp_path / "TEST-codex", tmp_path / "TEST-project"
    project.mkdir()
    config, core = install_codex_scope_recall(root, project_root=project, test_mode=True)
    path = _write_runtime_config(
        root / "trusted-runtime.json",
        _runtime_payload(config.to_binding(), session_id="TEST-session", allowed_scope_ids=config.scope_ids),
    )
    return config, core, project, path


@pytest.mark.parametrize("per_operation", [1.2, 1.5, 2.0])
def test_session_end_leaves_commit_in_durable_inbox(codex_install, metered_store, per_operation):
    assert_hook_budget(2 + HOOK_MARGIN_SECONDS, 2)
    with pytest.raises(AssertionError):
        assert_hook_budget(2 + HOOK_MARGIN_SECONDS + 0.001, 2)
    config, core, project, path = codex_install
    clock = metered_store(per_operation)
    handler = CodexHookHandler.from_config_path(str(config.config_path), trusted_runtime_config_path=str(path))
    try:
        with patch.object(handler, "_wake_after_capture"):
            started, before = clock(), clock.operations
            handler.handle_payload(
                {"hook_event_name": "SessionEnd", "session_id": "TEST-session", "cwd": str(project), "reason": "exit"}
            )
            elapsed = clock() - started
        # Only the inbox transaction runs. JSON dispatch and diagnostics use the real clock.
        assert elapsed <= _TOTAL_BUDGET_S + 0.1, (per_operation, elapsed)
        assert clock.operations - before == 1
        assert handler.call.queued and not handler.call.persisted
        context = handler.context(handler.audience_of({"cwd": str(project)}), "TEST-session", "host_generated")
        with core.storage.read(context) as tx:
            assert tx._check().execute("SELECT count(*) FROM capture_inbox").fetchone()[0] == 1
            assert tx.status().sources == 0
        receipts = replay_inbox(
            core.storage, core.clock, context, authorize=lambda scope: context.allowed_scope_ids, remaining_seconds=30
        )
        assert receipts and all(receipt.durability == "persisted" for receipt in receipts)
    finally:
        handler.close()


@pytest.mark.parametrize("stage", ["attach", "launch"])
def test_runtime_wait_uses_only_hook_time_left(codex_install, monkeypatch, metered_store, stage):
    config, core, project, _path = codex_install
    clock = metered_store(0.01)
    entered, released, closed = threading.Event(), threading.Event(), threading.Event()
    runtime = SimpleNamespace(core=core, configured=True, capability_gaps=(), close=lambda **kw: closed.set())

    def slow():
        entered.set()
        released.wait(1.0)

    def attach(**kwargs):
        if stage == "attach":
            slow()
        return runtime

    def launch(**kwargs):
        if stage == "launch":
            slow()
        return ()

    runtime.maybe_launch_bounded_worker = launch
    monkeypatch.setattr(handler_module, "attach_trusted_host_runtime", attach)
    handler = CodexHookHandler(config)
    handler.call.queued = True
    audience = handler.audience_of({"cwd": str(project)})
    started = clock()
    clock.advance(_TOTAL_BUDGET_S - 0.35)
    try:
        handler._wake_after_capture("TEST-session", audience, started + _TOTAL_BUDGET_S)
        elapsed = clock() - started
        assert entered.is_set()
        # Thread dispatch, the timed wait's scheduling and bookkeeping use the real clock.
        assert elapsed <= _TOTAL_BUDGET_S + 0.1, (stage, elapsed)
        handler.close()
    finally:
        released.set()
        handler.close()
    assert closed.wait(2), "a runtime finishing after the hook closes releases its resources"
    handler = CodexHookHandler(config)
    handler.call.queued = True

    def forbidden(**kwargs):
        pytest.fail("runtime work began without enough hook time")

    monkeypatch.setattr(handler_module, "attach_trusted_host_runtime", forbidden)
    try:
        for remaining in (0, 0.05):
            handler._wake_after_capture(
                "TEST-session", handler.audience_of({"cwd": str(project)}), time.monotonic() + remaining
            )
    finally:
        handler.close()


def _entry(monkeypatch, config, raw: bytes, closed: list, made: list, *, handle=None) -> int:
    """Run the hook entry and record handlers it built and ``close`` calls it made."""
    real_close = CodexHookHandler.close
    real_from = CodexHookHandler.from_config_path.__func__

    def spy(self):
        closed.append(self)
        real_close(self)

    def capturing(cls, *args, **kwargs):
        handler = real_from(cls, *args, **kwargs)
        made.append(handler)
        return handler

    monkeypatch.setattr(CodexHookHandler, "close", spy)
    monkeypatch.setattr(CodexHookHandler, "from_config_path", classmethod(capturing))
    if handle is not None:
        monkeypatch.setattr(CodexHookHandler, "handle_bytes", handle)
    monkeypatch.setattr(hook_entry.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw)))
    try:
        return hook_entry.main(["--config", str(config.config_path)])
    finally:
        for handler in made:
            if handler not in closed:
                real_close(handler)


def test_hook_entry_closes_handler_after_a_handled_event(codex_install, monkeypatch, capsys):
    config, _core, project, _path = codex_install
    closed, made = [], []
    raw = json.dumps(
        {"hook_event_name": "SessionEnd", "session_id": "TEST-session", "cwd": str(project), "reason": "exit"}
    ).encode()
    assert _entry(monkeypatch, config, raw, closed, made) == 0
    assert closed and closed == made
    assert capsys.readouterr().out.strip().startswith("{")


def test_hook_entry_closes_handler_when_handling_raises(codex_install, monkeypatch):
    config, _core, _project, _path = codex_install
    closed, made = [], []

    def exploded(self, raw):
        raise RuntimeError("TEST entry failed")

    with pytest.raises(RuntimeError, match="TEST entry failed"):
        _entry(monkeypatch, config, b"{}", closed, made, handle=exploded)
    assert closed and closed == made


def test_hook_entry_closes_handler_when_input_is_too_large(codex_install, monkeypatch, capsys):
    config, _core, _project, _path = codex_install
    closed, made = [], []
    assert _entry(monkeypatch, config, b"x" * 65537, closed, made) == 0
    assert closed and closed == made
    assert "input_too_large" in capsys.readouterr().err
