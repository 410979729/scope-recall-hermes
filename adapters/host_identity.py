"""Manifest-backed scope and source identity shared by all host adapters.

Principal hashes and scope encodings are persisted identities; changing their spelling would detach existing sources.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from .installation import SCHEMA_VERSION, HermesIdentityError, InstallationManifest, is_archive_scope


def _opaque_ref(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"principal:{prefix}:v1:{hashlib.sha256(payload).hexdigest()}"


def context_ref(prefix: str, *parts: str) -> str:
    """Derive a persisted context identity without changing its namespace or byte encoding."""
    payload = "\x1f".join(parts).encode("utf-8")
    return f"context:{prefix}:v1:{hashlib.sha256(payload).hexdigest()}"


def principal_ref(kind: str, installation_id: str, *parts: str) -> str:
    """A store's opaque ``kind`` principal (human, assistant or host).

    Every entry of a shared store derives its principals here, a local client's
    the way a Hermes home's does: the owner on a route is ``(installation, platform,
    user)``, an entry's assistant and host carry the entry.
    """
    return _opaque_ref(f"hermes-{kind}", installation_id, *parts)


def _matches_owner_principal(manifest: InstallationManifest, *, platform: str, user_id: str) -> bool:
    return any(item["platform"] == platform and item["user_id"] == user_id for item in manifest.owner_principals)


@dataclass(frozen=True)
class HermesRuntimeScope:
    """An immutable host route carried with captures for exact grant revalidation."""

    platform: str
    user_id: str
    chat_type: str
    chat_id: str
    thread_id: str
    agent_identity: str
    agent_workspace: str
    agent_context: str
    gateway_session_key: str = ""
    #: The shared store entry this session speaks for; empty in a local store.  It
    #: travels with a capture that waits in the inbox, so a replay is checked
    #: against the grants of the entry that made it, not the replayer's.
    entry_id: str = ""


@dataclass(frozen=True)
class RuntimeAudience:
    """The readable and writable scopes resolved from explicit route grants."""

    allowed_scope_ids: frozenset[str]
    capture_scope_id: str | None
    capability_gaps: tuple[str, ...]
    includes_owner_private: bool
    writable_scope_ids: frozenset[str] = frozenset()


def host_scope_payload(scope: HermesRuntimeScope) -> dict[str, str]:
    """What a capture waiting in the inbox keeps of its host scope.

    The entry is kept only in a shared store, so a local store's payload is
    what it was.
    """
    payload = asdict(scope)
    if not payload["entry_id"]:
        del payload["entry_id"]
    return payload


def _route_matches(row: Mapping[str, object], route: Mapping[str, str]) -> bool:
    """Every exact field matches, except that a row naming no gateway_session_key does not pin one.

    The session key is the host's own routing key, built from the platform, chat type and chat
    it names beside it, all of which the row still matches exactly.  Rows the installer and
    most operators write leave it empty, while a gateway sends one on every session, so an
    exact empty key would fail every gateway route closed.  A row that names a key still
    matches only that key.  The CLI sends none, so a CLI row is never relaxed: Hermes reports a
    relayed ``local`` gateway session to plugins as ``cli``, with its key, and that is not the CLI.
    A conversation of its local session transport reaches this with no key (``_transport_conversation``).
    """
    unpinned = row.get("gateway_session_key") == "" and route.get("platform") != "cli"
    return all(
        row.get(field) == value or (field == "gateway_session_key" and unpinned) for field, value in route.items()
    )


def resolve_runtime_audience(manifest: InstallationManifest, scope: HermesRuntimeScope) -> RuntimeAudience:
    """Match one exact, installer-approved audience mapping.

    Audience rows are deliberately not wildcards.  In particular, a group is
    allowed only the conversation/project scopes explicitly bound to that
    group, so another group or a private chat cannot inherit its scopes.
    """

    if manifest.schema_version != SCHEMA_VERSION:
        raise HermesIdentityError("explicit v3 audience upgrade required")
    gaps: list[str] = []
    owner_private_scope = manifest.audience_scopes.get("owner_private")
    if not owner_private_scope:
        return RuntimeAudience(frozenset(), None, ("capability_gap:owner_private_unconfigured",), False)
    owner_verified = _matches_owner_principal(manifest, platform=scope.platform, user_id=scope.user_id)

    exact_fields = {
        "platform": scope.platform,
        "user_id": scope.user_id,
        "gateway_session_key": scope.gateway_session_key,
        "chat_type": scope.chat_type,
        "chat_id": scope.chat_id,
        "thread_id": scope.thread_id,
        "agent_workspace": scope.agent_workspace,
    }
    matches = [row for row in manifest.audiences if _route_matches(row, exact_fields)]
    allowed: set[str] = set()
    writable: set[str] = set()
    capture_scope: str | None = None
    includes_owner_private = False
    archive_scopes = getattr(manifest, "archive_scopes", frozenset())
    for row in matches:
        kind = str(row.get("kind") or "conversation")
        if kind == "owner_private" and not owner_verified:
            continue
        allowed.update(
            scope_id
            for scope_id in row["allowed_scope_ids"]
            if scope_id in manifest.scope_ids
            and scope_id not in archive_scopes
            and scope_id not in manifest.retained_scope_ids
            and not is_archive_scope(scope_id)
        )
        writable.update(scope_id for scope_id in row.get("writable_scope_ids", ()) if scope_id in allowed)
        if capture_scope is None:
            cand = str(row["capture_scope_id"])
            if cand in writable:
                capture_scope = cand
        if kind == "owner_private":
            includes_owner_private = True
    if not matches:
        gaps.append("capability_gap:audience_unmapped")
        # A row that differs only in how the plain chat's thread is written: the host sends
        # an empty thread_id for an unthreaded chat, and rows copied from the CLI's "main"
        # never match it.  Named for the operator to correct; it grants nothing.
        near = [
            row
            for row in manifest.audiences
            if row.get("thread_id") in {"", "main"}
            and _route_matches(row, dict(exact_fields, thread_id=str(row.get("thread_id") or "")))
        ]
        if near and scope.thread_id in {"", "main"}:
            gaps.append(f"capability_gap:audience_thread_mismatch:row_says_{near[0].get('thread_id') or 'empty'}")
    elif not allowed:
        gaps.append("capability_gap:owner_unverified")
    if not includes_owner_private and owner_private_scope not in allowed:
        gaps.append("capability_gap:owner_private_denied")
    if not allowed:
        gaps.append("capability_gap:no_allowed_scope")
        return RuntimeAudience(frozenset(), None, tuple(dict.fromkeys(gaps)), False)
    return RuntimeAudience(
        frozenset(allowed),
        capture_scope if capture_scope in writable else None,
        tuple(dict.fromkeys(gaps)),
        includes_owner_private,
        frozenset(writable),
    )
