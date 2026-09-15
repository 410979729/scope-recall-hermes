"""Strict attribution of frozen Hermes's uncounted iteration-summary call."""
import json

SUMMARY_REQUEST = "You've reached the maximum number of tool-calling iterations allowed. Please provide a final response summarizing what you've found and accomplished so far, without calling any more tools."


def validate_summary_gap(record, response, observed, accounted, base):
    from p18_hermes_cli_transport import _read_ref
    entries = sorted(accounted["entries"], key=lambda item: item["id"])
    export = observed["export_usage"]
    refs = response.get("provider_response_archives", [])
    if (record["usage"]["status"] != "known" or export.get("status") != "known"
            or export["api_call_count"] + 1 != len(entries) or len(entries) < 2
            or len(refs) != len(entries)):
        raise ValueError("summary_gap_count_or_usage")
    archives = {}
    request_refs = {item["id"]: item["request"] for item in record["usage"]["entries"]}
    for ref in refs:
        raw = json.loads(_read_ref(ref, base))
        key = raw.get("ledger_request_id")
        if key in archives:
            raise ValueError("summary_gap_duplicate_archive")
        archives[key] = raw
    for entry in entries:
        raw = archives.get(entry["id"], {})
        usage = raw.get("response", {}).get("usage", {})
        if (raw.get("http_status") != 200
                or raw.get("request") != json.loads(_read_ref(request_refs[entry["id"]], base))
                or raw.get("request_artifact_sha256") != entry["request_sha256"]
                or raw.get("p18_active_operation", {}).get("operation_id") != record["operation_id"]
                or usage.get("prompt_tokens") != entry["actual_input"]
                or usage.get("completion_tokens") != entry["actual_output"]):
            raise ValueError("summary_gap_archive_ledger_binding")
    if (sum(e["actual_input"] for e in entries[:-1]) != export["input_tokens"]
            or sum(e["actual_output"] for e in entries[:-1]) != export["output_tokens"]):
        raise ValueError("summary_gap_export_prefix")
    for entry in entries[:-1]:
        choices = archives[entry["id"]].get("response", {}).get("choices", [])
        if len(choices) != 1 or choices[0].get("finish_reason") != "tool_calls":
            raise ValueError("summary_gap_non_tool_iteration")
    last = archives[entries[-1]["id"]]
    messages = last.get("request", {}).get("messages", [])
    choices = last.get("response", {}).get("choices", [])
    if (not messages or messages[-1].get("role") != "user" or messages[-1].get("content") != SUMMARY_REQUEST
            or len(choices) != 1 or choices[0].get("finish_reason") != "stop"
            or choices[0].get("message", {}).get("tool_calls")
            or choices[0].get("message", {}).get("content") != observed["answer_text"]):
        raise ValueError("summary_gap_final_not_bound")
    return {"kind": "frozen_Hermes_iteration_summary_missing_from_export", "ledger_authoritative": True,
            "unexported_ledger_id": entries[-1]["id"], "input_tokens": entries[-1]["actual_input"],
            "output_tokens": entries[-1]["actual_output"], "all_calls_accounted": True}
