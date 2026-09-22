"""Trusted Hermes installation manifest and explicit install helper."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence

from scope_recall.contracts import ENTRY_ID, InstanceBinding, TrustedContext
from scope_recall.core import CoreConfig, MemoryCore
from scope_recall.core.storage import SQLiteStorage

from .audiences import (
    EXACT_FIELDS, LOCAL_USER_ID, HermesIdentityError, _audience_entry, _normalize_audience_entry,
    is_archive_scope, normalize_local_platforms, normalize_retained_scope_ids, normalize_owner_principals,
)

MANIFEST_FILENAME = "installation.json"
SCHEMA_VERSION = "scope-recall.hermes-installation.v3"
# Exact per-principal/session rows need more room than the old coarse audiences.
_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_FIELD_LEN = 240
_HEX64_RE = re.compile(r"[0-9a-fA-F]{64}")
# Production migrations retain only these two inert audit namespaces.
# Source-to-archive remapping remains the hash-bound, TEST-only workflow.
AUDIT_RETENTION_SCOPES = {
    "orphan_bridge": "archive|reserved:orphan_bridge",
    "digest_audit": "archive|reserved:digest_audit",
}


def bounded_text(
    value: object, *, field: str, required: bool = True, error: type[Exception] = HermesIdentityError
) -> str:
    """A stripped string of at most 240 characters; ``required`` refuses a missing or blank one."""
    if type(value) is not str:
        if required:
            raise error(f"{field} is required")
        return ""
    text = value.strip()
    if required and not text:
        raise error(f"{field} is required")
    if len(text) > _MAX_FIELD_LEN:
        raise error(f"{field} exceeds bounded length")
    return text


def _scope_component(label: str, value: str) -> str:
    return f"{label}:{len(value)}:{value}"


def build_archive_scope_id(source_scope: str) -> str:
    """Generate a deterministic archive-only scope ID within the 240-character binding limit.

    Short originals keep the existing length-prefixed lowercase UTF-8 hex form,
    which is byte-reversible and does not strip or normalize whitespace. When
    that hex ID would exceed 240 characters, a separate archive-only prefix
    plus the full SHA-256 of the exact UTF-8 bytes is used instead so the bound
    ID stays below 240. The complete original string is retained byte-for-byte
    as the archive_source_map key; colliding bound values are rejected by the
    manifest. Hashes alone are not a claim of mathematical injectivity. This
    scope is strictly disallowed in runtime audiences.
    """
    if type(source_scope) is not str or source_scope in ('', '*'):
        raise HermesIdentityError("invalid source scope for archive ID")
    raw = source_scope.encode("utf-8")
    hex_id = f"archive|source:{len(raw)}:{raw.hex()}"
    if len(hex_id) <= _MAX_FIELD_LEN:
        return hex_id
    return f"archive|sha256:{hashlib.sha256(raw).hexdigest()}"


@dataclass(frozen=True)
class InstallationManifest:
    schema_version: str
    installation_id: str
    agent_id: str
    data_directory: Path
    scope_ids: frozenset[str]
    owner_principals: tuple[dict[str, str], ...]
    audience_scopes: dict[str, str]
    audiences: tuple[dict[str, Any], ...]
    test_mode: bool
    hermes_home: Path
    retained_scope_ids: frozenset[str] = frozenset()
    archive_scopes: frozenset[str] = frozenset()
    archive_source_map: dict[str, str] = field(default_factory=dict)
    archive_retention_scopes: dict[str, str] = field(default_factory=dict)
    archive_snapshot_hash: str = ""
    archive_catalog_hash: str = ""
    installation_kind: str = "local"
    #: In a shared store, the entry this manifest is the view of: that entry's own
    #: grants, the store's identity and directory.  ``None`` for a local installation.
    entry_id: str | None = None
    entry_name: str = ""

    def to_binding(self) -> InstanceBinding:
        return InstanceBinding(
            agent_id=self.agent_id,
            installation_id=self.installation_id,
            data_directory=self.data_directory,
            scope_ids=self.scope_ids,
            test_mode=self.test_mode,
            installation_kind=self.installation_kind,
        )


def _installation_id(hermes_home: Path) -> str:
    digest = hashlib.sha256(str(hermes_home.resolve()).encode("utf-8")).hexdigest()
    return f"hermes-install:{digest[:32]}"


def _archive_values(mapping: Mapping[str, str], *, label: str, reject_star: bool, seen: set[str]) -> None:
    """Every value must be a distinct archive-namespace scope; keys are nonempty originals."""
    for key, value in mapping.items():
        if type(key) is not str or not key or (reject_star and key == "*"):
            raise HermesIdentityError(f"{label} map key must be nonempty string" + (" and not '*'" if reject_star else ""))
        if type(value) is not str or not value or not is_archive_scope(value):
            raise HermesIdentityError(f"{label} map value must be nonempty string in archive namespace")
        if value in seen:
            raise HermesIdentityError("archive map value collision")
        seen.add(value)


def _validate_archive_fields(manifest: InstallationManifest, *, present: bool, test_mode: object) -> None:
    """Archive scopes are inert audit/import namespaces: hash-bound, TEST-only unless audit retention only."""
    audit_retention_only = (
        not manifest.archive_source_map
        and manifest.archive_snapshot_hash == ""
        and manifest.archive_catalog_hash == ""
        and manifest.archive_retention_scopes == AUDIT_RETENTION_SCOPES
    )
    if present and not audit_retention_only:
        if test_mode is not True:
            raise HermesIdentityError("archive fields require literal test_mode=True")
        for label, digest in (
            ("archive_snapshot_hash", manifest.archive_snapshot_hash),
            ("archive_catalog_hash", manifest.archive_catalog_hash),
        ):
            if type(digest) is not str or not _HEX64_RE.fullmatch(digest):
                raise HermesIdentityError(f"invalid or missing {label}")

    archive_values: set[str] = set()
    _archive_values(manifest.archive_source_map, label="source", reject_star=True, seen=archive_values)
    _archive_values(manifest.archive_retention_scopes, label="retention", reject_star=False, seen=archive_values)
    archive = set(manifest.archive_scopes)
    if archive != archive_values:
        raise HermesIdentityError("archive_scopes must exactly equal the union of source and retention map values")
    if not archive.issubset(manifest.scope_ids):
        raise HermesIdentityError("archive_scopes must be a subset of registered scope_ids")

    mapped = {scope_id for row in manifest.audiences for scope_id in row["allowed_scope_ids"]}
    runtime = mapped | set(manifest.audience_scopes.values())
    for scope_id in runtime:
        if is_archive_scope(scope_id) or scope_id in archive:
            raise HermesIdentityError("runtime scopes cannot use archive namespace or overlap archive_scopes")
    retained = set(manifest.retained_scope_ids)
    if retained & (runtime | archive):
        raise HermesIdentityError("retained_scope_ids must not overlap runtime or archive scopes")
    if set(manifest.scope_ids) != (mapped | archive | retained):
        raise HermesIdentityError("registered scope_ids must equal audience union retained union archive")


def _build_audience_scope_ids(
    *,
    platform: str,
    user_id: str,
    agent_identity: str,
    agent_workspace: str,
    project_id: str,
    conversation_key: str,
) -> dict[str, str]:
    shared = "|".join(
        [
            _scope_component("audience", "shared"),
            _scope_component("platform", platform),
            _scope_component("workspace", agent_workspace),
            _scope_component("agent", agent_identity),
        ]
    )
    owner_private = "|".join(
        [
            _scope_component("audience", "owner_private"),
            _scope_component("platform", platform),
            _scope_component("user", user_id),
            _scope_component("workspace", agent_workspace),
            _scope_component("agent", agent_identity),
        ]
    )
    project = "|".join(
        [
            _scope_component("audience", "project"),
            _scope_component("platform", platform),
            _scope_component("workspace", agent_workspace),
            _scope_component("agent", agent_identity),
            _scope_component("project", project_id),
        ]
    )
    conversation = "|".join(
        [
            _scope_component("audience", "conversation"),
            _scope_component("platform", platform),
            _scope_component("key", conversation_key or "default"),
        ]
    )
    return {
        "owner_private": owner_private,
        "shared": shared,
        "project": project,
        "conversation": conversation,
    }


def _grant(scope_id: str, *, kind: str, chat_type: str, chat_id: str, route: dict[str, str]) -> dict[str, Any]:
    """One exact audience row whose read, write and capture grants are the same single scope."""
    return _audience_entry(
        **route,
        chat_type=chat_type,
        chat_id=chat_id,
        thread_id="main",
        allowed_scope_ids=[scope_id],
        writable_scope_ids=[scope_id],
        capture_scope_id=scope_id,
        kind=kind,
    )


def _local_grant(owner_private_scope: str, *, platform: str, agent_workspace: str) -> dict[str, Any]:
    """The owner's private scope on a local surface, routed the way the adapter routes a session there."""
    route = dict(platform=platform, user_id=LOCAL_USER_ID, gateway_session_key="", agent_workspace=agent_workspace)
    return _grant(owner_private_scope, kind="owner_private", chat_type="private", chat_id=LOCAL_USER_ID, route=route)


