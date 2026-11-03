"""Current fact postings and indexed nomination over synthetic scoped truth."""

from dataclasses import replace

import pytest
from scope_recall.core.retrieval import SearchContext
from scope_recall.core.retrieval_storage import RetrievalStorage

from tests.contract.test_claims import accept, app, capture, draft, initial, revise_request
from tests.contract.test_deletion import authorize, request
from tests.v11_support import downgrade_store, recall_request


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


def test_unrelated_multiversion_facts_do_not_load_versions(app, monkeypatch):
    """Version reads follow query subjects rather than unrelated revision count."""
    import json

    from scope_recall.core import lexical_index

    core, ctx = app
    records = [(f"claim-TEST-unrelated-{i}", f"unrelated{i}", "theme", "amber") for i in range(80)]
    records.append(("claim-TEST-related", "alpha", "beta", "amber"))
    _synthetic_claims(core, ctx, records)
    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        for ref, *_ in records:
            row = dict(conn.execute("SELECT * FROM claim_versions WHERE claim_id=?", (ref,)).fetchone())
            row.update(revision=2, payload_json=json.dumps(json.loads(row["payload_json"])))
            conn.execute(
                f"INSERT INTO claim_versions({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                tuple(row.values()),
            )
            lexical_index.index_claim(conn, ref)
    fetched = []
    with core.storage.read(ctx) as tx:
        native = type(tx.claims).prefetch_versions

        def tracked(self, refs):
            refs = tuple(refs)
            fetched.extend(refs)
            return native(self, refs)

        monkeypatch.setattr(type(tx.claims), "prefetch_versions", tracked)
        RetrievalStorage().claims(tx, search(core, ctx, "alpha beta amber"), limit=6)
    assert fetched == ["claim-TEST-related"]


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
                assert indexed == _ideal_claims(tx, context, 48, monkeypatch), (mode, query, trusted)


@pytest.mark.parametrize("extra", [("amber", "cobalt", "green"), ("amber", "cobalt", "green", "blue")])
@pytest.mark.parametrize("mode", ["auto", "current"])
def test_subject_candidates_preserve_hit_order_after_policy(app, monkeypatch, extra, mode):
    """Compare subject-qualified postings to an exhaustive visible-head oracle."""
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
            ref for _, ref in sorted(exhaustive)
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


def test_subject_posting_probe_precedes_head_policy_lookup(app):
    """Only subject-qualified posting refs should pay for reading a claim head."""
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
        query = next(sql for sql in statements if sql.startswith("WITH subjects AS MATERIALIZED"))
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + query)]
        probe = next(index for index, step in enumerate(plan) if "SEARCH p" in step)
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


def _synthetic_claims(core, ctx, records):
    """Clone a valid synthetic version while varying the fields relevant to nomination."""
    import json

    from scope_recall.core import lexical_index

    item, _ = initial(core, ctx)
    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        head = dict(conn.execute("SELECT * FROM claims WHERE claim_id=?", (item.ref,)).fetchone())
        version = dict(conn.execute("SELECT * FROM claim_versions WHERE claim_id=?", (item.ref,)).fetchone())
        for ref, subject, predicate, value in records:
            clone = dict(head, claim_id=ref, subject=subject, predicate=predicate, slot_key=ref)
            payload = dict(json.loads(version["payload_json"]), subject=subject, predicate=predicate, value_text=value)
            body = dict(version, claim_id=ref, payload_json=json.dumps(payload))
            for table, row in (("claims", clone), ("claim_versions", body)):
                conn.execute(
                    f"INSERT INTO {table}({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values())
                )
            lexical_index.index_claim(conn, ref)


def _ideal_claims(tx, context, limit, monkeypatch):
    """Score every visible fact through the real version selector and final ranking."""
    from scope_recall.core import retrieval_storage

    visibility, parameters = retrieval_storage._claim_visibility(context)
    rows = tx._check().execute(f"SELECT c.claim_id FROM claims c WHERE {visibility}", parameters).fetchall()
    with monkeypatch.context() as patch:
        patch.setattr(retrieval_storage, "_claim_rows", lambda *_: rows)
        return RetrievalStorage().claims(tx, context, limit=limit)


