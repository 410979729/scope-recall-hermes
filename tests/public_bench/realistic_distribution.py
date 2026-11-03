"""Synthetic shared-store distribution with heterogeneous, multi-version claims.

Only aggregate counts determine the fixture. No content, reference, or identifier
is copied from a user store. Population uses captured row templates, then projects
the same lexical indexes ordinary writers maintain.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import replace

import numpy as np
from cases import Turn
from performance import NOW
from scope_recall.core import lexical_index
from scope_recall.core.events import lexical_terms

SCOPES = frozenset({"public", *(f"public-{i}" for i in range(1, 369))})
FACT_BINS = (
    (17698, 1, 1),
    (6153, 2, 2),
    (1628, 3, 3),
    (1415, 4, 5),
    (836, 6, 10),
    (177, 11, 20),
    (39, 21, 50),
    (2, 51, 100),
    (3, 101, 500),
)
SUBJECT_BINS = (
    (6290, 1, 1),
    (2849, 2, 2),
    (992, 3, 3),
    (1012, 4, 5),
    (761, 6, 10),
    (306, 11, 20),
    (126, 21, 50),
    (26, 51, 100),
    (14, 101, 309),
)
TOP_SUBJECTS = (7019, 1703, 1652, 1548, 723, 413, 413, 361, 310, 310)


def _counts(bins, total, rng):
    """Fill histogram bins to an exact total while preserving every bin boundary."""
    values = np.array([low for count, low, _high in bins for _ in range(count)], dtype=np.int64)
    ceilings = np.array([high for count, _low, high in bins for _ in range(count)], dtype=np.int64)
    remaining = total - int(values.sum())
    if remaining < 0 or remaining > int((ceilings - values).sum()):
        raise ValueError("distribution total outside histogram bounds")
    while remaining:
        available = np.flatnonzero(values < ceilings)
        rng.shuffle(available)
        chosen = available[:remaining]
        values[chosen] += 1
        remaining -= len(chosen)
    return values


def _labels(counts, rng):
    """Shuffle exact categorical counts reproducibly."""
    result = [label for label, count in counts for _ in range(count)]
    rng.shuffle(result)
    return result


def _simple_heads(states, kinds, conditioned):
    """Correlate head state with conditions while preserving marginal counts.

    Aggregate status ratios alone do not imply the observed head distribution:
    applying them independently would certify far fewer heads. Swapping labels
    retains exact status/condition totals and certifies the target head count.
    """
    selected = {index for index in range(19, 17698) if kinds[index] != "alias"}
    selected = set(sorted(selected)[:1718])
    active = iter(index for index, state in enumerate(states) if state == "active" and index not in selected)
    plain = iter(index for index, value in enumerate(conditioned) if not value and index not in selected)
    for index in sorted(selected):
        if states[index] != "active":
            donor = next(active)
            states[index], states[donor] = states[donor], states[index]
        if conditioned[index]:
            donor = next(plain)
            conditioned[index], conditioned[donor] = conditioned[donor], conditioned[index]
    proposals = iter(index for index in range(17698, len(states)) if states[index] == "proposed")
    for index in range(19, 17698):
        if index not in selected and kinds[index] != "alias" and states[index] == "active" and not conditioned[index]:
            donor = next(proposals)
            states[index], states[donor] = states[donor], states[index]


def source_scope(index):
    """Put most source traffic in the two busy scopes and spread the remainder."""
    bucket = index % 100
    number = 0 if bucket < 45 else 1 if bucket < 90 else 2 + index // 100 % 367
    return "public" if not number else f"public-{number}"


def declare_prefix_geometry():
    """Declare this process's synthetic model contract without changing product models.

    The fixture owns both the generated vectors and their local query encoder.
    Its prefix is a deliberate benchmark capability, never a claim about an
    unlisted real embedding model or a persistent application configuration.
    """
    from scope_recall.vector import cache

    original = cache._capability_table
    if getattr(original, "synthetic_prefix", False):
        return

    def declared():
        return {
            **original(),
            "synthetic-prefix": {"native_dimensions": 3072, "prefix_dimensions": [768]},
        }

    declared.synthetic_prefix = True
    cache._capability_table = declared


def synthetic_ports(cache):
    """Use local fake ports with an explicit, separately identified prefix space."""
    from ports import FakeEmbedder, Ports

    class PrefixEmbedder(FakeEmbedder):
        """Hash into a usable prefix and repeat its normalized geometry in every block."""

        def embed_text(self, text, remaining_seconds=1.0):
            """Return a unit vector with a nonzero 768-coordinate query prefix."""
            folded = [0.0] * 768
            for index, value in enumerate(super().embed_text(text, remaining_seconds)):
                folded[index % 768] += value
            length = sum(value * value for value in folded) ** 0.5
            if not length:
                folded[0], length = 1.0, 1.0
            prefix = tuple(value / (2 * length) for value in folded)
            return prefix * 4

    declare_prefix_geometry()
    ports = Ports(cache)
    ports.config["embedding"]["model"] = "synthetic-prefix"
    encoder = PrefixEmbedder()
    ports.embedder.embedder = encoder
    ports.evaluator_embedder.embedder = encoder
    return ports


def _insert(conn, table, rows):
    """Insert homogeneous row dictionaries with bounded parameter batches."""
    if rows:
        columns = tuple(rows[0])
        conn.executemany(
            f"INSERT INTO {table}({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            [tuple(row[column] for column in columns) for row in rows],
        )


def seed_sources(lab, count):
    """Create independent synthetic source rows, postings and session identities."""
    if lab.binding.installation_kind == "shared":
        lab.context = replace(lab.context, entry_id="bench")
        with lab.core.storage.write(lab.context) as tx:
            tx.registry.register_entry("bench", "Benchmark", "synthetic", now=NOW)
    turn = Turn("TEST-realistic-template", "TEST-template", "user", "TESTsubject00000 color amber.", NOW)
    ref = lab.capture(turn).event_refs[0].ref
    refs = [ref]
    with sqlite3.connect(lab.core.storage.path) as conn:
        conn.row_factory = sqlite3.Row
        template = dict(conn.execute("SELECT * FROM source_events WHERE event_id=?", (ref,)).fetchone())
        term_ids = dict(conn.execute("SELECT term,term_id FROM lexical_terms"))
        next_term = max(term_ids.values(), default=0) + 1
        for start in range(1, count, 2000):
            rows, postings = [], []
            for index in range(start, min(count, start + 2000)):
                event_ref = "event-" + hashlib.sha256(f"TEST-realistic:{index}".encode()).hexdigest()
                refs.append(event_ref)
                text = f"TESTsubject{index % 12386:05d} color amber. Synthetic record {index}."
                if index % 10 == 0:
                    text += " Synthetic detailed context color amber." * (64 + index % 4 * 48)
                row = dict(
                    template,
                    event_id=event_ref,
                    source_event_key=f"TEST-realistic:{index}",
                    source_group_key=f"TEST-realistic:{index}",
                    session_id=f"TEST-session-{index // 10}",
                    scope_id=source_scope(index),
                    content=text,
                    content_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    event_sha256=hashlib.sha256(f"{index}:{text}".encode()).hexdigest(),
                    source_id=template["source_id"] + index,
                )
                if index % 640 in (2, 3, 4):
                    part = index % 640 - 2
                    extra = json.loads(row["extra_json"])
                    extra["segment"] = dict(index=part, total=3, truncated=False)
                    row.update(
                        source_group_key=f"TEST-segment-{index // 640}",
                        segment_index=part,
                        segment_total=3,
                        extra_json=json.dumps(extra),
                    )
                rows.append(row)
                for term in lexical_terms(text):
                    if term not in term_ids:
                        term_ids[term] = next_term
                        conn.execute("INSERT INTO lexical_terms VALUES (?,?)", (next_term, term))
                        next_term += 1
                    postings.append((term_ids[term], row["source_id"]))
            _insert(conn, "source_events", rows)
            conn.executemany("INSERT OR IGNORE INTO lexical_postings VALUES (?,?)", postings)
            conn.commit()
        conn.execute("UPDATE work_items SET state='done'")
    return refs


def seed_claims(lab, refs):
    """Create the exact aggregate version/status/kind/subject histogram in one store.

    Version subjects are deliberately independent: a historical subject can be
    absent from the head, exercising nomination from every version. The last
    29 claim scopes each hold two facts; the first two hold all other facts.
    """
    rng = np.random.default_rng(42)
    versions_per_claim = _counts(FACT_BINS, 51612, rng)
    subject_counts = np.r_[TOP_SUBJECTS, _counts(SUBJECT_BINS, 51612 - sum(TOP_SUBJECTS), rng)]
    subjects = np.repeat(np.arange(len(subject_counts)), subject_counts)
    rng.shuffle(subjects)
    states = _labels((("proposed", 44740), ("active", 3207), ("retracted", 2786), ("disputed", 879)), rng)
    kinds = _labels(
        (
            ("procedure", 25593),
            ("fact", 10734),
            ("constraint", 9957),
            ("preference", 2226),
            ("decision", 1674),
            ("intention", 799),
            ("alias", 431),
            (None, 198),
        ),
        rng,
    )
    conditioned = np.r_[np.ones(33631, dtype=bool), np.zeros(17981, dtype=bool)]
    rng.shuffle(conditioned)
    _simple_heads(states, kinds, conditioned)
    examples = []
    with sqlite3.connect(lab.core.storage.path) as conn:
        conn.row_factory = sqlite3.Row
        source_rows = {}
        for source in conn.execute("SELECT event_id,scope_id FROM source_events ORDER BY source_id"):
            source_rows.setdefault(source["scope_id"], []).append(source["event_id"])
        position = 0
        for claim_index, count in enumerate(versions_per_claim):
            ref = f"claim-TEST-realistic-{claim_index:05d}"
            scope_number = 0 if claim_index < 18000 else 1 if claim_index < 27893 else 2 + (claim_index - 27893) // 2
            scope = "public" if scope_number == 0 else f"public-{scope_number}"
            pool = source_rows.get(scope, source_rows["public"])
            primary = pool[0 if claim_index % 2 == 0 else claim_index % len(pool)]
            source = dict(
                conn.execute(
                    "SELECT event_id,scope_id,content FROM source_events WHERE event_id=?", (primary,)
                ).fetchone()
            )
            scope = source["scope_id"]
            proofs = [source]
            if claim_index % 20 == 0:
                proofs.extend(
                    dict(
                        conn.execute(
                            "SELECT event_id,scope_id,content FROM source_events WHERE event_id=?",
                            (pool[(claim_index + offset) % len(pool)],),
                        ).fetchone()
                    )
                    for offset in (1, 2)
                )
            proofs = list({proof["event_id"]: proof for proof in proofs}.values())
            versions = []
            for revision in range(1, int(count) + 1):
                subject = f"TESTsubject{int(subjects[position]):05d}"
                kind = kinds[position]
                conditions = [f"TESTcondition{claim_index % 19:02d} enabled"] if conditioned[position] else []
                payload = dict(
                    subject=subject,
                    predicate="color",
                    value_text=f"amber TESTvalue{claim_index % 97:02d}",
                    conditions=conditions,
                    statement_kind="assertion",
                    valid_from=(
                        f"2026-01-{1 + (revision - 1) % 7:02d}T12:00:{(revision - 1) // 7:02d}Z" if count > 1 else None
                    ),
                    valid_to=None,
                    evidence_spans=[
                        dict(source_ref=proof["event_id"], source_revision=1, quote=proof["content"][:1024])
                        for proof in proofs
                    ],
                )
                if kind:
                    payload["kind"] = kind
                stamp = f"2026-01-{1 + (revision - 1) % 7:02d}T12:00:{(revision - 1) // 7:02d}Z"
                ended = "2026-01-08T12:00:00Z" if position < 19 else None
                versions.append(
                    dict(
                        claim_id=ref,
                        revision=revision,
                        payload_json=json.dumps(payload),
                        state=states[position],
                        basis="direct_report",
                        qualification_reason="synthetic",
                        valid_from=payload["valid_from"],
                        valid_to=ended,
                        recorded_from=stamp,
                        recorded_to=None,
                        replaces_revision=revision - 1 if revision > 1 else None,
                        conflict_revisions_json="[]",
                        entity_resolved=0,
                    )
                )
                if len(examples) < 1200:
                    examples.append(
                        dict(
                            ref=ref,
                            revision=revision,
                            subject=subject,
                            state=states[position],
                            condition=bool(conditions),
                            scope=scope,
                        )
                    )
                position += 1
            head = json.loads(versions[-1]["payload_json"])
            conn.execute(
                "INSERT INTO claims(claim_id,scope_id,subject,predicate,kind,slot_key,current_revision) "
                "VALUES (?,?,?,?,?,?,?)",
                (ref, scope, head["subject"], "color", head.get("kind", "fact"), ref, int(count)),
            )
            _insert(conn, "claim_versions", versions)
            conn.executemany(
                "INSERT INTO evidence_links VALUES ('claim',?,?,?,1,'supports',?,NULL)",
                [
                    (ref, version["revision"], proof["event_id"], proof["content"][:1024])
                    for version in versions
                    for proof in proofs
                ],
            )
            if claim_index % 500 == 499:
                conn.commit()
        conn.execute(
            "INSERT INTO candidate_scan_cursors(cursor_name,updated_at) VALUES ('claim_lexical',datetime('now')) "
            "ON CONFLICT(cursor_name) DO UPDATE SET position_ref=NULL,processed_count=0,completed=0"
        )
        while lexical_index.backfill_claim_page(conn):
            conn.commit()
        conn.commit()
    return dict(
        facts=27951,
        versions=position,
        subjects=len(subject_counts),
        examples=examples,
        version_histogram=dict(Counter(map(int, versions_per_claim))),
        subject_histogram=dict(Counter(map(int, subject_counts))),
        state_counts=dict(Counter(states)),
        kind_counts=dict(Counter(str(kind) for kind in kinds)),
        conditioned_versions=int(conditioned.sum()),
        binding_scopes=len(SCOPES),
        claim_scopes=31,
        seed=42,
        dimensions=3072,
    )


def seed_relations(lab):
    """Group synthetic sources into 200-member episodes inside each scope.

    Episode counts, memberships and long-text proportions are pressure
    assumptions; no aggregate episode distribution was supplied by the caller.
    Every evidence edge is same-scope and uses an existing synthetic source.
    """
    with sqlite3.connect(lab.core.storage.path) as conn:
        occupied = {row[0] for row in conn.execute("SELECT source_ref FROM episode_events")}
        grouped = {}
        for ref, scope in conn.execute("SELECT event_id,scope_id FROM source_events ORDER BY source_id"):
            if ref not in occupied:
                grouped.setdefault(scope, []).append(ref)
        sequence = conn.execute("SELECT COALESCE(max(sequence),0) FROM episode_events").fetchone()[0]
        episodes = members = dependencies = 0
        scope_episodes = {}
        for scope, refs in sorted(grouped.items()):
            for start in range(0, len(refs), 200):
                ref = f"episode-TEST-realistic-{scope}-{start // 200:05d}"
                scope_episodes.setdefault(scope, []).append(ref)
                page = refs[start : start + 200]
                conn.execute(
                    "INSERT INTO episodes(episode_id,scope_id,anchor_key,anchor_kind,series_key) "
                    "VALUES(?,?,?,'session',?)",
                    (ref, scope, ref, ref),
                )
                conn.execute(
                    "INSERT INTO episode_versions VALUES(?,1,'completed',NULL,?,?,?,NULL)",
                    (ref, "TEST-synthetic-watermark", sequence + len(page), NOW),
                )
                conn.executemany(
                    "INSERT INTO episode_events VALUES(?,?,?,1,'anchored',NULL)",
                    [(sequence + offset + 1, ref, source) for offset, source in enumerate(page)],
                )
                conn.executemany(
                    "INSERT INTO evidence_links VALUES('episode',?,1,?,1,'supports','',NULL)",
                    [(ref, source) for source in page],
                )
                sequence += len(page)
                episodes += 1
                members += len(page)
        for ref, scope, revision in conn.execute("SELECT claim_id,scope_id,current_revision FROM claims"):
            episode_refs = scope_episodes.get(scope)
            if episode_refs:
                targets = {
                    episode_refs[(int(ref.rsplit("-", 1)[-1]) + offset) % len(episode_refs)] for offset in range(3)
                }
                for episode in sorted(targets):
                    conn.execute(
                        "INSERT INTO object_dependencies VALUES('claim',?,?,'episode',?,1)",
                        (ref, revision, episode),
                    )
                    dependencies += 1
        conn.commit()
    return dict(
        pressure_episodes=episodes,
        pressure_members=members,
        pressure_dependencies=dependencies,
        episode_member_limit=200,
    )


def seed_vectors(lab, refs):
    """Stream synthetic float32 vectors through the production vector writer."""
    from cache import digest
    from scope_recall.runtime.vector_port import physical_partition_scope_id

    rng = np.random.default_rng(42)
    space = digest(lab.ports.config["embedding"])
    partitions = {
        scope: physical_partition_scope_id(
            agent_id=lab.binding.agent_id,
            installation_id=lab.binding.installation_id,
            embedding_space=space,
            logical_scope_id=scope,
            project_id=None,
            branch_id=None,
        )
        for scope in SCOPES
    }
    for start in range(0, len(refs), 4096):
        matrix = rng.standard_normal((min(4096, len(refs) - start), 3072), dtype=np.float32)
        matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
        rows = []
        for offset, vector in enumerate(matrix):
            index = start + offset
            scope = source_scope(index)
            metadata = dict(
                object_kind="event",
                object_ref=refs[index],
                object_revision=1,
                vector_id=f"TEST-vector-{index}",
                embedding_space=space,
                agent_id=lab.binding.agent_id,
                installation_id=lab.binding.installation_id,
                project_id=None,
                branch_id=None,
                logical_scope_id=scope,
            )
            rows.append(
                dict(
                    id=metadata["vector_id"],
                    scope_id=partitions[scope],
                    source=refs[index],
                    target=json.dumps(metadata),
                    content="",
                    summary="",
                    updated_at=NOW,
                    vector=vector,
                )
            )
        lab.companion.upsert_records(rows)
        if start % 40960 == 0:
            print(f"vectors {start}/{len(refs)}", flush=True)
    while True:
        # Fixture preparation drains production cache pages outside a worker
        # deadline. A partially prepared cache would measure tail ingestion
        # alongside recall; worker scheduling is benchmarked separately.
        receipt = lab.companion.maintain_index(max_rows=512)
        if not receipt or receipt["outcome"] != "indexed":
            raise RuntimeError("synthetic vector cache was not published")
        if receipt["rows"] < 512:
            break
    if lab.companion.count_rows() != len(refs):
        raise RuntimeError("synthetic vector count mismatch")
