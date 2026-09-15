"""Hermes-owned authorization for durable ingress replay."""
from __future__ import annotations

from collections.abc import Callable, Mapping

from scope_recall.contracts import ContractError, InstanceBinding

from .identity import HermesRuntimeScope, resolve_runtime_audience
from .installation import assert_binding_matches_manifest, load_installation_manifest


def build_ingress_authorizer(binding: InstanceBinding) -> Callable[[object], frozenset[str]]:
    """Re-resolve the captured Hermes audience against the current manifest."""

    if not isinstance(binding, InstanceBinding):
        raise TypeError("binding must be InstanceBinding")

    def authorize(raw: object) -> frozenset[str]:
        if raw is None and binding.test_mode:
            return binding.scope_ids
        if not isinstance(raw, Mapping):
            raise ContractError("IDENTITY_UNBOUND", "ingress_host_scope")
        manifest = load_installation_manifest(binding.data_directory.parent)
        assert_binding_matches_manifest(binding, manifest)
        try:
            scope = HermesRuntimeScope(**dict(raw))
        except (TypeError, ValueError) as exc:
            raise ContractError("IDENTITY_UNBOUND", "ingress_host_scope") from exc
        return resolve_runtime_audience(manifest, scope).writable_scope_ids

    return authorize


__all__ = ["build_ingress_authorizer"]