def unapproved_local_platforms(manifest: InstallationManifest, platforms: Sequence[str], *, agent_workspace: str) -> tuple[str, ...]:
    """Which of these local surfaces the manifest lacks the owner principal or the grant for."""
    missing = []
    for platform in normalize_local_platforms(list(platforms)):
        grant = _local_grant(manifest.audience_scopes["owner_private"], platform=platform, agent_workspace=agent_workspace)
        routed = any(all(row.get(name) == grant[name] for name in EXACT_FIELDS) for row in manifest.audiences)
        if dict(platform=platform, user_id=LOCAL_USER_ID) not in manifest.owner_principals or not routed:
            missing.append(platform)
    return tuple(missing)


def approve_local_platforms(manifest: InstallationManifest, platforms: Sequence[str], *, agent_workspace: str) -> InstallationManifest:
    """The same manifest with each local surface approved as the owner's own.

    Approval is two exact entries and nothing else: the owner principal
    ``(platform, "local")`` and one grant of the owner's private scope on that
    route.  The scope is one the manifest already registers, so the instance
    binding, and with it the store, is unchanged.  A surface already approved,
    or a route somebody declared by hand, is left as it is.
    """
    principals = list(manifest.owner_principals)
    rows = list(manifest.audiences)
    for platform in unapproved_local_platforms(manifest, platforms, agent_workspace=agent_workspace):
        principal = dict(platform=platform, user_id=LOCAL_USER_ID)
        if principal not in principals:
            principals.append(principal)
        grant = _local_grant(manifest.audience_scopes["owner_private"], platform=platform, agent_workspace=agent_workspace)
        if not any(all(row.get(name) == grant[name] for name in EXACT_FIELDS) for row in rows):
            rows.append(grant)
    return replace(manifest, owner_principals=normalize_owner_principals(principals), audiences=tuple(rows))


