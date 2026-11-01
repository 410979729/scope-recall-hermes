"""Current fact postings and indexed nomination over synthetic scoped truth."""

from dataclasses import replace

import pytest
from scope_recall.core.retrieval import SearchContext
from scope_recall.core.retrieval_storage import RetrievalStorage

from tests.contract.test_claims import accept, app, capture, draft, initial, revise_request
from tests.contract.test_deletion import authorize, request
from tests.v11_support import recall_request


def search(core, ctx, query, mode="current"):
    """Build a real-clock nomination request without any vector or model port."""
    return SearchContext.from_request(
        recall_request(query=query, mode=mode, **({"as_of": core.clock.utc_now()} if mode == "as_of" else {})),
        ctx,
        now=core.clock.utc_now(),
        deadline=core.clock.monotonic() + 10,
    )


def held(tx, ref):
    """Read the shared dictionary through the fact's current postings."""
    return {
        row[0]
        for row in tx._check().execute(
            "SELECT t.term FROM claim_lexical_postings p JOIN lexical_terms t USING(term_id) WHERE p.claim_id=?",
            (ref,),
        )
    }


def test_nomination_never_calls_substring_on_stored_claims(app):
    core, ctx = app
    item, _ = initial(core, ctx)
    with core.storage.read(ctx) as tx:

        def reject(*_args):
            raise AssertionError("stored claim substring scan")

        tx._check().create_function("instr", 2, reject)
        assert [row.ref for row in RetrievalStorage().claims(tx, search(core, ctx, "TEST-project 配色"), limit=6)] == [
            item.ref
        ]


def test_postings_follow_only_committed_current_head_and_delete(app):
    core, ctx = app
    item, _ = initial(core, ctx, value="TEST-cobalt")
    with core.storage.read(ctx) as tx:
        assert "test-cobalt" in held(tx, item.ref)
    source = capture(core, ctx, "Please correct TEST-project 配色: TEST-silver.", when="2026-09-02T12:00:00Z")
    core.revise(ctx, revise_request(item, source, "TEST-silver"), remaining_seconds=10)
    with core.storage.read(ctx) as tx:
        terms = held(tx, item.ref)
        assert "test-silver" in terms and "test-cobalt" not in terms
    current = core.current_claim(ctx, item.ref)
    authorize(core, ctx, current)
    core.forget(ctx, request(current), remaining_seconds=10)
    with core.storage.read(ctx) as tx:
        assert not held(tx, item.ref)


def test_shared_dictionary_survives_event_forget_while_claim_holds_term(app):
    from scope_recall.core import lexical_index

    core, ctx = app
    item, source = initial(core, ctx)
    with core.storage.write(ctx) as tx:
        lexical_index.forget(tx._check(write=True), source.ref)
        assert "配色" in held(tx, item.ref)


@pytest.mark.parametrize("mode", ["current", "auto", "history"])
def test_nomination_scopes_and_alias_exclusion(app, mode):
    core, ctx = app
    item, _ = initial(core, ctx)
    source = capture(core, ctx, "TEST-other 配色 蓝色。")
    accept(core, ctx, draft(source, subject="TEST-other"))
    with core.storage.read(ctx) as tx:
        rows = RetrievalStorage().claims(tx, search(core, ctx, "TEST-project 配色", mode), limit=6)
        assert [row.ref for row in rows] == [item.ref]
    for field in ("project_id", "branch_id"):
        other = replace(ctx, **{field: "TEST-outside"})
        with core.storage.read(other) as tx:
            assert not RetrievalStorage().claims(tx, search(core, other, "TEST-project 配色", mode), limit=6)


