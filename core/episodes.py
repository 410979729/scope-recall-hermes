"""Bounded, source-grounded episode and resume rules. No database or model I/O."""

from __future__ import annotations

import hashlib
import json
import re

from ..contracts import ContractError
from .claims import canonical_time
from .source_qualification import AUTHORITY_QUESTION, preserves_qualifiers

TOPIC_BREAK = re.compile(r"换个话题|另一个话题|转去|转到|接下来讨论|switch (?:topic|to)|new topic", re.I)
#: ``如果``/``假如``/``假设`` had no English counterpart, so an English
#: conditional read as an assertion: "If a tool failed because of setup state,
#: capture the FIX" was taken as a report that the episode had failed.
UNSETTLED = re.compile(
    r"假设|假如|如果|也许|可能|引用|据说|他说|她说|suppose|hypothetical|perhaps|maybe|quoted|\bif\b|\bin case\b", re.I
)
UNFINISHED = re.compile(
    r"尚未|还没|未完成|没有完成|没通过|未通过|not (?:yet |been )?(?:done|complete|passed)|will |going to|计划|打算",
    re.I,
)
#: Loose, and deliberately so: this gates a *quoted* ``verified_progress`` line
#: that the model already had to lift verbatim from an authorized source.  It
#: is not a terminal-state rule and must not be used as one.
FINISHED = re.compile(
    r'完成|通过|已确认|已经确认|确认了|done|completed|passed|verified|confirmed|exit.code["\s:：=]*0\b', re.I
)
WORK_REQUEST = re.compile(
    r"请|帮我|麻烦|我要|我想(?:做|把|写|完成|制作)|下一步|先做|继续(?:做|处理|实现|检查)|(?:目标|任务)(?:是|为)|please|help me|next step|(?:work|working) on|let[’\']s",
    re.I,
)

#: The three terminal markers, each required to name *what* reached that state.
#: ``failed`` used to be the exception -- a bare ``失败``/``failed`` anywhere in
#: a source ended the episode -- while ``cancelled`` and ``completed`` both
#: demanded the task noun.  In one store that produced 32 failed episode versions
#: and not one of them was a real failure: the only live source that still
#: reaches this rule says "two prior failed questions remain in evidence",
#: an adjective, 4 KB into a 7,674-character tool transcript.  A wrongly failed
#: episode is not merely mislabelled, it drops out of resume injection
#: (``core/background_context.py`` admits only ``open`` and ``interrupted``),
#: so live work disappears silently.  The machine signal is kept as its own
#: arm: a non-zero exit code is an observed outcome, not prose about one.
EPISODE_CANCELLED = re.compile(
    r"(?:取消|cancel(?:led)?)(?:整个)?(?:此|这个|该|the |this )?(?:任务|task)|任务(?:已)?取消", re.I
)
EPISODE_FAILED = re.compile(
    r'(?:整个任务|全部工作|任务|全部)(?:已经|已|都)?失败|(?:whole task|all work|the task) (?:is |was |has )?failed|exit.code["\s:：=]*[1-9]\d*',
    re.I,
)
EPISODE_COMPLETED = re.compile(
    r"(?:整个任务|全部工作|任务|全部)(?:已经|已|全部|都)?(?:完成|做完|搞定)|(?:whole task|all work|the task) (?:is |was |has been )?(?:completed|done|finished)",
    re.I,
)


def supported_work_goal(proposal, sources):
    goal = proposal["goal"]
    return any(
        f"{s.ref}@{s.revision}" in goal["evidence_refs"]
        and source_origin(s) == "human_direct"
        and not s.capture_gaps
        and not UNSETTLED.search(s.event["content"])
        and not AUTHORITY_QUESTION.search(s.event["content"])
        and WORK_REQUEST.search(s.event["content"])
        for s in sources
    )