def _archive_maps(
    archive_source_scopes: Sequence[str] | Mapping[str, str] | None,
    archive_retention_scopes: Mapping[str, str] | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Source originals to bound archive IDs, and the named retention namespaces."""
    source: dict[str, str] = {}
    if isinstance(archive_source_scopes, Mapping):
        for original, archive in archive_source_scopes.items():
            if type(original) is not str or type(archive) is not str:
                raise HermesIdentityError("archive scope map keys and values must be strings")
            source[original] = archive
    elif archive_source_scopes is not None:
        for original in archive_source_scopes:
            if type(original) is not str:
                raise HermesIdentityError("archive scope must be string")
            if original in source:
                raise HermesIdentityError("duplicate sequence source identifiers")
            source[original] = build_archive_scope_id(original)
    retention: dict[str, str] = {}
    if archive_retention_scopes is not None:
        if not isinstance(archive_retention_scopes, Mapping):
            raise HermesIdentityError("archive retention must be a mapping")
        for key, value in archive_retention_scopes.items():
            if type(key) is not str or type(value) is not str:
                raise HermesIdentityError("archive retention map keys and values must be strings")
            retention[key] = value
    return source, retention


def build_installation_manifest(
    hermes_home: Path,
    *,
    agent_id: str,
    platform: str = "cli",
    user_id: str = "local",
    agent_workspace: str = "default",
    project_id: str | None = None,
    conversation_key: str = "default",
    gateway_session_key: str = "",
    retained_scope_ids: Sequence[str] = (),
    owner_principals: Sequence[Mapping[str, str]] | None = None,
    audiences: Sequence[Mapping[str, Any]] | None = None,
    local_platforms: Sequence[str] = (),
    test_mode: bool = False,
    archive_source_scopes: Sequence[str] | Mapping[str, str] | None = None,
    archive_retention_scopes: Mapping[str, str] | None = None,
    archive_snapshot_hash: str | None = None,
    archive_catalog_hash: str | None = None,
) -> InstallationManifest:
    """Build v3 grants; supplied audiences are the complete exact runtime set.

    v2 files require an explicit rebuild with attested principal/session/write
    grants. Retained IDs register originals for import, never runtime access.
    ``local_platforms`` approves host surfaces that name no user as the owner's
    own (see ``approve_local_platforms``).
    """
    home = hermes_home.expanduser().resolve()
    if not home.is_absolute():
        raise HermesIdentityError("hermes_home must be absolute")
    agent = bounded_text(agent_id, field="agent_id")
    plat = bounded_text(platform or "cli", field="platform")
    owner = bounded_text(user_id or "local", field="user_id", required=False) or "local"
    principals = normalize_owner_principals(
        list(owner_principals) if owner_principals is not None else [dict(platform=plat, user_id=owner)]
    )
    if dict(platform=plat, user_id=owner) not in principals:
        raise HermesIdentityError("primary owner must be included in owner_principals")
    workspace = bounded_text(agent_workspace or "default", field="agent_workspace", required=False) or "default"
    project = bounded_text(project_id or workspace, field="project_id", required=False) or workspace
    conversation = bounded_text(conversation_key or "default", field="conversation_key", required=False) or "default"
    if plat != "cli" and conversation == "default":
        # Retain the historical fixture's explicitly named group-1 audience;
        # all other groups still require an installer supplied mapping.
        conversation = "group-1"
    scopes = _build_audience_scope_ids(
        platform=plat,
        user_id=owner,
        agent_identity=agent,
        agent_workspace=workspace,
        project_id=project,
        conversation_key=conversation,
    )
    # The owner grant is always explicit.  Other rows are exact entries the
    # trusted installer supplies; no non-CLI wildcard is synthesized.
    route = dict(platform=plat, user_id=owner, gateway_session_key=gateway_session_key, agent_workspace=workspace)
    owner_chat = ("cli", "local") if plat == "cli" else ("private", owner)
    rows = [_grant(scopes["owner_private"], kind="owner_private", chat_type=owner_chat[0], chat_id=owner_chat[1], route=route)]
    if audiences is not None:
        if project_id:
            raise HermesIdentityError("explicit audiences cannot be mixed with project convenience grants")
        rows = [_normalize_audience_entry(item) for item in audiences]
        if not rows:
            raise HermesIdentityError("explicit audiences must not be empty")
    else:
        # The historical convenience arguments become explicit rows.
        if plat != "cli":
            rows.append(_grant(scopes["conversation"], kind="conversation", chat_type="group", chat_id=conversation, route=route))
        if project_id:
            rows.append(_grant(scopes["project"], kind="project", chat_type="project", chat_id=project, route=route))
    audience_scopes: dict[str, str] = {}
    for row in rows:
        audience_scopes.setdefault(str(row.get("kind") or "conversation"), str(row["capture_scope_id"]))
    if "owner_private" not in audience_scopes:
        raise HermesIdentityError("an explicit owner_private audience is required")

    mapped = frozenset(scope_id for row in rows for scope_id in row["allowed_scope_ids"])
    source_map, retention = _archive_maps(archive_source_scopes, archive_retention_scopes)
    archive = frozenset(source_map.values()) | frozenset(retention.values())
    retained = normalize_retained_scope_ids(retained_scope_ids)
    manifest = InstallationManifest(
        schema_version=SCHEMA_VERSION,
        installation_id=_installation_id(home),
        agent_id=agent,
        data_directory=(home / "scope-recall").resolve(),
        scope_ids=mapped | archive | retained,
        owner_principals=principals,
        audience_scopes=audience_scopes,
        audiences=tuple(rows),
        test_mode=bool(test_mode),
        hermes_home=home,
        retained_scope_ids=retained,
        archive_scopes=archive,
        archive_source_map=source_map,
        archive_retention_scopes=retention,
        archive_snapshot_hash=archive_snapshot_hash if archive_snapshot_hash is not None else "",
        archive_catalog_hash=archive_catalog_hash if archive_catalog_hash is not None else "",
    )
    _validate_archive_fields(
        manifest,
        present=any(
            value is not None
            for value in (archive_source_scopes, archive_retention_scopes, archive_snapshot_hash, archive_catalog_hash)
        ),
        test_mode=test_mode,
    )
    return approve_local_platforms(manifest, local_platforms, agent_workspace=workspace)


def manifest_payload(manifest: InstallationManifest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": manifest.schema_version,
        "installation_id": manifest.installation_id,
        "agent_id": manifest.agent_id,
        "data_directory": str(manifest.data_directory),
        "scope_ids": sorted(manifest.scope_ids),
        "retained_scope_ids": sorted(manifest.retained_scope_ids),
        "owner_principals": [dict(item) for item in manifest.owner_principals],
        "audience_scopes": dict(manifest.audience_scopes),
        "audiences": [dict(item) for item in manifest.audiences],
        "test_mode": manifest.test_mode,
        "hermes_home": str(manifest.hermes_home),
    }
    if manifest.archive_scopes:
        payload["archive_scopes"] = sorted(manifest.archive_scopes)
        payload["archive_source_map"] = dict(manifest.archive_source_map)
        if manifest.archive_retention_scopes:
            payload["archive_retention_scopes"] = dict(manifest.archive_retention_scopes)
    if manifest.archive_snapshot_hash:
        payload["archive_snapshot_hash"] = manifest.archive_snapshot_hash
    if manifest.archive_catalog_hash:
        payload["archive_catalog_hash"] = manifest.archive_catalog_hash
    return payload


def write_installation_manifest(manifest: InstallationManifest) -> Path:
    if manifest.installation_kind != "local":
        # Its data directory is the shared store's, whose own manifest this would overwrite.
        raise HermesIdentityError("a shared store entry is not written as an installation manifest")
    manifest.data_directory.mkdir(parents=True, exist_ok=True)
    path = manifest.data_directory / MANIFEST_FILENAME
    encoded = json.dumps(manifest_payload(manifest), ensure_ascii=False, sort_keys=True, indent=2)
    if len(encoded.encode("utf-8")) > _MAX_MANIFEST_BYTES:
        raise HermesIdentityError("installation manifest exceeds bounded size")
    path.write_text(encoded + "\n", encoding="utf-8")
    return path


def _scope_id_list(raw: object) -> frozenset[str]:
    if not isinstance(raw, list) or not raw:
        raise HermesIdentityError("installation manifest scope_ids invalid")
    seen: set[str] = set()
    for scope_id in raw:
        if type(scope_id) is not str or not scope_id:
            raise HermesIdentityError("scope_id must be a nonempty string")
        if scope_id in seen:
            raise HermesIdentityError("duplicate scope_ids")
        seen.add(scope_id)
    return frozenset(seen)


def _audience_scope_map(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or not raw:
        raise HermesIdentityError("installation manifest audience_scopes invalid")
    if "owner_private" not in raw:
        raise HermesIdentityError("installation manifest audience_scopes incomplete")
    for key, value in raw.items():
        if type(key) is not str or not key:
            raise HermesIdentityError("audience_scopes key must be nonempty string")
        if type(value) is not str or not value:
            raise HermesIdentityError("audience_scopes value must be nonempty string")
    return dict(raw)


def _audience_rows(raw: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, list) or not raw:
        raise HermesIdentityError("installation manifest audiences invalid")
    for item in raw:
        if not isinstance(item, dict):
            raise HermesIdentityError("audience entry must be dict")
        allowed = item.get("allowed_scope_ids")
        if not isinstance(allowed, list):
            raise HermesIdentityError("audience allowed_scope_ids must be a list")
        seen: list[str] = []
        for scope_id in allowed:
            if type(scope_id) is not str or not scope_id:
                raise HermesIdentityError("audience allowed_scope_ids entry must be nonempty string")
            if scope_id in seen:
                raise HermesIdentityError("audience allowed_scope_ids contains duplicates")
            seen.append(scope_id)
        if type(item.get("capture_scope_id")) is not str:
            raise HermesIdentityError("audience capture_scope_id must be an explicit string")
    return tuple(_normalize_audience_entry(item) for item in raw)


def _archive_scope_list(raw: object) -> frozenset[str]:
    if type(raw) is not list:
        raise HermesIdentityError("installation manifest archive_scopes must be a list")
    for scope_id in raw:
        if type(scope_id) is not str:
            raise HermesIdentityError("installation manifest archive_scopes entry must be str")
    if len(raw) != len(set(raw)):
        raise HermesIdentityError("installation manifest archive_scopes contains duplicates")
    return frozenset(raw)


def _archive_map(name: str) -> Callable[[object], dict[str, str]]:
    def check(raw: object) -> dict[str, str]:
        if type(raw) is not dict:
            raise HermesIdentityError(f"installation manifest {name} must be a dict")
        return dict(raw)

    return check


# Manifest field -> validator producing the manifest attribute.  Required
# fields are checked in this order; archive fields are optional but, when
# present, may not be null.
_REQUIRED_FIELDS: tuple[tuple[str, Callable[[object], Any]], ...] = (
    ("scope_ids", _scope_id_list),
    ("audience_scopes", _audience_scope_map),
    ("audiences", _audience_rows),
    ("owner_principals", normalize_owner_principals),
)
_ARCHIVE_FIELDS: tuple[tuple[str, Callable[[object], Any], Callable[[], Any]], ...] = (
    ("archive_scopes", _archive_scope_list, frozenset),
    ("archive_source_map", _archive_map("archive_source_map"), dict),
    ("archive_retention_scopes", _archive_map("archive_retention_scopes"), dict),
    ("archive_snapshot_hash", lambda raw: raw, str),
    ("archive_catalog_hash", lambda raw: raw, str),
)


def _archive_field(payload: dict[str, Any], name: str, check: Callable[[object], Any], absent: Callable[[], Any]) -> Any:
    if name not in payload:
        return absent()
    if payload[name] is None:
        raise HermesIdentityError(f"explicit null {name} is not an absent field")
    return check(payload[name])


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise HermesIdentityError("installation manifest is required")
    if path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise HermesIdentityError("installation manifest exceeds bounded size")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HermesIdentityError("installation manifest is invalid") from exc
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise HermesIdentityError("unsupported installation manifest schema; explicit v3 upgrade required")
    return payload


def load_installation_manifest(hermes_home: Path | str) -> InstallationManifest:
    home = Path(str(hermes_home)).expanduser().resolve()
    payload = _read_manifest(home / "scope-recall" / MANIFEST_FILENAME)
    data_directory = Path(str(payload.get("data_directory") or "")).expanduser().resolve()
    if data_directory != (home / "scope-recall").resolve():
        raise HermesIdentityError("installation manifest data_directory mismatch")
    return _manifest_from_payload(payload, home=home, data_directory=data_directory)


def load_archived_installation(path: Path | str, hermes_home: Path | str) -> InstallationManifest:
    """A home's own installation manifest, read from where it was moved aside.

    Everything ``load_installation_manifest`` checks except where the file is:
    it must still be that home's, by its id.  Used to carry the home's grants
    into a shared store; nothing binds with it.
    """
    home = Path(str(hermes_home)).expanduser().resolve()
    file = Path(str(path)).expanduser().resolve()
    return _manifest_from_payload(_read_manifest(file), home=home, data_directory=file.parent)


def _manifest_from_payload(payload: dict[str, Any], *, home: Path, data_directory: Path) -> InstallationManifest:
    fields = {name: check(payload.get(name)) for name, check in _REQUIRED_FIELDS}
    archive = {name: _archive_field(payload, name, check, absent) for name, check, absent in _ARCHIVE_FIELDS}
    test_mode = payload.get("test_mode")
    manifest = InstallationManifest(
        schema_version=SCHEMA_VERSION,
        installation_id=bounded_text(payload.get("installation_id"), field="installation_id"),
        agent_id=bounded_text(payload.get("agent_id"), field="agent_id"),
        data_directory=data_directory,
        test_mode=bool(test_mode),
        hermes_home=home,
        retained_scope_ids=normalize_retained_scope_ids(payload.get("retained_scope_ids")),
        **fields,
        **archive,
    )
    _validate_archive_fields(
        manifest,
        present=any(name in payload for name, _check, _absent in _ARCHIVE_FIELDS),
        test_mode=test_mode,
    )
    if manifest.installation_id != _installation_id(home):
        raise HermesIdentityError("installation manifest installation_id mismatch")
    return manifest


def assert_binding_matches_manifest(binding: InstanceBinding, manifest: InstallationManifest) -> None:
    expected = manifest.to_binding()
    if (
        binding.agent_id,
        binding.installation_id,
        binding.data_directory.resolve(),
        binding.scope_ids,
        binding.test_mode,
        binding.installation_kind,
    ) != (
        expected.agent_id,
        expected.installation_id,
        expected.data_directory.resolve(),
        expected.scope_ids,
        expected.test_mode,
        expected.installation_kind,
    ):
        raise HermesIdentityError("core binding does not match installation manifest")


def assert_core_binding_matches(core: MemoryCore, binding: InstanceBinding) -> None:
    if core.config.binding != binding:
        raise HermesIdentityError("injected core binding mismatch")


# -- shared store -----------------------------------------------------------------------
#
# A shared store's manifest lives in the store's own directory and keeps every
# entry's grants, each exactly as that entry's own installation had them: the
# store is one, the audiences stay per entry.  So an entry's binding is its own
# scope set and does not change when another entry attaches, which matters
# because a running entry re-reads the manifest on every session switch and
# compares bindings.  An entry's home keeps only a pointer, attachment.json.

ATTACHMENT_FILENAME = "attachment.json"
ATTACHMENT_SCHEMA = "scope-recall.attachment/1"
SHARED_SCHEMA_VERSION = "scope-recall.shared-installation.v1"
SHARED_ID = re.compile(r"shared-install:[0-9a-f]{32}")
#: Every entry's audience rows; an instance migrated from 2.x brings about a hundred.
_MAX_SHARED_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_ATTACHMENT_BYTES = 4096
_MAX_DISPLAY_NAME = 32


@dataclass(frozen=True)
class Attachment:
    """The pointer that makes a Hermes home an entry of a shared store."""

    root: Path
    entry_id: str
    display_name: str


def _read_json(path: Path, *, limit: int, what: str) -> dict[str, Any]:
    if not path.is_file():
        raise HermesIdentityError(f"{what} is required")
    if path.stat().st_size > limit:
        raise HermesIdentityError(f"{what} exceeds bounded size")
    for attempt in range(3):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            break
        except PermissionError as exc:
            # Windows refuses a read while an attach replaces the file; the
            # replace takes milliseconds, and a session switch should not fail on it.
            if attempt == 2:
                raise HermesIdentityError(f"{what} is invalid") from exc
            time.sleep(0.02)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HermesIdentityError(f"{what} is invalid") from exc
    if not isinstance(payload, dict):
        raise HermesIdentityError(f"{what} is invalid")
    return payload


def _entry_id(value: object) -> str:
    if type(value) is not str or not ENTRY_ID.fullmatch(value):
        raise HermesIdentityError("entry_id must be 2 to 32 lowercase letters, digits or hyphens, starting with a letter")
    return value


def _display_name(value: object) -> str:
    if type(value) is not str or not value.strip() or len(value.strip()) > _MAX_DISPLAY_NAME:
        raise HermesIdentityError("display_name is required and at most 32 characters")
    return value.strip()


def _same_path(left: Path | str, right: Path | str) -> bool:
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def attachment_path(hermes_home: Path) -> Path:
    return hermes_home / "scope-recall" / ATTACHMENT_FILENAME


def _points_here(hermes_home: Path | str, store: Path, entry_id: str) -> bool:
    """Whether a home's pointer still names this store and entry; an unreadable one counts as yes."""
    try:
        attachment = read_attachment(hermes_home)
    except HermesIdentityError:
        return True
    return attachment is not None and attachment.entry_id == entry_id and _same_path(attachment.root, store)


def read_attachment(hermes_home: Path | str) -> Attachment | None:
    """This home's pointer to a shared store, or ``None`` when it has none."""
    path = attachment_path(Path(str(hermes_home)).expanduser().resolve())
    if not os.path.lexists(path):
        return None
    payload = _read_json(path, limit=_MAX_ATTACHMENT_BYTES, what="shared store attachment")
    if payload.get("schema") != ATTACHMENT_SCHEMA:
        raise HermesIdentityError("unsupported shared store attachment schema")
    if payload.get("host") != "hermes":
        raise HermesIdentityError("shared store attachment is not a Hermes entry")
    root = Path(str(payload.get("root") or ""))
    if not root.is_absolute():
        raise HermesIdentityError("shared store attachment root must be absolute")
    return Attachment(root.resolve(), _entry_id(payload.get("entry_id")), _display_name(payload.get("display_name")))


def read_shared_payload(root: Path | str) -> dict[str, Any]:
    """A shared store's manifest, checked as a whole."""
    store = Path(str(root)).expanduser().resolve()
    return _checked_shared_payload(
        _read_json(store / MANIFEST_FILENAME, limit=_MAX_SHARED_MANIFEST_BYTES, what="shared store manifest"))


def _checked_shared_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("schema_version") != SHARED_SCHEMA_VERSION or payload.get("installation_kind") != "shared":
        raise HermesIdentityError("not a shared store manifest")
    if type(payload.get("installation_id")) is not str or not SHARED_ID.fullmatch(payload["installation_id"]):
        raise HermesIdentityError("shared store installation_id invalid")
    bounded_text(payload.get("agent_id"), field="agent_id")
    if type(payload.get("test_mode")) is not bool:
        raise HermesIdentityError("shared store test_mode must be a boolean")
    entries = payload.get("entries")
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise HermesIdentityError("shared store entries invalid")
    ids = [_entry_id(entry.get("entry_id")) for entry in entries]
    if len(set(ids)) != len(ids):
        raise HermesIdentityError("shared store entries repeat an entry_id")
    carried: set[str] = set()
    for entry in entries:
        carried |= _scope_id_list(entry.get("scope_ids"))
    scope_ids = payload.get("scope_ids")
    if (not isinstance(scope_ids, list) or any(type(scope_id) is not str for scope_id in scope_ids)
            or set(scope_ids) != carried or len(set(scope_ids)) != len(scope_ids)):
        raise HermesIdentityError("shared store scope_ids must be exactly its entries' scopes")
    return payload


def shared_entry_manifest(root: Path | str, entry_id: str, *, hermes_home: Path | None = None) -> InstallationManifest:
    """One entry's view of a shared store: its own grants, the store's identity and directory.

    With ``hermes_home`` the view is for that home binding: the entry must be
    attached from it and not detached, so a pointer copied into another home
    binds nothing.  Without it the view is for re-checking a capture the entry
    made, which a detached entry's captures still get.
    """
    store = Path(str(root)).expanduser().resolve()
    payload = read_shared_payload(store)
    record = next((entry for entry in payload["entries"] if entry["entry_id"] == entry_id), None)
    if record is None:
        raise HermesIdentityError("shared store has no such entry")
    if hermes_home is not None:
        if record.get("detached_at"):
            raise HermesIdentityError("shared store entry is detached")
        if not _same_path(bounded_text(record.get("home"), field="home"), hermes_home):
            raise HermesIdentityError("shared store entry belongs to another home")
    return _entry_view(store, payload, record)


def _entry_view(store: Path, payload: dict[str, Any], record: Mapping[str, Any]) -> InstallationManifest:
    if record.get("host") != "hermes":
        raise HermesIdentityError("shared store entry is not a Hermes entry")
    home = Path(bounded_text(record.get("home"), field="home"))
    if not home.is_absolute():
        raise HermesIdentityError("shared store entry home must be absolute")
    fields = {name: check(record.get(name)) for name, check in _REQUIRED_FIELDS}
    manifest = InstallationManifest(
        schema_version=SCHEMA_VERSION,
        installation_id=payload["installation_id"],
        agent_id=payload["agent_id"],
        data_directory=store,
        test_mode=payload["test_mode"],
        hermes_home=home.resolve(),
        installation_kind="shared",
        entry_id=_entry_id(record.get("entry_id")),
        entry_name=_display_name(record.get("display_name")),
        **fields,
    )
    _validate_archive_fields(manifest, present=False, test_mode=payload["test_mode"])
    return manifest


def load_binding_for_home(hermes_home: Path | str) -> InstallationManifest:
    """The manifest a Hermes home binds with.

    A home holding a pointer is an entry of that shared store; any other home is
    its own installation, read exactly as ``load_installation_manifest`` reads it.
    """
    home = Path(str(hermes_home)).expanduser().resolve()
    attachment = read_attachment(home)
    if attachment is None:
        return load_installation_manifest(home)
    if os.path.lexists(home / "scope-recall" / MANIFEST_FILENAME):
        raise HermesIdentityError("home holds both its own installation and a shared store attachment")
    return shared_entry_manifest(attachment.root, attachment.entry_id, hermes_home=home)


def new_shared_payload(root: Path | str, *, agent_id: str = "default", test_mode: bool = False) -> dict[str, Any]:
    """A shared store with no entries yet.  Its id is drawn once and never changes;
    ``data_directory`` only records where it was created, and ``adopt`` rewrites it."""
    return {
        "schema_version": SHARED_SCHEMA_VERSION,
        "installation_kind": "shared",
        "installation_id": f"shared-install:{secrets.token_hex(16)}",
        "agent_id": bounded_text(agent_id, field="agent_id"),
        "data_directory": str(Path(str(root)).expanduser().resolve()),
        "test_mode": bool(test_mode),
        "scope_ids": [],
        "entries": [],
    }


def _replace_file(directory: Path, target: Path, text: str) -> None:
    """Write ``target`` in one step: a reader sees the old file or the new one."""
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, prefix=f".{target.stem}-",
                                         suffix=target.suffix, delete=False)
    with handle:
        handle.write(text)
    staged = Path(handle.name)
    for attempt in range(40):
        try:
            os.replace(staged, target)
            return
        except PermissionError:
            # Windows refuses while a reader holds the file open for a moment.
            if attempt == 39:
                staged.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