@pytest.mark.parametrize("mode", ["current", "auto", "history", "as_of", "method"])
def test_indexed_and_substring_nomination_deliver_equivalent_candidates(app, monkeypatch, mode):
    """Compare full nomination, including version selection and scores, to the former prefilter."""
    from tests.contract.test_aliases import _alias

    core, ctx = app
    for subject, predicate, value in (
        ("TEST-project", "配色", "蓝色"),
        ("TEST-project", "theme", "amber"),
        ("TEST-project-extra", "theme", "amber"),
        ("TEST-青山项目", "部署位置", "上海"),
        ("TEST-project", "category", "cat"),
        ("TEST-silent", "theme", "amber"),
        ("TEST-private", "theme", "amber"),
    ):
        source = capture(core, ctx, f"{subject} {predicate} {value}.")
        item = accept(core, ctx, draft(source, value, subject=subject, predicate=predicate)).items[0]
        if subject in {"TEST-silent", "TEST-private"}:
            action = "suppress" if subject == "TEST-silent" else "delete"
            authorize(core, ctx, item, mode=action)
            core.forget(ctx, request(item, mode=action), remaining_seconds=10)
        if predicate == "配色":
            target = item
            replacement = capture(core, ctx, "Please correct TEST-project 配色: 银色.", when="2026-09-02T12:00:00Z")
            core.revise(ctx, revise_request(item, replacement, "银色"), remaining_seconds=10)
    source = capture(core, ctx, "TEST-project 项目别名 TEST-sunrise.")
    alias = _alias(source, target, "TEST-sunrise")
    alias["subject"] = "TEST-project"
    alias_item = accept(core, ctx, alias).items[0]
    with core.storage.read(ctx) as tx:
        assert not held(tx, alias_item.ref)
    queries = [
        "TEST-project 配色",
        "TEST-project theme amber",
        "TEST-青山项目 部署位置",
        "TEST-private theme",
        "TEST-silent theme",
        "cat",
        "TEST-project-extra theme",
    ]
    for trusted in (ctx, replace(ctx, project_id="TEST-outside"), replace(ctx, branch_id="TEST-outside")):
        for query in queries:
            context = search(core, trusted, query, mode)
            if mode == "as_of":
                context = replace(context, as_of="2026-09-01T13:00:00Z")
            with core.storage.read(trusted) as tx:
                indexed = RetrievalStorage().claims(tx, context, limit=48)
                conn = tx._check()
                # This reference stays in the test: production has only indexed nomination.
                terms = tuple(
                    sorted(
                        __import__(
                            "scope_recall.core.recall_policy", fromlist=["meaningful_query_terms"]
                        ).meaningful_query_terms(query),
                        key=lambda t: (-len(t), t),
                    )[:32]
                )
                hits = " + ".join("(instr(statement,?)>0)" for _ in terms)
                auto = "AND c.suppressed=0" if mode == "auto" else ""
                block = "OR b.suppressed=1" if mode == "auto" else ""
                sql = f"""WITH heads AS MATERIALIZED (
                    SELECT c.claim_id,lower(c.subject||' '||c.predicate||' '||
                    coalesce(json_extract(v.payload_json,'$.value_text'),'')) AS statement
                    FROM claims c JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revision=c.current_revision
                    WHERE c.scope_id IN ({",".join("?" for _ in trusted.allowed_scope_ids)})
                    AND c.read_blocked=0 {auto} AND c.kind!='alias'
                    AND (c.project_id IS NULL OR c.project_id=?) AND (c.branch_id IS NULL OR c.branch_id=?)
                    AND NOT EXISTS(SELECT 1 FROM object_blocks b WHERE b.object_kind='claim'
                    AND b.object_ref=c.claim_id AND (b.read_blocked=1 {block})))
                    SELECT claim_id FROM (SELECT claim_id,{hits} AS hits FROM heads)
                    WHERE hits>0 ORDER BY hits DESC,claim_id LIMIT 256"""
                parameters = (*sorted(trusted.allowed_scope_ids), trusted.project_id, trusted.branch_id, *terms)

                with monkeypatch.context() as patch:
                    patch.setattr(
                        "scope_recall.core.retrieval_storage._claim_rows",
                        lambda *args, conn=conn, sql=sql, parameters=parameters: conn.execute(
                            sql, parameters
                        ).fetchall(),
                    )
                    previous = RetrievalStorage().claims(tx, context, limit=48)
                assert indexed == previous, (mode, query, trusted, indexed, previous)


