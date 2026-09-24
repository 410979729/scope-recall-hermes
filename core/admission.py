"""Conservative scheduling policy; source fidelity and authority are unchanged.

Only exact, content-free acknowledgements, successful tool wrappers and Scope
Recall's own reinjected output are cheap terminal cases. Unrecognized text
remains eligible. Queue pressure postpones derived work, never source
persistence or lexical search.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re
import unicodedata

from ..contracts import ContractError
from .work_storage import FRESH_CONVERSATION_ORIGINS, fresh_since

ADMISSION_KEY = "_scope_recall_admission"
WORK_TYPES = frozenset({"consolidate", "embed"})


@dataclass(frozen=True)
class AdmissionPolicy:
    # Split the total allowance equally between independent enrichment queues;
    # an unavailable optional embedding route cannot consume consolidation slots.
    enabled: bool = True
    max_pending_work: int = 2048
    important_reserve: int = 128

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("admission enabled must be boolean")
        if type(self.max_pending_work) is not int or not 2 <= self.max_pending_work <= 100000:
            raise ValueError("max_pending_work must be between 2 and 100000")
        if type(self.important_reserve) is not int or not 0 <= self.important_reserve <= 10000:
            raise ValueError("important_reserve must be between zero and 10000")


@dataclass(frozen=True)
class AdmissionDecision:
    disposition: str
    reason: str
    important: bool = False
    work_types: frozenset[str] = frozenset()

    @property
    def gap(self):
        return None if self.disposition == "schedule" else f"admission_{self.disposition}:{self.reason}"


@dataclass(frozen=True)
class SourceScheduleReceipt:
    ref: str
    revision: int
    disposition: str
    reason: str
    queued_work: int = 0


#: A Scope Recall tool result captured back as a source.  It stays persisted and
#: lexically searchable (retrieval already ranks it last), but it is never a
#: consolidation root or candidate evidence, and embedding it only re-indexes
#: what recall already returned.  So it earns no derived work: not at capture,
#: not on refill and not on demand.
_REINJECTION = AdmissionDecision("source_only", "memory_reinjection")


_ACKS = frozenset({"好", "好的", "嗯", "嗯嗯", "哦", "噢", "收到", "明白", "了解", "谢谢", "谢谢你", "你好", "早上好", "晚上好", "晚安", "哈哈", "ok", "okay", "yes", "thanks", "thankyou", "hello", "hi", "goodnight", "ack", "acknowledged", "gotit"})
_IMPORTANT = re.compile(r"更正|纠正|改为|改成|换成|调整为|取消|作废|不再|停止使用|停止采用|弃用|不要|必须|记住|偏好|喜欢|决定|采用|截止|完成|修复|失败|错误|\b(?:correct(?:ion)?|instead|cancel(?:led)?|no longer|switch to|discontinue|remember|prefer|decid\w*|deadline|must|error|fail\w*)\b", re.I)
_TOOL_OK = re.compile(r"(?:success|successful|done|completed|ok|process exited with (?:code|exit code) 0|exit code:? 0)[.!\s]*", re.I)
#: The capture filter's own placeholder for a tool output it withheld
#: (``capture_filters.sanitize_report_text``), and the 2.0 release's form of
#: it.  There is nothing in it to search for by meaning or to derive from: on
#: one instance 132,000 of 168,000 sources were such lines, each embedded.
_OMITTED_TOOL_SUMMARY = re.compile(r"Tool execution summary\b.*\b(?:output omitted|output_preview=omitted)\b", re.S)


def _ack(text):
    normalized = unicodedata.normalize("NFKC", text).casefold()
    if "?" in normalized:
        return False
    return re.sub(r"[\s.!。！,，~～]+", "", normalized) in _ACKS


def classify(event, policy=None):
    """No semantic guesses: a keyword only raises scheduling priority."""
    policy = policy or AdmissionPolicy()
    if not policy.enabled:
        return AdmissionDecision("schedule", "policy_disabled")
    if event.get("origin") == "memory_reinjection":
        # Before importance: recall output routinely repeats the keywords and
        # evidence refs that would otherwise raise its priority.
        return _REINJECTION
    text = event["content"]
    if event.get("role") == "tool" and _OMITTED_TOOL_SUMMARY.match(text.strip()):
        return AdmissionDecision("source_only", "tool_output_omitted")
    important = bool(event.get("artifact_refs") or event.get("evidence_refs") or event.get("segment") or _IMPORTANT.search(text))
    if important:
        return AdmissionDecision("schedule", "important_source", True)
    if event.get("capture_state") == "gap" and not text.strip():
        return AdmissionDecision("source_only", "capture_gap")
    if _ack(text):
        return AdmissionDecision("source_only", "acknowledgement")
    if event.get("role") == "tool":
        if _TOOL_OK.fullmatch(text.strip()):
            return AdmissionDecision("source_only", "successful_tool_wrapper")
        try:
            body = json.loads(text)
        except (ValueError, TypeError):
            body = None
        allowed = {"status", "success", "ok", "exit_code", "returncode", "duration_ms", "elapsed_ms", "stdout", "stderr", "output", "message"}
        if isinstance(body, dict) and body and set(body) <= allowed:
            success = body.get("success") is True or body.get("ok") is True or body.get("status") in ("ok", "success", "completed") or type(body.get("exit_code")) is int and body["exit_code"] == 0 or type(body.get("returncode")) is int and body["returncode"] == 0
            no_failure = body.get("success") is not False and body.get("ok") is not False and body.get("exit_code", 0) == 0 and body.get("returncode", 0) == 0
            empty_output = all(body.get(key) in (None, "", [], {}) for key in ("stdout", "stderr", "output"))
            message = body.get("message", "")
            empty_message = type(message) is str and (not message.strip() or _ack(message) or bool(_TOOL_OK.fullmatch(message.strip())))
            if success and no_failure and empty_output and empty_message:
                return AdmissionDecision("source_only", "successful_tool_wrapper")
    return AdmissionDecision("schedule", "content_not_classified_low_value")


def pending_count(tx, scope_id, *, ceiling, work_type):
    """Bound the scan and keep other scope/project/branch queues private."""
    tx._scope(scope_id)
    return int(tx._check().execute("""SELECT count(*) FROM (
        SELECT 1 FROM work_items WHERE work_type=?
        AND state IN ('pending','leased') AND scope_id=?
        AND project_id IS ? AND branch_id IS ? LIMIT ?)""",
        (work_type, scope_id, tx.context.project_id, tx.context.branch_id, ceiling)).fetchone()[0])


def _capacity(policy, important):
    return (policy.max_pending_work + (policy.important_reserve if important else 0)) // 2


def _available_types(tx, scope_id, policy, important, candidates=WORK_TYPES):
    if not policy.enabled:
        return frozenset(candidates)
    ceiling = _capacity(policy, important)
    return frozenset(kind for kind in candidates
                     if pending_count(tx, scope_id, ceiling=ceiling, work_type=kind) < ceiling)


def _repeated_tool_output(tx, scope_id, text) -> bool:
    """Whether an earlier, still readable tool output in this scope has exactly this content.

    On one instance 77% of 132,000 tool outputs were byte-identical to an
    earlier one, and each was embedded again.  The earlier copy already carries
    the vector and the lexical index lists both, so a repeat is kept as a
    source only.  An earlier copy that was itself kept as a source only (recall
    output, a repeat, a withheld summary) carries no vector and does not count.
    Text from a person is never a repeat: saying it again is new.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return tx._check().execute(
        """SELECT 1 FROM source_events WHERE scope_id=? AND role='tool' AND content_sha256=? AND read_blocked=0
           AND COALESCE(json_extract(extra_json,'$._scope_recall_admission.disposition'),'')!='source_only' LIMIT 1""",
        (scope_id, digest),
    ).fetchone() is not None


