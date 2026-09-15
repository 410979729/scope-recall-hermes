"""References to the original, mutable P18 budget ledger.

Only the ledger may live outside the immutable evidence directory. Its
canonical path and filesystem identity are frozen, rather than its changing
contents. These helpers never create, copy, move, or open a database.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


LEDGER_BINDING_SCHEMA = "scope-recall.p18-original-ledger.v1"


class LedgerReferenceError(ValueError):
    """The reference no longer identifies the frozen original ledger."""


def freeze_ledger_binding(path: Path) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise LedgerReferenceError("ledger_file_missing")
    stat = resolved.stat()
    if stat.st_ino <= 0:
        raise LedgerReferenceError("ledger_file_identity_unavailable")
    return {"schema": LEDGER_BINDING_SCHEMA, "canonical_path": str(resolved),
            "device": stat.st_dev, "inode": stat.st_ino}


def ledger_path_reference(path: Path, base: Path) -> str:
    resolved = Path(path).expanduser().resolve()
    root = Path(base).resolve()
    return str(resolved.relative_to(root)) if resolved.is_relative_to(root) else str(resolved)


def resolve_ledger_reference(value: Any, base: Path, binding: Any = None) -> Path:
    if type(value) is not str or not value.strip():
        raise LedgerReferenceError("ledger_path_invalid")
    reference = Path(value)
    root = Path(base).resolve()
    path = reference.resolve() if reference.is_absolute() else (root / reference).resolve()
    if not reference.is_absolute() and not path.is_relative_to(root):
        raise LedgerReferenceError("ledger_relative_path_escapes_config")
    if binding is None:
        # Retained in-root fixtures/configs remain valid; external access
        # requires the explicit original-file binding below.
        if reference.is_absolute():
            raise LedgerReferenceError("ledger_binding_required")
        if not path.is_file():
            raise LedgerReferenceError("ledger_file_missing")
        return path
    if (type(binding) is not dict
            or set(binding) != {"schema", "canonical_path", "device", "inode"}
            or binding.get("schema") != LEDGER_BINDING_SCHEMA
            or type(binding.get("canonical_path")) is not str
            or not Path(binding["canonical_path"]).is_absolute()
            or type(binding.get("device")) is not int or binding["device"] < 0
            or type(binding.get("inode")) is not int or binding["inode"] <= 0):
        raise LedgerReferenceError("ledger_binding_invalid")
    canonical = Path(binding["canonical_path"])
    if path != canonical or path != canonical.resolve():
        raise LedgerReferenceError("ledger_not_frozen_original")
    if not path.is_file():
        raise LedgerReferenceError("ledger_file_missing")
    stat = path.stat()
    if (stat.st_dev, stat.st_ino) != (binding["device"], binding["inode"]):
        raise LedgerReferenceError("ledger_file_identity_changed")
    return path
