#!/usr/bin/env python3
"""Stamp the version and regenerate the wheel module allowlist.

    python scripts/build.package_manifest.py --check   # what is stale
    python scripts/build.package_manifest.py --write   # bring it in line

Change the version in ``_version.py`` and run ``--write``; the two plugin.yaml
manifests, the Codex plugin.json and the packaging allowlist follow.  The
packaging tier fails when anything here is stale, so ``--check`` is what that
failure is telling you to run.

All of the logic lives in ``packaging_hooks/module_inventory.py`` so the build
hook and the tests share it; this file is only the command line.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packaging_hooks.module_inventory import (  # noqa: E402
    expected_allowlist,
    source_version,
    stale_version_files,
    write_all,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="report drift, change nothing")
    group.add_argument("--write", action="store_true", help="rewrite the generated files")
    args = parser.parse_args()

    version = source_version(ROOT)
    if args.write:
        changed = write_all(ROOT)
        print(json.dumps({"version": version, "changed": changed}, ensure_ascii=False, indent=2))
        return 0

    stale = stale_version_files(ROOT)
    allowlist_path = ROOT / "packaging" / "v11-module-allowlist.json"
    current = json.loads(allowlist_path.read_text(encoding="utf-8"))
    expected = expected_allowlist(ROOT)
    missing = sorted(set(expected["python_modules"]) - set(current["python_modules"]))
    extra = sorted(set(current["python_modules"]) - set(expected["python_modules"]))
    payload = {
        "version": version,
        "stale_version_files": stale,
        "modules_missing_from_allowlist": missing,
        "modules_no_longer_reachable": extra,
        "ok": not stale and not missing and not extra and current == expected,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
