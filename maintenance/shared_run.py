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


def replace_file(staged: Path, path: Path) -> None:
    """``staged`` moved over ``path`` in one step.  Windows refuses while a worker or host reads ``path`` for a moment,
    so the move is tried again for two seconds; then ``staged`` goes and the refusal is raised."""
    for attempt in range(40):
        try:
            os.replace(staged, path)
            return
        except PermissionError:
            if attempt == 39:
                staged.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.stem}-", suffix=".json", delete=False
    )
    with handle:
        handle.write(encoded(value))
    replace_file(Path(handle.name), path)


class Run:
    """One write command: the copies it keeps and the receipt it leaves."""

    def __init__(self, root: Path, command: str, now: str) -> None:
        self.root, self.command, self.now = root, command, now
        stamp = now.replace("-", "").replace(":", "")
        base = root / RECEIPTS_DIRNAME / f"{stamp}-{command}"
        # Named to the second, a second run of the command in that second kept its copies over the first's, and
        # the first's receipt then named copies of files the second replaced.  The folder is taken by making it, and
        # kept even when it stays empty, so no later run takes its name: two runs at once cannot share one.
        folder, count = base, 1
        while True:
            try:
                folder.mkdir(parents=True)
                break
            except FileExistsError:
                count += 1
                folder = base.with_name(f"{base.name}-{count}")
        self.folder = folder
        self.backups: list[str] = []

    def keep(self, path: Path, label: str) -> None:
        if path.is_file():
            self.folder.mkdir(parents=True, exist_ok=True)
            target = self.folder / f"{label}-{path.name}"
            shutil.copy2(path, target)
            self.backups.append(str(target))

    def receipt(self, body: dict[str, Any]) -> str:
        # Beside its folder, named after it whole: a moment with fractions of a second has a dot in it, and the
        # receipt named by replacing a suffix lost the fraction and the command.
        path = self.folder.with_name(f"{self.folder.name}.json")
        write_json(path, {"command": self.command, "at": self.now, "backups": self.backups, **body})
        return str(path)
