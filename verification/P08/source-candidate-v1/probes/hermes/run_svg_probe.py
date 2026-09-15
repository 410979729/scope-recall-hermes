import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import weakref


ROOT = Path(__file__).resolve().parents[2]
HOST = Path(r"F:\Agents\runtime\windows\hermes-tianxuan\hermes-agent")


def network_guard(ports, path):
    bound = weakref.WeakSet()
    def guard(event, args):
        if event == "socket.bind":
            if isinstance(args[1], tuple) and args[1][0] in ("127.0.0.1", "localhost"):
                bound.add(args[0])
            return
        address = args[1] if event == "socket.connect" else args[:2] if event == "socket.getaddrinfo" else None
        if address is None:
            return
        owned_ports = set(ports)
        for listener in list(bound):
            try:
                owned_ports.add(listener.getsockname()[1])
            except OSError:
                pass
        allowed = isinstance(address, tuple) and address[0] in ("127.0.0.1", "localhost") and int(address[1]) in owned_ports
        with path.open("a", encoding="utf-8") as log:
            log.write(json.dumps({"event": event, "address": str(address), "allowed": allowed}) + "\n")
        if not allowed:
            raise RuntimeError("TEST_SVG_NETWORK_DENIED")
    sys.addaudithook(guard)


def child(home, ports):
    home = home.resolve()
    if not home.is_relative_to(ROOT / ".execution") or not home.name.startswith("TEST-P02-svg-") or Path(os.environ["HERMES_HOME"]).resolve() != home:
        raise RuntimeError("Refusing a non-TEST child home")
    if len(ports) != 2 or any(not 1024 < p < 65536 for p in ports):
        raise RuntimeError("Invalid TEST loopback ports")
    if (home / "TEST-P02-profile.json").read_text(encoding="utf-8") != '{"dataset":"SYNTHETIC_TEST_ONLY","purpose":"P02_SVG_INTERFACE_PROBE"}':
        raise RuntimeError("TEST declaration required")
    network_guard(ports, home / "child-network.jsonl")
    sys.argv = ["hermes", "gateway", "run"]
    runpy.run_module("hermes_cli.main", run_name="__main__")