def test_subjectless_distractors_cannot_hide_a_qualified_answer(app, monkeypatch):
    """A token tie must not spend the candidate budget on facts that cannot answer."""
    core, ctx = app
    records = [(f"claim-TEST-a{i:04d}", f"unrelated{i}", "alpha beta", "plain") for i in range(256)]
    target = "claim-TEST-zz-answer"
    _synthetic_claims(core, ctx, [*records, (target, "alpha", "beta", "catfish")])
    context = search(core, ctx, "alpha beta cat")
    with core.storage.read(ctx) as tx:
        ideal = _ideal_claims(tx, context, 48, monkeypatch)
        actual = RetrievalStorage().claims(tx, context, limit=48)
    assert [item.ref for item in ideal] == [target]
    assert actual == ideal


@pytest.mark.parametrize("query", ["alpha beta cat", "alpha beta cat 海青 部署"])
@pytest.mark.parametrize("seed", [7, 4111, 992])
@pytest.mark.parametrize("mode", ["current", "auto", "as_of", "history"])
def test_adversarial_nomination_matches_unbounded_final_ranking(app, monkeypatch, seed, mode, query):
    """Random predicate/value noise and more than 256 answers preserve ideal top limits."""
    import json
    import random

    from scope_recall.core import lexical_index, retrieval_storage

    core, ctx = app
    rng = random.Random(seed)
    records = []
    for index in range(1200):
        subject = rng.choice(["alpha", "alpha beta", "alpha unrelated", "海青", f"unrelated{index}"])
        records.append(
            (
                f"claim-TEST-random-{index:04d}",
                subject,
                rng.choice(["beta", "beta cat", "theme"]),
                rng.choice(["catfish", "cat", "plain", "部署"]),
            )
        )
    _synthetic_claims(core, ctx, records)
    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        for index, (ref, *_rest) in enumerate(records):
            # Proposal thresholds, condition-only extra hits and both policy
            # flags affect final eligibility independently of body hit order.
            if index % 5 == 0:
                conn.execute("UPDATE claim_versions SET state='proposed' WHERE claim_id=?", (ref,))
            if index % 7 == 0:
                conn.execute(
                    "UPDATE claim_versions SET payload_json=json_set(payload_json,'$.conditions',json(?)) "
                    "WHERE claim_id=?",
                    (json.dumps(["cat"]), ref),
                )
            conn.execute(
                "UPDATE claims SET read_blocked=?,suppressed=? WHERE claim_id=?",
                (int(index % 17 == 0), int(index % 13 == 0), ref),
            )
            if mode != "as_of":
                conn.execute(
                    "UPDATE claim_versions SET valid_from=NULL,"
                    "payload_json=json_set(payload_json,'$.valid_from',NULL) WHERE claim_id=?",
                    (ref,),
                )
            stamp = [
                "2026-09-01T12:00:00Z",
                "2026-09-01T12:00:00.000001+00:00",
                "2026-09-01T12:00:00.999999+00:00",
                "2026-09-01T13:00:00+01:00",
            ][index % 4]
            conn.execute("UPDATE claim_versions SET recorded_from=? WHERE claim_id=?", (stamp, ref))
            lexical_index.index_claim(conn, ref)
    context = search(core, ctx, query, mode)
    with core.storage.read(ctx) as tx:
        visibility, parameters = retrieval_storage._claim_visibility(context)
        heads = (
            tx._check()
            .execute(
                "SELECT c.claim_id,lower(c.subject||' '||c.predicate||' '||"
                "coalesce(json_extract(v.payload_json,'$.value_text'),'')) statement FROM claims c "
                "JOIN claim_versions v ON v.claim_id=c.claim_id AND v.revision=c.current_revision "
                f"WHERE {visibility}",
                parameters,
            )
            .fetchall()
        )
        terms = retrieval_storage.meaningful_query_terms(context.query)
        old_rows = sorted(heads, key=lambda row: (-sum(term in row["statement"] for term in terms), row["claim_id"]))[
            :256
        ]
        for limit in (6, 300, 1500):
            ideal = _ideal_claims(tx, context, limit, monkeypatch)
            actual = RetrievalStorage().claims(tx, context, limit=limit)
            with monkeypatch.context() as patch:
                patch.setattr(retrieval_storage, "_claim_rows", lambda *_: old_rows)
                previous = RetrievalStorage().claims(tx, context, limit=limit)
            assert actual == ideal, (seed, mode, limit)
            ideal_refs = {item.ref for item in ideal}
            assert len(ideal_refs - {item.ref for item in actual}) <= len(ideal_refs - {item.ref for item in previous})
        assert len(ideal) > 256


