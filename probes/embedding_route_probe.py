import argparse
import hashlib
import http.client
import importlib.util
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]


def credential():
    vault = r"F:\Agents\shared\beidou\access-vault\bin\beidou-secret.py"
    result = subprocess.run([sys.executable, "-B", vault, "get", "api:beidou-gemini-embedding", "--instance", "tianji"], capture_output=True, timeout=12)
    if result.returncode:
        raise RuntimeError("Owner-authorized embedding credential lookup failed")
    try:
        matches = set(re.findall(r"AIza[A-Za-z0-9_-]{35}", result.stdout.decode("utf-8-sig")))
    except (UnicodeError, ValueError):
        raise RuntimeError("Embedding credential parsing failed") from None
    finally:
        result.stdout = b""
        result.stderr = b""
    if len(matches) != 1:
        raise RuntimeError("Expected one embedding credential in the authorized vault entry")
    return matches.pop()


def main(use_existing_proxy=False):
    approval = json.loads((ROOT / "verification/G0/model-route-selection.json").read_text(encoding="utf-8"))
    if not approval.get("numeric_api_budget_approved") or approval["proposed_aggregate"]["api_value_hard_cap_usd"] != 20 or approval["proposed_aggregate"]["embedding_batches"] != 4096:
        raise SystemExit("Embedding route budget not approved")
    spec = importlib.util.spec_from_file_location("p02_meter", ROOT / "probes/cheap_model_probe.py")
    meter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(meter)
    ledger = meter.Ledger(meter.STATE / "call-budget.sqlite3", "P02_EMBEDDING_PROXY_ROUTE" if use_existing_proxy else "P02_EMBEDDING_ROUTE")
    if any(r["batch"] == ledger.batch for r in ledger.snapshot()["requests"]):
        raise SystemExit("Embedding diagnostic has a reservation; no automatic replay")
    if use_existing_proxy:
        proxy = urllib.parse.urlparse(urllib.request.getproxies().get("https", ""))
        if proxy.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise SystemExit("Expected the already configured local HTTPS proxy")
    key = credential()
    body = {"model": "gemini-embedding-001", "input": "TEST_SCOPE_RECALL The synthetic TEST-ORBIT project uses a blue circle.", "dimensions": 3072, "encoding_format": "float"}
    raw = json.dumps(body).encode()
    request_id = ledger.reserve_embedding(raw)
    record = {"request_id": request_id, "request": body, "passed": False, "external_requests": 1, "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai/embeddings", "credential_source": "api:beidou-gemini-embedding via owner-authorized Beidou access", "credential_saved": False, "production_data_sent": False, "automatic_retry": False, "billing_tier": "not_inferred_from_response"}
    record["transport"] = "existing_system_local_https_proxy" if use_existing_proxy else "direct_https"
    connection = None if use_existing_proxy else http.client.HTTPSConnection("generativelanguage.googleapis.com", timeout=25)
    response = None
    status, usage = "network_error", None
    started = time.perf_counter()
    try:
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": "ScopeRecall-P02-EmbeddingProbe/1.1"}
        if use_existing_proxy:
            try:
                response = urllib.request.urlopen(urllib.request.Request(record["endpoint"], data=raw, headers=headers), timeout=25)
            except urllib.error.HTTPError as exc:
                response = exc
        else:
            connection.request("POST", "/v1beta/openai/embeddings", body=raw, headers=headers)
            response = connection.getresponse()
        status = "http_" + str(response.status)
        record["http_status"] = response.status
        encoded = response.read(262145)
        if len(encoded) > 262144:
            raise ValueError("response_size")
        data = json.loads(encoded)
        if response.status == 200:
            original_usage = data.get("usage", {})
            tokens = original_usage.get("prompt_tokens")
            if type(tokens) is int and tokens > 0:
                usage = {"prompt_tokens": tokens, "completion_tokens": 0}
            record["usage"] = {k: original_usage.get(k) for k in ("prompt_tokens", "total_tokens")}
            entries = data.get("data", [])
            vector = entries[0].get("embedding") if len(entries) == 1 else None
            valid = isinstance(vector, list) and len(vector) == 3072 and all(type(x) in (int, float) and math.isfinite(x) for x in vector)
            record["dimensions"] = len(vector) if isinstance(vector, list) else None
            record["finite_nonzero_vector"] = bool(valid and sum(x * x for x in vector) > 0)
            record["passed"] = record["finite_nonzero_vector"]
            if valid:
                record["vector_sha256"] = hashlib.sha256(json.dumps(vector, separators=(",", ":")).encode()).hexdigest()
        else:
            error = data.get("error", {})
            record["error_status"] = error.get("status")
            record["error_code"] = error.get("code")
    except (OSError, http.client.HTTPException, ValueError, TypeError, KeyError, IndexError) as exc:
        record["error_type"] = type(exc).__name__
    finally:
        if response is not None:
            response.close()
        if connection is not None:
            connection.close()
        key = ""
        ledger.finish(request_id, status, usage)
        record["duration_seconds"] = time.perf_counter() - started
    record.update(budget=ledger.snapshot(), source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), meter_source_sha256=hashlib.sha256((ROOT / "probes/cheap_model_probe.py").read_bytes()).hexdigest(), scope="Single synthetic embedding route/dimension check; not retrieval quality, scale, or LanceDB integration")
    destination = ROOT / "verification/P02/embedding-route-actual.json"
    destination.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: record.get(k) for k in ("passed", "http_status", "dimensions", "usage", "error_type", "error_status", "duration_seconds")}), flush=True)
    raise SystemExit(0 if record["passed"] else 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-authorized-model-probe", action="store_true", required=True)
    parser.add_argument("--use-existing-proxy", action="store_true")
    args = parser.parse_args()
    main(args.use_existing_proxy)
