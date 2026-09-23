"""Deny-first governance and dependency traversal in the sole truth transaction."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from ..contracts import ContractError
from . import lexical_index, lineage
from .claims import canonical_time

OBJECT_TABLES = {'event':('source_events','event_id'),'claim':('claims','claim_id'),
                 'episode':('episodes','episode_id'),'artifact':('artifacts','artifact_id'),
                 'reference':('reference_bindings','reference_id')}


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)


def retraction_after(conn, scope_ids, epoch: int) -> bool:
    """Whether a deletion or suppression touching any of ``scope_ids`` was recorded after ``epoch``.

    Every capture and every derived write moves ``memory_epoch``; only these withdraw anything a
    reader may already hold.  An operation records the epoch it moved the store to, so one recorded
    above ``epoch`` came after a read at ``epoch`` -- and a restore replays each with its own, so a
    deletion made after that read still counts once an older file is back.
    """
    scopes = sorted(set(scope_ids))
    if not scopes:
        return False
    marks = ",".join("?" for _ in scopes)
    return conn.execute(
        f"""SELECT 1 FROM deletion_operations o WHERE o.memory_epoch>?
            AND EXISTS(SELECT 1 FROM json_each(o.scope_ids_json) s WHERE s.value IN ({marks})) LIMIT 1""",
        (epoch, *scopes),
    ).fetchone() is not None


def group_digest(binding,scope_id,project_id,branch_id,group_key):
    return hashlib.sha256(canonical([binding.installation_id,scope_id,project_id,branch_id,group_key]).encode()).hexdigest()


@dataclass(frozen=True)
class DeleteTarget:
    kind: str
    ref: str
    revision: int
    scope_id: str
    project_id: str | None
    branch_id: str | None


class Deletions:
    def __init__(self,tx): self._tx = tx

    def target(self,ref: str) -> DeleteTarget:
        conn,ctx = self._tx._check(),self._tx.context
        # No target body is returned during eligibility/ownership checks.
        if ref.startswith("event-"):
            row = conn.execute("SELECT max(source_revision) AS revision,scope_id,project_id,branch_id FROM source_events WHERE event_id=? GROUP BY event_id",(ref,)).fetchone()
            kind = "event"
        elif (kind:=ref.split('-',1)[0]) in OBJECT_TABLES:
            table,key=OBJECT_TABLES[kind]
            row = conn.execute(f"SELECT current_revision AS revision,scope_id,project_id,branch_id FROM {table} WHERE {key}=?",(ref,)).fetchone()
        else:
            raise ContractError("ACCESS_DENIED","target_unavailable")
        if row is None or row["scope_id"] not in ctx.allowed_scope_ids or (row["project_id"],row["branch_id"]) != (ctx.project_id,ctx.branch_id):
            raise ContractError("ACCESS_DENIED","target_unavailable")
        return DeleteTarget(kind,ref,row["revision"],row["scope_id"],row["project_id"],row["branch_id"])

    def closure(self,targets: tuple[DeleteTarget, ...], *, delete: bool) -> tuple[DeleteTarget, ...]:
        conn = self._tx._check()
        pending = list(targets)
        seen = {}
        # Deleting a derived assertion also fences its source representation:
        # otherwise raw-source retrieval could immediately repeat that content.
        # A source is the minimum capture unit; exact source-span redaction is
        # not fabricated as a new human occurrence.
        for target in targets:
            if target.kind != 'event':
                pending.extend(self.target(source_ref) for source_ref in lineage.sources_of(conn, target.kind, target.ref))
        while pending:
            target = pending.pop()
            key = (target.kind,target.ref)
            if key in seen:
                continue
            if len(seen) >= 2000:
                raise ContractError("INPUT_INVALID","dependency_budget")
            seen[key] = target
            if target.kind == "event":
                siblings = conn.execute("SELECT DISTINCT event_id FROM source_events WHERE source_group_key IN (SELECT source_group_key FROM source_events WHERE event_id=?)",(target.ref,))
                pending.extend(self.target(r[0]) for r in siblings)
                for row in lineage.dependents(conn, target.ref):
                    if row[0] in OBJECT_TABLES:
                        pending.append(self.target(row[1]))
                    else:
                        raise ContractError("DERIVATION_INVALID","unresolved_dependency_kind")
            for row in lineage.dependents_on(conn, target.kind, target.ref):
                if row[0] not in OBJECT_TABLES:
                    raise ContractError('DERIVATION_INVALID','unresolved_dependency_kind')
                pending.append(self.target(row[1]))
            if target.kind == "claim":
                aliases = conn.execute("""SELECT DISTINCT c.claim_id FROM claims c JOIN claim_versions v USING(claim_id)
                    WHERE json_extract(v.payload_json,'$.alias.target_ref')=?""",(target.ref,))
                pending.extend(self.target(r[0]) for r in aliases)
        return tuple(seen[k] for k in sorted(seen))

    def request_key(self,request) -> tuple[str,str]:
        ctx = self._tx.context
        material = [ctx.binding.installation_id,sorted(ctx.allowed_scope_ids),ctx.project_id,ctx.branch_id,
                    request["mode"],sorted(request["target_refs"]),request["expected_revisions"]]
        digest = hashlib.sha256(canonical(material).encode()).hexdigest()
        return "forget-"+digest,digest

    def receipt(self,operation_id: str) -> dict | None:
        row = self._tx._check().execute("SELECT * FROM deletion_operations WHERE operation_id=?",(operation_id,)).fetchone()
        if row is None:
            return None
        ctx = self._tx.context
        if not set(json.loads(row["scope_ids_json"])) <= ctx.allowed_scope_ids or (row["project_id"],row["branch_id"]) != (ctx.project_id,ctx.branch_id):
            raise ContractError("ACCESS_DENIED","operation_unavailable")
        layers = json.loads(row["layers_json"])
        count = self._tx._check().execute("SELECT count(*) FROM deletion_members WHERE operation_id=?",(operation_id,)).fetchone()[0]
        return dict(operation_id=operation_id,mode=row["mode"],memory_epoch=row["memory_epoch"],
                    scope_ids=json.loads(row["scope_ids_json"]),project_id=row["project_id"],branch_id=row["branch_id"],
                    requested_refs=json.loads(row["requested_refs_json"]),minimum_content_unit="source_group",
                    read_blocked=row["mode"]=="delete",suppressed=True,affected_objects=count,
                    active_content_removed=bool(row["active_content_removed"]),layers=layers,
                    declared_scope_complete=False if row["mode"]=="delete" else True)

    def physical_members(self, operation_id: str) -> tuple[dict, ...]:
        """Return opaque active object identities for an external purge port."""
        conn = self._tx._check()
        receipt = self.receipt(operation_id)
        if receipt is None or receipt["mode"] != "delete":
            raise ContractError("ACCESS_DENIED", "operation_unavailable")
        members: list[dict] = []
        for row in conn.execute(
            "SELECT object_kind,object_ref FROM deletion_members WHERE operation_id=? ORDER BY object_kind,object_ref",
            (operation_id,),
        ):
            kind, ref = row["object_kind"], row["object_ref"]
            if kind == "event":
                revisions = conn.execute(
                    "SELECT source_revision AS revision FROM source_events WHERE event_id=? ORDER BY source_revision",
                    (ref,),
                ).fetchall()
            elif kind == "claim":
                revisions = conn.execute(
                    "SELECT revision FROM claim_versions WHERE claim_id=? ORDER BY revision",
                    (ref,),
                ).fetchall()
            elif kind == "episode":
                revisions = conn.execute(
                    "SELECT revision FROM episode_versions WHERE episode_id=? ORDER BY revision",
                    (ref,),
                ).fetchall()
            elif kind == "artifact":
                revisions = conn.execute(
                    "SELECT revision FROM artifact_versions WHERE artifact_id=? ORDER BY revision",
                    (ref,),
                ).fetchall()
            elif kind == "reference":
                revisions = conn.execute(
                    "SELECT revision FROM reference_versions WHERE reference_id=? ORDER BY revision",
                    (ref,),
                ).fetchall()
            else:
                raise ContractError("DERIVATION_INVALID", "unresolved_purge_kind")
            for revision in revisions:
                if revision["revision"] is not None:
                    members.append({"kind": kind, "ref": ref, "revision": int(revision["revision"])})
        return tuple(members)

    def block(self,request,targets: tuple[DeleteTarget, ...],*,now: str) -> str:
        conn,ctx = self._tx._check(write=True),self._tx.context
        op,digest = self.request_key(request)
        if self.receipt(op) is not None:
            return op
        delete = request["mode"] == "delete"
        layers = dict(sqlite_active="pending" if delete else "retained",sqlite_history="maintenance_pending" if delete else "retained",
                      vector_active="inventory_pending" if delete else "retained",vector_history="inventory_pending" if delete else "retained",
                      attachments="inventory_pending" if delete else "retained",backups="unknown",host_sources="outside_plugin_scope",physical_media="unverifiable")
        conn.execute("UPDATE instance_meta SET memory_epoch=memory_epoch+1 WHERE singleton=1")
        epoch = self._tx.status().memory_epoch
        conn.execute("""INSERT INTO deletion_operations(operation_id,request_sha256,mode,scope_ids_json,project_id,branch_id,
            requested_refs_json,expected_revisions_json,created_at,memory_epoch,layers_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (op,digest,request["mode"],canonical(sorted({t.scope_id for t in targets})),ctx.project_id,ctx.branch_id,
             canonical(sorted(request["target_refs"])),canonical(request["expected_revisions"]),canonical_time(now),epoch,canonical(layers)))
        self.apply_blocks(op,targets,delete=delete,now=canonical_time(now))
        if delete:
            for scope in sorted({t.scope_id for t in targets}):
                conn.execute("INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,available_at) VALUES ('purge',?,1,?,?,?,?)",
                             (op+":"+scope,scope,ctx.project_id,ctx.branch_id,canonical_time(now)))
        return op

    def apply_blocks(self,op,targets,*,delete: bool,now: str | None = None):
        conn = self._tx._check(write=True)
        if now is None:
            row = conn.execute("SELECT created_at FROM deletion_operations WHERE operation_id=?", (op,)).fetchone()
            if row is None:
                raise ContractError("SOURCE_MISSING", "deletion_operation")
            now = row["created_at"]
        # Pending captures have no public source ref yet. Cancel the affected
        # partition conservatively so a delayed event cannot undo forgetting.
        for scope, project, branch in {(t.scope_id, t.project_id, t.branch_id) for t in targets}:
            conn.execute("DELETE FROM capture_inbox WHERE scope_id=? AND project_id IS ? AND branch_id IS ?", (scope, project, branch))
        for target in targets:
            conn.execute("DELETE FROM consolidation_fragments WHERE work_id IN (SELECT work_id FROM work_items WHERE subject_ref=?)", (target.ref,))
            conn.execute("""INSERT INTO object_blocks(object_kind,object_ref,scope_id,project_id,branch_id,read_blocked,suppressed,operation_id)
                VALUES (?,?,?,?,?,?,1,?) ON CONFLICT(object_kind,object_ref) DO UPDATE SET
                read_blocked=max(object_blocks.read_blocked,excluded.read_blocked),suppressed=1,operation_id=excluded.operation_id""",
                (target.kind,target.ref,target.scope_id,target.project_id,target.branch_id,int(delete),op))
            conn.execute("INSERT INTO deletion_members(operation_id,object_kind,object_ref) VALUES (?,?,?) ON CONFLICT DO NOTHING",(op,target.kind,target.ref))
            if target.kind == "event":
                for group in conn.execute("SELECT DISTINCT source_group_key FROM source_events WHERE event_id=?",(target.ref,)):
                    digest = group_digest(self._tx.context.binding,target.scope_id,target.project_id,target.branch_id,group[0])
                    conn.execute("""INSERT INTO source_group_blocks(group_sha256,scope_id,project_id,branch_id,read_blocked,suppressed,operation_id)
                        VALUES (?,?,?,?,?,1,?) ON CONFLICT(group_sha256) DO UPDATE SET read_blocked=max(source_group_blocks.read_blocked,excluded.read_blocked),suppressed=1,operation_id=excluded.operation_id""",
                        (digest,target.scope_id,target.project_id,target.branch_id,int(delete),op))
            table,key = OBJECT_TABLES[target.kind]
            conn.execute(f"UPDATE {table} SET read_blocked=max(read_blocked,?),suppressed=1 WHERE {key}=?",(int(delete),target.ref))
            if delete:
                conn.execute("UPDATE work_items SET state='obsolete',lease_token=lease_token+1,lease_owner=NULL,lease_until=NULL WHERE subject_ref=? AND state IN ('pending','leased')",(target.ref,))
                conn.execute("""UPDATE unresolved_updates SET state='obsolete' WHERE state='unresolved' AND
                    (source_ref=? OR EXISTS(SELECT 1 FROM json_each(candidate_refs_json) WHERE value=?))""",(target.ref,target.ref))
        self._tx.candidates.block_objects(targets, operation_id=op, delete=delete, now=now)

    def purge_sqlite(self,operation_id: str) -> dict:
        conn = self._tx._check(write=True)
        receipt = self.receipt(operation_id)
        if receipt is None or receipt["mode"] != "delete":
            raise ContractError("ACCESS_DENIED","operation_unavailable")
        # This is deliberately only the SQLite scrub phase.  The active vector
        # layer is owned by the native purge port and cannot be acknowledged by
        # a truth-database transaction.  A repeated call after the scrub is
        # therefore idempotent, while the durable purge item remains available
        # for the physical phase.
        if receipt["layers"].get("sqlite_active") == "removed":
            return receipt
        # This overwrites SQLite payload cells, not host logs, copies or SSD
        # internals. VACUUM/checkpoint inventory remains explicit maintenance.
        conn.execute("PRAGMA secure_delete=ON")
        members = conn.execute("SELECT object_kind,object_ref FROM deletion_members WHERE operation_id=?",(operation_id,)).fetchall()
        for kind,ref in members:
            if kind == "event":
                groups = conn.execute("SELECT DISTINCT source_group_key FROM source_events WHERE event_id=?",(ref,)).fetchall()
                for group in groups:
                    replacement = "removed-"+hashlib.sha256(group[0].encode()).hexdigest()
                    conn.execute("UPDATE source_events SET source_group_key=? WHERE source_group_key=?",(replacement,group[0]))
                lexical_index.forget(conn, ref)
                conn.execute("""UPDATE source_events SET content='',source_event_key='removed-'||event_id,
                    extra_json='{"evidence_refs":[]}',source_original_origin=NULL,dataset_id=NULL,
                    capture_state='gap',capture_gaps_json='["deleted"]' WHERE event_id=?""",(ref,))
            elif kind == "claim":
                conn.execute("UPDATE claims SET subject='',predicate='' WHERE claim_id=?",(ref,))
                conn.execute("UPDATE claim_versions SET payload_json='{}' WHERE claim_id=?",(ref,))
            elif kind == 'episode':
                conn.execute('UPDATE episode_versions SET resume_json=NULL,environment_revision=NULL WHERE episode_id=?',(ref,))
            elif kind == 'artifact':
                # Keep only opaque retention metadata needed by attachment
                # cleanup. The public repository is already read-blocked.
                conn.execute("UPDATE artifact_versions SET label='',description_json='[]' WHERE artifact_id=?",(ref,))
            elif kind == 'reference':
                conn.execute("UPDATE reference_versions SET payload_json='{}' WHERE reference_id=?",(ref,))
            else:
                raise ContractError("DERIVATION_INVALID","unresolved_purge_kind")
            lineage.purge_quotes(conn, kind, ref)
        layers = receipt["layers"]
        layers["sqlite_active"] = "removed"
        conn.execute("UPDATE deletion_operations SET active_content_removed=0,layers_json=? WHERE operation_id=?",(canonical(layers),operation_id))
        updated = self.receipt(operation_id)
        if updated is None:
            raise ContractError("STORAGE_UNAVAILABLE", "deletion_receipt")
        return updated

    def mark_vector_active_removed(self, operation_id: str) -> dict:
        """Acknowledge native active-vector purge after its physical commit.

        The caller must perform this in the short final work transaction after
        the native port has returned a durable acknowledgement.  Historical
        vector storage and attachments remain separate layers.
        """
        conn = self._tx._check(write=True)
        receipt = self.receipt(operation_id)
        if receipt is None or receipt["mode"] != "delete":
            raise ContractError("ACCESS_DENIED", "operation_unavailable")
        layers = receipt["layers"]
        if layers.get("sqlite_active") != "removed":
            raise ContractError("VERSION_CONFLICT", "sqlite_scrub_required")
        layers["vector_active"] = "removed"
        active_removed = (
            layers.get("sqlite_active") == "removed"
            and layers.get("vector_active") == "removed"
            and layers.get("attachments") in {"removed", "shared_authorized_copy_retained"}
        )
        conn.execute(
            "UPDATE deletion_operations SET active_content_removed=?,layers_json=? WHERE operation_id=?",
            (int(active_removed), canonical(layers), operation_id),
        )
        updated = self.receipt(operation_id)
        if updated is None:
            raise ContractError("STORAGE_UNAVAILABLE", "deletion_receipt")
        return updated

    def attachment_plan(self,operation_id: str) -> dict:
        """Snapshot attachment cleanup candidates without deleting files."""
        conn=self._tx._check()
        receipt=self.receipt(operation_id)
        if receipt is None or receipt['mode'] != 'delete':
            raise ContractError('ACCESS_DENIED','operation_unavailable')
        if receipt['layers']['attachments'] in {'removed','shared_authorized_copy_retained'}:
            return dict(operation_id=operation_id, entries=(), already_done=True)
        rows=conn.execute('''SELECT v.* FROM deletion_members m JOIN artifact_versions v ON v.artifact_id=m.object_ref
            WHERE m.operation_id=? AND m.object_kind='artifact' AND v.blob_json IS NOT NULL''',(operation_id,)).fetchall()
        entries=[]
        for row in rows:
            live=conn.execute('''SELECT 1 FROM artifact_versions v JOIN artifacts a USING(artifact_id)
                WHERE v.sha256=? AND a.read_blocked=0 LIMIT 1''',(row['sha256'],)).fetchone()
            entries.append(dict(
                artifact_id=row['artifact_id'], revision=row['revision'],
                blob=json.loads(row['blob_json']), shared=bool(live),
            ))
        return dict(operation_id=operation_id, entries=tuple(entries), already_done=False)

    def finalize_attachments(self,operation_id: str, plan: dict, *, erased: bool) -> dict:
        """Commit attachment metadata only after the physical phase returns."""
        conn=self._tx._check(write=True)
        receipt=self.receipt(operation_id)
        if receipt is None or receipt['mode'] != 'delete':
            raise ContractError('ACCESS_DENIED','operation_unavailable')
        if receipt['layers']['attachments'] in {'removed','shared_authorized_copy_retained'}:
            return receipt
        if not isinstance(plan, dict) or plan.get('operation_id') != operation_id:
            raise ContractError('VERSION_CONFLICT','attachment_plan')
        if not erased:
            return receipt
        shared=any(bool(entry.get('shared')) for entry in plan.get('entries',()))
        for entry in plan.get('entries',()):
            conn.execute('UPDATE artifact_versions SET relative_path=NULL,blob_json=NULL WHERE artifact_id=? AND revision=?',
                         (entry['artifact_id'],entry['revision']))
        layers=receipt['layers']
        layers['attachments']='shared_authorized_copy_retained' if shared else 'removed'
        active_removed = (
            layers.get('sqlite_active') == 'removed'
            and layers.get('vector_active') == 'removed'
            and layers.get('attachments') in {'removed','shared_authorized_copy_retained'}
        )
        conn.execute('UPDATE deletion_operations SET active_content_removed=?,layers_json=? WHERE operation_id=?',
                     (int(active_removed),canonical(layers),operation_id))
        updated = self.receipt(operation_id)
        if updated is None:
            raise ContractError('STORAGE_UNAVAILABLE','deletion_receipt')
        return updated

    def purge_attachments(self,operation_id: str) -> dict:
        """Compatibility shim: planning is separate from physical deletion."""
        plan = self.attachment_plan(operation_id)
        if plan.get('already_done'):
            updated = self.receipt(operation_id)
            if updated is None:
                raise ContractError('STORAGE_UNAVAILABLE','deletion_receipt')
            return updated
        raise ContractError('STORAGE_UNAVAILABLE','attachment_physical_phase_required')
