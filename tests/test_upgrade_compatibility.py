"""N-1 installer compatibility checks that run before target mutation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import scope_recall.installer as installer
import scope_recall.vector_migration as vector_migration
from scope_recall.embedders import LocalHashEmbedder
from scope_recall.sql_store import ensure_schema, store_row
from scope_recall.vector_generation import GenerationIdentity, bootstrap_legacy_generation
from scope_recall.vector_generation_preflight import PREFLIGHT_RECEIPT_FILENAME
from scope_recall.vector_migration import build_vector_generation


def _write_home_config(home: Path, payload: dict[str, object]) -> None:
    storage = home / "scope-recall"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "config.json").write_text(
        json.dumps(payload, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _existing_target(home: Path) -> Path:
    target = home / "plugins" / "scope-recall"
    target.mkdir(parents=True, exist_ok=True)
    (target / "plugin.yaml").write_text(
        "name: scope-recall\nversion: 1.7.2\n",
        encoding="utf-8",
    )
    (target / "sentinel.txt").write_text("old-target-stays\n", encoding="utf-8")
    return target


def _assert_no_target_mutation(home: Path, target: Path) -> None:
    assert (target / "sentinel.txt").read_text(encoding="utf-8") == "old-target-stays\n"
    backup_root = home / "backups"
    assert not backup_root.exists() or not any(backup_root.rglob("*"))


def _ready_generation(home: Path, *, generation_id: str) -> Path:
    storage = home / "scope-recall"
    storage.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    store_row(
        conn,
        memory_id="upgrade-memory",
        scope_id="scope-upgrade",
        platform="test",
        user_id="owner-fixture",
        chat_id="allowed-chat-fixture",
        thread_id="",
        gateway_session_key="",
        agent_identity="test-agent",
        agent_workspace="test-workspace",
        session_id="upgrade-session",
        source="fixture",
        target="memory",
        content="Upgrade compatibility vector fixture.",
        allow_duplicate=True,
    )
    identity = GenerationIdentity(
        backend="sqlite-bruteforce",
        provider="local-hash",
        model="hash-v1",
        dimensions=16,
        metric="cosine",
        prompt_profile="default-v1",
        table_name="memories",
    )
    current = bootstrap_legacy_generation(conn, identity=identity, row_count=0)
    conn.commit()
    result = build_vector_generation(
        storage,
        conn,
        generation_id=generation_id,
        identity=identity,
        embedder=LocalHashEmbedder(dimensions=16, model="hash-v1"),
        index_general=False,
        activate=False,
        expected_current=str(current["generation_id"]),
    )
    assert result["status"] == "ready"
    conn.close()
    return storage / "vector-generations" / generation_id


def test_n_minus_one_isolation_key_passes_read_only_upgrade_preflight(tmp_path: Path) -> None:
    _write_home_config(
        tmp_path,
        {"memory_isolated_chat_ids": ["isolated-chat-fixture"]},
    )

    result = installer._upgrade_compatibility_preflight(
        tmp_path,
        installer.source_root(),
    )

    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["config"]["errors"] == []


def test_upgrade_preflight_rejects_manifestless_nonfresh_vector_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    storage = home / "scope-recall"
    storage.mkdir(parents=True)
    _write_home_config(
        home,
        {
            "vector": {
                "enabled": True,
                "backend": "sqlite-bruteforce",
                "fallback_backend": "sqlite-bruteforce",
            }
        },
    )
    db_path = storage / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    store_row(
        conn,
        memory_id="manifestless-truth",
        scope_id="scope-upgrade",
        platform="test",
        user_id="owner-fixture",
        chat_id="allowed-chat-fixture",
        thread_id="",
        gateway_session_key="",
        agent_identity="test-agent",
        agent_workspace="test-workspace",
        session_id="upgrade-session",
        source="fixture",
        target="memory",
        content="Manifestless truth must migrate before replacement.",
        allow_duplicate=True,
    )
    conn.commit()
    conn.close()
    before = db_path.read_bytes()

    result = installer._upgrade_compatibility_preflight(
        home,
        installer.source_root(),
    )

    assert result["ok"] is False
    assert result["read_only"] is True
    assert db_path.read_bytes() == before
    assert any("active vector generation manifest is missing" in item for item in result["failures"])
    assert any("migrate.vector_generation.py" in item for item in result["next_steps"])


def test_upgrade_preflight_rejects_manifestless_existing_companion(tmp_path: Path) -> None:
    home = tmp_path / "home"
    storage = home / "scope-recall"
    storage.mkdir(parents=True)
    _write_home_config(
        home,
        {
            "vector": {
                "enabled": True,
                "backend": "sqlite-bruteforce",
                "fallback_backend": "sqlite-bruteforce",
            }
        },
    )
    db_path = storage / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    ensure_schema(conn)
    conn.commit()
    conn.close()
    legacy_path = storage / "vector.sqlite3"
    legacy_path.write_bytes(b"manifestless-companion-fixture")
    before_db = db_path.read_bytes()
    before_companion = legacy_path.read_bytes()

    result = installer._upgrade_compatibility_preflight(
        home,
        installer.source_root(),
    )

    assert result["ok"] is False
    assert result["read_only"] is True
    assert db_path.read_bytes() == before_db
    assert legacy_path.read_bytes() == before_companion
    assert any("manifestless vector companion" in item for item in result["failures"])
    assert any("migrate.vector_generation.py" in item for item in result["next_steps"])


def test_runtime_verify_surfaces_candidate_config_load_errors(tmp_path: Path) -> None:
    home = tmp_path / "home"
    installer.install(hermes_home=home)
    storage = home / "scope-recall"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "config.json").write_text(
        json.dumps({"unsupported_upgrade_fixture": True}) + "\n",
        encoding="utf-8",
    )
    with sqlite3.connect(storage / "memory.sqlite3") as conn:
        ensure_schema(conn)
        conn.commit()

    result = installer.verify(hermes_home=home, runtime=True)

    assert result["ok"] is False
    runtime = result["runtime"]
    assert runtime["config_load_errors"]
    assert any("runtime config load failed" in str(item).casefold() for item in runtime["failures"])


def test_unknown_n_minus_one_config_key_blocks_before_backup_or_replace(
    tmp_path: Path,
) -> None:
    target = _existing_target(tmp_path)
    _write_home_config(tmp_path, {"removed_private_capability": True})

    dry_run = installer.install(tmp_path, dry_run=True, force=True)
    assert dry_run["ok"] is False
    assert dry_run["upgrade_compatibility"]["checked_before_backup"] is True
    with pytest.raises(installer.InstallError, match="compatibility preflight"):
        installer.install(tmp_path, force=True)

    _assert_no_target_mutation(tmp_path, target)


def test_ready_generation_without_receipt_blocks_before_backup_or_replace(
    tmp_path: Path,
) -> None:
    target = _existing_target(tmp_path)
    _write_home_config(tmp_path, {"vector": {"enabled": False}})
    generation_root = _ready_generation(tmp_path, generation_id="ready-without-receipt")
    (generation_root / PREFLIGHT_RECEIPT_FILENAME).unlink()
    vector_mtime = (generation_root / "vector.sqlite3").stat().st_mtime_ns

    preflight = installer._upgrade_compatibility_preflight(
        tmp_path,
        installer.source_root(),
    )

    assert preflight["ok"] is False
    assert preflight["ready_vector_generations"][0]["ok"] is False
    assert "receipt" in preflight["ready_vector_generations"][0]["error"]
    with pytest.raises(installer.InstallError, match="receipt|preflight"):
        installer.install(tmp_path, force=True)
    assert (generation_root / "vector.sqlite3").stat().st_mtime_ns == vector_mtime
    _assert_no_target_mutation(tmp_path, target)


def test_ready_generation_with_bound_receipt_passes_upgrade_preflight(
    tmp_path: Path,
) -> None:
    _write_home_config(tmp_path, {"vector": {"enabled": False}})
    generation_root = _ready_generation(tmp_path, generation_id="ready-with-receipt")
    receipt = generation_root / PREFLIGHT_RECEIPT_FILENAME
    before = (
        (generation_root / "vector.sqlite3").stat().st_mtime_ns,
        receipt.stat().st_mtime_ns,
    )

    result = installer._upgrade_compatibility_preflight(
        tmp_path,
        installer.source_root(),
    )

    assert result["ok"] is True
    assert result["ready_vector_generations"][0]["ok"] is True
    assert result["ready_vector_generations"][0]["receipt_sha256"]
    assert (
        (generation_root / "vector.sqlite3").stat().st_mtime_ns,
        receipt.stat().st_mtime_ns,
    ) == before


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_ready_generation_with_sidecar_blocks_before_backup_or_replace(
    suffix: str,
    tmp_path: Path,
) -> None:
    target = _existing_target(tmp_path)
    _write_home_config(tmp_path, {"vector": {"enabled": False}})
    generation_root = _ready_generation(tmp_path, generation_id=f"ready-sidecar-{suffix[1:]}")
    db_path = generation_root / "vector.sqlite3"
    sidecar = db_path.with_name(f"{db_path.name}{suffix}")
    sidecar.write_bytes(b"receipt-unbound-sidecar")

    preflight = installer._upgrade_compatibility_preflight(
        tmp_path,
        installer.source_root(),
    )

    assert preflight["ok"] is False
    check = preflight["ready_vector_generations"][0]
    assert check["ok"] is False
    assert "sidecar" in check["error"].casefold()
    with pytest.raises(installer.InstallError, match="sidecar|preflight"):
        installer.install(tmp_path, force=True)
    assert sidecar.read_bytes() == b"receipt-unbound-sidecar"
    _assert_no_target_mutation(tmp_path, target)


def test_ready_generation_with_stale_truth_cohort_blocks_before_backup_or_replace(
    tmp_path: Path,
) -> None:
    target = _existing_target(tmp_path)
    _write_home_config(tmp_path, {"vector": {"enabled": False}})
    generation_root = _ready_generation(tmp_path, generation_id="ready-stale-source")
    receipt = generation_root / PREFLIGHT_RECEIPT_FILENAME

    storage = tmp_path / "scope-recall"
    with sqlite3.connect(storage / "memory.sqlite3") as conn:
        conn.row_factory = sqlite3.Row
        store_row(
            conn,
            memory_id="post-build-memory",
            scope_id="scope-upgrade",
            platform="test",
            user_id="owner-fixture",
            chat_id="allowed-chat-fixture",
            thread_id="",
            gateway_session_key="",
            agent_identity="test-agent",
            agent_workspace="test-workspace",
            session_id="upgrade-session",
            source="fixture",
            target="memory",
            content="Truth added after the READY generation receipt was bound.",
            allow_duplicate=True,
        )
        conn.commit()

    before = {
        path.relative_to(generation_root).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in generation_root.rglob("*")
        if path.is_file()
    }

    preflight = installer._upgrade_compatibility_preflight(
        tmp_path,
        installer.source_root(),
    )

    assert preflight["ok"] is False
    check = preflight["ready_vector_generations"][0]
    assert check["ok"] is False
    assert "stale" in check["error"].casefold()
    assert "expected_count=1, current_count=2" in check["error"]
    dry_run = installer.install(tmp_path, dry_run=True, force=True)
    assert dry_run["ok"] is False
    with pytest.raises(installer.InstallError, match="stale|preflight"):
        installer.install(tmp_path, force=True)

    after = {
        path.relative_to(generation_root).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in generation_root.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert receipt.is_file()
    _assert_no_target_mutation(tmp_path, target)


@pytest.mark.parametrize("tampered_epoch", ["manifest", "physical"])
def test_ready_epoch_tamper_after_initial_preflight_blocks_inside_backup_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tampered_epoch: str,
) -> None:
    target = _existing_target(tmp_path)
    _write_home_config(tmp_path, {"vector": {"enabled": False}})
    generation_id = f"ready-pre-backup-{tampered_epoch}"
    generation_root = _ready_generation(tmp_path, generation_id=generation_id)
    storage = tmp_path / "scope-recall"
    real_backup = installer._backup_existing_plugin
    backup_entries = 0

    def tamper_then_enter_backup(*args, **kwargs):
        nonlocal backup_entries
        backup_entries += 1
        # This wrapper is the deterministic first-backup boundary: the initial
        # preflight and staging copy have completed, but no backup directory or
        # target replacement has happened yet.
        if tampered_epoch == "manifest":
            with sqlite3.connect(storage / "memory.sqlite3") as conn:
                conn.execute(
                    "UPDATE vector_generations SET row_count = row_count + 1 "
                    "WHERE generation_id = ? AND lower(status) = 'ready'",
                    (generation_id,),
                )
                conn.commit()
        else:
            db_path = generation_root / "vector.sqlite3"
            db_path.with_name(f"{db_path.name}-wal").write_bytes(
                b"post-preflight-unbound-physical-epoch"
            )
        return real_backup(*args, **kwargs)

    monkeypatch.setattr(installer, "_backup_existing_plugin", tamper_then_enter_backup)

    with pytest.raises(installer.InstallError, match="revalidation|epoch|sidecar"):
        installer.install(tmp_path, force=True)

    assert backup_entries == 1
    _assert_no_target_mutation(tmp_path, target)


def test_pinned_reader_prevents_sqlite_generation_from_publishing_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    storage = tmp_path / "scope-recall"
    storage.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(storage / "memory.sqlite3")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    store_row(
        conn,
        memory_id="pinned-upgrade-memory",
        scope_id="scope-upgrade",
        platform="test",
        user_id="owner-fixture",
        chat_id="allowed-chat-fixture",
        thread_id="",
        gateway_session_key="",
        agent_identity="test-agent",
        agent_workspace="test-workspace",
        session_id="upgrade-session",
        source="fixture",
        target="memory",
        content="PRIVATE-PINNED-BUILD-WAL-SENTINEL",
        allow_duplicate=True,
    )
    identity = GenerationIdentity(
        backend="sqlite-bruteforce",
        provider="local-hash",
        model="hash-v1",
        dimensions=16,
        metric="cosine",
        prompt_profile="default-v1",
        table_name="memories",
    )
    current = bootstrap_legacy_generation(conn, identity=identity, row_count=0)
    conn.commit()

    original_factory = vector_migration.build_vector_store
    pinned_readers: list[sqlite3.Connection] = []
    passive_results: list[tuple[int, int, int]] = []

    def build_pinned_store(*args, **kwargs):
        store = original_factory(*args, **kwargs)
        original_close = store.close
        original_seal = getattr(store, "seal", None)
        reader_is_pinned = False

        def pin_reader() -> None:
            nonlocal reader_is_pinned
            if reader_is_pinned:
                return
            reader_is_pinned = True
            db_path = store.db_path
            reader = sqlite3.connect(db_path)
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM vector_records").fetchone()
            writer_conn = store._conn
            assert writer_conn is not None
            raw_checkpoint = tuple(
                int(value) for value in writer_conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            )
            assert len(raw_checkpoint) == 3
            passive_results.append(
                (raw_checkpoint[0], raw_checkpoint[1], raw_checkpoint[2])
            )
            pinned_readers.append(reader)

        def close_with_pinned_reader() -> None:
            pin_reader()
            original_close()

        store.close = close_with_pinned_reader
        if callable(original_seal):

            def seal_with_pinned_reader():
                pin_reader()
                return original_seal()

            store.seal = seal_with_pinned_reader
        return store

    monkeypatch.setattr(vector_migration, "build_vector_store", build_pinned_store)
    result = None
    failure: RuntimeError | None = None
    try:
        try:
            result = build_vector_generation(
                storage,
                conn,
                generation_id="ready-pinned-reader",
                identity=identity,
                embedder=LocalHashEmbedder(dimensions=16, model="hash-v1"),
                index_general=False,
                activate=False,
                expected_current=str(current["generation_id"]),
            )
        except RuntimeError as exc:
            failure = exc

        generation_root = storage / "vector-generations" / "ready-pinned-reader"
        db_path = generation_root / "vector.sqlite3"
        sidecars = sorted(
            path.name
            for path in (
                db_path.with_name(f"{db_path.name}-wal"),
                db_path.with_name(f"{db_path.name}-shm"),
            )
            if path.exists()
        )
        manifest_status = str(
            conn.execute(
                "SELECT status FROM vector_generations WHERE generation_id = ?",
                ("ready-pinned-reader",),
            ).fetchone()[0]
        )

        assert passive_results
        busy, log, checkpointed = passive_results[0]
        assert busy == 0
        assert log > 0
        assert checkpointed == log
        assert sidecars == ["vector.sqlite3-shm", "vector.sqlite3-wal"]
        assert failure is not None, (
            "SQLite generation incorrectly published despite pinned WAL/SHM: "
            f"result={result!r}, manifest_status={manifest_status!r}, sidecars={sidecars!r}"
        )
        assert "checkpoint" in str(failure).casefold() or "sidecar" in str(failure).casefold()
        assert manifest_status == "failed"
        assert not (generation_root / PREFLIGHT_RECEIPT_FILENAME).exists()
    finally:
        for reader in pinned_readers:
            reader.rollback()
            reader.close()
        conn.close()
