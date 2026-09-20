"""Storage growth on a busy instance: episode lineage is written once per source.

The numbers behind these tests come from an instance that captured a thousand
sources a day: the copy-forward of episode evidence links produced a hundred
thousand rows for eight episodes in one day, quadratic in the segment length.
"""
from dataclasses import replace
import sqlite3

from scope_recall.core.episodes import source_watermark
from scope_recall.core.retrieval import CandidateRef
from scope_recall.core.schema import SCHEMA_VERSION
from test_v11_claims import app, capture
from test_v11_episodes import apply, artifact, ref, resume
from v11_support import downgrade_store


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
        postings = conn.execute("SELECT count(*) FROM lexical_postings").fetchone()[0]
        conn.commit()
    downgrade_store(core.storage.path, 1108)
    # A known older schema is brought forward by the first ordinary open.
    assert core.status(ctx).schema_version == SCHEMA_VERSION == 1109
    assert _lineage(core, episode.ref) == [(index + 1, s.ref) for index, s in enumerate(sources)]
    assert _dependencies(core, episode.ref) == [(2,)]
    with sqlite3.connect(core.storage.path) as conn:
        assert conn.execute("SELECT count(*) FROM evidence_links WHERE object_kind='claim'").fetchone()[0] == 2
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1109
        assert {"source_content", "source_ids"} <= {row[1] for row in conn.execute("PRAGMA index_list(source_events)")}
        # the lexical index was rebuilt from the text projection the old store carried
        assert conn.execute("SELECT count(*) FROM lexical_postings").fetchone()[0] == postings
        assert conn.execute("SELECT count(DISTINCT source_id) FROM source_events").fetchone()[0] == 4
    assert set(core.episodes(ctx)[0].evidence_refs) == {ref(s) for s in sources}
    assert core.initialize().schema_version == 1109


def test_an_episode_read_judges_its_members_in_one_query(app):
    """A 200-member episode cost 200 source loads, JSON and all, on every read
    and every listing.  The members are judged in one query."""
    core, ctx = app
    ctx = replace(ctx, task_anchor="TEST-read-cost")
    for index in range(40):
        capture(core, ctx, f"TEST read cost {index}")
    statements: list[str] = []
    original_open = core.storage._open

    def traced(mode, remaining_seconds=None, *, restoring=False):
        connection = original_open(mode, remaining_seconds, restoring=restoring)
        connection.set_trace_callback(statements.append)
        return connection

    core.storage._open = traced
    try:
        episode, = core.episodes(ctx)
    finally:
        core.storage._open = original_open
    assert len(episode.evidence_refs) == 40 and episode.gaps == ("unprocessed_events",)
    assert sum("FROM source_events" in sql for sql in statements) <= 2


def test_a_resume_is_judged_by_what_it_cites(app):
    """An uncited member that changed does not make the resume stale; a cited
    one that changed does."""
    core, ctx = app
    ctx = replace(ctx, task_anchor="TEST-judged")
    cited = capture(core, ctx, "TEST cited turn", key="TEST-judged/cited")
    capture(core, ctx, "TEST uncited tool line", origin="tool_observation", key="TEST-judged/uncited")
    apply(core, ctx, resumes=[resume(cited)])
    episode, = core.episodes(ctx)
    assert "source_version_changed" not in episode.gaps
    capture(core, ctx, "TEST uncited tool line, again", origin="tool_observation", key="TEST-judged/uncited", revision=2)
    episode, = core.episodes(ctx)
    assert "source_version_changed" not in episode.gaps
    capture(core, ctx, "TEST cited turn, corrected", key="TEST-judged/cited", revision=2)
    episode, = core.episodes(ctx)
    assert "source_version_changed" in episode.gaps and "resume_requires_rebuild" in episode.gaps


def test_a_long_episode_with_a_resume_is_delivered_on_its_cited_evidence(app):
    """The packet refuses an object with more than 32 evidence refs, and an
    episode's evidence used to be every member: a long episode's resume never
    reached a packet.  It is delivered on what the resume cites."""
    from scope_recall.core.recall_packet import fits_packet_schema
    from scope_recall.core.retrieval import SearchContext
    from scope_recall.core.retrieval_storage import RetrievalStorage
    from v11_support import recall_request

    core, ctx = app
    ctx = replace(ctx, task_anchor="TEST-packet")
    sources = [capture(core, ctx, f"TEST packet member {index}") for index in range(40)]
    apply(core, ctx, resumes=[resume(sources[-1])])
    episode, = core.episodes(ctx)
    assert len(episode.evidence_refs) == 40
    search = SearchContext.from_request(recall_request(mode="history"), ctx, now=core.clock.utc_now(), deadline=200.0)
    with core.storage.read(ctx) as tx:
        obj = RetrievalStorage(clock=core.clock).hydrate(tx, CandidateRef("episode", episode.ref, episode.revision, "exact_ref"), search)
    assert obj is not None and obj.evidence_refs == (ref(sources[-1]),)
    assert fits_packet_schema(obj)