def main():
    import yaml

    state = ROOT / ".execution" / ("TEST-P02-svg-" + str(time.time_ns()))
    state.mkdir(parents=True)
    (state / "TEST-P02-profile.json").write_text('{"dataset":"SYNTHETIC_TEST_ONLY","purpose":"P02_SVG_INTERFACE_PROBE"}', encoding="utf-8")
    shutil.copytree(ROOT / "probes/hermes/svg", state / "plugins/scope-recall-p02-svg", ignore=shutil.ignore_patterns("__pycache__"))
    calls = []
    errors = []

    class Model(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 262144:
                self.send_error(413)
                return
            payload = json.loads(self.rfile.read(length))
            calls.append({"path": self.path, "bytes": length, "model_fixture": True, "stream": payload.get("stream", False)})
            if len(calls) > 24:
                self.send_error(429)
                return
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            try:
                messages = payload["messages"]
                last = messages[-1]
                calls[-1].update(message_roles=[m.get("role") for m in messages], svg_in_request="<svg " in json.dumps(messages), actual_tool_names=[t["function"]["name"] for t in payload.get("tools", [])])
                if last.get("role") == "tool":
                    content = last["content"]
                    reply = {"role": "assistant", "content": content}
                    calls[-1]["tool_result"] = json.loads(content)
                else:
                    users = [m for m in messages if m.get("role") == "user"]
                    current = users[-1]["content"]
                    if isinstance(current, list):
                        current = "\n".join(p.get("text", "") for p in current if isinstance(p, dict))
                    if "SVG_OPEN reference=" in current:
                        ref = re.search(r"SVG_OPEN reference=(svg:[a-f0-9]{64})", current).group(1)
                        schemas = [t["function"]["name"] for t in payload.get("tools", [])]
                        if "p02_svg_open" not in schemas:
                            raise RuntimeError("Public provider tool is absent from actual request")
                        reply = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_svg_" + str(len(calls)), "type": "function", "function": {"name": "p02_svg_open", "arguments": json.dumps({"reference": ref})}}]}
                    else:
                        refs = re.findall(r"TEST_P02_SVG_REF=(svg:[a-f0-9]{64})", json.dumps(messages))
                        if not refs:
                            raise RuntimeError("Actual capture callback supplied no reference")
                        reply = {"role": "assistant", "content": refs[-1]}
                result = {"id": "p02-svg-" + str(len(calls)), "object": "chat.completion", "created": int(time.time()), "model": "p02-local-svg", "choices": [{"index": 0, "message": reply, "finish_reason": "tool_calls" if "tool_calls" in reply else "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
                if payload.get("stream"):
                    delta = dict(reply)
                    for index, tool_call in enumerate(delta.get("tool_calls", [])):
                        tool_call["index"] = index
                    chunk = {"id": result["id"], "object": "chat.completion.chunk", "created": result["created"], "model": result["model"], "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                    end = dict(chunk, choices=[{"index": 0, "delta": {}, "finish_reason": result["choices"][0]["finish_reason"]}], usage=result["usage"])
                    data = ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\ndata: " + json.dumps(end) + "\n\ndata: [DONE]\n\n").encode()
                else:
                    data = json.dumps(result, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if payload.get("stream") else "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                errors.append(type(exc).__name__ + ": " + str(exc))
                self.send_error(400, "Synthetic fixture failed")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        gateway_port = reserved.getsockname()[1]
    ports = {server.server_port, gateway_port}
    network_guard(ports, state / "controller-network.jsonl")
    endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    url = f"http://127.0.0.1:{gateway_port}"
    config = {
        "model": {"default": "p02-local-svg", "provider": "p02-local-svg", "max_tokens": 1024, "context_length": 131072},
        "custom_providers": [{"name": "p02-local-svg", "base_url": endpoint, "key_env": "P02_SVG_FAKE_KEY", "api_mode": "chat_completions", "model": "p02-local-svg", "models": {"p02-local-svg": {"context_length": 131072}}}],
        "agent": {"max_turns": 3, "api_max_retries": 1, "verbose": False, "system_prompt": "Synthetic SVG interface fixture. Use only the TEST provider tool."},
        "memory": {"memory_enabled": True, "user_profile_enabled": False, "nudge_interval": 0, "provider": "scope-recall-p02-svg"},
        "plugins": {"enabled": ["platforms/a2a", "scope-recall-p02-svg"]},
        "gateway": {"platforms": {"a2a": {"enabled": True, "extra": {"host": "127.0.0.1", "port": gateway_port}}}},
        "platform_toolsets": {"a2a": ["memory"]}, "terminal": {"backend": "local", "cwd": str(state)},
        "auxiliary": {"title_generation": {"enabled": False}},
        "compression": {"enabled": False}, "fallback_model": [],
    }
    (state / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT"}}
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
        path = state / "environment" / key.lower()
        path.mkdir(parents=True)
        env[key] = str(path)
    env.update(HERMES_HOME=str(state), A2A_HOST="127.0.0.1", A2A_PORT=str(gateway_port), A2A_AGENT_NAME="TEST_SCOPE_RECALL_SVG", A2A_ALLOW_ALL_USERS="true", P02_SVG_FAKE_KEY="TEST_FAKE_NO_CREDENTIAL", HERMES_MAX_TOKENS="1024", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8", NO_PROXY="*", TERM="dumb", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    command = [str(HOST / "venv/Scripts/python.exe"), "-B", str(Path(__file__).resolve()), "--child", str(state), "--ports", ",".join(map(str, sorted(ports)))]
    record = {"scope": "actual isolated Hermes A2A and public provider tool with a local fake model", "status": "failed", "state": str(state), "host_source": str(HOST), "external_model_calls": 0, "real_credentials_loaded": False, "semantic_quality_test": False, "visual_rendering_test": False, "runs": [], "fixture_errors": errors, "model_requests": calls}

    def send(parts, context, label):
        sent = {"jsonrpc": "2.0", "id": label, "method": "message/send", "params": {"message": {"messageId": label, "role": "ROLE_USER", "parts": parts, "contextId": context}}}
        request = urllib.request.Request(url, json.dumps(sent, ensure_ascii=False).encode(), {"Content-Type": "application/json"})
        with http.open(request, timeout=70) as response:
            received = json.load(response)
        record["runs"].append({"sent": sent, "received": received})
        result = received["result"]
        if result["status"]["state"] != "TASK_STATE_COMPLETED":
            raise RuntimeError("Actual A2A task did not complete")
        return "\n".join(p["text"] for p in result["status"]["message"]["parts"] if "text" in p)

    with (state / "gateway.log").open("wb") as log:
        process = subprocess.Popen(command, cwd=state, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        record.update(process_pid=process.pid, controller_pid=os.getpid(), gateway_url=url, model_url=endpoint)
        print(json.dumps({"state": str(state), "pid": process.pid}), flush=True)
        try:
            deadline = time.monotonic() + 50
            while True:
                try:
                    with http.open(url + "/.well-known/agent-card.json", timeout=1) as response:
                        card = json.load(response)
                    if card["name"] != "TEST_SCOPE_RECALL_SVG":
                        raise RuntimeError("Unexpected gateway identity")
                    warm_log = state / "logs/gateway.log"
                    if warm_log.exists() and "Press Ctrl+C to stop" in warm_log.read_text(encoding="utf-8"):
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Actual gateway warm-up did not complete")
                    time.sleep(0.2)
                except (OSError, urllib.error.URLError):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Isolated gateway failed to start")
                    time.sleep(0.2)
            originals = [
                {"filename": "TEST-same.svg", "svg": '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">\n<rect width="20" height="20" fill="#f00"/>\n</svg>', "comment": "TEST 评价一：保留红色方块。"},
                {"filename": "TEST-same.svg", "svg": '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">\n<circle cx="10" cy="10" r="8" fill="#00f"/>\n</svg>', "comment": "TEST 评价二：蓝色圆形，仍须能回读旧版。"},
            ]
            ref1 = send([{"text": "TEST_SCOPE_RECALL SVG_CAPTURE\n" + json.dumps(originals[0], ensure_ascii=False)}], "TEST-P02-svg-source", "TEST-P02-svg-v1")
            ref2 = send([{"text": "TEST_SCOPE_RECALL SVG_CAPTURE\n"}, {"data": originals[1], "mediaType": "application/json"}], "TEST-P02-svg-source", "TEST-P02-svg-v2")
            if not re.fullmatch(r"svg:[a-f0-9]{64}", ref1) or not re.fullmatch(r"svg:[a-f0-9]{64}", ref2) or ref1 == ref2:
                raise RuntimeError("Observed immutable references are missing or equal")
            opened = []
            for index, reference in enumerate((ref1, ref2)):
                result = json.loads(send([{"text": "TEST_SCOPE_RECALL SVG_OPEN reference=" + reference}], "TEST-P02-svg-reopen-" + str(index), "TEST-P02-svg-open-" + str(index)))
                original = originals[index]
                if any(result[k] != original[k] for k in ("filename", "svg", "comment")) or result["svg_hash"] != hashlib.sha256(original["svg"].encode()).hexdigest():
                    raise RuntimeError("Actual tool/host readback changed original SVG or evaluation")
                opened.append(result)
            record.update(status="interface_probe_pass", opened=opened, original_versions=originals, references=[ref1, ref2], provenance="synthetic_agent_relay_not_human_direct")
        except Exception as exc:
            record["error"] = {"type": type(exc).__name__, "message": str(exc)}
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
            server.shutdown()
            server.server_close()
            record.update(gateway_exit_code=process.returncode, shutdown="owned test process terminated; not graceful shutdown evidence")
    for name in ("controller", "child"):
        path = state / (name + "-network.jsonl")
        record[name + "_network"] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    database = state / "svg-probe.sqlite3"
    if database.exists():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            record["captured_versions"] = [dict(row) for row in db.execute("SELECT * FROM versions ORDER BY ordinal")]
            record["inbound"] = [dict(row) for row in db.execute("SELECT * FROM inbound ORDER BY ordinal")]
            record["events"] = [dict(row) for row in db.execute("SELECT * FROM events ORDER BY ordinal")]
    if record["status"] == "interface_probe_pass":
        events = [(e["event"], json.loads(e["metadata"])) for e in record["events"]]
        source_sessions = {v["session_id"] for v in record["captured_versions"]}
        tool_events = [m for e, m in events if e == "provider_tool"]
        checks = {
            "two_captured_versions": len(record["captured_versions"]) == 2,
            "two_capture_callbacks": sum(e == "pre_llm_capture" for e, m in events) == 2,
            "two_actual_provider_calls": len(tool_events) == 2,
            "two_actual_post_tool_callbacks": sum(e == "post_tool_call" for e, m in events) == 2,
            "new_distinct_sessions": len({m["session_id"] for m in tool_events}) == 2 and all(m["session_id"] not in source_sessions for m in tool_events),
            "six_local_model_requests": len(calls) == 6,
            "no_old_svg_in_new_session_input": len(calls) == 6 and not calls[2]["svg_in_request"] and not calls[4]["svg_in_request"],
            "svg_enters_after_actual_tool": len(calls) == 6 and calls[3]["svg_in_request"] and calls[5]["svg_in_request"],
            "no_fixture_errors": not errors,
            "exact_old_and_new_readback": all(all(record["opened"][i][k] == record["original_versions"][i][k] for k in ("filename", "svg", "comment")) for i in range(2)),
        }
        record["checks"] = checks
        if not all(checks.values()):
            record["status"] = "failed"
    record["scope_limits"] = ["Synthetic relay provenance, not human identity proof", "First pre_gateway_dispatch occurs before provider activation, so the first source is observed at pre_llm_call", "No visual rendering or semantic-model quality claim", "P02 feasibility only; product B10, M46-M50 and actual Codex desktop remain unaccepted"]
    record["probe_files"] = {str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__).resolve(), ROOT / "probes/hermes/svg/__init__.py", ROOT / "probes/hermes/svg/plugin.yaml"]}
    destination = ROOT / "verification/P02" / ("hermes-svg-interface-" + state.name.removeprefix("TEST-P02-svg-") + ".json")
    destination.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": record["status"], "evidence": str(destination), "external_model_calls": 0, "error": record.get("error")}), flush=True)
    return 0 if record["status"] == "interface_probe_pass" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", type=Path)
    parser.add_argument("--ports")
    args = parser.parse_args()
    if args.child:
        child(args.child, {int(p) for p in args.ports.split(",")})
    else:
        raise SystemExit(main())
