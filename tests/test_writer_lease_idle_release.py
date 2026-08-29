"""Idle writer-lease release (#58).

A writer with no user-turn activity beyond ``writer_lease.idle_release_seconds``
voluntarily quiesces its capture writer, closes the write pager, releases the
cross-process OS lease, and demotes itself to the read-only runtime so an
active peer can take over via the existing on-turn promotion probe. Recent
activity suppresses the release, ``0`` disables it, and a later turn
re-promotes the runtime. The release must be real at the OS level: a separate
process must acquire the lease afterwards.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from plugin_source import assert_same_source
from plugins.memory import load_memory_provider

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKSPACE_PROVIDER = (_REPO_ROOT / "provider.py").resolve()


def _write_config(hermes_home: Path, payload: dict) -> None:
    path = hermes_home / "scope-recall" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _provider():
    provider = load_memory_provider("scope-recall")
    module = inspect.getmodule(type(provider))
    assert module is not None
    assert_same_source(
        Path(module.__file__), _WORKSPACE_PROVIDER, label="runtime provider"
    )
    return provider


def _initialize(provider, hermes_home: Path, session: str) -> None:
    provider.initialize(
        session,
        hermes_home=str(hermes_home),
        platform="cli",
        user_id="lease-user",
        chat_id="lease-chat",
        agent_identity="tester",
        agent_workspace="hermes",
        agent_context="primary",
    )


def _lifecycle_module(provider):
    """Resolve the process_lifecycle module bound to this provider's namespace."""

    module = inspect.getmodule(type(provider))
    assert module is not None
    name = f"{module.__name__.rsplit('.', 1)[0]}._internal.runtime.process_lifecycle"
    lifecycle = sys.modules.get(name)
    if lifecycle is None:
        lifecycle = __import__(name, fromlist=["demote_writer_to_reader"])
    return lifecycle


