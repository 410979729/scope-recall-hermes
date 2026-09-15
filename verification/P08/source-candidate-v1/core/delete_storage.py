"""Deny-first governance and dependency traversal in the sole truth transaction."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from ..contracts import ContractError
from .claims import canonical_time

OBJECT_TABLES = {'event':('source_events','event_id'),'claim':('claims','claim_id'),
                 'episode':('episodes','episode_id'),'artifact':('artifacts','artifact_id'),
                 'reference':('reference_bindings','reference_id')}


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)


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
                pending.extend(self.target(r[0]) for r in conn.execute("SELECT DISTINCT source_ref FROM evidence_links WHERE object_kind=? AND object_ref=?",(target.kind,target.ref)))
        while pending:
            target = pending.pop()
            key = (target.kind,target.ref)
            if key in seen: continue
            if len(seen) >= 2000:
                raise ContractError("INPUT_INVALID","dependency_budget")
            seen[key] = target
            if target.kind == "event":
                siblings = conn.execute("SELECT DISTINCT event_id FROM source_events WHERE source_group_key IN (SELECT source_group_key FROM source_events WHERE event_id=?)",(target.ref,))
                pending.extend(self.target(r[0]) for r in siblings)
                dependents = conn.execute("SELECT DISTINCT object_kind,object_ref FROM evidence_links WHERE source_ref=?",(target.ref,))
                for row in dependents:
                    if row[0] in OBJECT_TABLES:
                        pending.append(self.target(row[1]))
                    else:
                        raise ContractError("DERIVATION_INVALID","unresolved_dependency_kind")
            for row in conn.execute('SELECT DISTINCT object_kind,object_ref FROM object_dependencies WHERE dependency_kind=? AND dependency_ref=?',(target.kind,target.ref)):
                if row[0] not in OBJECT_TABLES:raise ContractError('DERIVATION_INVALID','unresolved_dependency_kind')
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
        if row is None: return None
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

    def block(self,request,targets: tuple[DeleteTarget, ...],*,now: str) -> str:
        conn,ctx = self._tx._check(write=True),self._tx.context
        op,digest = self.request_key(request)
        if self.receipt(op) is not None: return op
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
        self.apply_blocks(op,targets,delete=delete)
        if delete:
            for scope in sorted({t.scope_id for t in targets}):
                conn.execute("INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,available_at) VALUES ('purge',?,1,?,?,?,?)",
                             (op+":"+scope,scope,ctx.project_id,ctx.branch_id,canonical_time(now)))
        return op

    def apply_blocks(self,op,targets,*,delete: bool):
        conn = self._tx._check(write=True)
        for target in targets:
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

    def purge_sqlite(self,operation_id: str) -> dict:
        conn = self._tx._check(write=True)
        receipt = self.receipt(operation_id)
        if receipt is None or receipt["mode"] != "delete":
            raise ContractError("ACCESS_DENIED","operation_unavailable")
        if receipt["active_content_removed"]: return receipt
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
                conn.execute("DELETE FROM lexical_projection WHERE event_id=?",(ref,))
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
            edges = conn.execute("""SELECT DISTINCT object_kind,object_ref,object_revision,source_ref,source_revision,relation
                FROM evidence_links WHERE (object_kind=? AND object_ref=?) OR source_ref=?""",(kind,ref,ref)).fetchall()
            conn.execute("DELETE FROM evidence_links WHERE (object_kind=? AND object_ref=?) OR source_ref=?",(kind,ref,ref))
            conn.executemany("""INSERT INTO evidence_links(object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote)
                VALUES (?,?,?,?,?,?,'') ON CONFLICT DO NOTHING""",[tuple(edge) for edge in edges])
        layers = receipt["layers"]
        layers["sqlite_active"] = "removed"
        conn.execute("UPDATE deletion_operations SET active_content_removed=1,layers_json=? WHERE operation_id=?",(canonical(layers),operation_id))
        conn.execute("UPDATE work_items SET state='done',lease_owner=NULL,lease_until=NULL WHERE work_type='purge' AND subject_ref LIKE ?",(operation_id+":%",))
        return self.receipt(operation_id)

    def purge_attachments(self,operation_id: str) -> dict:
        """Exact retained files only, after the operation's committed read fence.

        Other independently authorized references to identical bytes keep their
        shared copy. Unknown crash-orphan and backup inventories remain P15 work.
        """
        from .retained_artifacts import RetainedBlob,erase_retained
        conn=self._tx._check(write=True)
        receipt=self.receipt(operation_id)
        if receipt is None or receipt['mode']!='delete':raise ContractError('ACCESS_DENIED','operation_unavailable')
        if receipt['layers']['attachments'] in {'removed','shared_authorized_copy_retained'}:return receipt
        rows=conn.execute('''SELECT v.* FROM deletion_members m JOIN artifact_versions v ON v.artifact_id=m.object_ref
            WHERE m.operation_id=? AND m.object_kind='artifact' AND v.blob_json IS NOT NULL''',(operation_id,)).fetchall()
        shared=False
        for row in rows:
            live=conn.execute('''SELECT 1 FROM artifact_versions v JOIN artifacts a USING(artifact_id)
                WHERE v.sha256=? AND a.read_blocked=0 LIMIT 1''',(row['sha256'],)).fetchone()
            if live:shared=True
            else:erase_retained(self._tx.context.binding,RetainedBlob(**json.loads(row['blob_json'])))
            conn.execute('UPDATE artifact_versions SET relative_path=NULL,blob_json=NULL WHERE artifact_id=? AND revision=?',(row['artifact_id'],row['revision']))
        layers=receipt['layers'];layers['attachments']='shared_authorized_copy_retained' if shared else 'removed'
        conn.execute('UPDATE deletion_operations SET layers_json=? WHERE operation_id=?',(canonical(layers),operation_id))
        return self.receipt(operation_id)
