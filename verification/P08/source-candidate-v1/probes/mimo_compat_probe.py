import argparse
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import time


ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def main():
    approval = json.loads((ROOT / "verification/G0/model-route-selection.json").read_text(encoding="utf-8"))
    if not approval.get("numeric_api_budget_approved") or approval["proposed_aggregate"]["api_value_hard_cap_usd"] != 20:
        raise SystemExit("Project model budget is not approved")
    probe = module("p02_meter", ROOT / "probes/cheap_model_probe.py")
    ledger = probe.Ledger(probe.STATE / "call-budget.sqlite3", "P02_MIMO_COMPAT")
    if any(row["batch"] == ledger.batch for row in ledger.snapshot()["requests"]):
        raise SystemExit("This three-call diagnostic has reservations; no automatic replay")
    key = module("p02_credential_loader", ROOT / "probes/hermes/run_gateway_probe.py").credential()
    results = []
    for case in ("json", "tool", "output_limit"):
        record = {"case": case, "model": "mimo-v2.5", "passed": False}
        try:
            record["account_usage_before_call"] = probe.check_go_allowance(key)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            record["preflight_error_type"] = type(exc).__name__
            results.append(record)
            break
        body = probe.payload("mimo-v2.5", "tool" if case == "tool" else "json")
        body["max_completion_tokens"] = body.pop("max_tokens")
        body["thinking"] = {"type": "disabled"}
        if case == "tool":
            body["tool_choice"] = "auto"
        if case == "output_limit":
            body["max_completion_tokens"] = 16
            body.pop("response_format")
            body["messages"][0]["content"] = "Follow the synthetic output-length request. Do not summarize."
            body["messages"][1]["content"] = "TEST_SCOPE_RECALL Print the integers 1 through 400, in order, one integer per line. Print the full sequence."
        raw = json.dumps(body, ensure_ascii=False).encode()
        request_id = ledger.reserve("mimo-v2.5", raw)
        record.update(request_id=request_id, request_sha256=hashlib.sha256(raw).hexdigest(), request=body)
        connection = http.client.HTTPSConnection("opencode.ai", timeout=30)
        status, usage = "network_error", None
        started = time.perf_counter()
        try:
            connection.request("POST", "/zen/go/v1/chat/completions", body=raw, headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": "ScopeRecall-P02-MiMoCompat/1.1", "x-opencode-session": "scope-recall-TEST-P02-mimo-compat"})
            response = connection.getresponse()
            record["http_status"] = response.status
            status = "http_" + str(response.status)
            encoded = response.read(1048577)
            if len(encoded) > 1048576:
                raise ValueError("response_limit")
            answer = json.loads(encoded)
            usage = answer.get("usage")
            if response.status != 200:
                record["error_type"] = str(answer.get("error", {}).get("type", "provider_error"))[:80]
            else:
                choice = answer["choices"][0]
                message = choice["message"]
                record.update(response_model=answer.get("model"), finish_reason=choice.get("finish_reason"), reasoning_present=bool(message.get("reasoning_content")), usage={k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")} if isinstance(usage, dict) else None)
                reported_cost = answer.get("cost")
                if type(reported_cost) in (int, float, dict):
                    record["provider_reported_cost"] = reported_cost
                content = message.get("content", "")
                record["visible_content"] = content[:8192] if isinstance(content, str) else None
                calls = message.get("tool_calls") or []
                record["tool_calls"] = [{"id": c.get("id"), "type": c.get("type"), "function": c.get("function")} for c in calls]
                if case == "json":
                    record["passed"] = json.loads(content) == {"project": "TEST-ORBIT", "current_color": "蓝色", "tool_status": "failed", "assistant_claim_is_tool_evidence": False, "scope": "project"}
                elif case == "tool":
                    record["passed"] = len(calls) == 1 and calls[0]["function"]["name"] == "inspect_artifact" and json.loads(calls[0]["function"]["arguments"]) == {"reference": "TEST-ARTIFACT-v1"}
                else:
                    record["passed"] = choice.get("finish_reason") == "length" and isinstance(usage, dict) and type(usage.get("completion_tokens")) is int and 0 < usage["completion_tokens"] <= 16
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, IndexError) as exc:
            record["error_type"] = type(exc).__name__
        finally:
            connection.close()
            ledger.finish(request_id, status, usage)
            record["duration_seconds"] = time.perf_counter() - started
            results.append(record)
            print(json.dumps({k: record.get(k) for k in ("case", "passed", "http_status", "error_type", "duration_seconds")}), flush=True)
    key = ""
    result = {"scope": "Three-call public diagnostic; parameter compatibility only, no hidden evaluation or final semantic acceptance", "original_failure_retained": "verification/P02/cheap-model-actual.json", "hidden_reasoning_saved": False, "credentials_saved": False, "production_data_sent": False, "automatic_retry": False, "budget_ledger_reset": False, "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "meter_source_sha256": hashlib.sha256((ROOT / "probes/cheap_model_probe.py").read_bytes()).hexdigest(), "results": results, "budget": ledger.snapshot()}
    destination = ROOT / "verification/P02/mimo-compat-actual.json"
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    passed = sum(r["passed"] for r in results)
    print(json.dumps({"evidence": str(destination), "passed": passed, "failed": len(results) - passed, "not_run": 3 - len(results), "charge_micro_usd": result["budget"]["charge_micro_usd"]}), flush=True)
    raise SystemExit(0 if passed == 3 else 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-authorized-model-probe", action="store_true", required=True)
    parser.parse_args()
    main()
