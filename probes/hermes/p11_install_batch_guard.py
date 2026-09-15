"""Install the P11-only atomic batch ceiling on the frozen shared TEST ledger."""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from probes.hermes.p11_a2a_testkit import ARCHIVE, BATCH_GUARD_NAME, LEDGER, STATE, install_batch_guard, shared_ledger_status, write_json


def main() -> int:
    install_batch_guard()
    result = {"installed": True, "trigger": BATCH_GUARD_NAME, "ledger": str(LEDGER),
              "scope": "P11_A2A_V2 only; other batches unchanged", "status": shared_ledger_status(),
              "network_calls": 0, "model_posts": 0}
    write_json(ARCHIVE / "batch-guard.json", result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
