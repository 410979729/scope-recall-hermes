"""Shared textual guards for source assertions, without persistence or model I/O."""
from __future__ import annotations

import re
import unicodedata


AUTHORITY_QUESTION = re.compile(r'[?？]|是否|能否|可否|要不要|是不是|吗(?:[。.!，,;；\s]|$)|\b(?:whether|is it|(?:can|could|would|should)\s+(?:I|we|you))\b',re.I)
CLAUSE_BREAK = re.compile(r'[，,;；。!?！？\n]|\.(?:\s|$)')
QUALIFIER = re.compile(r'不要|不能|不必|不得|不准|不允许|并非|不是|没有|未曾|尚未|还没|尚无|不|没|未|无|仅|只|唯独|除了?|暂时|暂不|限于|\b(?:not|never|no|without|don[’\']t|cannot|can[’\']t|only|unless|except|until|temporarily)\b',re.I)
POLARITY = re.compile(r'不要|不能|不必|不得|不准|不允许|并非|不是|没有|未曾|尚未|还没|尚无|不|没|未|无|\b(?:not|never|no|without|don[’\']t|cannot|can[’\']t)\b',re.I)
RELATIVE_SCOPE = re.compile(r'本次|这次|本轮|此轮|今天|今日|明天|今晚|暂时|临时|\b(?:this time|this session|this task|today|tonight|tomorrow|temporarily|for now)\b', re.I)
UNASSERTED_UNCERTAINTY = re.compile(
    r'也许|或许|可能|大概|猜测?|听说|据说|好像|似乎|传闻|未经确认|未经验证|'
    r'\b(?:may|might|maybe|perhaps|possibly|probably|guess(?:ed)?|heard\s+that|'
    r'reportedly|apparently|allegedly|rumou?red|unconfirmed|unverified)\b',
    re.I,
)
REPORTED_SPEECH = re.compile(
    r'他说|她说|他们说|同事说|朋友说|客户说|有人说|文档写|引用|原文|'
    r'\b(?:he|she|they|someone|customer|colleague|friend)\s+(?:said|says|wrote)|'
    r'\baccording\s+to\b|\bquoted\b',
    re.I,
)
_UNASSERTED_CONTEXT = re.compile(
    r'假设|假如|如果|除非|举例|示例|引用|原文|他说|她说|客户说|'
    r'也许|或许|可能|猜测?|听说|据说|好像|似乎|传闻|'
    r'\b(?:if|unless|suppose|hypothetical|for example|quoted|he said|she said|'
    r'customer said|may|might|maybe|perhaps|guess(?:ed)?|heard\s+that|reportedly)\b',
    re.I,
)
_REPORTED_SELF = re.compile(r'(?:他说|她说|他们说|客户说|同事说|朋友说|引用|原文)|\b(?:he|she|they|customer|colleague|friend)\s+(?:said|says|wrote)\b|[“"「『][^”"」』\n]{0,256}(?:\bI\b|\bmy\b|我)', re.I)
_SENTENCE_BREAK = re.compile(r'[;；。!?！？\n]')
_DURABLE_DIRECTIVE = re.compile(
    r'以后|今后|从现在起|长期|始终|一直|每次|每当|默认|平时|通常|惯例|'
    r'(?:当|在)[^。！？!?;；\n]{1,80}时|'
    r'\b(?:from now on|long[- ]term|always|every time|whenever|by default|usually|generally|'
    r'when\s+[^.;!?\n]{1,80})\b',
    re.I,
)
_DIRECT_REQUEST = re.compile(
    r'(?:请|帮我|麻烦|给我)[^。！？!?;；\n]{0,80}'
    r'(?:写|生成|创建|检查|分析|修改|整理|发送|打开|运行|执行|部署|翻译|总结|回答|做)|'
    r'\bplease\b[^.;!?\n]{0,100}\b(?:write|create|generate|check|analy[sz]e|edit|organize|'
    r'send|open|run|execute|deploy|translate|summari[sz]e|answer|make)\b',
    re.I,
)


