"""What a shared-store write command holds and leaves behind: the store's lock while it runs (``store_lock``), the
error it refuses with, the JSON files it writes (replaced in one step, retried while Windows refuses because a reader
holds the file), and the receipt with the copies it kept (``Run``).  The commands live in ``shared`` and
``shared_import``."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from ..core.file_lock import advisory_file_lock

RECEIPTS_DIRNAME = "receipts"
#: Beside the store's manifest: held by each shared-store command that writes, from its first read to its last write.
LOCK_FILENAME = "commands.lock"
#: How long a command waits for the one holding the lock before it refuses.
LOCK_WAIT_SECONDS = 10.0
#: What the holder's note after the lock's first byte may weigh; the operating system locks that byte.
_NOTE_LIMIT = 1024


class SharedStoreError(RuntimeError):
    """A shared store command refused; the message says why and what to do."""


def encoded(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


@contextmanager
def store_lock(root: Path, command: str, now: str) -> Iterator[None]:
    """Hold the lock of the shared store at ``root`` while ``command`` (its words as an operator runs it) reads what its
    change rests on and writes it: no other shared-store command writes in between, so a change is never written over
    one made meanwhile nor checked against a manifest that is no longer the store's.  A command that finds the lock
    held waits ``LOCK_WAIT_SECONDS`` and then refuses, naming the command that holds it from the note the holder keeps
    after the lock's first byte."""
    lock = root / LOCK_FILENAME
    with ExitStack() as held:
        try:
            held.enter_context(advisory_file_lock(lock, timeout_seconds=LOCK_WAIT_SECONDS))
        except TimeoutError:
            holder = _holder(lock) or "another shared-store command"
            raise SharedStoreError(
                f"{holder} is writing this store; run {command} again once it has finished"
            ) from None
        _note(lock, {"command": command, "pid": os.getpid(), "since": now})
        held.callback(_note, lock, None)
        yield


def _note(lock: Path, note: dict[str, Any] | None) -> None:
    with lock.open("r+b") as handle:
        handle.seek(1)
        if note is not None:
            handle.write(json.dumps(note, ensure_ascii=False).encode("utf-8")[:_NOTE_LIMIT])
        handle.truncate()


def _holder(lock: Path) -> str:
    """The command holding ``lock`` as its note names it, or nothing when the note is not written yet or unreadable."""
    try:
        with lock.open("rb") as handle:
            handle.seek(1)
            note = json.loads(handle.read(_NOTE_LIMIT).decode("utf-8"))
        return f"{note['command']} (process {note['pid']}, since {note['since']})"
    except (OSError, ValueError, KeyError, TypeError):
        return ""


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
        # Named to the second, a second run of the command in that second would keep its copies over the first's, and
        # the first's receipt would then name copies of files the second replaced.  The folder is taken by making it,
        # and kept even when it stays empty, so no later run takes its name: two runs at once cannot share one.
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
        # receipt named by replacing a suffix would lose the fraction and the command.
        path = self.folder.with_name(f"{self.folder.name}.json")
        write_json(path, {"command": self.command, "at": self.now, "backups": self.backups, **body})
        return str(path)
