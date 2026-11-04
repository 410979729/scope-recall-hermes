"""Compare paired language evaluations or optional language libraries without network access."""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]


def offline(event, args):
    """Refuse network connections before optional scanners can verify any value."""
    if event in {"socket.connect", "socket.getaddrinfo"}:
        raise RuntimeError("offline evaluation forbids network access")


def dependency_size(name):
    """Count installed bytes for the complete applicable dependency closure, once per file."""
    seen, paths, pending, packages = set(), set(), [name], {}
    while pending:
        current = pending.pop()
        distribution = metadata.distribution(current)
        canonical = distribution.metadata["Name"].lower().replace("_", "-")
        if canonical in seen:
            continue
        seen.add(canonical)
        packages[canonical] = distribution.version
        for entry in distribution.files or ():
            path = distribution.locate_file(entry)
            if path.is_file() and path.suffix != ".pyc":
                paths.add(path)
        for item in distribution.requires or ():
            requirement = Requirement(item)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)
    return dict(installed_bytes=sum(p.stat().st_size for p in paths), packages=packages)


def time_comparison():
    """Compare whole UTC day windows, including negative and range cases from the existing suite."""
    import jionlp as jio
    from dateparser.search import search_dates
    from scope_recall.core.recall_scope import query_time

    rows = []
    for case in json.loads((ROOT / "fixtures/language/time_cases.json").read_text(encoding="utf-8")):
        now = datetime.fromisoformat(case["now"].replace("Z", "+00:00"))
        baseline = query_time(case["query"], now=case["now"], zone=UTC)
        old = (
            None
            if baseline is None
            else [
                [a[:10], (datetime.fromisoformat(b.replace("Z", "+00:00")) - timedelta(days=1)).date().isoformat()]
                for a, b in baseline.windows
            ]
        )
        if case["language"] == "zh":
            result = jio.ner.extract_time(case["query"], time_base=now.replace(tzinfo=None), with_parsing=True)
            parsed = [
                [r["detail"]["time"][0][:10], r["detail"]["time"][1][:10]]
                for r in result
                if isinstance(r.get("detail", {}).get("time"), list)
            ]
        else:
            result = search_dates(
                case["query"],
                languages=["en"],
                settings={
                    "RELATIVE_BASE": now.replace(tzinfo=None),
                    "PREFER_DATES_FROM": "past",
                    "RETURN_TIME_AS_PERIOD": True,
                },
            )
            parsed = [[r[1].date().isoformat()] * 2 for r in result or ()]
        rows.append(
            dict(
                case=case,
                baseline=old,
                candidate=parsed or None,
                baseline_pass=old == case["windows"],
                candidate_pass=(parsed or None) == case["windows"],
            )
        )
    summary = {
        language: dict(
            total=sum(r["case"]["language"] == language for r in rows),
            baseline_pass=sum(r["baseline_pass"] for r in rows if r["case"]["language"] == language),
            candidate_pass=sum(r["candidate_pass"] for r in rows if r["case"]["language"] == language),
        )
        for language in ("zh", "en")
    }
    return dict(
        summary=summary, rows=rows, dependencies={name: dependency_size(name) for name in ("jionlp", "dateparser")}
    )


def credential_comparison():
    """Measure scanner recall and false positives with live verification disabled."""
    from detect_secrets.core.scan import scan_line
    from detect_secrets.settings import default_settings
    from scope_recall.core.secret_patterns import contains_secret_like_text

    rows = []
    with default_settings() as settings:
        settings.disable_filters("detect_secrets.filters.common.is_ignored_due_to_verification_policies")
        for case in json.loads((ROOT / "fixtures/language/credential_cases.json").read_text(encoding="utf-8")):
            rows.append(
                dict(
                    id=case["id"],
                    expected=case["secret"],
                    baseline=contains_secret_like_text(case["text"]),
                    candidate=bool(list(scan_line(case["text"]))),
                )
            )
    summary = {}
    for scanner in ("baseline", "candidate"):
        summary[scanner] = dict(
            positive=sum(r["expected"] for r in rows),
            negative=sum(not r["expected"] for r in rows),
            true_positive=sum(r["expected"] and r[scanner] for r in rows),
            false_positive=sum(not r["expected"] and r[scanner] for r in rows),
        )
    return dict(summary=summary, rows=rows, dependencies={"detect-secrets": dependency_size("detect-secrets")})


def main():
    """Write reproducible comparison evidence; this entry point always forbids network access."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--before", type=Path)
    parser.add_argument("--after", type=Path)
    parser.add_argument("--require-acceptance", action="store_true")
    args = parser.parse_args()
    sys.addaudithook(offline)
    if args.before or args.after:
        if not args.before or not args.after:
            parser.error("paired comparison requires both --before and --after")
        from acceptance import compare

        result = compare(
            json.loads(args.before.read_text(encoding="utf-8")), json.loads(args.after.read_text(encoding="utf-8"))
        )
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result))
        return int(args.require_acceptance and not result["accepted"])
    report = dict(time=time_comparison(), credentials=credential_comparison())
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value["summary"] for key, value in report.items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