def literal_spans(content, text):
    """Exact text with ASCII identifier boundaries; no inferred CJK identity."""
    if not text:
        return ()
    identifier = r"[A-Za-z0-9_.-]"
    left = rf"(?<!{identifier})" if re.match(identifier, text[0]) else ""
    # A terminal full stop closes a sentence; a dot followed by identifier
    # characters still binds a filename/domain/version (blue != blue.txt).
    right = r"(?![A-Za-z0-9_-]|\.(?=[A-Za-z0-9_.-]))" if re.match(identifier, text[-1]) else ""
    return tuple(re.finditer(left + re.escape(text) + right, content))


def bound_literal(content, text):
    return bool(literal_spans(content, text))


def self_report_bound(content, value_text, *, kind=None):
    """Bind singular self-report to the value's clause, never to a third party.

    This deliberately does not equate a team, a possessive third-party noun,
    or a quoted first-person sentence with the current user.
    """
    if not value_text or _REPORTED_SELF.search(content):
        return False
    for occurrence in literal_spans(content, value_text):
        before = list(CLAUSE_BREAK.finditer(content, 0, occurrence.start()))
        left = before[-1].end() if before else 0
        # Include the value: a faithful negative value may itself contain the
        # assertion verb ("我" + "不喜欢蓝色", "I" + "do not like blue").
        prefix = content[left:occurrence.end()]
        # A bare 我 is grammatical first person; 我的X requires the exact
        # preference/decision attribute, not an arbitrary person owned by 我.
        chinese = re.search(
            r'(?:^|[\s：:])(?:本次|这次|今天|平时|通常|目前|现在|本轮)*'
            r'我(?:本次|这次|今天|平时|通常|个人|一直|目前|现在|本轮|更|最|不|很|也|还)*'
            r'(?:在[^，,;；。!?！？\n]{1,80}?)?(?:喜欢|偏好|需要|想要|选择|决定|要求|使用|习惯|希望|认可|接受|禁止|不许|不准)'
            r'|(?:^|[\s：:])我的(?:偏好|喜好|决定|要求|约束|习惯)', prefix)
        english = re.search(r'\bI\s+(?:(?:do\s+not|don[’\']t)\s+)?(?:prefer\b|like\b|want\b|need\b|use\b|choose\b|decide\b)|\bmy\s+(?:preference|decision|requirement|constraint|habit)\b', prefix, re.I)
        if kind == 'fact':
            chinese = chinese or re.search(r'(?:^|[\s：:])我(?:是|住在|工作于)', prefix)
            english = english or re.search(r'\bI\s+(?:am|have|live|work)\b', prefix, re.I)
        if chinese or english:
            return True
    return False


def _condition_text(text):
    # NFC/whitespace changes preserve identifiers; no case folding or aliases.
    text = ' '.join(unicodedata.normalize('NFC', text).split()).strip()
    if text.startswith('当') and text.endswith('时') and len(text) > 3:
        return text[1:-1].strip()
    if text.endswith('时') and len(text) > 2 and not text[-2].isdigit():
        return text[:-1].strip()
    matched = re.fullmatch(r'when\s+(.+)', text, re.I)
    return matched.group(1).strip() if matched else text