#: What a tool output earns: an embedding, so it is found by meaning.  It is not consolidated;
#: tool output is no derivation root (``evidence_question.DERIVATION_ROOT_ORIGINS``), so a
#: consolidation of it would show the model nothing.
TOOL_OUTPUT_WORK_TYPES = frozenset({"embed"})


def wanted_work_types(event) -> frozenset[str]:
    """The derived work a scheduled source is owed: every kind, or an embedding alone for tool output."""
    return TOOL_OUTPUT_WORK_TYPES if event.get("role") == "tool" else WORK_TYPES


def decide(tx, event, scope_id, policy=None):
    policy = policy or AdmissionPolicy()
    decision = classify(event, policy)
    if decision.disposition != "schedule":
        return decision
    if event.get("role") == "tool" and _repeated_tool_output(tx, scope_id, event["content"]):
        return AdmissionDecision("source_only", "tool_output_repeat")
    wanted = wanted_work_types(event)
    kinds = _available_types(tx, scope_id, policy, decision.important, wanted)
    if kinds != wanted:
        return AdmissionDecision("deferred", "queue_capacity", decision.important, kinds)
    return replace(decision, work_types=kinds)


def store_decision(tx, ref, revision, decision):
    """Internal scheduling metadata is excluded from the decoded SourceEvent.

    Called only after strict capture validation and source insertion. Never add
    operational decisions to capture_gaps, which describe evidence fidelity.
    """
    if decision.gap is None:
        return
    payload = json.dumps({"disposition": decision.disposition, "reason": decision.reason,
                          "important": decision.important}, separators=(",", ":"))
    tx._check(write=True).execute("""UPDATE source_events
        SET extra_json=json_set(extra_json,'$._scope_recall_admission',json(?))
        WHERE event_id=? AND source_revision=?""", (payload, ref, revision))


