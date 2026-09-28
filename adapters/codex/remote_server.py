"""A client on another machine, attached to a shared store here: its hooks and MCP tools over HTTP.

Claude Code or Codex on that machine keeps no store.  Its hooks forward each payload here with the entry's
token (``remote_client``), and this server runs the handler a local client's hooks run, so the entry, its
grants and its capture scope are this machine's, fixed by the token and never by what the client sends.  A
Claude Code client reads its own session record there and sends what the record shows being said
(``transcript.said``): the record itself never leaves that machine.  The entry's MCP tools are served over
streamable HTTP at ``/mcp``.

Listen only on an address the client reaches privately, a tailnet one, and give each entry its own token.
This machine keeps the token's SHA-256 in ``<home>/scope-recall/remote-server.json``; the token itself
stays on the client's machine, where ``remote_client token`` made it.  ``serve`` runs without a console: each
hook, each refused request and the server's own errors go to ``remote-server.log`` beside the config.

    python -m scope_recall.adapters.codex.remote_server configure --home <home> --host claude-code \
        --listen 100.64.0.5 --port 18765 --token-sha256 <hex>
    python -m scope_recall.adapters.codex.remote_server serve --home <home> --host claude-code [--env-file <file>]
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import json
import logging
import logging.handlers
import os
from pathlib import Path
import re
import time
from typing import Any

from ...runtime.resume_entry import host_process_credential_environment
from . import transcript
from .config import CodexConfigError, load_shared_client
from .handler import CodexHookHandler, RecordLines, SystemHookClock

CONFIG_NAME = "remote-server.json"
LOG_NAME = "remote-server.log"
#: The log is kept to about this size, with two older copies.
LOG_BYTES = 1024 * 1024
#: A hook payload, as a local hook reads it from stdin.
MAX_PAYLOAD_BYTES = 65536
#: One request: the payload and the lines a client read from its record in one Stop.
MAX_REQUEST_BYTES = 8 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_log = logging.getLogger("scope_recall.remote_server")


class RemoteServerError(ValueError):
    pass


#: A client's clock may run this far ahead of this machine's; a time past it is this machine's now.  The time
#: a hook carries dates the source and when its work falls due, so a work computer a day fast delayed its
#: messages' embeddings by a day.  An earlier time is left alone: a hook sent late from the spool is one.
CLOCK_AHEAD_SECONDS = 60


def _not_ahead(moment: datetime) -> datetime:
    now = datetime.now(timezone.utc)
    return now if moment > now + timedelta(seconds=CLOCK_AHEAD_SECONDS) else moment


@dataclass(frozen=True)
class RemoteServerConfig:
    home: Path
    host: str
    listen: str
    port: int
    token_sha256: str


def config_path(home: Path) -> Path:
    return home / "scope-recall" / CONFIG_NAME


def _listen_address(value: object) -> str:
    try:
        address = ipaddress.ip_address(str(value))
    except ValueError:
        raise RemoteServerError("listen must be an IP address of this machine") from None
    if address.is_unspecified or address.is_multicast:
        raise RemoteServerError("listen on one private address (a tailnet one), never on every interface")
    return str(address)


def _port(value: object) -> int:
    if type(value) is not int or not 1024 <= value <= 65535:
        raise RemoteServerError("port must be an integer from 1024 to 65535")
    return value


def _digest(value: object) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise RemoteServerError("token_sha256 must be 64 lowercase hex digits")
    return value


def write_server_config(home: Path, host: str, *, listen: str, port: int, token_sha256: str) -> Path:
    """Record where the entry is served and the SHA-256 of the client's token; the client must exist."""
    load_shared_client(home, host)
    path = config_path(home)
    body = {"schema": "scope-recall.remote-server/1", "host": host, "listen": _listen_address(listen),
            "port": _port(port), "token_sha256": _digest(token_sha256)}
    pending = path.with_name(f"{path.stem}.{os.getpid()}.tmp")
    pending.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    os.replace(pending, path)
    return path


