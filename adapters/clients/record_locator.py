"""Content-free locators for local captures whose session ledger is the durable fallback."""

import hashlib
import json
import os
import secrets
from pathlib import Path

from .config import SharedClientConfig


def remember(config, payload: dict) -> None:
    """Persist only the record locator before capturing; the host keeps the content through any process failure."""
    path = payload.get("transcript_path")
    session = payload.get("session_id")
    if type(path) is not str or type(session) is not str or not Path(path).is_absolute():
        return
    if isinstance(config, SharedClientConfig):
        return
    key = hashlib.sha256(session.encode()).hexdigest()
    root = config.data_directory / "capture-ledgers"
    try:
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"{key}.json"
        pending = target.with_name(f"{target.stem}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        pending.write_text(
            json.dumps(dict(config=str(config.config_path), session=session, record=path, cwd=payload.get("cwd"))),
            encoding="utf-8",
        )
        os.replace(pending, target)
    except OSError:
        pass
