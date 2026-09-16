"""Fail-closed, TEST-only model runtime with shared P02 accounting."""
from __future__ import annotations
from contextlib import closing
from dataclasses import dataclass
import hashlib, http.client, importlib.util, json, os, sqlite3, time
from pathlib import Path
from scope_recall.core.secret_patterns import contains_secret_like_text

ROOT=Path(__file__).resolve().parents[1]; STATE=ROOT/".execution/TEST-MODEL-BUDGET-V1"; LEDGER_PATH=STATE/"call-budget.sqlite3"
MODELS={"mimo-v2.5","deepseek-v4-flash","glm-5.3-flash"}; BATCH_CAPS={"P07_DEVELOPMENT":32,"P09_DEVELOPMENT":100,"P18_EVALUATION":8000}
RESERVE_INPUT=32768; MAX_REQUEST_BYTES=32000; MAX_OUTPUT=4096; MIMO_RESERVE_OUTPUT=131072
TOTAL_INPUT_CAP=64_000_000; TOTAL_OUTPUT_CAP=8_000_000; TOTAL_CALL_CAP=8000; CAP_MICRO_USD=20_000_000

def _cheap():
    spec=importlib.util.spec_from_file_location("scope_recall_p02_meter",ROOT/"probes/cheap_model_probe.py"); m=importlib.util.module_from_spec(spec); assert spec.loader is not None; spec.loader.exec_module(m); return m
def cost(model,input_tokens,output_tokens):
    if model not in MODELS or any(type(v) is not int or v<0 for v in (input_tokens,output_tokens)): raise ValueError("invalid_meter")
    return _cheap().cost(model,input_tokens,output_tokens)
def _json_bytes(value): return json.dumps(value,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode()
def _validate_test_messages(messages):
    if not isinstance(messages,list) or not messages: raise ValueError("synthetic_input_required")
    for msg in messages:
        if not isinstance(msg,dict) or set(msg)-{"role","content"} or msg.get("role") not in {"system","user","assistant","tool"} or not isinstance(msg.get("content"),str): raise ValueError("message_shape")
    if not messages[-1]["content"].startswith("TEST_SCOPE_RECALL "): raise ValueError("synthetic_input_required")

class RuntimeLedger:
    def __init__(self,path,batch):
        if batch not in BATCH_CAPS: raise ValueError("unapproved_batch")
        self.path,self.batch=path,batch; path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(path)) as db,db: db.execute("CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY,batch TEXT,model TEXT,body_sha256 TEXT,request_bytes INTEGER,reserved_input INTEGER,reserved_output INTEGER,actual_input INTEGER,actual_output INTEGER,charge_micro_usd INTEGER,status TEXT,started_ns INTEGER)")
    def reserve(self,model,body):
        if model not in MODELS or not 0<len(body)<=MAX_REQUEST_BYTES: raise ValueError("unsupported_model_or_size")
        try: req=json.loads(body)
        except (TypeError,ValueError,json.JSONDecodeError): raise ValueError("request_json")
        if req.get("model")!=model or req.get("n",1)!=1 or req.get("stream") is not False: raise ValueError("request_limit")
        _validate_test_messages(req.get("messages")); field="max_completion_tokens" if model=="mimo-v2.5" else "max_tokens"
        if type(req.get(field)) is not int or not 1<=req[field]<=MAX_OUTPUT: raise ValueError("request_limit")
        other="max_tokens" if field=="max_completion_tokens" else "max_completion_tokens"
        if other in req or (model in {"mimo-v2.5","deepseek-v4-flash"} and req.get("thinking")!={"type":"disabled"}): raise ValueError("request_limit")
        reserve_output=MIMO_RESERVE_OUTPUT if model=="mimo-v2.5" else MAX_OUTPUT; amount=cost(model,RESERVE_INPUT,reserve_output); cap_in,cap_out=_cheap().TOKEN_CAPS[model]
        with closing(sqlite3.connect(self.path,timeout=2)) as db,db:
            db.execute("BEGIN IMMEDIATE"); batch_count=db.execute("SELECT COUNT(*) FROM requests WHERE batch=?",(self.batch,)).fetchone()[0]; used=db.execute("SELECT COALESCE(SUM(charge_micro_usd),0) FROM requests").fetchone()[0]
            calls,inputs,outputs=db.execute("SELECT COUNT(*),COALESCE(SUM(COALESCE(actual_input,reserved_input)),0),COALESCE(SUM(COALESCE(actual_output,reserved_output)),0) FROM requests WHERE model IN (?,?,?)",tuple(MODELS)).fetchone(); mi,mo=db.execute("SELECT COALESCE(SUM(COALESCE(actual_input,reserved_input)),0),COALESCE(SUM(COALESCE(actual_output,reserved_output)),0) FROM requests WHERE model=?",(model,)).fetchone(); blocked=db.execute("SELECT 1 FROM requests WHERE status='meter_breach' LIMIT 1").fetchone()
            if batch_count>=BATCH_CAPS[self.batch] or calls>=TOTAL_CALL_CAP or inputs+RESERVE_INPUT>TOTAL_INPUT_CAP or outputs+reserve_output>TOTAL_OUTPUT_CAP or mi+RESERVE_INPUT>cap_in or mo+reserve_output>cap_out or used+amount>CAP_MICRO_USD or blocked: raise ValueError("budget_exhausted_or_meter_breach")
            return db.execute("INSERT INTO requests(batch,model,body_sha256,request_bytes,reserved_input,reserved_output,charge_micro_usd,status,started_ns) VALUES (?,?,?,?,?,?,?,?,?)",(self.batch,model,hashlib.sha256(body).hexdigest(),len(body),RESERVE_INPUT,reserve_output,amount,"reserved_before_network",time.time_ns())).lastrowid
    def finish(self,request_id,status,usage):
        with closing(sqlite3.connect(self.path,timeout=2)) as db,db:
            db.execute("BEGIN IMMEDIATE"); row=db.execute("SELECT model,status,reserved_input,reserved_output FROM requests WHERE id=?",(request_id,)).fetchone()
            if row is None or row[1]!="reserved_before_network": raise ValueError("reservation_state")
            inp=usage.get("prompt_tokens") if isinstance(usage,dict) else None; out=usage.get("completion_tokens") if isinstance(usage,dict) else None; valid=type(inp) is int and type(out) is int and min(inp,out)>=0
            if valid:
                if inp>row[2] or out>MAX_OUTPUT: status="meter_breach"
                db.execute("UPDATE requests SET status=?,actual_input=?,actual_output=?,charge_micro_usd=? WHERE id=?",(status,inp,out,cost(row[0],inp,out),request_id))
            else: status=status+"_usage_unknown_reserved_charge_retained"; db.execute("UPDATE requests SET status=? WHERE id=?",(status,request_id))
            return status