@pytest.mark.parametrize("extra", [("amber", "cobalt", "green"), ("amber", "cobalt", "green", "blue")])
@pytest.mark.parametrize("mode", ["auto", "current"])
def test_bounded_intersections_preserve_hit_order_after_policy(app, monkeypatch, extra, mode):
    """Compare the capped posting prefixes to an exhaustive visible-head oracle."""
    import json

    from scope_recall.core import lexical_index, retrieval_storage
    from scope_recall.core.events import lexical_terms

    core, ctx = app
    item, _ = initial(core, ctx, value="amber")
    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        head = dict(conn.execute("SELECT * FROM claims WHERE claim_id=?", (item.ref,)).fetchone())
        version = dict(conn.execute("SELECT * FROM claim_versions WHERE claim_id=?", (item.ref,)).fetchone())
        for mask in range(1, 1 << len(extra)):
            ref = f"claim-TEST-bounded-{mask:02d}"
            value = " ".join(word for index, word in enumerate(extra) if mask & (1 << index))
            clone = dict(
                head,
                claim_id=ref,
                subject="TEST-project",
                predicate="theme",
                slot_key=ref,
                read_blocked=int(mask % 5 == 0),
                suppressed=int(mask % 7 == 0),
            )
            payload = dict(
                json.loads(version["payload_json"]), subject="TEST-project", predicate="theme", value_text=value
            )
            body = dict(version, claim_id=ref, payload_json=json.dumps(payload))
            conn.execute(
                f"INSERT INTO claims({','.join(clone)}) VALUES ({','.join('?' for _ in clone)})", tuple(clone.values())
            )
            conn.execute(
                f"INSERT INTO claim_versions({','.join(body)}) VALUES ({','.join('?' for _ in body)})",
                tuple(body.values()),
            )
            lexical_index.index_claim(conn, ref)
    monkeypatch.setattr(retrieval_storage, "_CLAIM_SCAN_LIMIT", 3)
    query = "TEST-project " + " ".join(extra)
    context = search(core, ctx, query, mode)
    terms = retrieval_storage.meaningful_query_terms(query)
    with core.storage.read(ctx) as tx:
        exhaustive = []
        for row in tx._check().execute(
            "SELECT * FROM claims c JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revision=c.current_revision"
        ):
            if row["read_blocked"] or (mode == "auto" and row["suppressed"]):
                continue
            statement = " ".join([row["subject"], row["predicate"], json.loads(row["payload_json"])["value_text"]])
            count = len(set(terms).intersection(lexical_terms(statement)))
            if count:
                exhaustive.append((-count, row["claim_id"]))
        assert [row["claim_id"] for row in retrieval_storage._claim_rows(tx, context, terms)] == [
            ref for _, ref in sorted(exhaustive)[:3]
        ]


def test_claim_dictionary_lookup_uses_index_with_stale_statistics(app):
    """A migrated dictionary can grow long after ANALYZE recorded a handful of words."""
    from scope_recall.core import lexical_index

    core, ctx = app
    item, _ = initial(core, ctx)
    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        conn.execute("ANALYZE lexical_terms")
        conn.executemany("INSERT INTO lexical_terms(term) VALUES (?)", [(f"TEST-unrelated-{i}",) for i in range(2000)])
        statements = []
        conn.set_trace_callback(statements.append)
        lexical_index.index_claim(conn, item.ref)
        conn.set_trace_callback(None)
        insert = next(sql for sql in statements if sql.startswith("INSERT INTO claim_lexical_postings"))
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + insert)]
        assert any("SEARCH t" in step and "term=?" in step for step in plan), plan


def test_posting_intersection_precedes_head_policy_lookup(app):
    """Only intersected posting refs should pay for reading a potentially large claim head."""
    from scope_recall.core import retrieval_storage

    core, ctx = app
    initial(core, ctx)
    context = search(core, ctx, "TEST-project 配色")
    with core.storage.read(ctx) as tx:
        conn = tx._check()
        statements = []
        conn.set_trace_callback(statements.append)
        retrieval_storage._claim_rows(tx, context, retrieval_storage.meaningful_query_terms(context.query))
        conn.set_trace_callback(None)
        query = next(sql for sql in statements if sql.startswith("SELECT p.claim_id"))
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + query)]
        probe = next(index for index, step in enumerate(plan) if "SEARCH k" in step)
        policy = next(index for index, step in enumerate(plan) if "SEARCH c" in step)
        assert probe < policy, plan


def test_reindex_preserves_a_deletion_held_only_in_the_object_ledger(app):
    """Restore can replay a deletion independently of its denormalized head flag."""
    from scope_recall.core import lexical_index

    core, ctx = app
    item, _ = initial(core, ctx)
    authorize(core, ctx, item)
    core.forget(ctx, request(item), remaining_seconds=10)
    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        conn.execute("UPDATE claims SET read_blocked=0 WHERE claim_id=?", (item.ref,))
        lexical_index.index_claim(conn, item.ref)
        assert not held(tx, item.ref)
