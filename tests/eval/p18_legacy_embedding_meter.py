"""TEST-only loopback adapter for the frozen B OpenAI embedding client."""
import hashlib
import hmac
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from scope_recall.adapters.models import HttpsTransport
from scope_recall.runtime.auxiliary import _budget_policy_from_mapping
from scope_recall.runtime.model_budget import AuxiliaryBudgetLedger

MODEL = "gemini-embedding-001"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/openai/embeddings"

_DRAIN_SCRIPT = r'''
import importlib,importlib.util,json,os,pathlib,socket,sys
p=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
original_connect=socket.socket.connect
def loopback_only(self,address):
    if address[0] not in ("127.0.0.1","localhost") or address[1]!=p["port"]:
        raise RuntimeError("TEST legacy drain permits metered loopback only")
    return original_connect(self,address)
socket.socket.connect=loopback_only
os.environ["HERMES_HOME"]=p["home"]
sys.path.insert(0,p["source"])
archive=pathlib.Path(p["plugin"])
spec=importlib.util.spec_from_file_location("scope_recall",archive/"__init__.py",submodule_search_locations=[str(archive)])
m=importlib.util.module_from_spec(spec);sys.modules["scope_recall"]=m;spec.loader.exec_module(m)
provider=importlib.import_module("scope_recall.provider").ScopeRecallMemoryProvider()
try:
    provider.initialize("TEST-B-public-vector-drain",hermes_home=p["home"],platform="cli",agent_context="primary",agent_identity="default",agent_workspace="hermes")
    repair=provider.command_repair_vector()
    flushed=bool(provider.flush(timeout=15.0))
    result={"repair":repair,"flushed":flushed,"public_api":"initialize/command_repair_vector/flush/shutdown"}
    pathlib.Path(p["result"]).write_text(json.dumps(result,ensure_ascii=False,default=str),encoding="utf-8")
finally:provider.shutdown(timeout=5.0)
'''


def drain_legacy_embeddings(binding, owner, output):
    if not binding.get("legacy_embedding_meter"):
        raise ValueError("B_metered_embedding_binding_required")
    output=Path(output);input_path=output/"baseline-embedding-drain-input.json";result_path=output/"baseline-embedding-drain-result.json"
    # With raw import deliberately deferring vector startup, the legacy
    # provider requires its public initial-generation migration before repair.
    migration=Path(binding["loader"]["plugin_directory"]["path"])/"scripts/migrate.vector_generation.py"
    migrated=owner.run_process([binding["fixed_host"]["runtime_python_path"],"-I","-B",str(migration),
        "--hermes-home",binding["roots"]["home_path"],"--generation-id","TEST-P18-B-initial", "--apply","--activate","--json"],
        output,label="baseline-vector-migration",timeout_seconds=90)
    if migrated["returncode"]!=0 or migrated["error"]:
        raise ValueError("B_public_vector_generation_migration_failed")
    payload={"home":binding["roots"]["home_path"],"source":binding["fixed_host"]["source"]["path"],
        "plugin":binding["loader"]["plugin_directory"]["path"],"result":str(result_path),"port":binding["legacy_embedding_meter"]["port"]}
    input_path.write_text(json.dumps(payload),encoding="utf-8")
    process=owner.run_process([binding["fixed_host"]["runtime_python_path"],"-I","-B","-c",_DRAIN_SCRIPT,str(input_path)],output,label="baseline-embedding-drain",timeout_seconds=90)
    if process["returncode"]!=0 or process["error"] or not result_path.is_file():
        raise ValueError("B_public_embedding_drain_process_failed")
    result=json.loads(result_path.read_text(encoding="utf-8"));repair=result.get("repair",{});vector=repair.get("vector",{})
    # Ready plus all reported debt counters zero is required before query.
    embedder=vector.get("embedder",{})
    if embedder.get("model")!=MODEL or embedder.get("dimensions")!=3072:
        raise ValueError("B_public_embedding_model_or_dimensions_changed")
    if not result.get("flushed") or not repair.get("repaired") or vector.get("status")!="ready" or any(vector.get("debt_counts",{}).values()):
        raise ValueError("B_public_embedding_drain_not_ready")
    return {"status":"READY","result_path":str(result_path),"result_sha256":hashlib.sha256(result_path.read_bytes()).hexdigest(),"public_api":result["public_api"]}


