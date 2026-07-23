"""Provider shutdown contracts for background journal maintenance.

Shutdown must quiesce new work and keep shared resources alive when a worker
cannot acknowledge the stop request within the caller's deadline.
"""
from __future__ import annotations

import threading

import pytest

import scope_recall.provider as provider_module
from scope_recall.models import RuntimeScope
from scope_recall.provider import ScopeRecallMemoryProvider


class _Closable:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _provider(tmp_path) -> ScopeRecallMemoryProvider:
    provider = ScopeRecallMemoryProvider()
    provider._hermes_home = tmp_path
    provider._scope = RuntimeScope(agent_context="primary")
    provider._config = {
        "journal": {
            "enabled": True,
            "background_digest_enabled": True,
            "background_digest_synchronous": True,
            "digest_interval_hours": 1,
            "extractor": "heuristic",
        }
    }
    # Keep the regression behavioral against pre-fix implementations that do
    # not yet own this event.
    provider._shutdown_requested = threading.Event()
    return provider


def test_shutdown_request_blocks_new_background_digest(tmp_path, monkeypatch) -> None:
    provider = _provider(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        provider_module,
        "run_journal_digest",
        lambda **_kwargs: calls.append("digest") or {"ok": True},
    )

    provider._shutdown_requested.set()
    provider._maybe_start_background_journal_digest()

    assert calls == []
    assert provider._last_journal_digest_status == "never_run"


def test_synchronous_digest_is_registered_as_in_flight(
    tmp_path,
    monkeypatch,
) -> None:
    provider = _provider(tmp_path)
    started = threading.Event()
    released = threading.Event()

    def blocked_run(**_kwargs):
        started.set()
        released.wait(timeout=2.0)
        return {"ok": True}

    monkeypatch.setattr(provider_module, "run_journal_digest", blocked_run)
    worker = threading.Thread(target=provider._maybe_start_background_journal_digest)
    worker.start()
    assert started.wait(timeout=1.0)

    assert provider._journal_digest_thread is worker
    provider._shutdown_requested.set()
    released.set()
    worker.join(timeout=1.0)

    assert worker.is_alive() is False
    assert provider._journal_digest_thread is None


def test_shutdown_timeout_keeps_connections_open_for_safe_retry(
    tmp_path,
    monkeypatch,
) -> None:
    provider = _provider(tmp_path)
    connection = _Closable()
    vector_store = _Closable()
    provider._conn = connection
    provider._vector_store = vector_store
    released = threading.Event()
    started = threading.Event()

    def blocked_digest() -> None:
        started.set()
        released.wait(timeout=2.0)

    worker = threading.Thread(target=blocked_digest, name="blocked-journal-digest")
    provider._journal_digest_thread = worker
    worker.start()
    assert started.wait(timeout=1.0)

    unregister_calls: list[str] = []
    monkeypatch.setattr(provider_module, "shutdown_writer", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        provider,
        "_unregister_provider_instance",
        lambda: unregister_calls.append("unregistered"),
    )

    with pytest.raises(RuntimeError, match="journal digest did not acknowledge"):
        provider.shutdown(timeout=0.01)

    assert connection.closed is False
    assert vector_store.closed is False
    assert provider._conn is connection
    assert provider._vector_store is vector_store
    assert unregister_calls == []

    released.set()
    provider.shutdown(timeout=1.0)
    assert connection.closed is True
    assert vector_store.closed is True
    assert provider._conn is None
    assert provider._vector_store is None
    assert unregister_calls == ["unregistered"]


def test_shutdown_during_digest_skips_post_digest_promotion(
    tmp_path,
    monkeypatch,
) -> None:
    provider = _provider(tmp_path)
    started = threading.Event()
    released = threading.Event()
    promotions: list[str] = []

    def blocked_run(**_kwargs):
        started.set()
        released.wait(timeout=2.0)
        return {"ok": True}

    monkeypatch.setattr(provider_module, "run_journal_digest", blocked_run)
    monkeypatch.setattr(
        provider,
        "_maybe_run_auto_experience_promotion",
        lambda *, trigger: promotions.append(trigger),
    )

    worker = threading.Thread(
        target=provider._run_background_journal_digest,
        args=(dict(provider._config["journal"]),),
    )
    provider._journal_digest_thread = worker
    worker.start()
    assert started.wait(timeout=1.0)

    provider._shutdown_requested.set()
    released.set()
    worker.join(timeout=1.0)

    assert worker.is_alive() is False
    assert promotions == []
