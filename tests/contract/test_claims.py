"""P05 state contracts over captured synthetic text, not end-to-end model scores."""

import itertools
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from threading import Barrier

import pytest
from scope_recall.contracts import ContractError, ImportProvenance, TrustedSourcePrincipal, import_source_fingerprint
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.claims import (
    ClaimVersion,
    Qualification,
    holds_instead,
    is_denial,
    negates,
    qualify,
    states_a_value,
)
from scope_recall.core.confirmation import CONFIRMED_REASON
from test_denial_values import NEGATED_STATES
from v11_support import context, source_event


class Clock:
    now = "2026-09-06T12:00:00Z"

    def utc_now(self):
        return self.now

    def monotonic(self):
        return time.monotonic()


@pytest.fixture
def app(tmp_path):
    ctx = replace(context(tmp_path / "TEST-claims"), project_id="TEST-project", branch_id="TEST-main")
    core = MemoryCore(CoreConfig(ctx.binding), clock=Clock())
    core.initialize()
    core.test_sequence = itertools.count(1)
    return core, ctx


def capture(
    core,
    ctx,
    raw,
    *,
    origin="human_direct",
    key=None,
    revision=1,
    when="2026-09-01T12:00:00Z",
    attested=False,
    **changes,
):
    event = source_event(
        source_event_key=key or f"TEST-claims/{next(core.test_sequence)}",
        source_revision=revision,
        origin=origin,
        role="tool"
        if origin == "tool_observation"
        else "document"
        if origin in {"external_document", "imported"}
        else "user",
        content=raw,
        occurred_at=when,
        time_precision="unknown" if when is None else "instant",
        **changes,
    )
    importer = (
        ImportProvenance("human_direct", "a" * 64, frozenset({import_source_fingerprint(event)})) if attested else None
    )
    actor = replace(ctx, actor_origin=origin, import_provenance=importer)
    saved = core.record_event(actor, event, scope_id="TEST-scope", remaining_seconds=10)
    assert saved.durability == "persisted"
    return core.source(ctx, saved.event_refs[0].ref, revision)


def draft(source, value="蓝色", *, quote_override=None, **changes):
    quote = source.event["content"] if quote_override is None else quote_override
    result = dict(
        kind="decision",
        subject="TEST-project",
        predicate="配色",
        value_text=value,
        conditions=[],
        statement_kind="decision",
        valid_from=source.event["occurred_at"],
        valid_to=None,
        evidence_spans=[dict(source_ref=source.ref, source_revision=source.revision, quote=quote)],
    )
    result.update(changes)
    return result


def accept(core, ctx, *claims):
    refs = list(dict.fromkeys(f"{s['source_ref']}@{s['source_revision']}" for c in claims for s in c["evidence_spans"]))
    for c in claims:
        refs.extend(r for r in c.get("intention", {}).get("state_evidence_refs", []) if r not in refs)
        refs.extend(r for r in c.get("procedure", {}).get("counterexample_refs", []) if r not in refs)
    return core.records.accept_claim_proposals(
        ctx,
        dict(
            protocol_version="1.1",
            source_refs=refs,
            claim_proposals=list(claims),
            resume_proposals=[],
            reference_proposals=[],
        ),
        scope_id="TEST-scope",
        remaining_seconds=10,
    )


def initial(core, ctx, *, value="蓝色", when="2026-09-01T12:00:00Z", **changes):
    source = capture(core, ctx, f"TEST-project 配色 {value}。", when=when)
    item = accept(core, ctx, draft(source, value, **changes)).items[0]
    assert item.state == "active"
    return item, source


def revise_request(item, source, value="银色", **changes):
    request = dict(
        protocol_version="1.1",
        target_ref=item.ref,
        expected_revision=item.revision,
        new_value=value,
        conditions=[],
        source_evidence_refs=[f"{source.ref}@{source.revision}"],
        valid_from=source.event["occurred_at"],
    )
    request.update(changes)
    return request


def counts(core):
    with sqlite3.connect(core.storage.path) as conn:
        return {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("claims", "claim_versions", "evidence_links", "work_items")
        }


def test_C04_M26_capture_explicit_attribute_correction_is_immediate_without_vectors(app):
    core, ctx = app
    item, _ = initial(core, ctx, value="H100", kind="fact", predicate="配色")

    class NoModels:
        def __getattr__(self, key):
            raise AssertionError("mutation cannot call models")

    core.vectors = core.consolidation = NoModels()
    correction = capture(core, ctx, "刚才写错了，TEST-project 用H200。", when="2026-09-03T12:00:00Z")
    current = core.current_claim(ctx, item.ref)
    assert current.payload["value_text"] == "H200" and current.revision == 2
    history = core.claim_history(ctx, item.ref)
    assert history[0].payload["value_text"] == "H100" and history[1].replaces_revision == 1
    assert history[0].recorded_to == history[1].recorded_from
    assert core.source(ctx, correction.ref, 1).event["content"] == "刚才写错了，TEST-project 用H200。"
    with sqlite3.connect(core.storage.path) as conn:
        work = conn.execute(
            "SELECT subject_revision,work_type,state FROM work_items WHERE subject_ref=? ORDER BY subject_revision,work_type",
            (item.ref,),
        ).fetchall()
    # Every claim head is queued for the vector index too, so search can reach
    # the derived layer instead of depending on relation expansion out of some
    # event that happened to be retrieved first. Naming the work types keeps the
    # property explicit rather than implied by a row count: the superseded
    # revision's work is obsoleted and the new revision's is queued — and none
    # of it called a model, which NoModels above would have caught.
    assert work == [
        (1, "embed", "obsolete"),
        (1, "rebuild_projection", "obsolete"),
        (2, "embed", "pending"),
        (2, "rebuild_projection", "pending"),
    ]


def test_C05_late_import_cannot_replace_current_and_recorded_time_does_not_become_valid_time(app):
    core, ctx = app
    current, _ = initial(core, ctx, value="银色", when="2026-09-03T12:00:00Z")
    old = capture(
        core, ctx, "TEST-project 配色 蓝色。", origin="imported", source_original_origin="human_direct", attested=True
    )
    inserted = accept(core, ctx, draft(old)).items[0]
    assert inserted.disposition == "historical" and inserted.revision == 2
    assert core.current_claim(ctx, current.ref).payload["value_text"] == "银色"
    assert core.current_claim(ctx, current.ref, as_of="2026-09-02T12:00:00Z").payload["value_text"] == "蓝色"
    history = core.claim_history(ctx, current.ref)
    assert history[1].valid_from != history[1].recorded_from and history[1].replaces_revision is None
    assert all(v.current_revision == 1 for v in history)


def test_unknown_time_is_not_filled_from_recorded_and_cannot_answer_past(app):
    core, ctx = app
    item, _ = initial(core, ctx, when=None)
    version = core.current_claim(ctx, item.ref)
    assert version.valid_from is None and version.valid_to is None
    assert core.current_claim(ctx, item.ref, as_of="2026-09-01T12:00:00Z") is None


def test_C06_C07_scope_project_branch_are_read_and_evidence_boundaries(app):
    core, ctx = app
    item, source = initial(core, ctx)
    for other in (replace(ctx, project_id="TEST-other"), replace(ctx, branch_id="TEST-exp")):
        assert core.current_claim(other, item.ref) is None
        assert core.claim_history(other, item.ref) == ()
        with pytest.raises(ContractError, match="SOURCE_MISSING"):
            accept(core, other, draft(source))
    with core.storage.write(replace(ctx, project_id="TEST-other")) as tx:
        with pytest.raises(ContractError, match="ACCESS_DENIED"):
            tx.claims.require_target(core.claim_history(ctx, item.ref)[0])


def test_conditional_variants_and_C23_C24_constraints_coexist(app):
    core, ctx = app
    constrained = capture(core, ctx, "TEST-project 未授权不改网站，只限写文案任务。")
    c = accept(
        core,
        ctx,
        draft(constrained, "不改网站", kind="constraint", predicate="网站修改", conditions=["未授权", "写文案任务"]),
    ).items[0]
    allowed = capture(core, ctx, "TEST-project 沙箱已授权修改网页，只限TEST沙箱。", when="2026-09-03T12:00:00Z")
    a = accept(
        core,
        ctx,
        draft(allowed, "修改网页", kind="constraint", predicate="网站修改", conditions=["TEST沙箱", "已授权"]),
    ).items[0]
    assert c.state == a.state == "active" and c.ref != a.ref
    assert core.current_claim(ctx, c.ref).payload["conditions"] == ["未授权", "写文案任务"]
    assert core.current_claim(ctx, a.ref).payload["conditions"] == ["TEST沙箱", "已授权"]


def test_M27_finite_exception_expires_and_does_not_permanently_change_regular_rule(app):
    core, ctx = app
    item, _ = initial(core, ctx, value="白底")
    source = capture(core, ctx, "TEST-project 配色 今天破例用黑底，只限今天。", when="2026-09-06T00:00:00Z")
    exception = accept(core, ctx, draft(source, "黑底", valid_to="2026-09-07T00:00:00Z")).items[0]
    assert exception.state == "active" and exception.ref == item.ref
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "黑底"
    core.clock.now = "2026-09-07T00:00:00Z"
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "白底"


def test_M28_future_rule_and_recorded_asof_use_distinct_axes(app):
    core, ctx = app
    core.clock.now = "2026-09-01T12:00:00Z"
    item, _ = initial(core, ctx, value="短版")
    core.clock.now = "2026-09-02T12:00:00Z"
    source = capture(core, ctx, "从2026年9月3日起，TEST-project 配色 详细版。", when="2026-09-02T12:00:00Z")
    next_version = accept(core, ctx, draft(source, "详细版", valid_from="2026-09-03T00:00:00Z")).items[0]
    assert next_version.state == "active"
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "短版"
    assert core.current_claim(ctx, item.ref, as_of="2026-09-04T00:00:00Z").payload["value_text"] == "详细版"
    assert (
        core.current_claim(ctx, item.ref, as_of="2026-09-04T00:00:00Z", known_at="2026-09-01T15:00:00Z").payload[
            "value_text"
        ]
        == "短版"
    )
    # Original valid intervals remain immutable, with replacement links deciding
    # effective versions at the requested time/knowledge boundary.
    assert core.claim_history(ctx, item.ref)[0].valid_to is None


