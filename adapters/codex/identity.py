"""Map documented Codex cwd to trusted scope subsets from frozen installation config."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path

from scope_recall.contracts import Origin, TrustedContext, TrustedSourcePrincipal

from .config import CodexInstallationConfig


@dataclass(frozen=True)
class CodexRuntimeAudience:
    allowed_scope_ids: frozenset[str]
    capture_scope_id: str | None
    matched_project_root: str | None
    capability_gaps: tuple[str, ...]


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path.resolve())))


def _opaque_context(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"context:{prefix}:v1:{hashlib.sha256(payload).hexdigest()}"


def _source_principal(config: CodexInstallationConfig, origin: Origin) -> TrustedSourcePrincipal:
    # Codex Hook v1 has no verified account/user identifier.  A human prompt
    # therefore stays unresolved even in an owner-private visible audience.
    if origin == "human_direct":
        return TrustedSourcePrincipal("human", "unresolved")
    if origin == "assistant_visible":
        ref = _opaque_context("codex-assistant", config.installation_id, config.agent_id)
        return TrustedSourcePrincipal("assistant", "verified", ref.replace("context:", "principal:", 1))
    if origin == "host_generated":
        ref = _opaque_context("codex-host", config.installation_id)
        return TrustedSourcePrincipal("host", "verified", ref.replace("context:", "principal:", 1))
    kind = {
        "tool_observation": "tool",
        "external_document": "document",
    }.get(origin, "unknown")
    return TrustedSourcePrincipal(kind, "unresolved")


def resolve_runtime_audience(config: CodexInstallationConfig, cwd: object) -> CodexRuntimeAudience:
    if type(cwd) is not str or not cwd.strip():
        return CodexRuntimeAudience(frozenset(), None, None, ("capability_gap:missing_cwd",))
    try:
        resolved = _norm(Path(cwd).expanduser())
    except OSError:
        return CodexRuntimeAudience(frozenset(), None, None, ("capability_gap:invalid_cwd",))

    matched_root = ""
    matched_scope = ""
    for root, scope_id in config.project_roots.items():
        if resolved == root or resolved.startswith(root + os.sep):
            if len(root) >= len(matched_root):
                matched_root = root
                matched_scope = scope_id
    if not matched_root:
        return CodexRuntimeAudience(frozenset(), None, None, ("capability_gap:unrecognized_project_root",))

    allowed: set[str] = {matched_scope, config.audience_scopes["shared"]}
    if config.allow_owner_private:
        allowed.add(config.audience_scopes["owner_private"])
    allowed = {scope for scope in allowed if scope in config.scope_ids}
    if not allowed:
        return CodexRuntimeAudience(frozenset(), None, matched_root, ("capability_gap:no_allowed_scope",))
    capture = matched_scope if matched_scope in allowed else next(iter(sorted(allowed)))
    return CodexRuntimeAudience(frozenset(allowed), capture, matched_root, ())


def trusted_context(
    config: CodexInstallationConfig,
    audience: CodexRuntimeAudience,
    *,
    session_id: str,
    actor_origin: Origin = "human_direct",
) -> TrustedContext:
    if not session_id.strip():
        raise ValueError("session_id is required")
    if not audience.allowed_scope_ids:
        raise ValueError("allowed_scope_ids is required")
    return TrustedContext(
        config.to_binding(),
        session_id,
        audience.allowed_scope_ids,
        actor_origin,
        task_anchor=_opaque_context("codex-task", config.installation_id, session_id),
        source_principal=_source_principal(config, actor_origin),
    )
