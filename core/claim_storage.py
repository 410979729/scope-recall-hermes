"""Claim repositories inside the existing transaction owner. No independent opens."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from ..contracts import ClaimProposal, ContractError
from . import lineage
from .claim_normalization import PROJECT_TAG, normalize_frame
from .claims import ClaimVersion, Qualification, RootEvidence, canonical_time, claim_slot, evidence_refs
from .delete_storage import canonical
from .visibility import allowed, allowed_refs

if TYPE_CHECKING:
    from .storage import Transaction


def parse_source_ref(value: str) -> tuple[str, int]:
    try:
        ref, version = value.rsplit("@", 1)
        revision = int(version)
        if not ref or revision < 1 or version != str(revision):
            raise ValueError
        return ref, revision
    except (ValueError, AttributeError) as exc:
        raise ContractError("INPUT_INVALID", "source_ref") from exc


#: The columns a claim version is built from (``_claim_version``).
_VERSION_COLUMNS = (
    "c.claim_id",
    "v.revision",
    "c.current_revision",
    "c.scope_id",
    "c.project_id",
    "c.branch_id",
    "v.payload_json",
    "v.state",
    "v.basis",
    "v.qualification_reason",
    "v.valid_from",
    "v.valid_to",
    "v.recorded_from",
    "v.recorded_to",
    "v.replaces_revision",
    "v.conflict_revisions_json",
    "c.suppressed",
)
#: Claims ``Claims.prefetch_versions`` loads per statement.
_PREFETCH_PAGE = 400


def _versions_size(rows) -> int:
    """The text a claim's remembered versions hold (``Transaction.remember``)."""
    return sum(len(row["payload_json"]) for row in rows)


def _claim_version(row) -> ClaimVersion:
    """A claim version built afresh from its row, so that no reader shares another's payload."""
    return ClaimVersion(
        row["claim_id"],
        row["revision"],
        row["current_revision"],
        row["scope_id"],
        row["project_id"],
        row["branch_id"],
        json.loads(row["payload_json"]),
        row["state"],
        row["basis"],
        row["qualification_reason"],
        row["valid_from"],
        row["valid_to"],
        row["recorded_from"],
        row["recorded_to"],
        row["replaces_revision"],
        tuple(json.loads(row["conflict_revisions_json"])),
        bool(row["suppressed"]),
    )