def test_invented_dates_and_unknown_historical_time_stay_proposed(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 配色 蓝色。", when=None)
    assert accept(core, ctx, draft(source, valid_from="2026-08-01T00:00:00Z")).items[0].state == "proposed"
    past = capture(core, ctx, "TEST-project 以前配色 银色。", when=None)
    assert accept(core, ctx, draft(past, "银色")).items[0].state == "proposed"


def test_M29_observation_basis_does_not_claim_present_verification(app):
    core, ctx = app
    source = capture(
        core,
        ctx,
        "2026年8月1日TEST-project 报价 100单位，这是当日报价。",
        origin="tool_observation",
        when="2026-08-01T00:00:00Z",
    )
    item = accept(core, ctx, draft(source, "100单位", kind="fact", predicate="报价")).items[0]
    version = core.current_claim(ctx, item.ref)
    assert version.basis == "observed" and version.reason == "observation_at_source_time"
    assert version.valid_from == "2026-08-01T00:00:00.000000+00:00"
    # Packet-level stale-observation labels and actual host answers are P06/P09.


def test_M30_external_conflicting_unknown_order_becomes_disputed_regardless_scores(app):
    core, ctx = app
    a = capture(core, ctx, "TEST-project 验收 完成。", origin="external_document", when=None)
    one = accept(core, ctx, draft(a, "完成", kind="fact", predicate="验收")).items[0]
    b = capture(core, ctx, "TEST-project 验收 失败。", origin="external_document", when=None)
    two = accept(core, ctx, draft(b, "失败", kind="fact", predicate="验收")).items[0]
    assert one.state == "active" and two.state == "disputed"
    version = core.current_claim(ctx, one.ref)
    assert version.state == "disputed" and version.conflict_revisions == (1,)
    assert len(core.claim_history(ctx, one.ref)) == 2


def test_same_assertion_candidate_collision_is_zero_mutation(app):
    core, ctx = app
    item, first = initial(core, ctx)
    another = capture(core, ctx, "TEST-project 配色 蓝色。")
    before = core.storage.path.read_bytes()
    after = accept(core, ctx, draft(another))
    assert after.items[0].disposition == "duplicate" and after.items[0].revision == 1
    assert core.storage.path.read_bytes() == before
    assert core.claim_history(ctx, item.ref)[0].payload["evidence_spans"][0]["source_ref"] == first.ref


def test_proposed_collision_cannot_demote_active_or_advance_pointer(app):
    core, ctx = app
    item, _ = initial(core, ctx)
    synthetic = capture(core, ctx, "假设TEST-project 配色 银色。", origin="assistant_visible")
    proposal = accept(core, ctx, draft(synthetic, "银色")).items[0]
    assert proposal.state == "proposed" and proposal.disposition == "historical"
    assert core.current_claim(ctx, item.ref).revision == 1


#: When the stated value is said, and when the denial is.
_SAID, _LATER = "2026-09-01T12:00:00Z", "2026-09-03T12:00:00Z"
_OWNER = TrustedSourcePrincipal("human", "verified", "principal:TEST-owner")


def _stated(core, ctx, text, value, when=_SAID, **frame):
    """The claim drawn from ``text`` with ``value``: a fact about TEST-project's CI service unless ``frame`` says
    otherwise."""
    frame = {"kind": "fact", "predicate": "CI service", "statement_kind": "assertion", **frame}
    return accept(core, ctx, draft(capture(core, ctx, text, when=when), value, **frame)).items[0]


def _preferred(core, ctx, text, value, when=_SAID, predicate="饮食"):
    """The owner's preference drawn from ``text``, through a context that attests them; and that context."""
    owner = replace(ctx, source_principal=_OWNER)
    return _stated(core, owner, text, value, when, kind="preference", subject="user", predicate=predicate), owner


@pytest.mark.parametrize(
    "stated,denied",
    [
        (
            ("The TEST-project CI service is Jenkins.", "Jenkins"),
            ("The TEST-project CI service is not CircleCI.", "not CircleCI"),
        ),
        (
            ("TEST-project CI service 是 Jenkins。", "Jenkins"),
            ("TEST-project CI service 不是 CircleCI。", "不是 CircleCI"),
        ),
    ],
)
def test_a_denial_of_another_value_leaves_the_value_its_slot_holds(app, stated, denied):
    """(a) Not CircleCI says nothing about whether Jenkins still holds: the denial is kept beside it, a proposal."""
    core, ctx = app
    held = _stated(core, ctx, *stated)
    denial = _stated(core, ctx, *denied, _LATER)
    assert held.state == "active"
    assert (denial.ref, denial.state, denial.disposition) == (held.ref, "proposed", "historical")
    assert core.claim_history(ctx, held.ref)[-1].reason == "denial_of_another_value"
    assert core.current_claim(ctx, held.ref).payload["value_text"] == "Jenkins"


@pytest.mark.parametrize("text,value", [("我不吃辣。", "不吃辣"), ("I never drink coffee.", "never drink coffee")])
def test_a_denial_where_its_slot_holds_nothing_is_its_value(app, text, value):
    """(b) With nothing else known, the owner's 我不吃辣 is the preference itself."""
    core, ctx = app
    claim, owner = _preferred(core, ctx, text, value)
    assert claim.state == "active"
    assert core.current_claim(owner, claim.ref).payload["value_text"] == value


@pytest.mark.parametrize(
    "stated,denied",
    [
        (("我爱吃辣。", "爱吃辣"), ("我现在不吃辣了。", "不吃辣")),
        (("I drink coffee every morning.", "drink coffee"), ("I don't drink coffee any more.", "don't drink coffee")),
    ],
)
def test_a_denial_of_the_value_its_slot_holds_replaces_it(app, stated, denied):
    """(c) 我现在不吃辣了 says that 爱吃辣 holds no more: the owner's preference changed."""
    core, ctx = app
    held, owner = _preferred(core, ctx, *stated)
    denial, _owner = _preferred(core, ctx, *denied, _LATER)
    assert (denial.ref, denial.state, denial.disposition) == (held.ref, "active", "inserted")
    assert core.current_claim(owner, held.ref).payload["value_text"] == denied[1]


@pytest.mark.parametrize(
    "stated,denied",
    [
        (("I prefer bash as my shell.", "bash"), ("I don't like PowerShell as a shell.", "don't like PowerShell")),
        (("我喜欢用 bash 当 shell。", "bash"), ("我不喜欢用 PowerShell 当 shell。", "不喜欢用 PowerShell")),
    ],
)
def test_a_dislike_of_something_else_leaves_the_preference_its_slot_holds(app, stated, denied):
    """(d) Not liking PowerShell does not say which shell the person prefers; said again, it changes nothing."""
    core, ctx = app
    held, owner = _preferred(core, ctx, *stated, predicate="shell")
    for when in (_LATER, "2026-09-04T12:00:00Z"):
        dislike, _owner = _preferred(core, ctx, *denied, when, predicate="shell")
        assert (dislike.ref, dislike.state) == (held.ref, "proposed")
    assert core.current_claim(owner, held.ref).payload["value_text"] == "bash"


@pytest.mark.parametrize(
    "value,denial",
    [
        *((value, False) for value in ("不错", "不过", "没问题", "不客气", "不同意 TEST-X", "不知道", "不用谢")),
        *((value, False) for value in ("not bad", "don't know", "not sure", "isn't it TEST-X?")),
        ("not only TEST-X but TEST-Y", False),
        *((value, True) for value in ("不是 CircleCI", "不吃辣", "不喜欢用 PowerShell", "从不抽烟")),
        *((value, True) for value in ("不再用 Jenkins", "讨厌香菜", "一点也不喜欢喝咖啡", "现在不吃辣了")),
        *((value, True) for value in ("not CircleCI", "isn't Jenkins", "never drink coffee")),
        *((value, True) for value in ("no longer uses Jenkins", "can't stand PowerShell", "really don't like X")),
    ],
)
def test_a_denial_is_a_negation_with_what_it_negates(value, denial):
    """不错 and "not bad" praise, 不过 and "not only ... but" contrast, 不同意 disagrees, 不知道, "don't know" and "not
    sure" tell what the person does not know, and "isn't it X?" asks: none says what its slot is not."""
    assert is_denial({"kind": "preference", "value_text": value}) is denial
    assert is_denial({"kind": "constraint", "value_text": value}) is False


@pytest.mark.parametrize(
    "value,denial",
    [
        # Set in capitals, or written as one word with what it negates, a value is a term: a keyword, a status, a flag.
        *((value, False) for value in ("NOT NULL", "NOT FOUND", "DON'T CARE", "NO_CACHE", "NOT_STARTED", "N/A")),
        *((value, False) for value in ("No.1", "no-op", "nonprofit", "never-ending story", "不锈钢", "无所谓")),
        # Not applicable says the slot does not apply, as "don't know" says what the person does not know.
        *((value, False) for value in ("Not Applicable", "not applicable", "doesn't apply")),
        # Capitals on the negator alone are emphasis; a sentence's first word and a name keep their own case.
        *((value, True) for value in ("NOT Jenkins any more", "Not CircleCI", "not AWS", "不是 AWS")),
        # A denial shouted whole in capitals is set as a term is, and read as one.
        ("NOT JENKINS", False),
    ],
)
def test_a_value_written_as_a_term_denies_nothing(value, denial):
    """NOT NULL is a column's constraint, NOT FOUND a lookup's status and NO_CACHE a flag: written as a term is
    written, in capitals or as one word, a value names the term and denies nothing.  A person who puts the negator
    alone in capitals stresses a denial ("NOT Jenkins any more"), which stays one."""
    assert is_denial({"kind": "fact", "value_text": value}) is denial


@pytest.mark.parametrize("command", ["restore_displaced_values", "requalify_claims"])
def test_maintenance_keeps_every_negated_state_and_settles_the_denials(app, command):
    """Every tabulated description stays active; both preview and apply settle only denials and retry is inert."""
    core, ctx = app
    payloads = []
    for index, value in enumerate((*NEGATED_STATES["zh"], *NEGATED_STATES["en"])):
        predicate = f"state {index}"
        source = capture(core, ctx, f"The TEST-project {predicate} is {value}.")
        payloads.append(draft(source, value, kind="fact", predicate=predicate, statement_kind="assertion"))
    with core.storage.write(ctx) as tx:
        verdict = Qualification("active", "direct_report", CONFIRMED_REASON)
        states = [tx.claims.append("TEST-scope", payload, verdict, recorded_at=Clock.now) for payload in payloads]
    jenkins = _stated(core, ctx, "The TEST-project CI service is Jenkins.", "Jenkins")
    _denial_at_the_head(core, ctx, jenkins, "The TEST-project CI service is not jenkins any more.", "not jenkins")
    _stated(core, ctx, "The TEST-project shell is bash.", "bash", predicate="shell")
    shell = _stated(core, ctx, "The TEST-project shell is Jenkins.", "Jenkins", _LATER, predicate="shell")
    _denial_at_the_head(core, ctx, shell, "The TEST-project shell is not bash.", "not bash")
    region = capture(core, ctx, "The TEST-project deploy region is not us-east-2.", when=_LATER)
    alone = draft(region, "not us-east-2", kind="fact", predicate="deploy region", statement_kind="assertion")
    with core.storage.write(ctx) as tx:
        verdict = Qualification("active", "direct_report", "explicit_scoped_source")
        alone_ref = tx.claims.append("TEST-scope", alone, verdict, recorded_at=Clock.now).ref

    def run(dry_run):
        cursor, changes = "", []
        for _ in range(len(states) + 3):
            report = getattr(core.operations, command)(ctx, after_ref=cursor, limit=32, dry_run=dry_run)
            changes.extend(report["changed"])
            if not report["last_ref"]:
                return changes
            cursor = report["last_ref"]
        pytest.fail("maintenance did not finish its bounded pages")

    assert {item["ref"] for item in run(True)} == {jenkins.ref, shell.ref, alone_ref}
    assert all(core.claim_history(ctx, claim.ref) == (claim,) for claim in states)
    assert {item["ref"] for item in run(False)} == {jenkins.ref, shell.ref, alone_ref}
    assert all(core.current_claim(ctx, claim.ref).state == "active" for claim in states)
    assert [core.current_claim(ctx, claim.ref).payload for claim in states] == payloads
    assert core.current_claim(ctx, jenkins.ref) is None and core.current_claim(ctx, alone_ref) is None
    assert core.current_claim(ctx, shell.ref).payload["value_text"] == "Jenkins"
    assert run(False) == []


def test_a_term_in_capitals_is_its_slot_s_value_and_maintenance_keeps_it(app):
    """TEST-project's id column is NOT NULL: written, that is the slot's value; and NOT FOUND, which a store already
    holds as a lookup's status, stays its value through requalify and restore-displaced-values."""
    core, ctx = app
    written = _stated(
        core, ctx, "The TEST-project id column constraint is NOT NULL.", "NOT NULL", predicate="id column constraint"
    )
    status = capture(core, ctx, "The TEST-project lookup status is NOT FOUND.", when=_LATER)
    held = draft(status, "NOT FOUND", kind="fact", predicate="lookup status", statement_kind="assertion")
    with core.storage.write(ctx) as tx:
        verdict = Qualification("active", "direct_report", "explicit_scoped_source")
        held_ref = tx.claims.append("TEST-scope", held, verdict, recorded_at=Clock.now).ref
    assert written.state == "active"
    for command in ("requalify_claims", "restore_displaced_values"):
        assert getattr(core.operations, command)(ctx, limit=32, dry_run=False)["changed"] == [], command
    assert core.current_claim(ctx, written.ref).payload["value_text"] == "NOT NULL"
    assert core.current_claim(ctx, held_ref).payload["value_text"] == "NOT FOUND"


@pytest.mark.parametrize(
    "stated,said,predicate",
    [
        (("TEST-project 部署状态 有故障。", "有故障"), ("TEST-project 部署状态 没问题。", "没问题"), "部署状态"),
        (
            ("The TEST-project deploy status is shaky.", "shaky"),
            ("The TEST-project deploy status is not bad.", "not bad"),
            "deploy status",
        ),
    ],
)
def test_a_value_that_opens_with_a_negator_word_updates_its_slot(app, stated, said, predicate):
    """没问题 and "not bad" deny nothing: said later, each replaces the value held as any other value does."""
    core, ctx = app
    held = _stated(core, ctx, *stated, predicate=predicate)
    update = _stated(core, ctx, *said, _LATER, predicate=predicate)
    assert (update.ref, update.state, update.disposition) == (held.ref, "active", "inserted")
    assert core.current_claim(ctx, held.ref).payload["value_text"] == said[1]


@pytest.mark.parametrize(
    "held,denied",
    [("bash", "not bash"), ("Jenkins", "not jenkins any more"), ("Jenkins", "NOT JENKINS ANY MORE")],
)
def test_writing_an_unmarked_denial_uses_the_slot_s_current_or_past_value(app, held, denied):
    """An unmarked word is retained alone, ends a matching value and leaves a different current value standing."""
    core, ctx = app
    alone = _stated(core, ctx, f"The TEST-project shell is {denied}.", denied, predicate="shell")
    assert alone.state == "active"
    first = _stated(core, ctx, f"The TEST-project CI service is {held}.", held)
    ended = _stated(core, ctx, f"The TEST-project CI service is {denied}.", denied, _LATER)
    assert ended.state == "retracted" and core.current_claim(ctx, first.ref) is None
    _stated(core, ctx, f"The TEST-project tool is {held}.", held, predicate="tool")
    current = _stated(core, ctx, "The TEST-project tool is Redis.", "Redis", _LATER, predicate="tool")
    denial = _stated(core, ctx, f"The TEST-project tool is {denied}.", denied, "2026-09-04T12:00:00Z", predicate="tool")
    assert denial.state == "proposed"
    assert core.current_claim(ctx, current.ref).payload["value_text"] == "Redis"


@pytest.mark.parametrize(
    "denied,held,same",
    [
        ("不吃辣", "爱吃辣", True),
        ("不吃辣了", "吃辣", True),
        ("不是周三", "周三下午", True),
        ("不是周三下午", "周三", False),
        ("don't like PowerShell", "PowerShell", True),
        ("don't drink coffee", "drinks coffee", True),
        ("NOT ＣｉｒｃｌｅＣＩ", "circleci", True),
        ("not v3.1", "v3.1", True),
        ("not v3", "v3.1", False),
        ("not 14 GB", "14 GB", True),
        ("not 4 GB", "14 GB", False),
        ("not #ops-pager", "#ops-pager", True),
        ("not CircleCI", "Jenkins", False),
        ("not MySQL", "SQL", False),
        ("不喜欢喝红茶", "红茶", True),
        ("不喜欢喝红茶", "茶", False),
        ("don't drive a car", "have a car", False),
        ("don't like having meetings early in the morning", "late in the morning", False),
        ("不吃了", "爱吃辣", False),
    ],
)
def test_a_denial_negates_a_value_that_holds_all_it_denies(denied, held, same):
    """Compared as the store compares values, identifiers whole: the held value holds all the denial denies, or the
    denial denies the held value itself under a verb of liking or doing.  A longer denial narrows what it denies, so
    不是周三下午 leaves 周三, as "not v3" leaves v3.1, standing; a verb alone (不吃了) names nothing to deny."""
    assert negates({"kind": "fact", "value_text": denied}, {"value_text": held}) is same


@pytest.mark.parametrize(
    "value,denial,negated",
    [
        ("不是很喜欢 TEST-X", True, True),
        ("不太喜欢 TEST-X", True, True),
        ("不再用 TEST-X 改用 TEST-Y", True, True),
        ("never use TEST-X again", True, True),
        ("别再用 TEST-X", True, True),
        ("不一定喜欢 TEST-X", False, False),
        ("never again", False, False),
        ("isn't any more", False, False),
        ("not TEST-X but TEST-Y", True, True),
    ],
)
def test_a_denial_is_read_as_its_negator_target_contrast_and_time(value, denial, negated):
    """A denial is read as its negator, the target it denies, what it says holds instead and a word of time, each for
    itself: the target of 不是很喜欢 X and of "never use X again" is X, 不一定喜欢 X doubts and denies nothing, and
    "never again" names no target at all."""
    assert is_denial({"kind": "preference", "value_text": value}) is denial
    assert negates({"kind": "preference", "value_text": value}, {"value_text": "TEST-X"}) is negated


@pytest.mark.parametrize(
    "text,value,predicate",
    [("我不吃了。", "不吃了", "饮食"), ("I don't drink any more.", "don't drink any more", "drink")],
)
def test_an_action_without_a_target_remains_the_person_s_value(app, text, value, predicate):
    """An action without a named target supplies no positive evidence of another slot value."""
    core, ctx = app
    denial, owner = _preferred(core, ctx, text, value, predicate=predicate)
    assert denial.state == "active"
    assert core.current_claim(owner, denial.ref).payload["value_text"] == value


@pytest.mark.parametrize(
    "stated,switched",
    [
        (("我用 TEST-X。", "TEST-X"), ("我现在不用 TEST-X 了，而是用 TEST-Y。", "不用 TEST-X 了，而是用 TEST-Y")),
        (("I use TEST-X.", "TEST-X"), ("I don't use TEST-X but TEST-Y now.", "don't use TEST-X but TEST-Y")),
    ],
)
def test_a_denial_that_says_what_holds_instead_ends_the_value_and_is_none(app, stated, switched):
    """不用 X 了，而是用 Y is two claims in one: it ends X, which it denies, and is no value itself, the person's own
    included; Y is a claim of its own.  (Said with 改用, it is a correction too, and capture applies Y at once.)"""
    core, ctx = app
    tool, owner = _preferred(core, ctx, *stated, predicate="tool")
    denial, _owner = _preferred(core, ctx, *switched, _LATER, predicate="tool")
    assert (tool.state, denial.ref, denial.state) == ("active", tool.ref, "retracted")
    assert core.current_claim(owner, tool.ref) is None
    assert core.claim_history(owner, tool.ref)[-1].reason == "denial_not_a_value"


@pytest.mark.parametrize(
    "text,value",
    [
        ("The TEST-project deploy region is not us-east-2.", "not us-east-2"),
        ("TEST-project deploy region 不是 us-east-2。", "不是 us-east-2"),
    ],
)
def test_a_denial_about_anything_but_the_person_is_never_its_slot_s_value(app, text, value):
    """(b) With nothing known of TEST-project's region, "not us-east-2" still does not say which it is; re-judging the
    stored claim's text does not make it the value either."""
    core, ctx = app
    denial = _stated(core, ctx, text, value, predicate="deploy region")
    assert (denial.state, core.claim_history(ctx, denial.ref)[-1].reason) == ("proposed", "denial_not_a_value")
    assert core.current_claim(ctx, denial.ref) is None
    assert core.operations.requalify_claims(ctx, limit=32, dry_run=False)["changed"] == []
    assert core.current_claim(ctx, denial.ref) is None


@pytest.mark.parametrize(
    "stated,denied",
    [
        (
            ("The TEST-project CI service is Jenkins.", "Jenkins"),
            ("The TEST-project CI service is not Jenkins any more.", "not Jenkins"),
        ),
        (
            ("TEST-project CI service 是 Jenkins。", "Jenkins"),
            ("TEST-project CI service 不是 Jenkins 了。", "不是 Jenkins"),
        ),
    ],
)
def test_a_denial_of_the_value_held_about_anything_but_the_person_ends_it(app, stated, denied):
    """(c) "Not Jenkins any more" ends Jenkins when it is said, as a retraction: the slot then holds no value, its
    history keeps Jenkins, and the denial is kept beside it as a proposal."""
    core, ctx = app
    held = _stated(core, ctx, *stated)
    ended = _stated(core, ctx, *denied, _LATER)
    assert (ended.ref, ended.state, ended.disposition) == (held.ref, "retracted", "retracted")
    assert core.current_claim(ctx, held.ref) is None
    assert core.current_claim(ctx, held.ref, as_of="2026-09-02T12:00:00Z").payload["value_text"] == "Jenkins"
    assert [(v.state, v.reason, v.payload["value_text"]) for v in core.claim_history(ctx, held.ref)] == [
        ("active", "explicit_scoped_source", "Jenkins"),
        ("retracted", "value_denied", "Jenkins"),
        ("proposed", "denial_not_a_value", denied[1]),
    ]


@pytest.mark.parametrize(
    "text,value,predicate",
    [
        ("我不吃辣。", "不吃辣", "饮食"),
        ("我没有车。", "没有车", "车"),
        ("I don't have a car.", "don't have a car", "car"),
    ],
)
def test_a_negative_self_report_is_the_person_s_fact(app, text, value, predicate):
    """(b) A fact about the person in their own words holds its negation, as their preference does."""
    core, ctx = app
    owner = replace(ctx, source_principal=_OWNER)
    fact = _stated(core, owner, text, value, predicate=predicate, subject="user")
    assert fact.state == "active"
    assert core.current_claim(owner, fact.ref).payload["value_text"] == value


def test_a_negative_self_report_replaces_the_fact_it_denies(app):
    """(c) The person's facts keep the four cases: their "don't have a car any more" replaces "have a car"."""
    core, ctx = app
    owner = replace(ctx, source_principal=_OWNER)
    held = _stated(core, owner, "I have a car.", "a car", predicate="have", subject="user")
    denial = _stated(
        core, owner, "I don't have a car any more.", "don't have a car", _LATER, predicate="have", subject="user"
    )
    assert held.state == "active"
    assert (denial.ref, denial.state, denial.disposition) == (held.ref, "active", "inserted")


@pytest.mark.parametrize(
    "text,predicate,condition",
    [
        ("要是值班表调整，TEST-project 告警频道是 ops-pager。", "告警频道", "值班表调整"),
        (
            "If the rotation changes, the TEST-project alert channel is ops-pager.",
            "alert channel",
            "the rotation changes",
        ),
    ],
)
def test_a_claim_said_under_a_condition_holds_only_with_it(app, text, predicate, condition):
    """Dropped, the condition would make ops-pager the alert channel now; kept, the claim is a slot of its own."""
    core, ctx = app
    bare = _stated(core, ctx, text, "ops-pager", predicate=predicate)
    assert (bare.state, core.claim_history(ctx, bare.ref)[-1].reason) == ("proposed", "condition_not_preserved")
    kept = _stated(core, ctx, text, "ops-pager", predicate=predicate, conditions=[condition])
    assert kept.state == "active" and kept.ref != bare.ref


def test_a_later_active_value_holds_in_place_of_the_value_it_replaced(app):
    core, ctx = app
    held = _stated(core, ctx, "The TEST-project CI service is Jenkins.", "Jenkins")
    moved = _stated(core, ctx, "The TEST-project CI service is GitHub Actions.", "GitHub Actions", _LATER)
    assert (moved.ref, moved.state, moved.disposition) == (held.ref, "active", "inserted")
    versions = core.claim_history(ctx, held.ref)
    assert holds_instead(versions, held.revision, Clock.now).revision == moved.revision
    assert holds_instead(versions, moved.revision, Clock.now) is None, "the value that holds is replaced by nothing"
    assert holds_instead(versions, held.revision, "2026-09-02T12:00:00Z") is None, "Jenkins held until the change"


def test_nothing_holds_in_place_of_a_value_said_again_disputed_or_only_proposed(app):
    core, ctx = app
    held = _stated(core, ctx, "The TEST-project CI service is Jenkins.", "Jenkins")
    again = _stated(core, ctx, "The TEST-project CI service is jenkins.", "jenkins", _LATER)
    assert again.revision > held.revision
    assert holds_instead(core.claim_history(ctx, held.ref), held.revision, Clock.now) is None
    guess = _stated(core, ctx, "我猜 TEST-project 验收 完成。", "完成", predicate="验收", statement_kind="hypothetical")
    later = _stated(
        core, ctx, "我猜 TEST-project 验收 失败。", "失败", _LATER, predicate="验收", statement_kind="hypothetical"
    )
    assert (guess.state, later.state, later.ref) == ("proposed", "proposed", guess.ref)
    assert holds_instead(core.claim_history(ctx, guess.ref), guess.revision, Clock.now) is None
    a = capture(core, ctx, "TEST-project 发布 完成。", origin="external_document", when=None)
    one = accept(core, ctx, draft(a, "完成", kind="fact", predicate="发布")).items[0]
    b = capture(core, ctx, "TEST-project 发布 失败。", origin="external_document", when=None)
    assert accept(core, ctx, draft(b, "失败", kind="fact", predicate="发布")).items[0].state == "disputed"
    assert holds_instead(core.claim_history(ctx, one.ref), one.revision, Clock.now) is None


def _version(state="active", **payload):
    """A version of TEST-project's CI service claim as the store holds it, stating Jenkins unless ``payload`` says
    otherwise."""
    body = {
        "kind": "fact",
        "subject": "TEST-project",
        "predicate": "CI service",
        "value_text": "Jenkins",
        "conditions": [],
        "statement_kind": "assertion",
        "valid_from": _SAID,
        "valid_to": None,
        "evidence_spans": [],
        **payload,
    }
    return ClaimVersion(
        ref="TEST-claim",
        revision=1,
        current_revision=1,
        scope_id="TEST-scope",
        project_id=None,
        branch_id=None,
        payload=body,
        state=state,
        basis="direct_report",
        reason="explicit_scoped_source",
        valid_from=_SAID,
        valid_to=None,
        recorded_from=_SAID,
        recorded_to=None,
        replaces_revision=None,
        conflict_revisions=(),
    )


@pytest.mark.parametrize(
    "version,states",
    [
        (_version(), True),
        # The fact gate could not prove it, and the sentence still said what the slot holds.
        (_version("proposed"), True),
        (_version("proposed", statement_kind="hypothetical"), False),
        (_version("proposed", statement_kind="quotation"), False),
        (_version("proposed", value_text="not CircleCI"), False),
        # The person's own denial, taken as their answer.
        (_version(kind="preference", subject="user", value_text="不吃辣"), True),
        (_version("retracted"), False),
        (_version(kind="procedure"), False),
    ],
)
def test_a_version_states_a_value_when_it_says_what_its_slot_holds(version, states):
    assert states_a_value(version) is states


def _denial_at_the_head(core, ctx, claim, text, value):
    """The denial in ``text``, said later, written over ``claim`` as its head and active, the slot's value."""
    denied = capture(core, ctx, text, when=_LATER)
    with core.storage.write(ctx) as tx:
        head = next(v for v in tx.claims.versions(claim.ref) if v.revision == v.current_revision)
        quoted = [dict(source_ref=denied.ref, source_revision=denied.revision, quote=text)]
        payload = {**head.payload, "value_text": value, "valid_from": denied.event["occurred_at"], "valid_to": None}
        verdict = Qualification("active", "direct_report", "explicit_scoped_source")
        tx.claims.append(
            "TEST-scope", {**payload, "evidence_spans": quoted}, verdict, recorded_at=Clock.now, previous=head
        )


def test_restore_withdraws_a_denial_that_is_no_value_and_gives_back_what_it_displaced(app):
    """A denial at its claim's head that is not its slot's value is withdrawn: one of another value, which gives back
    the value it displaced if that value held when the denial was written and holds still, and any about something
    other than the person.  The person's own denials are their values and stay.  The preview writes nothing; every
    version stays."""
    core, ctx = app
    jenkins = _stated(core, ctx, "The TEST-project CI service is Jenkins.", "Jenkins")
    _denial_at_the_head(core, ctx, jenkins, "The TEST-project CI service is not CircleCI.", "not CircleCI")
    region = _stated(
        core,
        ctx,
        "The TEST-project deploy region is us-east-1 until 2026-09-02.",
        "us-east-1",
        predicate="deploy region",
        valid_to="2026-09-02T00:00:00Z",
    )
    _denial_at_the_head(core, ctx, region, "The TEST-project deploy region is not us-west-2.", "not us-west-2")
    bucket = _stated(core, ctx, "The TEST-project backup bucket is vault-1.", "vault-1", predicate="backup bucket")
    _denial_at_the_head(core, ctx, bucket, "The TEST-project backup bucket is not vault-1 any more.", "not vault-1")
    alone, owner = _preferred(core, ctx, "我不吃辣。", "不吃辣")
    spicy, _owner = _preferred(core, ctx, "我爱吃辣。", "爱吃辣", predicate="口味")
    _preferred(core, ctx, "我现在不吃辣了。", "不吃辣", _LATER, predicate="口味")
    assert {region.state, alone.state, spicy.state} == {"active"}
    before = core.storage.path.read_bytes()
    preview = core.operations.restore_displaced_values(ctx, limit=32)
    assert {item["ref"]: item["restores"] for item in preview["changed"]} == {
        jenkins.ref: 1,
        region.ref: None,
        bucket.ref: None,
    }
    assert {
        item["ref"]: (item["subject"], item["predicate"], item["value"], item["gives_back"])
        for item in preview["changed"]
    } == {
        jenkins.ref: ("TEST-project", "CI service", "not CircleCI", "Jenkins"),
        region.ref: ("TEST-project", "deploy region", "not us-west-2", None),
        bucket.ref: ("TEST-project", "backup bucket", "not vault-1", None),
    }
    assert core.storage.path.read_bytes() == before
    assert len(core.operations.restore_displaced_values(ctx, limit=32, dry_run=False)["changed"]) == 3
    assert core.current_claim(ctx, jenkins.ref).payload["value_text"] == "Jenkins"
    assert core.current_claim(ctx, jenkins.ref, as_of="2026-09-05T00:00:00Z").payload["value_text"] == "Jenkins"
    assert [v.state for v in core.claim_history(ctx, jenkins.ref)] == ["active", "active", "retracted", "active"]
    # The expired region is not re-instated, and the denial of a bucket the project no longer uses leaves none.
    assert core.current_claim(ctx, region.ref) is None
    assert [v.payload["value_text"] for v in core.claim_history(ctx, region.ref)] == [
        "us-east-1",
        *["not us-west-2"] * 2,
    ]
    assert core.current_claim(ctx, bucket.ref) is None
    assert core.current_claim(ctx, bucket.ref, as_of="2026-09-02T12:00:00Z").payload["value_text"] == "vault-1"
    assert core.current_claim(owner, alone.ref).payload["value_text"] == "不吃辣"
    assert core.current_claim(owner, spicy.ref).payload["value_text"] == "不吃辣"
    assert core.operations.restore_displaced_values(ctx, limit=32, dry_run=False)["changed"] == []


@pytest.mark.parametrize("command", ["restore_displaced_values", "requalify_claims"])
def test_a_denial_over_denials_gives_back_the_nearest_value_under_them(app, command):
    """Denials an older rule stacked on a slot displaced only what was a value: "not Travis" over "not CircleCI" over
    Jenkins gives back Jenkins, and "not Jenkins" over "not CircleCI" alone gives back nothing; a second run finds
    nothing left to do."""
    core, ctx = app
    jenkins = _stated(core, ctx, "The TEST-project CI service is Jenkins.", "Jenkins")
    _denial_at_the_head(core, ctx, jenkins, "The TEST-project CI service is not CircleCI.", "not CircleCI")
    _denial_at_the_head(core, ctx, jenkins, "The TEST-project CI service is not Travis.", "not Travis")
    first = capture(core, ctx, "The TEST-project build service is not CircleCI.")
    with core.storage.write(ctx) as tx:
        verdict = Qualification("active", "direct_report", "explicit_scoped_source")
        payload = draft(first, "not CircleCI", kind="fact", predicate="build service", statement_kind="assertion")
        build = tx.claims.append("TEST-scope", payload, verdict, recorded_at=Clock.now)
    _denial_at_the_head(core, ctx, build, "The TEST-project build service is not Jenkins.", "not Jenkins")
    run = getattr(core.operations, command)
    assert {item["ref"]: item["restores"] for item in run(ctx, limit=32, dry_run=False)["changed"]} == {
        jenkins.ref: 1,
        build.ref: None,
    }
    assert core.current_claim(ctx, jenkins.ref).payload["value_text"] == "Jenkins"
    assert core.current_claim(ctx, build.ref) is None
    assert run(ctx, limit=32, dry_run=False)["changed"] == []


def test_requalify_keeps_the_person_s_own_claims_as_they_were(app):
    """The person's own claims are stored under their principal, bound from their 我 when written: re-judging binds
    them the same way and changes none, nor does restoring displaced values."""
    core, ctx = app
    preference, owner = _preferred(core, ctx, "我爱吃辣。", "爱吃辣")
    fact = _stated(core, owner, "I have a car.", "a car", predicate="have", subject="user")
    denial = _stated(core, owner, "我没有车。", "没有车", predicate="车", subject="user")
    refs = (preference.ref, fact.ref, denial.ref)
    before = [core.claim_history(owner, ref) for ref in refs]
    assert [version[-1].state for version in before] == ["active"] * 3
    report = core.operations.requalify_claims(ctx, limit=32, dry_run=False)
    assert (report["changed"], report["skipped"], report["examined"]) == ([], [], 3)
    assert core.operations.restore_displaced_values(ctx, limit=32, dry_run=False)["changed"] == []
    assert [core.claim_history(owner, ref) for ref in refs] == before


def test_requalify_reads_a_denial_at_the_head_as_writing_does(app):
    """Re-judging the store settles a denial as writing would (``mutate.denial_verdict``): one an older rule made the
    head of TEST-project's CI service is withdrawn and Jenkins, which it displaced, comes back; one that became the
    deploy region's only value is withdrawn and leaves none; and one the text gate now proves stays a proposal."""
    core, ctx = app
    jenkins = _stated(core, ctx, "The TEST-project CI service is Jenkins.", "Jenkins")
    _denial_at_the_head(core, ctx, jenkins, "The TEST-project CI service is not CircleCI.", "not CircleCI")
    region = capture(core, ctx, "The TEST-project deploy region is not us-east-2.", when=_LATER)
    bucket = capture(core, ctx, "The TEST-project backup bucket is not vault-1.", when=_LATER)
    alone = draft(region, "not us-east-2", kind="fact", predicate="deploy region", statement_kind="assertion")
    unproved = draft(bucket, "not vault-1", kind="fact", predicate="backup bucket", statement_kind="assertion")
    with core.storage.write(ctx) as tx:
        verdict = Qualification("active", "direct_report", "explicit_scoped_source")
        alone_ref = tx.claims.append("TEST-scope", alone, verdict, recorded_at=Clock.now).ref
        refused = Qualification("proposed", "inferred_suggestion", "fact_entailment_unproved")
        bucket_ref = tx.claims.append("TEST-scope", unproved, refused, recorded_at=Clock.now).ref
    report = core.operations.requalify_claims(ctx, limit=32, dry_run=False)
    assert {item["ref"]: item["now"] for item in report["changed"]} == {
        jenkins.ref: "retracted:denial_of_another_value",
        alone_ref: "retracted:denial_not_a_value",
        bucket_ref: "proposed:denial_not_a_value",
    }
    assert core.current_claim(ctx, jenkins.ref).payload["value_text"] == "Jenkins"
    assert core.current_claim(ctx, alone_ref) is None and core.current_claim(ctx, bucket_ref) is None
    assert core.operations.requalify_claims(ctx, limit=32, dry_run=False)["changed"] == []


@pytest.mark.parametrize(
    "stated,correction,value,reason",
    [
        (
            ("TEST-project CI service 是 Jenkins。", "Jenkins"),
            "更正：TEST-project CI service 不是 CircleCI。",
            "不是 CircleCI",
            "denial_of_another_value",
        ),
        (
            ("TEST-project CI service 是 Jenkins。", "Jenkins"),
            "更正：TEST-project CI service 不是 Jenkins 了。",
            "不是 Jenkins",
            "denial_not_a_value",
        ),
        (
            ("TEST-project CI service 不是 CircleCI。", "不是 CircleCI"),
            "更正：TEST-project CI service 不是 CircleCI。",
            "不是 CircleCI",
            "denial_not_a_value",
        ),
    ],
)
def test_a_revision_never_makes_a_denial_about_anything_but_the_person_a_value(app, stated, correction, value, reason):
    """A denial about anything but the person is no value of its slot (``mutate.denial_verdict``), and no revision makes
    it one: correcting TEST-project's CI service to 不是 CircleCI or to 不是 Jenkins 了, or a denial kept a proposal to
    itself, is refused, and the claim stays as it was.  The refusal tells the model what it can do instead."""
    core, ctx = app
    claim = _stated(core, ctx, *stated)
    before = core.claim_history(ctx, claim.ref)
    source = capture(core, ctx, correction, when=_LATER)
    with pytest.raises(ContractError, match=reason) as refused:
        core.revise(ctx, revise_request(claim, source, value), remaining_seconds=10)
    assert "give the value that holds" in str(refused.value) and "new_value null" in str(refused.value)
    assert core.claim_history(ctx, claim.ref) == before


def test_a_revision_to_the_person_s_own_denial_replaces_what_it_denies(app):
    """The person's own 不吃辣 corrects their 爱吃辣, as saying it replaces it."""
    core, ctx = app
    held, owner = _preferred(core, ctx, "我爱吃辣。", "爱吃辣")
    source = capture(core, owner, "更正：饮食上我现在不吃辣了。", when=_LATER)
    assert core.revise(owner, revise_request(held, source, "不吃辣"), remaining_seconds=10).items[0].state == "active"
    assert core.current_claim(owner, held.ref).payload["value_text"] == "不吃辣"


def test_a_revision_uses_past_values_to_recognize_an_unmarked_target(app):
    """A correction cannot install a denial of an unmarked historical value as the project's current value."""
    core, ctx = app
    _stated(core, ctx, "The TEST-project CI service is bash.", "bash")
    current = _stated(core, ctx, "The TEST-project CI service is Redis.", "Redis", _LATER)
    before = core.claim_history(ctx, current.ref)
    source = capture(core, ctx, "更正：TEST-project CI service is not bash.", when="2026-09-04T12:00:00Z")
    with pytest.raises(ContractError, match="denial_of_another_value"):
        core.revise(ctx, revise_request(current, source, "not bash"), remaining_seconds=10)
    assert core.claim_history(ctx, current.ref) == before


def test_negative_state_transition_is_preserved_across_lifecycle(app):
    """Applying or revising to a negative state preserves it, and maintenance leaves it active and idempotent."""
    core, ctx = app
    initial_en = _stated(core, ctx, "The TEST-project host status is online.", "online", predicate="host status")
    initial_zh = _stated(core, ctx, "TEST-project 服务状态 在线。", "在线", predicate="服务状态")
    transition_en = _stated(
        core, ctx, "The TEST-project host status is not online.", "not online", _LATER, predicate="host status"
    )
    transition_zh = _stated(core, ctx, "TEST-project 服务状态 不在线。", "不在线", _LATER, predicate="服务状态")
    assert transition_en.state == "active" and transition_en.disposition == "inserted"
    assert core.current_claim(ctx, initial_en.ref).payload["value_text"] == "not online"
    assert transition_zh.state == "active" and transition_zh.disposition == "inserted"
    assert core.current_claim(ctx, initial_zh.ref).payload["value_text"] == "不在线"

    conf = _stated(core, ctx, "The TEST-project deploy status is configured.", "configured", predicate="deploy status")
    source_en = capture(core, ctx, "更正：TEST-project deploy status is not Configured.", when="2026-09-04T12:00:00Z")
    revised_en = core.revise(ctx, revise_request(conf, source_en, "not Configured"), remaining_seconds=10)
    assert revised_en.items[0].state == "active"
    assert core.current_claim(ctx, conf.ref).payload["value_text"] == "not Configured"

    def_zh = _stated(core, ctx, "TEST-project 设置状态 默认的。", "默认的", predicate="设置状态")
    source_zh = capture(core, ctx, "更正：TEST-project 设置状态 不是默认的。", when="2026-09-04T12:00:00Z")
    revised_zh = core.revise(ctx, revise_request(def_zh, source_zh, "不是默认的"), remaining_seconds=10)
    assert revised_zh.items[0].state == "active"
    assert core.current_claim(ctx, def_zh.ref).payload["value_text"] == "不是默认的"

    for command in ("requalify_claims", "restore_displaced_values"):
        assert getattr(core.operations, command)(ctx, limit=32, dry_run=False)["changed"] == []
        assert getattr(core.operations, command)(ctx, limit=32, dry_run=False)["changed"] == []

    assert core.current_claim(ctx, initial_en.ref).payload["value_text"] == "not online"
    assert core.current_claim(ctx, initial_zh.ref).payload["value_text"] == "不在线"
    assert core.current_claim(ctx, conf.ref).payload["value_text"] == "not Configured"
    assert core.current_claim(ctx, def_zh.ref).payload["value_text"] == "不是默认的"

    jenkins = _stated(core, ctx, "The TEST-project CI service is Jenkins.", "Jenkins")
    denial_circle = _stated(core, ctx, "The TEST-project CI service is not CircleCI.", "not CircleCI", _LATER)
    assert denial_circle.state == "proposed"
    assert core.current_claim(ctx, jenkins.ref).payload["value_text"] == "Jenkins"
    denial_jenkins = _stated(
        core, ctx, "The TEST-project CI service is not Jenkins any more.", "not Jenkins", "2026-09-05T12:00:00Z"
    )
    assert denial_jenkins.state == "retracted"
    assert core.current_claim(ctx, jenkins.ref) is None


@pytest.mark.parametrize("origin", ["assistant_visible", "host_generated", "memory_reinjection", "origin_unknown"])
def test_C13_derived_echoes_have_one_root_without_new_authority_or_literal_laundering(app, origin):
    core, ctx = app
    item, source = initial(core, ctx)
    echo = capture(core, ctx, "TEST-project 配色 银色。", origin=origin, evidence_refs=[f"{source.ref}@1"])
    with core.storage.read(ctx) as tx:
        roots = tx.claims.roots((f"{source.ref}@1", f"{echo.ref}@1"))
    assert [(r.ref, r.revision) for r in roots] == [(source.ref, 1)]
    assert accept(core, ctx, draft(echo, "银色")).items[0].state == "proposed"
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "蓝色"


def test_C15_assistant_success_does_not_override_tool_failure(app):
    core, ctx = app
    failure = capture(core, ctx, "TEST-project 测试 失败。", origin="tool_observation")
    item = accept(core, ctx, draft(failure, "失败", kind="fact", predicate="测试")).items[0]
    success = capture(core, ctx, "TEST-project 测试 通过。", origin="assistant_visible")
    assert accept(core, ctx, draft(success, "通过", kind="fact", predicate="测试")).items[0].state == "proposed"
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "失败"


def test_imported_role_and_original_label_require_runtime_manifest_attestation(app):
    core, ctx = app
    forged = capture(core, ctx, "TEST-project 配色 蓝色。", origin="imported", source_original_origin="human_direct")
    assert accept(core, ctx, draft(forged)).items[0].state == "proposed"
    trusted = capture(
        core, ctx, "TEST-project 配色 银色。", origin="imported", source_original_origin="human_direct", attested=True
    )
    assert accept(core, ctx, draft(trusted, "银色")).items[0].state == "active"
    assert core.source(ctx, trusted.ref, 1).import_provenance_sha256 == "a" * 64


@pytest.mark.parametrize("state", ["partial", "gap"])
def test_incomplete_root_cannot_promote(app, state):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 配色 蓝色。", capture_state=state)
    assert accept(core, ctx, draft(source)).items[0].state == "proposed"


def test_quote_trimming_cannot_remove_hypothesis_or_negation(app):
    core, ctx = app
    source = capture(core, ctx, "假设TEST-project 配色 蓝色。")
    p = draft(source)
    p["evidence_spans"][0]["quote"] = "TEST-project 配色 蓝色"
    assert accept(core, ctx, p).items[0].state == "proposed"


def test_C36_ambiguous_correction_preserves_both_currents_and_raw_update(app):
    core, ctx = app
    one, _ = initial(core, ctx)
    two_source = capture(core, ctx, "TEST-project 字号 16。")
    two = accept(core, ctx, draft(two_source, "16", predicate="字号")).items[0]
    correction = capture(core, ctx, "那个不对，改一下。", when="2026-09-03T12:00:00Z")
    pending = core.records.unresolved_updates(ctx)
    assert len(pending) == 1 and set(pending[0]["candidate_refs"]) == {one.ref, two.ref}
    assert pending[0]["source_ref"] == correction.ref
    assert core.current_claim(ctx, one.ref).revision == core.current_claim(ctx, two.ref).revision == 1


def test_ambiguous_update_closes_once_the_user_settles_it_on_one_candidate(app):
    core, ctx = app
    one, one_source = initial(core, ctx)
    two_source = capture(core, ctx, "TEST-project 字号 16。")
    two = accept(core, ctx, draft(two_source, "16", predicate="字号")).items[0]
    capture(core, ctx, "那个不对，改一下。", when="2026-09-03T12:00:00Z")
    assert len(core.records.unresolved_updates(ctx)) == 1
    # The user names which one they meant.  ``resolved`` had no writer at all
    # before this, so the row stayed open forever and kept being handed to the
    # host on every read.
    settle = capture(core, ctx, "TEST-project 配色改为银色。", when="2026-09-04T12:00:00Z")
    assert core.current_claim(ctx, one.ref).payload["value_text"] == "银色"
    assert core.records.unresolved_updates(ctx) == ()
    with sqlite3.connect(core.storage.path) as conn:
        state, resolved_at = conn.execute("SELECT state,resolved_at FROM unresolved_updates").fetchone()
    assert state == "resolved" and resolved_at
    # The untouched candidate is left exactly as it was; settling one ambiguity
    # is not a licence to revise the other.
    assert core.current_claim(ctx, two.ref).revision == 1


def test_a_correction_with_no_claim_to_place_it_against_keeps_no_open_row(app):
    """A row naming no candidate can never be closed (``resolve_updates`` needs one revised), so none is kept open:
    it would be handed to every read for good.  The message itself is stored like any other."""
    core, ctx = app
    said = capture(core, ctx, "那个不对，改一下。", when="2026-09-03T12:00:00Z")
    assert core.records.unresolved_updates(ctx) == ()
    assert core.source(ctx, said.ref, 1) is not None
    # What an older release left: such a row, closed by the worker's pass.
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute(
            """INSERT INTO unresolved_updates(update_id,source_ref,source_revision,scope_id,project_id,branch_id,
            candidate_refs_json,created_at) VALUES ('update-TEST-legacy',?,1,'TEST-scope',?,?,'[]','2026-09-03T12:00:00Z')""",
            (said.ref, ctx.project_id, ctx.branch_id),
        )
        conn.commit()
    assert len(core.records.unresolved_updates(ctx)) == 1
    core.drain_worker(ctx, max_items=8, remaining_seconds=10)
    assert core.records.unresolved_updates(ctx) == ()
    with core.storage.write(ctx) as tx:
        assert tx.claims.close_unplaceable_updates() == 0, "closed once"


def test_ambiguity_the_user_never_settled_is_still_reported(app):
    core, ctx = app
    initial(core, ctx)
    capture(core, ctx, "TEST-project 字号 16。")
    capture(core, ctx, "那个不对，改一下。", when="2026-09-03T12:00:00Z")
    open_before = {row["ref"] for row in core.records.unresolved_updates(ctx)}
    assert open_before
    # An unrelated later capture settles nothing, so the question the host still
    # has to ask stays on the list.  Closing rows is tied to an authorized
    # revision of a named candidate, not to time passing.
    capture(core, ctx, "今天天气不错。", when="2026-09-04T12:00:00Z")
    assert open_before <= {row["ref"] for row in core.records.unresolved_updates(ctx)}


def test_explicit_revision_compare_and_swap_and_no_silent_retries(app):
    core, ctx = app
    item, _ = initial(core, ctx)
    source = capture(core, ctx, "Please correct TEST-project 配色: 银色。", when="2026-09-03T12:00:00Z")
    request = revise_request(item, source)
    result = core.revise(ctx, request, remaining_seconds=10)
    assert result.items[0].state == "active" and result.items[0].revision == 2
    before = core.storage.path.read_bytes()
    with pytest.raises(ContractError, match="VERSION_CONFLICT"):
        core.revise(ctx, request, remaining_seconds=10)
    assert core.storage.path.read_bytes() == before


def test_two_real_writers_with_same_expected_revision_have_exactly_one_winner(app):
    core, ctx = app
    item, _ = initial(core, ctx)
    source = capture(core, ctx, "Please correct TEST-project 配色: 银色。", when="2026-09-03T12:00:00Z")
    request = revise_request(item, source)
    barrier = Barrier(2)

    def write():
        barrier.wait()
        try:
            return core.revise(ctx, request, remaining_seconds=5).items[0].revision
        except Exception as exc:
            # Threads share the process's writer lease; SQLite can reject the loser before revision checking.
            if (
                isinstance(exc, sqlite3.OperationalError)
                and getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_BUSY
            ):
                return "SQLITE_BUSY"
            return getattr(exc, "code", type(exc).__name__)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: write(), range(2)))
    assert results.count(2) == 1 and len(core.claim_history(ctx, item.ref)) == 2, results
    assert any(x in {"VERSION_CONFLICT", "TruthWriterBusyError", "SQLITE_BUSY"} for x in results if x != 2), results


