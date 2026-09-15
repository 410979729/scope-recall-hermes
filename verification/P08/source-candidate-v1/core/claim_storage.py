"""Claim repositories inside the existing transaction owner. No independent opens."""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from ..contracts import ClaimProposal, ContractError
from .claims import ClaimVersion, Qualification, RootEvidence, canonical_time, claim_slot

if TYPE_CHECKING:
    from .storage import Transaction


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def parse_source_ref(value: str) -> tuple[str, int]:
    try:
        ref, version = value.rsplit("@", 1)
        revision = int(version)
        if not ref or revision < 1 or version != str(revision):
            raise ValueError
        return ref, revision
    except (ValueError, AttributeError) as exc:
        raise ContractError("INPUT_INVALID", "source_ref") from exc


class Claims:
    def __init__(self, transaction: Transaction) -> None:
        self._tx = transaction

    def versions(self, ref: str) -> tuple[ClaimVersion, ...]:
        conn = self._tx._check()
        from .visibility import allowed
        if not allowed(self._tx,"claim",ref):
            return ()
        scopes = sorted(self._tx.context.allowed_scope_ids)
        rows = conn.execute(f"""SELECT c.*,v.* FROM claims c JOIN claim_versions v USING(claim_id)
            WHERE c.claim_id=? AND c.scope_id IN ({','.join('?' for _ in scopes)}) AND c.read_blocked=0
            AND (c.project_id IS NULL OR c.project_id=?) AND (c.branch_id IS NULL OR c.branch_id=?)
            ORDER BY v.revision""", (ref, *scopes, self._tx.context.project_id, self._tx.context.branch_id)).fetchall()
        return tuple(ClaimVersion(row["claim_id"],row["revision"],row["current_revision"],row["scope_id"],
                    row["project_id"],row["branch_id"],json.loads(row["payload_json"]),row["state"],row["basis"],
                    row["qualification_reason"],row["valid_from"],row["valid_to"],row["recorded_from"],row["recorded_to"],
                    row["replaces_revision"],tuple(json.loads(row["conflict_revisions_json"])),bool(row["suppressed"])) for row in rows)

    def slot(self, scope_id: str, proposal: ClaimProposal) -> tuple[ClaimVersion, ...]:
        self._tx._scope(scope_id)
        context = self._tx.context
        slot = claim_slot(scope_id, context.project_id, context.branch_id, proposal)
        row = self._tx._check().execute("SELECT claim_id,read_blocked FROM claims WHERE slot_key=?", (slot,)).fetchone()
        if row is None:
            return ()
        if row["read_blocked"]:
            raise ContractError("ACCESS_DENIED", "claim_unavailable")
        return self.versions(row["claim_id"])

    def list_refs(self, *, subject: str | None = None, predicate: str | None = None, limit: int = 200) -> tuple[str, ...]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ContractError("INPUT_INVALID", "claim_limit")
        ctx = self._tx.context
        scopes = sorted(ctx.allowed_scope_ids)
        filters = [f"scope_id IN ({','.join('?' for _ in scopes)})", "read_blocked=0",
                   "NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='claim' AND b.object_ref=claims.claim_id AND b.read_blocked=1)",
                   "(project_id IS NULL OR project_id=?)", "(branch_id IS NULL OR branch_id=?)"]
        params = [*scopes, ctx.project_id, ctx.branch_id]
        for name, value in (("subject", subject),("predicate", predicate)):
            if value is not None:
                filters.append(f"{name}=?"); params.append(value)
        return tuple(r[0] for r in self._tx._check().execute(f"SELECT claim_id FROM claims WHERE {' AND '.join(filters)} ORDER BY claim_id LIMIT ?", (*params,limit)))

    def roots(self, refs: tuple[str, ...]) -> tuple[RootEvidence, ...]:
        conn = self._tx._check()
        pending = [parse_source_ref(ref) for ref in refs]
        visited = set()
        roots = {}
        while pending:
            key = pending.pop()
            if key in visited:
                continue
            if len(visited) >= 200:
                raise ContractError("DERIVATION_INVALID", "evidence_depth")
            visited.add(key)
            source = self._tx.source(*key)
            if source is None:
                raise ContractError("SOURCE_MISSING")
            origin = source.event["origin"]
            if origin in {"human_direct", "tool_observation", "external_document", "imported"}:
                roots[key] = RootEvidence(source.ref,source.revision,origin,source.event.get("source_original_origin"),
                    source.event["content"],source.event["occurred_at"],source.event["capture_state"],source.session_id,
                    source.import_provenance_sha256 is not None,source.capture_gaps)
                continue
            links = conn.execute("SELECT source_ref,source_revision FROM evidence_links WHERE object_kind='event' AND object_ref=? AND object_revision=?", key).fetchall()
            pending.extend((r[0],r[1]) for r in links)
        return tuple(roots[k] for k in sorted(roots))

    def link_source(self, ref: str, revision: int) -> None:
        conn = self._tx._check(write=True)
        source = self._tx.source(ref, revision)
        if source is None:
            raise ContractError("SOURCE_MISSING")
        lineage_origin = source.event.get("source_original_origin") if source.event["origin"] == "imported" else source.event["origin"]
        if lineage_origin not in {"assistant_visible","host_generated","memory_reinjection","origin_unknown"}:
            return
        for text_ref in source.event["evidence_refs"]:
            try:
                key = parse_source_ref(text_ref)
            except ContractError:
                continue
            from .visibility import allowed
            if not allowed(self._tx,"event",key[0]):
                raise ContractError("SOURCE_MISSING","deleted_derivation")
            parent = self._tx.source(*key)
            if parent is None or (parent.ref,parent.revision) == (ref,revision):
                continue
            if (parent.scope_id,parent.project_id,parent.branch_id) != (source.scope_id,source.project_id,source.branch_id):
                continue
            conn.execute("""INSERT INTO evidence_links(object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote)
                VALUES ('event',?,?,?,?,'derived_from','') ON CONFLICT DO NOTHING""", (ref, revision, parent.ref, parent.revision))
            if parent.suppressed:
                conn.execute("UPDATE source_events SET suppressed=1 WHERE event_id=? AND source_revision=?",(ref,revision))

    def append(self, scope_id: str, proposal: ClaimProposal, qualification: Qualification, *, recorded_at: str,
               previous: ClaimVersion | None = None, advance_head: bool = True,
               conflicts: tuple[int, ...] = (), expected_revision: int | None = None) -> ClaimVersion:
        conn = self._tx._check(write=True)
        self._tx._scope(scope_id)
        ctx = self._tx.context
        recorded = canonical_time(recorded_at)
        slot_key = claim_slot(scope_id,ctx.project_id,ctx.branch_id,proposal)
        ref = previous.ref if previous else "claim-" + hashlib.sha256((ctx.binding.installation_id+":"+slot_key).encode()).hexdigest()
        from .visibility import allowed
        if not allowed(self._tx,"claim",ref):
            raise ContractError("ACCESS_DENIED","claim_unavailable")
        if previous:
            self.require_target(previous)
            if previous.scope_id != scope_id or claim_slot(scope_id,ctx.project_id,ctx.branch_id,previous.payload) != slot_key:
                raise ContractError("DERIVATION_INVALID", "claim_identity")
            current = conn.execute("SELECT current_revision,read_blocked FROM claims WHERE claim_id=?", (ref,)).fetchone()
            if current is None or current["read_blocked"]:
                raise ContractError("SOURCE_MISSING")
            if current["current_revision"] != (expected_revision if expected_revision is not None else previous.current_revision):
                raise ContractError("VERSION_CONFLICT")
            revision = conn.execute("SELECT max(revision)+1 FROM claim_versions WHERE claim_id=?", (ref,)).fetchone()[0]
        else:
            revision = 1
            conn.execute("""INSERT INTO claims(claim_id,scope_id,project_id,branch_id,subject,predicate,kind,slot_key,current_revision)
                VALUES (?,?,?,?,?,?,?,?,1)""", (ref,scope_id,ctx.project_id,ctx.branch_id,proposal["subject"],proposal["predicate"],proposal["kind"],slot_key))
        if previous and advance_head:
            conn.execute("UPDATE claim_versions SET recorded_to=? WHERE claim_id=? AND revision=?", (recorded,ref,previous.current_revision))
        conn.execute("""INSERT INTO claim_versions(claim_id,revision,payload_json,state,basis,qualification_reason,valid_from,valid_to,
            recorded_from,replaces_revision,conflict_revisions_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (ref,revision,_json(proposal),qualification.state,
            qualification.basis,qualification.reason,canonical_time(proposal["valid_from"]),canonical_time(proposal["valid_to"]),
            recorded,previous.current_revision if previous and advance_head else None,_json(conflicts)))
        if previous and advance_head:
            conn.execute("UPDATE claims SET current_revision=? WHERE claim_id=?", (revision,ref))
        for span in proposal["evidence_spans"]:
            source = self._tx.source(span["source_ref"], span["source_revision"])
            if source is None or (source.scope_id,source.project_id,source.branch_id) != (scope_id,ctx.project_id,ctx.branch_id) or span["quote"] not in source.event["content"]:
                raise ContractError("DERIVATION_INVALID", "evidence_span")
            self.require_live_source(source.ref, source.revision)
            if source.suppressed:
                conn.execute("UPDATE claims SET suppressed=1 WHERE claim_id=?",(ref,))
            conn.execute("""INSERT INTO evidence_links(object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote,location)
                VALUES ('claim',?,?,?,?,'supports',?,?) ON CONFLICT DO NOTHING""", (ref,revision,source.ref,source.revision,span["quote"],span.get("location")))
        for relation,refs in (("contradicts",proposal.get("procedure",{}).get("counterexample_refs",[])),
                              ("supports",proposal.get("intention",{}).get("state_evidence_refs",[]))):
            for evidence_ref in refs:
                key = parse_source_ref(evidence_ref)
                self.require_live_source(*key)
                source = self._tx.source(*key)
                if source.scope_id != scope_id:
                    raise ContractError("ACCESS_DENIED", "evidence_scope")
                conn.execute("""INSERT INTO evidence_links(object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote)
                    VALUES ('claim',?,?,?,?,?,'') ON CONFLICT DO NOTHING""", (ref,revision,*key,relation))
        conn.execute("UPDATE instance_meta SET memory_epoch=memory_epoch+1 WHERE singleton=1")
        if advance_head and qualification.state != "proposed":
            conn.execute("UPDATE work_items SET state='obsolete' WHERE subject_ref=? AND state IN ('pending','leased')", (ref,))
            conn.execute("INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,available_at) VALUES ('rebuild_projection',?,?,?,?,?,?)", (ref,revision,scope_id,ctx.project_id,ctx.branch_id,recorded))
        return next(v for v in self.versions(ref) if v.revision == revision)

    def require_target(self, version: ClaimVersion) -> None:
        ctx = self._tx.context
        self._tx._scope(version.scope_id)
        if (version.project_id, version.branch_id) != (ctx.project_id, ctx.branch_id):
            raise ContractError("ACCESS_DENIED", "claim_context")

    def require_live_source(self, ref: str, revision: int) -> None:
        source = self._tx.source(ref, revision)
        ctx = self._tx.context
        if source is None or (source.project_id,source.branch_id) != (ctx.project_id,ctx.branch_id):
            raise ContractError("SOURCE_MISSING")
        row = self._tx._check().execute("""SELECT 1 FROM source_events e WHERE e.event_id=? AND e.source_revision=?
            AND NOT EXISTS(SELECT 1 FROM source_events newer WHERE newer.source_group_key=e.source_group_key AND newer.source_revision>e.source_revision)""", (ref,revision)).fetchone()
        if row is None:
            raise ContractError("VERSION_CONFLICT", "source_revision")

    def current_human(self, refs: tuple[str, ...], scope_id: str):
        ctx = self._tx.context
        row = self._tx._check().execute("""SELECT event_id,source_revision FROM source_events WHERE origin='human_direct'
            AND session_id=? AND scope_id=? AND project_id IS ? AND branch_id IS ? AND read_blocked=0 ORDER BY rowid DESC LIMIT 1""",
            (ctx.session_id,scope_id,ctx.project_id,ctx.branch_id)).fetchone()
        if row is None or f"{row[0]}@{row[1]}" not in refs:
            raise ContractError("ACCESS_DENIED", "current_human_evidence")
        source = self._tx.source(row[0],row[1])
        self.require_live_source(row[0],row[1])
        if source.capture_gaps or source.event["capture_state"] != "complete":
            raise ContractError("ACCESS_DENIED", "incomplete_authorization")
        if ctx.recent_messages and source.event["content"] not in ctx.recent_messages[-1]:
            raise ContractError("ACCESS_DENIED", "current_human_evidence")
        return source

    def unresolved(self, source_ref: str, source_revision: int, candidates: tuple[str, ...], *, recorded_at: str) -> str:
        conn = self._tx._check(write=True)
        source = self._tx.source(source_ref, source_revision)
        if source is None:
            raise ContractError("SOURCE_MISSING")
        self.require_live_source(source_ref,source_revision)
        for target in candidates:
            versions = self.versions(target)
            if not versions or versions[0].scope_id != source.scope_id:
                raise ContractError("SOURCE_MISSING")
            self.require_target(versions[0])
        ref = "update-" + hashlib.sha256(f"{source_ref}@{source_revision}".encode()).hexdigest()
        result = conn.execute("""INSERT INTO unresolved_updates(update_id,source_ref,source_revision,scope_id,project_id,branch_id,candidate_refs_json,created_at)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(update_id) DO NOTHING""", (ref,source_ref,source_revision,source.scope_id,source.project_id,source.branch_id,_json(candidates),canonical_time(recorded_at)))
        if result.rowcount:
            conn.execute("UPDATE instance_meta SET memory_epoch=memory_epoch+1 WHERE singleton=1")
        return ref

    def unresolved_updates(self) -> tuple[dict, ...]:
        ctx = self._tx.context
        scopes = sorted(ctx.allowed_scope_ids)
        rows = self._tx._check().execute(f"""SELECT update_id,source_ref,source_revision,candidate_refs_json FROM unresolved_updates
            WHERE state='unresolved' AND scope_id IN ({','.join('?' for _ in scopes)}) AND project_id IS ? AND branch_id IS ? ORDER BY created_at,update_id""",
            (*scopes,ctx.project_id,ctx.branch_id)).fetchall()
        return tuple(dict(ref=r[0],source_ref=r[1],source_revision=r[2],candidate_refs=tuple(json.loads(r[3]))) for r in rows)
