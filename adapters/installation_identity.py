"""Path anchors for local installations, separate from a recovered store's stable identity."""

import hashlib
from pathlib import Path


def path_identity(host: str, path: Path) -> str:
    """Derive the local binding anchor without changing any persisted source identity."""
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
    return f"{host}-install:{digest[:32]}"
