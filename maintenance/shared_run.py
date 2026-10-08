"""What a shared-store write command leaves behind: the error it refuses with, the JSON files it writes (replaced in
one step, retried while Windows refuses because a reader holds the file), and the receipt with the copies it kept
(``Run``).  The commands live in ``shared`` and ``shared_import``."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

RECEIPTS_DIRNAME = "receipts"


class SharedStoreError(RuntimeError):
    """A shared store command refused; the message says why and what to do."""


def encoded(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.stem}-", suffix=".json", delete=False
    )
    with handle:
        handle.write(encoded(value))
    for attempt in range(40):
        try:
            os.replace(handle.name, path)
            return
        except PermissionError:
            # Windows refuses while a worker or host reads the file for a moment.
            if attempt == 39:
                Path(handle.name).unlink(missing_ok=True)
                raise
            time.sleep(0.05)


class Run:
    """One write command: the copies it keeps and the receipt it leaves."""

    def __init__(self, root: Path, command: str, now: str) -> None:
        self.root, self.command, self.now = root, command, now
        stamp = now.replace("-", "").replace(":", "")
        self.folder = root / RECEIPTS_DIRNAME / f"{stamp}-{command}"
        self.backups: list[str] = []

    def keep(self, path: Path, label: str) -> None:
        if path.is_file():
            self.folder.mkdir(parents=True, exist_ok=True)
            target = self.folder / f"{label}-{path.name}"
            shutil.copy2(path, target)
            self.backups.append(str(target))

    def receipt(self, body: dict[str, Any]) -> str:
        path = self.folder.with_suffix(".json")
        write_json(path, {"command": self.command, "at": self.now, "backups": self.backups, **body})
        return str(path)