def condition_supports_value(content, condition, value_text):
    """Require a proposed condition beside the value it is said to govern.

    Merely finding a condition in one sentence and a value in another does not
    establish their relationship. Commas remain inside the assertion so common
    forms such as ``写小说时，我偏好详细`` stay representable.
    """
    if any(type(value) is not str or not value.strip() for value in (content, condition, value_text)):
        return False
    normalized_content = '\n'.join(
        ' '.join(unicodedata.normalize('NFC', line).split())
        for line in content.splitlines()
    )
    normalized_condition = ' '.join(unicodedata.normalize('NFC', condition).split())
    normalized_value = ' '.join(unicodedata.normalize('NFC', value_text).split())
    candidates = tuple(dict.fromkeys((normalized_condition, _condition_text(normalized_condition))))
    for occurrence in literal_spans(normalized_content, normalized_value):
        before = list(_SENTENCE_BREAK.finditer(normalized_content, 0, occurrence.start()))
        left = before[-1].end() if before else 0
        after = _SENTENCE_BREAK.search(normalized_content, occurrence.end())
        right = after.start() if after else len(normalized_content)
        assertion = normalized_content[left:right]
        if any(candidate and bound_literal(assertion, candidate) for candidate in candidates):
            return True
    return False


def is_transient_request(content):
    """Recognize an execution request that lacks a durable applicability cue."""
    if type(content) is not str or not content.strip():
        return False
    return bool(_DIRECT_REQUEST.search(content) and not _DURABLE_DIRECTIVE.search(content))


def first_person_reference(content):
    """Return whether direct text refers to its verified speaker in first person."""
    if type(content) is not str or REPORTED_SPEECH.search(content):
        return False
    return re.search(r'(?:\b(?:I|me|my|mine|myself)\b|我|我的|本人)', content, re.I) is not None


def conditions_match(conditions, context_text):
    """Match explicit present conditions without inventing scope or identity.

    Only whitespace/NFC and the syntactic wrappers 当X时 / X时 / when X
    are normalized. Relative dates/tasks require provenance unavailable to
    this text-only helper and therefore never activate background context.
    """
    if not conditions:
        return True
    if not isinstance(conditions, (list, tuple)) or type(context_text) is not str:
        return False
    context = ' '.join(unicodedata.normalize('NFC', context_text).split())
    if _UNASSERTED_CONTEXT.search(context):
        return False
    for condition in conditions:
        if type(condition) is not str or not condition.strip() or RELATIVE_SCOPE.search(condition):
            return False
        text = _condition_text(condition)
        if not text or not preserves_qualifiers(context, text, polarity_only=True):
            return False
        # Asking for help after asserting a task does not make that task
        # hypothetical. Only a question in the condition's own clause blocks
        # applicability ("写小说吗"), not "写小说，能帮我润色吗".
        for occurrence in literal_spans(context, text):
            before = list(CLAUSE_BREAK.finditer(context, 0, occurrence.start()))
            left = before[-1].end() if before else 0
            after = CLAUSE_BREAK.search(context, occurrence.end())
            right = after.end() if after else len(context)
            if AUTHORITY_QUESTION.search(context[left:right]):
                return False
    return True


def preserves_qualifiers(content, text, *, conditions=(), polarity_only=False):
    """Reject a substring which drops its clause's polarity or restriction.

    The full negative clause remains valid. All matching occurrences must be
    unambiguous because the current resume contract has no character offsets.
    """
    if not text:
        return False
    occurrences = literal_spans(content, text)
    for occurrence in occurrences:
        at, end = occurrence.span()
        before=list(CLAUSE_BREAK.finditer(content,0,at))
        left=before[-1].end() if before else 0
        after=CLAUSE_BREAK.search(content,end)
        right=after.start() if after else len(content)
        # Explicit conditions retain their own negation (e.g. 未授权). They do
        # not justify dropping a separate prohibition from the asserted value.
        covered = [match.span() for condition in conditions for match in literal_spans(content, condition)
                   if left <= match.start() and match.end() <= right]
        markers = POLARITY if polarity_only else QUALIFIER
        if any((m.start()<at or m.end()>end) and not any(a <= m.start() and m.end() <= b for a,b in covered)
               for m in markers.finditer(content,left,right)):
            return False
    return bool(occurrences)


def asserted_marker(pattern, content):
    return not AUTHORITY_QUESTION.search(content) and any(
        preserves_qualifiers(content,match.group()) for match in pattern.finditer(content))
