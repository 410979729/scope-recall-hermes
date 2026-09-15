"""Stable maintenance migration entry point.

The v1.1 migration contract has one implementation in :mod:`migrate_v2`.
This module remains as the public import and ``python -m maintenance.migrate``
entry point so existing callers do not acquire a second migration engine.
"""

from __future__ import annotations

from .migrate_v2 import (
    LEGACY_BASELINE,
    MigrationError,
    main,
    migrate_legacy,
)

__all__ = ["LEGACY_BASELINE", "MigrationError", "main", "migrate_legacy"]


if __name__ == "__main__":
    raise SystemExit(main())
