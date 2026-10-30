"""Dataset-specific answer metrics and coverage-aware result summaries."""

from __future__ import annotations

import json
import re
import string
from collections import Counter

BINARY_METRICS = {"judge_accuracy", "substring_exact_match", "abstention"}


def normalized(text: str, *, remove_and=False) -> str:
    """Normalize case, ASCII punctuation, articles and whitespace for lexical scoring."""
    text = text.lower().translate(str.maketrans("", "", string.punctuation))
    articles = r"\b(a|an|the|and)\b" if remove_and else r"\b(a|an|the)\b"
    return " ".join(re.sub(articles, " ", text).split())


def _stem_tokens(text: str) -> list[str]:
    from nltk.stem import PorterStemmer

    stemmer = PorterStemmer()
    return [stemmer.stem(token) for token in normalized(text, remove_and=True).split()]


def _token_f1(prediction: str, gold: str) -> float:
    predicted, expected = _stem_tokens(prediction), _stem_tokens(gold)
    common = sum((Counter(predicted) & Counter(expected)).values())
    return 2 * common / (len(predicted) + len(expected)) if common else 0.0


def _fact_match(prediction: str, gold: str) -> bool:
    answers = json.loads(gold)
    if isinstance(answers, str):
        answers = [answers]
    answers = [item for group in answers for item in (group if isinstance(group, list) else [group])]
    # The answer prefix takes precedence over an earlier explanatory line.
    explicit = re.search(r"Answer:(.*)(?:\n|$)", prediction, flags=re.IGNORECASE)
    selected = explicit.group(1).strip() if explicit else prediction.split("\n", 1)[0].strip()
    selected = re.sub(r"^Answer:", "", selected, flags=re.IGNORECASE).strip()
    return any(normalized(str(answer)) in normalized(selected) for answer in answers)


def score_local(dataset: str, category: str, gold: str, prediction: str) -> dict:
    """Score local lexical/refusal metrics, explicitly reporting unavailable dependencies."""
    if dataset == "factconsolidation" and category in {"single-hop", "multi-hop"}:
        return {"status": "scored", "metric": "substring_exact_match", "score": float(_fact_match(prediction, gold))}
    if dataset != "locomo" or category not in {"single-hop", "multi-hop", "temporal", "open-domain", "adversarial"}:
        return {"status": "unsupported", "reason": "dataset_category_not_supported"}
    if category == "adversarial":
        refusal = any(term in prediction.lower() for term in ("no information available", "not mentioned"))
        return {"status": "scored", "metric": "abstention", "score": float(refusal)}
    try:
        if category == "multi-hop":
            targets = [part.strip() for part in gold.split(",")]
            choices = [part.strip() for part in prediction.split(",")]
            value = sum(max(_token_f1(choice, target) for choice in choices) for target in targets) / len(targets)
        else:
            target = gold.split(";", 1)[0].strip() if category == "open-domain" else gold
            value = _token_f1(prediction, target)
    except ImportError:
        return {"status": "unsupported", "metric": "stemmed_f1", "reason": "nltk_required"}
    return {"status": "scored", "metric": "stemmed_f1", "score": value}


def summarize(results: list[dict], incomplete: list[dict], *, fake=False, retrieval_only=False) -> dict:
    """Summarize only selected questions, retaining missing coverage and unsupported metrics."""
    groups = {}
    for row in results + incomplete:
        group = groups.setdefault(
            row["category"], {"selected": 0, "completed": 0, "scored": 0, "unsupported": 0, "scores": []}
        )
        group["selected"] += 1
    binary, scores = True, []
    for row in results:
        group = groups[row["category"]]
        group["completed"] += 1
        evaluation = row.get("evaluation", {"status": "not_run"})
        if evaluation["status"] == "scored":
            group["scored"] += 1
            group["scores"].append(evaluation["score"])
            scores.append(evaluation["score"])
            binary = binary and evaluation["metric"] in BINARY_METRICS
        elif evaluation["status"] == "unsupported":
            group["unsupported"] += 1
    for group in groups.values():
        values = group.pop("scores")
        group["mean_score"] = sum(values) / len(values) if values and not fake else None
    complete = not incomplete and len(scores) == len(results) and bool(results)
    status = (
        "incomplete"
        if incomplete
        else "retrieval_only"
        if retrieval_only
        else "smoke"
        if fake
        else "complete"
        if complete
        else "unsupported"
    )
    score = sum(scores) / len(scores) if complete and not fake and not retrieval_only else None
    return {"status": status, "score": score, "accuracy": score if binary else None, "categories": groups}
