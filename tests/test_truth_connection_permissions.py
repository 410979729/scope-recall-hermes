"""Truth-store path and permission hardening contracts."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import textwrap

import pytest

import scope_recall.doctor_sqlite as doctor_sqlite
from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.truth_connection import (
    TruthDatabaseConnectionError,
    connect_truth_database,
    truth_storage_permissions,
)
from writer_lease import (
    TRUTH_WRITER_LEASE_FILENAME,
    TruthWriterLease,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _child_acquire_status(storage_dir: Path) -> str:
    """Ask a new process whether it can take the writer lease."""

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


def test_truth_connection_preserves_sqlite_memory_database_semantics():
    conn = connect_truth_database(":memory:", mode="rwc")
    try:
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()
        conn.execute("CREATE TABLE marker(value TEXT)")
        conn.execute("INSERT INTO marker(value) VALUES ('ready')")
        stored = conn.execute("SELECT value FROM marker").fetchone()
    finally:
        conn.close()

    assert foreign_keys is not None and int(foreign_keys[0]) == 1
    assert stored is not None and stored[0] == "ready"


def test_truth_connection_handles_sqlite_uri_metacharacters(tmp_path):
    metacharacters = "# percent" if os.name == "nt" else "? # percent"
    db_path = tmp_path / f"memory {metacharacters}.sqlite3"

    conn = connect_truth_database(db_path, mode="rwc")
    try:
        conn.execute("CREATE TABLE marker(value TEXT)")
        conn.commit()
    finally:
        conn.close()

    assert db_path.is_file()


def test_mutable_truth_connection_does_not_resolve_paths_before_no_follow(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "memory.sqlite3"
    original_resolve = Path.resolve

    def forbid_database_resolve(self, *args, **kwargs):
        if Path(self).name == "memory.sqlite3":
            raise AssertionError("mutable truth path must not resolve symlinks")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", forbid_database_resolve)
    conn = connect_truth_database(db_path, mode="rwc")
    conn.close()

    assert db_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX O_NOFOLLOW contract")
def test_mutable_truth_connection_rejects_database_symlink(tmp_path):
    target = tmp_path / "target.sqlite3"
    sqlite_conn = connect_truth_database(target, mode="rwc")
    sqlite_conn.close()
    link = tmp_path / "linked.sqlite3"
    link.symlink_to(target)

    report = truth_storage_permissions(link)
    assert report["status"] == "unsafe"
    assert report["symlink"] is True
    with pytest.raises(TruthDatabaseConnectionError, match="symlink"):
        connect_truth_database(link, mode="rw")


@pytest.mark.skipif(os.name == "nt", reason="Windows uses inherited ACLs, not POSIX mode bits")
def test_mutable_truth_connection_hardens_directory_and_database_modes(tmp_path):
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir(mode=0o777)
    os.chmod(storage_dir, 0o777)
    db_path = storage_dir / "memory.sqlite3"

    conn = connect_truth_database(db_path, mode="rwc")
    conn.close()

    assert stat.S_IMODE(storage_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_sqlite_doctor_fails_closed_on_unsafe_posix_permissions(
    tmp_path,
    monkeypatch,
):
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir()
    db_path = storage_dir / "memory.sqlite3"
    conn = connect_truth_database(db_path, mode="rwc")
    try:
        ensure_schema(conn)
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(
        doctor_sqlite,
        "truth_storage_permissions",
        lambda _path: {
            "status": "unsafe",
            "ok": False,
            "platform_policy": "posix-owner-only",
            "directory_mode": "0755",
            "database_mode": "0644",
        },
        raising=False,
    )

    payload, check, recommendations = doctor_sqlite.sqlite_report(tmp_path)

    assert payload["storage_permissions"]["status"] == "unsafe"
    assert check["ok"] is False
    assert any("permissions" in failure.lower() for failure in check["failures"])
    assert any("0600" in item and "0700" in item for item in recommendations)


def test_sqlite_doctor_fails_closed_on_incomplete_factual_freshness_coverage(
    tmp_path,
):
    storage_dir = tmp_path / "scope-recall"
    storage_dir.mkdir()
    db_path = storage_dir / "memory.sqlite3"
    conn = connect_truth_database(db_path, mode="rwc")
    conn.row_factory = doctor_sqlite.sqlite3.Row
    try:
        ensure_schema(conn)
        store_row(
            conn,
            memory_id="doctor-untracked-fact",
            scope_id="shared-scope",
            platform="telegram",
            user_id="joy",
            chat_id="dm",
            thread_id="",
            gateway_session_key="",
            agent_identity="yuheng",
            agent_workspace="hermes",
            session_id="doctor-freshness",
            source="tool-store",
            target="ops",
            content="Doctor untracked factual freshness sentinel.",
            metadata='{"memory_type":"factual","lifecycle":"promoted"}',
        )
        conn.execute(
            "DELETE FROM fact_freshness WHERE subject_id = 'doctor-untracked-fact'"
        )
        conn.commit()
    finally:
        conn.close()

    payload, check, recommendations = doctor_sqlite.sqlite_report(tmp_path)

    assert payload["fact_freshness"]["coverage"] == {
        "factual_memories": 1,
        "tracked_memory_facts": 0,
        "coverage_percent": 0.0,
    }
    assert check["ok"] is False
    assert any(
        "freshness coverage" in failure.lower() for failure in check["failures"]
    )
    assert any("backfill" in item.lower() for item in recommendations)


def test_writable_live_memory_connection_holds_child_lease_until_close(tmp_path):
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    db_path = storage / "memory.sqlite3"
    conn = connect_truth_database(db_path, mode="rwc")
    try:
        assert isinstance(conn, sqlite3.Connection)
        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        assert conn.row_factory is sqlite3.Row
        assert _child_acquire_status(storage) == "STATUS:busy"
        peer = TruthWriterLease(storage, role="provider")
        shared = peer.acquire()
        assert shared["status"] == "acquired"
        assert shared.get("scope") == "same_process_shared"
        peer.release()
        assert _child_acquire_status(storage) == "STATUS:busy"
    finally:
        conn.close()
    assert _child_acquire_status(storage) == "STATUS:acquired"


def test_readonly_truth_connection_remains_usable_under_external_owner(tmp_path):
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    db_path = storage / "memory.sqlite3"
    seed = connect_truth_database(db_path, mode="rwc")
    seed.close()
    owner = TruthWriterLease(storage, role="provider")
    assert owner.acquire()["status"] == "acquired"
    try:
        conn = connect_truth_database(db_path, mode="ro")
        try:
            assert isinstance(conn, sqlite3.Connection)
            assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
            assert int(conn.execute("PRAGMA query_only").fetchone()[0]) == 1
            assert conn.execute("SELECT 1").fetchone()[0] == 1
        finally:
            conn.close()
        assert owner.acquired is True
        assert _child_acquire_status(storage) == "STATUS:busy"
    finally:
        owner.release()


def test_non_live_backup_filename_does_not_create_truth_lease(tmp_path):
    storage = tmp_path / "scope-recall"
    backups = storage / "backups"
    backups.mkdir(parents=True)
    backup = backups / "pre-requeue.sqlite3"
    conn = connect_truth_database(backup, mode="rwc")
    try:
        assert isinstance(conn, sqlite3.Connection)
        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        assert not (backups / TRUTH_WRITER_LEASE_FILENAME).exists()
        assert not (storage / TRUTH_WRITER_LEASE_FILENAME).exists()
        assert _child_acquire_status(storage) == "STATUS:acquired"
        assert _child_acquire_status(backups) == "STATUS:acquired"
    finally:
        conn.close()


def test_leased_truth_connection_close_failure_retains_lease_for_retry(
    tmp_path, monkeypatch
):
    from scope_recall.truth_connection import _LeasedTruthConnection

    storage = tmp_path / "scope-recall"
    storage.mkdir()
    db_path = storage / "memory.sqlite3"
    original_close = _LeasedTruthConnection.close

    def fail_before_super(self) -> None:
        raise RuntimeError("injected sqlite close failure")

    monkeypatch.setattr(_LeasedTruthConnection, "close", fail_before_super)
    conn = connect_truth_database(db_path, mode="rwc")
    assert isinstance(conn, _LeasedTruthConnection)
    with pytest.raises(RuntimeError, match="injected sqlite close failure"):
        conn.close()
    assert _child_acquire_status(storage) == "STATUS:busy"
    monkeypatch.setattr(_LeasedTruthConnection, "close", original_close)
    conn.close()
    assert _child_acquire_status(storage) == "STATUS:acquired"


def test_leased_truth_connection_release_error_after_sqlite_close(tmp_path):
    from scope_recall.truth_connection import _LeasedTruthConnection

    storage = tmp_path / "scope-recall"
    storage.mkdir()
    db_path = storage / "memory.sqlite3"

    retained = connect_truth_database(db_path, mode="rwc")
    try:
        assert isinstance(retained, _LeasedTruthConnection)
        lease = retained._truth_writer_lease
        assert lease is not None
        real_release = lease.release

        def release_still_held() -> None:
            raise RuntimeError("injected lease release failure")

        lease.release = release_still_held  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="injected lease release failure"):
            retained.close()
        assert lease.acquired is True
        assert retained._truth_writer_lease is lease
        assert _child_acquire_status(storage) == "STATUS:busy"
        lease.release = real_release  # type: ignore[method-assign]
        retained.close()
        assert retained._truth_writer_lease is None
        assert lease.acquired is False
    finally:
        try:
            retained.close()
        except Exception:
            pass
    assert _child_acquire_status(storage) == "STATUS:acquired"

    detached = connect_truth_database(db_path, mode="rw")
    try:
        lease = detached._truth_writer_lease
        assert lease is not None
        real_release = lease.release

        def release_then_raise() -> None:
            real_release()
            raise RuntimeError("injected release after authority gone")

        lease.release = release_then_raise  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="injected release after authority gone"):
            detached.close()
        assert lease.acquired is False
        assert detached._truth_writer_lease is None
        assert _child_acquire_status(storage) == "STATUS:acquired"
    finally:
        try:
            detached.close()
        except Exception:
            pass


def _path_ends_with_live_truth_filename(node: ast.AST) -> bool:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return (
            isinstance(node.right, ast.Constant) and node.right.value == "memory.sqlite3"
        )
    if isinstance(node, ast.Call) and node.args:
        func = node.func
        if isinstance(func, ast.Name) and func.id == "Path":
            first = node.args[0]
            return isinstance(first, ast.Constant) and first.value == "memory.sqlite3"
    return False


def _sqlite_connect_target(node: ast.Call) -> ast.AST | None:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "connect":
        value = func.value
        if isinstance(value, ast.Name) and value.id == "sqlite3" and node.args:
            return node.args[0]
    return None


def test_shipped_scripts_open_live_truth_only_through_connect_truth_database():
    scripts_dir = _REPO_ROOT / "scripts"
    violations: list[str] = []
    for script in sorted(scripts_dir.glob("*.py")):
        tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script.name))
        live_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                if _path_ends_with_live_truth_filename(node.value):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            live_names.add(target.id)
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.value is not None and _path_ends_with_live_truth_filename(
                    node.value
                ):
                    live_names.add(node.target.id)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = _sqlite_connect_target(node)
            if target is None:
                continue
            if _path_ends_with_live_truth_filename(target):
                violations.append(f"{script.name}: sqlite3.connect(live memory.sqlite3)")
                continue
            if isinstance(target, ast.Name) and target.id in live_names:
                violations.append(
                    f"{script.name}: sqlite3.connect({target.id}) where "
                    f"{target.id} is live memory.sqlite3"
                )
    assert violations == []


def test_truth_connection_setup_close_failure_retains_retryable_cleanup_owner(
    tmp_path, monkeypatch
):
    import scope_recall.truth_connection as truth_module
    import writer_lease as writer_lease_module
    from scope_recall.truth_connection import (
        TruthDatabaseCleanupError,
        _LeasedTruthConnection,
    )

    storage = tmp_path / "scope-recall"
    storage.mkdir()
    db_path = storage / "memory.sqlite3"
    baseline = len(writer_lease_module._PROCESS_REGISTRY)
    original_close = _LeasedTruthConnection.close

    def fail_setup(_conn):
        raise RuntimeError("injected post-open setup failure")

    def fail_before_sqlite_close(self) -> None:
        raise RuntimeError("injected sqlite close failure")

    monkeypatch.setattr(truth_module, "require_foreign_keys", fail_setup)
    monkeypatch.setattr(_LeasedTruthConnection, "close", fail_before_sqlite_close)

    with pytest.raises(TruthDatabaseCleanupError) as caught:
        connect_truth_database(db_path, mode="rwc")
    err = caught.value
    cause = err.__cause__
    assert isinstance(cause, RuntimeError)
    assert "injected post-open setup failure" in str(cause)
    assert err.cleanup_pending is True
    rendered = f"{err!s} {err!r}"
    assert str(storage) not in rendered
    assert "memory.sqlite3" not in rendered
    assert _child_acquire_status(storage) == "STATUS:busy"
    assert len(writer_lease_module._PROCESS_REGISTRY) == baseline + 1

    monkeypatch.setattr(_LeasedTruthConnection, "close", original_close)
    err.retry_cleanup()
    assert err.cleanup_pending is False
    err.retry_cleanup()
    assert err.cleanup_pending is False
    assert len(writer_lease_module._PROCESS_REGISTRY) == baseline
    assert _child_acquire_status(storage) == "STATUS:acquired"


def test_truth_connection_cleanup_retry_failure_remains_pending(tmp_path, monkeypatch):
    import scope_recall.truth_connection as truth_module
    import writer_lease as writer_lease_module
    from scope_recall.truth_connection import (
        TruthDatabaseCleanupError,
        _LeasedTruthConnection,
    )

    storage = tmp_path / "scope-recall"
    storage.mkdir()
    db_path = storage / "memory.sqlite3"
    baseline = len(writer_lease_module._PROCESS_REGISTRY)
    original_close = _LeasedTruthConnection.close

    def fail_setup(_conn):
        raise RuntimeError("injected post-open setup failure")

    def fail_before_sqlite_close(self) -> None:
        raise RuntimeError("injected sqlite close failure")

    monkeypatch.setattr(truth_module, "require_foreign_keys", fail_setup)
    monkeypatch.setattr(_LeasedTruthConnection, "close", fail_before_sqlite_close)

    with pytest.raises(TruthDatabaseCleanupError) as caught:
        connect_truth_database(db_path, mode="rwc")
    err = caught.value
    assert err.cleanup_pending is True
    with pytest.raises(RuntimeError, match="injected sqlite close failure"):
        err.retry_cleanup()
    assert err.cleanup_pending is True
    assert _child_acquire_status(storage) == "STATUS:busy"
    assert len(writer_lease_module._PROCESS_REGISTRY) == baseline + 1

    monkeypatch.setattr(_LeasedTruthConnection, "close", original_close)
    err.retry_cleanup()
    assert err.cleanup_pending is False
    assert len(writer_lease_module._PROCESS_REGISTRY) == baseline
    assert _child_acquire_status(storage) == "STATUS:acquired"


def _seed_live_truth(storage: Path) -> Path:
    """Create canonical live truth with one durable probe row, then close."""

    storage.mkdir(parents=True, exist_ok=True)
    db_path = storage / "memory.sqlite3"
    conn = connect_truth_database(db_path, mode="rwc")
    try:
        conn.execute("CREATE TABLE alias_probe(value TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO alias_probe(value) VALUES ('seed')")
        conn.commit()
    finally:
        conn.close()
    return db_path


def _probe_values(db_path: Path) -> list[str]:
    conn = connect_truth_database(db_path, mode="rw")
    try:
        rows = conn.execute("SELECT value FROM alias_probe ORDER BY value").fetchall()
    finally:
        conn.close()
    return [str(row[0]) for row in rows]


@pytest.mark.skipif(os.name != "nt", reason="Windows case-alias writer-lease bypass")
def test_windows_uppercase_alias_cannot_mutate_live_truth_under_provider_lease(
    tmp_path,
):
    storage = tmp_path / "scope-recall"
    canonical = _seed_live_truth(storage)
    alias = os.path.join(str(storage), "MEMORY.SQLITE3")
    assert Path(alias).name == "MEMORY.SQLITE3"
    assert os.path.samefile(canonical, alias)

    owner = TruthWriterLease(storage, role="provider")
    assert owner.acquire()["status"] == "acquired"
    try:
        child_script = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(_REPO_ROOT)!r})
            from truth_connection import connect_truth_database
            from writer_lease import TruthWriterBusyError
            try:
                conn = connect_truth_database({alias!r}, mode="rw")
            except TruthWriterBusyError:
                print("STATUS:busy", flush=True)
                raise SystemExit(0)
            try:
                leased = getattr(conn, "_truth_writer_lease", None) is not None
                conn.execute(
                    "INSERT INTO alias_probe(value) VALUES ('case-alias-bypass')"
                )
                conn.commit()
                print("STATUS:wrote", flush=True)
                print("LEASED:" + str(leased), flush=True)
            finally:
                conn.close()
            """
        )
        child = subprocess.run(
            [sys.executable, "-c", child_script],
            capture_output=True,
            text=True,
            timeout=20,
        )
        status_lines = [
            line for line in child.stdout.splitlines() if line.startswith("STATUS:")
        ]
        lease_lines = [
            line for line in child.stdout.splitlines() if line.startswith("LEASED:")
        ]
        assert status_lines == ["STATUS:busy"], (
            f"stdout={child.stdout!r} stderr={child.stderr!r} "
            f"rc={child.returncode} leased={lease_lines}"
        )
    finally:
        owner.release()

    assert _probe_values(canonical) == ["seed"]