@pytest.mark.parametrize("mode", ["current", "auto", "history", "method"])
def test_dense_undated_heads_preserve_ideal_ranking_without_loading_the_whole_pool(app, monkeypatch, mode):
    """A popular subject can have hundreds of answers; final rank bounds version hydration."""
    from scope_recall.core import lexical_index

    core, ctx = app
    records = [(f"claim-TEST-dense-{i:04d}", "alpha beta", "cat" if i % 3 else "theme", "plain") for i in range(600)]
    _synthetic_claims(core, ctx, records)
    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        conn.execute(
            "UPDATE claim_versions SET valid_from=NULL,payload_json=json_set(payload_json,'$.valid_from',NULL) "
            "WHERE claim_id LIKE 'claim-TEST-dense-%'"
        )
        for ref, *_ in records:
            lexical_index.index_claim(conn, ref)
    context = search(core, ctx, "alpha beta cat", mode)
    with core.storage.read(ctx) as tx:
        ideal = _ideal_claims(tx, context, 48, monkeypatch)
        fetched = []
        native = type(tx.claims).prefetch_versions

        def tracked(self, refs):
            refs = tuple(refs)
            fetched.extend(refs)
            return native(self, refs)

        monkeypatch.setattr(type(tx.claims), "prefetch_versions", tracked)
        actual = RetrievalStorage().claims(tx, context, limit=48)
    assert actual == ideal
    assert len(fetched) <= 48


@pytest.mark.parametrize("change", ["state", "validity", "payload", "insert", "head"])
def test_truth_edits_invalidate_rank_certificates_before_limiting(app, monkeypatch, change):
    """A state or payload edit cannot leave a stale exact-rank shortcut hiding another answer."""
    core, ctx = app
    refs = ["claim-TEST-cert-a", "claim-TEST-cert-b"]
    _synthetic_claims(core, ctx, [(refs[0], "alpha", "beta", "cat"), (refs[1], "alpha", "beta", "plain")])
    from scope_recall.core import lexical_index

    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        for ref in refs:
            conn.execute(
                "UPDATE claim_versions SET valid_from=NULL,"
                "payload_json=json_set(payload_json,'$.valid_from',NULL) WHERE claim_id=?",
                (ref,),
            )
            lexical_index.index_claim(conn, ref)
        assert conn.execute("SELECT is_simple FROM claim_lexical_heads WHERE claim_id=?", (refs[0],)).fetchone()[0] == 1
        statements = {
            "state": "UPDATE claim_versions SET state='proposed' WHERE claim_id=?",
            "validity": "UPDATE claim_versions SET valid_to='2026-01-01T00:00:00Z' WHERE claim_id=?",
            "payload": "UPDATE claim_versions SET payload_json=json_set(payload_json,'$.conditions',"
            "json('[\"cat\"]')) WHERE claim_id=?",
            "head": "UPDATE claims SET predicate='other' WHERE claim_id=?",
        }
        if change == "insert":
            row = dict(conn.execute("SELECT * FROM claim_versions WHERE claim_id=?", (refs[0],)).fetchone())
            row.update(revision=2, state="proposed")
            conn.execute(
                f"INSERT INTO claim_versions({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                tuple(row.values()),
            )
        else:
            conn.execute(statements[change], (refs[0],))
        assert conn.execute("SELECT is_simple FROM claim_lexical_heads WHERE claim_id=?", (refs[0],)).fetchone()[0] == 0
    context = search(core, ctx, "alpha beta cat")
    with core.storage.read(ctx) as tx:
        assert RetrievalStorage().claims(tx, context, limit=1) == _ideal_claims(tx, context, 1, monkeypatch)