class Claims:
    def __init__(self, transaction: Transaction) -> None:
        self._tx = transaction

    def versions(self, ref: str) -> tuple[ClaimVersion, ...]:
        conn = self._tx._check()

        if not allowed(self._tx, "claim", ref):
            return ()
        scopes = sorted(self._tx.context.allowed_scope_ids)
        rows = self._tx.remembered(
            ("versions", ref),
            lambda: tuple(
                conn.execute(
                    f"""SELECT {",".join(_VERSION_COLUMNS)} FROM claims c JOIN claim_versions v USING(claim_id)
            WHERE c.claim_id=? AND c.scope_id IN ({",".join("?" for _ in scopes)}) AND c.read_blocked=0
            AND (c.project_id IS NULL OR c.project_id=?) AND (c.branch_id IS NULL OR c.branch_id=?)
            ORDER BY v.revision""",
                    (ref, *scopes, self._tx.context.project_id, self._tx.context.branch_id),
                ).fetchall()
            ),
            size=_versions_size,
        )
        return tuple(_claim_version(row) for row in rows)

    def prefetch_versions(self, refs) -> None:
        """Load the versions of these claims into a read transaction's memory, with whether ``visibility.allowed``
        admits each: two statements and two rows a page, however many claims there are.  A recall read each candidate
        claim's versions on its own, and in a busy Hermes gateway every statement and row waited for the GIL (3.7.7,
        ``Transaction.remembered``).  A write transaction loads nothing ahead."""
        tx = self._tx
        if not tx.remembers:
            return

        wanted = [ref for ref in dict.fromkeys(refs) if type(ref) is str and not tx.knows(("versions", ref))]
        conn = tx._check()
        scopes = sorted(tx.context.allowed_scope_ids)
        fields = ",".join(f"'{column.split('.', 1)[1]}',{column}" for column in _VERSION_COLUMNS)
        # On the primary key, never the scope's index (``Transaction.prefetch_sources``).
        for start in range(0, len(wanted), _PREFETCH_PAGE):
            page = wanted[start : start + _PREFETCH_PAGE]
            admitted = allowed_refs(tx, "claim", page)
            row = conn.execute(
                f"""SELECT json_group_array(json_object({fields})) FROM claims c JOIN claim_versions v USING(claim_id)
                WHERE c.claim_id IN ({",".join("?" for _ in page)}) AND +c.scope_id IN ({",".join("?" for _ in scopes)})
                AND +c.read_blocked=0 AND (c.project_id IS NULL OR c.project_id=?) AND (c.branch_id IS NULL OR c.branch_id=?)""",
                (*page, *scopes, tx.context.project_id, tx.context.branch_id),
            ).fetchone()
            by_claim: dict[str, list[dict]] = {}
            for item in json.loads(row[0]):
                by_claim.setdefault(item["claim_id"], []).append(item)
            for ref in page:
                if ref in admitted:
                    rows = tuple(sorted(by_claim.get(ref, ()), key=lambda item: item["revision"]))
                    tx.remember(("versions", ref), rows, size=_versions_size(rows))

    def version(self, ref: str, revision: int) -> ClaimVersion | None:
        """Load one visible claim version without scanning its history."""
        conn = self._tx._check()

        if not allowed(self._tx, "claim", ref):
            return None
        scopes = sorted(self._tx.context.allowed_scope_ids)
        row = conn.execute(
            f"""SELECT c.*,v.* FROM claims c JOIN claim_versions v USING(claim_id)
            WHERE c.claim_id=? AND v.revision=? AND c.scope_id IN ({",".join("?" for _ in scopes)})
            AND c.read_blocked=0 AND (c.project_id IS NULL OR c.project_id=?)
            AND (c.branch_id IS NULL OR c.branch_id=?)""",
            (ref, revision, *scopes, self._tx.context.project_id, self._tx.context.branch_id),
        ).fetchone()
        if row is None:
            return None
        return _claim_version(row)

    def current_revision(self, ref: str) -> int | None:
        """Resolve a visible claim head without loading its historical versions."""
        if not allowed(self._tx, "claim", ref):
            return None
        scopes = sorted(self._tx.context.allowed_scope_ids)
        row = (
            self._tx._check()
            .execute(
                f"""SELECT current_revision FROM claims WHERE claim_id=? AND read_blocked=0
            AND scope_id IN ({",".join("?" for _ in scopes)})
            AND (project_id IS NULL OR project_id=?) AND (branch_id IS NULL OR branch_id=?)""",
                (ref, *scopes, self._tx.context.project_id, self._tx.context.branch_id),
            )
            .fetchone()
        )
        return int(row["current_revision"]) if row is not None else None

    def slot(self, scope_id: str, proposal: ClaimProposal) -> tuple[ClaimVersion, ...]:
        self._tx._scope(scope_id)
        context = self._tx.context
        slot = claim_slot(scope_id, context.project_id, context.branch_id, proposal)
        row = self._tx._check().execute("SELECT claim_id,read_blocked FROM claims WHERE slot_key=?", (slot,)).fetchone()
        if row is None:
            # Legacy extraction could wrap a literal frame in a project prefix
            # or put the subject/verb inside a preference value. Reuse only a
            # uniquely equivalent, freshly grounded slot; similarity is never
            # enough to merge identities.

            projects = [p for c in proposal["conditions"] for p in PROJECT_TAG.findall(c)]
            if len(set(projects)) != 1:
                return ()
            candidates = (
                self._tx._check()
                .execute(
                    """SELECT c.claim_id FROM claims c
                JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revision=c.current_revision
                WHERE c.scope_id=? AND c.project_id IS ? AND c.branch_id IS ?
                  AND c.read_blocked=0 AND c.suppressed=0 AND c.kind=?
                  AND v.state IN ('active','proposed','disputed') AND instr(v.payload_json,?)>0
                ORDER BY c.claim_id LIMIT 201""",
                    (scope_id, context.project_id, context.branch_id, proposal["kind"], projects[0]),
                )
                .fetchall()
            )
            if len(candidates) > 200:
                raise ContractError("VERSION_CONFLICT", "ambiguous_normalized_slot")
            matches = []
            for candidate in candidates:
                versions = self.versions(candidate[0])
                head = next((v for v in versions if v.revision == v.current_revision), None)
                if head is None:
                    continue
                try:
                    normalized = normalize_frame(head.payload, self.roots(evidence_refs(head.payload)))
                except ContractError:
                    continue
                if claim_slot(scope_id, context.project_id, context.branch_id, normalized) == slot:
                    matches.append((head, versions))
            active = [m for m in matches if m[0].state in {"active", "disputed"}]
            eligible = active or matches
            if len(eligible) > 1:
                raise ContractError("VERSION_CONFLICT", "ambiguous_normalized_slot")
            return eligible[0][1] if eligible else ()
        if row["read_blocked"]:
            raise ContractError("ACCESS_DENIED", "claim_unavailable")
        return self.versions(row["claim_id"])

    def list_refs(
        self,
        *,
        subject: str | None = None,
        predicate: str | None = None,
        limit: int = 200,
        kind: str | None = None,
        value_text: str | None = None,
        alias_name: str | None = None,
    ) -> tuple[str, ...]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ContractError("INPUT_INVALID", "claim_limit")
        ctx = self._tx.context
        scopes = sorted(ctx.allowed_scope_ids)
        if kind is not None and (
            type(kind) is not str
            or kind
            not in {
                "fact",
                "preference",
                "constraint",
                "decision",
                "procedure",
                "intention",
                "alias",
            }
        ):
            raise ContractError("INPUT_INVALID", "kind")
        if value_text is not None and (type(value_text) is not str or not 1 <= len(value_text) <= 8192):
            raise ContractError("INPUT_INVALID", "value_text")
        if alias_name is not None and (type(alias_name) is not str or not 1 <= len(alias_name) <= 240):
            raise ContractError("INPUT_INVALID", "alias_name")
        if value_text is None and alias_name is None:
            filters = [
                f"scope_id IN ({','.join('?' for _ in scopes)})",
                "read_blocked=0",
                "NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='claim' AND b.object_ref=claims.claim_id AND b.read_blocked=1)",
                "(project_id IS NULL OR project_id=?)",
                "(branch_id IS NULL OR branch_id=?)",
            ]
            params: list[object] = [*scopes, ctx.project_id, ctx.branch_id]
            for name, value in (("subject", subject), ("predicate", predicate), ("kind", kind)):
                if value is not None:
                    filters.append(f"{name}=?")
                    params.append(value)
            return tuple(
                r[0]
                for r in self._tx._check().execute(
                    f"SELECT claim_id FROM claims WHERE {' AND '.join(filters)} ORDER BY claim_id LIMIT ?",
                    (*params, limit),
                )
            )
        filters = [
            f"c.scope_id IN ({','.join('?' for _ in scopes)})",
            "c.read_blocked=0",
            "NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='claim' AND b.object_ref=c.claim_id AND b.read_blocked=1)",
            "(c.project_id IS NULL OR c.project_id=?)",
            "(c.branch_id IS NULL OR c.branch_id=?)",
        ]
        params = [*scopes, ctx.project_id, ctx.branch_id]
        for name, value in (("subject", subject), ("predicate", predicate), ("kind", kind)):
            if value is not None:
                filters.append(f"c.{name}=?")
                params.append(value)
        if value_text is not None:
            filters.append("json_extract(v.payload_json,'$.value_text')=?")
            params.append(value_text)
        if alias_name is not None:
            filters.append("c.kind='alias'")
            filters.append(
                "(json_extract(v.payload_json,'$.value_text')=? OR json_extract(v.payload_json,'$.alias.name')=?)"
            )
            params.extend((alias_name, alias_name))
        source = "claims c JOIN claim_versions v ON v.claim_id=c.claim_id"
        return tuple(
            r[0]
            for r in self._tx._check().execute(
                f"SELECT DISTINCT c.claim_id FROM {source} WHERE {' AND '.join(filters)} ORDER BY c.claim_id LIMIT ?",
                (*params, limit),
            )
        )

    def correction_refs(self, text: str, scope_id: str, *, limit: int = 200) -> tuple[str, ...]:
        """Find explicit targets before applying the bounded correction cap.

        Taking the first N claims and *then* matching silently loses corrections
        as a memory grows. This read is scoped to the exact source context and
        still requires the mutation layer to prove a unique authorized target.
        """
        self._tx._scope(scope_id)
        if type(text) is not str or len(text) > 65536 or type(limit) is not int or not 1 <= limit <= 200:
            raise ContractError("INPUT_INVALID", "correction_targets")
        rows = (
            self._tx._check()
            .execute(
                """SELECT c.claim_id FROM claims c JOIN claim_versions v
               ON v.claim_id=c.claim_id AND v.revision=c.current_revision
               WHERE c.scope_id=? AND c.project_id IS ? AND c.branch_id IS ?
                 AND c.read_blocked=0 AND v.state IN ('active','disputed')
                 AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE
                     b.object_kind='claim' AND b.object_ref=c.claim_id AND b.read_blocked=1)
                 AND (instr(?,c.claim_id)>0 OR instr(?,c.subject)>0
                      OR instr(?,json_extract(v.payload_json,'$.value_text'))>0)
               ORDER BY c.claim_id LIMIT ?""",
                (scope_id, self._tx.context.project_id, self._tx.context.branch_id, text, text, text, limit + 1),
            )
            .fetchall()
        )
        # Too many possible targets is ambiguity, never permission to choose.
        if len(rows) > limit:
            return ()
        return tuple(row[0] for row in rows)

    def proposed_refs(self, text: str, scope_id: str, *, limit: int = 200) -> tuple[str, ...]:
        """Proposals this text explicitly names, for the confirmation path.

        A sibling of ``correction_refs`` rather than a flag on it: correction
        acts on claims that are already believed, confirmation acts on ones
        that are not, and sharing one query would make it one edit away from
        letting a correction retarget a proposal or the reverse.
        """
        self._tx._scope(scope_id)
        if type(text) is not str or len(text) > 65536 or type(limit) is not int or not 1 <= limit <= 200:
            raise ContractError("INPUT_INVALID", "confirmation_targets")
        rows = (
            self._tx._check()
            .execute(
                """SELECT c.claim_id FROM claims c JOIN claim_versions v
               ON v.claim_id=c.claim_id AND v.revision=c.current_revision
               WHERE c.scope_id=? AND c.project_id IS ? AND c.branch_id IS ?
                 AND c.read_blocked=0 AND c.suppressed=0 AND v.state='proposed'
                 AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE
                     b.object_kind='claim' AND b.object_ref=c.claim_id
                     AND (b.read_blocked=1 OR b.suppressed=1))
                 AND (instr(?,c.claim_id)>0 OR instr(?,c.subject)>0
                      OR instr(?,json_extract(v.payload_json,'$.predicate'))>0
                      OR instr(?,json_extract(v.payload_json,'$.value_text'))>0)
               ORDER BY c.claim_id LIMIT ?""",
                (scope_id, self._tx.context.project_id, self._tx.context.branch_id, text, text, text, text, limit + 1),
            )
            .fetchall()
        )
        # Too many possible targets is ambiguity, never permission to choose.
        if len(rows) > limit:
            return ()
        return tuple(row[0] for row in rows)

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
                roots[key] = RootEvidence(
                    source.ref,
                    source.revision,
                    origin,
                    source.event.get("source_original_origin"),
                    source.event["content"],
                    source.event["occurred_at"],
                    source.event["capture_state"],
                    source.session_id,
                    source.import_provenance_sha256 is not None,
                    source.capture_gaps,
                    source.event.get("source_principal"),
                )
                continue
            pending.extend(lineage.evidence(conn, "event", *key))
        return tuple(roots[k] for k in sorted(roots))

    def link_source(self, ref: str, revision: int) -> None:
        conn = self._tx._check(write=True)
        source = self._tx.source(ref, revision)
        if source is None:
            raise ContractError("SOURCE_MISSING")
        lineage_origin = (
            source.event.get("source_original_origin")
            if source.event["origin"] == "imported"
            else source.event["origin"]
        )
        if lineage_origin not in {"assistant_visible", "host_generated", "memory_reinjection", "origin_unknown"}:
            return
        for text_ref in source.event["evidence_refs"]:
            try:
                key = parse_source_ref(text_ref)
            except ContractError:
                continue

            if not allowed(self._tx, "event", key[0]):
                raise ContractError("SOURCE_MISSING", "deleted_derivation")
            parent = self._tx.source(*key)
            if parent is None or (parent.ref, parent.revision) == (ref, revision):
                continue
            if (parent.scope_id, parent.project_id, parent.branch_id) != (
                source.scope_id,
                source.project_id,
                source.branch_id,
            ):
                continue
            lineage.link(conn, "event", ref, revision, parent.ref, parent.revision)
            if parent.suppressed:
                conn.execute(
                    "UPDATE source_events SET suppressed=1 WHERE event_id=? AND source_revision=?", (ref, revision)
                )

    def append(
        self,
        scope_id: str,
        proposal: ClaimProposal,
        qualification: Qualification,
        *,
        recorded_at: str,
        previous: ClaimVersion | None = None,
        advance_head: bool = True,
        conflicts: tuple[int, ...] = (),
        expected_revision: int | None = None,
    ) -> ClaimVersion:
        conn = self._tx._check(write=True)
        self._tx._scope(scope_id)
        ctx = self._tx.context
        recorded = canonical_time(recorded_at)
        slot_key = claim_slot(scope_id, ctx.project_id, ctx.branch_id, proposal)
        ref = (
            previous.ref
            if previous
            else "claim-" + hashlib.sha256((ctx.binding.installation_id + ":" + slot_key).encode()).hexdigest()
        )

        if not allowed(self._tx, "claim", ref):
            raise ContractError("ACCESS_DENIED", "claim_unavailable")
        if previous:
            self.require_target(previous)
            previous_slot = claim_slot(scope_id, ctx.project_id, ctx.branch_id, previous.payload)
            if previous_slot != slot_key:
                previous_slot = claim_slot(
                    scope_id,
                    ctx.project_id,
                    ctx.branch_id,
                    normalize_frame(previous.payload, self.roots(evidence_refs(previous.payload))),
                )
            if previous.scope_id != scope_id or previous_slot != slot_key:
                raise ContractError("DERIVATION_INVALID", "claim_identity")
            current = conn.execute(
                "SELECT current_revision,read_blocked FROM claims WHERE claim_id=?", (ref,)
            ).fetchone()
            if current is None or current["read_blocked"]:
                raise ContractError("SOURCE_MISSING")
            if current["current_revision"] != (
                expected_revision if expected_revision is not None else previous.current_revision
            ):
                raise ContractError("VERSION_CONFLICT")
            revision = conn.execute("SELECT max(revision)+1 FROM claim_versions WHERE claim_id=?", (ref,)).fetchone()[0]
        else:
            revision = 1
            conn.execute(
                """INSERT INTO claims(claim_id,scope_id,project_id,branch_id,subject,predicate,kind,slot_key,current_revision)
                VALUES (?,?,?,?,?,?,?,?,1)""",
                (
                    ref,
                    scope_id,
                    ctx.project_id,
                    ctx.branch_id,
                    proposal["subject"],
                    proposal["predicate"],
                    proposal["kind"],
                    slot_key,
                ),
            )
        if previous and advance_head:
            conn.execute(
                "UPDATE claim_versions SET recorded_to=? WHERE claim_id=? AND revision=?",
                (recorded, ref, previous.current_revision),
            )
        conn.execute(
            """INSERT INTO claim_versions(claim_id,revision,payload_json,state,basis,qualification_reason,valid_from,valid_to,
            recorded_from,replaces_revision,conflict_revisions_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ref,
                revision,
                canonical(proposal),
                qualification.state,
                qualification.basis,
                qualification.reason,
                canonical_time(proposal["valid_from"]),
                canonical_time(proposal["valid_to"]),
                recorded,
                previous.current_revision if previous and advance_head else None,
                canonical(conflicts),
            ),
        )
        if previous and advance_head:
            conn.execute(
                "UPDATE claims SET current_revision=?,slot_key=?,subject=?,predicate=? WHERE claim_id=?",
                (revision, slot_key, proposal["subject"], proposal["predicate"], ref),
            )
        for span in proposal["evidence_spans"]:
            source = self._tx.source(span["source_ref"], span["source_revision"])
            # Three different findings once shared one name, so a diagnostic record
            # said ``evidence_span`` and left an operator to guess which: a source
            # that is not there, one that belongs to someone else, or a quote that
            # is in neither.  Only the third is about what the model wrote.
            if source is None:
                raise ContractError("DERIVATION_INVALID", "evidence_source_missing")
            if (source.scope_id, source.project_id, source.branch_id) != (scope_id, ctx.project_id, ctx.branch_id):
                raise ContractError("DERIVATION_INVALID", "evidence_scope")
            if span["quote"] not in source.event["content"]:
                raise ContractError("DERIVATION_INVALID", "evidence_quote")
            self.require_live_source(source.ref, source.revision)
            if source.suppressed:
                conn.execute("UPDATE claims SET suppressed=1 WHERE claim_id=?", (ref,))
            lineage.link(
                conn,
                "claim",
                ref,
                revision,
                source.ref,
                source.revision,
                relation="supports",
                quote=span["quote"],
                location=span.get("location"),
            )
        for relation, refs in (
            ("contradicts", proposal.get("procedure", {}).get("counterexample_refs", [])),
            ("supports", proposal.get("intention", {}).get("state_evidence_refs", [])),
        ):
            for evidence_ref in refs:
                key = parse_source_ref(evidence_ref)
                self.require_live_source(*key)
                source = self._tx.source(*key)
                if source is None:
                    raise ContractError("SOURCE_MISSING")
                if source.scope_id != scope_id:
                    raise ContractError("ACCESS_DENIED", "evidence_scope")
                lineage.link(conn, "claim", ref, revision, *key, relation=relation)
        conn.execute("UPDATE instance_meta SET memory_epoch=memory_epoch+1 WHERE singleton=1")
        if advance_head and qualification.state != "proposed":
            # The old revision's work is moot, except an embed that has held a lease: its vector may have landed
            # already, and it completes against its own revision, which stays readable, as one done a moment earlier
            # would have.  That includes one sent back to wait after it wrote (a dependency or its deadline moved).
            # Made obsolete, it would leave a point in the store no ledger expected.
            conn.execute(
                """UPDATE work_items SET state='obsolete' WHERE subject_ref=? AND state IN ('pending','leased')
                            AND NOT (work_type='embed' AND lease_token>0)""",
                (ref,),
            )
            conn.execute(
                "INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,available_at) VALUES ('rebuild_projection',?,?,?,?,?,?)",
                (ref, revision, scope_id, ctx.project_id, ctx.branch_id, recorded),
            )
        # Every head, promoted or not, is queued for the vector index. Proposals
        # are admitted to recall labelled, and search cannot surface what it has
        # no vector for: the lexical, vector and recent channels all yield
        # events, leaving relation expansion as a claim's only other route in.
        if advance_head:
            conn.execute(
                """INSERT INTO work_items(work_type,subject_ref,subject_revision,scope_id,project_id,branch_id,available_at)
                   VALUES ('embed',?,?,?,?,?,?) ON CONFLICT(work_type,subject_ref,subject_revision) DO NOTHING""",
                (ref, revision, scope_id, ctx.project_id, ctx.branch_id, recorded),
            )
        return next(v for v in self.versions(ref) if v.revision == revision)

    def require_target(self, version: ClaimVersion) -> None:
        ctx = self._tx.context
        self._tx._scope(version.scope_id)
        if (version.project_id, version.branch_id) != (ctx.project_id, ctx.branch_id):
            raise ContractError("ACCESS_DENIED", "claim_context")

    def live_sources(self, refs, scope_id: str) -> list:
        """The sources ``refs`` cite, each in ``scope_id`` and this context's project and branch and the newest
        version of its group; SOURCE_MISSING otherwise."""
        ctx = self._tx.context
        cited = [self._tx.source(*parse_source_ref(ref)) for ref in refs]
        sources = [
            s
            for s in cited
            if s is not None and (s.scope_id, s.project_id, s.branch_id) == (scope_id, ctx.project_id, ctx.branch_id)
        ]
        if len(sources) != len(cited):
            raise ContractError("SOURCE_MISSING")
        for source in sources:
            self.require_live_source(source.ref, source.revision)
        return sources

    def require_live_source(self, ref: str, revision: int) -> None:
        source = self._tx.source(ref, revision)
        ctx = self._tx.context
        if source is None or (source.project_id, source.branch_id) != (ctx.project_id, ctx.branch_id):
            raise ContractError("SOURCE_MISSING")
        # Whether it is its group's newest version, once per read transaction (``Transaction.prefetch_sources``).
        head = self._tx.remembered(
            ("head", ref, revision),
            lambda: (
                self._tx._check()
                .execute(
                    """SELECT 1 FROM source_events e WHERE e.event_id=? AND e.source_revision=?
            AND NOT EXISTS(SELECT 1 FROM source_events newer WHERE newer.source_group_key=e.source_group_key AND newer.source_revision>e.source_revision)""",
                    (ref, revision),
                )
                .fetchone()
                is not None
            ),
        )
        if not head:
            raise ContractError("VERSION_CONFLICT", "source_revision")

    def current_human(self, refs: tuple[str, ...], scope_id: str):
        ctx = self._tx.context
        row = (
            self._tx._check()
            .execute(
                """SELECT event_id,source_revision FROM source_events WHERE origin='human_direct'
            AND session_id=? AND scope_id=? AND project_id IS ? AND branch_id IS ? AND read_blocked=0 ORDER BY rowid DESC LIMIT 1""",
                (ctx.session_id, scope_id, ctx.project_id, ctx.branch_id),
            )
            .fetchone()
        )
        if row is None or f"{row[0]}@{row[1]}" not in refs:
            raise ContractError("ACCESS_DENIED", "current_human_evidence")
        source = self._tx.source(row[0], row[1])
        if source is None:
            raise ContractError("SOURCE_MISSING")
        self.require_live_source(row[0], row[1])
        if source.capture_gaps or source.event["capture_state"] != "complete":
            raise ContractError("ACCESS_DENIED", "incomplete_authorization")
        if ctx.recent_messages and source.event["content"] not in ctx.recent_messages[-1]:
            raise ContractError("ACCESS_DENIED", "current_human_evidence")
        return source

    def unresolved(
        self, source_ref: str, source_revision: int, candidates: tuple[str, ...], *, recorded_at: str
    ) -> str:
        conn = self._tx._check(write=True)
        source = self._tx.source(source_ref, source_revision)
        if source is None:
            raise ContractError("SOURCE_MISSING")
        self.require_live_source(source_ref, source_revision)
        for target in candidates:
            versions = self.versions(target)
            if not versions or versions[0].scope_id != source.scope_id:
                raise ContractError("SOURCE_MISSING")
            self.require_target(versions[0])
        ref = "update-" + hashlib.sha256(f"{source_ref}@{source_revision}".encode()).hexdigest()
        result = conn.execute(
            """INSERT INTO unresolved_updates(update_id,source_ref,source_revision,scope_id,project_id,branch_id,candidate_refs_json,created_at)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(update_id) DO NOTHING""",
            (
                ref,
                source_ref,
                source_revision,
                source.scope_id,
                source.project_id,
                source.branch_id,
                canonical(candidates),
                canonical_time(recorded_at),
            ),
        )
        if result.rowcount:
            conn.execute("UPDATE instance_meta SET memory_epoch=memory_epoch+1 WHERE singleton=1")
        return ref

    def unresolved_updates(self) -> tuple[dict, ...]:
        ctx = self._tx.context
        scopes = sorted(ctx.allowed_scope_ids)
        rows = (
            self._tx._check()
            .execute(
                f"""SELECT update_id,source_ref,source_revision,candidate_refs_json FROM unresolved_updates
            WHERE state='unresolved' AND scope_id IN ({",".join("?" for _ in scopes)}) AND project_id IS ? AND branch_id IS ? ORDER BY created_at,update_id""",
                (*scopes, ctx.project_id, ctx.branch_id),
            )
            .fetchall()
        )
        return tuple(
            dict(ref=r[0], source_ref=r[1], source_revision=r[2], candidate_refs=tuple(json.loads(r[3]))) for r in rows
        )

    def close_unplaceable_updates(self, *, limit: int = 64) -> int:
        """Close open rows that name no candidate claim: nothing can ever settle them.

        Releases before 3.4.0rc10 recorded a correction with no claim in scope as such a row
        (``capture_correction``); 36 were open on the pilot, handed to every read.  They are marked ``obsolete``,
        the state a deletion leaves too, since nothing was settled.
        """
        if type(limit) is not int or not 1 <= limit <= 256:
            raise ContractError("INPUT_INVALID", "unresolved_limit")
        ctx = self._tx.context
        scopes = sorted(ctx.allowed_scope_ids)
        return (
            self._tx._check(write=True)
            .execute(
                f"""UPDATE unresolved_updates SET state='obsolete'
            WHERE update_id IN (SELECT update_id FROM unresolved_updates WHERE state='unresolved'
              AND scope_id IN ({",".join("?" for _ in scopes)}) AND project_id IS ? AND branch_id IS ?
              AND json_array_length(candidate_refs_json)=0 ORDER BY created_at,update_id LIMIT ?)""",
                (*scopes, ctx.project_id, ctx.branch_id, limit),
            )
            .rowcount
        )

    def resolve_updates(self, claim_ref: str, *, resolved_at: str) -> int:
        """Close ambiguity rows once one of their candidates is actually revised.

        ``unresolved`` records a correction the bounded path could not place:
        either several claims matched or the times ran backwards.  The schema
        has always had ``resolved``/``resolved_at`` and nothing ever wrote them,
        so a row that the user went on to settle stayed open for good -- surfaced
        to the host on every read, and bumping ``memory_epoch`` on the way in.
        ``obsolete`` had a writer (deletion); the settled case had none.

        An authorized revision of one of the named candidates is that answer:
        it is the same human, through the path that requires the target to be
        literally bound, doing what the ambiguous message asked for.  A
        ``duplicate`` return is deliberately not routed here -- nothing changed,
        so nothing was settled.  Scoping matches ``unresolved_updates`` exactly,
        so a row can only be closed by a caller that could have read it.
        """
        ctx = self._tx.context
        scopes = sorted(ctx.allowed_scope_ids)
        return (
            self._tx._check(write=True)
            .execute(
                f"""UPDATE unresolved_updates SET state='resolved',resolved_at=?
            WHERE state='unresolved' AND scope_id IN ({",".join("?" for _ in scopes)}) AND project_id IS ? AND branch_id IS ?
              AND EXISTS(SELECT 1 FROM json_each(unresolved_updates.candidate_refs_json) WHERE value=?)""",
                (canonical_time(resolved_at), *scopes, ctx.project_id, ctx.branch_id, claim_ref),
            )
            .rowcount
        )