class LegacyEmbeddingMeter:
    def __init__(self, *, ledger_path, policy, output, key, token, transport=None):
        self.ledger = AuxiliaryBudgetLedger(Path(ledger_path), _budget_policy_from_mapping(policy))
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.key, self.token = key, token
        self.transport = transport or HttpsTransport()
        self.server = self.thread = None

    def handle(self, raw):
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("model") != MODEL or not payload.get("input"):
            raise ValueError("frozen_B_embedding_model_and_input_required")
        if payload.get("dimensions", 3072) != 3072:
            raise ValueError("frozen_B_embedding_dimensions_required")
        if not self.key:
            raise ValueError("B_embedding_upstream_key_missing")
        inputs = payload["input"]
        if isinstance(inputs, list) and len(inputs) > 1:
            if not all(isinstance(text, str) for text in inputs):
                raise ValueError("B_batch_requires_text_inputs")
            data, children, prompt_tokens = [], [], []
            audit = {"request_sha256": hashlib.sha256(raw).hexdigest(), "input_count": len(inputs),
                     "strategy": "one_request_per_input_in_original_order", "children": children,
                     "model": MODEL, "dimensions": 3072, "credential_values_written": False}
            audit_path = self.output / f"batch-{time.time_ns()}.json"
            try:
                for index, text in enumerate(inputs):
                    single = json.dumps({**payload, "input": [text]}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    status, body = self.handle(single)
                    children.append({"original_index": index, "request_sha256": hashlib.sha256(single).hexdigest(),
                                     "response_sha256": hashlib.sha256(body).hexdigest(), "http_status": status})
                    if status >= 400:
                        audit["status"] = "UPSTREAM_FAILED_PARTIAL_ROWS_RETAINED"
                        return status, body
                    response = json.loads(body)
                    entries = response.get("data", [])
                    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
                        raise ValueError("B_single_input_response_row_count")
                    row = entries[0]
                    if row.get("index") is not None and (type(row["index"]) is not int or row["index"] != 0):
                        raise ValueError("B_single_input_response_index")
                    # Exact singleton request identifies the row without guessing
                    # omitted batch indices. Embedding values are untouched.
                    data.append({**row, "index": index})
                    usage = response.get("usage")
                    prompt_tokens.append(usage.get("prompt_tokens") if isinstance(usage, dict) else None)
                result = {"object": "list", "model": MODEL, "data": data}
                if all(type(count) is int and count >= 0 for count in prompt_tokens):
                    result["usage"] = {"prompt_tokens": sum(prompt_tokens), "total_tokens": sum(prompt_tokens)}
                audit["status"] = "COMPLETE_ORDER_PRESERVED"
                return 200, json.dumps(result, separators=(",", ":")).encode("utf-8")
            except Exception as exc:
                audit.update(status="REJECTED_PARTIAL_ROWS_RETAINED", error_type=type(exc).__name__, error=str(exc)[:160])
                raise
            finally:
                audit_path.write_text(json.dumps(audit, indent=2), encoding="utf-8")
        rid = self.ledger.reserve(MODEL, raw, reserved_input=max(8192, len(raw)), reserved_output=0)
        status, body, error = 502, b"", None
        try:
            status, body = self.transport.post(ENDPOINT, body=raw,
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.key},
                timeout_seconds=30, max_response_bytes=16*1024*1024)
        except Exception as exc:
            error = type(exc).__name__
        try: response = json.loads(body)
        except (ValueError, UnicodeError): response = {}
        usage = response.get("usage") if isinstance(response, dict) else None
        settled_usage = {"promptTokenCount": usage["prompt_tokens"]} if isinstance(usage,dict) and type(usage.get("prompt_tokens")) is int else None
        settled = self.ledger.finish_embedding(rid, "completed" if status < 400 and error is None else "network_error", settled_usage)
        request_path = self.output / f"embedding-{rid}.request.bin"
        response_path = self.output / f"embedding-{rid}.response.bin"
        request_path.write_bytes(raw)
        response_path.write_bytes(body)
        receipt = {"model": MODEL, "dimensions": 3072, "ledger_request_id": rid,
            "request_artifact": str(request_path), "response_artifact": str(response_path),
            "request_sha256": hashlib.sha256(raw).hexdigest(), "response_sha256": hashlib.sha256(body).hexdigest(),
            "http_status": status, "ledger_status": settled, "error_type": error,
            "usage": usage, "credential_values_written": False}
        (self.output/f"embedding-{rid}.json").write_text(json.dumps(receipt,indent=2),encoding="utf-8")
        return status, body or b'{"error":"TEST upstream transport failed"}'

    def start(self, port=29990):
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                status,body=400,b'{"error":"TEST embedding request rejected"}'
                try:
                    bearer_ok=self.path=="/v1/embeddings" and hmac.compare_digest(self.headers.get("Authorization",""),"Bearer "+owner.token)
                    # Frozen 578b deliberately strips credential headers on
                    # HTTP. A random path capability stays in child env only.
                    path_ok=hmac.compare_digest(self.path,"/"+owner.token+"/v1/embeddings")
                    if not (bearer_ok or path_ok):
                        raise ValueError("route_or_token")
                    size=int(self.headers.get("Content-Length","0"))
                    if not 0<size<=786432:raise ValueError("request_size")
                    raw=self.rfile.read(size)
                    if len(raw)!=size:raise ValueError("truncated")
                    status,body=owner.handle(raw)
                except Exception:pass
                self.send_response(status);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
        self.server=ThreadingHTTPServer(("127.0.0.1",port),Handler)
        self.thread=Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        return self.server.server_port

    def close(self):
        if self.server:
            self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
            self.server=self.thread=None
