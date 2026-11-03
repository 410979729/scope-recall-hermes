"""Small child-side probes that import the explicitly selected installed product."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))

from real_host_io import write_json  # noqa: E402


def hook(args):
    """Tee a real hook's exact output; observations never add model context."""
    evidence = args.evidence / (uuid4().hex + ".json")
    row = dict(event=None, session=None, phase="started", output="", stderr="", exit_code=None)
    write_json(evidence, row)
    # Isolated Python can use the Windows locale for stdin. Preserve bytes:
    # locale decoding can introduce surrogates that cannot be encoded as UTF-8.
    body = sys.stdin.buffer.read()
    payload = json.loads(body)
    row.update(event=payload.get("hook_event_name"), session=payload.get("session_id"), phase="received")
    write_json(evidence, row)
    command = [
        args.python,
        "-I",
        "-B",
        "-m",
        "scope_recall.adapters.clients.hook_entry",
        "--home",
        str(args.home),
        "--host",
        args.host,
    ]
    try:
        done = subprocess.run(command, input=body, capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        row.update(phase="failed", exit_code=1, stderr=type(exc).__name__)
        write_json(evidence, row)
        sys.stdout.buffer.write(b"{}")
        return 1
    row.update(
        phase="completed",
        output=done.stdout.decode("utf-8", errors="replace"),
        stderr=done.stderr.decode("utf-8", errors="replace"),
        exit_code=done.returncode,
    )
    write_json(evidence, row)
    sys.stdout.buffer.write(done.stdout)
    sys.stderr.buffer.write(done.stderr)
    return done.returncode


def fake_host(args):
    """Exercise hooks in a separate deterministic host process, without account access."""
    from scenario_store import host_text

    definition = json.loads(args.definition.read_text(encoding="utf-8"))
    session = args.session or str(uuid4())
    context = []
    for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
        payload = dict(
            hook_event_name=event,
            session_id=session,
            cwd=str(args.project),
            prompt=args.prompt,
            turn_id=str(uuid4()),
            prompt_id=str(uuid4()),
            generation_id=str(uuid4()),
            last_assistant_message="TEST acknowledged.",
        )
        done = subprocess.run(
            definition["hook_argv"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        if done.returncode:
            return done.returncode
        if done.stdout.strip():
            context.append(host_text(json.loads(done.stdout)))
    # A fake answer can only echo what a hook delivered, never a test's expected value.
    print(json.dumps(dict(result="\n".join(context) or "TEST acknowledged.", session_id=session)))
    return 0


def matrix(args):
    """Verify all release snapshots in one process and retain every independent result."""
    from sample_store import verify

    samples = release_samples(args.samples)
    if len(samples) != 58:
        raise ValueError(f"expected 58 published-release samples, found {len(samples)}")
    rows = []
    for path in samples:
        result = verify(path.parent, output=args.output / path.parent.name)
        rows.append(dict(sample=str(path.parent), **result))
        write_json(args.output / "matrix.json", rows)
    return 0 if all(row["passed"] for row in rows) else 1


def release_samples(root):
    """Exclude the two extra backend fixtures from the published-release matrix."""
    from packaging.version import InvalidVersion, Version

    result = []
    for path in root.glob("*/sample.json"):
        try:
            version = Version(path.parent.name.removesuffix("-pypi"))
        except InvalidVersion:
            continue
        if version.major == 3 and not version.is_prerelease:
            result.append(path)
    return sorted(result)


def raw_check(args):
    """Read raw capture through the installed hook, then inspect active derived truth."""
    from contextlib import closing

    from scope_recall.adapters.clients import CodexHookHandler
    from scope_recall.adapters.clients.config import load_shared_client

    config = load_shared_client(args.home, args.host)
    fact = "TEST-install 配色是琥珀色。"
    with closing(CodexHookHandler.from_home(str(args.home), args.host)) as handler:
        handler.handle_payload(
            dict(
                hook_event_name="UserPromptSubmit",
                session_id=str(uuid4()),
                prompt=fact,
                cwd=str(args.home.parent),
                turn_id=str(uuid4()),
                prompt_id=str(uuid4()),
                generation_id=str(uuid4()),
            )
        )
    with closing(CodexHookHandler.from_home(str(args.home), args.host)) as handler:
        reply = handler.handle_payload(
            dict(
                hook_event_name="UserPromptSubmit",
                session_id=str(uuid4()),
                prompt="TEST-install 配色是什么？",
                cwd=str(args.home.parent),
                turn_id=str(uuid4()),
                prompt_id=str(uuid4()),
                generation_id=str(uuid4()),
            )
        )
    from scenario_store import host_text

    write_json(
        args.output,
        dict(raw_available="琥珀色" in host_text(reply), response=reply, store=str(config.to_binding().data_directory)),
    )
    return 0 if "琥珀色" in host_text(reply) else 1


def main():
    """Dispatch a probe inside the chosen interpreter without importing the source tree."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("hook", "fake-host", "matrix", "raw"))
    parser.add_argument("--python")
    parser.add_argument("--host", choices=("codex", "claude-code"))
    for name in ("home", "evidence", "definition", "project", "samples", "output"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--session")
    parser.add_argument("--prompt")
    args = parser.parse_args()
    return {"hook": hook, "fake-host": fake_host, "matrix": matrix, "raw": raw_check}[args.action](args)


if __name__ == "__main__":
    raise SystemExit(main())
