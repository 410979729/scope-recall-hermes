"""Exact artifact versions. Grants are runtime capabilities, never model paths."""
from __future__ import annotations

from dataclasses import asdict,dataclass
import hashlib
import json

from ..contracts import ContractError
from ..secret_patterns import contains_secret_like_text
from .claim_storage import parse_source_ref
from .delete_storage import canonical
from .retained_artifacts import ArtifactGrant,RetainedBlob,retain,read_retained
from .visibility import allowed


def artifact_identity(context,scope_id,key):
    if scope_id not in context.allowed_scope_ids or type(key) is not str or not 1<=len(key)<=240:
        raise ContractError('ACCESS_DENIED')
    return 'artifact-'+hashlib.sha256(canonical([context.binding.installation_id,scope_id,context.project_id,context.branch_id,key]).encode()).hexdigest()


@dataclass(frozen=True)
class Artifact:
    ref: str
    revision: int
    scope_id: str
    project_id: str | None
    branch_id: str | None
    label: str
    media_type: str
    sha256: str | None
    size_bytes: int | None
    retention_state: str
    blob: RetainedBlob | None
    descriptions: tuple
    evidence_refs: tuple[str,...]
    suppressed: bool


class Artifacts:
    def __init__(self,tx): self.tx=tx

    def get(self,ref,revision=None):
        conn,ctx=self.tx._check(),self.tx.context
        if not allowed(self.tx,'artifact',ref): return None
        scopes=sorted(ctx.allowed_scope_ids)
        row=conn.execute(f'''SELECT a.*,v.* FROM artifacts a JOIN artifact_versions v ON v.artifact_id=a.artifact_id
            AND v.revision=COALESCE(?,a.current_revision) WHERE a.artifact_id=? AND a.read_blocked=0
            AND a.scope_id IN ({','.join('?' for _ in scopes)})
            AND (a.project_id IS NULL OR a.project_id=?) AND (a.branch_id IS NULL OR a.branch_id=?)''',
            (revision,ref,*scopes,ctx.project_id,ctx.branch_id)).fetchone()
        if row is None:return None
        refs=tuple(f'{r[0]}@{r[1]}' for r in conn.execute("SELECT source_ref,source_revision FROM evidence_links WHERE object_kind='artifact' AND object_ref=? AND object_revision=? ORDER BY source_ref,source_revision",(ref,row['revision'])))
        if any(self.tx.source(*parse_source_ref(r)) is None for r in refs):return None
        blob=RetainedBlob(**json.loads(row['blob_json'])) if row['blob_json'] else None
        return Artifact(ref,row['revision'],row['scope_id'],row['project_id'],row['branch_id'],row['label'],row['media_type'],row['sha256'],row['size_bytes'],row['retention_state'],blob,tuple(json.loads(row['description_json'])),refs,bool(row['suppressed']))

    def register(self,*,key,revision,scope_id,source_ref,label,media_type,now,grant=None,description=None):
        conn,ctx=self.tx._check(write=True),self.tx.context
        ref=artifact_identity(ctx,scope_id,key)
        if type(revision) is not int or revision<1 or type(label) is not str or not 1<=len(label)<=240 or contains_secret_like_text(label):
            raise ContractError('INPUT_INVALID','artifact_metadata')
        if not allowed(self.tx,'artifact',ref): raise ContractError('SOURCE_MISSING')
        source=self.tx.source(*parse_source_ref(source_ref))
        if source is None or (source.scope_id,source.project_id,source.branch_id)!=(scope_id,ctx.project_id,ctx.branch_id): raise ContractError('SOURCE_MISSING')
        self.tx.claims.require_live_source(source.ref,source.revision)
        if ref not in source.event.get('artifact_refs',[]): raise ContractError('ACCESS_DENIED','artifact_not_attached')
        snapshot=source.event.get('display_snapshot',{})
        if snapshot and dict(artifact_ref=ref,revision=revision) not in snapshot['items']: raise ContractError('VERSION_CONFLICT','artifact_display_version')
        if grant is not None and (not isinstance(grant,ArtifactGrant) or grant.media_type!=media_type): raise ContractError('INPUT_INVALID','artifact_grant')
        if description is not None and (type(description) is not str or not 1<=len(description)<=4096 or description not in source.event['content']): raise ContractError('DERIVATION_INVALID','artifact_description')
        existing=self.get(ref,revision)
        if existing:
            if existing.sha256!=(grant.sha256 if grant else None) or existing.label!=label or existing.media_type!=media_type:
                raise ContractError('VERSION_CONFLICT','artifact_version')
            return existing
        head=conn.execute('SELECT current_revision FROM artifacts WHERE artifact_id=?',(ref,)).fetchone()
        if revision!=(head[0]+1 if head else 1): raise ContractError('VERSION_CONFLICT','artifact_sequence')
        # Bounded local file capture occurs while the single writer owns the
        # source/delete fence. Failed final DB commit can leave an unreferenced
        # file; P15 inventories retained files and never claims unknown cleanup.
        blob=retain(ctx.binding,grant) if grant else None
        if head is None:
            conn.execute('INSERT INTO artifacts(artifact_id,scope_id,project_id,branch_id,current_revision,suppressed) VALUES (?,?,?,?,?,?)',(ref,scope_id,ctx.project_id,ctx.branch_id,revision,int(source.suppressed)))
        else: conn.execute('UPDATE artifacts SET current_revision=?,suppressed=max(suppressed,?) WHERE artifact_id=?',(revision,int(source.suppressed),ref))
        descriptions=[] if description is None else [dict(text=description,evidence_ref=source_ref,origin=source.event['origin'])]
        conn.execute('''INSERT INTO artifact_versions(artifact_id,revision,label,media_type,sha256,size_bytes,retention_state,relative_path,blob_json,description_json,recorded_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''',(ref,revision,label,media_type,blob.sha256 if blob else None,blob.size_bytes if blob else None,'retained_artifact' if blob else 'described_artifact' if description else 'reference_only',blob.relative_path if blob else None,canonical(asdict(blob)) if blob else None,canonical(descriptions),now))
        conn.execute("INSERT INTO evidence_links VALUES ('artifact',?,?,?,?,'derived_from',?,NULL)",(ref,revision,source.ref,source.revision,description or ''))
        conn.execute('UPDATE instance_meta SET memory_epoch=memory_epoch+1 WHERE singleton=1')
        return self.get(ref,revision)

    def open(self,ref,revision):
        artifact=self.get(ref,revision)
        if artifact is None: raise ContractError('SOURCE_MISSING')
        if artifact.blob is None: raise ContractError('SOURCE_MISSING','artifact_not_retained')
        return artifact,read_retained(self.tx.context.binding,artifact.blob)
