"""Human-only private-mode commands; all policy lives in the management API.

The command line is the operator interface.  No agent tool or host event may
open or close an interval: a model writing the same words is not the person's
command.  A rule stores metadata only, never a message, attachment or question.
"""

from __future__ import annotations

import argparse
import json

from scope_recall.contracts import ContractError, validate_payload
from scope_recall.core.private import MODES, parse_duration

from .memory import open_management, table
from .restore import RestoreError

FORMAT = "scope-recall-private/1"


def _common(parser):
    parser.add_argument("--root", help="shared store root")
    parser.add_argument("--host", choices=("hermes", "codex", "claude-code", "workbuddy", "dsh"))
    parser.add_argument("--instance-root", help="host installation home for a store of its own")
    parser.add_argument("--json", action="store_true", help="emit the versioned JSON contract")


def _target_arguments(parser, *, required: bool):
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--session", help="exact session id as stored in Scope Recall")
    group.add_argument("--project", help="absolute project directory; a rule covers it and its children")
    group.add_argument("--all", dest="all_scope", action="store_true", help="the whole store")


def parser():
    """Build the private-mode arguments without opening a store or calling a provider."""
    root = argparse.ArgumentParser(prog="scope-recall private", description=__doc__)
    commands = root.add_subparsers(dest="action", required=True)
    for action in ("on", "off", "status", "list"):
        command = commands.add_parser(action)
        _common(command)
        if action in {"on", "off"}:
            _target_arguments(command, required=True)
        if action == "status":
            _target_arguments(command, required=False)
        if action == "on":
            command.add_argument("--mode", choices=MODES, default="full")
            command.add_argument("--for", dest="duration", help="30m, 12h or 7d; default: until turned off")
        if action == "list":
            command.add_argument("--all", dest="include_closed", action="store_true", help="include closed intervals")
    return root


def _target(args):
    if getattr(args, "all_scope", False):
        return {"target": "global"}
    if getattr(args, "session", None) is not None:
        return {"target": "session", "session": args.session}
    if getattr(args, "project", None) is not None:
        return {"target": "project", "project": args.project}
    return {}


def _execute(api, args):
    if args.action == "on":
        target = _target(args)
        duration = parse_duration(args.duration) if args.duration else None
        rule = api.private_on(mode=args.mode, duration_seconds=duration, **target)
        scope = {key: value for key, value in target.items() if key != "target"}
        status = api.private_status(**scope)
        return {"rule": rule, "mode": status["mode"]}
    if args.action == "off":
        target = _target(args)
        result = api.private_off(**target)
        scope = {key: value for key, value in target.items() if key != "target"}
        status = api.private_status(**scope)
        return {"closed": result["closed"], "mode": status["mode"]}
    if args.action == "status":
        scope = {key: value for key, value in _target(args).items() if key != "target"}
        return api.private_status(**scope)
    return api.private_list(active_only=not args.include_closed)


def _label(rule) -> str:
    if rule["target"] == "session":
        return f"session {rule['session']}"
    if rule["target"] == "project":
        return f"project {rule['project']}"
    return "whole store"


def render(payload, *, as_json):
    """Render readable output by default, keeping JSON output independent of locale."""
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    if payload["status"] == "error":
        print(f"{payload['code']}: {payload.get('field', '')}")
        return
    data = payload["data"]
    if "rules" in data:
        rows = [
            dict(
                target=rule["target"],
                scope=rule["session"] or rule["project"] or "whole store",
                mode=rule["mode"],
                started=rule["started_at"],
                ends=rule["ends_at"] or "",
                active="yes" if rule["active"] else "no",
            )
            for rule in data["rules"]
        ]
        table(rows, ("target", "scope", "mode", "started", "ends", "active"))
        if "mode" in data:
            print(f"Effective mode: {data['mode']}")
    elif "rule" in data:
        rule = data["rule"]
        print(f"Private {data['mode']} mode in force for {_label(rule)} until {rule['ends_at'] or 'turned off'}.")
    else:
        print(f"Closed {data['closed']} interval(s). Effective mode: {data['mode']}")


def main(argv=None):
    """Run one local owner command; refused inputs or a missing store exit with code 2."""
    args = parser().parse_args(argv)
    try:
        data = _execute(open_management(args), args)
        payload = dict(format=FORMAT, action=args.action, status="ok", data=data)
        validate_payload("private_mode", payload)
        code = 0
    except ContractError as exc:
        payload = dict(format=FORMAT, action=args.action, status="error", code=exc.code, field=exc.field)
        validate_payload("private_mode", payload)
        code = 2
    except (RestoreError, OSError, ValueError) as exc:
        payload = dict(
            format=FORMAT,
            action=args.action,
            status="error",
            code=getattr(exc, "code", "CONFIG_INVALID"),
            field=getattr(exc, "hint", "store"),
        )
        code = 2
    render(payload, as_json=args.json)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