def _child_acquire_status(storage_dir: Path) -> str:
    """Ask a separate process whether it can take the writer lease."""

    child_script = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        sys.path.insert(0, {str(_REPO_ROOT)!r})
        from writer_lease import TruthWriterLease
        lease = TruthWriterLease(Path({str(storage_dir)!r}), role="probe")
        result = lease.acquire()
        print("STATUS:" + result["status"], flush=True)
        if result["status"] == "acquired":
            lease.release()
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_script],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = (child.stdout.readline() or "").strip()
        child.wait(timeout=10)
        return line
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def _wait_for(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _shutdown(provider) -> None:
    try:
        provider.shutdown(timeout=5.0)
    except Exception:
        pass


def test_idle_writer_voluntarily_releases_lease_and_demotes(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {"vector": {"enabled": False}, "writer_lease": {"idle_release_seconds": 1}},
    )
    provider = _provider()
    _initialize(provider, tmp_path, "idle-release")
    try:
        assert provider._truth_writer_role == "owner"
        lifecycle = _lifecycle_module(provider)
        assert provider.flush(timeout=5.0)
        provider._last_writer_activity = time.monotonic() - 10.0
        lifecycle.maybe_idle_release_writer_lease(provider)
        assert _wait_for(
            lambda: not getattr(provider, "_idle_release_in_progress", False)
        ), "idle-release thread did not finish"
        assert provider._truth_writer_role == "reader"
        assert provider._truth_writer_lease is None
        # The release must be real at the OS level, not just role bookkeeping.
        assert _child_acquire_status(tmp_path / "scope-recall") == "STATUS:acquired"
    finally:
        _shutdown(provider)


def test_recent_activity_suppresses_idle_release(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {"vector": {"enabled": False}, "writer_lease": {"idle_release_seconds": 60}},
    )
    provider = _provider()
    _initialize(provider, tmp_path, "idle-suppressed")
    try:
        assert provider._truth_writer_role == "owner"
        lifecycle = _lifecycle_module(provider)
        provider.on_turn_start(1, "hello")
        lifecycle.maybe_idle_release_writer_lease(provider)
        assert not getattr(provider, "_idle_release_in_progress", False)
        time.sleep(0.2)
        assert provider._truth_writer_role == "owner"
        assert _child_acquire_status(tmp_path / "scope-recall") == "STATUS:busy"
    finally:
        _shutdown(provider)


def test_zero_idle_release_disables_the_probe(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {"vector": {"enabled": False}, "writer_lease": {"idle_release_seconds": 0}},
    )
    provider = _provider()
    _initialize(provider, tmp_path, "idle-disabled")
    try:
        assert provider._truth_writer_role == "owner"
        lifecycle = _lifecycle_module(provider)
        provider._last_writer_activity = time.monotonic() - 100000.0
        lifecycle.maybe_idle_release_writer_lease(provider)
        assert not getattr(provider, "_idle_release_in_progress", False)
        time.sleep(0.2)
        assert provider._truth_writer_role == "owner"
        assert _child_acquire_status(tmp_path / "scope-recall") == "STATUS:busy"
    finally:
        _shutdown(provider)


def test_demote_refuses_while_foreground_busy(tmp_path: Path) -> None:
    _write_config(tmp_path, {"vector": {"enabled": False}})
    provider = _provider()
    _initialize(provider, tmp_path, "busy-guard")
    try:
        assert provider._truth_writer_role == "owner"
        lifecycle = _lifecycle_module(provider)
        provider._last_writer_activity = time.monotonic() - 100000.0
        provider._foreground_busy_count = 1
        try:
            assert (
                lifecycle.demote_writer_to_reader(provider, idle_seconds=1.0) is False
            )
            assert provider._truth_writer_role == "owner"
        finally:
            provider._foreground_busy_count = 0
    finally:
        _shutdown(provider)


def test_demoted_runtime_re_promotes_on_next_turn(tmp_path: Path) -> None:
    _write_config(tmp_path, {"vector": {"enabled": False}})
    provider = _provider()
    _initialize(provider, tmp_path, "re-promote")
    try:
        assert provider._truth_writer_role == "owner"
        lifecycle = _lifecycle_module(provider)
        assert provider.flush(timeout=5.0)
        provider._last_writer_activity = time.monotonic() - 100000.0
        assert lifecycle.demote_writer_to_reader(provider, idle_seconds=1.0) is True
        assert provider._truth_writer_role == "reader"
        provider.on_turn_start(2, "hello again")
        assert provider._truth_writer_role == "owner"
        thread = getattr(provider, "_writer_thread", None)
        assert thread is not None and thread.is_alive()
        assert _child_acquire_status(tmp_path / "scope-recall") == "STATUS:busy"
    finally:
        _shutdown(provider)


def test_demote_refuses_while_digest_running(tmp_path: Path) -> None:
    _write_config(tmp_path, {"vector": {"enabled": False}})
    provider = _provider()
    _initialize(provider, tmp_path, "digest-guard")
    fake_digest = None
    try:
        assert provider._truth_writer_role == "owner"
        lifecycle = _lifecycle_module(provider)
        provider._last_writer_activity = time.monotonic() - 100000.0
        stop = threading.Event()

        def _fake_digest() -> None:
            stop.wait(timeout=30.0)

        fake_digest = threading.Thread(target=_fake_digest, daemon=True)
        fake_digest.start()
        provider._background_work().thread = fake_digest
        assert (
            lifecycle.demote_writer_to_reader(provider, idle_seconds=1.0) is False
        )
        assert provider._truth_writer_role == "owner"
        assert _child_acquire_status(tmp_path / "scope-recall") == "STATUS:busy"
    finally:
        if fake_digest is not None:
            stop.set()
            fake_digest.join(timeout=5.0)
        if provider._background_work() is not None:
            provider._background_work().thread = None
        _shutdown(provider)


def test_default_config_leaves_release_disabled(tmp_path: Path) -> None:
    # No writer_lease section at all: the feature must stay opt-in.
    _write_config(tmp_path, {"vector": {"enabled": False}})
    provider = _provider()
    _initialize(provider, tmp_path, "default-disabled")
    try:
        assert provider._truth_writer_role == "owner"
        lifecycle = _lifecycle_module(provider)
        provider._last_writer_activity = time.monotonic() - 100000.0
        lifecycle.maybe_idle_release_writer_lease(provider)
        assert not getattr(provider, "_idle_release_in_progress", False)
        time.sleep(0.2)
        assert provider._truth_writer_role == "owner"
    finally:
        _shutdown(provider)


def test_idle_release_config_is_clamped(tmp_path: Path) -> None:
    _write_config(tmp_path, {"vector": {"enabled": False}})
    provider = _provider()
    _initialize(provider, tmp_path, "clamp-check")
    try:
        lifecycle = _lifecycle_module(provider)
        provider._config["writer_lease"] = {"idle_release_seconds": "junk"}
        assert lifecycle.idle_writer_lease_release_seconds(provider) == 0.0
        provider._config["writer_lease"] = {"idle_release_seconds": -5}
        assert lifecycle.idle_writer_lease_release_seconds(provider) == 0.0
        provider._config["writer_lease"] = {"idle_release_seconds": 999999}
        assert (
            lifecycle.idle_writer_lease_release_seconds(provider)
            == lifecycle.MAX_IDLE_WRITER_LEASE_SECONDS
        )
        provider._config["writer_lease"] = {"idle_release_seconds": 1800}
        assert lifecycle.idle_writer_lease_release_seconds(provider) == 1800.0
    finally:
        _shutdown(provider)