def load_server_config(home: Path, host: str) -> RemoteServerConfig:
    try:
        raw = json.loads(config_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RemoteServerError(f"no readable {CONFIG_NAME} under {home}: configure it first") from exc
    if not isinstance(raw, dict) or raw.get("schema") != "scope-recall.remote-server/1" or raw.get("host") != host:
        raise RemoteServerError(f"{CONFIG_NAME} is not a remote server config for {host}")
    return RemoteServerConfig(home=home, host=host, listen=_listen_address(raw.get("listen")),
                              port=_port(raw.get("port")), token_sha256=_digest(raw.get("token_sha256")))


def token_matches(authorization: bytes | str | None, token_sha256: str) -> bool:
    """Whether an ``Authorization: Bearer <token>`` header carries the token whose SHA-256 this is."""
    if isinstance(authorization, bytes):
        authorization = authorization.decode("latin-1")
    if type(authorization) is not str or not authorization.startswith("Bearer "):
        return False
    token = authorization[len("Bearer "):].strip()
    if not token:
        return False
    return hmac.compare_digest(hashlib.sha256(token.encode("utf-8")).hexdigest(), token_sha256)


class _ObservedClock(SystemHookClock):
    """A hook the client could not send when it happened: its moment, not the replay's."""

    def __init__(self, observed_at: str) -> None:
        self._observed_at = observed_at

    def utc_now(self) -> str:
        return self._observed_at


def _observed_at(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise RemoteServerError("observed_at must be an ISO time")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RemoteServerError("observed_at must be an ISO time") from None
    if moment.tzinfo is None:
        raise RemoteServerError("observed_at must carry its offset")
    return _not_ahead(moment.astimezone(timezone.utc)).isoformat().replace("+00:00", "Z")


def _said_not_ahead(said: transcript.Said | None) -> transcript.Said | None:
    if said is None:
        return None
    moment = datetime.fromisoformat(said.occurred_at.replace("Z", "+00:00"))
    bounded = _not_ahead(moment)
    return said if bounded is moment else replace(said, occurred_at=bounded.isoformat().replace("+00:00", "Z"))


def record_from_wire(value: object) -> RecordLines | None:
    """The lines a client read from its record, checked; None when it sent none."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RemoteServerError("record must be an object")
    start, lines = value.get("start"), value.get("lines")
    if type(start) is not int or start < 0 or not isinstance(lines, list):
        raise RemoteServerError("record needs start and lines")
    checked: list[tuple[int, transcript.Said | None]] = []
    position = start
    for item in lines:
        if not isinstance(item, list) or len(item) != 2 or type(item[0]) is not int or item[0] <= position:
            raise RemoteServerError("record lines need increasing offsets past start")
        position = item[0]
        checked.append((position, _said_not_ahead(transcript.said_from_wire(item[1])) if item[1] is not None else None))
    return RecordLines(start=start, lines=checked)


def handle_request(config: RemoteServerConfig, body: dict[str, Any], *, started: float | None = None) -> dict[str, Any]:
    """One forwarded hook: the handler's answer, and how far the client's record was stored."""
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise RemoteServerError("payload must be the hook's object")
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise RemoteServerError("payload too large")
    record = record_from_wire(body.get("record"))
    # The client's transcript_path is a file on its own machine; nothing here opens a path a request names.
    payload = {key: value for key, value in payload.items() if key != "transcript_path"}
    observed_at = _observed_at(body.get("observed_at"))
    handler = CodexHookHandler.from_home(
        str(config.home), config.host,
        clock=_ObservedClock(observed_at) if observed_at is not None else None,
        hook_started_at=started if started is not None else time.monotonic(),
    )
    try:
        result = handler.handle_payload(payload, record=record, local_record=False)
    finally:
        handler.close()
    # ``retry``: the store was busy or away and the event was not stored; a client that keeps its hooks sends
    # this one again.  The answer (a recall) is good either way.
    return {"result": result, "through": record.through if record is not None else None,
            "reason": handler.diagnostics.last_reason, "error": handler.diagnostics.capture_error_detail,
            "retry": not handler.diagnostics.capture_settled}


def build_app(config: RemoteServerConfig):
    """The entry's MCP tools at ``/mcp``, its hook at ``/hook``, both behind the token."""
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from .mcp_server import build_server

    client = load_shared_client(config.home, config.host)
    host_name = f"[{config.listen}]" if ":" in config.listen else config.listen
    tools = build_server(client, workspace=None)
    app = tools.server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        host=config.listen,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            # An IPv6 address comes in the Host header in brackets; unbracketed, every /mcp call got 421.
            allowed_hosts=[host_name, f"{host_name}:{config.port}"],
            allowed_origins=[],
        ),
    )

    async def hook(request: Request) -> JSONResponse:
        started = time.monotonic()
        size = request.headers.get("content-length")
        if size is None or not size.isdigit() or int(size) > MAX_REQUEST_BYTES:
            _log.warning("hook refused: %s bytes", size)
            return JSONResponse({"error": "request_too_large_or_unsized"}, status_code=413)
        raw = await request.body()
        if len(raw) > MAX_REQUEST_BYTES:
            return JSONResponse({"error": "request_too_large"}, status_code=413)
        try:
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise RemoteServerError("body must be an object")
            payload = body.get("payload")
            event = str(payload.get("hook_event_name"))[:40] if isinstance(payload, dict) else None
            answer = await run_in_threadpool(handle_request, config, body, started=started)
        except (UnicodeError, json.JSONDecodeError, RemoteServerError) as exc:
            # The request itself, which the client drops on a 400.  An error from the store (a ContractError is a
            # ValueError too) is this machine's and answers 500, so the client keeps the hook to send again.
            _log.warning("hook refused: %s", str(exc)[:200])
            return JSONResponse({"error": "invalid_request", "detail": str(exc)[:200]}, status_code=400)
        except CodexConfigError as exc:
            _log.error("hook: the entry is unavailable: %s", str(exc)[:200])
            return JSONResponse({"error": "entry_unavailable"}, status_code=503)
        # The error is the capture's code (DEADLINE_EXCEEDED, SECRET_DETECTED, ...), never any of its text.
        _log.info("hook %s: %s%s%s, record through %s, %d ms", event, answer["reason"],
                  f" ({answer['error']})" if answer.get("error") else "",
                  ", not stored, to be sent again" if answer.get("retry") else "", answer["through"],
                  round((time.monotonic() - started) * 1000))
        return JSONResponse(answer)

    async def health(request: Request) -> JSONResponse:
        from ..._version import __version__
        return JSONResponse({"entry_id": client.entry_id, "host": config.host, "version": __version__})

    app.router.routes.append(Route("/hook", hook, methods=["POST"]))
    app.router.routes.append(Route("/health", health, methods=["GET"]))
    return _TokenGate(app, config.token_sha256)


