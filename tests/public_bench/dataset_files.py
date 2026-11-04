"""Validate frozen local benchmark inputs without downloading or executing them."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def file_records(manifest: dict) -> dict[str, dict]:
    """Flatten dataset file records and reject conflicting names."""
    records = {}
    for dataset in manifest.values():
        entries = [dataset] if "file" in dataset else dataset.get("files", [])
        for entry in entries:
            name = entry["file"]
            if name in records and records[name] != entry:
                raise ValueError(f"conflicting_manifest_record:{name}")
            records[name] = entry
    return records


def verify_file(path: Path, record: dict) -> dict:
    """Hash actual file bytes and require the frozen SHA-256 before use."""
    if path.name != record["file"]:
        raise ValueError("manifest_filename_mismatch")
    with path.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != record["sha256"]:
        raise ValueError(f"sha256_mismatch:{path.name}")
    return record


def verify_inputs(data: Path, dataset: str) -> dict:
    """Verify the selected corpus and any external judge prompts before parsing."""
    manifest = json.loads((data / "datasets-manifest.json").read_text(encoding="utf-8"))
    groups = ["memoryagentbench" if dataset == "factconsolidation" else dataset]
    if dataset == "longmemeval":
        groups.append("longmemeval_judge")
    records = file_records({name: manifest[name] for name in groups})
    for filename, record in records.items():
        path = (data / filename).resolve()
        if not path.is_relative_to(data.resolve()):
            raise ValueError("manifest_path_outside_data_directory")
        verify_file(path, record)
    return manifest
