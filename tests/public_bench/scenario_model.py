"""Deterministic model ports for pipeline execution, with no semantic quality claim."""

from __future__ import annotations

import json


def proposal(source, *, subject="TEST-azalea", value="琥珀色", kind="decision", predicate="配色"):
    """Quote captured evidence exactly and let the real core decide its authority."""
    return dict(
        kind=kind,
        subject=subject,
        predicate=predicate,
        value_text=value,
        conditions=[],
        statement_kind="decision" if kind == "decision" else "assertion",
        valid_from=source.event["occurred_at"],
        valid_to=None,
        evidence_spans=[dict(source_ref=source.ref, source_revision=source.revision, quote=source.event["content"])],
    )


def envelope(sources, proposals):
    """Use the shipped model-result protocol, including original source references."""
    return json.dumps(
        dict(
            protocol_version="1.1",
            source_refs=[f"{s.ref}@{s.revision}" for s in sources],
            claim_proposals=proposals,
            resume_proposals=[],
            reference_proposals=[],
        ),
        ensure_ascii=False,
    )


class FixtureModel:
    """Propose only explicitly marked fixture facts; every result is validated by Core."""

    def __init__(self, *, sufficient=True, away=False):
        self.calls, self.sufficient, self.away = [], sufficient, away

    def propose(self, sources, **kwargs):
        """Extract the fixture's literal color facts without any external request."""
        self.calls.append("propose")
        if self.away:
            raise TimeoutError("TEST model unavailable")
        proposals = []
        for source in sources:
            text = source.event["content"]
            if text.startswith("TEST-azalea 配色") and "琥珀色" in text:
                proposals.append(proposal(source))
            if text.startswith("TEST-promotion") and "琥珀色" in text:
                proposals.append(proposal(source, subject=text.split()[0], kind="fact"))
            if text.startswith("TEST-distractor"):
                subject, predicate, value = text.rstrip(".").split()
                proposals.append(proposal(source, subject=subject, predicate=predicate, value=value, kind="fact"))
        return envelope(sources, proposals)

    def evaluate_candidate(self, candidate, sources, **kwargs):
        """Return a supported candidate or an explicitly empty evidence verdict."""
        self.calls.append("evaluate_candidate")
        proposals = []
        if self.sufficient:
            subject = candidate.payload["subject"]
            confirmed = [
                source for source in sources if source.event["content"].startswith("补充确认：" + subject + " ")
            ]
            proposals = [proposal(confirmed[-1], subject=subject, kind="fact")] if confirmed else [candidate.payload]
        return envelope(sources, proposals)

    def judge(self, messages, **kwargs):
        """Answer adoption and support stages with fixed, inspectable fixture choices."""
        body = json.loads(messages[-1]["content"])
        self.calls.append(body.get("task", "support"))
        if body.get("task") == "locate_adoption":
            value = dict(adopted=True, fragment_ids=[2], scope="whole", conditions=[], reference_label="方案二")
        elif "adopted_fragments" in body:
            value = dict(decision="先备份配置，再平滑重启，失败时恢复备份。", topic="配置重启策略")
        else:
            value = dict(
                support="supported" if self.sufficient else "uncertain",
                action="new",
                target_ref=None,
                target_revision=None,
            )
        return json.dumps(value, ensure_ascii=False)


def drain(scenes, model, *, rounds=6):
    """Run bounded real worker passes and retain receipts and fake-port call counts."""
    receipts = []
    for _ in range(rounds):
        receipt = scenes.core.drain_worker(
            scenes.context(),
            owner_id="TEST-scenarios-worker",
            consolidation=model,
            max_items=64,
            remaining_seconds=20,
        )
        receipts.append(str(receipt))
    return dict(receipts=receipts, model_calls=list(getattr(model, "calls", ())))


def seed_claim(scenes):
    """Capture a literal decision through a hook, then derive it with the fake port."""
    scenes.hook("codex", "TEST-azalea 配色是琥珀色。")
    model = FixtureModel()
    drain(scenes, model)
    rows = scenes.rows("SELECT * FROM claims WHERE subject='TEST-azalea'")
    if len(rows) != 1:
        raise AssertionError(f"fixture claim was not derived: {rows}")
    return rows[0]["claim_id"]