def test_long_queries_retain_subject_terms_for_final_scoring(app, monkeypatch):
    """Long queries retain the subject terms required by final scoring."""
    core, ctx = app
    _synthetic_claims(core, ctx, [("claim-TEST-long-query", "alpha", "beta", "plain")])
    query = " ".join([*(f"unmatchedidentifier{i:03d}" for i in range(40)), "alpha", "beta"])
    context = search(core, ctx, query)
    with core.storage.read(ctx) as tx:
        actual = RetrievalStorage().claims(tx, context, limit=6)
        assert actual == _ideal_claims(tx, context, 6, monkeypatch)
        assert [item.ref for item in actual] == ["claim-TEST-long-query"]


def test_replayed_absence_cannot_take_a_readable_facts_rank_slot(app, monkeypatch):
    """A restored absence is excluded before exact nomination consumes its result limit."""
    from scope_recall.contracts import TrustedContext
    from scope_recall.core import lexical_index
    from scope_recall.core.restore import (
        InstallationMaintenance,
        begin_restore,
        export_deletion_ledger,
        ledger_digest,
        replay_deletion_ledger,
    )

    core, ctx = app
    blocked, readable = "claim-TEST-absence-a", "claim-TEST-absence-b"
    _synthetic_claims(core, ctx, [(blocked, "alpha", "beta", "cat"), (readable, "alpha", "beta", "plain")])
    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        for ref in (blocked, readable):
            conn.execute("UPDATE claim_versions SET valid_from=NULL WHERE claim_id=?", (ref,))
            lexical_index.index_claim(conn, ref)
        conn.execute(
            "INSERT INTO restored_absence_blocks VALUES ('claim',?,?,?,?,?,?)",
            (blocked, "TEST-scope", ctx.project_id, ctx.branch_id, "a" * 64, "governance_body_missing_from_snapshot"),
        )
    authority = InstallationMaintenance(
        TrustedContext(ctx.binding, "TEST-restore", ctx.binding.scope_ids, "host_generated")
    )
    ledger = export_deletion_ledger(core.storage, authority)
    begin_restore(core.storage, authority, expected_ledger_sha256=ledger_digest(ledger))
    with core.storage._transaction(authority.context, writable=True, remaining_seconds=10, restoring=True) as tx:
        tx._check(write=True).execute("DELETE FROM restored_absence_blocks")
    replay_deletion_ledger(core.storage, authority, ledger)
    context = search(core, ctx, "alpha beta cat")
    with core.storage.read(ctx) as tx:
        assert tx.claims.versions(blocked) == ()
        ideal = _ideal_claims(tx, context, 1, monkeypatch)
        actual = RetrievalStorage().claims(tx, context, limit=1)
    assert [item.ref for item in ideal] == [readable]
    assert actual == ideal


def _normalized_subject_history(core, ctx, future):
    """Append a normalized successor to a synthetic legacy slot through the real writer."""
    from scope_recall.contracts import TrustedSourcePrincipal
    from scope_recall.core.claims import Qualification

    ctx = replace(ctx, source_principal=TrustedSourcePrincipal("human", "verified", "principal:human:TEST"))
    prefix = "项目【TEST-project】"
    source = capture(core, ctx, prefix + "alpha beta使用amber。")
    proposal = draft(source, "amber", subject=prefix + "alpha beta", predicate="使用")
    qualification = Qualification("active", "direct_report", "synthetic first-hand assertion")
    with core.storage.write(ctx) as tx:
        old = tx.claims.append("TEST-scope", proposal, qualification, recorded_at="2026-09-01T12:00:00Z")
    source = capture(core, ctx, prefix + "alpha beta使用cobalt。", when="2026-09-02T12:00:00Z")
    proposal = draft(
        source,
        "cobalt",
        subject="alpha beta",
        predicate="使用",
        conditions=[prefix],
        valid_from="2026-09-10T12:00:00Z" if future else "2026-09-03T12:00:00Z",
    )
    with core.storage.write(ctx) as tx:
        new = tx.claims.append(
            "TEST-scope",
            proposal,
            qualification,
            recorded_at="2026-09-02T12:00:00Z",
            previous=tx.claims.version(old.ref, 1),
        )
    assert new.ref == old.ref and new.revision == 2
    return old.ref


