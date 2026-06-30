"""Secret-indexing helper for explicitly enabled maintenance scans.

Secret tools are disabled by default and must not expose raw credentials in public reports."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .gating import clean_text, compact_text
from .graph import normalize_entity

_ALLOWED_SECRET_TYPES = {"password", "token", "api_key", "private_key", "cookie", "credential", "other"}
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_\s-]?key|token|secret|password|passwd|credential|private[_\s-]?key|cookie)\s*[:=]\s*[^\s,'\"\]}]+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/=]{16,}")


def _clean_field(value: Any, *, max_chars: int = 240) -> str:
    text = compact_text(clean_text(str(value or "")), max_chars)
    text = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _BEARER_RE.sub("bearer [REDACTED]", text)
    return text


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        values = [str(item).strip() for item in value]
    else:
        values = []
    return [item for item in values if item]


def _secret_type(value: Any) -> str:
    normalized = str(value or "credential").strip().lower().replace("-", "_")
    return normalized if normalized in _ALLOWED_SECRET_TYPES else "other"


def _fingerprint(secret_value: Any) -> str:
    value = str(secret_value or "")
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def build_secret_index(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Build a recallable secret index without storing plaintext credentials.

    Scope Recall stores this as a normal memory row so it can be searched by
    service/account/purpose. The plaintext secret value is intentionally not
    included in content or metadata; callers should put the secret in an
    external vault/keyring and provide ``vault_ref`` here.
    """

    label = _clean_field(args.get("label") or args.get("name"), max_chars=160)
    service = _clean_field(args.get("service"), max_chars=120)
    account = _clean_field(args.get("account"), max_chars=120)
    username = _clean_field(args.get("username"), max_chars=120)
    hostname = _clean_field(args.get("hostname"), max_chars=120)
    vault_ref = _clean_field(args.get("vault_ref") or args.get("locator"), max_chars=260)
    notes = _clean_field(args.get("notes"), max_chars=300)
    rotation_due = _clean_field(args.get("rotation_due") or args.get("expires_at"), max_chars=80)
    secret_type = _secret_type(args.get("secret_type") or args.get("type"))
    fingerprint = _fingerprint(args.get("secret_value"))

    if not label:
        label = service or account or vault_ref or "unnamed credential"

    lines = [f"Secret index: {label}", f"Kind: {secret_type}"]
    if service:
        lines.append(f"Service: {service}")
    if account:
        lines.append(f"Account: {account}")
    if username:
        lines.append(f"Username: {username}")
    if hostname:
        lines.append(f"Host: {hostname}")
    if vault_ref:
        lines.append(f"Vault ref: {vault_ref}")
    else:
        lines.append("Vault ref: [not provided]")
    if rotation_due:
        lines.append(f"Rotation due: {rotation_due}")
    if fingerprint:
        lines.append(f"Secret fingerprint: sha256:{fingerprint}")
    if notes:
        lines.append(f"Notes: {notes}")
    lines.append("Plaintext secret value: [not stored in scope-recall SQL/FTS/vector]")

    content = "\n".join(lines)
    entities = [label, service, account, username, hostname, vault_ref]
    entities.extend(_string_list(args.get("entities")))
    tags = ["secret-index", "credential", f"secret-type:{secret_type}"]
    tags.extend(_string_list(args.get("tags")))
    metadata: dict[str, Any] = {
        "memory_type": "resource",
        "importance": 0.82,
        "sensitivity": "secret-index",
        "secret_storage": "external-vault-reference",
        "secret_value_stored": False,
        "secret_type": secret_type,
        "secret_value_sha256_prefix": fingerprint,
        "entities": sorted({entity for entity in (normalize_entity(item) for item in entities) if entity}),
        "tags": sorted({tag.strip().lower() for tag in tags if tag.strip()}),
    }
    if vault_ref:
        metadata["vault_ref"] = vault_ref
    if service:
        metadata["service"] = service
    if account:
        metadata["account"] = account
    if rotation_due:
        metadata["rotation_due"] = rotation_due
    return content, metadata