def test_old_human_quote_and_agent_echo_cannot_authorize_new_revision(app):
    core, ctx = app
    item, _ = initial(core, ctx)
    source = capture(core, ctx, "Please correct TEST-project 配色: 银色。", when="2026-09-03T12:00:00Z")
    capture(core, ctx, "现在讨论另一件事情。", when="2026-09-04T12:00:00Z")
    with pytest.raises(ContractError, match="current_human_evidence"):
        core.revise(ctx, revise_request(item, source))
    assert core.current_claim(ctx, item.ref).revision == 1


def test_retraction_blocks_current_but_keeps_history_and_past(app):
    core, ctx = app
    item, _ = initial(core, ctx)
    capture(core, ctx, "撤回TEST-project 配色蓝色。", when="2026-09-03T12:00:00Z")
    assert core.current_claim(ctx, item.ref) is None
    assert core.claim_history(ctx, item.ref)[-1].state == "retracted"
    assert core.current_claim(ctx, item.ref, as_of="2026-09-02T12:00:00Z").payload["value_text"] == "蓝色"
    old = capture(
        core, ctx, "TEST-project 配色 蓝色。", origin="imported", source_original_origin="human_direct", attested=True
    )
    assert accept(core, ctx, draft(old)).items[0].disposition == "duplicate"
    assert core.current_claim(ctx, item.ref) is None