@pytest.mark.parametrize("case", ["as_of", "future", "old_subject_only"])
def test_normalized_slot_versions_are_scored_on_the_selected_subject(app, monkeypatch, case):
    """Historical or still-effective legacy subjects survive a differently named current head."""
    core, ctx = app
    ref = _normalized_subject_history(core, ctx, case != "as_of")
    query = "项目 TEST-project" if case == "old_subject_only" else "项目 TEST-project 使用"
    context = search(core, ctx, query, "as_of" if case == "as_of" else "current")
    if case == "as_of":
        context = replace(context, as_of="2026-09-01T13:00:00Z")
    with core.storage.read(ctx) as tx:
        ideal = _ideal_claims(tx, context, 1, monkeypatch)
        actual = RetrievalStorage().claims(tx, context, limit=1)
    assert [(item.ref, item.revision) for item in ideal] == [(ref, 1)]
    assert actual == ideal
    if case == "as_of":
        after = replace(search(core, ctx, "alpha beta 使用", "as_of"), as_of="2026-09-04T12:00:00Z")
        with core.storage.read(ctx) as tx:
            ideal = _ideal_claims(tx, after, 1, monkeypatch)
            actual = RetrievalStorage().claims(tx, after, limit=1)
        assert [(item.ref, item.revision) for item in ideal] == [(ref, 2)]
        assert actual == ideal


def test_entity_completion_keeps_exact_rank_and_bounded_version_prefetch(app, monkeypatch):
    """Real entity jobs change processing state without invalidating an otherwise exact rank."""
    from scope_recall.core.claims import Qualification

    from tests.contract.test_entity_resolution import RecordedModel, drain_entities

    core, ctx = app
    refs = []
    for index in range(6):
        source = capture(core, ctx, f"alpha beta tag{index} cat.")
        proposal = draft(
            source,
            "cat",
            subject="alpha beta",
            predicate=f"tag{index}",
            kind="fact",
            statement_kind="assertion",
            valid_from=None,
        )
        with core.storage.write(ctx) as tx:
            refs.append(
                tx.claims.append(
                    "TEST-scope",
                    proposal,
                    Qualification("active", "direct_report", "synthetic"),
                    recorded_at="2026-09-01T12:00:00Z",
                ).ref
            )
    receipt = drain_entities(core, ctx, RecordedModel(False))
    assert receipt.failed == 0
    with core.storage.read(ctx) as tx:
        conn = tx._check()
        assert conn.execute("SELECT count(*) FROM claim_versions WHERE entity_resolved=1").fetchone()[0] == 6
        simple = conn.execute("SELECT count(*) FROM claim_lexical_heads WHERE is_simple=1").fetchone()[0]
        fetched = []
        native = type(tx.claims).prefetch_versions

        def tracked(self, selected):
            selected = tuple(selected)
            fetched.extend(selected)
            return native(self, selected)

        monkeypatch.setattr(type(tx.claims), "prefetch_versions", tracked)
        actual = RetrievalStorage().claims(tx, search(core, ctx, "alpha beta cat"), limit=1)
    assert actual
    assert (simple, len(fetched)) == (6, 1)