#: How much of a refused request is read before the 401 goes out.
_DRAIN_BYTES = 1 << 20


async def _drain(receive) -> None:
    """Read what a refused client sent, up to ``_DRAIN_BYTES``.  Answered first, the connection was closed with the
    request unread, and Windows resets such a socket: the client got WinError 10053 instead of the 401."""
    read = 0
    while read <= _DRAIN_BYTES:
        message = await receive()
        if message.get("type") != "http.request":
            return
        read += len(message.get("body") or b"")
        if not message.get("more_body"):
            return


class _TokenGate:
    """Every HTTP request carries the entry's token or gets 401; lifespan events pass."""

    def __init__(self, app, token_sha256: str) -> None:
        self.app = app
        self.token_sha256 = token_sha256

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or ())
            if not token_matches(headers.get(b"authorization"), self.token_sha256):
                from starlette.responses import JSONResponse
                client = scope.get("client") or ("?", 0)
                _log.warning("refused %s %s from %s: no valid token", scope.get("method"), scope.get("path"), client[0])
                await _drain(receive)
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def log_to_file(home: Path) -> logging.Handler:
    """Send this server's lines, and warnings from the libraries it runs, to ``remote-server.log``."""
    handler = logging.handlers.RotatingFileHandler(config_path(home).with_name(LOG_NAME), maxBytes=LOG_BYTES,
                                                   backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    _log.setLevel(logging.INFO)
    return handler


def serve(config: RemoteServerConfig, *, env_file: Path | None = None) -> None:
    import uvicorn

    if env_file is not None:
        client = load_shared_client(config.home, config.host)
        os.environ.update(host_process_credential_environment(client.runtime_config_path, env_file))
    log_to_file(config.home)
    from ..._version import __version__
    _log.info("serving the %s entry at %s on %s:%d (%s)", config.host, config.home, config.listen, config.port,
              __version__)
    # log_config=None keeps uvicorn's own errors in the file above instead of a console there is none of.
    uvicorn.run(build_app(config), host=config.listen, port=config.port, log_level="warning", log_config=None,
                timeout_graceful_shutdown=5)


def _absolute(value: str, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SystemExit(f"{field} must be absolute")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scope-recall-remote-server", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("configure", help="record where an entry is served and its client's token digest")
    run = sub.add_parser("serve", help="serve an entry to its client on another machine")
    for command in (setup, run):
        command.add_argument("--home", required=True, help="absolute home of the client's entry, on this machine")
        command.add_argument("--host", required=True, choices=("codex", "claude-code"))
    setup.add_argument("--listen", required=True, help="this machine's private (tailnet) address")
    setup.add_argument("--port", required=True, type=int)
    setup.add_argument("--token-sha256", required=True, help="what remote_client token printed on the client")
    run.add_argument("--env-file", default=None, help="absolute file holding the credential names the runtime "
                     "config declares, read in place")
    args = parser.parse_args(argv)
    home = _absolute(args.home, "home")
    try:
        if args.command == "configure":
            path = write_server_config(home, args.host, listen=args.listen, port=args.port, token_sha256=args.token_sha256)
            print(json.dumps({"status": "configured", "config": str(path)}))
            return 0
        serve(load_server_config(home, args.host),
              env_file=_absolute(args.env_file, "env_file") if args.env_file else None)
    except (RemoteServerError, CodexConfigError) as exc:
        raise SystemExit(str(exc)) from None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
