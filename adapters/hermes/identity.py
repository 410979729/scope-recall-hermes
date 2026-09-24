"""Fail-closed Hermes initialize kwargs to immutable manifest-backed identity binding."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
from typing import Mapping, cast

from scope_recall.contracts import (
    ContractError,
    InstanceBinding,
    Origin,
    PrincipalKind,
    SourceContext,
    TrustedContext,
    TrustedSourcePrincipal,
    bounded_source_context,
)

from ..runtime_wiring import RUNTIME_CONFIG_FILENAME
from .audiences import LOCAL_PLATFORMS, LOCAL_USER_ID, approved_local_platforms
from .installation import (
    HermesIdentityError,
    InstallationManifest,
    assert_binding_matches_manifest,
    is_archive_scope,
    load_binding_for_home,
    SCHEMA_VERSION,
)


_NON_PRIMARY_CONTEXTS = frozenset({"subagent", "cron", "flush"})
_UNATTESTED_HUMAN_PLATFORMS = frozenset({"a2a"})


def _opaque_ref(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"principal:{prefix}:v1:{hashlib.sha256(payload).hexdigest()}"


def _context_ref(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"context:{prefix}:v1:{hashlib.sha256(payload).hexdigest()}"


def _scope_component(label: str, value: str) -> str:
    return f"{label}:{len(value)}:{value}"


def _normalize_platform(value: object) -> str:
    text = str(value or "cli").strip().lower()
    return text or "cli"


def _normalize_user_id(platform: str, user_id: object, user_id_alt: object, local_platforms: frozenset[str] = frozenset()) -> str:
    primary = str(user_id or "").strip()
    alternate = str(user_id_alt or "").strip()
    # The host's user_id_alt is another stable id of the same sender in another namespace
    # (Feishu's union_id beside its open_id, Signal's UUID), never a second person, so two
    # different values are not a conflict (#116).  The principal stays user_id: the audience
    # rows and owner principals every earlier release wrote are keyed on it.
    resolved = primary or alternate
    # A session the host names no user for is the owner's on the CLI, and on a
    # local surface the installer approved for this installation.  Anywhere
    # else it is nobody's, and is refused.
    if resolved or platform == "cli" or platform in local_platforms:
        return resolved or LOCAL_USER_ID
    hint = f"; approve it as the owner's local surface with apply-install --local-platform {platform}" if platform in LOCAL_PLATFORMS else ""
    raise HermesIdentityError("user principal required for non-cli platform" + hint)


def _normalize_chat_type(value: object) -> str:
    return str(value or "").strip().lower()


def _matches_owner_principal(manifest: InstallationManifest, *, platform: str, user_id: str) -> bool:
    return any(item["platform"] == platform and item["user_id"] == user_id for item in manifest.owner_principals)


@dataclass(frozen=True)
class HermesRuntimeScope:
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
    allowed_scope_ids: frozenset[str]
    capture_scope_id: str | None
    capability_gaps: tuple[str, ...]
    includes_owner_private: bool
    writable_scope_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class HermesIdentity:
    hermes_home: Path
    session_id: str
    parent_session_id: str
    scope: HermesRuntimeScope
    manifest: InstallationManifest
    binding: InstanceBinding
    owner_private_scope_id: str
    runtime_audience: RuntimeAudience
    read_only: bool

    @property
    def writable_scope_ids(self) -> frozenset[str]:
        return frozenset() if self.read_only else self.runtime_audience.writable_scope_ids

    @property
    def local_scope_id(self) -> str:
        return self.runtime_audience.capture_scope_id or self.owner_private_scope_id

    @property
    def shared_scope_id(self) -> str:
        return self.manifest.audience_scopes.get("shared", self.owner_private_scope_id)

    @property
    def entry_id(self) -> str | None:
        return self.manifest.entry_id

    @property
    def runtime_config_path(self) -> Path | None:
        """An entry's own runtime config: its model routes, beside its pointer.

        The store's directory holds the shared worker's, which is not the
        entry's to load.  ``None`` for a local installation, whose default is
        found beside its store.
        """
        if self.entry_id is None:
            return None
        return self.hermes_home / "scope-recall" / RUNTIME_CONFIG_FILENAME

    def stored_session_id(self, session_id: str | None = None) -> str:
        """The session id a context carries into storage.

        Two entries of a shared store can see the same host session id, so there
        it carries the entry.  Only what reaches storage does: the host
        dispatches its hooks by its own id (``hooks._active_adapter``).
        """
        active = self.session_id if session_id is None else session_id
        return f"{self.entry_id}:{active}" if self.entry_id is not None else active

    def trusted_context(
        self,
        *,
        session_id: str | None = None,
        actor_origin: str | None = None,
        project_id: str | None = None,
        branch_id: str | None = None,
        task_anchor: str | None = None,
        environment_revision: str | None = None,
        recent_messages: tuple[str, ...] = (),
        mutation: bool = False,
    ) -> TrustedContext:
        """Bind read operations to readable scopes, mutations to writable scopes only."""
        active_session = session_id if session_id is not None else self.session_id
        if not active_session.strip():
            raise HermesIdentityError("session_id is required")
        stored_session = self.stored_session_id(active_session)
        scopes = self.writable_scope_ids if mutation else self.runtime_audience.allowed_scope_ids
        if not scopes:
            raise ContractError("ACCESS_DENIED")
        # A platform identity is trusted metadata, but an authenticated remote
        # peer is not an operator confirmation.  Keep the existing human
        # default for platforms whose host contract explicitly represents the
        # operator; A2A has no such attestation at this boundary.  Callers
        # still pass tool_observation/assistant_visible explicitly below.
        resolved_origin = (
            "origin_unknown"
            if actor_origin is None and self.scope.platform in _UNATTESTED_HUMAN_PLATFORMS
            else (actor_origin or "human_direct")
        )
        resolved_task = task_anchor or _context_ref(
            "hermes-task",
            self.binding.installation_id,
            self.scope.platform,
            self.scope.chat_type,
            self.scope.chat_id,
            self.scope.thread_id,
            self.scope.agent_workspace,
            stored_session,
        )
        return TrustedContext(
            self.binding,
            stored_session,
            scopes,
            cast(Origin, resolved_origin),
            project_id=project_id,
            branch_id=branch_id,
            task_anchor=resolved_task,
            environment_revision=environment_revision,
            recent_messages=recent_messages,
            source_principal=self.source_principal(cast(Origin, resolved_origin)),
            entry_id=self.entry_id,
        )

    def source_principal(self, origin: Origin) -> TrustedSourcePrincipal:
        """Map only host-attested metadata to a source actor."""

        if origin == "human_direct":
            if self.scope.platform in _UNATTESTED_HUMAN_PLATFORMS:
                return TrustedSourcePrincipal("human", "unresolved")
            return TrustedSourcePrincipal(
                "human",
                "verified",
                _opaque_ref(
                    "hermes-human",
                    self.binding.installation_id,
                    self.scope.platform,
                    self.scope.user_id,
                ),
            )
        # The owner is one person whichever entry is spoken to; each entry's
        # assistant and host are its own.
        entry = (self.entry_id,) if self.entry_id is not None else ()
        if origin == "assistant_visible":
            return TrustedSourcePrincipal(
                "assistant",
                "verified",
                _opaque_ref(
                    "hermes-assistant", self.binding.installation_id, self.scope.agent_identity, *entry,
                ),
            )
        if origin == "host_generated":
            return TrustedSourcePrincipal(
                "host", "verified", _opaque_ref("hermes-host", self.binding.installation_id, *entry),
            )
        kinds = {
            "tool_observation": "tool",
            "external_document": "document",
            "origin_unknown": "unknown",
            "memory_reinjection": "unknown",
            "imported": "unknown",
        }
        return TrustedSourcePrincipal(cast(PrincipalKind, kinds.get(origin, "unknown")), "unresolved")


def host_scope_payload(scope: HermesRuntimeScope) -> dict[str, str]:
    """What a capture waiting in the inbox keeps of its host scope.

    The entry is kept only in a shared store, so a local store's payload is
    what it was.
    """
    payload = asdict(scope)
    if not payload["entry_id"]:
        del payload["entry_id"]
    return payload


def trusted_source_context(scope: HermesRuntimeScope) -> SourceContext | None:
    """Return the initialized host platform/chat_type only; never message text."""

    return bounded_source_context({"platform": scope.platform, "chat_type": scope.chat_type})


def _route_matches(row: Mapping[str, object], route: Mapping[str, str]) -> bool:
    """Every exact field matches, except that a row naming no gateway_session_key does not pin one.

    The session key is the host's own routing key, built from the platform, chat type and chat
    it names beside it, all of which the row still matches exactly.  Rows the installer and
    most operators write leave it empty, while a gateway sends one on every session, so an
    exact empty key failed every gateway route closed (#124).  A row that names a key still
    matches only that key.  The CLI sends none, so a CLI row is never relaxed: Hermes reports a
    relayed ``local`` gateway session to plugins as ``cli``, with its key, and that is not the CLI.
    """
    unpinned = row.get("gateway_session_key") == "" and route.get("platform") != "cli"
    return all(row.get(field) == value or (field == "gateway_session_key" and unpinned)
               for field, value in route.items())


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
        # never match it (#124).  Named for the operator to correct; it grants nothing.
        near = [row for row in manifest.audiences if row.get("thread_id") in {"", "main"}
                and _route_matches(row, dict(exact_fields, thread_id=row.get("thread_id")))]
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


def bind_hermes_identity(session_id: str, **kwargs: object) -> HermesIdentity:
    """Read-only bind from an existing trusted installation manifest and host kwargs."""

    if type(session_id) is not str or not session_id.strip() or len(session_id) > 240:
        raise HermesIdentityError("session_id is required")
    raw_home = kwargs.get("hermes_home")
    if raw_home is None:
        raise HermesIdentityError("hermes_home is required")
    hermes_home = Path(str(raw_home)).expanduser().resolve()
    if not hermes_home.is_absolute():
        raise HermesIdentityError("hermes_home must be absolute")

    manifest = load_binding_for_home(hermes_home)
    db_path = manifest.data_directory / "memory.sqlite3"
    if not db_path.is_file():
        raise HermesIdentityError("verified core database is required")
    if manifest.entry_id is not None and len(manifest.entry_id) + 1 + len(session_id) > 240:
        raise HermesIdentityError("session_id is required")

    platform = _normalize_platform(kwargs.get("platform"))
    local_platforms = approved_local_platforms(manifest.owner_principals)
    user_id = _normalize_user_id(platform, kwargs.get("user_id"), kwargs.get("user_id_alt"), local_platforms)
    agent_identity = str(kwargs.get("agent_identity") or "").strip()
    agent_workspace = str(kwargs.get("agent_workspace") or "default").strip() or "default"
    agent_context = str(kwargs.get("agent_context") or "primary").strip() or "primary"
    if agent_context not in {"primary", *sorted(_NON_PRIMARY_CONTEXTS)}:
        raise HermesIdentityError(f"unsupported agent_context: {agent_context}")
    if not agent_identity:
        raise HermesIdentityError("agent_identity is required")
    if agent_identity != manifest.agent_id:
        raise HermesIdentityError("agent_identity conflict")

    chat_type = _normalize_chat_type(kwargs.get("chat_type"))
    chat_id = str(kwargs.get("chat_id") or "").strip()
    thread_id = str(kwargs.get("thread_id") or "").strip()
    # The local CLI is an explicit trusted audience.  This normalization is
    # platform-specific; an absent non-CLI chat type never becomes private.
    if platform == "cli":
        chat_type = chat_type or "cli"
        chat_id = chat_id or "local"
        # Missing CLI routing uses the local default; explicit empty remains
        # an unthreaded route, including on session switch.
        if "thread_id" not in kwargs:
            thread_id = "main"
    elif platform in local_platforms and user_id == LOCAL_USER_ID:
        # The host names no chat on a local surface either; the route is the
        # one the installer's grant declares.  A login there is a user like any
        # other and keeps the route the host sent.
        chat_type = chat_type or "private"
        chat_id = chat_id or LOCAL_USER_ID
        if "thread_id" not in kwargs:
            thread_id = "main"
    scope = HermesRuntimeScope(
        platform=platform,
        user_id=user_id,
        chat_type=chat_type,
        chat_id=chat_id,
        thread_id=thread_id,
        gateway_session_key=str(kwargs.get("gateway_session_key") or "").strip(),
        agent_identity=agent_identity,
        agent_workspace=agent_workspace,
        agent_context=agent_context,
        entry_id=manifest.entry_id or "",
    )
    runtime_audience = resolve_runtime_audience(manifest, scope)
    binding = manifest.to_binding()
    assert_binding_matches_manifest(binding, manifest)
    parent_session_id = str(kwargs.get("parent_session_id") or "").strip()
    owner_private_scope_id = manifest.audience_scopes["owner_private"]
    return HermesIdentity(
        hermes_home=hermes_home,
        session_id=session_id,
        parent_session_id=parent_session_id,
        scope=scope,
        manifest=manifest,
        binding=binding,
        owner_private_scope_id=owner_private_scope_id,
        runtime_audience=runtime_audience,
        read_only=agent_context in _NON_PRIMARY_CONTEXTS or not runtime_audience.writable_scope_ids,
    )


def switch_hermes_identity(
    current: HermesIdentity, new_session_id: str, *, parent_session_id: str = "", **kwargs: object,
) -> HermesIdentity:
    """Rebind with initialize's validation; presence, not truthiness, wins.

    A missing principal inherits only on the same platform. An explicit empty
    principal must pass the normal CLI fallback/non-CLI missing-principal gate.
    """
    values: dict[str, object] = asdict(current.scope)
    values.update({key: kwargs[key] for key in values if key in kwargs})
    if "user_id" in kwargs or "user_id_alt" in kwargs:
        values["user_id"] = kwargs.get("user_id")
        values["user_id_alt"] = kwargs.get("user_id_alt")
    elif _normalize_platform(values["platform"]) != current.scope.platform:
        values["user_id"] = ""
    fresh = bind_hermes_identity(
        new_session_id, hermes_home=current.hermes_home,
        parent_session_id=parent_session_id or current.parent_session_id, **values,
    )
    assert_same_installation(current, fresh)
    return fresh


def assert_same_installation(current: HermesIdentity | None, fresh: HermesIdentity) -> None:
    if current is None:
        return
    if current.hermes_home != fresh.hermes_home:
        raise HermesIdentityError("hermes_home rebinding is not allowed")
    if current.entry_id != fresh.entry_id:
        # Two entries can hold the same scopes, and so the same binding.
        raise HermesIdentityError("entry rebinding is not allowed")
    assert_binding_matches_manifest(current.binding, fresh.manifest)
    assert_binding_matches_manifest(fresh.binding, fresh.manifest)
    if current.binding != fresh.binding:
        raise HermesIdentityError("installation binding conflict")
    if current.owner_private_scope_id != fresh.owner_private_scope_id:
        raise HermesIdentityError("owner_private scope conflict")

