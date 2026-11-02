"""Fixed synthetic salience and extractive-summary evaluation through the approved chat port."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from scope_recall.core.consolidation_summary import grounded_sentences
from scope_recall.core.language_resources import prompt_text
from scope_recall.core.worker_summaries import summary_messages
from subject import ChatPort, package_identity


def evaluate_scores(port, cases, *, summaries_only):
    """Run the fixed optional salience probes without adding calls to summary-only evaluation."""

    def score(pair):
        index, repeat = pair
        case = cases[index]
        raw = port.propose(
            [
                dict(
                    role="system",
                    content=prompt_text("importance")
                    + '\n对已给出的内容评估重要度，只返回 {"importance":整数}。这不是事实资格判断。',
                ),
                dict(role="user", content=case["text"]),
            ],
            remaining_seconds=120,
        )
        try:
            result = json.loads(raw)["importance"]
        except (ValueError, KeyError, TypeError):
            result = None
        return dict(id=case["id"], repeat=repeat, category=case["category"], gold=case["score"], score=result, raw=raw)

    if summaries_only:
        return []
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(score, ((index, repeat) for index in range(len(cases)) for repeat in range(3))))


def main():
    """Record unmodified replies, corpus hashes, score agreement, evidence coverage and server token meters."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--approved-endpoint", required=True)
    parser.add_argument("--summaries-only", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must be a new task-owned directory")
    args.output.mkdir(parents=True)
    corpus = Path(__file__).parents[2] / "fixtures/language/importance_cases.json"
    cases = json.loads(corpus.read_text(encoding="utf-8"))
    port = ChatPort(json.loads(args.config.read_text(encoding="utf-8")), {args.approved_endpoint})
    score_route = replace(port.route, max_output_tokens=128)
    summary_route = port.route
    port.route = score_route

    scores = evaluate_scores(port, cases, summaries_only=args.summaries_only)
    score_usage = dict(port.usage)
    port.route = summary_route
    pages = []
    for session in range(12):
        sources = []
        for turn in range(8 if session < 8 else 9):
            for role in ("user", "assistant"):
                text = (
                    f"项目{session}第{turn}项决定使用蓝色，尚未发布。"
                    if role == "user"
                    else f"项目{session}第{turn}项已记录；检查尚未运行。"
                )
                sources.append(
                    SimpleNamespace(
                        ref=f"event-TEST-{session}-{turn}-{role}", revision=1, event=dict(content=text, role=role)
                    )
                )
        pages.extend((session, tuple(sources[start : start + 16])) for start in range(0, len(sources), 16))

    def summarize(page):
        session, sources = page
        raw = port.propose(summary_messages(sources), remaining_seconds=120)
        stats = {"expanded": 0, "rejected": 0}
        accepted_sentences = 0
        try:
            value = json.loads(raw)
            sentences, _spans = grounded_sentences(value, sources, stats=stats)
            accepted_sentences = len(sentences)
            grounded = len(sentences)
            total = len(sentences)
            accepted = True
        except (ValueError, KeyError, TypeError, RuntimeError):
            value = {}
            sentences = []
            grounded, total, accepted = 0, 0, False
        # Evidence precision is measured on every returned sentence, even when the page is refused.
        if isinstance(value, dict) and isinstance(value.get("sentences"), list):
            total = len(value["sentences"])
            source_map = {(s.ref, s.revision): s.event["content"] for s in sources}
            grounded = sum(
                isinstance(s, dict)
                and isinstance(s.get("text"), str)
                and bool(s["text"].strip())
                and s["text"] in source_map.get((s.get("source_ref"), s.get("source_revision")), "")
                for s in value["sentences"]
            )
        return dict(
            session=session,
            input_sources=len(sources),
            sentences=total,
            grounded=grounded,
            accepted=accepted,
            accepted_sentences=accepted_sentences,
            expanded=stats["expanded"],
            rejected=stats["rejected"],
            raw=raw,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        summaries = list(pool.map(summarize, pages))
    port.close()
    per_score = {}
    for score_value in range(1, 6):
        rows = [s for s in scores if s["gold"] == score_value]
        ids = {s["id"] for s in rows}
        per_score[str(score_value)] = dict(
            samples=len(ids),
            ratings=len(rows),
            exact=sum(s["score"] == s["gold"] for s in rows),
            unanimous=sum(len({s["score"] for s in rows if s["id"] == cid}) == 1 for cid in ids),
        )
    result = dict(
        corpus_sha256=hashlib.sha256(corpus.read_bytes()).hexdigest(),
        per_score=per_score,
        scores=scores,
        summaries=summaries,
        score_usage=score_usage,
        summary_usage={key: port.usage[key] - score_usage[key] for key in port.usage},
        summary_calls_per_100_turns=len(pages),
        summary_dialogue_turns=100,
        summary_sentence_precision=dict(
            grounded=sum(s["grounded"] for s in summaries), total=sum(s["sentences"] for s in summaries)
        ),
        all_summary_pages_accepted=all(s["accepted"] for s in summaries),
        excerpt_expanded=sum(s["expanded"] for s in summaries),
        excerpt_rejected=sum(s["rejected"] for s in summaries),
        accepted_sentences=sum(s["accepted_sentences"] for s in summaries),
        score_histogram=dict(Counter(str(s["score"]) for s in scores)),
    )
    result.update(
        subject=package_identity(),
        model_config_sha256=hashlib.sha256(args.config.read_bytes()).hexdigest(),
        evaluator_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        prompt_sha256={
            name: hashlib.sha256(prompt_text(name).encode()).hexdigest() for name in ("importance", "session_summary")
        },
        protocol=dict(
            concurrency=4,
            score_repeats=0 if args.summaries_only else 3,
            summary_sessions=12,
            summary_turns=100,
            input_sources_per_page=16,
            grounding="exact_quote_and_full_clause_qualifiers",
        ),
    )
    (args.output / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {key: value for key, value in result.items() if key not in {"scores", "summaries"}}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
