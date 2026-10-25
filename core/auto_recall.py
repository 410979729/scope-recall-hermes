"""Conservative automatic-recall eligibility, shared by every host.

Only whole conversational messages are skipped. A message with a named topic,
an owner question or a request about the past still reaches retrieval.
"""

from __future__ import annotations

import re
import sqlite3
import time
import unicodedata
from typing import Protocol

from ..contracts import ContractError, TrustedContext

_SOCIAL = re.compile(
    r"(?:ok(?:ay)?|yes|yeah|yep|sure|alright|understood|got it|sounds good|"
    r"thanks?(?: you)?|thank you|well done|great(?: job)?|good (?:job|work)|excellent|perfect|approved|"
    r"好的?|行|可以|同意|没问题|嗯+|收到|明白了?|知道了|谢谢(?:你)?|辛苦了|太棒了|你?真棒|干得好|做得(?:很)?好|不错)",
    re.I,
)
_FOLLOW_THROUGH = re.compile(
    r"(?:please\s+)?(?:continue|resume|go on|go ahead|keep going|carry on|proceed|"
    r"(?:let['’]s )?do (?:that|it)|(?:do|proceed with) (?:what|as) you (?:recommend|suggest)(?:ed)?)"
    r"(?:\s+please)?|(?:请)?(?:继续|接着|继续执行|继续做|接着做)(?:吧|呀|啊)?|"
    r"(?:就)?按(?:照)?你(?:推荐|建议|说|刚才说)的(?:办|做|执行|设置|来|改|弄)(?:吧)?",
    re.I,
)
_SESSION_STATUS = re.compile(
    r"(?:(?:is|are) (?:it|that|this|you|the (?:agent|task|job|run|test|build)) "
    r"(?:done|finished|running|ready|working|started)(?: yet)?|"
    r"(?:did|have) you (?:finish|finished|start|started|delete|deleted)(?: it| that)?|"
    r"(?:any progress|how is it going|what(?:'s| is) the status))|"
    r"(?:(?:你|任务|测试|构建|进程|代理|codex|claude(?: code)?|workbuddy|dsh|hermes)"
    r"(?:已经|在|开始|还在)?(?:开始)?(?:跑|运行|执行|启动|完成|做完|结束|好了)(?:了|了吗|了没|没有|吗|没)?|"
    r"(?:删(?:除|掉)?|完成|做完|结束|启动|开始|跑起来|好了)(?:了吗|了没|没有|没)|"
    r"(?:现在)?(?:进度|进展)(?:怎么样|如何|呢)|现在怎么样了)",
    re.I,
)
_CLOSING = " \t\r\n。.!！?？,，;；~～"
_CONVERSATIONAL = re.compile(
    rf"(?:(?P<acknowledgment>{_SOCIAL.pattern})|(?P<session_continuation>{_FOLLOW_THROUGH.pattern})|"
    rf"(?P<session_status>{_SESSION_STATUS.pattern}))(?=[{re.escape(_CLOSING)}]|$)",
    re.I,
)
#: A busy scope may hide an older turn beyond this window. That uncertainty
#: permits recall, rather than scanning the entire scope to suppress it.  On a
#: shared store with several agents writing, a session's previous prompt lay
#: within the newest 512 rows of its scope for 92-99% of prompts per entry
#: (128 rows: 77-97%).
SESSION_PROBE_WINDOW = 512


class _Clock(Protocol):
    """The recall request's monotonic clock."""

    def monotonic(self) -> float: ...


def _conversation_reason(text: str) -> str | None:
    """Recognize only complete sequences of social, continuation and status intents."""
    tail = text.strip(_CLOSING)
    reasons = set()
    while tail:
        match = _CONVERSATIONAL.match(tail)
        if match is None:
            return None
        reasons.add(match.lastgroup)
        tail = tail[match.end() :].lstrip(_CLOSING)
    return next(
        (reason for reason in ("session_status", "session_continuation") if reason in reasons), "acknowledgment"
    )


def _prior_prompt(tx, context: TrustedContext, current_source_refs, *, deadline, clock) -> bool:
    """Probe bounded scope windows through the time index, within the request's deadline."""
    conn = tx._check()

    def expired():
        return deadline is not None and clock.monotonic() >= deadline

    if expired():
        raise ContractError("DEADLINE_EXCEEDED")
    interruptible = deadline is not None and callable(getattr(conn, "set_progress_handler", None))
    if interruptible:
        conn.set_progress_handler(lambda: int(expired()), 1000)
    excluded = ""
    if current_source_refs:
        excluded = f" AND event_id || '@' || source_revision NOT IN ({','.join('?' for _ in current_source_refs)})"
    try:
        for scope in sorted(context.allowed_scope_ids):
            # LIMIT belongs inside the indexed scope scan. Filtering the session
            # before it would still scan every row when the session is fresh.
            prior = conn.execute(
                f"""SELECT 1 FROM (
                SELECT event_id,source_revision,session_id,entry_id,role,source_original_origin,origin,
                       project_id,branch_id,read_blocked
                FROM source_events INDEXED BY source_scope_time WHERE scope_id=?
                ORDER BY occurred_at DESC,event_id DESC,source_revision DESC LIMIT ?)
                WHERE session_id=? AND entry_id=? AND role='user'
                  AND COALESCE(source_original_origin,origin)='human_direct'
                  AND project_id IS ? AND branch_id IS ? AND read_blocked=0{excluded} LIMIT 1""",
                (
                    scope,
                    SESSION_PROBE_WINDOW,
                    context.session_id,
                    context.entry_id or "local",
                    context.project_id,
                    context.branch_id,
                    *current_source_refs,
                ),
            ).fetchone()
            if expired():
                raise ContractError("DEADLINE_EXCEEDED")
            if prior is not None:
                return True
    except sqlite3.OperationalError as exc:
        if interruptible and getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
            raise ContractError("DEADLINE_EXCEEDED") from exc
        raise
    finally:
        if interruptible:
            conn.set_progress_handler(None, 0)
    return False


def automatic_recall_skip_reason(
    query: str | None,
    *,
    tx=None,
    context: TrustedContext | None = None,
    current_source_refs: tuple[str, ...] = (),
    deadline: float | None = None,
    clock: _Clock = time,
) -> str | None:
    """Return a diagnostic reason to skip, or None to recall.

    Blank and slash input need no store read. Conversational messages are
    skipped only with evidence of an earlier human prompt in this entry's session.
    Hosts capture the current prompt before recalling, so its source segments
    are excluded from that check. No process-local turn counter is used.
    """
    text = unicodedata.normalize("NFKC", query or "").strip()
    if not text:
        return "blank_prompt"
    if text.startswith("/"):
        return "slash_command"
    reason = _conversation_reason(text)
    if reason is None:
        return None
    if tx is None or context is None or not context.allowed_scope_ids:
        return None
    return reason if _prior_prompt(tx, context, current_source_refs, deadline=deadline, clock=clock) else None