def decision_marker(tx, ref, revision):
    if tx.source(ref, revision) is None:
        return None
    row = tx._check().execute("""SELECT json_extract(extra_json,'$._scope_recall_admission.disposition'),
        json_extract(extra_json,'$._scope_recall_admission.reason') FROM source_events
        WHERE event_id=? AND source_revision=?""", (ref, revision)).fetchone()
    if row is None or row[0] not in {"source_only", "deferred"}:
        return None
    return f"admission_{row[0]}:{row[1]}"


def _schedule(tx, clock, ref, revision, policy, *, on_demand=True, fresh=False):
    source = tx.source(ref, revision)
    current = tx.source_current(ref)
    from .visibility import allowed
    if source is None or current is None or current.revision != revision or source.suppressed or not allowed(tx, "event", ref, automatic=True):
        raise ContractError("SOURCE_MISSING")
    if source.project_id != tx.context.project_id or source.branch_id != tx.context.branch_id:
        raise ContractError("SOURCE_MISSING")
    conn = tx._check(write=True)
    existing = conn.execute("SELECT work_type FROM work_items WHERE subject_ref=? AND subject_revision=? AND work_type IN ('consolidate','embed')", (ref, revision)).fetchall()
    present = {row[0] for row in existing}
    missing = wanted_work_types(source.event) - present
    if not missing:
        # A tool output deferred before it stopped being owed a consolidation may already hold its
        # embedding; settle its marker, or the refill would select it on every pass.
        if decision_marker(tx, ref, revision) == "admission_deferred:queue_capacity":
            conn.execute("""UPDATE source_events SET extra_json=json_remove(extra_json,'$._scope_recall_admission')
                WHERE event_id=? AND source_revision=?""", (ref, revision))
        return SourceScheduleReceipt(ref, revision, "unchanged", "already_scheduled")
    decision = classify(source.event, policy)
    if decision == _REINJECTION:
        # Neither a refill nor an explicit request turns recall output into
        # work.  A row deferred before this rule settles as source_only, so the
        # refill page stops selecting it; a settled marker is not rewritten.
        if decision_marker(tx, ref, revision) != decision.gap:
            store_decision(tx, ref, revision, decision)
        return SourceScheduleReceipt(ref, revision, decision.disposition, decision.reason)
    prior_priority = conn.execute("""SELECT json_extract(extra_json,'$._scope_recall_admission.important')
        FROM source_events WHERE event_id=? AND source_revision=?""", (ref, revision)).fetchone()[0]
    priority = on_demand or prior_priority == 1 or decision.important
    # Freshness lends the reserve only while it lasts; it is never stored as importance.
    ready = _available_types(tx, source.scope_id, policy, priority or fresh, missing)
    if not ready:
        return SourceScheduleReceipt(ref, revision, "deferred", "queue_capacity")
    for kind in sorted(ready):
        tx.enqueue_source(ref, revision, work_type=kind, available_at=clock.utc_now())
    if ready != missing:
        store_decision(tx, ref, revision, AdmissionDecision("deferred", "queue_capacity", priority))
        return SourceScheduleReceipt(ref, revision, "partial", "queue_capacity", len(ready))
    conn.execute("""UPDATE source_events SET extra_json=json_remove(extra_json,'$._scope_recall_admission')
        WHERE event_id=? AND source_revision=?""", (ref, revision))
    return SourceScheduleReceipt(ref, revision, "scheduled", "on_demand" if on_demand else "queue_capacity_available", len(ready))


def schedule_source(storage, clock, context, ref, revision, *, policy=None, remaining_seconds=1.0):
    """Trusted on-demand scheduling; no data recovery or failed-work resets."""
    if context.actor_origin not in {"human_direct", "host_generated"}:
        raise ContractError("ACCESS_DENIED", "schedule_origin")
    with storage.write(context, remaining_seconds=remaining_seconds) as tx:
        return _schedule(tx, clock, ref, revision, policy or AdmissionPolicy())


