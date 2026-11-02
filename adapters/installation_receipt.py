"""Read and verify installation receipts shared by installers and capture authorization."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

RECEIPT_FILENAME = ".scope-recall-install-receipt.json"
RECEIPT_SCHEMA = "scope-recall.install-receipt.v1"


def receipt_digest(payload: dict) -> str:
    """Hash every receipt field except its digest, using the installer's canonical encoding."""
    body = {key: value for key, value in payload.items() if key != "receipt_sha256"}
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def read_receipt(instance_root: Path, *, error: type[Exception] = ValueError) -> dict | None:
    """Return a verified receipt or None when absent; unreadable or invalid records raise error."""
    path = instance_root / RECEIPT_FILENAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise error("existing install receipt is unreadable") from exc
    if not isinstance(payload, dict):
        raise error("existing install receipt is invalid")
    if payload.get("schema_version") != RECEIPT_SCHEMA:
        raise error("existing install receipt schema mismatch")
    digest = payload.get("receipt_sha256")
    if type(digest) is not str or not digest:
        raise error("receipt digest missing")
    if receipt_digest(payload) != digest:
        raise error("receipt digest mismatch")
    return payload
