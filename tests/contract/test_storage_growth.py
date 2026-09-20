"""Storage growth on a busy instance: episode lineage is written once per source.

The numbers behind these tests come from an instance that captured a thousand
sources a day: the copy-forward of episode evidence links produced a hundred
thousand rows for eight episodes in one day, quadratic in the segment length.
"""
from dataclasses import replace
import sqlite3

import pytest

from scope_recall.contracts import ContractError
from scope_recall.core.episodes import source_watermark
from scope_recall.core.retrieval import CandidateRef
from scope_recall.core.schema import SCHEMA_VERSION
from test_v11_claims import app, capture
from test_v11_episodes import apply, artifact, ref, resume


def _lineage(core, episode_ref):
    with sqlite3.connect(core.storage.path) as conn:
        return conn.execute(
            "SELECT object_revision,source_ref FROM evidence_links WHERE object_kind='episode' AND object_ref=? ORDER BY object_revision,source_ref",
            (episode_ref,),
        ).fetchall()


def _dependencies(core, episode_ref):
    with sqlite3.connect(core.storage.path) as conn:
        return conn.execute(
            "SELECT object_revision FROM object_dependencies WHERE object_kind='episode' AND object_ref=? ORDER BY object_revision",
            (episode_ref,),
        ).fetchall()


def test_an_episode_lineage_row_is_written_once_at_the_revision_its_source_entered(app):
    """Every attach copied the previous revision's links onto the new one, so a
    200-event segment held 20,100 rows for 200 sources.  Each source now has one
    row, a revision's evidence is every row at or below it, and a resume that
    cites members adds nothing."""
    core, ctx = app
    ctx = replace(ctx, task_anchor="TEST-lineage")
    sources = [capture(core, ctx, f"TEST lineage {index}") for index in range(40)]
    episode, = core.episodes(ctx)
    rows = _lineage(core, episode.ref)
    assert [row[0] for row in rows] == list(range(1, 41))
    assert [row[1] for row in rows] == [s.ref for s in sources]
    assert episode.revision == 40 and set(episode.evidence_refs) == {ref(s) for s in sources}
    with core.storage.read(ctx) as tx:
        early = tx.episodes.get(episode.ref, 3)
    assert early.revision == 3 and set(early.evidence_refs) == {ref(s) for s in sources[:3]}

    apply(core, ctx, resumes=[resume(sources[0])])
    latest, = core.episodes(ctx)
    assert latest.revision == 41 and latest.resume is not None
    assert set(latest.evidence_refs) == {ref(s) for s in sources}
    assert _lineage(core, episode.ref) == rows


def test_relation_expansion_names_an_episode_at_its_head(app):
    """The row for a source sits at the revision it entered; the episode it
    belongs to is delivered at its head, which is the only revision the live
    modes hydrate."""
    core, ctx = app
    ctx = replace(ctx, task_anchor="TEST-related")
    sources = [capture(core, ctx, f"TEST related {index}") for index in range(6)]
    episode, = core.episodes(ctx)
    seed = CandidateRef("event", sources[1].ref, sources[1].revision, "exact_ref")
    with core.storage.read(ctx) as tx:
        related = core.recall_pipeline.storage_reader.related(tx, seed, limit=24)
    episodes = [(item.ref, item.revision) for item in related if item.kind == "episode"]
    assert episodes == [(episode.ref, episode.revision)] and episode.revision == 6


def test_a_resume_records_a_cited_artifact_once(app, tmp_path):
    core, ctx = app
    item, source, _ = artifact(core, ctx, tmp_path)
    work = capture(core, ctx, "TEST 下一步调整这张图的配色。", artifact_refs=[item.ref])
    refs = [ref(source), ref(work)]
    proposal = resume(work, artifact_refs=[f"{item.ref}@1"], evidence_refs=refs, source_watermark=source_watermark(refs))
    apply(core, ctx, resumes=[proposal])
    first = core.episodes(ctx)[0]
    assert _dependencies(core, first.ref) == [(first.revision,)]
    apply(core, ctx, resumes=[dict(proposal, open_items=[dict(text="调整这张图的配色", evidence_refs=[ref(work)])])])
    second = core.episodes(ctx)[0]
    assert second.revision == first.revision + 1 and second.resume != first.resume
    assert _dependencies(core, first.ref) == [(first.revision,)]
    assert [row[1] for row in _lineage(core, first.ref)] == [source.ref, work.ref]


def test_upgrade_1108_keeps_the_earliest_copy_of_every_episode_lineage_row(app):
    """A 1108 database holds each link once per revision it was copied onto.
    The copy at the revision the source entered is the one the readers expect
    now; claim links are not episode lineage and are left alone."""
    core, ctx = app
    ctx = replace(ctx, task_anchor="TEST-upgrade")
    sources = [capture(core, ctx, f"TEST upgrade {index}") for index in range(4)]
    episode, = core.episodes(ctx)
    with sqlite3.connect(core.storage.path) as conn:
        for revision in range(2, 5):
            conn.execute(
                """INSERT OR IGNORE INTO evidence_links(object_kind,object_ref,object_revision,source_ref,source_revision,relation,quote)
                   SELECT 'episode',object_ref,?,source_ref,source_revision,relation,quote FROM evidence_links
                   WHERE object_kind='episode' AND object_ref=? AND object_revision<?""",
                (revision, episode.ref, revision),
            )
        for revision in (2, 4):
            conn.execute("INSERT INTO object_dependencies VALUES ('episode',?,?,'artifact','TEST-artifact',1)", (episode.ref, revision))
        for revision in (1, 2):
            conn.execute("INSERT INTO evidence_links VALUES ('claim','TEST-claim',?,?,1,'supports','TEST',NULL)", (revision, sources[0].ref))
        assert conn.execute("SELECT count(*) FROM evidence_links WHERE object_kind='episode'").fetchone()[0] == 10
        conn.execute("UPDATE instance_meta SET schema_version=1108 WHERE singleton=1")
        conn.execute("PRAGMA user_version=1108")
        conn.commit()
    with pytest.raises(ContractError, match="SCHEMA_UNSUPPORTED"):
        core.status(ctx)
    assert core.initialize().schema_version == SCHEMA_VERSION == 1109
    assert _lineage(core, episode.ref) == [(index + 1, s.ref) for index, s in enumerate(sources)]
    assert _dependencies(core, episode.ref) == [(2,)]
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT count(*) FROM evidence_links WHERE object_kind='claim'").fetchone()[0] == 2
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1109
    assert set(core.episodes(ctx)[0].evidence_refs) == {ref(s) for s in sources}
    assert core.initialize().schema_version == 1109