def write_shared_payload(root: Path | str, payload: dict[str, Any]) -> Path:
    store = Path(str(root)).expanduser().resolve()
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if len(encoded.encode("utf-8")) > _MAX_SHARED_MANIFEST_BYTES:
        raise HermesIdentityError("shared store manifest exceeds bounded size")
    store.mkdir(parents=True, exist_ok=True)
    _replace_file(store, store / MANIFEST_FILENAME, encoded)
    return store / MANIFEST_FILENAME


def shared_entry_record(
    source: InstallationManifest,
    *,
    entry_id: str,
    display_name: str,
    attached_at: str,
    python_executable: str | None = None,
) -> dict[str, Any]:
    """An entry's grants, carried over from the local installation it was.

    The audience rows are the ones the owner approved for that installation,
    unchanged, so every chat reaches what it reached before and the owner's
    chats meet in the scopes the installations already share.  Archive and
    retained scopes stay with the old store: a shared store starts empty.
    """
    if source.installation_kind != "local":
        raise HermesIdentityError("an entry is carried over from a local installation")
    rows = [dict(row) for row in source.audiences]
    mapped = sorted({scope_id for row in rows for scope_id in row["allowed_scope_ids"]})
    record: dict[str, Any] = {
        "entry_id": _entry_id(entry_id),
        "display_name": _display_name(display_name),
        "host": "hermes",
        "home": str(source.hermes_home),
        "attached_at": bounded_text(attached_at, field="attached_at"),
        "scope_ids": mapped,
        "owner_principals": [dict(item) for item in source.owner_principals],
        "audience_scopes": {kind: scope for kind, scope in source.audience_scopes.items() if scope in mapped},
        "audiences": rows,
    }
    if python_executable is not None:
        record["python_executable"] = bounded_text(python_executable, field="python_executable")
    return record