def source_watermark(refs) -> str:
    return (
        "watermark-"
        + hashlib.sha256(json.dumps(list(refs), ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    )


def source_origin(source) -> str:
    if source.event["origin"] == "imported":
        return (
            source.event.get("source_original_origin", "origin_unknown")
            if source.import_provenance_sha256
            else "origin_unknown"
        )
    return source.event["origin"]


def _said_state(raw: str) -> str | None:
    """The state a message's words give the task, plainly and with their qualifiers: cancelled, failed, completed
    with nothing left unfinished, or open again (paused, resumed); None when they give none."""
    cancelled = EPISODE_CANCELLED.search(raw)
    failed = EPISODE_FAILED.search(raw)
    completed = EPISODE_COMPLETED.search(raw)
    if cancelled and preserves_qualifiers(raw, cancelled.group()):
        return "cancelled"
    if failed and preserves_qualifiers(raw, failed.group()):
        return "failed"
    if completed and preserves_qualifiers(raw, completed.group()) and not UNFINISHED.search(raw):
        return "completed"
    if any(
        preserves_qualifiers(raw, marker.group())
        for marker in re.finditer(r"暂停|先停|继续|\b(?:pause|resume|continue)\b", raw, re.I)
    ):
        return "open"
    return None


def state_from_sources(sources, *, has_goal=False, previous="unknown") -> str:
    state = previous
    # Occurrence order is independent of import/arrival order. Unknown times
    # cannot be used to establish a newer terminal state over an existing one.
    ordered = sorted(sources, key=lambda s: (canonical_time(s.event["occurred_at"]) or "", s.ref, s.revision))
    for source in ordered:
        if source.event["occurred_at"] is None and state != "unknown":
            continue
        if source_origin(source) not in {"human_direct", "tool_observation", "host_generated"} or source.capture_gaps:
            continue
        # A tool call that failed, was stopped or came back cut off (stored ``partial``) tells its own outcome, not the
        # task's: a grep that finds nothing exits 1, a command the person stopped exits 130.  Kept since 3.7.8, such a
        # Hermes result turned the open task failed, and resume offers only an open or an interrupted one.
        if source_origin(source) == "tool_observation" and source.event.get("capture_state") != "complete":
            continue
        raw = source.event["content"]
        if UNSETTLED.search(raw):
            continue
        if source_origin(source) == "host_generated":
            # Only exact structured host lifecycle observation can end a turn;
            # a model's narration of interruption is not that observation.
            try:
                lifecycle = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if type(lifecycle) is dict and lifecycle.get("lifecycle") == "interrupted":
                state = "interrupted"
            continue
        if AUTHORITY_QUESTION.search(raw):
            continue
        said = _said_state(raw)
        if said is not None:
            state = said
    return "open" if state == "unknown" and has_goal else state


def _quoted_sources(by_ref, refs, text: str, missing: str, omitted: str) -> list:
    """The cited sources that quote ``text`` with its qualifiers: ``missing`` when none quotes it, ``omitted`` when
    every quote drops a qualifier."""
    candidates = [by_ref[ref] for ref in refs if ref in by_ref and text in by_ref[ref].event["content"]]
    if not candidates:
        raise ContractError("DERIVATION_INVALID", missing)
    candidates = [s for s in candidates if preserves_qualifiers(s.event["content"], text)]
    if not candidates:
        raise ContractError("DERIVATION_INVALID", omitted)
    return candidates


def _states_goal(source) -> bool:
    """Whether a source can state a work goal: the person's own complete message, neither asked nor unsettled.

    A consolidation proposal may only restate a user's own stated goal.  Assistant narration, tool output, quotations,
    and hypotheses cannot create a work obligation or turn an ordinary conversation into a completed episode.
    """
    return (
        source_origin(source) == "human_direct"
        and source.event["capture_state"] == "complete"
        and not AUTHORITY_QUESTION.search(source.event["content"])
        and not UNSETTLED.search(source.event["content"])
    )


def _authorizes_entry(field: str, text: str, source) -> bool:
    """Whether a source can authorize a decision (the person's own message) or verified progress (also a tool's
    observation): complete, neither asked nor unsettled, and for progress only where every clause quoting it says it
    is finished."""
    origin = source_origin(source)
    if (
        origin not in ({"human_direct"} if field == "decisions" else {"human_direct", "tool_observation"})
        or source.capture_gaps
        or source.event["capture_state"] != "complete"
    ):
        return False
    # Include local comma clause to catch trimmed negation while
    # permitting one sentence's separate finished/open clauses.
    clauses = [c for c in re.split(r"[，,;；。.!?！？\n]", source.event["content"]) if text.rstrip("。.!") in c]
    contexts = clauses or [source.event["content"]]
    if UNSETTLED.search(source.event["content"]) or AUTHORITY_QUESTION.search(source.event["content"]):
        return False
    return not (field == "verified_progress" and any(UNFINISHED.search(c) or not FINISHED.search(c) for c in contexts))


def _asks_step(source) -> bool:
    """Whether a source can ask for a next step: the person's own complete message without gaps, neither unsettled
    nor asked."""
    return (
        source_origin(source) == "human_direct"
        and source.event["capture_state"] == "complete"
        and not source.capture_gaps
        and not UNSETTLED.search(source.event["content"])
        and not AUTHORITY_QUESTION.search(source.event["content"])
    )


def qualify_resume(proposal, sources) -> None:
    """Require extractive support and preserve report/observation distinctions.

    Semantic reformulation is left to the answering host. This intermediate
    structure retains the quoted claims and does not invent verified progress.
    """
    by_ref = {f"{s.ref}@{s.revision}": s for s in sources}
    refs = proposal["evidence_refs"]
    if proposal["source_watermark"] != source_watermark(refs):
        raise ContractError("DERIVATION_INVALID", "source_watermark")
    for field in ("goal", "decisions", "verified_progress", "open_items", "blockers"):
        values = [proposal[field]] if field == "goal" else proposal[field]
        for item in values:
            candidates = _quoted_sources(
                by_ref, item["evidence_refs"], item["text"], "resume_text_support", "resume_qualifier_omitted"
            )
            if field == "goal" and not any(_states_goal(source) for source in candidates):
                raise ContractError("DERIVATION_INVALID", "goal_authority")
            if field in {"decisions", "verified_progress"} and not [
                source for source in candidates if _authorizes_entry(field, item["text"], source)
            ]:
                raise ContractError("DERIVATION_INVALID", "resume_authority")
    step = proposal["next_step"]
    if step is not None:
        supporting = _quoted_sources(by_ref, refs, step, "next_step_support", "next_step_qualifier_omitted")
        basis = proposal["next_step_basis"]
        if basis in {"user_requested", "existing_plan"} and not any(_asks_step(s) for s in supporting):
            raise ContractError("DERIVATION_INVALID", "next_step_authority")
