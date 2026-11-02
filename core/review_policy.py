"""Scope-owned publication policy and stable identities for owner decisions."""

import hashlib
import json
from dataclasses import replace

from ..contracts import ContractError

PENDING = "owner_review_required"
PENDING_OBSERVATION = "owner_review_observation"
REVIEW_REASONS = frozenset({PENDING_OBSERVATION, PENDING, "owner_review_approved", "owner_review_rejected"})


def digest(value):
    """Hash a canonical native-state snapshot for optimistic concurrency."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def policy(tx, scope_id):
    """Read one authorized scope's latest policy, defaulting to disabled."""
    tx._scope(scope_id)
    row = (
        tx._check()
        .execute("SELECT * FROM scope_review_policy WHERE scope_id=? ORDER BY version DESC LIMIT 1", (scope_id,))
        .fetchone()
    )
    return dict(row, enabled=bool(row["enabled"])) if row else dict(scope_id=scope_id, version=0, enabled=False)


def set_policy(tx, clock, scope_id, enabled, expected_version):
    """Append a policy change only when the reviewed version is still current."""
    if type(enabled) is not bool or type(expected_version) is not int or expected_version < 0:
        raise ContractError("INPUT_INVALID", "review_policy")
    current = policy(tx, scope_id)
    if current["version"] != expected_version:
        raise ContractError("VERSION_CONFLICT", "expected_version")
    tx._check(write=True).execute(
        "INSERT INTO scope_review_policy VALUES(?,?,?,?,?)",
        (scope_id, expected_version + 1, enabled, clock.utc_now(), "local_user_management"),
    )
    return policy(tx, scope_id)


def held_proposal(tx, proposal, scope_id, history):
    """Keep exact held/rejected replays held even after the policy is disabled."""
    for version in reversed(history):
        if version.reason in REVIEW_REASONS and version.payload == proposal:
            return version
    return None


def health_key(category, issue):
    """Identify a computed finding by its original fact versions and candidate name."""
    return ["health", category, [[v["ref"], v["revision"]] for v in issue["facts"]], issue.get("name")]


def health_decided(tx, key):
    """Read a finding's latest owner decision from the existing management audit."""
    row = (
        tx._check()
        .execute(
            "SELECT action FROM management_actions WHERE action IN ('review_decide','review_undo') "
            "AND json_extract(targets_json,'$.key_hash')=? ORDER BY rowid DESC LIMIT 1",
            (digest(key),),
        )
        .fetchone()
    )
    return row is not None and row[0] == "review_decide"


def publication_qualification(tx, scope_id, qualification, previous, proposal, *, source_view=False):
    """Keep alternate automatic promotion paths behind the same owner gate."""
    if source_view:
        return qualification
    if qualification.state == "active" and proposal["kind"] == "observation":
        if qualification.reason == "grounded_observation" and (
            policy(tx, scope_id)["enabled"] or previous is not None and previous.reason == PENDING_OBSERVATION
        ):
            return replace(qualification, state="proposed", reason=PENDING_OBSERVATION)
        return qualification
    if qualification.state != "active" or qualification.reason == "local_user_management" or previous is None:
        return qualification
    held = previous.reason in REVIEW_REASONS
    if previous.state == "proposed" and (held or policy(tx, scope_id)["enabled"]):
        return replace(qualification, state="proposed", reason=PENDING)
    return qualification
