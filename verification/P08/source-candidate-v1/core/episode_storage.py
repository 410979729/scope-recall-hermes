"""Traceable episode skeleton and versioned resume state in the owning transaction."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from ..contracts import ContractError
from .claim_storage import parse_source_ref
from .claims import canonical_time
from .delete_storage import canonical
from .episodes import TOPIC_BREAK,qualify_resume,source_origin,source_watermark,state_from_sources,supported_work_goal
from .visibility import allowed


@dataclass(frozen=True)
class Episode:
    ref: str
    revision: int
    scope_id: str
    project_id: str | None
    branch_id: str | None
    state: str
    resume: dict | None
    evidence_refs: tuple[str,...]
    source_watermark: str
    unprocessed_events: int
    suppressed: bool
    gaps: tuple[str,...]
    needs_revalidation: bool


class Episodes:
    def __init__(self,tx): self.tx=tx

    def get(self,ref,revision=None) -> Episode | None:
        conn,ctx=self.tx._check(),self.tx.context
        if not allowed(self.tx,'episode',ref): return None
        scopes=sorted(ctx.allowed_scope_ids)
        row=conn.execute(f'''SELECT e.*,v.* FROM episodes e JOIN episode_versions v ON v.episode_id=e.episode_id
            AND v.revision=COALESCE(?,e.current_revision) WHERE e.episode_id=? AND e.read_blocked=0
            AND e.scope_id IN ({','.join('?' for _ in scopes)})
            AND (e.project_id IS NULL OR e.project_id=?) AND (e.branch_id IS NULL OR e.branch_id=?)''',
            (revision,ref,*scopes,ctx.project_id,ctx.branch_id)).fetchone()
        if row is None: return None
        links=conn.execute('SELECT source_ref,source_revision FROM evidence_links WHERE object_kind=\'episode\' AND object_ref=? AND object_revision=? ORDER BY source_ref,source_revision',(ref,row['revision'])).fetchall()
        gaps=[]
        refs=tuple(f'{r[0]}@{r[1]}' for r in links)
        for key in links:
            source=self.tx.source(*key)
            if source is None: return None
            try: self.tx.claims.require_live_source(*key)
            except ContractError: gaps.append('source_version_changed')
            gaps.extend(source.capture_gaps)
        pending=conn.execute('SELECT count(*) FROM episode_events WHERE episode_id=? AND sequence>?',(ref,row['processed_sequence'])).fetchone()[0]
        if pending: gaps.append('unprocessed_events')
        resume=json.loads(row['resume_json']) if row['resume_json'] else None
        changed=row['environment_revision'] is not None and ctx.environment_revision!=row['environment_revision']
        if resume:
            for progress in resume['verified_progress']:
                for proof in progress['evidence_refs']:
                    captured=conn.execute('SELECT environment_revision FROM episode_events WHERE episode_id=? AND source_ref=? AND source_revision=?',
                                          (ref,*parse_source_ref(proof))).fetchone()
                    if captured is None or captured[0]!=ctx.environment_revision:changed=True
        if changed: gaps.append('environment_needs_revalidation')
        if gaps and row['resume_json'] is not None and any(g!='environment_needs_revalidation' for g in gaps):
            gaps.append('resume_requires_rebuild')
        return Episode(ref,row['revision'],row['scope_id'],row['project_id'],row['branch_id'],row['state'],
                       resume,refs,row['source_watermark'],pending,
                       bool(row['suppressed']),tuple(dict.fromkeys(gaps)),changed)

    def list(self,*,limit=200):
        if type(limit) is not int or not 1<=limit<=200: raise ContractError('INPUT_INVALID','episode_limit')
        ctx=self.tx.context; scopes=sorted(ctx.allowed_scope_ids)
        rows=self.tx._check().execute(f'''SELECT episode_id FROM episodes WHERE scope_id IN ({','.join('?' for _ in scopes)})
            AND read_blocked=0 AND (project_id IS NULL OR project_id=?) AND (branch_id IS NULL OR branch_id=?)
            ORDER BY episode_id LIMIT ?''',(*scopes,ctx.project_id,ctx.branch_id,limit)).fetchall()
        return tuple(item for r in rows if (item:=self.get(r[0])) is not None)

    def source_episode(self,ref,revision):
        row=self.tx._check().execute('SELECT episode_id FROM episode_events WHERE source_ref=? AND source_revision=?',(ref,revision)).fetchone()
        return self.get(row[0]) if row else None

    def _latest_occurrence(self,ref):
        rows=self.tx._check().execute('''SELECT s.occurred_at FROM episode_events ee JOIN source_events s
            ON s.event_id=ee.source_ref AND s.source_revision=ee.source_revision WHERE ee.episode_id=?''',(ref,))
        return max((canonical_time(r[0]) for r in rows if r[0]),default=None)

    def attach(self,source,now):
        conn,ctx=self.tx._check(write=True),self.tx.context
        if self.source_episode(source.ref,source.revision): return
        prefix=[ctx.binding.installation_id,source.scope_id,source.project_id,source.branch_id]
        kind='task' if ctx.task_anchor else 'session'
        key=ctx.task_anchor
        if key is None:
            # Exact source version relation can bridge sessions, never a title.
            snapshot=source.event.get('display_snapshot')
            candidates=[]
            if snapshot and snapshot['order']=='observed' and snapshot['items']:
                pairs=' OR '.join("(json_extract(d.value,'$.artifact_ref')=? AND json_extract(d.value,'$.revision')=?)" for _ in snapshot['items'])
                params=[x for item in snapshot['items'] for x in (item['artifact_ref'],item['revision'])]
                candidates=conn.execute(f'''SELECT DISTINCT e.series_key,e.anchor_kind FROM episode_events ee
                    JOIN episodes e ON e.episode_id=ee.episode_id JOIN source_events s ON s.event_id=ee.source_ref AND s.source_revision=ee.source_revision
                    JOIN json_each(s.extra_json,'$.display_snapshot.items') d
                    WHERE e.scope_id=? AND e.project_id IS ? AND e.branch_id IS ? AND e.read_blocked=0 AND s.read_blocked=0
                    AND NOT EXISTS(SELECT 1 FROM source_events newer WHERE newer.source_group_key=s.source_group_key AND newer.source_revision>s.source_revision)
                    AND ({pairs}) LIMIT 2''',(source.scope_id,source.project_id,source.branch_id,*params)).fetchall()
            candidate=candidates[0] if len(candidates)==1 else None
            if candidate:
                anchor=candidate['series_key'];kind=candidate['anchor_kind']
            if candidate is None:
                previous=conn.execute('''SELECT e.series_key,e.episode_id FROM episode_events ee JOIN source_events s
                    ON s.event_id=ee.source_ref AND s.source_revision=ee.source_revision JOIN episodes e ON e.episode_id=ee.episode_id
                    WHERE s.session_id=? AND s.scope_id=? AND s.project_id IS ? AND s.branch_id IS ? AND e.read_blocked=0
                    ORDER BY ee.sequence DESC LIMIT 1''',(source.session_id,source.scope_id,source.project_id,source.branch_id)).fetchone()
                if previous and not (source_origin(source)=='human_direct' and TOPIC_BREAK.search(source.event['content'])):
                    anchor=previous['series_key']
                else: anchor=hashlib.sha256(canonical([*prefix,'session',source.session_id,source.ref]).encode()).hexdigest()
        else: anchor=hashlib.sha256(canonical([*prefix,'task',key]).encode()).hexdigest()
        series=anchor
        segment=conn.execute('''SELECT e.anchor_key,e.segment_index,(SELECT count(*) FROM episode_events ee WHERE ee.episode_id=e.episode_id) AS count
            FROM episodes e WHERE e.series_key=? ORDER BY e.segment_index DESC LIMIT 1''',(series,)).fetchone()
        segment_index=(segment['segment_index']+(1 if segment['count']>=200 else 0)) if segment else 0
        anchor=hashlib.sha256(canonical([series,segment_index]).encode()).hexdigest()
        ref='episode-'+hashlib.sha256(anchor.encode()).hexdigest()
        if not allowed(self.tx,'episode',ref): raise ContractError('SOURCE_MISSING','episode_unavailable')
        row=conn.execute('SELECT current_revision FROM episodes WHERE anchor_key=?',(anchor,)).fetchone()
        previous=self.get(ref) if row else None
        if row is None:
            conn.execute('INSERT INTO episodes(episode_id,scope_id,project_id,branch_id,anchor_key,anchor_kind,series_key,segment_index) VALUES (?,?,?,?,?,?,?,?)',
                         (ref,source.scope_id,source.project_id,source.branch_id,anchor,kind,series,segment_index))
        revision=(row[0]+1) if row else 1
        state=state_from_sources((source,),previous=previous.state if previous else 'unknown')
        if previous:
            latest=self._latest_occurrence(ref)
            if latest and (source.event['occurred_at'] is None or canonical_time(source.event['occurred_at'])<latest):
                state=previous.state
        prior=conn.execute('SELECT processed_sequence,resume_json,source_watermark,environment_revision FROM episode_versions WHERE episode_id=? AND revision=?',(ref,row[0])).fetchone() if row else None
        conn.execute('INSERT INTO episode_versions(episode_id,revision,state,resume_json,source_watermark,processed_sequence,recorded_at,environment_revision) VALUES (?,?,?,?,?,?,?,?)',
                     (ref,revision,state,prior['resume_json'] if prior else None,prior['source_watermark'] if prior else source_watermark(()),prior['processed_sequence'] if prior else 0,now,
                      prior['environment_revision'] if prior and prior['resume_json'] else ctx.environment_revision))
        conn.execute('UPDATE episodes SET current_revision=?,suppressed=max(suppressed,?) WHERE episode_id=?',(revision,int(source.suppressed),ref))
        conn.execute('INSERT INTO episode_events(episode_id,source_ref,source_revision,membership,environment_revision) VALUES (?,?,?,?,?)',
                     (ref,source.ref,source.revision,'anchored' if kind!='session' else 'provisional',ctx.environment_revision))
        if previous:
            conn.execute('''INSERT INTO evidence_links(object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote)
                SELECT 'episode',object_ref,?,source_ref,source_revision,relation,quote FROM evidence_links
                WHERE object_kind='episode' AND object_ref=? AND object_revision=?''',(revision,ref,previous.revision))
            conn.execute('''INSERT INTO object_dependencies SELECT object_kind,object_ref,?,dependency_kind,dependency_ref,dependency_revision
                FROM object_dependencies WHERE object_kind='episode' AND object_ref=? AND object_revision=?''',(revision,ref,previous.revision))
        conn.execute('''INSERT INTO evidence_links(object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote)
            VALUES ('episode',?,?,?,?,'derived_from','') ON CONFLICT DO NOTHING''',(ref,revision,source.ref,source.revision))

    def sources(self,ref,*,after_sequence=0,limit=32):
        if self.get(ref) is None: raise ContractError('SOURCE_MISSING')
        if type(limit) is not int or not 1<=limit<=200 or type(after_sequence) is not int or after_sequence<0: raise ContractError('INPUT_INVALID','episode_page')
        rows=self.tx._check().execute('SELECT sequence,source_ref,source_revision FROM episode_events WHERE episode_id=? AND sequence>? ORDER BY sequence LIMIT ?',
                                     (ref,after_sequence,limit+1)).fetchall()
        items=tuple((r[0],s) for r in rows[:limit] if (s:=self.tx.source(r[1],r[2])) is not None)
        return items,(rows[limit-1][0] if len(rows)>limit else None)

    def apply_resume(self,proposal,scope_id,now):
        conn,ctx=self.tx._check(write=True),self.tx.context
        sources=[self.tx.source(*parse_source_ref(ref)) for ref in proposal['evidence_refs']]
        if any(s is None or (s.scope_id,s.project_id,s.branch_id)!=(scope_id,ctx.project_id,ctx.branch_id) for s in sources): raise ContractError('SOURCE_MISSING')
        for source in sources:self.tx.claims.require_live_source(source.ref,source.revision)
        episodes={self.source_episode(s.ref,s.revision).ref for s in sources}
        if len(episodes)!=1: raise ContractError('DERIVATION_INVALID','episode_membership_ambiguous')
        ref=next(iter(episodes)); current=self.get(ref)
        if proposal['episode_ref'] not in {None,ref}: raise ContractError('DERIVATION_INVALID','episode_membership')
        qualify_resume(proposal,sources)
        if not ctx.task_anchor and not supported_work_goal(proposal,sources):
            raise ContractError('DERIVATION_INVALID','work_goal_unconfirmed')
        payload=dict(proposal,episode_ref=ref)
        if current.resume==payload: return current
        for artifact in payload['artifact_refs']:
            target,revision=parse_source_ref(artifact)
            item=self.tx.artifacts.get(target,revision)
            if item is None: raise ContractError('SOURCE_MISSING')
            exact_version=dict(artifact_ref=target,revision=revision)
            observed=any(exact_version in s.event.get('display_snapshot',{}).get('items',[]) for s in sources)
            if not observed and not set(item.evidence_refs).intersection(payload['evidence_refs']):
                raise ContractError('DERIVATION_INVALID','artifact_version_evidence')
        revision=current.revision+1
        prior=conn.execute('SELECT processed_sequence FROM episode_versions WHERE episode_id=? AND revision=?',(ref,current.revision)).fetchone()[0]
        rows=conn.execute('SELECT sequence,source_ref,source_revision FROM episode_events WHERE episode_id=? AND sequence>? ORDER BY sequence',(ref,prior)).fetchall()
        declared=set(payload['evidence_refs']); processed=prior
        for row in rows:
            if f'{row[1]}@{row[2]}' not in declared: break
            processed=row[0]
        state=state_from_sources(sources,has_goal=True,previous=current.state)
        latest=self._latest_occurrence(ref)
        used_times=[canonical_time(s.event['occurred_at']) for s in sources if s.event['occurred_at']]
        if latest and (not used_times or max(used_times)<latest):state=current.state
        conn.execute('INSERT INTO episode_versions VALUES (?,?,?,?,?,?,?,?)',(ref,revision,state,canonical(payload),payload['source_watermark'],processed,now,ctx.environment_revision))
        conn.execute('UPDATE episodes SET current_revision=? WHERE episode_id=?',(revision,ref))
        for source in sources:
            conn.execute('''INSERT INTO evidence_links VALUES ('episode',?,?,?,?,'derived_from','',NULL)''',(ref,revision,source.ref,source.revision))
        for artifact in payload['artifact_refs']:
            target,version=parse_source_ref(artifact)
            conn.execute("INSERT INTO object_dependencies VALUES ('episode',?,?,'artifact',?,?)",(ref,revision,target,version))
        conn.execute('UPDATE instance_meta SET memory_epoch=memory_epoch+1 WHERE singleton=1')
        return self.get(ref)