@dataclass(frozen=True)
class RuntimeResult: content:str|None; usage:dict|None; ledger_id:int; status:str; error_type:str|None=None
def _is_link(path): return path.is_symlink() or (hasattr(os.path,"isjunction") and os.path.isjunction(path))
def _linked_ancestor(path):
    while True:
        if _is_link(path): return True
        if path.parent==path: return False
        path=path.parent

def _safe_allowance(value):
    """Keep the three documented Go windows, never provider extensions."""
    if not isinstance(value,dict): return None
    result={}
    for name in ('rolling','weekly','monthly'):
        window=value.get(name)
        if not isinstance(window,dict): continue
        status,percent=window.get('status'),window.get('percent')
        if status!='ok' or type(percent) not in (int,float) or not 0<=percent<80:
            raise ValueError('account_allowance_guard')
        clean={'status':'ok','percent':percent}
        reset=window.get('resetsAt')
        if type(reset) is int or (type(reset) is str and len(reset)<=64):clean['resetsAt']=reset
        result[name]=clean
    return result

class EvalModelRuntime:
    def __init__(self,*,batch,audit_dir,transport=None,key_loader=None,allowance_checker=None,ledger_path=LEDGER_PATH,allow_glm=False):
        if not isinstance(audit_dir,Path) or not audit_dir.is_absolute() or "TEST" not in str(audit_dir) or _linked_ancestor(audit_dir): raise ValueError("test_audit_directory_required")
        audit_dir=audit_dir.resolve(strict=False)
        if transport is None and (not audit_dir.is_relative_to((ROOT/".execution").resolve()) or not any("TEST" in part for part in audit_dir.relative_to((ROOT/".execution").resolve()).parts)): raise ValueError("live_audit_directory_required")
        self.batch,self.audit_dir,self.transport=batch,audit_dir,transport; self.key_loader,self.allowance_checker,self.allow_glm=key_loader,allowance_checker,allow_glm; self.ledger=RuntimeLedger(LEDGER_PATH if transport is None else ledger_path,batch)
    def _post(self,body,key):
        if self.transport is not None: return self.transport(body)
        c=http.client.HTTPSConnection("opencode.ai",timeout=55)
        try:
            c.request("POST","/zen/go/v1/chat/completions",body=body,headers={"Authorization":"Bearer "+key,"Content-Type":"application/json","User-Agent":"ScopeRecall-P07-EvalRuntime/1.1","x-opencode-session":"scope-recall-TEST-"+self.batch}); r=c.getresponse(); raw=r.read(1_048_577)
            if len(raw)>1_048_576: raise ValueError("response_limit")
            return r.status,json.loads(raw)
        finally: c.close()
    def _write_exclusive(self,name,record):
        encoded=_json_bytes(record)
        if contains_secret_like_text(encoded.decode()): raise ValueError("sensitive_evidence")
        self.audit_dir.mkdir(parents=True,exist_ok=True)
        with (self.audit_dir/name).open("xb") as h:
            h.write(encoded); h.flush(); os.fsync(h.fileno())
    @staticmethod
    def _validate_options(response_format,tools,tool_choice):
        if response_format is not None and (not isinstance(response_format,dict) or set(response_format)!={"type"} or response_format["type"] not in {"text","json_object"}): raise ValueError("response_format_shape")
        if tools is not None:
            if not isinstance(tools,list): raise ValueError("tools_shape")
            for t in tools:
                if not isinstance(t,dict) or set(t)-{"type","function"} or t.get("type")!="function" or not isinstance(t.get("function"),dict): raise ValueError("tools_shape")
                f=t["function"]
                if set(f)-{"name","description","parameters"} or not isinstance(f.get("name"),str) or not isinstance(f.get("parameters"),dict): raise ValueError("tools_shape")
        if tool_choice is not None:
            if isinstance(tool_choice,str):
                if tool_choice not in {"auto","none","required"}: raise ValueError("tool_choice_shape")
            elif not (isinstance(tool_choice,dict) and set(tool_choice)=={"type","function"} and tool_choice["type"]=="function" and isinstance(tool_choice["function"],dict) and set(tool_choice["function"])=={"name"} and isinstance(tool_choice["function"]["name"],str)):
                raise ValueError("tool_choice_shape")
    def send(self,*,model,messages,response_format=None,tools=None,tool_choice=None):
        if model not in MODELS or (model=="glm-5.3-flash" and not self.allow_glm): raise ValueError("model_not_approved")
        _validate_test_messages(messages); self._validate_options(response_format,tools,tool_choice); body={"model":model,"messages":messages,"stream":False,"n":1}
        if model=="mimo-v2.5": body.update(max_completion_tokens=MAX_OUTPUT,thinking={"type":"disabled"})
        else: body["max_tokens"]=MAX_OUTPUT
        if model=="deepseek-v4-flash":body['thinking']={'type':'disabled'}
        if response_format is not None: body["response_format"]=response_format
        if tools is not None: body["tools"]=tools
        if tool_choice is not None: body["tool_choice"]=tool_choice
        raw=_json_bytes(body)
        if len(raw)>MAX_REQUEST_BYTES: raise ValueError("request_too_large")
        if contains_secret_like_text(raw.decode()): raise ValueError("sensitive_request")
        meter=_cheap(); key=self.key_loader() if self.key_loader is not None else None
        if key is None:
            spec=importlib.util.spec_from_file_location("scope_recall_eval_credentials",ROOT/"probes/hermes/run_gateway_probe.py"); assert spec.loader is not None; module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); key=module.credential()
        checked_ns=time.time_ns()
        try: allowance=(self.allowance_checker or meter.check_go_allowance)(key)
        except Exception as exc:
            self._write_exclusive(f"preflight-{checked_ns}.json",{"status":"preflight_failed","error_type":type(exc).__name__,"checked_ns":checked_ns}); raise
        ledger_id=self.ledger.reserve(model,raw)
        try: self._write_exclusive(f"request-{ledger_id}.json",{"status":"reserved_before_network","checked_ns":checked_ns,"allowance":_safe_allowance(allowance),"request":body})
        except Exception: self.ledger.finish(ledger_id,"evidence_write_failed",None); raise
        status,usage,content,error_type="network_error",None,None,None
        try:
            code,payload=self._post(raw,key); status="http_"+str(code)
            if code!=200: error_type="provider_error"
            elif not isinstance(payload,dict): error_type="malformed_response"
            else:
                candidate=payload.get("usage"); allowed={"prompt_tokens","completion_tokens","total_tokens"}
                if not isinstance(candidate,dict) or any(type(candidate.get(k)) is not int or candidate[k]<0 for k in ("prompt_tokens","completion_tokens")): error_type="usage_unknown"
                else:
                    usage={k:candidate[k] for k in allowed if k in candidate and type(candidate[k]) is int and candidate[k]>=0}; choices=payload.get("choices")
                    if not isinstance(choices,list) or not choices or not isinstance(choices[0],dict) or not isinstance(choices[0].get("message"),dict) or not isinstance(choices[0]["message"].get("content"),str): error_type="malformed_response"
                    else: content=choices[0]["message"]["content"]
        except (OSError,http.client.HTTPException,ValueError,KeyError,TypeError,IndexError): error_type="malformed_response"
        status=self.ledger.finish(ledger_id,status,usage)
        if "usage_unknown" in status or status=="meter_breach": content=None
        self._write_exclusive(f"response-{ledger_id}.json",{"status":status,"error_type":error_type,"usage":usage,"content":content})
        return RuntimeResult(content,usage,ledger_id,status,error_type)
