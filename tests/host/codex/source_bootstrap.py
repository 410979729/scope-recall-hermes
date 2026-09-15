"""Source-tree subprocess bootstrap for hook_entry CLI tests.

Clean-wheel ``python -I -m scope_recall.adapters.codex.hook_entry`` coverage is a
separate P14 obligation. This module only wires the checkout source tree.
"""
from __future__ import annotations

import os
from pathlib import Path

CHECKOUT_ROOT = Path(__file__).resolve().parents[3]

HOOK_ENTRY_BOOTSTRAP = """
from scope_recall.adapters.codex.hook_entry import main
raise SystemExit(main())
"""


def subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(CHECKOUT_ROOT / "tests"), str(CHECKOUT_ROOT)])
    return env
