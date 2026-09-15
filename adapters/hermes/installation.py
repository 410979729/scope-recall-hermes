"""Trusted Hermes installation manifest and explicit install helper."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from scope_recall.contracts import InstanceBinding
from scope_recall.core import CoreConfig, MemoryCore


from .audiences import (
    HermesIdentityError, _audience_entry, _normalize_audience_entry,
    is_archive_scope, normalize_retained_scope_ids, normalize_owner_principals,
)


_MAX_FIELD_LEN = 240


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




_HEX64_RE = __import__("re").compile(r"[0-9a-fA-F]{64}")

def _validate_archive_manifest_fields(
    *,
    has_archive_fields: bool,
    archive_scopes: frozenset[str],
    archive_source_map: dict[str, str],
    archive_retention_scopes: dict[str, str],
    archive_snapshot_hash: Any,
    archive_catalog_hash: Any,
    test_mode: Any,
    scope_ids: frozenset[str],
    mapped_scopes: frozenset[str],
    audience_scopes_values: frozenset[str],
    retained_scope_ids: frozenset[str],
) -> None:
    # Production migrations retain only these two inert audit namespaces.
    # Source-to-archive remapping remains the hash-bound, TEST-only workflow.
    audit_retention_only = (
        not archive_source_map and archive_snapshot_hash == "" and archive_catalog_hash == ""
        and archive_retention_scopes == {
            "orphan_bridge": "archive|reserved:orphan_bridge",
            "digest_audit": "archive|reserved:digest_audit",
        }
    )
    if has_archive_fields and not audit_retention_only:
        if test_mode is not True:
            raise HermesIdentityError("archive fields require literal test_mode=True")
        if type(archive_snapshot_hash) is not str or not _HEX64_RE.fullmatch(archive_snapshot_hash):
            raise HermesIdentityError("invalid or missing archive_snapshot_hash")
        if type(archive_catalog_hash) is not str or not _HEX64_RE.fullmatch(archive_catalog_hash):
            raise HermesIdentityError("invalid or missing archive_catalog_hash")

    all_vals = set()
    for k, v in archive_source_map.items():
        if type(k) is not str or not k or k == "*":
            raise HermesIdentityError("source map key must be nonempty string and not '*'")
        if type(v) is not str or not v or not is_archive_scope(v):
            raise HermesIdentityError("source map value must be nonempty string in archive namespace")
        if v in all_vals:
            raise HermesIdentityError("archive map value collision")
        all_vals.add(v)

    for k, v in archive_retention_scopes.items():
        if type(k) is not str or not k:
            raise HermesIdentityError("retention map key must be nonempty string")
        if type(v) is not str or not v or not is_archive_scope(v):
            raise HermesIdentityError("retention map value must be nonempty string in archive namespace")
        if v in all_vals:
            raise HermesIdentityError("archive map value collision")
        all_vals.add(v)

    if set(archive_scopes) != all_vals:
        raise HermesIdentityError("archive_scopes must exactly equal the union of source and retention map values")

    if not set(archive_scopes).issubset(set(scope_ids)):
        raise HermesIdentityError("archive_scopes must be a subset of registered scope_ids")

    runtime_scopes = set(mapped_scopes) | set(audience_scopes_values)
    for s in runtime_scopes:
        if is_archive_scope(s) or s in archive_scopes:
            raise HermesIdentityError("runtime scopes cannot use archive namespace or overlap archive_scopes")

    if retained_scope_ids & (runtime_scopes | set(archive_scopes)):
        raise HermesIdentityError("retained_scope_ids must not overlap runtime or archive scopes")
    if set(scope_ids) != (set(mapped_scopes) | set(archive_scopes) | set(retained_scope_ids)):
        raise HermesIdentityError("registered scope_ids must equal audience union retained union archive")

MANIFEST_FILENAME = "installation.json"
SCHEMA_VERSION = "scope-recall.hermes-installation.v3"
# Exact per-principal/session rows need more room than the old coarse audiences.
_MAX_MANIFEST_BYTES = 512 * 1024


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

    def to_binding(self) -> InstanceBinding:
        return InstanceBinding(
            agent_id=self.agent_id,
            installation_id=self.installation_id,
            data_directory=self.data_directory,
            scope_ids=self.scope_ids,
            test_mode=self.test_mode,
        )


def _installation_id(hermes_home: Path) -> str:
    digest = hashlib.sha256(str(hermes_home.resolve()).encode("utf-8")).hexdigest()
    return f"hermes-install:{digest[:32]}"


def _bounded_text(value: object, *, field: str, required: bool = True) -> str:
    if type(value) is not str:
        if required:
            raise HermesIdentityError(f"{field} is required")
        return ""
    text = value.strip()
    if required and not text:
        raise HermesIdentityError(f"{field} is required")
    if len(text) > _MAX_FIELD_LEN:
        raise HermesIdentityError(f"{field} exceeds bounded length")
    return text


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
    test_mode: bool = False,
    archive_source_scopes: Sequence[str] | Mapping[str, str] | None = None,
    archive_retention_scopes: Mapping[str, str] | None = None,
    archive_snapshot_hash: str | None = None,
    archive_catalog_hash: str | None = None,
) -> InstallationManifest:
    """Build v3 grants; supplied audiences are the complete exact runtime set.

    v2 files require an explicit rebuild with attested principal/session/write
    grants. Retained IDs register originals for import, never runtime access.
    """
    home = hermes_home.expanduser().resolve()
    if not home.is_absolute():
        raise HermesIdentityError("hermes_home must be absolute")
    agent = _bounded_text(agent_id, field="agent_id")
    plat = _bounded_text(platform or "cli", field="platform")
    owner = _bounded_text(user_id or "local", field="user_id", required=False) or "local"
    principals = normalize_owner_principals(
        list(owner_principals) if owner_principals is not None else [dict(platform=plat, user_id=owner)]
    )
    if dict(platform=plat, user_id=owner) not in principals:
        raise HermesIdentityError("primary owner must be included in owner_principals")
    workspace = _bounded_text(agent_workspace or "default", field="agent_workspace", required=False) or "default"
    project = _bounded_text(project_id or workspace, field="project_id", required=False) or workspace
    conversation = _bounded_text(conversation_key or "default", field="conversation_key", required=False) or "default"
    if plat != "cli" and conversation == "default":
        # Retain the historical fixture's explicitly named group-1 audience;
        # all other groups still require an installer supplied mapping.
        conversation = "group-1"
    generated_scopes = _build_audience_scope_ids(
        platform=plat,
        user_id=owner,
        agent_identity=agent,
        agent_workspace=workspace,
        project_id=project,
        conversation_key=conversation,
    )
    # The owner mapping is always explicit.  Other mappings are exact entries
    # supplied by the trusted installer; no non-CLI wildcard is synthesized.
    owner_chat_type = "cli" if plat == "cli" else "private"
    owner_chat_id = "local" if plat == "cli" else owner
    owner_thread = "main"
    mappings = [
        _audience_entry(
            platform=plat,
            user_id=owner,
            gateway_session_key=gateway_session_key,
            chat_type=owner_chat_type,
            chat_id=owner_chat_id,
            thread_id=owner_thread,
            agent_workspace=workspace,
            allowed_scope_ids=[generated_scopes["owner_private"]],
            writable_scope_ids=[generated_scopes["owner_private"]],
            capture_scope_id=generated_scopes["owner_private"],
            kind="owner_private",
        )
    ]
    if audiences is not None:
        if project_id:
            raise HermesIdentityError("explicit audiences cannot be mixed with project convenience grants")
        mappings = [_normalize_audience_entry(item) for item in audiences]
        if not mappings:
            raise HermesIdentityError("explicit audiences must not be empty")
    # Preserve the historical convenience arguments as explicit mappings.
    elif plat != "cli":
        mappings.append(
            _audience_entry(
                platform=plat,
                user_id=owner,
                gateway_session_key=gateway_session_key,
                chat_type="group",
                chat_id=conversation,
                thread_id="main",
                agent_workspace=workspace,
                allowed_scope_ids=[generated_scopes["conversation"]],
                writable_scope_ids=[generated_scopes["conversation"]],
                capture_scope_id=generated_scopes["conversation"],
                kind="conversation",
            )
        )
    if project_id:
        mappings.append(
            _audience_entry(
                platform=plat,
                user_id=owner,
                gateway_session_key=gateway_session_key,
                chat_type="project",
                chat_id=project,
                thread_id="main",
                agent_workspace=workspace,
                allowed_scope_ids=[generated_scopes["project"]],
                writable_scope_ids=[generated_scopes["project"]],
                capture_scope_id=generated_scopes["project"],
                kind="project",
            )
        )
    audience_scopes: dict[str, str] = {}
    for mapping in mappings:
        key = str(mapping.get("kind") or "conversation")
        audience_scopes.setdefault(key, str(mapping["capture_scope_id"]))
    if "owner_private" not in audience_scopes:
        raise HermesIdentityError("an explicit owner_private audience is required")
    mapped_scopes = frozenset(
        scope_id
        for mapping in mappings
        for scope_id in mapping["allowed_scope_ids"]
    )
    archive_map: dict[str, str] = {}
    arch_retention: dict[str, str] = {}
    
    has_archive_fields = (
        archive_source_scopes is not None or
        archive_retention_scopes is not None or
        archive_snapshot_hash is not None or
        archive_catalog_hash is not None
    )

    if archive_source_scopes is not None:
        if isinstance(archive_source_scopes, Mapping):
            for src, arch in archive_source_scopes.items():
                if type(src) is not str or type(arch) is not str:
                    raise HermesIdentityError("archive scope map keys and values must be strings")
                archive_map[src] = arch
        else:
            seen_src = set()
            for item in archive_source_scopes:
                if type(item) is not str:
                    raise HermesIdentityError("archive scope must be string")
                if item in seen_src:
                    raise HermesIdentityError("duplicate sequence source identifiers")
                seen_src.add(item)
                a_clean = build_archive_scope_id(item)
                archive_map[item] = a_clean

    if archive_retention_scopes is not None:
        if not isinstance(archive_retention_scopes, Mapping):
            raise HermesIdentityError("archive retention must be a mapping")
        for key, val in archive_retention_scopes.items():
            if type(key) is not str or type(val) is not str:
                raise HermesIdentityError("archive retention map keys and values must be strings")
            arch_retention[key] = val

    arch_scopes = frozenset(archive_map.values()) | frozenset(arch_retention.values())
    retained = normalize_retained_scope_ids(retained_scope_ids)
    scope_ids = mapped_scopes | arch_scopes | retained
    
    _validate_archive_manifest_fields(
        has_archive_fields=has_archive_fields,
        archive_scopes=arch_scopes,
        archive_source_map=archive_map,
        archive_retention_scopes=arch_retention,
        archive_snapshot_hash=archive_snapshot_hash if archive_snapshot_hash is not None else "",
        archive_catalog_hash=archive_catalog_hash if archive_catalog_hash is not None else "",
        test_mode=test_mode,
        scope_ids=scope_ids,
        mapped_scopes=mapped_scopes,
        audience_scopes_values=frozenset(audience_scopes.values()),
        retained_scope_ids=retained,
    )

    data_directory = (home / "scope-recall").resolve()
    return InstallationManifest(
        schema_version=SCHEMA_VERSION,
        installation_id=_installation_id(home),
        agent_id=agent,
        data_directory=data_directory,
        scope_ids=scope_ids,
        owner_principals=principals,
        audience_scopes=audience_scopes,
        audiences=tuple(mappings),
        test_mode=bool(test_mode),
        hermes_home=home,
        retained_scope_ids=retained,
        archive_scopes=arch_scopes,
        archive_source_map=archive_map,
        archive_retention_scopes=arch_retention,
        archive_snapshot_hash=archive_snapshot_hash if archive_snapshot_hash is not None else "",
        archive_catalog_hash=archive_catalog_hash if archive_catalog_hash is not None else "",
    )


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
    manifest.data_directory.mkdir(parents=True, exist_ok=True)
    path = manifest.data_directory / MANIFEST_FILENAME
    encoded = json.dumps(manifest_payload(manifest), ensure_ascii=False, sort_keys=True, indent=2)
    if len(encoded.encode("utf-8")) > _MAX_MANIFEST_BYTES:
        raise HermesIdentityError("installation manifest exceeds bounded size")
    path.write_text(encoded + "\n", encoding="utf-8")
    return path


def load_installation_manifest(hermes_home: Path | str) -> InstallationManifest:
    home = Path(str(hermes_home)).expanduser().resolve()
    path = home / "scope-recall" / MANIFEST_FILENAME
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
    data_directory = Path(str(payload.get("data_directory") or "")).expanduser().resolve()
    expected_directory = (home / "scope-recall").resolve()
    if data_directory != expected_directory:
        raise HermesIdentityError("installation manifest data_directory mismatch")
    scope_ids = payload.get("scope_ids")
    if not isinstance(scope_ids, list) or not scope_ids:
        raise HermesIdentityError("installation manifest scope_ids invalid")
    seen_scope_ids = set()
    for s in scope_ids:
        if type(s) is not str or not s:
            raise HermesIdentityError("scope_id must be a nonempty string")
        if s in seen_scope_ids:
            raise HermesIdentityError("duplicate scope_ids")
        seen_scope_ids.add(s)
    
    audience_scopes = payload.get("audience_scopes")
    if not isinstance(audience_scopes, dict) or not audience_scopes:
        raise HermesIdentityError("installation manifest audience_scopes invalid")
    if "owner_private" not in audience_scopes:
        raise HermesIdentityError("installation manifest audience_scopes incomplete")
    for k, v in audience_scopes.items():
        if type(k) is not str or not k:
            raise HermesIdentityError("audience_scopes key must be nonempty string")
        if type(v) is not str or not v:
            raise HermesIdentityError("audience_scopes value must be nonempty string")
    raw_audiences = payload.get("audiences")
    if not isinstance(raw_audiences, list) or not raw_audiences:
        raise HermesIdentityError("installation manifest audiences invalid")
    for item in raw_audiences:
        if not isinstance(item, dict):
            raise HermesIdentityError("audience entry must be dict")
        allowed = item.get("allowed_scope_ids")
        if not isinstance(allowed, list):
            raise HermesIdentityError("audience allowed_scope_ids must be a list")
        seen_allowed: list[str] = []
        for s in allowed:
            if type(s) is not str or not s:
                raise HermesIdentityError("audience allowed_scope_ids entry must be nonempty string")
            if s in seen_allowed:
                raise HermesIdentityError("audience allowed_scope_ids contains duplicates")
            seen_allowed.append(s)
        capture = item.get("capture_scope_id")
        if type(capture) is not str:
            raise HermesIdentityError("audience capture_scope_id must be an explicit string")
    audiences = tuple(_normalize_audience_entry(item) for item in raw_audiences)
    principals = normalize_owner_principals(payload.get("owner_principals"))

    has_archive_fields = any(
        k in payload for k in (
            "archive_scopes", "archive_source_map", "archive_retention_scopes",
            "archive_snapshot_hash", "archive_catalog_hash"
        )
    )

    archive_scopes_raw = payload.get("archive_scopes")
    if "archive_scopes" in payload:
        if archive_scopes_raw is None:
            raise HermesIdentityError("explicit null archive_scopes is not an absent field")
        if type(archive_scopes_raw) is not list:
            raise HermesIdentityError("installation manifest archive_scopes must be a list")
        for s in archive_scopes_raw:
            if type(s) is not str:
                raise HermesIdentityError("installation manifest archive_scopes entry must be str")
        if len(archive_scopes_raw) != len(set(archive_scopes_raw)):
            raise HermesIdentityError("installation manifest archive_scopes contains duplicates")
        archive_scopes = frozenset(archive_scopes_raw)
    else:
        archive_scopes = frozenset()

    archive_source_map_raw = payload.get("archive_source_map")
    if "archive_source_map" in payload:
        if archive_source_map_raw is None:
            raise HermesIdentityError("explicit null archive_source_map is not an absent field")
        if type(archive_source_map_raw) is not dict:
            raise HermesIdentityError("installation manifest archive_source_map must be a dict")
        archive_source_map = dict(archive_source_map_raw)
    else:
        archive_source_map = {}

    archive_retention_scopes_raw = payload.get("archive_retention_scopes")
    if "archive_retention_scopes" in payload:
        if archive_retention_scopes_raw is None:
            raise HermesIdentityError("explicit null archive_retention_scopes is not an absent field")
        if type(archive_retention_scopes_raw) is not dict:
            raise HermesIdentityError("installation manifest archive_retention_scopes must be a dict")
        archive_retention_scopes = dict(archive_retention_scopes_raw)
    else:
        archive_retention_scopes = {}

    if "archive_snapshot_hash" in payload:
        archive_snapshot_hash = payload.get("archive_snapshot_hash")
        if archive_snapshot_hash is None:
            raise HermesIdentityError("explicit null archive_snapshot_hash is not an absent field")
    else:
        archive_snapshot_hash = ""
    if "archive_catalog_hash" in payload:
        archive_catalog_hash = payload.get("archive_catalog_hash")
        if archive_catalog_hash is None:
            raise HermesIdentityError("explicit null archive_catalog_hash is not an absent field")
    else:
        archive_catalog_hash = ""
    test_mode_val = payload.get("test_mode")

    mapped_scopes = frozenset(
        scope_id for item in audiences for scope_id in item["allowed_scope_ids"]
    )
    scope_ids_set = frozenset(scope_ids)
    retained = normalize_retained_scope_ids(payload.get("retained_scope_ids"))

    _validate_archive_manifest_fields(
        has_archive_fields=has_archive_fields,
        archive_scopes=archive_scopes,
        archive_source_map=archive_source_map,
        archive_retention_scopes=archive_retention_scopes,
        archive_snapshot_hash=archive_snapshot_hash,
        archive_catalog_hash=archive_catalog_hash,
        test_mode=test_mode_val,
        scope_ids=scope_ids_set,
        mapped_scopes=mapped_scopes,
        audience_scopes_values=frozenset(audience_scopes.values()),
        retained_scope_ids=retained,
    )

    manifest = InstallationManifest(
        schema_version=SCHEMA_VERSION,
        installation_id=_bounded_text(payload.get("installation_id"), field="installation_id"),
        agent_id=_bounded_text(payload.get("agent_id"), field="agent_id"),
        data_directory=data_directory,
        scope_ids=scope_ids_set,
        owner_principals=tuple(principals),
        audience_scopes=dict(audience_scopes),
        audiences=audiences,
        test_mode=bool(test_mode_val),
        hermes_home=home,
        retained_scope_ids=retained,
        archive_scopes=archive_scopes,
        archive_source_map=archive_source_map,
        archive_retention_scopes=archive_retention_scopes,
        archive_snapshot_hash=archive_snapshot_hash,
        archive_catalog_hash=archive_catalog_hash,
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
    ) != (
        expected.agent_id,
        expected.installation_id,
        expected.data_directory.resolve(),
        expected.scope_ids,
        expected.test_mode,
    ):
        raise HermesIdentityError("core binding does not match installation manifest")


def assert_core_binding_matches(core: MemoryCore, binding: InstanceBinding) -> None:
    if core.config.binding != binding:
        raise HermesIdentityError("injected core binding mismatch")


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
        archive_retention_scopes=(
            {"orphan_bridge": "archive|reserved:orphan_bridge",
             "digest_audit": "archive|reserved:digest_audit"}
            if legacy_audit_retention else None
        ),
        test_mode=test_mode,
    )
    write_installation_manifest(manifest)
    binding = manifest.to_binding()
    core = MemoryCore(CoreConfig(binding), clock=clock)
    core.initialize()
    assert_binding_matches_manifest(core.config.binding, manifest)
    return binding, core


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
    
    orig_home = Path(hermes_home)
    if not orig_home.is_absolute():
        raise HermesIdentityError("hermes_home must be absolute before resolve")
        
    home = orig_home.expanduser().resolve()
    
    if not any(part.upper().startswith("TEST") for part in home.parts):
        raise HermesIdentityError(
            "archive-only installation target must be beneath a TEST-named path component"
        )
    target_data = home / "scope-recall"

    if type(expected_source_hash) is not str or not _HEX64_RE.fullmatch(expected_source_hash):
        raise HermesIdentityError("expected_source_hash must be exact 64-hex string")
    if type(expected_catalog_hash) is not str or not _HEX64_RE.fullmatch(expected_catalog_hash):
        raise HermesIdentityError("expected_catalog_hash must be exact 64-hex string")

    from scope_recall.maintenance.migrate_v2 import build_legacy_catalog

    catalog = build_legacy_catalog(source_database)

    if catalog["source_sha256"] != expected_source_hash:
        raise HermesIdentityError(
            f"source snapshot digest mismatch: expected {expected_source_hash}, got {catalog['source_sha256']}"
        )
    if catalog["catalog_sha256"] != expected_catalog_hash:
        raise HermesIdentityError(
            f"catalog digest mismatch: expected {expected_catalog_hash}, got {catalog['catalog_sha256']}"
        )
    if not catalog["is_supported"]:
        unsupported_reasons = [u.get("reason", "unknown") for u in catalog.get("unsupported", [])]
        raise HermesIdentityError(
            f"legacy catalog reports unsupported semantics: {unsupported_reasons}"
        )

    real_scopes_set = set()
    real_source_scopes = []
    for s in catalog["content_scopes"] + catalog["shared_only_scopes"] + catalog["audit_only_scopes"]:
        if s not in real_scopes_set:
            real_scopes_set.add(s)
            real_source_scopes.append(s)

    archive_source_map = {src: build_archive_scope_id(src) for src in real_source_scopes}
    archive_retention_scopes = {
        "orphan_bridge": "archive|reserved:orphan_bridge",
        "digest_audit": "archive|reserved:digest_audit",
    }

    manifest = build_installation_manifest(
        home,
        agent_id=agent_id,
        platform=platform,
        user_id=user_id,
        agent_workspace=agent_workspace,
        test_mode=True,
        archive_source_scopes=archive_source_map,
        archive_retention_scopes=archive_retention_scopes,
        archive_snapshot_hash=catalog["source_sha256"],
        archive_catalog_hash=catalog["catalog_sha256"],
    )
    intended_payload = manifest_payload(manifest)

    if target_data.exists() and any(target_data.iterdir()):
        try:
            existing = load_installation_manifest(home)
        except HermesIdentityError as exc:
            raise HermesIdentityError(
                f"archive target exists but manifest is invalid or unreadable: {exc}"
            ) from exc
        
        existing_payload = manifest_payload(existing)
        if existing_payload != intended_payload:
            raise HermesIdentityError(
                "existing manifest payload does not match intended payload; refusing unrelated target"
            )
        
        binding = existing.to_binding()
        core = MemoryCore(CoreConfig(binding), clock=clock)
        core.initialize()
        assert_binding_matches_manifest(core.config.binding, existing)
        return binding, existing, catalog

    write_installation_manifest(manifest)
    binding = manifest.to_binding()
    core = MemoryCore(CoreConfig(binding), clock=clock)
    core.initialize()
    assert_binding_matches_manifest(core.config.binding, manifest)
    return binding, manifest, catalog

