from pathlib import Path

import pytest

from scope_recall.contracts import (
    ContractError,
    InstanceBinding,
    TrustedContext,
    TrustedSourcePrincipal,
    validate_capture,
)
from v11_support import source_event


def _context(directory: Path, principal: TrustedSourcePrincipal | None = None) -> TrustedContext:
    binding = InstanceBinding(
        "TEST-agent",
        "TEST-installation",
        directory,
        frozenset({"TEST-shared"}),
        True,
    )
    return TrustedContext(
        binding,
        "TEST-session",
        frozenset({"TEST-shared"}),
        "human_direct",
        source_principal=principal,
    )


def test_trusted_principal_is_injected_when_event_does_not_claim_identity(tmp_path):
    principal = TrustedSourcePrincipal(
        "human",
        "verified",
        principal_ref="principal:TEST-alice",
        display_name="Person A",
    )

    accepted = validate_capture(source_event(), _context(tmp_path, principal))

    assert accepted["source_principal"] == {
        "kind": "human",
        "resolution": "verified",
        "principal_ref": "principal:TEST-alice",
        "display_name": "Person A",
    }


def test_event_cannot_self_assert_or_replace_verified_principal(tmp_path):
    trusted = TrustedSourcePrincipal("human", "verified", principal_ref="principal:TEST-alice")
    spoofed = {
        "kind": "human",
        "resolution": "verified",
        "principal_ref": "principal:TEST-owner",
    }

    with pytest.raises(ContractError, match="source_principal"):
        validate_capture(source_event(source_principal=spoofed), _context(tmp_path, trusted))
    with pytest.raises(ContractError, match="source_principal"):
        validate_capture(source_event(source_principal=spoofed), _context(tmp_path))


def test_unresolved_principal_never_carries_an_authority_ref(tmp_path):
    unresolved = TrustedSourcePrincipal("human", "unresolved", display_name="Current user")
    accepted = validate_capture(source_event(), _context(tmp_path, unresolved))

    assert accepted["source_principal"] == {
        "kind": "human",
        "resolution": "unresolved",
        "display_name": "Current user",
    }
    with pytest.raises(ContractError, match="source_principal"):
        TrustedSourcePrincipal("human", "unresolved", principal_ref="principal:TEST-owner")
