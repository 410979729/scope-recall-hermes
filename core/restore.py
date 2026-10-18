"""Explicit closed restore admission; no automatic import during normal reads.

The ``restore`` maintenance command (``maintenance/restore.py``) drives it: the
ledger is exported from the latest authorized store, never from the snapshot
being restored, admission is closed, the snapshot is copied over the store and
the ledger is replayed. Admission stays closed from the marker's write until the
replay has truncated the write-ahead log, with the ledger kept beside the marker
all along, so that a run stopped anywhere in between is finished by running it
again. Ordinary open paths cannot clear the marker or accept a ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from ..contracts import ContractError, TrustedContext
from .delete_storage import OBJECT_TABLES, DeleteTarget, canonical, purge_work_ref
from .retained_artifacts import extended_path
from .schema import SCHEMA_VERSION

#: Beside ``memory.sqlite3`` while a restore is pending; every normal open refuses while it exists.
MARKER_FILENAME = "restore-required.json"
#: Beside the marker: the ledger the pending restore replays, written whole before the marker is.  It stays once the
#: restore is done, content-free, until the next restore writes its own; every release that reads the format below
#: finds it here.
LEDGER_FILENAME = "restore-ledger.json"
#: What a ledger holds, by name.  Every release that exports this format exports the same sections with the same
#: fields, and a release replays a ledger of this format whatever schema the exporting release wrote: a ledger
#: exported where the latest store lives is replayed by the release that reads the snapshot.  A release whose
#: deletions need anything this format does not carry exports another one, which the releases before it refuse.
LEDGER_FORMAT = "scope-recall-deletion-ledger/1"


@dataclass(frozen=True)
class InstallationMaintenance:
    """Runtime capability minted only by an explicit installation maintenance entry.

    This is deliberately not a model DTO or an ordinary project context. Only
    the maintenance commands create it; source, recall and tool adapters never do.
    """

    context: TrustedContext


def _maintenance_context(storage, authority):
    if not isinstance(authority, InstallationMaintenance) or not isinstance(authority.context, TrustedContext):
        raise ContractError("ACCESS_DENIED", "restore_authority")
    context = authority.context
    storage._context_check(context)
    storage._path_check()
    if context.allowed_scope_ids != storage.binding.scope_ids or context.actor_origin not in {
        "human_direct",
        "host_generated",
    }:
        raise ContractError("ACCESS_DENIED", "restore_authority")
    return context


def ledger_digest(ledger) -> str:
    return hashlib.sha256(canonical(ledger).encode()).hexdigest()


def _ledger_in_transaction(tx, context) -> dict:
    conn = tx._check()
    operations = []
    for row in conn.execute("SELECT * FROM deletion_operations ORDER BY memory_epoch,operation_id"):
        if not set(json.loads(row["scope_ids_json"])) <= context.allowed_scope_ids:
            raise ContractError("ACCESS_DENIED")
        members = [
            dict(r)
            for r in conn.execute(
                """SELECT b.object_kind,b.object_ref,b.scope_id,b.project_id,b.branch_id
            FROM deletion_members m JOIN object_blocks b USING(object_kind,object_ref) WHERE m.operation_id=? ORDER BY object_kind,object_ref""",
                (row["operation_id"],),
            )
        ]
        operations.append(dict(operation=dict(row), members=members))
    withdrawals = [
        dict(row)
        for row in conn.execute("""SELECT c.claim_id,c.scope_id,c.project_id,c.branch_id,c.current_revision,
        v.valid_from,v.valid_to,v.recorded_from,v.state,json_extract(v.payload_json,'$.intention.state') AS intention_state
        FROM claims c JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revision=c.current_revision
        WHERE (v.state='retracted' OR json_extract(v.payload_json,'$.intention.state') IN ('cancelled','completed','expired'))
        AND c.read_blocked=0 ORDER BY c.claim_id""")
    ]
    return dict(
        format=LEDGER_FORMAT,
        schema_version=SCHEMA_VERSION,
        memory_epoch=tx.status().memory_epoch,
        installation_id=context.binding.installation_id,
        agent_id=context.binding.agent_id,
        scope_ids=sorted(context.allowed_scope_ids),
        operations=operations,
        withdrawals=withdrawals,
        source_groups=[dict(row) for row in conn.execute("SELECT * FROM source_group_blocks ORDER BY group_sha256")],
        absent_objects=[
            dict(row) for row in conn.execute("SELECT * FROM restored_absence_blocks ORDER BY object_kind,object_ref")
        ],
    )


def export_deletion_ledger(storage, authority: InstallationMaintenance) -> dict:
    context = _maintenance_context(storage, authority)
    with storage.read(context) as tx:
        return _ledger_in_transaction(tx, context)


def _digest_shaped(value) -> bool:
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _sync_directory(directory: Path) -> None:
    """Make a rename or a removal in ``directory`` durable where a directory can be synced: on POSIX, by ``fsync`` of
    the directory.  Windows has none to sync; ``_write_whole`` moves files there with write-through instead."""
    if sys.platform == "win32":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def restore_pending(data_directory: Path) -> bool:
    """Whether a restore's marker is beside the store, whole or not: every open of the store refuses while it is.

    On Windows the marker is looked for, as it is written, by its extended-length name, to which the 260-character
    path limit does not apply.
    """
    return os.path.lexists(extended_path(data_directory / MARKER_FILENAME))


def _write_whole(path: Path, text: str) -> None:
    """``text`` written to ``path`` through a file of its own beside it, synced, then moved over it on the disk.

    A crash leaves the earlier file or the whole new one, never part of one.  On Windows the move is ``MoveFileExW``
    with ``MOVEFILE_WRITE_THROUGH``, which returns only once the file is moved on the disk, where ``os.replace``
    leaves the flag out; elsewhere it is ``os.replace``, then ``fsync`` of the directory.  On Windows both files are
    named in the extended-length form, to which the 260-character path limit does not apply.
    """
    staged = extended_path(path.with_name(f".{path.name}.{os.urandom(8).hex()}.tmp"))
    target = extended_path(path)
    try:
        with staged.open("x", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if sys.platform == "win32":
            import ctypes  # Loaded here: every open of a store imports this module, and only a restore writes files.
            from ctypes import wintypes

            move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
            move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
            move.restype = wintypes.BOOL
            if not move(str(staged), str(target), 0x1 | 0x8):  # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            os.replace(staged, target)
            _sync_directory(path.parent)
    finally:
        staged.unlink(missing_ok=True)


def _kept_ledger(storage) -> dict | None:
    """The ledger kept beside the store (``LEDGER_FILENAME``), or None when none can be read."""
    path = extended_path(storage.binding.data_directory / LEDGER_FILENAME)
    if path.is_symlink() or not path.is_file():
        return None
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return ledger if isinstance(ledger, dict) else None


def _closable(storage, context, ledger) -> bool:
    """Whether closing admission on ``ledger`` writes the marker; False when the store is closed on it already.

    No marker, or one its writer left cut short (``checkpoint_unreadable``), is written: a restore copies a snapshot
    over the store only once its marker is whole, so the store is still the one being closed.  Not when a ledger newer
    than the store's present state is kept beside a cut-short marker: the store was replaced after it was closed, and
    only that ledger holds what was forgotten since (``marker_damaged``).  A whole marker of another ledger refuses:
    the store is closed on that ledger (``restore_already_pending`` when it is kept, ``ledger_missing`` when not).
    """
    if not restore_pending(storage.binding.data_directory):
        return True
    kept = _kept_ledger(storage)
    digest = ledger_digest(ledger)
    try:
        _, pending = _pending_marker(storage, context)
    except ContractError as exc:
        if exc.field != "checkpoint_unreadable":
            raise
        epoch = None if kept is None else kept.get("memory_epoch")
        if type(epoch) is int and ledger_digest(kept) != digest and epoch >= ledger["memory_epoch"]:
            raise ContractError("RESTORE_UNVERIFIED", "marker_damaged") from None
        return True
    if pending["expected_ledger_sha256"] == digest:
        return False
    if kept is not None and ledger_digest(kept) == pending["expected_ledger_sha256"]:
        raise ContractError("RESTORE_UNVERIFIED", "restore_already_pending")
    raise ContractError("RESTORE_UNVERIFIED", "ledger_missing")


def begin_restore(storage, authority: InstallationMaintenance, *, expected_ledger_sha256: str) -> None:
    """Close admission on the store's ledger, the one ``expected_ledger_sha256`` names.

    The ledger is written beside the store first and the marker after it, each whole (``_write_whole``): once the
    marker is there, so is the ledger it names, and a restore copies a snapshot over the store only after both.  A
    store closed on this ledger already gets its ledger file written again; a marker cut short is written again when
    the store is still the one it was closing (``_closable``).
    """
    context = _maintenance_context(storage, authority)
    if not _digest_shaped(expected_ledger_sha256):
        raise ContractError("RESTORE_UNVERIFIED", "checkpoint_required")
    data_directory = storage.binding.data_directory
    payload = dict(
        installation_id=context.binding.installation_id,
        agent_id=context.binding.agent_id,
        data_directory=str(context.binding.data_directory),
        expected_ledger_sha256=expected_ledger_sha256,
    )
    # The same writer lease and transaction as ordinary mutations, so the independently supplied checkpoint cannot go
    # stale before admission closes; through the fence, so that a store closed already can be closed again.
    with storage._transaction(context, writable=True, remaining_seconds=None, restoring=True) as tx:
        ledger = _ledger_in_transaction(tx, context)
        if ledger_digest(ledger) != expected_ledger_sha256:
            raise ContractError("RESTORE_UNVERIFIED", "checkpoint_changed")
        write_marker = _closable(storage, context, ledger)
        _write_whole(data_directory / LEDGER_FILENAME, canonical(ledger) + "\n")
        if write_marker:
            _write_whole(data_directory / MARKER_FILENAME, canonical(payload) + "\n")


def _pending_marker(storage, context):
    """The pending restore's marker and what it holds.

    Refused with ``checkpoint_missing`` when there is none, ``marker_path`` when it is not a plain file,
    ``checkpoint_unreadable`` when it holds no whole marker, and ``identity`` when it names another installation.
    """
    marker = storage.binding.data_directory / MARKER_FILENAME
    if not restore_pending(storage.binding.data_directory):
        raise ContractError("RESTORE_UNVERIFIED", "checkpoint_missing")
    found = extended_path(marker)
    if found.is_symlink() or not found.is_file():
        raise ContractError("RESTORE_UNVERIFIED", "marker_path")
    try:
        pending = json.loads(found.read_text(encoding="utf-8")) if found.stat().st_size <= 4096 else None
    except ValueError:
        pending = None
    if (
        not isinstance(pending, dict)
        or any(type(pending.get(field)) is not str for field in ("installation_id", "agent_id", "data_directory"))
        or not _digest_shaped(pending.get("expected_ledger_sha256"))
    ):
        raise ContractError("RESTORE_UNVERIFIED", "checkpoint_unreadable")
    if (pending["installation_id"], pending["agent_id"], pending["data_directory"]) != (
        context.binding.installation_id,
        context.binding.agent_id,
        str(context.binding.data_directory),
    ):
        raise ContractError("RESTORE_UNVERIFIED", "identity")
    return marker, pending


def _restore_checkpoint(storage, context, ledger):
    """The pending restore's marker and the ledger's checkpoint epoch, once the marker, the ledger and this
    installation agree."""
    marker, pending = _pending_marker(storage, context)
    if ledger_digest(ledger) != pending.get("expected_ledger_sha256"):
        raise ContractError("RESTORE_UNVERIFIED", "ledger_incomplete")
    sections = {
        "format",
        "schema_version",
        "memory_epoch",
        "installation_id",
        "agent_id",
        "scope_ids",
        "operations",
        "withdrawals",
        "source_groups",
        "absent_objects",
    }
    # Whatever schema the exporting release wrote: the format names what the ledger holds.
    if set(ledger) != sections or ledger["format"] != LEDGER_FORMAT or type(ledger["schema_version"]) is not int:
        raise ContractError("RESTORE_UNVERIFIED", "ledger_format")
    if (ledger["installation_id"], ledger["agent_id"], ledger["scope_ids"]) != (
        context.binding.installation_id,
        context.binding.agent_id,
        sorted(context.allowed_scope_ids),
    ):
        raise ContractError("RESTORE_UNVERIFIED", "ledger_identity")
    checkpoint_epoch = ledger["memory_epoch"]
    if type(checkpoint_epoch) is not int or not 0 <= checkpoint_epoch < 9223372036854775807:
        raise ContractError("RESTORE_UNVERIFIED", "ledger_epoch")
    return marker, checkpoint_epoch


def pending_restore_ledger(storage, authority: InstallationMaintenance) -> tuple[dict, bool]:
    """The ledger the pending restore replays, and whether it is the one kept beside its marker.

    The kept ledger, whichever release exported it, once the marker names it and its format and this installation
    accept it.  Otherwise the store's own ledger, read through the fence, on which ``begin_restore`` then closes the
    store again, when ``_closable`` allows it.  Refused with ``RESTORE_UNVERIFIED`` when neither holds; nothing is
    written.
    """
    context = _maintenance_context(storage, authority)
    kept = _kept_ledger(storage)
    try:
        _, pending = _pending_marker(storage, context)
    except ContractError as exc:
        if exc.field != "checkpoint_unreadable":
            raise
        pending = None
    if pending is not None and kept is not None and ledger_digest(kept) == pending["expected_ledger_sha256"]:
        _restore_checkpoint(storage, context, kept)
        return kept, True
    with storage._transaction(context, writable=False, remaining_seconds=None, restoring=True) as tx:
        ledger = _ledger_in_transaction(tx, context)
    _closable(storage, context, ledger)
    return ledger, False


def _replay_operation(tx, conn, context, entry, checkpoint_epoch) -> int:
    """Replay one deletion operation of the ledger: the operation, its members' tombstones and, for a delete, the
    purge.  Returns how many objects it names."""
    if set(entry) != {"operation", "members"}:
        raise ContractError("RESTORE_UNVERIFIED", "operation_shape")
    row = entry["operation"]
    fields = tuple(row)
    allowed_fields = {
        "operation_id",
        "request_sha256",
        "mode",
        "scope_ids_json",
        "project_id",
        "branch_id",
        "requested_refs_json",
        "expected_revisions_json",
        "created_at",
        "memory_epoch",
        "layers_json",
        "active_content_removed",
    }
    if (
        set(fields) != allowed_fields
        or row["mode"] not in {"delete", "suppress"}
        or not set(json.loads(row["scope_ids_json"])) <= context.allowed_scope_ids
    ):
        raise ContractError("RESTORE_UNVERIFIED", "operation_shape")
    if type(row["memory_epoch"]) is not int or not 0 <= row["memory_epoch"] <= checkpoint_epoch:
        raise ContractError("RESTORE_UNVERIFIED", "operation_epoch")
    # The latest ledger's physical-erasure status is not evidence about
    # this older file. Replayed deletion starts physical cleanup again.
    row = dict(row)
    layers = json.loads(row["layers_json"])
    if row["mode"] == "delete":
        layers.update(
            sqlite_active="pending",
            sqlite_history="pending",
            vector_active="inventory_pending",
            vector_history="inventory_pending",
            attachments="inventory_pending",
        )
    row.update(active_content_removed=0, layers_json=canonical(layers))
    conn.execute(
        f"INSERT INTO deletion_operations({','.join(fields)}) VALUES ({','.join('?' for _ in fields)}) ON CONFLICT(operation_id) DO NOTHING",
        tuple(row[f] for f in fields),
    )
    if row["mode"] == "delete":
        conn.execute(
            "UPDATE deletion_operations SET active_content_removed=0,layers_json=? WHERE operation_id=?",
            (row["layers_json"], row["operation_id"]),
        )
    targets = []
    for member in entry["members"]:
        if set(member) != {"object_kind", "object_ref", "scope_id", "project_id", "branch_id"}:
            raise ContractError("RESTORE_UNVERIFIED", "member_shape")
        if member["scope_id"] not in context.allowed_scope_ids or member["object_kind"] not in OBJECT_TABLES:
            raise ContractError("RESTORE_UNVERIFIED", "member_scope")
        targets.append(
            DeleteTarget(
                member["object_kind"],
                member["object_ref"],
                1,
                member["scope_id"],
                member["project_id"],
                member["branch_id"],
            )
        )
    # Tombstones are applied even to objects not yet present in the old
    # snapshot, so a later replay cannot resurrect them.
    tx.deletions.apply_blocks(row["operation_id"], targets, delete=row["mode"] == "delete")
    if row["mode"] == "delete":
        _replay_purge(tx, conn, context, row)
    return len(targets)


def _replay_purge(tx, conn, context, row) -> None:
    """Purge a replayed delete's rows and queue its physical cleanup again, in each of its scopes."""
    # Purge method checks the operation's owning project; use a
    # context restricted to that actual project during replay.
    previous_context = tx.context
    try:
        tx.context = replace(context, project_id=row["project_id"], branch_id=row["branch_id"])
        tx.deletions.purge_sqlite(row["operation_id"])
        # An older snapshot may predate the purge work itself or
        # contain a completed lease. Explicit restore creates a new
        # physical cleanup obligation and fences old workers.
        for scope in sorted(json.loads(row["scope_ids_json"])):
            conn.execute(
                """INSERT INTO work_items(
                work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,available_at)
                VALUES ('purge',?,1,?,?,?,?)
                ON CONFLICT(work_type,subject_ref,subject_revision) DO UPDATE SET
                state='pending',attempt=0,available_at=excluded.available_at,
                lease_token=work_items.lease_token+1,lease_owner=NULL,lease_until=NULL,last_error_code=NULL""",
                (
                    purge_work_ref(row["operation_id"], scope),
                    scope,
                    row["project_id"],
                    row["branch_id"],
                    row["created_at"],
                ),
            )
    finally:
        tx.context = previous_context


