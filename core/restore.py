"""Explicit closed restore admission; no automatic import during normal reads.

P15 wraps this with the backup/copy/identity-selection CLI. A checkpoint must be
obtained independently from the latest authorized store, not from the old backup
being restored. Ordinary open paths cannot clear the marker or accept a ledger.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

from ..contracts import ContractError, TrustedContext
from .delete_storage import DeleteTarget, canonical,OBJECT_TABLES
from .schema import SCHEMA_VERSION


@dataclass(frozen=True)
class InstallationMaintenance:
    """Runtime capability minted only by an explicit installation maintenance entry.

    This is deliberately not a model DTO or an ordinary project context. P15's
    maintenance CLI owns creation; normal source/recall/tool adapters must not.
    """
    context: TrustedContext


def _maintenance_context(storage,authority):
    if not isinstance(authority,InstallationMaintenance) or not isinstance(authority.context,TrustedContext):
        raise ContractError("ACCESS_DENIED","restore_authority")
    context = authority.context
    storage._context_check(context)
    storage._path_check()
    if context.allowed_scope_ids != storage.binding.scope_ids or context.actor_origin not in {"human_direct","host_generated"}:
        raise ContractError("ACCESS_DENIED","restore_authority")
    return context


def ledger_digest(ledger) -> str:
    return hashlib.sha256(canonical(ledger).encode()).hexdigest()


def _ledger_in_transaction(tx, context) -> dict:
    conn = tx._check()
    operations = []
    for row in conn.execute("SELECT * FROM deletion_operations ORDER BY memory_epoch,operation_id"):
        if not set(json.loads(row["scope_ids_json"])) <= context.allowed_scope_ids:
            raise ContractError("ACCESS_DENIED")
        members = [dict(r) for r in conn.execute("""SELECT b.object_kind,b.object_ref,b.scope_id,b.project_id,b.branch_id
            FROM deletion_members m JOIN object_blocks b USING(object_kind,object_ref) WHERE m.operation_id=? ORDER BY object_kind,object_ref""",(row["operation_id"],))]
        operations.append(dict(operation=dict(row),members=members))
    withdrawals = [dict(row) for row in conn.execute("""SELECT c.claim_id,c.scope_id,c.project_id,c.branch_id,c.current_revision,
        v.valid_from,v.valid_to,v.recorded_from,v.state,json_extract(v.payload_json,'$.intention.state') AS intention_state
        FROM claims c JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revision=c.current_revision
        WHERE (v.state='retracted' OR json_extract(v.payload_json,'$.intention.state') IN ('cancelled','completed','expired'))
        AND c.read_blocked=0 ORDER BY c.claim_id""")]
    return dict(format="scope-recall-deletion-ledger/1",schema_version=SCHEMA_VERSION,
                memory_epoch=tx.status().memory_epoch,
                installation_id=context.binding.installation_id,agent_id=context.binding.agent_id,
                scope_ids=sorted(context.allowed_scope_ids),operations=operations,withdrawals=withdrawals,
                source_groups=[dict(row) for row in conn.execute("SELECT * FROM source_group_blocks ORDER BY group_sha256")],
                absent_objects=[dict(row) for row in conn.execute("SELECT * FROM restored_absence_blocks ORDER BY object_kind,object_ref")])


def export_deletion_ledger(storage,authority: InstallationMaintenance) -> dict:
    context = _maintenance_context(storage,authority)
    with storage.read(context) as tx:
        return _ledger_in_transaction(tx, context)


def begin_restore(storage,authority: InstallationMaintenance,*,expected_ledger_sha256: str) -> None:
    context = _maintenance_context(storage,authority)
    if type(expected_ledger_sha256) is not str or len(expected_ledger_sha256)!=64 or any(c not in "0123456789abcdef" for c in expected_ledger_sha256):
        raise ContractError("RESTORE_UNVERIFIED","checkpoint_required")
    marker = storage.binding.data_directory / "restore-required.json"
    payload = dict(installation_id=context.binding.installation_id,agent_id=context.binding.agent_id,
                   data_directory=str(context.binding.data_directory),expected_ledger_sha256=expected_ledger_sha256)
    # Take the same writer lease/transaction as ordinary mutations, so the
    # independently supplied checkpoint cannot go stale before admission closes.
    with storage.write(context) as tx:
        if marker.exists() or marker.is_symlink():
            raise ContractError("RESTORE_UNVERIFIED","restore_already_pending")
        if ledger_digest(_ledger_in_transaction(tx, context)) != expected_ledger_sha256:
            raise ContractError("RESTORE_UNVERIFIED", "checkpoint_changed")
        # Closed admission is durable BEFORE P15 replaces/copies the database.
        with marker.open("x",encoding="utf-8") as stream:
            stream.write(canonical(payload)+"\n")
            stream.flush()
            os.fsync(stream.fileno())


def replay_deletion_ledger(storage,authority: InstallationMaintenance,ledger,*,remaining_seconds: float = 10.0) -> dict:
    context = _maintenance_context(storage,authority)
    marker = storage.binding.data_directory / "restore-required.json"
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_size>4096:
        raise ContractError("RESTORE_UNVERIFIED","checkpoint_missing")
    pending = json.loads(marker.read_text(encoding="utf-8"))
    if (pending.get("installation_id"),pending.get("agent_id"),pending.get("data_directory")) != (context.binding.installation_id,context.binding.agent_id,str(context.binding.data_directory)):
        raise ContractError("RESTORE_UNVERIFIED","identity")
    if ledger_digest(ledger)!=pending.get("expected_ledger_sha256"):
        raise ContractError("RESTORE_UNVERIFIED","ledger_incomplete")
    if (ledger.get("format"),ledger.get("schema_version"),ledger.get("installation_id"),ledger.get("agent_id"),ledger.get("scope_ids")) != (
        "scope-recall-deletion-ledger/1",SCHEMA_VERSION,context.binding.installation_id,context.binding.agent_id,sorted(context.allowed_scope_ids)):
        raise ContractError("RESTORE_UNVERIFIED","ledger_identity")
    checkpoint_epoch = ledger.get("memory_epoch")
    if type(checkpoint_epoch) is not int or not 0 <= checkpoint_epoch < 9223372036854775807:
        raise ContractError("RESTORE_UNVERIFIED","ledger_epoch")
    count = 0
    with storage._transaction(context,writable=True,remaining_seconds=remaining_seconds,restoring=True) as tx:
        conn = tx._check(write=True)
        # Transient input captured before this explicit restore cannot be
        # treated as fresh post-restore authorization. Rebuild partial derived
        # summaries from the restored source, with its normal tombstone checks.
        conn.execute("DELETE FROM capture_inbox")
        conn.execute("DELETE FROM consolidation_fragments")
        conn.execute("UPDATE work_items SET consolidation_offset=0 WHERE state IN ('pending','leased') AND work_type='consolidate'")
        for entry in ledger["operations"]:
            row = entry["operation"]
            fields = tuple(row)
            allowed_fields = {"operation_id","request_sha256","mode","scope_ids_json","project_id","branch_id","requested_refs_json","expected_revisions_json","created_at","memory_epoch","layers_json","active_content_removed"}
            if set(fields)!=allowed_fields or row["mode"] not in {"delete","suppress"} or not set(json.loads(row["scope_ids_json"])) <= context.allowed_scope_ids:
                raise ContractError("RESTORE_UNVERIFIED","operation_shape")
            if type(row["memory_epoch"]) is not int or not 0 <= row["memory_epoch"] <= checkpoint_epoch:
                raise ContractError("RESTORE_UNVERIFIED","operation_epoch")
            # The latest ledger's physical-erasure status is not evidence about
            # this older file. Replayed deletion starts physical cleanup again.
            row = dict(row)
            layers = json.loads(row["layers_json"])
            if row["mode"]=="delete":
                layers.update(sqlite_active="pending", sqlite_history="maintenance_pending",
                              vector_active="inventory_pending", vector_history="inventory_pending",
                              attachments="inventory_pending")
            row.update(active_content_removed=0,layers_json=canonical(layers))
            conn.execute(f"INSERT INTO deletion_operations({','.join(fields)}) VALUES ({','.join('?' for _ in fields)}) ON CONFLICT(operation_id) DO NOTHING",tuple(row[f] for f in fields))
            if row["mode"]=="delete":
                conn.execute("UPDATE deletion_operations SET active_content_removed=0,layers_json=? WHERE operation_id=?",(row["layers_json"],row["operation_id"]))
            targets=[]
            for member in entry["members"]:
                if member["scope_id"] not in context.allowed_scope_ids or member["object_kind"] not in OBJECT_TABLES:
                    raise ContractError("RESTORE_UNVERIFIED","member_scope")
                targets.append(DeleteTarget(member["object_kind"],member["object_ref"],1,member["scope_id"],member["project_id"],member["branch_id"]))
            # Tombstones are applied even to objects not yet present in the old
            # snapshot, so a later replay cannot resurrect them.
            tx.deletions.apply_blocks(row["operation_id"],targets,delete=row["mode"]=="delete")
            if row["mode"]=="delete":
                # Purge method checks the operation's owning project; use a
                # context restricted to that actual project during replay.
                from dataclasses import replace
                previous_context = tx.context
                try:
                    tx.context = replace(context,project_id=row["project_id"],branch_id=row["branch_id"])
                    tx.deletions.purge_sqlite(row["operation_id"])
                    # An older snapshot may predate the purge work itself or
                    # contain a completed lease. Explicit restore creates a new
                    # physical cleanup obligation and fences old workers.
                    for scope in sorted(json.loads(row["scope_ids_json"])):
                        conn.execute("""INSERT INTO work_items(
                            work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,available_at)
                            VALUES ('purge',?,1,?,?,?,?)
                            ON CONFLICT(work_type,subject_ref,subject_revision) DO UPDATE SET
                            state='pending',attempt=0,available_at=excluded.available_at,
                            lease_token=work_items.lease_token+1,lease_owner=NULL,lease_until=NULL,last_error_code=NULL""",
                            (row["operation_id"]+":"+scope, scope, row["project_id"], row["branch_id"], row["created_at"]))
                finally:
                    tx.context = previous_context
            count += len(targets)
        for group in ledger["source_groups"]:
            if set(group)!={"group_sha256","scope_id","project_id","branch_id","read_blocked","suppressed","operation_id"} or group["scope_id"] not in context.allowed_scope_ids:
                raise ContractError("RESTORE_UNVERIFIED","source_group")
            conn.execute("""INSERT INTO source_group_blocks(group_sha256,scope_id,project_id,branch_id,read_blocked,suppressed,operation_id)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(group_sha256) DO UPDATE SET
                read_blocked=max(source_group_blocks.read_blocked,excluded.read_blocked),suppressed=max(source_group_blocks.suppressed,excluded.suppressed)""",
                tuple(group[k] for k in ("group_sha256","scope_id","project_id","branch_id","read_blocked","suppressed","operation_id")))
        for withdrawal in ledger["withdrawals"]:
            if withdrawal["scope_id"] not in context.allowed_scope_ids:
                raise ContractError("RESTORE_UNVERIFIED","withdrawal_scope")
            old = conn.execute("""SELECT c.current_revision,v.payload_json FROM claims c JOIN claim_versions v
                ON v.claim_id=c.claim_id AND v.revision=c.current_revision WHERE c.claim_id=? AND c.read_blocked=0""",(withdrawal["claim_id"],)).fetchone()
            if old is not None and old["current_revision"] < withdrawal["current_revision"]:
                payload = json.loads(old["payload_json"])
                if withdrawal["intention_state"] in {"cancelled","completed","expired"}:
                    if "intention" not in payload:
                        raise ContractError("RESTORE_UNVERIFIED","intention_shape")
                    payload["intention"]["state"]=withdrawal["intention_state"]
                conn.execute("""INSERT INTO claim_versions(claim_id,revision,payload_json,state,basis,qualification_reason,
                    valid_from,valid_to,recorded_from,replaces_revision) VALUES (?,?,?,?,'unknown','restored_governance_ledger',?,?,?,?)""",
                    (withdrawal["claim_id"],withdrawal["current_revision"],canonical(payload),withdrawal["state"],withdrawal["valid_from"],withdrawal["valid_to"],withdrawal["recorded_from"],old["current_revision"]))
                conn.execute("UPDATE claim_versions SET recorded_to=? WHERE claim_id=? AND revision=?",(withdrawal["recorded_from"],withdrawal["claim_id"],old["current_revision"]))
                conn.execute("UPDATE claims SET current_revision=? WHERE claim_id=?",(withdrawal["current_revision"],withdrawal["claim_id"]))
                conn.execute("UPDATE work_items SET state='obsolete',lease_token=lease_token+1 WHERE subject_ref=? AND state IN ('pending','leased')",(withdrawal["claim_id"],))
            elif old is None:
                conn.execute("""INSERT INTO restored_absence_blocks(object_kind,object_ref,scope_id,project_id,branch_id,checkpoint_sha256,reason)
                    VALUES ('claim',?,?,?,?,?,'governance_body_missing_from_snapshot') ON CONFLICT DO NOTHING""",
                    (withdrawal["claim_id"],withdrawal["scope_id"],withdrawal["project_id"],withdrawal["branch_id"],ledger_digest(ledger)))
        for absent in ledger["absent_objects"]:
            if absent["scope_id"] not in context.allowed_scope_ids or absent["object_kind"]!="claim":
                raise ContractError("RESTORE_UNVERIFIED","absent_scope")
            conn.execute("""INSERT INTO restored_absence_blocks(object_kind,object_ref,scope_id,project_id,branch_id,checkpoint_sha256,reason)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",tuple(absent[k] for k in ("object_kind","object_ref","scope_id","project_id","branch_id","checkpoint_sha256","reason")))
        # Restoring an older file must never reuse a previously released cache
        # epoch. The independently obtained latest checkpoint is the floor.
        conn.execute("UPDATE instance_meta SET memory_epoch=max(memory_epoch,?)+1 WHERE singleton=1",(checkpoint_epoch,))
    # A crash before this unlink leaves admission closed and replay is safe.
    storage._path_check()
    if marker.is_symlink() or marker.parent.resolve()!=storage.binding.data_directory.resolve():
        raise ContractError("RESTORE_UNVERIFIED","marker_path")
    marker.unlink()
    return dict(status="deletion_ledger_replayed",affected_objects=count,checkpoint_sha256=ledger_digest(ledger),
                boundary="deletion and withdrawal admission only; P15 validates the complete restored dataset")
