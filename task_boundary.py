"""Task-boundary and closure classification helpers for Experience promotion.

This module is read-only and has no storage dependency. It centralizes the
question "did this task really finish, fail, or remain uncertain?" so later
journal, experience, and governance code do not each carry separate closure
rules.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

LOW_SIGNAL_GOALS = {
    "继续",
    "继续。",
    "进度如何",
    "进度如何了",
    "进度怎么样",
    "在吗",
    "新会话测试ok",
    "新会话测试OK",
}

SUCCESS_TOKENS = (
    "passed",
    "pass",
    "ok",
    "green",
    "完成",
    "通过",
    "已验证",
    "验证完成",
    "成功",
)

COMPLETION_TOKENS = (
    "完成",
    "通过",
    "已验证",
    "验证完成",
    "验证通过",
    "成功",
    "done",
    "fixed",
    "success",
    "ok=true",
    "ok: true",
    "passed",
    "green",
)

VERIFICATION_PATTERNS = (
    re.compile(r"\bpython(?:3)?\s+-m\s+pytest\b", re.IGNORECASE),
    re.compile(r"\bpytest\b", re.IGNORECASE),
    re.compile(r"\b\d+\s+passed\b", re.IGNORECASE),
    re.compile(r"\bruff\b", re.IGNORECASE),
    re.compile(r"\bpyright\b", re.IGNORECASE),
    re.compile(r"\bdoctor\b", re.IGNORECASE),
    re.compile(r"release gate", re.IGNORECASE),
    re.compile(r"验证(?:完成|通过)|测试通过"),
)

FAILURE_PATTERNS = (
    r"\bblocked\b",
    r"\bblocker\b",
    r"\bfailed\b",
    r"\bfailure\b",
    r"\bfailing\b",
    r"\bfails\b",
    r"\btraceback\b",
    r"\bexception\b",
    r"\berrors?\b(?!\s*(?:0|zero|none|found|detected|remaining))",
    r"\bnot\s+completed?\b",
    r"\bincomplete\b",
    r"\bstill\s+(?:failing|fails|blocked)\b",
    r"\btests?\s+failed\b",
    r"失败",
    r"未完成",
    r"没完成",
    r"没有完成",
    r"阻塞",
    r"报错",
    r"仍有问题",
    r"还有问题",
    r"仍然失败",
    r"还有失败",
    r"不能沉淀",
    r"不能发布",
    r"不可发布",
)
_FAILURE_RE = re.compile("|".join(f"(?:{pattern})" for pattern in FAILURE_PATTERNS), re.IGNORECASE)
NEGATED_FAILURE_PATTERNS = (
    r"未发现(?:任何)?(?:阻塞|失败|报错|错误)(?:[、或和及\s]+(?:阻塞|失败|报错|错误))*(?:问题|项)?",
    r"(?:没有|无)(?:任何)?(?:阻塞|失败|报错|错误)(?:[、或和及\s]+(?:阻塞|失败|报错|错误))*(?:问题|项)?",
    r"(?:0|零)\s*个?\s*(?:阻塞|失败|报错|错误)(?:[、或和及\s]+(?:阻塞|失败|报错|错误))*(?:问题|项)?",
    r"\b(?:0|zero)\s+errors?\b",
    r"\bfailed\s+checks?\s*:\s*\[\s*\]",
    r"\bno\s+(?:blocking\s+)?(?:failures?|errors?|blockers?)\s+(?:found|detected|remaining)\b",
    r"\bno\s+blocking\s+failures?\b",
)
_NEGATED_FAILURE_RE = re.compile("|".join(f"(?:{pattern})" for pattern in NEGATED_FAILURE_PATTERNS), re.IGNORECASE)


UNCERTAIN_PATTERNS = (
    r"\bstill\s+(?:reviewing|checking|validating|working|investigating|pending)\b",
    r"\bpending\b",
    r"\bbefore\s+closeout\b",
    r"\bremaining\s+(?:work|tasks?|issues?|risks?)\b",
    r"\bnot\s+(?:ready|deployed|released|published|pushed|merged|verified)\b",
    r"\bbut\s+not\s+(?:deployed|released|published|pushed|merged|verified)\b",
    r"仍在(?:检查|审查|验证|处理)",
    r"还(?:在|要|需|需要)",
    r"待(?:检查|验证|收口|发布|部署|提交|推送|合并)",
    r"未(?:发布|部署|提交|推送|合并|验证|收口)",
)
_UNCERTAIN_RE = re.compile("|".join(f"(?:{pattern})" for pattern in UNCERTAIN_PATTERNS), re.IGNORECASE)


@dataclass(frozen=True)
class TaskClosure:
    state: str
    reason: str
    final_evidence: tuple[str, ...] = ()


def _entry_value(entry: Any, key: str, default: Any = "") -> Any:
    try:
        return entry[key]  # type: ignore[index]
    except Exception:
        if isinstance(entry, Mapping):
            return entry.get(key, default)
        return getattr(entry, key, default)


def entry_text(entries: Sequence[Any]) -> str:
    return "\n".join(str(_entry_value(entry, "content", "") or "") for entry in entries)


def tail_text(entries: Sequence[Any], *, roles: set[str] | None = None, limit: int = 4) -> str:
    selected: list[str] = []
    allowed_roles = roles or {"assistant", "tool"}
    for entry in reversed(entries):
        if str(_entry_value(entry, "role", "") or "") not in allowed_roles:
            continue
        content = str(_entry_value(entry, "content", "") or "").strip()
        if content:
            selected.append(content)
        if len(selected) >= limit:
            break
    return "\n".join(reversed(selected))


def goal_signal_key(goal: str) -> str:
    return re.sub(r"[\s\W_]+", "", goal, flags=re.UNICODE).lower()


def is_low_signal_goal(goal: str) -> bool:
    stripped = str(goal or "").strip()
    key = goal_signal_key(stripped)
    low_signal_keys = {goal_signal_key(item) for item in LOW_SIGNAL_GOALS}
    if key in low_signal_keys:
        return True
    lowered = stripped.lower()
    if lowered.startswith("只回答") or lowered.startswith("只回复"):
        return True
    return False


def has_failure_signal(text: str) -> bool:
    normalized = _NEGATED_FAILURE_RE.sub("", text or "")
    return bool(_FAILURE_RE.search(normalized))


def has_uncertain_signal(text: str) -> bool:
    return bool(_UNCERTAIN_RE.search(text or ""))


def contains_any(text: str, tokens: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(token.lower() in lowered for token in tokens)


def extract_final_evidence(entries: Sequence[Any], *, limit: int = 6) -> tuple[str, ...]:
    evidence: list[str] = []
    for raw in tail_text(entries, roles={"assistant", "tool"}, limit=6).splitlines():
        line = raw.strip()
        if not line:
            continue
        if has_failure_signal(line):
            continue
        if any(pattern.search(line) for pattern in VERIFICATION_PATTERNS):
            evidence.append(line[:220])
        if len(evidence) >= limit:
            break
    return tuple(evidence)


def classify_task_closure(entries: Sequence[Any]) -> TaskClosure:
    """Classify final task state for safe experience promotion.

    Historical success tokens are not enough: final assistant/tool tail wins.
    """
    text = entry_text(entries)
    tail = tail_text(entries)
    assistant_tail = tail_text(entries, roles={"assistant"}, limit=3)
    final_evidence = extract_final_evidence(entries)
    if has_failure_signal(tail) or has_failure_signal(assistant_tail):
        return TaskClosure("failed", "final_failure_signal", final_evidence)
    if has_uncertain_signal(tail) or has_uncertain_signal(assistant_tail):
        return TaskClosure("uncertain", "final_uncertain_signal", final_evidence)
    if contains_any(assistant_tail, COMPLETION_TOKENS):
        return TaskClosure("success", "final_success_signal", final_evidence)
    last_role = str(_entry_value(entries[-1], "role", "") or "") if entries else ""
    if last_role == "tool" and contains_any(tail, COMPLETION_TOKENS):
        return TaskClosure("success", "final_tool_success_signal", final_evidence)
    if has_failure_signal(text):
        return TaskClosure("uncertain", "historical_failure_signal", final_evidence)
    if contains_any(text, SUCCESS_TOKENS):
        return TaskClosure("uncertain", "success_not_final", final_evidence)
    return TaskClosure("unknown", "no_completion_signal", final_evidence)
