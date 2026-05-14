from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any


def migrate_legacy_scope_recall_storage(hermes_home: Path | None, storage_dir: Path | None) -> dict[str, Any]:
    info: dict[str, Any] = {"migrated": False}
    if hermes_home is None or storage_dir is None:
        return info

    legacy_dir = hermes_home / "lancepro"
    if not legacy_dir.exists():
        return info

    new_db = storage_dir / "memory.sqlite3"
    legacy_db = legacy_dir / "memory.sqlite3"
    if not new_db.exists() and legacy_db.exists():
        src = sqlite3.connect(f"file:{legacy_db}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(new_db)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        info = {
            "migrated": True,
            "source": str(legacy_db),
            "target": str(new_db),
        }

    legacy_config = legacy_dir / "config.json"
    new_config = storage_dir / "config.json"
    if not new_config.exists() and legacy_config.exists():
        shutil.copy2(legacy_config, new_config)
        info["config_copied"] = True

    return info
