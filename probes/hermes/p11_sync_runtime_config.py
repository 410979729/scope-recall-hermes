"""Synchronize the active TEST runtime config after a reserve-policy correction."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from probes.hermes.p11_a2a_testkit import (
    ARCHIVE, AUX_RESERVE_OUTPUT, BUDGET_CONFIG, CORE_DIR, LEDGER, MAIN_MODEL,
    MAIN_RESERVE_OUTPUT, RUNTIME_CONFIG, STATE, budget_mapping, load_json, scrub,
    write_json,
)


def main() -> int:
    if not RUNTIME_CONFIG.is_file() or not STATE.is_dir():
        raise SystemExit("active TEST prepare is required")
    old_bytes = RUNTIME_CONFIG.read_bytes()
    old_sha = hashlib.sha256(old_bytes).hexdigest()
    old = load_json(RUNTIME_CONFIG)
    budget = old.get("auxiliary", {}).get("budget", {})
    old_reserve = dict(budget.get("model_reserve_output", {}))
    receipt = {
        "kind": "runtime-config-before-reserve-fix",
        "path": str(RUNTIME_CONFIG), "sha256": old_sha,
        "old_model_reserve_output": old_reserve,
        "credential_values_written": False, "network_calls": 0,
    }
    write_json(ARCHIVE / "runtime-config-before-reserve-fix.json", scrub(receipt))
    updated = dict(old)
    updated_aux = dict(updated["auxiliary"])
    updated_budget = dict(updated_aux["budget"])
    updated_budget["model_reserve_output"] = {
        MAIN_MODEL: MAIN_RESERVE_OUTPUT, "mimo-v2.5": AUX_RESERVE_OUTPUT, "glm-5.3-flash": 4_096,
    }
    updated_aux["budget"] = updated_budget
    updated["auxiliary"] = updated_aux
    write_json(RUNTIME_CONFIG, updated)
    write_json(BUDGET_CONFIG, budget_mapping())
    new_sha = hashlib.sha256(RUNTIME_CONFIG.read_bytes()).hexdigest()
    result = {
        "synced": True, "path": str(RUNTIME_CONFIG), "old_sha256": old_sha,
        "new_sha256": new_sha, "old_model_reserve_output": old_reserve,
        "new_model_reserve_output": updated_budget["model_reserve_output"],
        "ledger": str(LEDGER), "credential_values_written": False, "network_calls": 0,
    }
    write_json(ARCHIVE / "runtime-config-reserve-fix.json", scrub(result))
    print(json.dumps(scrub(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
