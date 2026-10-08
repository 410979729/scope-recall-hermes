"""Claude Code or Codex on another machine: forward each hook to its entry's server over HTTP.

This runs where the client runs.  It keeps no store, no model key and no memory: each hook's payload goes to
the entry's server (``remote_server``) with the entry's token, and the server's answer is the hook's answer.
A Claude Code Stop or SessionEnd also reads this machine's session record from a cursor kept here and sends
what the record shows being said (``transcript.said``); the cursor moves only as far as the server stored.

When the server cannot be reached the hook answers with nothing, so the client is held up only briefly: a
connection that has not opened in ``CONNECT_SECONDS`` is given up, and for ``AWAY_SECONDS`` after that no
hook tries.  A Claude Code session's record carries what was said to that session's next Stop that gets
through; what a session had not sent when it ended stays unsent.  A Codex hook is kept in a spool here and
sent, with the moment it happened, by the next hook that reaches the server; a full spool drops its oldest.
Requests go straight to the server, never through a proxy this machine has for the internet, which cannot
reach a private address.  What did not get through, and what the spool dropped, is logged in the state folder.
A WorkBuddy client (``host: workbuddy``) is forwarded as the claude-code host's is, its record read with
``transcript.workbuddy_said``.  WorkBuddy has no plugin here: ``install`` merges the hooks and the MCP server into
WorkBuddy's own settings.json and mcp.json (``--plugin-dir`` names WorkBuddy's home), as the local installer does,
keeping everything else in them and a copy of each file it changes under the state folder's ``backups``.

    python -m scope_recall.adapters.codex.remote_client token --config <client.json>
    python -m scope_recall.adapters.codex.remote_client install --config <client.json> --plugin-dir <dir>
    python -m scope_recall.adapters.codex.remote_client --config <client.json>        (the hook itself)
    python -m scope_recall.adapters.codex.remote_client flush --config <client.json>  (started by a hook)

``client.json`` holds ``url`` (the server, e.g. ``http://100.64.0.10:18765``), ``host`` (``claude-code``,
``codex`` or ``workbuddy``), ``token_file`` and ``state_dir``, all absolute.  The token never leaves this
machine except in the requests' ``Authorization`` header.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import itertools
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import transcript
from .boundary import EMPTY_ANSWER, is_workbuddy_agent_run, without_lone_surrogates

HOSTS = ("claude-code", "codex", "workbuddy")
#: Hosts whose Stop and SessionEnd send the lines of their own session record (``transcript``).
_RECORD_HOSTS = frozenset({"claude-code", "workbuddy"})
#: How long the client's host waits for each hook (the plugin's hooks.json): the local installers' ceilings
#: (``maintenance/install_claude_code.py``, ``maintenance/install_codex.py``), for the events forwarded.  A hook
#: answers as soon as the server does; the server's work is bounded by the entry's budget.  WorkBuddy's hooks are
#: given the same waits as the claude-code host's: its prompt is blocked by a hook that runs past its wait.
HOOK_TIMEOUTS = {
    "claude-code": {"UserPromptSubmit": 15, "Stop": 10, "SessionEnd": 10},
    "codex": {"SessionStart": 5, "UserPromptSubmit": 15, "Stop": 10, "Interrupt": 3, "SessionEnd": 3},
    "workbuddy": {"UserPromptSubmit": 15, "Stop": 10, "SessionEnd": 10},
}
#: The part of each wait the request may use; the interpreter's start and the answer take the rest.
_REQUEST_SHARE = 0.8
#: A Stop sends at most this much of the record; a long backlog goes over several turns.
RECORD_READ_BYTES = 2 * 1024 * 1024
#: Codex hooks kept for later, oldest first; past this many the oldest is dropped.
SPOOL_LIMIT = 256
#: How long a flush of the spool may run, in its own process after a hook that got through.
FLUSH_SECONDS = 60.0
#: A connection to the server not open after this long is given up (over a tailnet relay one opens in about
#: a second), so a prompt is not held for the hook's whole wait while the server is away.
CONNECT_SECONDS = 3.0
#: After a connection could not be opened, hooks do not try for this long: a client whose server is away is
#: held up once in this time, not at every prompt.
AWAY_SECONDS = 60.0
#: The log of requests that did not get through is kept to about this size (one older copy is kept).
LOG_BYTES = 256 * 1024
_SPOOLED = frozenset({"UserPromptSubmit", "Stop", "SessionEnd", "Interrupt"})
#: The server refused the request itself (malformed, too large): sent again it is refused again, so it is
#: dropped and said in the log rather than kept at the head of the spool, where it stopped every later flush.
_REFUSED_FOR_GOOD = frozenset({400, 413})
_MAX_STDIN = 65536


class RemoteClientError(ValueError):
    pass


def absolute_path(value: object, field: str) -> Path:
    if type(value) is not str or not value.strip():
        raise RemoteClientError(f"{field} must be an absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RemoteClientError(f"{field} must be an absolute path")
    return path


def load_client_config(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RemoteClientError(f"unreadable client config {path}") from exc
    if not isinstance(raw, dict) or raw.get("host") not in HOSTS:
        raise RemoteClientError(f"client config needs host: {', '.join(HOSTS)}")
    url = raw.get("url")
    if (
        type(url) is not str
        or urllib.parse.urlsplit(url).scheme not in ("http", "https")
        or not urllib.parse.urlsplit(url).hostname
    ):
        raise RemoteClientError("client config needs the server's url")
    return {
        "url": url.rstrip("/"),
        "host": raw["host"],
        "token_file": absolute_path(raw.get("token_file"), "token_file"),
        "state_dir": absolute_path(raw.get("state_dir"), "state_dir"),
        "config": path,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _log(config: dict[str, Any], line: str) -> None:
    """One line in the state folder's log: a request that did not get through, or what the spool did."""
    path = config["state_dir"] / "remote-client.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > LOG_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{_now()} {line}\n")
    except OSError:
        pass