def resume_deferred(storage, clock, context, policy=None, *, limit=16, remaining_seconds=1.0):
    """Bounded automatic queue refill; never promote low-value terminal sources.

    Fresh conversation -- a human_direct or tool_observation source inside the
    worker's fresh lane window -- is refilled first, oldest first, and may use
    the important reserve.  Oldest-first alone left a message typed now behind
    every older deferred source, and behind important ones for good while they
    kept the reserve full.  The ceilings themselves are unchanged.
    """
    if type(limit) is not int or not 1 <= limit <= 64:
        raise ContractError("INPUT_INVALID", "deferred_limit")
    if context.actor_origin not in {"human_direct", "host_generated"}:
        raise ContractError("ACCESS_DENIED", "schedule_origin")
    policy = policy or AdmissionPolicy()
    with storage.write(context, remaining_seconds=remaining_seconds) as tx:
        scopes = sorted(context.allowed_scope_ids)
        marks = ",".join("?" for _ in scopes)
        origins = sorted(FRESH_CONVERSATION_ORIGINS)
        fresh = f"(e.persisted_at>=? AND e.origin IN ({','.join('?' for _ in origins)}))"
        fresh_params = (fresh_since(clock.utc_now()), *origins)
        # Filter before LIMIT: a long embedding-only backlog must not hide a
        # later source whose healthy consolidation slot can be filled now.
        eligible = []
        eligibility_params = []
        # The queue is counted once for every scope, not once per scope and
        # type: a shared store binds hundreds of scopes, and with 32,000 items
        # queued after an import the 442 separate counts took 90 s of a 120 s
        # pass, so the watchdog ended every pass before it embedded anything.
        kinds = sorted(WORK_TYPES)
        queued = {(scope, kind): count for scope, kind, count in tx._check().execute(
            f"""SELECT scope_id,work_type,count(*) FROM work_items
                WHERE state IN ('pending','leased') AND scope_id IN ({marks}) AND project_id IS ? AND branch_id IS ?
                AND work_type IN ({','.join('?' for _ in kinds)}) GROUP BY scope_id,work_type""",
            (*scopes, context.project_id, context.branch_id, *kinds))}
        for scope in scopes:
            tx._scope(scope)
            for kind in kinds:
                count = queued.get((scope, kind), 0)
                ordinary = not policy.enabled or count < _capacity(policy, False)
                priority = not policy.enabled or count < _capacity(policy, True)
                if not priority:
                    continue
                # A tool output is owed an embedding only (``wanted_work_types``), so it never holds
                # a consolidation.  It is picked for one only once its embedding is queued, to settle
                # a marker written before that rule; picked while its embedding waited for room, it
                # came first on every pass and held the page.
                owed = "" if kind in TOOL_OUTPUT_WORK_TYPES else """AND (e.role<>'tool' OR EXISTS(
                    SELECT 1 FROM work_items o WHERE o.subject_ref=e.event_id
                    AND o.subject_revision=e.source_revision AND o.work_type='embed'))"""
                priority_filter, priority_params = "", ()
                if not ordinary:
                    priority_filter = f"AND (json_extract(e.extra_json,'$._scope_recall_admission.important')=1 OR {fresh})"
                    priority_params = fresh_params
                eligible.append(f"""(e.scope_id=? {owed} {priority_filter} AND NOT EXISTS(
                    SELECT 1 FROM work_items w WHERE w.subject_ref=e.event_id
                    AND w.subject_revision=e.source_revision AND w.work_type=?))""")
                eligibility_params.extend((scope, *priority_params, kind))
        if not eligible:
            return ()
        rows = tx._check().execute(f"""SELECT e.event_id,e.source_revision,{fresh} AS fresh FROM source_events e
            WHERE e.scope_id IN ({marks}) AND e.project_id IS ? AND e.branch_id IS ?
            AND e.read_blocked=0 AND e.suppressed=0
            AND json_extract(e.extra_json,'$._scope_recall_admission.disposition')='deferred'
            AND json_extract(e.extra_json,'$._scope_recall_admission.reason')='queue_capacity'
            AND ({' OR '.join(eligible)})
            AND NOT EXISTS(SELECT 1 FROM source_events n WHERE n.source_group_key=e.source_group_key AND n.source_revision>e.source_revision)
            ORDER BY fresh DESC,e.persisted_at,e.event_id LIMIT ?""",
            (*fresh_params, *scopes, context.project_id, context.branch_id, *eligibility_params, limit)).fetchall()
        results = []
        for row in rows:
            try:
                results.append(_schedule(tx, clock, row[0], row[1], policy, on_demand=False, fresh=bool(row[2])))
            except ContractError as exc:
                if exc.code != "SOURCE_MISSING":
                    raise
        return tuple(results)
