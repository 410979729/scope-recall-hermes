"""Check P18 aggregate consistency against retained evidence, without scoring cases."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from pathlib import Path


METHOD_ID = "codex_windows_appserver_native_hooks_v2"
METHOD_SHA256 = "029f8a2eeecf47ddc7d2a4be62457993a9667d58277f5158a4e745bda17653a4"
HERMES_METHOD_ID = "hermes_cli_local_input_v1"
HERMES_METHOD_SHA256 = "119f596efd98476a87401775b692bf0dd7718e72949e0f1813571720c85c57af"
# Frozen protocol labels remain immutable. Each revised entry needs its own
# independently adjudicated artifact; a renamed host label supplies no proof.
PROTOCOL_HOST_METHODS = {
    "hermes_a2a": HERMES_METHOD_ID,
    "codex_windows_desktop": METHOD_ID,
}
CAPS = {
    "api_charge_micro_usd": 20_000_000,
    "go_calls": 8_000,
    "go_input_tokens": 64_000_000,
    "go_output_tokens": 8_000_000,
    "codex_calls": 1_500,
    "codex_input_tokens": 40_000_000,
    "codex_output_tokens": 2_000_000,
    "codex_native_aux_calls": 0,
    "codex_native_aux_input_tokens": 0,
    "codex_native_aux_output_tokens": 0,
}
NATIVE_AUX_AUTHORIZATION_SHA256 = "64aab41980f39cfb2fcc1ae779c2b79fabe033ca05d673769743f91f804c8fb2"
NATIVE_AUX_BATCH = "P18_NATIVE_A_AUX"
ATTEMPT_AUTHORIZATION_SHA256 = "c6912c4d6ae367406bdea27fb944cc9f055d147b2b2aee77515310d971155ba9"
PRIOR_ATTEMPT_AUTHORIZATION_SHA256 = "5f31c11d59e0194f8904d7be8bafdd3b3553c87c94d2964907c465b4a7f40f6f"

GO_AUTHORIZATION_SHA256 = "5753a576fad78b4e0e521a4607305de29837ad58c292f6f62fd4e56e3a60bb87"
HERMES_HISTORICAL_AUTHORIZATION_SHA256 = "4d9e223c8176ad127294f2e0289594efb099d0fc8f7208f4337413edd8e94570"


def effective_budget_caps(budget: dict, base: Path) -> dict:
    """Accept only the recorded Go authorization and a retained frozen policy."""
    result = dict(CAPS)
    attempt_raw = _attempt_authorization(budget, base)
    if attempt_raw is not None:
        authorization = strict_json(attempt_raw)
        mapping = {
            "codex_calls": "call_cap",
            "codex_input_tokens": "input_cap",
            "codex_output_tokens": "output_cap",
        }
        for output, key in mapping.items():
            value = authorization.get(key)
            if type(value) is not int or value <= 0:
                raise ValueError("attempt_authorization_cap_invalid")
            result[output] = value
    native = budget.get("native_aux_authorization")
    if native is not None:
        _, native_raw = _artifact(native, base)
        if hashlib.sha256(native_raw).hexdigest() != NATIVE_AUX_AUTHORIZATION_SHA256:
            raise ValueError("native_aux_authorization_unapproved")
        authorization = strict_json(native_raw)
        if (authorization.get("batch") != NATIVE_AUX_BATCH
                or authorization.get("purchase_authorized") is not False
                or authorization.get("metering_required") is not True
                or authorization.get("unknown_usage_policy") != "reservation_retained"):
            raise ValueError("native_aux_authorization_invalid")
        result.update(authorization["technical_caps"])
    override = budget.get("authorization_override")
    if override is None:
        return result
    if type(override) is not dict:
        raise ValueError("budget_authorization_invalid")
    _, raw = _artifact(override.get("authorization"), base)
    if hashlib.sha256(raw).hexdigest() != GO_AUTHORIZATION_SHA256:
        raise ValueError("budget_authorization_unapproved")
    authorization = strict_json(raw)
    if (authorization.get("purchase_authorized") is not False
            or authorization.get("metering_required") is not True):
        raise ValueError("budget_authorization_policy_invalid")
    _, raw = _artifact(override.get("frozen_budget"), base)
    policy = strict_json(raw)
    if policy.get("batch") != "P18_EVALUATION":
        raise ValueError("budget_batch_invalid")
    mapping = {"api_charge_micro_usd": "cap_micro_usd", "go_calls": "total_call_cap",
               "go_input_tokens": "total_input_cap", "go_output_tokens": "total_output_cap"}
    for output, key in mapping.items():
        value = policy.get(key)
        if type(value) is not int or value <= 0:
            raise ValueError("budget_cap_invalid")
        result[output] = value
    # Go override never silently changes Codex caps; those move only via frozen attempt authorization.
    return result


def strict_json(raw: bytes | str) -> dict:
    def reject_constant(value: str):
        raise ValueError("nonfinite_json_number")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def finite_float(raw_number: str) -> float:
        value = float(raw_number)
        if not math.isfinite(value):
            raise ValueError("nonfinite_json_number")
        return value

    value = json.loads(raw, parse_constant=reject_constant, parse_float=finite_float,
                       object_pairs_hook=unique_object)
    if type(value) is not dict:
        raise ValueError("json_object_required")
    return value


def _artifact(reference: object, base: Path) -> tuple[Path, bytes]:
    if type(reference) is not dict:
        raise ValueError("artifact_reference_missing")
    name, digest = reference.get("path"), reference.get("sha256")
    if type(name) is not str or type(digest) is not str or len(digest) != 64:
        raise ValueError("artifact_reference_invalid")
    relative = Path(name)
    target = (base / relative).resolve()
    if relative.is_absolute() or not target.is_relative_to(base):
        raise ValueError("artifact_outside_bundle")
    raw = target.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("artifact_hash_mismatch")
    return target, raw


def _approved_attempt_raw(raw: bytes | None) -> dict:
    if raw is None:
        return {}
    if hashlib.sha256(raw).hexdigest() not in {ATTEMPT_AUTHORIZATION_SHA256, PRIOR_ATTEMPT_AUTHORIZATION_SHA256}:
        raise ValueError("attempt_authorization_unapproved")
    return strict_json(raw)


def _attempt_authorization(budget: dict, base: Path) -> bytes | None:
    reference = budget.get("codex_attempt_authorization")
    if reference is None:
        return None
    _, raw = _artifact(reference, base)
    _approved_attempt_raw(raw)
    return raw


def _approved_hermes_historical_raw(raw: bytes | None) -> dict | None:
    if raw is None:
        return None
    if hashlib.sha256(raw).hexdigest() != HERMES_HISTORICAL_AUTHORIZATION_SHA256:
        raise ValueError("hermes_historical_authorization_unapproved")
    return strict_json(raw)


def _hermes_historical_authorization(budget: dict, base: Path) -> bytes | None:
    reference = budget.get("hermes_historical_authorization")
    if reference is None:
        return None
    _, raw = _artifact(reference, base)
    _approved_hermes_historical_raw(raw)
    return raw


def _exact_historical_breaches(prefix: str, *authorizations: dict | None) -> tuple[dict, ...]:
    matches = []
    for authorization in authorizations:
        if not authorization:
            continue
        for exact in authorization.get("covered_historical_breaches") or ():
            if type(exact) is not dict:
                raise ValueError("historical_breach_identity_invalid")
            table = exact.get("table")
            if prefix == "codex" and table in (None, "codex_submissions"):
                matches.append(exact)
            elif prefix == "go" and table in (None, "requests", "go"):
                matches.append(exact)
    return tuple(matches)


def _row_matches_exact_breach(found: sqlite3.Row, exact: dict, usage: tuple[object, object, object, object]) -> bool:
    payload = {key: exact[key] for key in exact if key != "table"}
    if any(dict(found).get(key) != value for key, value in payload.items()):
        return False
    return usage == tuple(exact[key] for key in ("reserved_input", "reserved_output", "actual_input", "actual_output"))


def _historical_breach_authorized(db, prefix: str, usage: tuple[object, object, object, object], *, identity, authorizations: tuple[dict | None, ...]) -> bool:
    exacts = _exact_historical_breaches(prefix, *authorizations)
    if not exacts:
        return False
    previous_factory = db.row_factory
    db.row_factory = sqlite3.Row
    try:
        for exact in exacts:
            if prefix == "codex":
                operation_id = exact.get("operation_id")
                if identity != operation_id:
                    continue
                found = db.execute("SELECT * FROM codex_submissions WHERE operation_id=?", (operation_id,)).fetchone()
            else:
                row_id = exact.get("id")
                if identity != row_id:
                    continue
                found = db.execute("SELECT * FROM requests WHERE id=?", (row_id,)).fetchone()
            if found is not None and _row_matches_exact_breach(found, exact, usage):
                return True
    finally:
        db.row_factory = previous_factory
    return False


def ledger_totals(
    path: Path,
    *,
    codex_attempt_authorization: bytes | None = None,
    hermes_historical_authorization: bytes | None = None,
) -> dict[str, int]:
    amendment = _approved_attempt_raw(codex_attempt_authorization)
    hermes_auth = _approved_hermes_historical_raw(hermes_historical_authorization)
    totals = dict.fromkeys(CAPS, 0)
    with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        for table, prefix in (("requests", "go"), ("codex_submissions", "codex")):
            charge = ",charge_micro_usd" if prefix == "go" else ""
            has_batch = prefix == "codex" and "batch" in {row[1] for row in db.execute("PRAGMA table_info(codex_submissions)")}
            batch_column = ",batch" if has_batch else ""
            has_operation = prefix == "codex" and "operation_id" in {row[1] for row in db.execute("PRAGMA table_info(codex_submissions)")}
            has_request_id = prefix == "go" and "id" in {row[1] for row in db.execute("PRAGMA table_info(requests)")}
            identity_column = ""
            if prefix == "codex" and has_operation and amendment:
                identity_column = ",operation_id"
            elif prefix == "go" and has_request_id:
                identity_column = ",id"
            rows = db.execute(
                "SELECT reserved_input,reserved_output,actual_input,actual_output,status"
                + charge + batch_column + identity_column + " FROM " + table
            )
            for row in rows:
                reserved_in, reserved_out, actual_in, actual_out, status = row[:5]
                if any(value is not None and (type(value) is not int or value < 0)
                       for value in row[:4]):
                    raise ValueError("ledger_token_value_invalid")
                effective = (
                    actual_in if actual_in is not None else reserved_in,
                    actual_out if actual_out is not None else reserved_out,
                )
                if any(value is None for value in effective):
                    raise ValueError("ledger_usage_unbounded_or_breached")
                if status == "meter_breach":
                    identity = None
                    if identity_column:
                        identity = row[-1]
                    authorized = _historical_breach_authorized(
                        db,
                        prefix,
                        (reserved_in, reserved_out, actual_in, actual_out),
                        identity=identity,
                        authorizations=(amendment, hermes_auth),
                    )
                    if not authorized:
                        raise ValueError("ledger_usage_unbounded_or_breached")
                category = "codex_native_aux" if has_batch and row[5] == NATIVE_AUX_BATCH else prefix
                totals[category + "_calls"] += 1
                totals[category + "_input_tokens"] += effective[0]
                totals[category + "_output_tokens"] += effective[1]
                if prefix == "go":
                    if type(row[5]) is not int or row[5] < 0:
                        raise ValueError("ledger_charge_invalid")
                    totals["api_charge_micro_usd"] += row[5]
    return totals


def validate_evidence(payload: dict, receipt_path: Path) -> list[str]:
    reasons = []
    base = receipt_path.parent
    method = payload.get("method_adjudication", {})
    try:
        _, raw = _artifact(method.get("artifact"), base)
        adjudication = strict_json(raw)
        if (method.get("method_id") != METHOD_ID
                or hashlib.sha256(raw).hexdigest() != METHOD_SHA256
                or adjudication.get("method", {}).get("id") != METHOD_ID
                or adjudication.get("original_protocol", {}).get("sha256")
                != payload.get("protocol", {}).get("sha256")):
            raise ValueError("method_identity_mismatch")
    except (OSError, ValueError, AttributeError):
        reasons.append("method_artifact_binding_invalid")

    hermes_method = payload.get("hermes_method", {})
    try:
        _, raw = _artifact(hermes_method.get("artifact"), base)
        adjudication = strict_json(raw)
        if (hermes_method.get("id") != HERMES_METHOD_ID
                or hashlib.sha256(raw).hexdigest() != HERMES_METHOD_SHA256
                or adjudication.get("method", {}).get("id") != HERMES_METHOD_ID
                or adjudication.get("original_protocol", {}).get("sha256")
                != payload.get("protocol", {}).get("sha256")):
            raise ValueError("hermes_method_identity_mismatch")
    except (OSError, ValueError, AttributeError):
        reasons.append("hermes_method_artifact_binding_invalid")

    gates = payload.get("gates")
    gate = gates.get("G2", {}) if type(gates) is dict else {}
    try:
        _, raw = _artifact(gate.get("artifact"), base)
        report = strict_json(raw)
        expected = {
            "gate": "G2", "status": "PASS", "evidence_kind": "real",
            "kind": "independent_gate_review", "source": payload.get("source"),
            "protocol": payload.get("protocol"), "method_id": METHOD_ID,
            "hermes_method": hermes_method,
        }
        if any(report.get(key) != value for key, value in expected.items()):
            raise ValueError("G2_report_mismatch")
        if report.get("unresolved_p0_p1") != [] or not report.get("evidence"):
            raise ValueError("G2_evidence_missing")
        for evidence in report["evidence"]:
            _artifact(evidence, base)
    except (OSError, ValueError, AttributeError, TypeError):
        reasons.append("G2_report_binding_invalid")

    budget = payload.get("budget", {})
    try:
        snapshot, _ = _artifact(budget.get("ledger_snapshot"), base)
        totals = ledger_totals(
            snapshot,
            codex_attempt_authorization=_attempt_authorization(budget, base),
            hermes_historical_authorization=_hermes_historical_authorization(budget, base),
        )
        effective_caps = effective_budget_caps(budget, base)
        if budget.get("caps") != effective_caps or budget.get("accounted") != totals:
            raise ValueError("ledger_aggregate_mismatch")
        if any(totals[key] > cap for key, cap in effective_caps.items()):
            raise ValueError("ledger_cap_exceeded")
        if budget.get("codex_monetary_status") != "unavailable":
            raise ValueError("codex_monetary_status_invalid")
        count = budget.get("original_ledger", {}).get("actual_total_calls")
        if type(count) is not int or count > totals["go_calls"] + totals["codex_calls"]:
            raise ValueError("formal_calls_exceed_original_ledger")
    except (OSError, ValueError, AttributeError, sqlite3.Error):
        reasons.append("original_ledger_evidence_invalid")

    scorer = payload.get("scorer_report", {})
    try:
        _, raw = _artifact(scorer.get("artifact"), base)
        report = strict_json(raw)
        expected = {
            "schema": "scope-recall.p18-independent-aggregate.v1",
            "kind": "independent", "run_status": "COMPLETED",
            "source": payload.get("source"), "protocol": payload.get("protocol"),
            "method_adjudication": method, "gates": payload.get("gates"),
            "hermes_method": hermes_method,
            "coverage": payload.get("coverage"), "budget": budget,
            "formal_execution": payload.get("formal_execution"),
            "denominators": scorer.get("denominators"),
        }
        if any(report.get(key) != value for key, value in expected.items()):
            raise ValueError("aggregate_report_mismatch")
        if not isinstance(report.get("reviewer"), str) or not report["reviewer"].strip():
            raise ValueError("independent_reviewer_missing")
    except (OSError, ValueError, AttributeError):
        reasons.append("independent_scorer_content_mismatch")
    return reasons
