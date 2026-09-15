"""Request shutdown of only the owned TEST launcher."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import os
import sys
import time

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from probes.hermes.p11_a2a_testkit import ARCHIVE, HERMES_PYTHON, RUNTIME_RECORD, STATE, STOP_FILE, load_json, write_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()
    if not RUNTIME_RECORD.is_file():
        print(json.dumps({"stopped": False, "reason": "runtime_record_missing"}, sort_keys=True))
        return 2
    record = load_json(RUNTIME_RECORD)
    command = record.get("gateway_command", [])
    if not isinstance(command, list) or not command or str(HERMES_PYTHON) not in " ".join(str(item) for item in command):
        print(json.dumps({"stopped": False, "reason": "not_owned_official_gateway_record"}, sort_keys=True))
        return 2
    STOP_FILE.write_text("requested_by_TEST_launcher\n", encoding="utf-8")
    result = {"stopped": True, "stop_file": str(STOP_FILE), "launcher_pid": record.get("controller_pid"),
              "gateway_pid": record.get("gateway_pid"), "bridge_pid": record.get("bridge_pid"),
              "waited": False, "arbitrary_process_kill": False}
    if args.wait:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if any((ARCHIVE / name).is_file() for name in os.listdir(ARCHIVE) if name.startswith("stop-")):
                result["waited"] = True
                break
            time.sleep(0.25)
    write_json(ARCHIVE / f"stop-request-{time.time_ns()}.json", result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