def attach_shared_entry(
    root: Path | str,
    source: InstallationManifest,
    *,
    entry_id: str,
    display_name: str,
    now: str,
    python_executable: str | None = None,
) -> InstallationManifest:
    """Make ``source``'s home an entry of the shared store at ``root``, with ``source``'s grants.

    Everything is checked before anything is written.  Then the store (its
    scopes, then the entry), the store's manifest, and last the pointer: stopped
    anywhere, running it again finishes the job, and until the pointer exists
    the home binds nothing new.  Returns the entry's view.
    """
    store = Path(str(root)).expanduser().resolve()
    payload = read_shared_payload(store)
    if (source.agent_id, source.test_mode) != (payload["agent_id"], payload["test_mode"]):
        raise HermesIdentityError("installation agent_id or test_mode differs from the shared store's")
    record = shared_entry_record(source, entry_id=entry_id, display_name=display_name, attached_at=now,
                                 python_executable=python_executable)
    home = source.hermes_home
    for entry in payload["entries"]:
        if (entry["entry_id"] == record["entry_id"] and not _same_path(entry["home"], home)
                and _points_here(entry["home"], store, entry["entry_id"])):
            # A home that no longer points here gives its id up: the store was
            # copied to another machine and adopted, or the home was detached.
            raise HermesIdentityError("entry_id is already attached from another home")
        if entry["entry_id"] != record["entry_id"] and _same_path(entry["home"], home) and not entry.get("detached_at"):
            raise HermesIdentityError("home is already attached as another entry")
    before = frozenset(payload["scope_ids"])
    after = before | frozenset(record["scope_ids"])
    updated = _checked_shared_payload({
        **payload,
        "entries": [entry for entry in payload["entries"] if entry["entry_id"] != record["entry_id"]] + [record],
        "scope_ids": sorted(after),
    })
    view = _entry_view(store, updated, record)

    def binding(scope_ids: frozenset[str]) -> InstanceBinding:
        return InstanceBinding(payload["agent_id"], payload["installation_id"], store, scope_ids,
                               payload["test_mode"], "shared")

    if not (store / "memory.sqlite3").exists():
        SQLiteStorage(binding(after)).initialize()
        before = after
    storage = SQLiteStorage(binding(before or after))
    context = TrustedContext(storage.binding, "shared-store-attach", storage.binding.scope_ids, "host_generated")
    with storage.write(context) as tx:
        tx.register_scopes(after)
        tx.register_entry(view.entry_id, view.entry_name, "hermes", now=now)
    write_shared_payload(store, updated)
    pointer = {"schema": ATTACHMENT_SCHEMA, "root": str(store), "entry_id": view.entry_id,
               "display_name": view.entry_name, "host": "hermes", "attached_at": now}
    path = attachment_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    _replace_file(path.parent, path, json.dumps(pointer, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return view


def _initialize_core(manifest: InstallationManifest, clock: Any | None) -> tuple[InstanceBinding, MemoryCore]:
    binding = manifest.to_binding()
    core = MemoryCore(CoreConfig(binding), clock=clock)
    core.initialize()
    assert_binding_matches_manifest(core.config.binding, manifest)
    return binding, core


def install_hermes_scope_recall(
    hermes_home: Path | str,
    *,
    agent_id: str,
    platform: str = "cli",
    user_id: str = "local",
    agent_workspace: str = "default",
    project_id: str | None = None,
    conversation_key: str = "default",
    gateway_session_key: str = "",
    retained_scope_ids: Sequence[str] = (),
    owner_principals: Sequence[Mapping[str, str]] | None = None,
    audiences: Sequence[Mapping[str, Any]] | None = None,
    local_platforms: Sequence[str] = (),
    legacy_audit_retention: bool = False,
    test_mode: bool = False,
    clock: Any | None = None,
) -> tuple[InstanceBinding, MemoryCore]:
    """Explicit trusted install for isolated tests and P14 reuse."""

    if type(legacy_audit_retention) is not bool:
        raise HermesIdentityError("legacy_audit_retention must be a boolean")
    manifest = build_installation_manifest(
        Path(hermes_home),
        agent_id=agent_id,
        platform=platform,
        user_id=user_id,
        agent_workspace=agent_workspace,
        project_id=project_id,
        conversation_key=conversation_key,
        gateway_session_key=gateway_session_key,
        retained_scope_ids=retained_scope_ids,
        owner_principals=owner_principals,
        audiences=audiences,
        local_platforms=local_platforms,
        archive_retention_scopes=AUDIT_RETENTION_SCOPES if legacy_audit_retention else None,
        test_mode=test_mode,
    )
    write_installation_manifest(manifest)
    return _initialize_core(manifest, clock)


def _verified_legacy_catalog(source_database: Path | str, source_hash: str, catalog_hash: str) -> dict[str, Any]:
    from scope_recall.maintenance.migrate_v2 import build_legacy_catalog

    catalog = build_legacy_catalog(source_database)
    if catalog["source_sha256"] != source_hash:
        raise HermesIdentityError(
            f"source snapshot digest mismatch: expected {source_hash}, got {catalog['source_sha256']}"
        )
    if catalog["catalog_sha256"] != catalog_hash:
        raise HermesIdentityError(
            f"catalog digest mismatch: expected {catalog_hash}, got {catalog['catalog_sha256']}"
        )
    if not catalog["is_supported"]:
        reasons = [item.get("reason", "unknown") for item in catalog.get("unsupported", [])]
        raise HermesIdentityError(f"legacy catalog reports unsupported semantics: {reasons}")
    return catalog


def install_hermes_archive_migration(
    hermes_home: Path | str,
    *,
    source_database: Path | str,
    agent_id: str = "p15-archive-agent",
    platform: str = "cli",
    user_id: str = "local",
    agent_workspace: str = "default",
    test_mode: bool = True,
    expected_source_hash: str | None = None,
    expected_catalog_hash: str | None = None,
    clock: Any | None = None,
) -> tuple[InstanceBinding, InstallationManifest, dict[str, Any]]:
    """Explicit opt-in trusted install for isolated archive migrations."""
    if test_mode is not True:
        raise HermesIdentityError("archive-only migration requires test_mode=True (literal True)")
    home = Path(hermes_home)
    if not home.is_absolute():
        raise HermesIdentityError("hermes_home must be absolute before resolve")
    home = home.expanduser().resolve()
    if not any(part.upper().startswith("TEST") for part in home.parts):
        raise HermesIdentityError("archive-only installation target must be beneath a TEST-named path component")
    for label, digest in (("expected_source_hash", expected_source_hash), ("expected_catalog_hash", expected_catalog_hash)):
        if type(digest) is not str or not _HEX64_RE.fullmatch(digest):
            raise HermesIdentityError(f"{label} must be exact 64-hex string")

    catalog = _verified_legacy_catalog(source_database, expected_source_hash, expected_catalog_hash)
    sources = dict.fromkeys(catalog["content_scopes"] + catalog["shared_only_scopes"] + catalog["audit_only_scopes"])
    manifest = build_installation_manifest(
        home,
        agent_id=agent_id,
        platform=platform,
        user_id=user_id,
        agent_workspace=agent_workspace,
        test_mode=True,
        archive_source_scopes={source: build_archive_scope_id(source) for source in sources},
        archive_retention_scopes=AUDIT_RETENTION_SCOPES,
        archive_snapshot_hash=catalog["source_sha256"],
        archive_catalog_hash=catalog["catalog_sha256"],
    )

    target_data = home / "scope-recall"
    if target_data.exists() and any(target_data.iterdir()):
        try:
            existing = load_installation_manifest(home)
        except HermesIdentityError as exc:
            raise HermesIdentityError(f"archive target exists but manifest is invalid or unreadable: {exc}") from exc
        if manifest_payload(existing) != manifest_payload(manifest):
            raise HermesIdentityError("existing manifest payload does not match intended payload; refusing unrelated target")
        manifest = existing
    else:
        write_installation_manifest(manifest)
    binding, _core = _initialize_core(manifest, clock)
    return binding, manifest, catalog
