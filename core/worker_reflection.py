"""Persisted reflection generation and independent support stages outside SQLite."""

from __future__ import annotations

import json
from functools import partial

from ..contracts import ContractError
from . import private
from .candidate_lifecycle import SELF_SUBJECTS, model_subject
from .evidence_support import using_support
from .language_resources import prompt_text
from .reflection_storage import decode_observations, publish_observation, reflection_job
from .reflection_validity import live_members
from .semantic_proposals import prepare_support
from .summary_validity import group_signature
from .worker_outcomes import (
    budget_left,
    defer_model_pass,
    finalize_work,
    hold_for_private_mode,
    model_failure,
    stale_result,
    work_result,
)


def reflection_messages(members, *, subject):
    """Bound source input independently of store size; source text never acts as instructions."""
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
        members = json.loads(group["manifest_json"])
        sources = live_members(tx, members)
        if group_signature(tx, members) != group["signature"]:
            raise ContractError("VERSION_CONFLICT", "reflection_dependencies")
    return private.outbound_blocked(storage, clock, context, sources=sources)


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
            sources = live_members(tx, members)
        guard = partial(_guard, storage, clock, context, group)
        if guard():
            return hold_for_private_mode(storage, clock, context, item, started=started, budget=budget)
        verdicts = ()
        proposals = json.loads(group["proposals_json"]) if group["proposals_json"] is not None else None
        with private.using_outbound_check(guard):
            private.require_outbound()
            if proposals is None:
                raw = model.judge(
                    reflection_messages(
                        [dict(m, content=s.event["content"][:512]) for m, s in zip(members, sources, strict=True)],
                        subject=model_subject(group["subject"], sources),
                    ),
                    remaining_seconds=budget_left(started, clock, budget),
                )
                proposals = decode_observations(raw, members, sources)
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
                    model.judge,
                    remaining_seconds=budget_left(started, clock, budget),
                    observation=True,
                )
        return _publish_stage(
            storage, clock, context, item, (group, proposals, verdicts), started=started, budget=budget
        )
    except (ContractError, ValueError, TypeError) as exc:
        if getattr(exc, "code", None) in {"SOURCE_MISSING", "VERSION_CONFLICT", "ACCESS_DENIED"}:
            return _discard_stage(storage, clock, context, item, started=started, budget=budget)
        return finish("retry", getattr(exc, "code", None) or "DERIVATION_INVALID")
    except (OSError, RuntimeError, TimeoutError) as exc:
        return finish(*model_failure(exc))


def _publish_stage(storage, clock, context, item, prepared, *, started, budget):
    """Commit a paid stage once, only under unchanged sources, privacy and lease."""
    group, proposals, verdicts = prepared
    with storage.write(context, remaining_seconds=budget_left(started, clock, budget)) as tx:
        if not tx.work._verify_lease(*item.lease, now=clock.utc_now()):
            return stale_result(tx, item)
        members = json.loads(group["manifest_json"])
        sources = live_members(tx, members)
        if private.source_blocked(private.active_intervals(tx, clock.utc_now()), None) or any(
            private.source_blocked(private.active_intervals(tx, clock.utc_now()), source) for source in sources
        ):
            return work_result(
                tx.work.defer_without_attempt(*item.lease, now=clock.utc_now(), error_code="private_mode", seconds=300)
            )
        if group_signature(tx, members) != group["signature"]:
            tx._check().execute("UPDATE reflection_groups SET job_ref=NULL WHERE group_key=?", (group["group_key"],))
            return work_result(tx.work.mark_obsolete(*item.lease, now=clock.utc_now()))
        index = group["next_proposal"]
        if group["proposals_json"] is None:
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
