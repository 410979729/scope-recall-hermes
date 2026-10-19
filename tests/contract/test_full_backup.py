"""Contract tests for full backup bundles, rotation, deletion mirroring, and disaster recovery.

Tests:
1. Bundle completeness, manifest hashing, and credential redaction.
2. Tamper, corruption, and missing member detection.
3. Disaster restore recall equivalence to original synthetic store.
4. Post-backup deletion mirror replay preventing forgotten content resurrection.
5. Rebuild queuing when vector companions are omitted.
6. 7 daily and 4 weekly backup rotation count bounds.
7. Deletion resilience when mirror writes fail.
8. Immediate post-commit deletion mirroring across sub-transactions sharing a connection.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import zipfile
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from scope_recall.adapters.clients.config import install_codex_scope_recall
from scope_recall.contracts import InstanceBinding, TrustedContext
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.deletion_mirror import (
    DELETION_MIRROR_FILENAME,
    catchup_deletion_mirror,
)
from scope_recall.core.storage import SQLiteStorage, Transaction
from scope_recall.runtime.instance_config import RuntimeInstanceConfig
from scope_recall.maintenance.backup_bundle import (
    BackupBundleError,
    create_backup_bundle,
    verify_backup_bundle,
)
from scope_recall.maintenance.backup_rotation import (
    DAILY_LIMIT,
    WEEKLY_LIMIT,
    rotate_backup_bundles,
)
from scope_recall.maintenance.disaster_restore import (
    apply_disaster_restore,
    plan_disaster_restore,
)
from scope_recall.maintenance.doctor_report import DoctorReport
from scope_recall.maintenance.doctor_store import check_deletion_mirror
from v11_support import source_event


def _create_synthetic_store(root: Path) -> tuple[MemoryCore, TrustedContext, InstanceBinding]:
    project = root / "project"
    project.mkdir(parents=True, exist_ok=True)
    config, core = install_codex_scope_recall(root / "install", project_root=project, agent_id="agent1")
    scope = config.audience_scopes["owner_private"]
    binding = config.to_binding()
    ctx = TrustedContext(binding, "sess_001", frozenset({scope}), "human_direct")
    return core, ctx, binding


def _say(core: MemoryCore, ctx: TrustedContext, text: str, key: str = "msg1"):
    scope = next(iter(ctx.allowed_scope_ids))
    saved = core.record_event(
        ctx,
        source_event(source_event_key=f"evt-{key}", content=text),
        scope_id=scope,
        remaining_seconds=10,
    )
    return core.source(ctx, saved.event_refs[0].ref, 1)


def _forget(core: MemoryCore, ctx: TrustedContext, source):
    _say(core, ctx, f"删除 {source.ref}", key=f"cmd-del-{source.ref}")
    req = {
        "protocol_version": "1.1",
        "target_refs": [source.ref],
        "mode": "delete",
        "expected_revisions": {source.ref: source.revision},
    }
    return core.forget(ctx, req, remaining_seconds=10)


def test_bundle_completeness_and_secret_redaction(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    data_dir = binding.data_directory

    runtime_conf = {
        "binding": {
            "installation_id": binding.installation_id,
            "agent_id": binding.agent_id,
            "data_directory": str(data_dir),
            "scope_ids": sorted(binding.scope_ids),
        },
        "api_key": "sk-secret-token-value-12345",
        "nested": {
            "token": "bearer-token-abcde",
            "safe_param": "value_one",
        },
    }
    (data_dir / "runtime-config.json").write_text(json.dumps(runtime_conf), encoding="utf-8")

    retained_dir = data_dir / "retained"
    retained_dir.mkdir(parents=True, exist_ok=True)
    (retained_dir / "attachment_001.bin").write_bytes(b"synthetic-binary-payload")

    inbox_dir = data_dir / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    (inbox_dir / "queue_item.json").write_text('{"item": "pending_one"}', encoding="utf-8")

    bundle_dest = tmp_path / "backups" / "scope-recall-backup-20261019T140000Z.zip"
    receipt = create_backup_bundle(binding=binding, output=bundle_dest, with_vectors=False)

    assert receipt["status"] == "created"
    assert bundle_dest.is_file()
    assert receipt["secrets_redacted"] >= 2

    with zipfile.ZipFile(bundle_dest, "r") as zf:
        namelist = set(zf.namelist())
        assert "manifest.json" in namelist
        assert "database.sqlite3" in namelist
        assert "configs/runtime-config.json" in namelist
        assert "retained/attachment_001.bin" in namelist
        assert "inbox/queue_item.json" in namelist

        archived_conf = json.loads(zf.read("configs/runtime-config.json").decode("utf-8"))
        assert archived_conf["api_key"] == "[REDACTED_SECRET]"
        assert archived_conf["nested"]["token"] == "[REDACTED_SECRET]"
        assert archived_conf["nested"]["safe_param"] == "value_one"
        assert archived_conf["binding"]["agent_id"] == "agent1"


def test_tamper_and_missing_member_detection(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    bundle_dest = tmp_path / "bundle.zip"
    create_backup_bundle(binding=binding, output=bundle_dest)

    verify_receipt = verify_backup_bundle(bundle_dest)
    assert verify_receipt["status"] == "verified"

    tampered_dest = tmp_path / "tampered.zip"
    with zipfile.ZipFile(bundle_dest, "r") as src_zf, zipfile.ZipFile(tampered_dest, "w") as dst_zf:
        for item in src_zf.infolist():
            data = src_zf.read(item.filename)
            if item.filename == "database.sqlite3":
                data = data + b"\x00corrupt"
            dst_zf.writestr(item, data)

    with pytest.raises(BackupBundleError) as exc_info:
        verify_backup_bundle(tampered_dest)
    assert "checksum mismatch" in str(exc_info.value) or "size mismatch" in str(exc_info.value)

    missing_dest = tmp_path / "missing.zip"
    with zipfile.ZipFile(bundle_dest, "r") as src_zf, zipfile.ZipFile(missing_dest, "w") as dst_zf:
        for item in src_zf.infolist():
            if item.filename != "database.sqlite3":
                dst_zf.writestr(item, src_zf.read(item.filename))

    with pytest.raises(BackupBundleError) as exc_info:
        verify_backup_bundle(missing_dest)
    assert "missing" in str(exc_info.value)


def test_disaster_restore_recall_equivalence_and_path_rewriting(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    data_dir = binding.data_directory

    source = _say(core, ctx, "NameOne preferences: color blue", key="pref1")

    runtime_conf = {
        "binding": {
            "installation_id": binding.installation_id,
            "agent_id": binding.agent_id,
            "data_directory": str(data_dir),
            "scope_ids": sorted(binding.scope_ids),
            "test_mode": binding.test_mode,
            "installation_kind": binding.installation_kind,
        },
        "session_id": "session_001",
        "allowed_scope_ids": sorted(binding.scope_ids),
        "vector": {
            "backend": "lancedb",
            "storage_dir": str(data_dir / "vectors" / "default_space"),
            "table_name": "t1",
            "dimensions": 384,
            "test_injection_override": True,
        },
    }
    (data_dir / "runtime-config.json").write_text(json.dumps(runtime_conf), encoding="utf-8")

    bundle_dest = tmp_path / "disaster_backup.zip"
    create_backup_bundle(binding=binding, output=bundle_dest, with_vectors=False)

    fresh_target = tmp_path / "restored_store"
    plan = plan_disaster_restore(bundle_dest, fresh_target)
    assert plan["status"] == "planned"
    assert not fresh_target.exists()

    restore_receipt = apply_disaster_restore(bundle_dest, fresh_target)
    assert restore_receipt["status"] == "restored"
    assert fresh_target.is_dir()
    assert (fresh_target / "memory.sqlite3").is_file()

    with sqlite3.connect(fresh_target / "memory.sqlite3") as conn:
        meta_dir = conn.execute("SELECT data_directory FROM instance_meta WHERE singleton=1").fetchone()[0]
        assert Path(meta_dir).resolve() == fresh_target.resolve()

    restored_conf = json.loads((fresh_target / "runtime-config.json").read_text(encoding="utf-8"))
    restored_obj = RuntimeInstanceConfig.from_mapping(restored_conf)
    assert restored_obj.binding.data_directory.resolve() == fresh_target.resolve()
    assert restored_obj.vector.storage_dir.resolve() == (fresh_target / "vectors" / "default_space").resolve()

    restored_binding = InstanceBinding(
        agent_id=binding.agent_id,
        installation_id=binding.installation_id,
        data_directory=fresh_target,
        scope_ids=binding.scope_ids,
        test_mode=binding.test_mode,
        installation_kind=binding.installation_kind,
    )
    restored_core = MemoryCore(CoreConfig(restored_binding))
    restored_ctx = TrustedContext(
        restored_binding,
        "restored_session",
        ctx.allowed_scope_ids,
        "human_direct",
    )
    found = restored_core.source(restored_ctx, source.ref, 1)
    assert found is not None
    assert "NameOne preferences: color blue" in found.event["content"]

    results = restored_core.search_sources(restored_ctx, "NameOne preferences")
    assert len(results) >= 1
    assert any("NameOne preferences: color blue" in s.event["content"] for s in results)

    assert "unrecoverable_later_interval" in restore_receipt
    assert restore_receipt["unrecoverable_later_interval"]["start_utc"] == restore_receipt["recovery_point_utc"]


def test_post_backup_deletion_mirror_replay(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    data_dir = binding.data_directory
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    (data_dir / "backup-config.json").write_text(json.dumps({"backup_dir": str(backup_dir)}), encoding="utf-8")

    source = _say(core, ctx, "NameOne secret note", key="secret1")

    bundle_dest = backup_dir / "scope-recall-backup-snapshot.zip"
    create_backup_bundle(binding=binding, output=bundle_dest)

    _forget(core, ctx, source)

    catchup_receipt = catchup_deletion_mirror(core.storage, backup_dir)
    assert (backup_dir / DELETION_MIRROR_FILENAME).is_file()

    fresh_target = tmp_path / "restored_with_mirror"
    restore_receipt = apply_disaster_restore(
        bundle=bundle_dest,
        to=fresh_target,
        mirror=backup_dir / DELETION_MIRROR_FILENAME,
    )

    assert restore_receipt["status"] == "restored"
    assert restore_receipt["mirror_replayed_operations"] >= 1

    restored_binding = InstanceBinding(
        agent_id=binding.agent_id,
        installation_id=binding.installation_id,
        data_directory=fresh_target,
        scope_ids=binding.scope_ids,
        test_mode=binding.test_mode,
        installation_kind=binding.installation_kind,
    )
    restored_core = MemoryCore(CoreConfig(restored_binding))
    restored_ctx = TrustedContext(
        restored_binding,
        "restored_session",
        ctx.allowed_scope_ids,
        "human_direct",
    )
    assert restored_core.source(restored_ctx, source.ref, 1) is None
    results = restored_core.search_sources(restored_ctx, "NameOne secret note")
    assert not any(s.ref == source.ref for s in results)


@pytest.mark.parametrize("state", ["pending", "done"])
def test_no_vectors_queues_rebuild(tmp_path: Path, state):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    data_dir = binding.data_directory

    source = _say(core, ctx, "NameOne text requiring embedding rebuild", key="embed1")
    with core.storage.write(ctx) as tx:
        tx._check(write=True).execute("UPDATE work_items SET state=? WHERE work_type='embed'", (state,))

    bundle_dest = tmp_path / "bundle_no_vectors.zip"
    create_backup_bundle(binding=binding, output=bundle_dest, with_vectors=False)

    fresh_target = tmp_path / "restored_rebuild"
    restore_receipt = apply_disaster_restore(bundle_dest, fresh_target)

    assert restore_receipt["with_vectors"] is False
    assert restore_receipt["vector_rebuild_queued"] >= 1

    restored_binding = InstanceBinding(
        agent_id=binding.agent_id,
        installation_id=binding.installation_id,
        data_directory=fresh_target,
        scope_ids=binding.scope_ids,
        test_mode=binding.test_mode,
        installation_kind=binding.installation_kind,
    )
    restored_core = MemoryCore(CoreConfig(restored_binding))
    restored_ctx = TrustedContext(
        restored_binding,
        "restored_session",
        ctx.allowed_scope_ids,
        "human_direct",
    )
    found = restored_core.source(restored_ctx, source.ref, 1)
    assert found is not None
    assert "NameOne text requiring embedding rebuild" in found.event["content"]
    with restored_core.storage.read(restored_ctx) as tx:
        assert tx._check().execute("SELECT state FROM work_items WHERE work_type='embed'").fetchone()[0] == "pending"

    with sqlite3.connect(fresh_target / "memory.sqlite3") as conn:
        queued = conn.execute(
            "SELECT count(*) FROM work_items WHERE work_type='embed' AND subject_ref=?", (source.ref,)
        ).fetchone()[0]
        assert queued == 1


def test_backup_rotation_counts(tmp_path: Path):
    backup_dir = tmp_path / "rotation_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    base_time = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)

    created_files: list[str] = []
    for day_offset in range(14):
        dt = base_time - timedelta(days=day_offset)
        ts_slug = dt.strftime("%Y%m%dT%H%M%SZ")
        filename = f"scope-recall-backup-{ts_slug}.zip"
        file_path = backup_dir / filename
        file_path.write_bytes(b"dummy-zip-data")
        created_files.append(filename)

    assert len(list(backup_dir.glob("scope-recall-backup-*.zip"))) == 14

    result = rotate_backup_bundles(backup_dir, now=base_time)

    assert len(result.kept_daily) <= DAILY_LIMIT
    assert len(result.kept_weekly) <= WEEKLY_LIMIT
    assert result.retained_total <= DAILY_LIMIT + WEEKLY_LIMIT
    assert len(result.removed) > 0

    remaining = [p.name for p in backup_dir.glob("scope-recall-backup-*.zip")]
    assert len(remaining) == result.retained_total
    for removed_file in result.removed:
        assert not (backup_dir / removed_file).exists()


def test_deletion_resilience_when_mirror_fails(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    data_dir = binding.data_directory

    barrier_file = tmp_path / "barrier_file"
    barrier_file.write_text("file_not_dir", encoding="utf-8")
    invalid_backup_dir = barrier_file / "invalid_sub"
    (data_dir / "backup-config.json").write_text(json.dumps({"backup_dir": str(invalid_backup_dir)}), encoding="utf-8")

    source = _say(core, ctx, "NameOne persistent entry", key="persist1")

    result = _forget(core, ctx, source)
    assert result["read_blocked"] == 1 or result["read_blocked"] is True

    lag_file = data_dir / "deletion-mirror-lag.json"
    assert lag_file.is_file()
    lag_data = json.loads(lag_file.read_text(encoding="utf-8"))
    assert lag_data.get("failed") is True or lag_data.get("status") == "lagging"

    report = DoctorReport(host="hermes", status="ok")
    check_deletion_mirror(report, data_dir)
    assert "deletion_mirror_lag" in report.capability_gaps


def test_sub_transaction_deletion_immediately_mirrored_on_commit(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    data_dir = binding.data_directory
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    (data_dir / "backup-config.json").write_text(json.dumps({"backup_dir": str(backup_dir)}), encoding="utf-8")

    source = _say(core, ctx, "NameOne item deleted through sub transaction", key="subdel1")
    mirror_file = backup_dir / DELETION_MIRROR_FILENAME
    assert not mirror_file.is_file()

    with core.storage.write(ctx) as outer_tx:
        sub_ctx = TrustedContext(binding, "sub_sess_001", ctx.allowed_scope_ids, "human_direct")
        sub_tx = Transaction(outer_tx._check(write=True), sub_ctx, writable=True)
        req = {
            "protocol_version": "1.1",
            "target_refs": [source.ref],
            "mode": "delete",
            "expected_revisions": {source.ref: source.revision},
        }
        del_target = sub_tx.deletions.target(source.ref)
        closure = sub_tx.deletions.closure((del_target,), delete=True)
        sub_tx.deletions.block(req, closure, now=core.clock.utc_now())

    assert mirror_file.is_file()
    lines = [json.loads(line) for line in mirror_file.read_text(encoding="utf-8").strip().splitlines() if line]
    assert len(lines) == 1
    assert lines[0]["operation"]["mode"] == "delete"
    assert source.ref in lines[0]["operation"]["requested_refs_json"]


def _listed(path: str, data: bytes) -> dict[str, object]:
    return {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}


def _zip_entry(name: str, data: bytes, offset: int) -> tuple[bytes, bytes, int]:
    """One stored zip member, keeping ``name`` exactly, plus the next local offset."""
    encoded = name.encode("utf-8")
    crc = zlib.crc32(data) & 0xFFFFFFFF
    local = (
        struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50,
            20,
            0,
            0,
            0,
            0,
            crc,
            len(data),
            len(data),
            len(encoded),
            0,
        )
        + encoded
        + data
    )
    central = struct.pack(
        "<IHHHHHHIIIHHHHHII",
        0x02014B50,
        20,
        20,
        0,
        0,
        0,
        0,
        crc,
        len(data),
        len(data),
        len(encoded),
        0,
        0,
        0,
        0,
        0,
        offset,
    ) + encoded
    return local, central, offset + len(local)


def _repack(
    target: Path,
    manifest: dict[str, object],
    payload: dict[str, bytes],
    *,
    duplicate: str | None = None,
) -> None:
    """Write a zip whose entry names are the given strings, including unsafe ones."""
    entries = [("manifest.json", json.dumps(manifest).encode("utf-8"))]
    for name, data in payload.items():
        entries.append((name, data))
        if name == duplicate:
            entries.append((name, data))
    parts: list[bytes] = []
    central: list[bytes] = []
    offset = 0
    for name, data in entries:
        local, directory, offset = _zip_entry(name, data, offset)
        parts.append(local)
        central.append(directory)
    body = b"".join(parts)
    directory = b"".join(central)
    end = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(entries), len(entries), len(directory), len(body), 0)
    target.write_bytes(body + directory + end)
    with zipfile.ZipFile(target, "r") as archive:
        names = archive.namelist()
    if duplicate is not None and names.count(duplicate) != 2:
        raise AssertionError(f"{duplicate} was not stored twice")
    for name, _data in entries:
        if name not in names:
            raise AssertionError(f"{name} was not stored as an archive member")


def _untouched(sentinel: Path, original: bytes, dest: Path) -> None:
    assert sentinel.read_bytes() == original
    assert not dest.exists()


@pytest.mark.parametrize("kind", ["parent", "absolute", "collision", "duplicate", "manifest"])
def test_unsafe_archive_members_do_not_escape(tmp_path: Path, kind: str):
    core, _ctx, binding = _create_synthetic_store(tmp_path)
    bundle = tmp_path / "bundle.zip"
    create_backup_bundle(binding=binding, output=bundle)
    with zipfile.ZipFile(bundle, "r") as archive:
        manifest = json.loads(archive.read("manifest.json"))
        payload = {
            info.filename: archive.read(info.filename)
            for info in archive.infolist()
            if info.filename != "manifest.json"
        }
    sentinel = tmp_path / "victim"
    sentinel.write_bytes(b"sentinel")
    original = sentinel.read_bytes()
    dest = tmp_path / "restore-target"
    duplicate = None
    if kind == "parent":
        payload["../victim"] = b"escaped"
        expected = ".."
    elif kind == "absolute":
        payload[sentinel.resolve().as_posix()] = b"escaped"
        expected = "drive"
    elif kind == "collision":
        blob = b"not-the-database"
        payload["configs/memory.sqlite3"] = blob
        manifest["files"] = [*manifest["files"], _listed("configs/memory.sqlite3", blob)]
        expected = "same destination"
    elif kind == "duplicate":
        duplicate = "database.sqlite3"
        expected = "duplicate"
    else:
        manifest["files"] = [*manifest["files"], _listed("manifest.json", b"{}")]
        expected = "manifest.json is listed as a payload member"
    bad = tmp_path / f"bad-{kind}.zip"
    _repack(bad, manifest, payload, duplicate=duplicate)

    with pytest.raises(BackupBundleError) as verify_error:
        verify_backup_bundle(bad)
    assert expected in str(verify_error.value)
    _untouched(sentinel, original, dest)

    with pytest.raises(BackupBundleError) as restore_error:
        apply_disaster_restore(bad, dest)
    assert expected in str(restore_error.value)
    _untouched(sentinel, original, dest)
    assert core.storage.path.is_file()


def test_normal_bundle_restore_stays_inside_destination(tmp_path: Path):
    core, ctx, binding = _create_synthetic_store(tmp_path)
    source = _say(core, ctx, "NameOne stays inside the restore root", key="inside")
    bundle = tmp_path / "good.zip"
    create_backup_bundle(binding=binding, output=bundle)
    sentinel = tmp_path / "victim"
    sentinel.write_bytes(b"sentinel")
    dest = tmp_path / "restored-good"
    verify_backup_bundle(bundle)
    receipt = apply_disaster_restore(bundle, dest)

    assert receipt["status"] == "restored"
    assert sentinel.read_bytes() == b"sentinel"
    assert (dest / "memory.sqlite3").is_file()
    assert not (tmp_path / "memory.sqlite3").exists()
    restored = InstanceBinding(
        binding.agent_id,
        binding.installation_id,
        dest,
        binding.scope_ids,
        binding.test_mode,
        binding.installation_kind,
    )
    restored_core = MemoryCore(CoreConfig(restored))
    restored_ctx = TrustedContext(restored, "restored_session", ctx.allowed_scope_ids, "human_direct")
    found = restored_core.source(restored_ctx, source.ref, 1)
    assert found is not None
    assert "NameOne stays inside the restore root" in found.event["content"]
