"""Explicit, bounded synthetic scenarios against the already running TEST gateway."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import time
import urllib.request
import zlib


STATE = Path(__file__).resolve().parents[2] / ".execution/TEST-P02-hermes"
URL = "http://127.0.0.1:19921"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def rpc(method, params, label):
    sent = {"jsonrpc": "2.0", "id": label, "method": method, "params": params}
    started = time.perf_counter()
    request = urllib.request.Request(URL, json.dumps(sent).encode(), {"Content-Type": "application/json"})
    with HTTP.open(request, timeout=80) as response:
        received = json.load(response)
    return {"sent": sent, "received": received, "duration_seconds": time.perf_counter() - started}


def budget_count():
    with sqlite3.connect((STATE / "call-budget.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        return db.execute("select count(*) from requests").fetchone()[0]


def png(rgb):
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\0" + bytes(rgb))) + chunk(b"IEND", b"")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=["ordinary", "attachments", "cancel"], required=True)
    args = parser.parse_args()
    with HTTP.open(URL + "/.well-known/agent-card.json", timeout=5) as response:
        card = json.load(response)
    if card.get("name") != "TEST_SCOPE_RECALL_P02":
        raise SystemExit("Refusing a non-TEST gateway")
    before = budget_count()
    reserve = 3 if args.scenario == "cancel" else 1
    if before + reserve > 12:
        raise SystemExit("Insufficient approved request allowance for scenario")
    label = "TEST-P02-" + args.scenario
    record = {"scenario": args.scenario, "scope": "actual isolated Hermes A2A", "budget_before": before, "started_ns": time.time_ns()}
    prompt = "TEST_SCOPE_RECALL What is 2 + 3? Answer with the number only. Do not use tools."
    extra = []
    if args.scenario == "attachments":
        prompt = "TEST_SCOPE_RECALL Two revisions of the same synthetic image are attached in order, first version 1 then version 2. State whether their actual pixels are available to you. If they are unavailable, say UNAVAILABLE. Do not infer pixels from filenames or use tools."
        blobs = [png((255, 0, 0)), png((0, 0, 255))]
        extra = [{"raw": base64.b64encode(blob).decode(), "filename": "TEST-P02-same-image.png", "mediaType": "image/png"} for blob in blobs]
        record["attachment_versions"] = [{"ordinal": i + 1, "filename": "TEST-P02-same-image.png", "sha256": hashlib.sha256(blob).hexdigest(), "byte_length": len(blob)} for i, blob in enumerate(blobs)]
    elif args.scenario == "cancel":
        prompt = "TEST_SCOPE_RECALL Run exactly one harmless terminal command: sleep 12 . Then answer TEST_P02_WAIT_FINISHED. Do not read or modify files or run any other tool. The caller may cancel this synthetic test."
    params = {"message": {"messageId": label, "role": "ROLE_USER", "parts": [{"text": prompt}] + extra, "contextId": label}, "configuration": {"returnImmediately": args.scenario == "cancel"}}
    record["send"] = rpc("message/send", params, label)
    if args.scenario == "cancel":
        task_id = record["send"]["received"]["result"]["id"]
        deadline = time.monotonic() + 20
        while budget_count() == before and time.monotonic() < deadline:
            time.sleep(0.1)
        record["upstream_reserved_before_cancel"] = budget_count() > before
        record["cancel"] = rpc("tasks/cancel", {"id": task_id}, label + "-cancel")
        time.sleep(25)
        record["readback"] = rpc("tasks/get", {"id": task_id}, label + "-readback")
    record.update(budget_after=budget_count(), completed_ns=time.time_ns())
    destination = STATE / ("actual-" + args.scenario + ".json")
    destination.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(destination), "budget_before": before, "budget_after": record["budget_after"], "result": record["send"]["received"]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
