import hashlib
import json
import os
from pathlib import Path
import sqlite3


def register(ctx):
    from agent.memory_provider import MemoryProvider
    from hermes_cli.plugins import iter_hook_callbacks

    home = Path(os.environ["HERMES_HOME"]).resolve()
    roots = [p for p in Path(__file__).resolve().parents if (p / "probes/hermes/svg/__init__.py").is_file()]
    if not roots or not home.is_relative_to(roots[0] / ".execution") or not home.name.startswith("TEST-P02-svg-"):
        raise RuntimeError("Isolated SVG probe home required")
    if (home / "TEST-P02-profile.json").read_text(encoding="utf-8") != '{"dataset":"SYNTHETIC_TEST_ONLY","purpose":"P02_SVG_INTERFACE_PROBE"}':
        raise RuntimeError("SVG probe declaration mismatch")
    database = home / "svg-probe.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE IF NOT EXISTS inbound (ordinal INTEGER PRIMARY KEY, message_id TEXT, context_id TEXT, sender TEXT, text_hash TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS versions (ordinal INTEGER PRIMARY KEY, reference TEXT UNIQUE, session_id TEXT, turn_id TEXT UNIQUE, filename TEXT, svg TEXT, comment TEXT, svg_hash TEXT, text_hash TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS events (ordinal INTEGER PRIMARY KEY, event TEXT, metadata TEXT)")

    def log(event, **metadata):
        with sqlite3.connect(database) as db:
            db.execute("INSERT INTO events(event,metadata) VALUES (?,?)", (event, json.dumps(metadata, ensure_ascii=False)))

    def body(text):
        if isinstance(text, list):
            text = "\n".join(p.get("text", "") for p in text if isinstance(p, dict) and p.get("type") == "text")
        if not isinstance(text, str):
            return ""
        if text.startswith("[A2A inbound "):
            text = text.partition("\n\n")[2]
        return text if text.startswith("TEST_SCOPE_RECALL SVG_") else ""

    def inbound(event, **kwargs):
        text = body(event.text)
        if text:
            with sqlite3.connect(database) as db:
                db.execute("INSERT INTO inbound(message_id,context_id,sender,text_hash) VALUES (?,?,?,?)", (event.message_id, event.source.chat_id, event.source.user_id, hashlib.sha256(text.encode()).hexdigest()))

    def capture(session_id, turn_id, user_message, **kwargs):
        text = body(user_message)
        prefix = "TEST_SCOPE_RECALL SVG_CAPTURE\n"
        if not text.startswith(prefix):
            return None
        encoded = text[len(prefix):].lstrip("\n")
        if encoded.startswith("[data (application/json)]\n"):
            encoded = encoded.partition("\n")[2]
        value = json.loads(encoded)
        if set(value) != {"filename", "svg", "comment"} or value["filename"] != "TEST-same.svg":
            raise ValueError("Unexpected synthetic SVG envelope")
        if any(not isinstance(v, str) or len(v.encode()) > 8192 for v in value.values()) or not value["svg"].startswith("<svg "):
            raise ValueError("Invalid bounded SVG probe input")
        digest = hashlib.sha256(value["svg"].encode()).hexdigest()
        reference = "svg:" + hashlib.sha256((session_id + turn_id + digest).encode()).hexdigest()
        with sqlite3.connect(database) as db:
            db.execute("INSERT OR IGNORE INTO versions(reference,session_id,turn_id,filename,svg,comment,svg_hash,text_hash) VALUES (?,?,?,?,?,?,?,?)", (reference, session_id, turn_id, value["filename"], value["svg"], value["comment"], digest, hashlib.sha256(text.encode()).hexdigest()))
        log("pre_llm_capture", session_id=session_id, turn_id=turn_id, reference=reference, svg_hash=digest, origin="synthetic_agent_relay", human_direct=False)
        return {"context": "TEST_P02_SVG_REF=" + reference}

    def tool_observed(**kwargs):
        log("post_tool_call", **{k: v for k, v in kwargs.items() if k in {"session_id", "turn_id", "tool_name", "tool_call_id"} and isinstance(v, str)})

    class Provider(MemoryProvider):
        @property
        def name(self):
            return "scope-recall-p02-svg"

        def is_available(self):
            return True

        def initialize(self, session_id, **kwargs):
            if Path(kwargs["hermes_home"]).resolve() != home:
                raise RuntimeError("SVG home binding mismatch")
            self.session_id = session_id

        def on_session_switch(self, new_session_id, **kwargs):
            self.session_id = new_session_id

        def get_tool_schemas(self):
            return [{"name": "p02_svg_open", "description": "Open an immutable synthetic SVG reference captured by this isolated probe.", "parameters": {"type": "object", "properties": {"reference": {"type": "string"}}, "required": ["reference"], "additionalProperties": False}}]

        def handle_tool_call(self, tool_name, args, **kwargs):
            if tool_name != "p02_svg_open" or set(args) != {"reference"} or not isinstance(args["reference"], str):
                raise ValueError("Invalid probe tool call")
            with sqlite3.connect(database) as db:
                row = db.execute("SELECT reference,session_id,turn_id,filename,svg,comment,svg_hash FROM versions WHERE reference=?", (args["reference"],)).fetchone()
            if row is None:
                result = {"status": "not_found", "reference": args["reference"]}
            else:
                result = dict(zip(("reference", "source_session_id", "source_turn_id", "filename", "svg", "comment", "svg_hash"), row))
                result.update(status="available", origin="synthetic_agent_relay", human_direct=False)
            log("provider_tool", session_id=self.session_id, tool_name=tool_name, result=result)
            return json.dumps(result, ensure_ascii=False)

    ctx.register_memory_provider(Provider())
    for name, callback in (("pre_gateway_dispatch", inbound), ("pre_llm_call", capture), ("post_tool_call", tool_observed)):
        identity = ("scope-recall-p02-svg", str(home), name)
        if not any(getattr(c, "p02_svg_identity", None) == identity for c in iter_hook_callbacks(name)):
            callback.p02_svg_identity = identity
            ctx.register_hook(name, callback)
