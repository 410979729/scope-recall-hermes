"""Pure fact-identity contract tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from scope_recall.fact_identity import (
    FactIdentity,
    FactIdentityError,
    build_fact_identity,
    canonical_fact_fingerprint,
    canonical_fact_key,
    normalize_fact_component,
)


def test_fact_key_identifies_a_slot_not_one_particular_value():
    old = build_fact_identity(" Asha ", "Lives   In", "Mumbai")
    new = build_fact_identity("asha", "lives in", "Bangalore")

    assert old.fact_key == new.fact_key
    assert old.value_fingerprint != new.value_fingerprint
    assert old.subject == "asha"
    assert old.predicate == "lives in"


def test_unicode_nfkc_and_whitespace_normalization_are_stable():
    assert normalize_fact_component("  Ｐｒｏｊｅｃｔ\u3000Atlas  ") == "project atlas"
    assert canonical_fact_key("Ｐｒｏｊｅｃｔ Atlas", " Runs  On ") == canonical_fact_key(
        "project atlas",
        "runs on",
    )
    assert canonical_fact_fingerprint("Project Atlas", "runs on", "GPU-A") == canonical_fact_fingerprint(
        "project atlas",
        "RUNS ON",
        "gpu-a",
    )


@pytest.mark.parametrize(
    ("subject", "predicate"),
    [("", "lives in"), ("Asha", ""), ("   ", "lives in")],
)
def test_fact_identity_rejects_missing_slot_components(subject, predicate):
    with pytest.raises(FactIdentityError):
        canonical_fact_key(subject, predicate)


def test_fact_identity_rejects_overlong_components():
    with pytest.raises(FactIdentityError, match="subject"):
        build_fact_identity("s" * 201, "lives in", "Bangalore")
    with pytest.raises(FactIdentityError, match="predicate"):
        build_fact_identity("Asha", "p" * 121, "Bangalore")
    with pytest.raises(FactIdentityError, match="value"):
        build_fact_identity("Asha", "lives in", "v" * 2001)


def test_fact_identity_is_immutable_and_serializable():
    identity = FactIdentity.from_parts("Asha", "lives in", "Bangalore")

    assert identity.as_dict() == {
        "subject": "asha",
        "predicate": "lives in",
        "value": "bangalore",
        "fact_key": identity.fact_key,
        "value_fingerprint": identity.value_fingerprint,
    }
    with pytest.raises(FrozenInstanceError):
        identity.subject = "changed"  # type: ignore[misc]
