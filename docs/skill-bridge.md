# Skill Bridge

Scope Recall's Skill Bridge turns reviewed Experience playbooks into **skill candidates**. A candidate is a review artifact: it can be inspected, exported, and edited by an operator, but it is not automatically written into the Hermes skill library.

## Candidate schema

```json
{
  "schema_version": "skill_candidate.v1",
  "source_playbook_id": "pb_...",
  "title": "Short skill title",
  "trigger_conditions": ["When this skill should be loaded or used"],
  "steps": ["Actionable step with required context"],
  "verification": ["How to prove the result worked"],
  "pitfalls": ["Known failure mode or boundary"],
  "risk_class": "low|medium|high",
  "evidence_refs": ["playbook:pb_...", "session:...:turn:..."],
  "requires_operator_review": true
}
```

## Safety rules

- Candidates require at least one step, one verification check, one trigger condition, and one evidence reference.
- `risk_class` is limited to `low`, `medium`, or `high`.
- Secret-like content is rejected before redaction so credentials do not enter candidate artifacts.
- `requires_operator_review` is always `true` for generated candidates.
- The bridge never creates, patches, or deletes formal Hermes skills by itself.

## Intended workflow

1. Experience Kernel identifies a reusable, successful playbook.
2. Skill Bridge converts it into a `skill_candidate.v1` payload.
3. An operator reviews the trigger conditions, steps, verification, pitfalls, risk, and evidence.
4. Only after review should a separate skill-management workflow create or update a real Hermes skill.

## Relationship to Experience playbooks

Experience playbooks describe observed procedural knowledge and replay evidence. Skill candidates are a packaging proposal for reusable instructions. The bridge preserves evidence anchors so reviewers can trace a proposed skill back to the playbook and supporting session evidence.

## Feedback loop

Generated or manually created skills can be linked back to playbooks through `related_skills` / `skill_anchors`. When a skill-use failure is reported, `record_skill_feedback()` routes that feedback to the linked playbook run ledger instead of editing the skill file directly.

Rules:

- feedback is recorded as Experience playbook feedback with `skill:<name>` evidence;
- repeated negative outcomes can mark the linked playbook `needs_review` after a configurable threshold;
- the bridge does not delete or rewrite formal Hermes skills automatically;
- operator review remains required before changing a real skill.