def test_explicit_condition_variant_does_not_change_base(app):
    core, ctx = app
    item, _ = initial(core, ctx)
    source = capture(core, ctx, "Please correct TEST-project 配色: 银色，只限TEST沙箱。", when="2026-09-03T12:00:00Z")
    result = core.revise(ctx, revise_request(item, source, conditions=["TEST沙箱"]), remaining_seconds=10)
    assert result.items[0].ref != item.ref
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "蓝色"


def test_stale_source_revision_is_fenced_before_any_derived_write(app):
    core, ctx = app
    old = capture(core, ctx, "TEST-project 配色 蓝色。", key="TEST-edited")
    capture(core, ctx, "TEST-project 配色 银色。", key="TEST-edited", revision=2)
    before = core.storage.path.read_bytes()
    with pytest.raises(ContractError, match="VERSION_CONFLICT"):
        accept(core, ctx, draft(old))
    assert core.storage.path.read_bytes() == before


def test_batch_invalid_span_rolls_back_other_valid_proposals(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 配色 蓝色。")
    bad = draft(source, "银色")
    bad["evidence_spans"][0]["quote"] = "This was never said"
    before = core.storage.path.read_bytes()
    with pytest.raises(ContractError, match="DERIVATION_INVALID"):
        accept(core, ctx, draft(source), bad)
    assert core.storage.path.read_bytes() == before and counts(core)["claims"] == 0


def test_sql_foreign_key_enforces_one_existing_head(app):
    core, ctx = app
    item, _ = initial(core, ctx)
    with sqlite3.connect(core.storage.path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("UPDATE claims SET current_revision=999 WHERE claim_id=?", (item.ref,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.commit()
        conn.rollback()
    assert core.current_claim(ctx, item.ref).revision == 1


def intention(source, state="pending"):
    return draft(
        source,
        "提醒检查散热",
        kind="intention",
        predicate="散热检查",
        intention=dict(
            cue="验收前",
            target="检查散热",
            conditions=["TEST-project 验收"],
            state=state,
            state_evidence_refs=[f"{source.ref}@{source.revision}"],
        ),
    )


def test_M38_intention_cancellation_uses_same_versions_and_does_not_erase_history(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 验收前提醒检查散热。")
    one = accept(core, ctx, intention(source)).items[0]
    cancel = capture(core, ctx, "TEST-project 散热检查约定取消了。", when="2026-09-03T12:00:00Z")
    two = accept(core, ctx, intention(cancel, "cancelled")).items[0]
    assert one.ref == two.ref and two.revision == 2 and two.state == "active"
    assert core.current_claim(ctx, one.ref).payload["intention"]["state"] == "cancelled"
    assert core.claim_history(ctx, one.ref)[0].payload["intention"]["state"] == "pending"


@pytest.mark.parametrize(
    "origin,text",
    [
        ("assistant_visible", "TEST-project 提醒你检查散热。"),
        ("human_direct", "TEST-project 已提醒，但尚未完成散热检查。"),
    ],
)
def test_M40_reminder_does_not_complete_intention(app, origin, text):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 验收前提醒检查散热。")
    item = accept(core, ctx, intention(source)).items[0]
    reminder = capture(core, ctx, text, origin=origin, when="2026-09-03T12:00:00Z")
    attempt = accept(core, ctx, intention(reminder, "completed")).items[0]
    assert attempt.state == "proposed" and core.current_claim(ctx, item.ref).payload["intention"]["state"] == "pending"


def test_a_cancellation_or_an_acceptance_said_under_a_condition_proves_nothing(app):
    core, ctx = app
    item = accept(core, ctx, intention(capture(core, ctx, "TEST-project 验收前提醒检查散热。"))).items[0]
    cancel = capture(core, ctx, "要是验收提前，TEST-project 散热检查约定就取消。", when="2026-09-03T12:00:00Z")
    assert accept(core, ctx, intention(cancel, "cancelled")).items[0].state == "proposed"
    assert core.current_claim(ctx, item.ref).payload["intention"]["state"] == "pending"
    source = capture(core, ctx, "要是复测通过，TEST-project 验收前检查方法就认可，先检查温度，再验收。")
    procedure = draft(
        source,
        "检查方法",
        kind="procedure",
        predicate="验收方法",
        procedure=dict(
            conditions=["验收前"],
            non_applicable=[],
            method=["先检查温度", "再验收"],
            verification_basis="user_accepted",
            counterexample_refs=[],
        ),
    )
    assert accept(core, ctx, procedure).items[0].state == "proposed"


def test_intention_actual_human_completion_and_procedure_evidence_are_qualified(app):
    core, ctx = app
    completed = capture(core, ctx, "TEST-project 散热检查已完成。")
    assert accept(core, ctx, intention(completed, "completed")).items[0].state == "active"
    source = capture(core, ctx, "TEST-project 验收前检查方法已认可，先检查温度，再验收。未安装设备时不适用。")
    procedure = draft(
        source,
        "检查方法",
        kind="procedure",
        predicate="验收方法",
        procedure=dict(
            conditions=["验收前"],
            non_applicable=["未安装设备"],
            method=["先检查温度", "再验收"],
            verification_basis="user_accepted",
            counterexample_refs=[],
        ),
    )
    item = accept(core, ctx, procedure).items[0]
    assert item.state == "active" and core.current_claim(ctx, item.ref).payload["procedure"]["non_applicable"] == [
        "未安装设备"
    ]


def test_import_attestation_is_not_a_model_request_field(app):
    core, ctx = app
    item, source = initial(core, ctx)
    request = revise_request(item, source)
    request["import_provenance"] = {"original_origin": "human_direct", "manifest_sha256": "a" * 64}
    with pytest.raises(ContractError, match="INPUT_INVALID"):
        core.revise(ctx, request)


@pytest.mark.parametrize(
    "raw", ["不要撤回TEST-project 配色蓝色。", "TEST-project 配色改为银色？", "不要把TEST-project 配色改成银色。"]
)
def test_negative_and_questioned_operations_do_not_change_current(app, raw):
    core, ctx = app
    item, _ = initial(core, ctx)
    source = capture(core, ctx, raw, when="2026-09-03T12:00:00Z")
    assert core.current_claim(ctx, item.ref).revision == 1
    with pytest.raises(ContractError, match="revision_not_asserted"):
        core.revise(ctx, revise_request(item, source, None if "撤回" in raw else "银色"))


@pytest.mark.parametrize(
    "raw,value",
    [
        # Said under a condition or a supposition: what would change, not what did.
        ("要是值班表调整，TEST-project 配色就会改成 #A1。", "#A1"),
        ("万一预算削减，TEST-project 配色换成银色。", "银色"),
        ("预算削减的话，TEST-project 配色换成银色。", "银色"),
        ("If the budget is cut, TEST-project 配色 would switch to 银色.", "银色"),
        # Asked without a question mark.
        ("TEST-project 配色换成银色怎么样", "银色"),
        ("Did TEST-project 配色 change to 银色", "银色"),
        # Someone else's words, or quoted text.
        ("张三说：TEST-project 配色换成银色。", "银色"),
        ("张三说 TEST-project 配色换成银色了。", "银色"),
        ("张三：TEST-project 配色换成银色。", "银色"),
        ("张三表示 TEST-project 配色换成银色。", "银色"),
        ("张三称 TEST-project 配色换成银色。", "银色"),
        ("张三提到 TEST-project 配色换成银色了。", "银色"),
        ("张三认为 TEST-project 配色应该换成银色。", "银色"),
        ("NameOne: TEST-project 配色 should change to 银色.", "银色"),
        ("NameOne claims TEST-project 配色 should switch to 银色.", "银色"),
        ("@nameone: TEST-project 配色改成银色。", "银色"),
        ("NameOne (PM): TEST-project 配色改成银色。", "银色"),
        ("欧阳明明：TEST-project 配色换成银色。", "银色"),
        ("司马懿：TEST-project 配色换成银色。", "银色"),
        ("> TEST-project 配色改成银色。", "银色"),
        ("The style guide says “TEST-project 配色改成银色”.", "银色"),
    ],
)
def test_a_change_the_person_does_not_assert_corrects_nothing(app, raw, value):
    core, ctx = app
    item, _ = initial(core, ctx)
    source = capture(core, ctx, raw, when="2026-09-03T12:00:00Z")
    current = core.current_claim(ctx, item.ref)
    assert (current.revision, current.payload["value_text"]) == (1, "蓝色")
    with pytest.raises(ContractError, match="revision_not_asserted"):
        core.revise(ctx, revise_request(item, source, value))
    assert core.current_claim(ctx, item.ref).revision == 1


@pytest.mark.parametrize(
    "raw",
    [
        "TEST-project 配色还是改成银色。",
        "I would like TEST-project 配色 to switch to 银色.",
        "我表示 TEST-project 配色换成银色。",
        "更正：TEST-project 配色换成银色。",
        "INFO: TEST-project 配色改成银色。",
        "WARN: TEST-project 配色改成银色。",
        "ERROR: TEST-project 配色改成银色。",
        "DEBUG: TEST-project 配色改成银色。",
        "status: TEST-project 配色改成银色。",
        "value: TEST-project 配色改成银色。",
    ],
)
def test_a_plain_correction_takes_the_value_its_replacement_names(app, raw):
    """In 还是改成银色 the looser replacement word 是 stands too; the value is the word 改成 names.  The person's own
    我表示, a heading of theirs and a log's or a key's prefix give their words to no one else."""
    core, ctx = app
    item, _ = initial(core, ctx)
    capture(core, ctx, raw, when="2026-09-03T12:00:00Z")
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "银色"


@pytest.mark.parametrize(
    "held,raw,value",
    [
        ("30 天", "TEST-project 配色改成 90 天了。", "90 天"),
        ("1 GB", "TEST-project 配色换成 2 GB。", "2 GB"),
        ("10 分钟", "TEST-project 配色调整为 30 分钟。", "30 分钟"),
        ("30 days", "TEST-project 配色: change to 90 days.", "90 days"),
        ("#night-shift", "TEST-project 配色换成 #ops-pager。", "#ops-pager"),
    ],
)
def test_a_correction_takes_the_whole_value_it_names(app, held, raw, value):
    """A number with its unit (90 天, 2 GB), or a word opening with the mark a channel's name carries (#ops-pager), is
    all of the value."""
    core, ctx = app
    item, _ = initial(core, ctx, value=held)
    capture(core, ctx, raw, when="2026-09-03T12:00:00Z")
    assert core.current_claim(ctx, item.ref).payload["value_text"] == value


@pytest.mark.parametrize(
    "raw", ["TEST-project 配色改成 Jenkins 来跑 CI。", "TEST-project 配色: switch to Jenkins for the nightly runs."]
)
def test_a_replacement_that_names_more_than_a_value_is_left_for_the_person_to_settle(app, raw):
    """A replacement that may say more than which value ("Jenkins 来跑 CI") leaves the claim its value, and the change
    waits, unresolved."""
    core, ctx = app
    item, _ = initial(core, ctx)
    source = capture(core, ctx, raw, when="2026-09-03T12:00:00Z")
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "蓝色"
    assert core.records.unresolved_updates(ctx)[0]["source_ref"] == source.ref


@pytest.mark.parametrize(
    "raw,state",
    [
        ("张三：TEST-project 配色是银色。", "proposed"),
        ("张三：\nTEST-project 配色是银色。", "proposed"),
        ("张三表示 TEST-project 配色是银色。", "proposed"),
        ("我表示 TEST-project 配色是银色。", "active"),
        ("更正：TEST-project 配色是银色。", "active"),
        ("库里只有这一句：TEST-project 配色是银色。", "active"),
        ("记住：TEST-project 配色是银色。", "active"),
        ("Remember: TEST-project 配色 is 银色.", "active"),
        ("NameOne (PM): TEST-project 配色是银色。", "proposed"),
        ("@nameone: TEST-project 配色是银色。", "proposed"),
        ("欧阳明明：TEST-project 配色是银色。", "proposed"),
        ("NameOne: I prefer TEST-project 配色 银色.", "proposed"),
        ("INFO: TEST-project 配色是银色。", "active"),
        ("status: TEST-project 配色是银色。", "active"),
        ("Value: TEST-project 配色是银色。", "active"),
    ],
)
def test_a_claim_in_words_given_to_someone_else_is_not_the_person_s(app, raw, state):
    """Qualification gives words to a speaker as the correction path does (``attributed_to_another``)."""
    core, ctx = app
    item = accept(core, ctx, draft(capture(core, ctx, raw), "银色")).items[0]
    reason = core.claim_history(ctx, item.ref)[-1].reason
    assert (item.state, reason) == (state, "other_speaker" if state == "proposed" else "explicit_scoped_source")


def test_a_labelled_third_party_neither_ends_nor_retracts_a_value(app):
    """NameOne (PM)'s "not Jenkins any more" is her word, not the person's, so Jenkins stays; nor does her 不再使用
    retract anything."""
    core, ctx = app
    jenkins = _stated(core, ctx, "The TEST-project CI service is Jenkins.", "Jenkins")
    text = "NameOne (PM): The TEST-project CI service is not Jenkins any more."
    denial = _stated(core, ctx, text, "not Jenkins", _LATER)
    assert (denial.state, core.claim_history(ctx, jenkins.ref)[-1].reason) == ("proposed", "other_speaker")
    assert core.current_claim(ctx, jenkins.ref).payload["value_text"] == "Jenkins"
    item, _ = initial(core, ctx)
    capture(core, ctx, "NameOne (PM): 不再使用 TEST-project 配色蓝色。", when=_LATER)
    current = core.current_claim(ctx, item.ref)
    assert current is not None and current.payload["value_text"] == "蓝色"


def test_a_correction_naming_its_claim_by_ref_applies(app):
    """A claim's ref (claim-…) is the surest name a person gives it; its "claim" is no one else's report."""
    core, ctx = app
    item, _ = initial(core, ctx)
    capture(core, ctx, f"TEST-project 配色这条 {item.ref} 写错了，改为银色。", when="2026-09-03T12:00:00Z")
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "银色"


@pytest.mark.parametrize(
    "raw",
    [
        "TEST-project 配色下周换成银色。",
        "TEST-project 配色会改成银色。",
        "TEST-project 配色 will switch to 银色 next week.",
    ],
)
def test_a_change_said_for_later_is_left_for_the_person_to_settle(app, raw):
    """Capture dates a correction to its message; a change said for later keeps the raw update instead."""
    core, ctx = app
    item, _ = initial(core, ctx)
    source = capture(core, ctx, raw, when="2026-09-03T12:00:00Z")
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "蓝色"
    assert core.records.unresolved_updates(ctx)[0]["source_ref"] == source.ref


def test_question_marked_preference_source_cannot_become_active_claim(app):
    core, ctx = app
    source = capture(core, ctx, "我喜欢蓝色吗？")
    proposal = draft(source, "蓝色", kind="preference", predicate="颜色偏好", statement_kind="assertion")
    result = accept(core, ctx, proposal).items[0]
    assert result.state != "active"
    assert core.claim_history(ctx, result.ref)[0].state != "active"


def test_unpunctuated_question_revision_cannot_execute(app):
    core, ctx = app
    item, _ = initial(core, ctx, value="蓝色")
    source = capture(core, ctx, "把TEST-project 配色改成银色吗")
    assert core.current_claim(ctx, item.ref).revision == item.revision
    with pytest.raises(ContractError, match="revision_not_asserted"):
        core.revise(ctx, revise_request(item, source, "银色"))
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "蓝色"


def test_negative_cancellation_does_not_cancel_pending_intention(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 验收前提醒检查散热。")
    pending = accept(core, ctx, intention(source)).items[0]
    negative = capture(core, ctx, "TEST-project 不要取消验收前检查散热的约定。")
    attempted = accept(core, ctx, intention(negative, "cancelled")).items[0]
    assert attempted.state == "proposed"
    assert core.current_claim(ctx, pending.ref).payload["intention"]["state"] == "pending"


def test_late_direct_event_cannot_use_capture_fastpath_to_replace_newer_current(app):
    core, ctx = app
    item, _ = initial(core, ctx, value="银色", when="2026-09-03T12:00:00Z")
    old = capture(core, ctx, "TEST-project 配色改为蓝色。", when="2026-09-01T12:00:00Z")
    assert core.current_claim(ctx, item.ref).payload["value_text"] == "银色"
    assert core.records.unresolved_updates(ctx)[0]["source_ref"] == old.ref


def test_invented_method_steps_and_intention_targets_cannot_gain_active(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 方法已认可，先检查温度。")
    p = draft(
        source,
        "方法",
        kind="procedure",
        predicate="方法",
        procedure=dict(
            conditions=[],
            non_applicable=[],
            method=["部署到生产"],
            verification_basis="user_accepted",
            counterexample_refs=[],
        ),
    )
    assert accept(core, ctx, p).items[0].state == "proposed"
    p = intention(source)
    assert accept(core, ctx, p).items[0].state == "proposed"


def test_failure_after_head_evidence_epoch_and_work_updates_rolls_back_everything(app, monkeypatch):
    from scope_recall.core.claim_storage import Claims

    core, ctx = app
    item, _ = initial(core, ctx)
    source = capture(core, ctx, "Please correct TEST-project 配色: 银色。", when="2026-09-03T12:00:00Z")
    before = core.storage.path.read_bytes()
    append = Claims.append

    def fail_after(self, *args, **kwargs):
        append(self, *args, **kwargs)
        raise RuntimeError("TEST fault after mutation")

    monkeypatch.setattr(Claims, "append", fail_after)
    with pytest.raises(RuntimeError, match="TEST fault"):
        core.revise(ctx, revise_request(item, source))
    assert core.storage.path.read_bytes() == before
    assert core.current_claim(ctx, item.ref).revision == 1


def test_unrelated_hypothetical_sentence_cannot_demote_exact_asserted_span(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 配色 蓝色。别的项目银色只是一个假设方案。")
    p = draft(source)
    p["evidence_spans"][0]["quote"] = "TEST-project 配色 蓝色"
    assert accept(core, ctx, p).items[0].state == "active"


def test_import_attestation_cannot_be_reused_for_another_source_body_or_revision(app):
    core, ctx = app
    event = source_event(
        source_event_key="TEST-attested",
        origin="imported",
        source_original_origin="human_direct",
        content="TEST-project 配色 蓝色。",
    )
    provenance = ImportProvenance("human_direct", "a" * 64, frozenset({import_source_fingerprint(event)}))
    actor = replace(ctx, actor_origin="imported", import_provenance=provenance)
    for changes in (
        {"content": "TEST-project 配色 银色。"},
        {"source_revision": 2},
        {"source_event_key": "TEST-forged"},
    ):
        with pytest.raises(ContractError, match="import_provenance"):
            core.record_event(actor, {**event, **changes}, scope_id="TEST-scope", remaining_seconds=10)
    assert counts(core)["claims"] == 0 and core.status(ctx).sources == 0


def test_unknown_retraction_time_does_not_resurrect_known_old_fact_for_asof(app):
    core, ctx = app
    item, _ = initial(core, ctx)
    source = capture(core, ctx, "TEST-project 配色蓝色需要撤回。", when=None)
    # Capture itself performs this unambiguous retraction without inventing time.
    assert core.current_claim(ctx, item.ref) is None
    assert core.current_claim(ctx, item.ref, as_of="2026-09-02T00:00:00Z") is None
    assert core.claim_history(ctx, item.ref)[-1].valid_from is None


def test_explicit_revision_cannot_borrow_unrelated_sentence_deadline(app):
    core, ctx = app
    item, _ = initial(core, ctx)
    source = capture(
        core,
        ctx,
        "Please correct TEST-project 配色: 银色。另一份红色方案到2026-09-30截止。",
        when="2026-09-03T12:00:00Z",
    )
    with pytest.raises(ContractError, match="time_not_grounded"):
        core.revise(ctx, revise_request(item, source, {"value_text": "银色", "valid_to": "2026-09-30T00:00:00Z"}))
    assert core.current_claim(ctx, item.ref).revision == 1


# The model is shown ``json.dumps(content, ensure_ascii=False)``, so a quote it
# copies faithfully can be the escaped form rather than the stored bytes.


def test_encoded_origin_map_agrees_with_json_dumps_character_for_character():
    import json

    from scope_recall.core.evidence_quote import encoded_with_origin

    for raw in (
        'a"b',
        "line\nbreak",
        "path C:\\temp\\x",
        "tab\there",
        '中文 mixed "quote" and \\slash',
        "control" + chr(1) + "char",
    ):
        encoded, origin = encoded_with_origin(raw)
        assert encoded == json.dumps(raw, ensure_ascii=False)[1:-1]
        assert len(origin) == len(encoded)
        assert all(0 <= index < len(raw) for index in origin)


def test_every_slice_of_what_the_model_sees_maps_back_to_a_real_stored_slice():
    from scope_recall.core.evidence_quote import encoded_with_origin, resolve_quote

    raw = '用户说"蓝色"，路径 C:\\a\\b\n第二行。'
    encoded, _origin = encoded_with_origin(raw)
    for start in range(len(encoded) - 1):
        for end in range(start + 1, len(encoded) + 1):
            quote = encoded[start:end]
            stored = resolve_quote(quote, raw)
            assert stored is not None, quote
            # The contract that makes this safe: what comes back is a literal
            # substring of the stored source, and re-encoding it still contains
            # what the model actually quoted.  A boundary that falls inside an
            # escape widens the slice to the whole character; it never invents
            # text that is not in the source.
            assert stored in raw
            assert quote in encoded_with_origin(stored)[0]


def test_quote_resolution_is_monotone_and_refuses_text_that_is_simply_absent():
    from scope_recall.core.evidence_quote import resolve_quote

    raw = 'TEST-project 配色 "蓝色"。'
    for start in range(len(raw) - 4):
        assert resolve_quote(raw[start : start + 4], raw) == raw[start : start + 4]
    assert resolve_quote("红色", raw) is None
    assert resolve_quote("", raw) is None
    assert resolve_quote(None, raw) is None
    assert resolve_quote("a", None) is None


def test_claim_quoted_from_the_encoded_source_is_accepted_and_stored_as_raw_bytes(app):
    import json

    core, ctx = app
    content = 'TEST-project 配色定为"蓝色"。路径 C:\\conf\\a.json。'
    source = capture(core, ctx, content)
    escaped = json.dumps(content, ensure_ascii=False)[1:-1]
    assert escaped != content
    proposal = draft(source, "蓝色")
    proposal["evidence_spans"][0]["quote"] = escaped
    item = accept(core, ctx, proposal).items[0]
    with sqlite3.connect(core.storage.path) as conn:
        stored = conn.execute(
            "SELECT quote FROM evidence_links WHERE object_ref=? AND object_revision=?", (item.ref, item.revision)
        ).fetchone()[0]
    # What gets recorded is the stored slice, never the escaped text the model saw.
    assert stored == content and stored in source.event["content"]


def _escaped_envelope():
    # The shape of a question_not_asserted rejection:
    # a tool envelope stored as an escaped JSON body, so the only line breaks
    # are two-character \n sequences and the first *real* punctuation after the
    # quoted line is a question mark hundreds of characters away.
    return (
        '{"lines": "TEST-project 配色定为蓝色\\ncontainer_memory 5120\\nprovider registered 7 tools\\n是否改成红色？"}'
    )


def test_a_question_elsewhere_in_an_escaped_envelope_no_longer_vetoes_the_asserted_line(app):
    core, ctx = app
    source = capture(core, ctx, _escaped_envelope())
    assert "\n" not in source.event["content"]
    item = accept(core, ctx, draft(source, "蓝色", quote_override="TEST-project 配色定为蓝色")).items[0]
    assert item.state == "active"


def test_the_quoted_line_being_the_question_is_still_refused(app):
    core, ctx = app
    source = capture(core, ctx, _escaped_envelope())
    item = accept(core, ctx, draft(source, "红色", quote_override="是否改成红色")).items[0]
    assert item.state == "proposed"
    history = core.claim_history(ctx, item.ref)
    assert history[-1].reason == "question_not_asserted"


def test_quote_absent_from_both_forms_is_still_rejected(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 配色定为蓝色。")
    proposal = draft(source, "蓝色")
    proposal["evidence_spans"][0]["quote"] = "TEST-project 配色定为红色。"
    with pytest.raises(ContractError):
        accept(core, ctx, proposal)


def test_a_quote_copying_a_quoted_placeholder_is_accepted(app):
    """A result is screened text by text, as written.  Read through its JSON encoding, the placeholder's quotes are
    escaped and the quote the model copied from the source reads as a credential, refusing the whole result."""
    core, ctx = app
    content = "TEST config" + chr(10) + 'api_key: "<your-api-key>"' + chr(10) + "TEST-project 配色 蓝色。"
    source = capture(core, ctx, content)
    assert accept(core, ctx, draft(source)).items[0].state == "active"


def test_a_result_carrying_a_credential_is_refused(app):
    core, ctx = app
    source = capture(core, ctx, "TEST-project 配色 蓝色。")
    with pytest.raises(ContractError) as refused:
        accept(core, ctx, draft(source, conditions=["password: hunter2-not-a-placeholder"]))
    assert refused.value.field == "plaintext_secret_rejected"