def test_hardlink_alias_to_canonical_memory_is_classified_and_leased(tmp_path):
    from scope_recall.truth_connection import is_live_truth_database_path

    storage = tmp_path / "scope-recall"
    canonical = _seed_live_truth(storage)
    alias = storage / "truth-hardlink.sqlite3"
    try:
        os.link(canonical, alias)
    except OSError as exc:
        pytest.skip(f"filesystem cannot create the hardlink: {exc}")
    assert os.path.samefile(canonical, alias)
    assert is_live_truth_database_path(alias) is True
    assert is_live_truth_database_path(canonical) is True

    conn = connect_truth_database(alias, mode="rw")
    try:
        assert _child_acquire_status(storage) == "STATUS:busy"
    finally:
        conn.close()
    assert _child_acquire_status(storage) == "STATUS:acquired"

    owner = TruthWriterLease(storage, role="provider")
    assert owner.acquire()["status"] == "acquired"
    try:
        child_script = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(_REPO_ROOT)!r})
            from truth_connection import connect_truth_database
            from writer_lease import TruthWriterBusyError
            try:
                conn = connect_truth_database({str(alias)!r}, mode="rw")
            except TruthWriterBusyError:
                print("STATUS:busy", flush=True)
                raise SystemExit(0)
            try:
                leased = getattr(conn, "_truth_writer_lease", None) is not None
                conn.execute(
                    "INSERT INTO alias_probe(value) VALUES ('hardlink-alias-bypass')"
                )
                conn.commit()
                print("STATUS:wrote", flush=True)
                print("LEASED:" + str(leased), flush=True)
            finally:
                conn.close()
            """
        )
        child = subprocess.run(
            [sys.executable, "-c", child_script],
            capture_output=True,
            text=True,
            timeout=20,
        )
        status_lines = [
            line for line in child.stdout.splitlines() if line.startswith("STATUS:")
        ]
        assert status_lines == ["STATUS:busy"], (
            f"stdout={child.stdout!r} stderr={child.stderr!r} rc={child.returncode}"
        )
    finally:
        owner.release()
    assert _probe_values(canonical) == ["seed"]


def test_live_truth_path_classifier_excludes_non_alias_companions(tmp_path):
    from scope_recall.truth_connection import is_live_truth_database_path

    storage = tmp_path / "scope-recall"
    storage.mkdir()
    backups = storage / "backups"
    backups.mkdir()
    companions = (
        backups / "pre-requeue.sqlite3",
        storage / "vector.sqlite3",
        storage / "staging.sqlite3",
    )
    for companion in companions:
        conn = connect_truth_database(companion, mode="rwc")
        conn.close()
        assert is_live_truth_database_path(companion) is False
        assert not (companion.parent / TRUTH_WRITER_LEASE_FILENAME).exists()

    assert is_live_truth_database_path(":memory:") is False
    assert is_live_truth_database_path(storage / "memory.sqlite3") is True
    assert is_live_truth_database_path(storage / "MEMORY.SQLITE3") is True
    assert is_live_truth_database_path(storage / "Memory.Sqlite3") is True
    assert is_live_truth_database_path(storage / "missing-other.sqlite3") is False
    assert _child_acquire_status(storage) == "STATUS:acquired"

    canonical = _seed_live_truth(storage)
    assert is_live_truth_database_path(canonical) is True
    for companion in companions:
        assert is_live_truth_database_path(companion) is False
        conn = connect_truth_database(companion, mode="rw")
        try:
            assert _child_acquire_status(storage) == "STATUS:acquired"
        finally:
            conn.close()
    assert _child_acquire_status(storage) == "STATUS:acquired"


def test_live_truth_path_classifier_samefile_oserror_does_not_bypass_canonical_basename(
    tmp_path, monkeypatch
):
    from scope_recall.truth_connection import is_live_truth_database_path

    def boom(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
        raise OSError("injected samefile failure")

    monkeypatch.setattr(os.path, "samefile", boom)
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    assert is_live_truth_database_path(storage / "memory.sqlite3") is True
    assert is_live_truth_database_path(storage / "MEMORY.SQLITE3") is True
    assert is_live_truth_database_path(storage / "vector.sqlite3") is False
    assert is_live_truth_database_path(storage / "missing-other.sqlite3") is False


def test_non_live_vector_filename_does_not_create_truth_lease(tmp_path):
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    vector = storage / "vector.sqlite3"
    conn = connect_truth_database(vector, mode="rwc")
    try:
        assert isinstance(conn, sqlite3.Connection)
        assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        assert not (storage / TRUTH_WRITER_LEASE_FILENAME).exists()
        assert _child_acquire_status(storage) == "STATUS:acquired"
    finally:
        conn.close()


def test_non_live_different_filename_does_not_create_truth_lease(tmp_path):
    storage = tmp_path / "scope-recall"
    storage.mkdir()
    other = storage / "staging.sqlite3"
    conn = connect_truth_database(other, mode="rwc")
    try:
        assert isinstance(conn, sqlite3.Connection)
        assert not (storage / TRUTH_WRITER_LEASE_FILENAME).exists()
        assert _child_acquire_status(storage) == "STATUS:acquired"
    finally:
        conn.close()
