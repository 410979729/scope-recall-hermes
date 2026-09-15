"""Versioned references; display order is evidence, file sorting is not."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

from ..contracts import ContractError
from .claim_storage import parse_source_ref
from .delete_storage import canonical
from .episodes import source_origin, UNSETTLED
from .source_qualification import AUTHORITY_QUESTION
from .visibility import allowed


@dataclass(frozen=True)
class Reference:
    ref: str
    revision: int
    scope_id: str
    project_id: str | None
    branch_id: str | None
    payload: dict
    reason: str
    suppressed: bool


def ordinal(mention, count):
    match = re.search(r"第([一二三四五六七八九十]|\d{1,2})(?:张|个|版)", mention)
    if match:
        value = match[1]
        index = (
            int(value) if value.isdigit() else "一二三四五六七八九十".index(value) + 1
        )
        return index - 1 if 1 <= index <= count else None
    if "中间" in mention and count % 2 == 1:
        return count // 2
    if "最后" in mention and count:
        return count - 1
    return None


class References:
    def __init__(self, tx):
        self.tx = tx

    def get(self, ref, revision=None):
        conn, ctx = self.tx._check(), self.tx.context
        if not allowed(self.tx, "reference", ref):
            return None
        scopes = sorted(ctx.allowed_scope_ids)
        row = conn.execute(
            f"""SELECT r.*,v.* FROM reference_bindings r JOIN reference_versions v ON v.reference_id=r.reference_id
            AND v.revision=COALESCE(?,r.current_revision) WHERE r.reference_id=? AND r.read_blocked=0
            AND r.scope_id IN ({",".join("?" for _ in scopes)}) AND (r.project_id IS NULL OR r.project_id=?) AND (r.branch_id IS NULL OR r.branch_id=?)""",
            (revision, ref, *scopes, ctx.project_id, ctx.branch_id),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        if any(
            self.tx.source(*parse_source_ref(s)) is None
            for s in payload["evidence_refs"]
        ):
            return None
        if revision is None:
            for source in payload["evidence_refs"]:
                try:
                    self.tx.claims.require_live_source(*parse_source_ref(source))
                except ContractError:
                    return None
        return Reference(
            ref,
            row["revision"],
            row["scope_id"],
            row["project_id"],
            row["branch_id"],
            payload,
            row["qualification_reason"],
            bool(row["suppressed"]),
        )

    def _stored_head(self, ref):
        """Load the stored head without treating stale evidence as current.

        ``get`` intentionally hides a head whose evidence is no longer live.
        An explicit clarification must still be able to create the next
        version of that binding, while the stale version remains historical.
        """
        row = (
            self.tx._check()
            .execute(
                """SELECT r.current_revision,v.payload_json
            FROM reference_bindings r JOIN reference_versions v
            ON v.reference_id=r.reference_id AND v.revision=r.current_revision
            WHERE r.reference_id=? AND r.read_blocked=0""",
                (ref,),
            )
            .fetchone()
        )
        return (
            (int(row["current_revision"]), json.loads(row["payload_json"]))
            if row
            else None
        )

    def _visible_artifact_identity_count(self, label, revision):
        """Count every visible artifact identity in the trusted context."""
        ctx = self.tx.context
        scopes = sorted(ctx.allowed_scope_ids)
        return (
            self.tx._check()
            .execute(
                f"""SELECT count(DISTINCT a.artifact_id) FROM artifacts a JOIN artifact_versions v
            ON v.artifact_id=a.artifact_id
            WHERE v.label=? AND v.revision=? AND a.scope_id IN ({",".join("?" for _ in scopes)})
            AND a.read_blocked=0 AND (a.project_id IS NULL OR a.project_id=?)
            AND (a.branch_id IS NULL OR a.branch_id=?)
            AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='artifact'
                AND b.object_ref=a.artifact_id AND b.read_blocked=1)""",
                (label, revision, *scopes, ctx.project_id, ctx.branch_id),
            )
            .fetchone()[0]
        )

    @staticmethod
    def _explicit_candidate_mention(raw, label, revision, candidate, mention):
        """Return true only for a local, affirmative label/version mention."""
        if UNSETTLED.search(raw) or AUTHORITY_QUESTION.search(raw):
            return False
        pieces = [p for p in re.split(r"[，,;；。!?！？\n]|\.(?:\s|$)", raw) if p]
        version = rf"(?:v\s*{revision}(?![.\d])\b|第\s*{revision}\s*版)"
        marker = re.compile(re.escape(label) + rf"\s*(?:的|版本)?\s*{version}", re.I)
        if re.search(version + r"$", label, re.I):
            marker = re.compile(re.escape(label))

        def linked(piece, start, end):
            for occurrence in re.finditer(re.escape(mention), piece):
                if (
                    start <= occurrence.start() < end
                    or occurrence.start() <= start < occurrence.end()
                ):
                    return True
                if occurrence.end() <= start and re.fullmatch(
                    r"[\s“”‘’\"\']*(?:说的是|指的是|就是|是|为)[\s“”‘’\"\']*",
                    piece[occurrence.end() : start],
                ):
                    return True
            return False

        for piece in pieces:
            if UNSETTLED.search(piece):
                continue
            if candidate in piece:
                prefix = piece[: piece.find(candidate)]
                if re.search(r"(?:不是|并非|非|not|never)\s*$", prefix, re.I):
                    continue
                if linked(piece, len(prefix), len(prefix) + len(candidate)):
                    return True
            if label not in piece:
                continue
            if marker.search(piece):
                if re.search(
                    r"(?:不是|并非|非|不是所说的|not|never)\s*(?:[^，,;；。.!?！？]*?)"
                    + re.escape(label),
                    piece,
                    re.I,
                ):
                    continue
                if any(
                    linked(piece, match.start(), match.end())
                    for match in marker.finditer(piece)
                ):
                    return True
        return False

    def apply(self, proposal, scope_id, now):
        conn, ctx = self.tx._check(write=True), self.tx.context
        sources = [
            self.tx.source(*parse_source_ref(r)) for r in proposal["evidence_refs"]
        ]
        if any(
            s is None
            or (s.scope_id, s.project_id, s.branch_id)
            != (scope_id, ctx.project_id, ctx.branch_id)
            for s in sources
        ):
            raise ContractError("SOURCE_MISSING")
        for source in sources:
            self.tx.claims.require_live_source(source.ref, source.revision)
        mentioned = [s for s in sources if proposal["mention"] in s.event["content"]]
        if not mentioned:
            raise ContractError("DERIVATION_INVALID", "reference_mention")
        episodes = {
            self.tx.episodes.source_episode(s.ref, s.revision).ref for s in mentioned
        }
        if len(episodes) != 1:
            raise ContractError("DERIVATION_INVALID", "reference_episode")
        episode = next(iter(episodes))
        candidates = []
        for candidate in proposal["candidate_refs"]:
            ref, revision = parse_source_ref(candidate)
            item = self.tx.artifacts.get(ref, revision)
            if item is None:
                raise ContractError("SOURCE_MISSING")
            candidates.append(item)
        resolved = set()
        reason = "model_candidates_only"
        for source in mentioned:
            if source_origin(source) != "human_direct" or source.capture_gaps:
                continue
            snapshot = source.event.get("display_snapshot", {})
            raw = source.event["content"]
            ordinal_safe = not UNSETTLED.search(raw) and not AUTHORITY_QUESTION.search(
                raw
            )
            if ordinal_safe and re.search(
                r"(?:不是|并非|非|not|never)\s*" + re.escape(proposal["mention"]),
                raw,
                re.I,
            ):
                ordinal_safe = False
            if snapshot.get("order") == "observed" and ordinal_safe:
                index = ordinal(proposal["mention"], len(snapshot["items"]))
                if index is not None:
                    selected = snapshot["items"][index]
                    version = f"{selected['artifact_ref']}@{selected['revision']}"
                    if version in proposal["candidate_refs"]:
                        resolved.add(version)
                        reason = "observed_display_order"
            for candidate, item in zip(proposal["candidate_refs"], candidates):
                # The proposal list is not the universe of visible artifacts:
                # omitted same-label versions must prevent false uniqueness.
                unique_visible = (
                    candidate in raw
                    or self._visible_artifact_identity_count(item.label, item.revision)
                    == 1
                )
                exact = unique_visible and self._explicit_candidate_mention(
                    raw, item.label, item.revision, candidate, proposal["mention"]
                )
                if exact:
                    resolved.add(candidate)
                    reason = "explicit_version_mention"
        choice = next(iter(resolved)) if len(resolved) == 1 else None
        payload = dict(
            proposal,
            resolved_ref=choice,
            resolution="resolved"
            if choice
            else "ambiguous"
            if len(candidates) > 1
            else "unresolved",
        )
        primary = mentioned[-1]
        ref = (
            "reference-"
            + hashlib.sha256(
                canonical(
                    [
                        ctx.binding.installation_id,
                        scope_id,
                        ctx.project_id,
                        ctx.branch_id,
                        primary.ref,
                        proposal["mention"],
                    ]
                ).encode()
            ).hexdigest()
        )
        # An explicit later clarification updates the one matching earlier
        # mention within this episode; ordinary repeated words create occurrences.
        if any(
            re.search(r"刚才|说的是|指的是|I meant|clarif", s.event["content"], re.I)
            for s in mentioned
        ):
            matches = conn.execute(
                """SELECT r.reference_id FROM reference_bindings r JOIN reference_versions v
                ON v.reference_id=r.reference_id AND v.revision=r.current_revision WHERE r.episode_id=? AND r.read_blocked=0
                AND json_extract(v.payload_json,'$.mention')=? ORDER BY v.recorded_at DESC LIMIT 2""",
                (episode, proposal["mention"]),
            ).fetchall()
            if len(matches) == 1:
                ref = matches[0][0]
        if not allowed(self.tx, "reference", ref):
            raise ContractError("SOURCE_MISSING")
        current = self.get(ref)
        stored = self._stored_head(ref)
        if current and current.payload == payload:
            return current
        revision = (stored[0] + 1) if stored else 1
        if stored:
            conn.execute(
                "UPDATE reference_bindings SET current_revision=? WHERE reference_id=?",
                (revision, ref),
            )
            # No stale dependent summary remains eligible after a binding change.
            for row in conn.execute(
                "SELECT DISTINCT object_ref FROM object_dependencies WHERE dependency_kind='reference' AND dependency_ref=? AND object_kind='episode'",
                (ref,),
            ):
                conn.execute(
                    "UPDATE episode_versions SET processed_sequence=0 WHERE episode_id=?",
                    (row[0],),
                )
        else:
            conn.execute(
                "INSERT INTO reference_bindings(reference_id,scope_id,project_id,branch_id,episode_id,current_revision,suppressed) VALUES (?,?,?,?,?,?,?)",
                (
                    ref,
                    scope_id,
                    ctx.project_id,
                    ctx.branch_id,
                    episode,
                    revision,
                    int(any(s.suppressed for s in sources)),
                ),
            )
        conn.execute(
            "INSERT INTO reference_versions VALUES (?,?,?,?,?)",
            (
                ref,
                revision,
                canonical(payload),
                reason if choice else "ambiguous_or_unconfirmed",
                now,
            ),
        )
        for source in sources:
            conn.execute(
                "INSERT INTO evidence_links VALUES ('reference',?,?,?,?,'derived_from','',NULL)",
                (ref, revision, source.ref, source.revision),
            )
        for item in candidates:
            conn.execute(
                "INSERT INTO object_dependencies VALUES ('reference',?,?,'artifact',?,?)",
                (ref, revision, item.ref, item.revision),
            )
        conn.execute(
            "UPDATE instance_meta SET memory_epoch=memory_epoch+1 WHERE singleton=1"
        )
        return self.get(ref)
