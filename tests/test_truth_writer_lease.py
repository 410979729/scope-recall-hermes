"""Cross-process truth writer lease tests (issue #39 corruption class).

Three live corruption incidents shared one mechanism family: two *processes*
holding read-write SQLite connections to the same truth database. These tests
pin the contract: cross-process writers are exclusive (later processes degrade
to read-only recall and can promote once the lease frees), while same-process
peer providers keep sharing one refcounted process lease because that pattern
is supported by dedicated lock-recovery machinery (issues #25/#43).
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from plugins.memory import load_memory_provider

import writer_lease as writer_lease_module
from writer_lease import (
    TRUTH_WRITER_LEASE_FILENAME,
    TruthWriterLease,
    read_truth_writer_owner,
)


READ_ONLY_STATUS = "active_read_only"
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _provider():
    provider = load_memory_provider("scope-recall")
    assert provider is not None
    return provider


def _write_config(hermes_home: Path, payload: dict) -> None:
    path = hermes_home / "scope-recall" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


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


@contextlib.contextmanager
def _external_lease_holder(storage_dir: Path, *, role: str = "external-process"):
    """Hold the writer lease from a real child process (cross-process owner)."""

    storage_dir.mkdir(parents=True, exist_ok=True)
    child_script = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        sys.path.insert(0, {str(_REPO_ROOT)!r})
        from writer_lease import TruthWriterLease
        lease = TruthWriterLease(Path({str(storage_dir)!r}), role={role!r})
        result = lease.acquire()
        print("STATUS:" + result["status"], flush=True)
        sys.stdin.readline()
        lease.release()
        print("RELEASED", flush=True)
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "STATUS:acquired"
        yield child
    finally:
        if child.poll() is None:
            try:
                child.stdin.write("\n")
                child.stdin.close()
                assert child.stdout.readline().strip() == "RELEASED"
                child.wait(timeout=10)
            except Exception:
                child.kill()
                child.wait(timeout=10)


def test_lease_roundtrip(tmp_path):
    storage = tmp_path / "scope-recall"
    lease = TruthWriterLease(storage, role="provider")
    result = lease.acquire()
    assert result["status"] == "acquired"
    assert lease.acquired is True
    assert (storage / TRUTH_WRITER_LEASE_FILENAME).is_file()
    owner = read_truth_writer_owner(storage)
    assert owner.get("role") == "provider"
    assert "pid" not in owner
    assert "hostname" not in owner
    lease.release()
    assert lease.acquired is False

    again = TruthWriterLease(storage, role="journal_digest")
    assert again.acquire()["status"] == "acquired"
    again.release()


def test_same_process_leases_share_one_refcounted_lock(tmp_path):
    storage = tmp_path / "scope-recall"
    key_count = lambda: len(writer_lease_module._PROCESS_REGISTRY)  # noqa: E731
    baseline = key_count()

    first = TruthWriterLease(storage, role="provider")
    second = TruthWriterLease(storage, role="journal_digest")
    assert first.acquire()["status"] == "acquired"
    shared = second.acquire()
    assert shared["status"] == "acquired"
    assert shared.get("scope") == "same_process_shared"
    assert key_count() == baseline + 1

    # Releasing one holder keeps the process lease (and the OS lock) alive.
    first.release()
    assert key_count() == baseline + 1
    second.release()
    assert key_count() == baseline


def test_cross_process_lease_blocks_second_process(tmp_path):
    storage = tmp_path / "scope-recall"
    with _external_lease_holder(storage, role="child") as child:
        mine = TruthWriterLease(storage, role="provider")
        busy = mine.acquire()
        assert busy["status"] == "busy"
        assert busy["scope"] == "cross_process"
        # Sidecar only publishes allowlisted roles. Arbitrary child labels
        # collapse to unknown and must not echo pid/hostname.
        assert busy["owner"].get("role") == "unknown"
        assert "pid" not in (busy.get("owner") or {})
        del child

    recovered = mine.acquire()
    assert recovered["status"] == "acquired"
    mine.release()


def test_same_process_peer_providers_both_write(tmp_path):
    """Hermes-supported same-process peers must keep full write capability."""

    _write_config(tmp_path, {"vector": {"enabled": False}})
    first = _provider()
    second = _provider()
    try:
        _initialize(first, tmp_path, "peer-first")
        _initialize(second, tmp_path, "peer-second")
        assert first.runtime_status == "active"
        assert second.runtime_status == "active"
        assert first._truth_writer_role == "owner"
        assert second._truth_writer_role == "owner"
        receipt = json.loads(
            second.handle_tool_call(
                "scope_recall_store",
                {"content": "same-process peer write stays allowed", "target": "ops"},
            )
        )
        assert receipt.get("id")
    finally:
        for provider in (first, second):
            try:
                provider.shutdown()
            except Exception:
                pass


def test_provider_degrades_to_read_only_under_external_writer(tmp_path):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    storage = tmp_path / "scope-recall"

    # Phase A: a normal writer seeds one durable memory, then exits cleanly.
    seeder = _provider()
    _initialize(seeder, tmp_path, "seed-session")
    store_receipt = json.loads(
        seeder.handle_tool_call(
            "scope_recall_store",
            {"content": "The lease pilot project deploys with uv run app.", "target": "ops"},
        )
    )
    assert store_receipt.get("id")
    seeder.shutdown()

    # Phase B: an external process owns the lease; this provider is a reader.
    reader = _provider()
    try:
        with _external_lease_holder(storage, role="provider"):
            _initialize(reader, tmp_path, "reader-session")
            assert reader.runtime_status == READ_ONLY_STATUS
            assert reader._truth_writer_role == "reader"
            assert reader.is_available() is True

            search = json.loads(
                reader.handle_tool_call(
                    "scope_recall_search", {"query": "lease pilot project deploy"}
                )
            )
            assert search.get("count", 0) >= 1

            stats = json.loads(reader.handle_tool_call("scope_recall_stats", {}))
            assert stats["truth_writer"]["role"] == "reader"
            assert stats["truth_writer"]["owner"].get("role") == "provider"
            serialized_owner = json.dumps(stats["truth_writer"]["owner"])
            assert "pid" not in serialized_owner.lower()
            assert "hostname" not in serialized_owner.lower()

            blocked = json.loads(
                reader.handle_tool_call(
                    "scope_recall_store",
                    {"content": "must not be stored", "target": "ops"},
                )
            )
            assert "truth_writer_busy" in str(blocked.get("error") or "")
            blocked_update = json.loads(
                reader.handle_tool_call(
                    "scope_recall_memory",
                    {"action": "update", "id": store_receipt["id"], "content": "nope"},
                )
            )
            assert "truth_writer_busy" in str(blocked_update.get("error") or "")

            db_path = storage / "memory.sqlite3"
            probe = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
            journal_before = probe.execute(
                "SELECT COUNT(*) FROM journal_entries"
            ).fetchone()[0]
            reader.sync_turn(
                "please remember this reader-mode line for the lease test",
                "acknowledged with a sufficiently long assistant reply for capture",
            )
            assert reader.on_pre_compress(
                [{"role": "user", "content": "reader-mode compression boundary text"}]
            ) == ""
            journal_after = probe.execute(
                "SELECT COUNT(*) FROM journal_entries"
            ).fetchone()[0]
            probe.close()
            assert journal_after == journal_before

            assert "read-only recall mode" in reader.system_prompt_block()

        # Phase C: external owner exited; the next turn promotes this runtime.
        reader.on_turn_start(2, "probe after external writer exit")
        assert reader._truth_writer_role == "owner"
        assert reader.runtime_status == "active"
        promoted = json.loads(
            reader.handle_tool_call(
                "scope_recall_store",
                {"content": "stored after writer-lease promotion succeeded", "target": "ops"},
            )
        )
        assert promoted.get("id")
    finally:
        try:
            reader.shutdown()
        except Exception:
            pass


def test_legacy_lease_disable_key_is_ignored_and_degrades_to_reader(tmp_path):
    """Old truth_writer_lease.enabled=false must not restore a second writer."""

    _write_config(
        tmp_path,
        {"vector": {"enabled": False}, "truth_writer_lease": {"enabled": False}},
    )
    storage = tmp_path / "scope-recall"
    provider = _provider()
    try:
        with _external_lease_holder(storage, role="ignored-external"):
            _initialize(provider, tmp_path, "legacy-session")
            assert provider.runtime_status == READ_ONLY_STATUS
            assert provider._truth_writer_role == "reader"
    finally:
        try:
            provider.shutdown()
        except Exception:
            pass


def test_failed_writer_startup_releases_the_lease(tmp_path):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    broken = _provider()
    broken._initialize_writer_runtime = lambda: (_ for _ in ()).throw(
        RuntimeError("simulated writer startup failure")
    )
    with pytest.raises(RuntimeError, match="simulated writer startup failure"):
        _initialize(broken, tmp_path, "broken-session")
    assert broken._truth_writer_lease is None

    healthy = _provider()
    try:
        _initialize(healthy, tmp_path, "healthy-session")
        assert healthy.runtime_status == "active"
        assert healthy._truth_writer_role == "owner"
    finally:
        healthy.shutdown()


def test_reader_mode_without_database_stays_graceful(tmp_path):
    _write_config(tmp_path, {"vector": {"enabled": False}})
    storage = tmp_path / "scope-recall"
    provider = _provider()
    try:
        with _external_lease_holder(storage, role="external-holder"):
            _initialize(provider, tmp_path, "no-db-session")
            assert provider.runtime_status == READ_ONLY_STATUS
            assert provider._conn is None
            assert provider.prefetch("anything relevant to recall here") == ""
            provider.sync_turn("captured nothing", "captured nothing either")
    finally:
        try:
            provider.shutdown()
        except Exception:
            pass


@pytest.mark.skipif(
    sys.platform == "win32", reason="descriptor hardening is POSIX-only"
)
def test_descriptor_hardening_runs_once_per_process(tmp_path, monkeypatch):
    import truth_connection as tc

    db_path = tmp_path / "harden" / "memory.sqlite3"
    first = tc.connect_truth_database(db_path, mode="rwc")
    first.close()

    opened: list[str] = []
    real_open = tc.os.open

    def counting_open(path, flags, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(tc.os, "open", counting_open)
    second = tc.connect_truth_database(db_path, mode="rw")
    second.close()
    assert str(db_path) not in opened, (
        "a second connection re-opened the live truth file with os.open, "
        "which cancels POSIX advisory locks (issue #39)"
    )
