"""The Hermes adapter's kept captures on disk: what its retry buffer could not store by a shutdown, by its give-up time
or for want of room, for the next start of an adapter of the same installation to write (``CaptureRetry``).

A file a capture, named by its installation and its key, so a capture kept again replaces its own file and the adapter
that stores it removes it by name; each is written whole, a temporary file renamed over it.  A file holds what the
capture inbox keeps of a capture: its event, the context it was said in, its scope and its host scope, which the next
start writes under the grant its scope has then, as the retry buffer does.  Nothing here touches the store, so a
writer that holds the store's lease, or a store slow to answer, cannot keep a capture off the disk."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

from scope_recall.contracts import ContractError, InstanceBinding
from scope_recall.core.capture_inbox import context_payload, stored_context

from .boundary import SourceIdentity
from .capture import RETRY_EVENT_BYTES, RetryCapture, label
from .identity import HermesRuntimeScope, host_scope_payload

_log = logging.getLogger("scope_recall.adapters.hermes.provider")

#: The folder beside the store that holds them.
DIRECTORY = "kept-captures"
#: Files it holds at most: 64 MiB of event data plus envelope metadata. A capture past this limit is refused.
LIMIT = 256
_FORMAT = 1


def folder(binding: InstanceBinding) -> Path:
    return binding.data_directory / DIRECTORY


def _file(binding: InstanceBinding, identity: SourceIdentity) -> Path:
    name = json.dumps([binding.installation_id, binding.agent_id, identity[0], identity[1]], ensure_ascii=False)
    return folder(binding) / f"{hashlib.sha256(name.encode('utf-8')).hexdigest()[:32]}.json"


def keep(binding: InstanceBinding, identity: SourceIdentity, capture: RetryCapture) -> bool:
    """Write one kept capture to disk; whether it is there."""
    target = _file(binding, identity)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() and sum(1 for _ in target.parent.glob("*.json")) >= LIMIT:
            _log.warning("scope-recall: the kept captures' folder is full (%d): %s", LIMIT, label(identity))
            return False
        payload = {
            "format": _FORMAT,
            "installation_id": binding.installation_id,
            "agent_id": binding.agent_id,
            "identity": [identity[0], identity[1]],
            "context": context_payload(capture.context),
            "event": capture.event,
            "gaps": list(capture.gaps),
            "masked_secret_count": capture.masked_secret_count,
            "capture_gaps": capture.capture_gaps,
            "scope_id": capture.scope_id,
            "host_scope": host_scope_payload(capture.host_scope),
        }
        pending = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > RETRY_EVENT_BYTES + 65536:
            raise ValueError("kept_capture_size")
        with pending.open("w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, target)
    except (OSError, TypeError, ValueError, ContractError) as exc:
        _log.warning(
            "scope-recall: a kept capture could not be written to disk (%s): %s", type(exc).__name__, label(identity)
        )
        return False
    return True


def forget(binding: InstanceBinding, identity: SourceIdentity) -> None:
    """Remove a kept capture's file: it is stored, or no longer to be written."""
    try:
        _file(binding, identity).unlink(missing_ok=True)
    except OSError:
        # Left behind, it is read at the next start and written again, which the store takes as the capture it holds.
        pass


def _written_at(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def load(binding: InstanceBinding, *, skip, room: int) -> tuple[list[tuple[SourceIdentity, RetryCapture]], bool]:
    """The kept captures of ``binding`` on disk, oldest first, up to ``room`` of them and none whose key ``skip`` holds;
    and whether more are left there. Files that cannot be read remain on disk and are reported for inspection."""
    try:
        files = sorted(folder(binding).glob("*.json"), key=_written_at)
    except OSError:
        return [], False
    loaded: list[tuple[SourceIdentity, RetryCapture]] = []
    for path in files:
        if len(loaded) >= room:
            return loaded, True
        try:
            if path.stat().st_size > RETRY_EVENT_BYTES + 65536:
                raise ValueError("kept_capture_size")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("installation_id") != binding.installation_id or payload.get("agent_id") != binding.agent_id:
                continue
            if payload["format"] != _FORMAT:
                raise ValueError("kept_capture_format")
            key, revision = payload["identity"]
            identity = (str(key), int(revision))
            if (payload["event"]["source_event_key"], payload["event"]["source_revision"]) != identity:
                raise ValueError("kept_capture_identity")
            if path != _file(binding, identity):
                raise ValueError("kept_capture_filename")
            if identity in skip:
                continue
            capture = RetryCapture(
                stored_context(binding, payload["context"]),
                dict(payload["event"]),
                tuple(payload["gaps"]),
                str(payload["scope_id"]),
                HermesRuntimeScope(**dict(payload["host_scope"])),
                time.monotonic(),
                int(payload.get("masked_secret_count", 0)),
                tuple(payload.get("capture_gaps", ())),
            )
        except (OSError, KeyError, TypeError, ValueError, AttributeError, ContractError) as exc:
            _log.warning(
                "scope-recall: a kept capture's file could not be read (%s), retained: %s",
                type(exc).__name__,
                path.name,
            )
            continue
        loaded.append((identity, capture))
    return loaded, False
