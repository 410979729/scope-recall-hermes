"""Persisted reflection generation and independent support stages outside SQLite."""

from __future__ import annotations

import json
from functools import partial

from ..contracts import ContractError
from . import private
from .candidate_lifecycle import SELF_SUBJECTS, model_subject
from .evidence_support import using_support
from .language_resources import prompt_text
from .reflection_policy import day_reset, finish_generation, reserve_call, utc
from .reflection_storage import decode_observations, publish_observation, reflection_job, session_numbers
from .reflection_validity import check_dependencies
from .semantic_proposals import prepare_support
from .worker_outcomes import (
    budget_left,
    defer_model_pass,
    finalize_work,
    model_failure,
    stale_result,
    work_result,
)


def reflection_messages(members, *, subject):
    """Bound source input independently of store size; source text never acts as instructions."""
    numbered = session_numbers(members)
    members = [dict(m, session_number=numbered[m["session_id"]]) for m in members]
    messages = [
        dict(role="system", content=prompt_text("reflection")),
        dict(
            role="user", content=json.dumps(dict(task="reflect", subject=subject, sources=members), ensure_ascii=False)
        ),
    ]
    if sum(len(m["content"].encode()) for m in messages) > 32000:
        raise ContractError("INPUT_INVALID", "reflection_input_budget")
    return messages


def _guard(storage, clock, context, group):
    with storage.read(context, remaining_seconds=1.0) as tx:
        check_dependencies(tx, json.loads(group["manifest_json"]), group["signature"], now=clock.utc_now())
    return False


def _judge(storage, clock, context, group, model, messages, *, remaining_seconds):
    """Reserve against the exact scope before dispatch without renewing a model allowance."""
    private.require_outbound()
    with storage.write(context, remaining_seconds=remaining_seconds) as tx:
        check_dependencies(tx, json.loads(group["manifest_json"]), group["signature"], now=clock.utc_now())
        if not reserve_call(tx, group["scope_id"], now=clock.utc_now()):
            raise ContractError("BUDGET_EXHAUSTED", "reflection_daily_limit")
    return model.judge(messages, remaining_seconds=remaining_seconds)


