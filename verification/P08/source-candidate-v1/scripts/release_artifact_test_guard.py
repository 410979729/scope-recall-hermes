"""Fail closed when release rehearsals import product code outside the exact wheel.

This module is package data in the release wheel and is loaded as a pytest
plugin from the installed distribution.  It never records local paths; the
caller retains raw logs separately as local-restricted evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any


OUTPUT_ENV = "SCOPE_RECALL_ARTIFACT_GUARD_OUTPUT"
SOURCE_ROOT_ENV = "SCOPE_RECALL_ARTIFACT_SOURCE_ROOT"
EXPECTED_SHA_ENV = "SCOPE_RECALL_ARTIFACT_SHA256"
INSTALL_RECEIPT_SHA_ENV = "SCOPE_RECALL_INSTALL_RECEIPT_SHA256"


def _canonical_path(value: str | os.PathLike[str]) -> Path:
    return Path(value).resolve(strict=False)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _guard_payload() -> dict[str, object]:
    import importlib.metadata
    import scope_recall  # pyright: ignore[reportMissingImports]

    source_root = _canonical_path(os.environ[SOURCE_ROOT_ENV])
    module_file = _canonical_path(scope_recall.__file__ or "")
    source_on_sys_path = any(
        _canonical_path(item or os.curdir) == source_root for item in sys.path
    )
    source_module_imported = _is_relative_to(module_file, source_root)
    distribution = importlib.metadata.distribution("hermes-scope-recall")
    distribution_root = _canonical_path(str(distribution.locate_file("")))
    installed_module = _is_relative_to(module_file, distribution_root)
    if source_on_sys_path or source_module_imported or not installed_module:
        raise RuntimeError("release rehearsal imported outside isolated site-packages")
    expected_sha = str(os.environ.get(EXPECTED_SHA_ENV) or "")
    install_receipt_sha = str(os.environ.get(INSTALL_RECEIPT_SHA_ENV) or "")
    for label, value in (
        ("artifact", expected_sha),
        ("install receipt", install_receipt_sha),
    ):
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise RuntimeError(f"release rehearsal {label} digest is invalid")
    return {
        "schema_version": "scope-recall.artifact-import-guard.v1",
        "artifact_sha256": expected_sha,
        "install_receipt_sha256": install_receipt_sha,
        "installed_distribution": (
            f"hermes-scope-recall=={distribution.version}"
        ),
        "imported_module_path_class": "isolated-site-packages",
        "source_worktree_imported": False,
        "source_worktree_on_sys_path": False,
        "sys_path_fingerprint": hashlib.sha256(
            json.dumps(
                [
                    "source-root" if _canonical_path(item or os.curdir) == source_root
                    else "path-entry"
                    for item in sys.path
                ],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "result": "passed",
    }


class ArtifactImportGuard:
    def __init__(self, output: Path) -> None:
        self.output = output
        self.payload: dict[str, object] | None = None

    def pytest_sessionstart(self, session: object) -> None:
        del session
        self.payload = _guard_payload()

    def pytest_sessionfinish(self, session: object, exitstatus: int) -> None:
        del session
        payload = dict(self.payload or _guard_payload())
        payload["pytest_exit_status"] = int(exitstatus)
        if exitstatus != 0:
            payload["result"] = "failed"
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )


def pytest_configure(config: Any) -> None:
    output = str(os.environ.get(OUTPUT_ENV) or "").strip()
    if not output:
        raise RuntimeError(f"{OUTPUT_ENV} is required")
    config.pluginmanager.register(
        ArtifactImportGuard(Path(output)),
        "scope-recall-artifact-import-guard",
    )