def _replay_source_group(conn, context, group) -> None:
    """Replay one source group's read and suppression blocks; a block only ever grows."""
    if (
        set(group)
        != {"group_sha256", "scope_id", "project_id", "branch_id", "read_blocked", "suppressed", "operation_id"}
        or group["scope_id"] not in context.allowed_scope_ids
    ):
        raise ContractError("RESTORE_UNVERIFIED", "source_group")
    conn.execute(
        """INSERT INTO source_group_blocks(group_sha256,scope_id,project_id,branch_id,read_blocked,suppressed,operation_id)
        VALUES (?,?,?,?,?,?,?) ON CONFLICT(group_sha256) DO UPDATE SET
        read_blocked=max(source_group_blocks.read_blocked,excluded.read_blocked),suppressed=max(source_group_blocks.suppressed,excluded.suppressed)""",
        tuple(
            group[k]
            for k in (
                "group_sha256",
                "scope_id",
                "project_id",
                "branch_id",
                "read_blocked",
                "suppressed",
                "operation_id",
            )
        ),
    )


def _replay_withdrawal(conn, context, ledger, withdrawal) -> None:
    """Replay one withdrawn claim: its later revision when the restored file holds an older one, else a block on the
    claim the file does not hold."""
    if set(withdrawal) != {
        "claim_id",
        "scope_id",
        "project_id",
        "branch_id",
        "current_revision",
        "valid_from",
        "valid_to",
        "recorded_from",
        "state",
        "intention_state",
    }:
        raise ContractError("RESTORE_UNVERIFIED", "withdrawal_shape")
    if withdrawal["scope_id"] not in context.allowed_scope_ids:
        raise ContractError("RESTORE_UNVERIFIED", "withdrawal_scope")
    old = conn.execute(
        """SELECT c.current_revision,v.payload_json FROM claims c JOIN claim_versions v
        ON v.claim_id=c.claim_id AND v.revision=c.current_revision WHERE c.claim_id=? AND c.read_blocked=0""",
        (withdrawal["claim_id"],),
    ).fetchone()
    if old is not None and old["current_revision"] < withdrawal["current_revision"]:
        payload = json.loads(old["payload_json"])
        if withdrawal["intention_state"] in {"cancelled", "completed", "expired"}:
            if "intention" not in payload:
                raise ContractError("RESTORE_UNVERIFIED", "intention_shape")
            payload["intention"]["state"] = withdrawal["intention_state"]
        conn.execute(
            """INSERT INTO claim_versions(claim_id,revision,payload_json,state,basis,qualification_reason,
            valid_from,valid_to,recorded_from,replaces_revision) VALUES (?,?,?,?,'unknown','restored_governance_ledger',?,?,?,?)""",
            (
                withdrawal["claim_id"],
                withdrawal["current_revision"],
                canonical(payload),
                withdrawal["state"],
                withdrawal["valid_from"],
                withdrawal["valid_to"],
                withdrawal["recorded_from"],
                old["current_revision"],
            ),
        )
        conn.execute(
            "UPDATE claim_versions SET recorded_to=? WHERE claim_id=? AND revision=?",
            (withdrawal["recorded_from"], withdrawal["claim_id"], old["current_revision"]),
        )
        conn.execute(
            "UPDATE claims SET current_revision=? WHERE claim_id=?",
            (withdrawal["current_revision"], withdrawal["claim_id"]),
        )
        conn.execute(
            "UPDATE work_items SET state='obsolete',lease_token=lease_token+1 WHERE subject_ref=? AND state IN ('pending','leased')",
            (withdrawal["claim_id"],),
        )
    elif old is None:
        conn.execute(
            """INSERT INTO restored_absence_blocks(object_kind,object_ref,scope_id,project_id,branch_id,checkpoint_sha256,reason)
            VALUES ('claim',?,?,?,?,?,'governance_body_missing_from_snapshot') ON CONFLICT DO NOTHING""",
            (
                withdrawal["claim_id"],
                withdrawal["scope_id"],
                withdrawal["project_id"],
                withdrawal["branch_id"],
                ledger_digest(ledger),
            ),
        )