def _daily_hold(storage, clock, context, item, *, started, budget):
    """Keep the exact paid stage pending until the independent UTC counter resets."""
    now = clock.utc_now()
    seconds = (utc(day_reset(now)) - utc(now)).total_seconds()
    with storage.write(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
        return work_result(
            tx.work.defer_without_attempt(*item.lease, now=now, error_code="reflection_daily_limit", seconds=seconds)
        )


def process_reflection(storage, clock, context, item, *, model, started, budget):
    """Run one durable stage, sharing the model port's per-pass call allowance."""
    finish = partial(finalize_work, storage, clock, context, item, started=started, budget=budget)
    if not callable(getattr(model, "judge", None)):
        return finish("retry", "model_unavailable")
    deferred = defer_model_pass(storage, clock, context, item, model, started=started, budget=budget)
    if deferred is not None:
        return deferred
    try:
        with storage.read(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
            group = reflection_job(tx, item)
            if group is None:
                return finish("obsolete", "SOURCE_MISSING")
            members = json.loads(group["manifest_json"])
            sources = check_dependencies(tx, members, group["signature"], now=clock.utc_now())
        guard = partial(_guard, storage, clock, context, group)
        guard()
        verdicts = ()
        rejections = []
        judge = partial(_judge, storage, clock, context, group, model)
        proposals = json.loads(group["proposals_json"]) if group["proposals_json"] is not None else None
        with private.using_outbound_check(guard):
            private.require_outbound()
            if proposals is None:
                raw = judge(
                    reflection_messages(
                        [dict(m, content=s.event["content"][:512]) for m, s in zip(members, sources, strict=True)],
                        subject=model_subject(group["subject"], sources),
                    ),
                    remaining_seconds=budget_left(started, clock, budget),
                )
                proposals = decode_observations(raw, members, sources, rejections=rejections)
                public_subject = model_subject(group["subject"], sources)
                for proposal in proposals:
                    if public_subject != group["subject"] and (
                        proposal["subject"] == public_subject or proposal["subject"] in SELF_SUBJECTS
                    ):
                        proposal["subject"] = group["subject"]
            else:
                verdicts = prepare_support(
                    dict(claim_proposals=[proposals[group["next_proposal"]]]),
                    sources,
                    judge,
                    remaining_seconds=budget_left(started, clock, budget),
                    observation=True,
                )
        return _publish_stage(
            storage, clock, context, item, (group, proposals, verdicts, rejections), started=started, budget=budget
        )
    except (ContractError, ValueError, TypeError) as exc:
        if getattr(exc, "field", None) == "reflection_daily_limit":
            return _daily_hold(storage, clock, context, item, started=started, budget=budget)
        if getattr(exc, "code", None) in {"SOURCE_MISSING", "VERSION_CONFLICT", "ACCESS_DENIED"}:
            return _discard_stage(storage, clock, context, item, started=started, budget=budget)
        return finish("retry", getattr(exc, "code", None) or "DERIVATION_INVALID")
    except (OSError, RuntimeError, TimeoutError) as exc:
        return finish(*model_failure(exc))


def _publish_stage(storage, clock, context, item, prepared, *, started, budget):
    """Commit a paid stage once, only under unchanged sources, privacy and lease."""
    group, proposals, verdicts, rejections = prepared
    with storage.write(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
        if not tx.work._verify_lease(*item.lease, now=clock.utc_now()):
            return stale_result(tx, item)
        members = json.loads(group["manifest_json"])
        check_dependencies(tx, members, group["signature"], now=clock.utc_now())
        index = group["next_proposal"]
        if group["proposals_json"] is None:
            finish_generation(tx, group["group_key"], empty=not proposals)
            for rejected in rejections:
                tx._check().execute(
                    "INSERT INTO reflection_rejections VALUES(?,?,?,?,?)",
                    (
                        item.subject_ref,
                        item.subject_revision,
                        rejected["candidate"],
                        rejected["reason"],
                        clock.utc_now(),
                    ),
                )
            tx._check().execute(
                "UPDATE reflection_groups SET proposals_json=? WHERE group_key=?",
                (json.dumps(proposals), group["group_key"]),
            )
        else:
            failure = next((v.failure_code for v in verdicts if v.failure_code), None)
            if failure:
                return work_result(
                    tx.work.fail(*item.lease, now=clock.utc_now(), error_code=failure, recoverable=True),
                    error_code=failure,
                )
            with using_support(verdicts):
                publish_observation(tx, group, proposals[index], now=clock.utc_now())
            index += 1
            tx._check().execute(
                "UPDATE reflection_groups SET next_proposal=? WHERE group_key=?", (index, group["group_key"])
            )
        if index < len(proposals):
            return work_result(
                tx.work.defer_without_attempt(
                    *item.lease, now=clock.utc_now(), error_code="reflection_stage", seconds=0
                )
            )
        tx._check().execute("UPDATE reflection_groups SET job_ref=NULL WHERE group_key=?", (group["group_key"],))
        return work_result(tx.work.complete(*item.lease, now=clock.utc_now()))


def _discard_stage(storage, clock, context, item, *, started, budget):
    """Release an invalid dependency batch so fresh evidence can schedule another one."""
    with storage.write(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
        if not tx.work._verify_lease(*item.lease, now=clock.utc_now()):
            return stale_result(tx, item)
        tx._check().execute(
            "UPDATE reflection_groups SET job_ref=NULL,proposals_json=NULL WHERE job_ref=?", (item.subject_ref,)
        )
        return work_result(tx.work.mark_obsolete(*item.lease, now=clock.utc_now()))