def _away_marker(config: dict[str, Any]) -> Path:
    return config["state_dir"] / "server-away"


def _server_away(config: dict[str, Any]) -> bool:
    """Whether a connection failed less than ``AWAY_SECONDS`` ago."""
    try:
        return time.time() - _away_marker(config).stat().st_mtime < AWAY_SECONDS
    except OSError:
        return False


def _post(config: dict[str, Any], body: dict[str, Any], timeout: float) -> dict[str, Any] | None:
    """The server's answer, or None when it could not be reached in time or refused.

    The connection is opened to the server itself, never through a proxy: HTTP_PROXY and the system proxy
    are for the internet, and one on 127.0.0.1 would take the request away from the private network.
    """
    event = (body.get("payload") or {}).get("hook_event_name")
    try:
        token = config["token_file"].read_text(encoding="utf-8").strip()
    except OSError as exc:
        _log(config, f"{event}: no token to send ({type(exc).__name__})")
        return None
    url = urllib.parse.urlsplit(config["url"])
    kind = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    timeout = max(0.2, timeout)
    until = time.monotonic() + timeout
    connection = kind(url.hostname, url.port, timeout=min(CONNECT_SECONDS, timeout))
    try:
        try:
            connection.connect()
        except OSError as exc:
            _log(config, f"{event}: no connection ({type(exc).__name__}); not trying for {AWAY_SECONDS:.0f} s")
            try:
                _away_marker(config).parent.mkdir(parents=True, exist_ok=True)
                _away_marker(config).write_text(_now(), encoding="utf-8")
            except OSError:
                pass
            return None
        connection.sock.settimeout(max(0.2, until - time.monotonic()))
        connection.request(
            "POST",
            f"{url.path}/hook",
            body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        response = connection.getresponse()
        raw = response.read()
        if response.status in _REFUSED_FOR_GOOD:
            _log(config, f"{event}: HTTP {response.status}, refused for good: not kept")
            return {"refused": response.status}
        if response.status != 200:
            _log(config, f"{event}: HTTP {response.status}")
            return None
        answer = json.loads(raw.decode("utf-8"))
    except (OSError, http.client.HTTPException, ValueError) as exc:
        _log(config, f"{event}: no answer in {timeout:.1f} s ({type(exc).__name__})")
        return None
    finally:
        connection.close()
    try:
        _away_marker(config).unlink(missing_ok=True)
    except OSError:
        pass
    return answer if isinstance(answer, dict) else None


# -- the spool (Codex) -------------------------------------------------------


def _spool_dir(config: dict[str, Any]) -> Path:
    return config["state_dir"] / "spool"


_SPOOL_SEQUENCE = itertools.count()


def _spool(config: dict[str, Any], payload: dict[str, Any], observed_at: str) -> Path | None:
    folder = _spool_dir(config)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        kept = sorted(folder.glob("*.json"))
        dropped = kept[: max(0, len(kept) - SPOOL_LIMIT + 1)]
        for stale in dropped:
            stale.unlink(missing_ok=True)
        if dropped:
            _log(config, f"spool full: dropped the {len(dropped)} oldest")
        # The clock can read the same twice in a row, and a name taken twice replaced the hook kept under it.
        name = f"{time.time_ns():020d}-{os.getpid()}-{next(_SPOOL_SEQUENCE):06d}.json"
        pending = folder / f"{name}.tmp"
        pending.write_text(
            json.dumps({"payload": payload, "observed_at": observed_at}, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(pending, folder / name)
        return folder / name
    except OSError:
        return None


def flush_spool(config: dict[str, Any], seconds: float = FLUSH_SECONDS) -> int:
    """Send what earlier hooks could not, oldest first, within ``seconds``; stop at the first failure.

    One flusher at a time (a lock file beside the spool).  Returns how many were sent.
    """
    folder = _spool_dir(config)
    if not folder.is_dir():
        return 0
    lock = folder / "flush.lock"
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime > 2 * seconds:
            lock.unlink(missing_ok=True)
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except OSError:
        return 0
    sent = 0
    until = time.monotonic() + seconds
    try:
        for item in sorted(folder.glob("*.json")):
            left = until - time.monotonic()
            if left < 1.0:
                break
            try:
                kept = json.loads(item.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                _log(config, f"flush: dropped an unreadable spool file ({type(exc).__name__})")
                item.unlink(missing_ok=True)
                continue
            answer = _post(config, kept, min(left, 30.0))
            if answer is None or answer.get("retry"):
                break
            item.unlink(missing_ok=True)
            if not answer.get("refused"):
                sent += 1
    finally:
        lock.unlink(missing_ok=True)
    left = sum(1 for _item in folder.glob("*.json"))
    _log(config, f"flush: sent {sent}, {left} still kept")
    return sent


def _start_flush(config: dict[str, Any]) -> None:
    """Flush the spool in a process of its own, so the hook itself answers inside its short wait.

    On Windows with a console of its own that has no window, not detached: the interpreter is often a launcher
    that starts python.exe as its child, and a console program started by a process without a console gets a
    new console, which Windows Terminal shows as a window on the desktop.
    """
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                "-m",
                "scope_recall.adapters.codex.remote_client",
                "flush",
                "--config",
                str(config["config"]),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )
    except OSError:
        pass


# -- the hook ----------------------------------------------------------------


def _record_part(config: dict[str, Any], payload: dict[str, Any]) -> tuple[dict[str, Any], "transcript.Cursor"] | None:
    """What this Stop sends of the session record, and the cursor to move on success."""
    session_id = payload.get("session_id")
    if type(session_id) is not str or not session_id.strip():
        return None
    workbuddy = config["host"] == "workbuddy"
    record = (
        transcript.workbuddy_record_path(
            payload.get("transcript_path"), session_id.strip(), record_id=payload.get("agent_id")
        )
        if workbuddy
        else transcript.record_path(payload.get("transcript_path"), session_id.strip())
    )
    if record is None:
        return None
    cursor = transcript.Cursor(config["state_dir"], session_id.strip(), record)
    start = cursor.load()
    try:
        lines = transcript.read(
            record, start, limit=RECORD_READ_BYTES, rows=transcript.workbuddy_said if workbuddy else transcript.said
        )
    except OSError:
        return None
    if not lines:
        return None
    wire = [[end, transcript.said_to_wire(said)] for end, said in lines if said is not None]
    if not wire or wire[-1][0] != lines[-1][0]:
        wire.append([lines[-1][0], None])
    return {"start": start, "lines": wire}, cursor


def _error_reply(payload: dict[str, Any]) -> bool:
    """Whether a WorkBuddy Stop's reply is an error its record here marks (``transcript.workbuddy_error_reply``)."""
    reply, session_id = payload.get("last_assistant_message"), payload.get("session_id")
    if type(reply) is not str or type(session_id) is not str or not session_id.strip():
        return False
    try:
        record = transcript.workbuddy_record_path(
            payload.get("transcript_path"), session_id.strip(), record_id=payload.get("agent_id")
        )
    except OSError:
        return False
    return record is not None and transcript.workbuddy_error_reply(record, reply)


def run_hook(config: dict[str, Any], raw: bytes, *, started: float | None = None) -> dict[str, Any]:
    started = time.monotonic() if started is None else started
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        return {}
    if not isinstance(payload, dict):
        return {}
    # Half of a broken emoji (a lone surrogate, which JavaScript writes) cannot be sent as UTF-8: the request would fail
    # and the turn have no recall.  It is sent, kept and stored as U+FFFD, as a hook here stores it.
    payload = without_lone_surrogates(payload)
    event = payload.get("hook_event_name")
    host = config["host"]
    wait = HOOK_TIMEOUTS[host].get(event)
    if wait is None:
        return {}
    until = started + wait * _REQUEST_SHARE
    # Every request carries the moment the hook ran here.  A request that timed out here may still have been
    # stored there, and its replay must then be the same event, not a second one with a later time.
    observed_at = _now()
    body: dict[str, Any] = {"payload": payload, "observed_at": observed_at}
    cursor = None
    if (
        host in _RECORD_HOSTS
        and event in ("Stop", "SessionEnd")
        and not (host == "workbuddy" and is_workbuddy_agent_run(payload))
    ):
        part = _record_part(config, payload)
        if part is not None:
            body["record"], cursor = part
        if host == "workbuddy" and event == "Stop" and _error_reply(payload):
            # The record is here, not on the server: this side says whether the reply is an error WorkBuddy showed.
            body["error_reply"] = True
    # Kept before it is sent: Codex ends Interrupt and SessionEnd at 3 s, and with the interpreter's start and
    # a connection that does not open that is all of it, so a hook that waited for the server was killed before
    # it could keep anything.  An answer that stored it removes it again.
    spooled = _spool(config, payload, observed_at) if host == "codex" and event in _SPOOLED else None
    if _server_away(config):
        _log(config, f"{event}: not sent, the server was away less than {AWAY_SECONDS:.0f} s ago")
        answer = None
    else:
        answer = _post(config, body, until - time.monotonic())
    if answer is None:
        return {}
    if spooled is not None and not answer.get("retry"):
        spooled.unlink(missing_ok=True)
    if answer.get("refused"):
        return {}
    through = answer.get("through")
    if cursor is not None and type(through) is int and through > body["record"]["start"]:
        cursor.save(through)
    if host == "codex" and any(_spool_dir(config).glob("*.json")):
        _start_flush(config)
    result = answer.get("result")
    return result if isinstance(result, dict) else {}


# -- setup on this machine ---------------------------------------------------


def make_token(config: dict[str, Any]) -> str:
    """Create the entry's token here if there is none, readable by this user only; return its SHA-256."""
    path = config["token_file"]
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_name(path.name + ".tmp")
        pending.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        if os.name == "nt":
            user = os.environ.get("USERNAME", "")
            subprocess.run(
                ["icacls", str(pending), "/inheritance:r", "/grant:r", f"{user}:F", "*S-1-5-18:F"],
                check=True,
                capture_output=True,
            )
        else:
            pending.chmod(0o600)
        os.replace(pending, path)
    return hashlib.sha256(path.read_text(encoding="utf-8").strip().encode("utf-8")).hexdigest()


def _empty_answer(path: object) -> str:
    """What a hook whose config did not load writes: nothing to add, as the host its config still names takes it
    (``EMPTY_ANSWER``), else "{}"."""
    try:
        host = json.loads(Path(str(path)).read_text(encoding="utf-8")).get("host")
    except (OSError, ValueError, AttributeError):
        host = None
    answer = EMPTY_ANSWER.get(host, "{}") if type(host) is str else "{}"
    return answer + "\n" if answer else ""


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    args = list(sys.argv[1:] if argv is None else argv)
    command = args.pop(0) if args and args[0] in ("token", "flush") else "hook"
    parser = argparse.ArgumentParser(prog="scope-recall-remote-client")
    parser.add_argument("--config", required=True)
    parsed = parser.parse_args(args)
    try:
        config = load_client_config(absolute_path(parsed.config, "config"))
        if command == "token":
            print(json.dumps({"token_sha256": make_token(config)}))
            return 0
        if command == "flush":
            print(json.dumps({"sent": flush_spool(config)}))
            return 0
    except RemoteClientError as exc:
        if command == "hook":
            sys.stdout.write(_empty_answer(parsed.config))
            sys.stderr.write(f"SCOPE_RECALL_REMOTE:{exc}\n")
            return 0
        raise SystemExit(str(exc)) from None
    raw = sys.stdin.buffer.read(_MAX_STDIN + 1)
    result = run_hook(config, raw, started=started) if len(raw) <= _MAX_STDIN else {}
    # ASCII only, as the local hook writes it: a pipe on Windows carries the system code page (GBK on a Chinese
    # Windows, under -I whatever PYTHONUTF8 says), and the host reads UTF-8, so recalled Chinese text arrived
    # garbled or not at all.  Nothing to add is written as the local hook writes it for this client (EMPTY_ANSWER).
    if result or EMPTY_ANSWER[config["host"]]:
        sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
