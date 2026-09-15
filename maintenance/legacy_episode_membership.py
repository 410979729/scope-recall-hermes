"""Project legacy M:N journal references onto Core's unique processing owner.

This is an opt-in migration seam, not a patch/monkeypatch of migrate_v2.
Build ONE plan from the complete resolved episode -> journal source-ref mapping
before its episode loop. After each episode/version insert, call ``apply`` for
EACH original resolved ref instead of the episode_events SELECT/INSERT block.
Keep the subsequent evidence_links INSERT unconditional and keep legacy_fields
(including the original ordered journal_entry_ids) unchanged. Call ``verify``
after the entire loop, before committing, and persist its body-free audit result.

Ownership uses the lexically smallest exact Core episode ID, not arrival order,
recency, source authority, or a claimed historical owner. Sequences retain the
caller's insertion order. This module only writes episode_events and raises
suppressed to 1 on secondary episodes: losing a pending processing event must
not accidentally make an imported resume eligible for automatic recall.

Core Episodes.get/list, inspect and RetrievalStorage.related/hydrate expose the
full evidence_links graph (subject to existing visibility). Episodes.sources,
source_episode, worker batching and apply_resume use processing ownership only.
Secondary references therefore remain inspectable/history-retrievable, but are
NOT extra processing events, ordered source_texts, or resume-update authority.
No source, evidence, resume, schema, or existing owner is rewritten. The caller
owns the transaction and must roll back on ANY failure; no commit, savepoint,
DDL, source-file access, or implicit retry is performed here.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import sqlite3
from types import MappingProxyType
from typing import Any


class MembershipProjectionError(ValueError):
    """Incomplete or incompatible projection; caller must abort its transaction."""


def _ref(value: str) -> str:
    if type(value) is not str or not value or len(value) > 240:
        raise MembershipProjectionError("invalid exact Core reference")
    return value


def _transaction(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise MembershipProjectionError("caller-owned transaction required")


@dataclass(frozen=True)
class LegacyMembershipPlan:
    """Immutable complete-batch projection for legacy journal revision 1 only."""

    memberships: Mapping[str, tuple[str, ...]]
    episode_refs: tuple[str, ...]
    input_reference_count: int

    def audit(self) -> dict[str, Any]:
        """Return a deterministic projection declaration, not a success receipt."""
        edges = sum(len(episodes) for episodes in self.memberships.values())
        secondary = sorted({ep for eps in self.memberships.values() for ep in eps[1:]})
        return {
            "policy": "legacy-primary-owner-lexical-core-episode-ref-v1",
            "verified": False,
            "auto_promoted": False,
            "episode_count": len(self.episode_refs),
            "input_reference_count": self.input_reference_count,
            "distinct_evidence_edges": edges,
            "primary_memberships": len(self.memberships),
            "evidence_only_edges": edges - len(self.memberships),
            "duplicate_input_references": self.input_reference_count - edges,
            "automatic_recall_suppressed_episodes": secondary,
            "processing_projection_changed": bool(secondary),
            "owners": [
                {"source_ref": ref, "source_revision": 1,
                 "primary_episode_ref": eps[0], "evidence_episode_refs": list(eps)}
                for ref, eps in self.memberships.items()
            ],
        }

    def apply(self, conn: sqlite3.Connection, episode_ref: str, source_ref: str) -> dict[str, Any]:
        """Insert only the chosen owner, returning a body-free per-edge receipt.

        Source and episode must already exist in the same scope/project/branch.
        The planned owner may be inserted later in the caller's episode loop.
        Existing noncanonical ownership is an error, never reassigned or ignored.
        Evidence links remain the caller's responsibility, including on no-op.
        """
        _transaction(conn)
        episodes = self.memberships.get(source_ref, ())
        if episode_ref not in episodes:
            raise MembershipProjectionError("edge absent from complete batch plan")
        owner = episodes[0]
        source = conn.execute(
            "SELECT scope_id,project_id,branch_id FROM source_events WHERE event_id=? AND source_revision=1",
            (source_ref,),
        ).fetchone()
        episode = conn.execute(
            "SELECT scope_id,project_id,branch_id FROM episodes WHERE episode_id=?",
            (episode_ref,),
        ).fetchone()
        if source is None or episode is None or tuple(source) != tuple(episode):
            raise MembershipProjectionError("missing or cross-context legacy edge")
        prior = conn.execute(
            "SELECT episode_id,sequence,membership,environment_revision FROM episode_events WHERE source_ref=? AND source_revision=1",
            (source_ref,),
        ).fetchone()
        if prior is not None and (prior[0] != owner or prior[2] != "anchored"):
            raise MembershipProjectionError("existing processing owner conflicts with plan")
        if prior is not None and prior[3] is not None:
            raise MembershipProjectionError("existing membership is not legacy environment")
        if episode_ref != owner:
            # Do not clear read blocks or suppression, or claim pending work was processed.
            conn.execute("UPDATE episodes SET suppressed=1 WHERE episode_id=? AND suppressed=0", (episode_ref,))
            disposition, sequence = "evidence_only", None
        elif prior is not None:
            disposition, sequence = "primary_existing", prior[1]
        else:
            cursor = conn.execute(
                "INSERT INTO episode_events(episode_id,source_ref,source_revision,membership,environment_revision) VALUES (?,?,1,'anchored',NULL)",
                (episode_ref, source_ref),
            )
            disposition, sequence = "primary_inserted", cursor.lastrowid
        return {
            "episode_ref": episode_ref, "source_ref": source_ref, "source_revision": 1,
            "primary_episode_ref": owner, "disposition": disposition, "sequence": sequence,
            "evidence_link_required": True, "auto_promoted": False,
        }

    def verify(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Verify every planned owner AND every M:N evidence edge before commit.

        This is not a source completeness audit: the caller must supply the full
        resolved map and preserve the original legacy_fields, including missing
        references reported separately by migrate_v2. Never certify a subset map
        as the full source migration. Unrelated target rows are not modified.
        """
        _transaction(conn)
        for episode_ref in self.episode_refs:
            if conn.execute(
                "SELECT 1 FROM episodes e JOIN episode_versions v ON v.episode_id=e.episode_id WHERE e.episode_id=? AND v.revision=1",
                (episode_ref,),
            ).fetchone() is None:
                raise MembershipProjectionError("planned legacy episode/version missing")
        for ref, episodes in self.memberships.items():
            prior = conn.execute(
                "SELECT episode_id,membership,environment_revision FROM episode_events WHERE source_ref=? AND source_revision=1",
                (ref,),
            ).fetchone()
            if prior is None or tuple(prior) != (episodes[0], "anchored", None):
                raise MembershipProjectionError("planned primary membership missing or changed")
            for episode_ref in episodes:
                if conn.execute(
                    "SELECT 1 FROM evidence_links WHERE object_kind='episode' AND object_ref=? AND object_revision=1 AND source_ref=? AND source_revision=1 AND relation='derived_from' AND quote=''",
                    (episode_ref, ref),
                ).fetchone() is None:
                    raise MembershipProjectionError("legacy M:N evidence link missing")
                if episode_ref != episodes[0] and conn.execute(
                    "SELECT 1 FROM episodes WHERE episode_id=? AND suppressed=1", (episode_ref,),
                ).fetchone() is None:
                    raise MembershipProjectionError("secondary episode automatic-recall fence missing")
        return dict(self.audit(), verified=True)


def plan_legacy_episode_memberships(
    episode_sources: Mapping[str, Iterable[str]],
) -> LegacyMembershipPlan:
    """Plan from exact resolved Core IDs; never parse, normalize or invent IDs.

    Include all episodes and all their resolved journal refs, NOT refs[:32].
    Repeated refs within one episode are one relation; original ordering and
    duplicates must remain in the untouched legacy_fields journal_entry_ids.
    Identical complete input produces identical owners independent of row order.
    """
    memberships: dict[str, set[str]] = {}
    count = 0
    for episode_ref, refs in episode_sources.items():
        _ref(episode_ref)
        if isinstance(refs, (str, bytes)):
            raise MembershipProjectionError("source refs must be an iterable of references")
        for ref in refs:
            _ref(ref)
            count += 1
            memberships.setdefault(ref, set()).add(episode_ref)
    return LegacyMembershipPlan(
        MappingProxyType({ref: tuple(sorted(eps)) for ref, eps in sorted(memberships.items())}),
        tuple(sorted(episode_sources)), count,
    )