def _replay_absence(conn, context, absent) -> None:
    """Replay one block on an object the checkpoint says is absent."""
    fields = {"object_kind", "object_ref", "scope_id", "project_id", "branch_id", "checkpoint_sha256", "reason"}
    if set(absent) != fields:
        raise ContractError("RESTORE_UNVERIFIED", "absent_shape")
    if absent["scope_id"] not in context.allowed_scope_ids or absent["object_kind"] != "claim":
        raise ContractError("RESTORE_UNVERIFIED", "absent_scope")
    conn.execute(
        """INSERT INTO restored_absence_blocks(object_kind,object_ref,scope_id,project_id,branch_id,checkpoint_sha256,reason)
        VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
        tuple(
            absent[k]
            for k in (
                "object_kind",
                "object_ref",
                "scope_id",
                "project_id",
                "branch_id",
                "checkpoint_sha256",
                "reason",
            )
        ),
    )


def replay_deletion_ledger(
    storage, authority: InstallationMaintenance, ledger, *, remaining_seconds: float = 10.0
) -> dict:
    context = _maintenance_context(storage, authority)
    marker, checkpoint_epoch = _restore_checkpoint(storage, context, ledger)
    count = 0
    with storage._transaction(context, writable=True, remaining_seconds=remaining_seconds, restoring=True) as tx:
        conn = tx._check(write=True)
        # Transient input captured before this explicit restore cannot be
        # treated as fresh post-restore authorization. Rebuild partial derived
        # summaries from the restored source, with its normal tombstone checks.
        conn.execute("DELETE FROM capture_inbox")
        conn.execute("DELETE FROM consolidation_fragments")
        conn.execute(
            "UPDATE work_items SET consolidation_offset=0 WHERE state IN ('pending','leased') AND work_type='consolidate'"
        )
        for entry in ledger["operations"]:
            count += _replay_operation(tx, conn, context, entry, checkpoint_epoch)
        for group in ledger["source_groups"]:
            _replay_source_group(conn, context, group)
        for withdrawal in ledger["withdrawals"]:
            _replay_withdrawal(conn, context, ledger, withdrawal)
        for absent in ledger["absent_objects"]:
            _replay_absence(conn, context, absent)
        # Restoring an older file must never reuse a previously released cache
        # epoch. The independently obtained latest checkpoint is the floor.
        conn.execute(
            "UPDATE instance_meta SET memory_epoch=max(memory_epoch,?)+1 WHERE singleton=1", (checkpoint_epoch,)
        )
    # The write-ahead log holds the snapshot's pages, deleted words included, until it is truncated, so admission
    # reopens only after that.  A reader still on an older snapshot holds the truncation back (wal_busy); a crash
    # before the unlink below leaves admission closed too, and replaying again is safe.
    if not storage.truncate_wal(context, remaining_seconds=remaining_seconds, restoring=True):
        raise ContractError("RESTORE_UNVERIFIED", "wal_busy")
    storage._path_check()
    found = extended_path(marker)
    if found.is_symlink() or marker.parent.resolve() != storage.binding.data_directory.resolve():
        raise ContractError("RESTORE_UNVERIFIED", "marker_path")
    found.unlink()
    _sync_directory(marker.parent)
    return dict(
        status="deletion_ledger_replayed",
        affected_objects=count,
        checkpoint_sha256=ledger_digest(ledger),
        boundary="deletion and withdrawal admission only; the snapshot's integrity and identity are the caller's",
    )
