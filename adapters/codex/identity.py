"""Map documented Codex cwd to trusted scope subsets from frozen installation config.

A client attached to a shared store has one audience, the owner's, whatever its
cwd; its captures carry the entry's route so a replay is re-checked against the
entry's grants in the store's manifest.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Mapping

from scope_recall.adapters.hermes.audiences import LOCAL_USER_ID
from scope_recall.adapters.hermes.identity import host_scope_payload, principal_ref
from scope_recall.contracts import Origin, TrustedContext, TrustedSourcePrincipal

from .config import CodexInstallationConfig, SharedClientConfig


@dataclass(frozen=True)
class CodexRuntimeAudience:
    allowed_scope_ids: frozenset[str]
    capture_scope_id: str | None
    matched_project_root: str | None
    capability_gaps: tuple[str, ...]
    #: What a capture records for its replay to be re-authorized: the matched
    #: project root, or a shared entry's route.
    host_scope: Mapping[str, str] | None = None
    #: A shared entry's writable scopes, which bound its mutations.  A local
    #: installation has no separate write map.
    writable_scope_ids: frozenset[str] = frozenset()


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path.resolve())))


def _opaque_context(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"context:{prefix}:v1:{hashlib.sha256(payload).hexdigest()}"


def _shared_principal(config: SharedClientConfig, origin: Origin) -> TrustedSourcePrincipal | None:
    # The entry was attached as the owner at this machine: the operator's approval,
    # like the Hermes CLI's, is what verifies the person typing into the client.
    if origin == "human_direct":
        return TrustedSourcePrincipal("human", "verified",
                                      principal_ref("human", config.installation_id, config.host, LOCAL_USER_ID))
    if origin == "assistant_visible":
        return TrustedSourcePrincipal("assistant", "verified",
                                      principal_ref("assistant", config.installation_id, config.agent_id, config.entry_id))
    if origin == "host_generated":
        return TrustedSourcePrincipal("host", "verified", principal_ref("host", config.installation_id, config.entry_id))
    return None


def _source_principal(config: CodexInstallationConfig | SharedClientConfig, origin: Origin) -> TrustedSourcePrincipal:
    if isinstance(config, SharedClientConfig):
        shared = _shared_principal(config, origin)
        if shared is not None:
            return shared
    # Codex Hook v1 has no verified account/user identifier.  A human prompt
    # therefore stays unresolved even in an owner-private visible audience.
    elif origin == "human_direct":
        return TrustedSourcePrincipal("human", "unresolved")
    elif origin == "assistant_visible":
        ref = _opaque_context("codex-assistant", config.installation_id, config.agent_id)
        return TrustedSourcePrincipal("assistant", "verified", ref.replace("context:", "principal:", 1))
    elif origin == "host_generated":
        ref = _opaque_context("codex-host", config.installation_id)
        return TrustedSourcePrincipal("host", "verified", ref.replace("context:", "principal:", 1))
    kind = {
        "tool_observation": "tool",
        "external_document": "document",
    }.get(origin, "unknown")
    return TrustedSourcePrincipal(kind, "unresolved")


def resolve_runtime_audience(config: CodexInstallationConfig | SharedClientConfig, cwd: object) -> CodexRuntimeAudience:
    if isinstance(config, SharedClientConfig):
        audience = config.audience
        return CodexRuntimeAudience(audience.allowed_scope_ids, audience.capture_scope_id, None, (),
                                    host_scope_payload(config.scope), audience.writable_scope_ids)
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
    return CodexRuntimeAudience(frozenset(allowed), capture, matched_root, (), {"cwd": matched_root})


def stored_session_id(config: CodexInstallationConfig | SharedClientConfig, session_id: str) -> str:
    """The session id storage sees.  In a shared store two entries can see the same host
    session id, so there it carries the entry, as a Hermes entry's does."""
    return f"{config.entry_id}:{session_id}" if isinstance(config, SharedClientConfig) else session_id


def trusted_context(
    config: CodexInstallationConfig | SharedClientConfig,
    audience: CodexRuntimeAudience,
    *,
    session_id: str,
    actor_origin: Origin = "human_direct",
    mutation: bool = False,
) -> TrustedContext:
    if not session_id.strip():
        raise ValueError("session_id is required")
    shared = isinstance(config, SharedClientConfig)
    # A shared entry's mutations are bounded by what it may write; a local
    # installation's by its visible scopes, as before.
    scopes = audience.writable_scope_ids if shared and mutation else audience.allowed_scope_ids
    if not scopes:
        raise ValueError("allowed_scope_ids is required")
    stored = stored_session_id(config, session_id)
    anchor = (_opaque_context(f"{config.host}-task", config.installation_id, stored) if shared
              else _opaque_context("codex-task", config.installation_id, session_id))
    return TrustedContext(
        config.to_binding(),
        stored,
        scopes,
        actor_origin,
        task_anchor=anchor,
        source_principal=_source_principal(config, actor_origin),
        entry_id=config.entry_id if shared else None,
    )