@pytest.mark.parametrize("release", ["owner", "rescan"])
@pytest.mark.parametrize("indirect", [False, True])
def test_screened_dependencies_are_filtered_before_the_claim_rank_limit(app, monkeypatch, release, indirect):
    """A real source hold cannot consume a readable fact's exact-rank slot."""
    from scope_recall.core import lexical_index, lineage
    from scope_recall.core.injection_guard import screen_sources
    from scope_recall.core.restore import InstallationMaintenance
    from scope_recall.core.source_records import SourceWrite

    core, ctx = app
    a_source = capture(core, ctx, "alpha beta cat.")
    b_source = capture(core, ctx, "alpha beta readable plain.")
    refs = []
    for source, value, predicate in [(a_source, "cat", "beta"), (b_source, "plain", "beta readable")]:
        item = accept(core, ctx, draft(source, value, subject="alpha", predicate=predicate, valid_from=None)).items[0]
        refs.append(item.ref)
    assert len(set(refs)) == 2
    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        for ref in refs:
            conn.execute("UPDATE claim_versions SET state='active',valid_from=NULL WHERE claim_id=?", (ref,))
            lexical_index.index_claim(conn, ref)
        if indirect:
            conn.execute("DELETE FROM evidence_links WHERE object_kind='claim' AND object_ref=?", (refs[0],))
            lineage.depend(conn, "claim", refs[0], 1, "event", a_source.ref, 1)
        conn.execute(
            "INSERT INTO injection_rules VALUES ('TEST-rank-hold','instruction','tokens',?,?,1)",
            ('{"slots":[["cat"]],"span":256}', "pending_review" if release == "rescan" else "quarantined"),
        )
        screen_sources(tx, [SourceWrite("duplicate", a_source.ref, 1)], core.clock.utc_now(), remaining_seconds=2)
        assert tx.claims.versions(refs[0]) == ()
        assert conn.execute("SELECT is_simple FROM claim_lexical_heads WHERE claim_id=?", (refs[0],)).fetchone()[0] == 1
    context = search(core, ctx, "alpha beta cat")
    with core.storage.read(ctx) as tx:
        ideal = _ideal_claims(tx, context, 1, monkeypatch)
        actual = RetrievalStorage().claims(tx, context, limit=1)
    assert [item.ref for item in ideal] == [refs[1]]
    assert actual == ideal
    downgrade_store(core.storage.path, 1116)
    core.storage.initialize()
    context = search(core, ctx, "alpha beta cat")
    with core.storage.read(ctx) as tx:
        assert RetrievalStorage().claims(tx, context, limit=1) == _ideal_claims(tx, context, 1, monkeypatch) == ideal
    if release == "owner":
        # The review targets the source, while the dependent fact's head and
        # postings stay unchanged throughout the hold and release.
        from scope_recall.core.quarantine_management import preview_release, release_source

        preview = preview_release(core, InstallationMaintenance(ctx), a_source.ref, 1)
        release_source(core, InstallationMaintenance(ctx), a_source.ref, 1, content_sha256=preview["content_sha256"])
    else:
        with core.storage.write(ctx) as tx:
            tx._check(write=True).execute("UPDATE injection_rules SET enabled=0 WHERE rule_id='TEST-rank-hold'")
            screen_sources(tx, [SourceWrite("duplicate", a_source.ref, 1)], core.clock.utc_now(), remaining_seconds=0)
        core.drain_worker(ctx, max_items=8, remaining_seconds=10)
    with core.storage.read(ctx) as tx:
        assert tx.claims.versions(refs[0])
        actual = RetrievalStorage().claims(tx, context, limit=1)
        assert actual == _ideal_claims(tx, context, 1, monkeypatch)
    assert [item.ref for item in actual] == [refs[0]]


def test_uncertified_head_edits_remain_searchable(app):
    from scope_recall.core import lexical_index

    core, ctx = app
    _synthetic_claims(core, ctx, [("claim-TEST-kind-edit", "alpha", "beta", "amber")])
    with core.storage.write(ctx) as tx:
        conn = tx._check(write=True)
        conn.execute("UPDATE claims SET kind='alias' WHERE claim_id='claim-TEST-kind-edit'")
        lexical_index.index_claim(conn, "claim-TEST-kind-edit")
        conn.execute("UPDATE claims SET kind='fact' WHERE claim_id='claim-TEST-kind-edit'")
    with core.storage.read(ctx) as tx:
        assert [row.ref for row in RetrievalStorage().claims(tx, search(core, ctx, "alpha beta amber"), limit=6)] == [
            "claim-TEST-kind-edit"
        ]
