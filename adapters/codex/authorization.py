"""Codex-owned authorization for durable ingress replay."""
from __future__ import annotations

from collections.abc import Callable, Mapping

from scope_recall.contracts import ContractError, InstanceBinding

from .config import CONFIG_FILENAME, CodexInstallationConfig, load_codex_config
from .identity import resolve_runtime_audience


def _binding_matches(binding: InstanceBinding, config: CodexInstallationConfig) -> bool:
    actual = config.to_binding()
    return (
        actual.agent_id == binding.agent_id
        and actual.installation_id == binding.installation_id
        and actual.data_directory.resolve() == binding.data_directory.resolve()
        and actual.scope_ids == binding.scope_ids
        and actual.test_mode == binding.test_mode
    )


def build_ingress_authorizer(binding: InstanceBinding) -> Callable[[object], frozenset[str]]:
    """Re-resolve a captured Codex cwd against the current installation map."""

    if not isinstance(binding, InstanceBinding):
        raise TypeError("binding must be InstanceBinding")

    def authorize(raw: object) -> frozenset[str]:
        if raw is None and binding.test_mode:
            return binding.scope_ids
        if not isinstance(raw, Mapping) or type(raw.get("cwd")) is not str:
            raise ContractError("IDENTITY_UNBOUND", "ingress_host_scope")
        config = load_codex_config(binding.data_directory.parent / CONFIG_FILENAME)
        if not _binding_matches(binding, config):
            raise ContractError("IDENTITY_UNBOUND", "ingress_binding")
        audience = resolve_runtime_audience(config, raw["cwd"])
        # Codex has no separate write map in v1.  Capture is restricted to the
        # exact mapped project scope; shared/owner visibility does not imply a
        # right to write a replayed human source there.
        return frozenset({audience.capture_scope_id}) if audience.capture_scope_id else frozenset()

    return authorize


__all__ = ["build_ingress_authorizer"]
