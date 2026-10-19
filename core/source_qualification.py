"""Shared textual guards for source assertions, without persistence or model I/O."""

from __future__ import annotations

import re
import unicodedata

AUTHORITY_QUESTION = re.compile(
    r"[?？]|是否|能否|可否|要不要|是不是|吗(?:[。.!，,;；\s]|$)|\b(?:whether|is it|(?:can|could|would|should)\s+(?:I|we|you))\b",
    re.I,
)
CLAUSE_BREAK = re.compile(r"[，,;；。!?！？\n]|\.(?:\s|$)")
QUALIFIER = re.compile(
    r"不要|不能|不必|不得|不准|不允许|并非|不是|没有|未曾|尚未|还没|尚无|不|没|未|无|仅|只|唯独|除了?|暂时|暂不|限于|\b(?:not|never|no|without|don[’\']t|cannot|can[’\']t|only|unless|except|until|temporarily)\b",
    re.I,
)
POLARITY = re.compile(
    r"不要|不能|不必|不得|不准|不允许|并非|不是|没有|未曾|尚未|还没|尚无|不|没|未|无|\b(?:not|never|no|without|don[’\']t|cannot|can[’\']t)\b",
    re.I,
)
RELATIVE_SCOPE = re.compile(
    r"本次|这次|本轮|此轮|今天|今日|明天|今晚|暂时|临时|\b(?:this time|this session|this task|today|tonight|tomorrow|temporarily|for now)\b",
    re.I,
)
UNASSERTED_UNCERTAINTY = re.compile(
    r"也许|或许|可能|大概|猜测?|听说|据说|好像|似乎|传闻|未经确认|未经验证|"
    r"\b(?:may|might|maybe|perhaps|possibly|probably|guess(?:ed)?|heard\s+that|"
    r"reportedly|apparently|allegedly|rumou?red|unconfirmed|unverified)\b",
    re.I,
)
#: A condition or a supposition that what follows depends on ("要是值班表调整，就会改成 X", "if the rotation changes it
#: would be X"): it says what would hold, not what holds.  "would like", "would prefer" and "would rather" ask for
#: something instead.
CONDITIONAL = re.compile(
    r"如果|假如|假设|假使|假定|设想|要是|若是|倘若|万一|一旦|除非|的话|"
    r"\b(?:if|unless|suppose|supposing|imagine|in\s+case|provided\s+that|as\s+long\s+as|hypothetically"
    r"|would(?!\s+(?:like|love|prefer|rather)\b))\b",
    re.I,
)
REPORTED_SPEECH = re.compile(
    r"他说|她说|他们说|同事说|朋友说|客户说|有人说|文档写|引用|原文|"
    r"\b(?:he|she|they|someone|customer|colleague|friend)\s+(?:said|says|wrote)|"
    r"\baccording\s+to\b|\bquoted\b",
    re.I,
)
#: What makes a text say what might hold rather than what holds: a condition or a supposition, an example, a quotation
#: or someone else's words, a guess or hearsay.
UNASSERTED_CONTEXT = re.compile(
    r"假设|假如|如果|除非|举例|示例|引用|原文|他说|她说|客户说|"
    r"也许|或许|可能|猜测?|听说|据说|好像|似乎|传闻|"
    r"\b(?:if|unless|suppose|hypothetical|for example|quoted|he said|she said|"
    r"customer said|may|might|maybe|perhaps|guess(?:ed)?|heard\s+that|reportedly)\b",
    re.I,
)
#: What a statement says holds or changes: a copula, 用, a change (改成, 换成, "switch to"), what should be.
_STATEMENT = r"(?:是|为|用|改|换|调整|应该|应当|要|\b(?:is|are|was|were|uses?|should|will|must|change|switch)\b)"
#: A named or unnamed speaker other than the one writing, before what they said: ``张三说：``, ``张三说，``, a name
#: opening a clause before 说 with nothing between (``张三说配色换成了银色``), or before 表示, 称, 提到, 认为, 觉得,
#: 指出 or 声称 and a statement (``张三表示配色换成银色``; ``红色表示错误`` states nothing), ``NameOne said``, ``the
#: wiki says``, ``Everyone thinks``.  ``我说：``, ``我表示`` and ``I said`` are the writer's own; 说 in a noun (小说,
#: 传说, 学说) says nothing, nor does it in 说错 (misspoke), 说好 (agreed), 说明 (explain) or 说得 (put it), nor 称 in
#: a name (名称, 简称, 称为); and a verb that opens an identifier (``claim-4daf…``, a claim's ref) is no verb.
_SAID_BY_ANOTHER = re.compile(
    r"(?<![我小传学演游劝解])(?:说|讲|表示|写道)\s*[：:，,“\"「『'‘]"
    r"|(?:^|(?<=[\n，,。；;！!？?、\s]))(?!我)[^\s，,。；;！!？?、：:]{1,8}?(?:(?<![小传学演游劝解])说(?![错好明得法服])"
    r"|(?:表示|(?<![名简昵俗统通职堪相对匀])称(?![为作呼号赞心职重])|提到(?!的)|认为|觉得|指出|声称)"
    rf"(?=[^。！？；;!?\n]*?{_STATEMENT}))"
    r"|\b(?!I\b)\w+\s+(?:(?:said|says|wrote|thinks?|thought|believes?|believed|claims?|claimed|assumes?|assumed"
    r"|guess(?:es|ed)?|heard)\b(?!-)|(?:told|tells)\s+\w+\b)",
    re.I,
)
#: The common surnames a Chinese name begins with, less those that also begin everyday keys (方案, 高度, 任务, 金额,
#: 周一).
_SURNAME = (
    "[王李张刘陈杨黄赵吴徐孙朱胡郭林罗郑梁谢宋唐韩冯邓曹彭肖董袁潘蒋蔡杜苏魏吕"
    "丁沈姚卢姜崔谭汪廖贾韦邹孟熊秦邱尹薛闫雷侯陶贺顾郝龚邵覃武戴莫孔汤欧司]"
)
#: Surnames of two characters (欧阳明明, 司马懿, 上官婉儿).
_COMPOUND_SURNAME = "(?:欧阳|司马|上官|诸葛|东方|皇甫|慕容|司徒|公孙|夏侯|令狐|尉迟|长孙|宇文)"
#: A speaker's name as a line's label: a surname and one or two characters after it (张三, 李经理, 欧阳明明), 小 or 老
#: before one (小王, 老张), 阿 and a character (阿乙), a role (同事, 客户, 老板), a handle (@nameone), or a capitalised
#: Latin word that is no heading, log level or key (NameOne; not Note, Update, Remember, Status, Value); and a role or a
#: time after it in brackets (NameOne (PM), 张三（产品）).  An instruction or a key before its value (记住：, 配色：), a
#: log's level (INFO:, ERROR:) and a key in lower case (status:, value:) are no name.
_SPEAKER_NAME = (
    rf"(?:{_COMPOUND_SURNAME}[\u3400-\u9fff]{{1,2}}|[小老]{_SURNAME}|{_SURNAME}[\u3400-\u9fff]{{1,2}}|阿[\u3400-\u9fff]"
    r"|同事|客户|老板|领导|经理|主管|组长|老师|同学|朋友|家人|妈妈|爸爸|儿子|女儿|老婆|老公|对方|甲方|乙方|助手"
    r"|@[\w.-]{1,24}"
    r"|(?!(?:Note|Notes|Nb|Update|Updates|Correction|Edit|Summary|Fyi|Reminder|Remember|Important|Warning|Warn"
    r"|Error|Info|Debug|Notice|Tip|Hint|Btw|Ps|Todo|Answer|Question|Result|Results|Conclusion|Decision|Context"
    r"|Background|Status|State|Value|Values|Example|Reason|Cause|Output|Input|Source|Subject|Log|Name|Type|Mode"
    r"|Level|Version|Owner|Problem|Issue|Fix|Plan|Goal|Task|Step|Steps|Next|Done)\b)[A-Z][a-z][A-Za-z0-9_.-]{0,15})"
    r"(?:[^\S\n]*[(（][^()（）\n]{1,24}[)）])?"
)
#: What a line says when it holds a sentence rather than a key's value: a statement (``_STATEMENT``), a negation, a
#: person or a liking (张三：我不吃辣, NameOne: I prefer blue).
_SAYS = (
    rf"(?i:{_STATEMENT}|不|没|别|我|你|他|她|喜欢|偏好|讨厌"
    r"|\b(?:i|my|me|we|our|you|he|she|they|not|never|no|prefer|like|love|hate|want|need)\b|n['’]t\b)"
)
#: A line that opens with another speaker's name (``_SPEAKER_NAME``) and a colon before a sentence (``_SAYS``), on it or
#: on the next line (``张三：TEST-project 配色换成银色``, ``NameOne (PM): the CI is GitLab``,
#: ``NameOne: I prefer blue``),
#: after a list marker, a ``[10:32]`` or in Markdown emphasis.  The writer's own ``我：``, a heading or a correction of
#: theirs (``更正：``, ``Update:``, ``刚才写错了：``), a head that is a phrase (``库里只有这一句：``), a log's line
#: (``INFO: …``) and a key before its value (``status: …``, ``Region: us-east-1``) give no one else's words.
_SPEAKER_LINE = re.compile(
    r"(?:^|(?<=\n))[^\S\n]*(?:(?:[-*+•]|\d+[.)、])[^\S\n]+)?(?:\[[^\]\n]{0,24}\][^\S\n]*)?(?:\*\*|__)?"
    rf"{_SPEAKER_NAME}(?:\*\*|__)?[：:](?!//)(?=[^\n]*?{_SAYS}|[^\S\n]*\n[^\n]*?{_SAYS})"
)
#: Someone else's words, or an example, around a first person: a speaker other than the writer
#: (``_SAID_BY_ANOTHER``, and ``NameOne writes``, ``NameOne hears``), a quoted first person in double, CJK or single
#: quotes (an apostrophe inside a word, ``I'm``, opens no quote), and an example (``比如``, ``for example``); and a
#: first person after a speaker's label (``_after_a_label``).  The speaker's own framing (``_OWN_FRAMING``) is no one
#: else's words.
_REPORTED_SELF = re.compile(
    r"(?:他说|她说|他们说|客户说|同事说|朋友说|引用|原文|比如|例如|举例|譬如)|"
    + _SAID_BY_ANOTHER.pattern
    + r"|\b(?!I\b)\w+\s+(?:writes|hears?)\b"
    r"|\b(?:for example|for instance|e\.g\.|imagine)\b"
    r"|[“\"「『][^”\"」』\n]{0,256}(?:\bI\b|\bmy\b|我)"
    r"|(?:^|[\s:：,，])['‘][^'’\n]{0,256}(?:\bI\b|\bmy\b|我)",
    re.I,
)
#: A line opening with a speaker's label: a short head ending in a colon (``儿子：``, ``NameOne:``,
#: ``NameOne (10:32):``,
#: ``From NameOne on Monday:``), a chat log's ``<nameone>``, a ``[10:32]`` before either, after a list marker, on a
#: quoted line or in Markdown emphasis (``**NameOne:**``).  Any such head counts, the owner's own ``Update:`` too:
#: telling a speaker's name from a heading is not this rule's to try.  A head that begins with the first person
#: (``我说：``, ``I think:``) is no label, nor is a URL's scheme.
_SPEAKER_LABEL = re.compile(
    r"(?:^|[\n。！？!?；;])[^\S\n]*(?:(?:[-*+•]|\d+[.)、])[^\S\n]+)?"
    r"(?:\[[^\]\n]{0,24}\][^\S\n]*)?(?:\*\*|__|\*|_)?"
    r"(?:<[^<>\n]{1,24}>|(?!我|I\b)[^\s:：*_][^\n:：]{0,32}[：:](?!//))",
    re.I,
)
_FIRST_PERSON = re.compile(r"\bI\b|\bmy\b|我", re.I)
#: A line of three or more backticks or tildes, which opens or closes a fenced block of pasted text (Markdown), with
#: whatever follows it on the line.
FENCE = re.compile(r"(?m)^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
#: A speaker's own framing at the head of a clause, after a list marker too (老实说，一般来说，我跟你说，"That said,"):
#: what follows it is their own words.  After a name it is someone else's again: 张三跟你说，…, "a message that
#: said, …".
_OWN_FRAMING = re.compile(
    r"(?:^|(?<=[\n，,;；。.!?！？：:]))\s*(?:(?:[-*+•]|\d+[.)、])\s*)?"
    r"(?:我?(?:(?:一般|总的|总得|总体|整体|具体|简单|严格|相对|通常|对我|对于我)来"
    r"|老实|坦白|直白|简单|实话实|实话|换句话|话|再|虽|不用|照理|按理"
    r"|这么|那么|怎么|所以|也就是|就是|可以|应该|不得不|跟你|和你)说"
    r"|(?:that\s+said|that\s+being\s+said|having\s+said\s+that)\b)",
    re.I,
)
#: The head of an English clause before its first person: a list marker, then words that frame it.  An English
#: self-report stands there: "The rumor that I prefer blue is false" reports no preference.
_ENGLISH_HEAD = (
    r"^\s*(?:(?:[-*+•]|\d+[.)])\s+)?"
    r"(?:(?:honestly|actually|personally|frankly|basically|generally|usually|normally|overall|anyway|also|and|but"
    r"|so|well|now|yes|yeah|yep|no|ok|okay|oh|btw|fyi|tbh|lately|nowadays|in\s+general|in\s+practice|in\s+fact"
    r"|in\s+short|of\s+course|as\s+a\s+rule|for\s+the\s+most\s+part|most\s+of\s+the\s+time|at\s+home|at\s+work"
    r"|by\s+the\s+way|to\s+be\s+honest|for\s+me|as\s+for\s+me|these\s+days)\b[\s,]*)*"
)
#: An everyday self-report -- 我不吃辣, 我从不抽烟, 我对花生过敏, I never drink coffee: the person heads the clause and
#: the value stands straight after the verb or holds it (``_everyday_self_report``).  看, 听 and 说 are not among the
#: verbs: 我看不吃辣的人更健康 is the speaker's view, not a report of themselves; nor is a plan (打算, plan to), whose
#: negation an intention would not keep.  对X过敏 crosses no other 对 and no 的 (我对象对花生过敏 is a partner's).
_EVERYDAY_SELF_REPORT = (
    re.compile(
        r"(?:^|[\s：:])(?:本次|这次|今天|平时|通常|目前|现在|本轮)*"
        r"我(?:本次|这次|今天|平时|通常|个人|一直|目前|现在|本轮|从来|向来|基本|几乎|很少|偶尔|经常|总是|一般|平常|"
        r"每天|早上|中午|晚上|已经|也|还|都|就|只|从|不|没|太|很|更|最|能|会)*"
        r"(?:(?:(?:吃|喝|抽|吸|穿|戴|碰|用|玩|开|住|睡|养|信|爱|讨厌|怕|害怕|戒)"
        r"(?:了|过|着|不了|得了|不惯|得惯)?){1,2}"
        r"|对(?![象面方])[^，,;；。!?！？\n对的]{1,20}?(?:过敏|感兴趣|有兴趣|没兴趣|没有兴趣)(?!的))"
    ),
    re.compile(
        _ENGLISH_HEAD
        + r"(?:I\s+(?:(?:usually|always|often|never|rarely|seldom|sometimes|also|really|just|still|mostly|generally|"
        r"normally)\s+)*(?:(?:do\s+not|don[’']t|cannot|can[’']t|will\s+not|won[’']t)\s+)?"
        r"(?:(?:really|usually|always|often|ever|even)\s+)?"
        r"(?:eat|drink|smoke|wear|drive|play|hate|love|enjoy|dislike|avoid)\b(?:\s+(?:a|an|the)\b)?"
        r"|I(?:\s+am|[’']m)\s+allergic\s+to\b)",
        re.I,
    ),
)
#: An English first person of preference or decision, at the head of its clause as the everyday ones are.
_ENGLISH_SELF_REPORT = re.compile(
    _ENGLISH_HEAD
    + r"(?:I\s+(?:(?:do\s+not|don[’']t)\s+)?(?:prefer|like|want|need|use|choose|decide)\b"
    + r"|my\s+(?:preference|decision|requirement|constraint|habit)\b)",
    re.I,
)
_ENGLISH_FACT = re.compile(
    _ENGLISH_HEAD + r"I\s+(?:am|(?:(?:do\s+not|don[’']t|never|no\s+longer)\s+)?(?:have|live|work))\b", re.I
)
_CJK_CHAR = re.compile(r"[\u3400-\u9fff]")
_SENTENCE_BREAK = re.compile(r"[;；。!?！？\n]")
_DURABLE_DIRECTIVE = re.compile(
    r"以后|今后|从现在起|长期|始终|一直|每次|每当|默认|平时|通常|惯例|"
    r"(?:当|在)[^。！？!?;；\n]{1,80}时|"
    r"\b(?:from now on|long[- ]term|always|every time|whenever|by default|usually|generally|"
    r"when\s+[^.;!?\n]{1,80})\b",
    re.I,
)
_DIRECT_REQUEST = re.compile(
    r"(?:请|帮我|麻烦|给我)[^。！？!?;；\n]{0,80}"
    r"(?:写|生成|创建|检查|分析|修改|整理|发送|打开|运行|执行|部署|翻译|总结|回答|做)|"
    r"\bplease\b[^.;!?\n]{0,100}\b(?:write|create|generate|check|analy[sz]e|edit|organize|"
    r"send|open|run|execute|deploy|translate|summari[sz]e|answer|make)\b",
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


def _everyday_self_report(content, left, occurrence):
    """Whether the clause from ``left`` holds an everyday self-report whose verb the value ``occurrence`` stands
    straight after or overlaps.  A value that opens a relative clause (``讨厌吃香菜`` + ``的人``) reports no one."""
    after = CLAUSE_BREAK.search(content, occurrence.end())
    clause = content[left : after.start() if after else len(content)]
    start, end = occurrence.start() - left, occurrence.end() - left
    if clause[end : end + 1] == "的" and _CJK_CHAR.match(clause[end + 1 : end + 2]):
        return False
    return any(
        match.start() <= start and (start < match.end() or not clause[match.end() : start].strip())
        for pattern in _EVERYDAY_SELF_REPORT
        for match in pattern.finditer(clause)
    )


def _outside_fences(before: str) -> tuple[bool, str]:
    """Whether a fenced block is open at the end of ``before``, and ``before`` without the blocks in it.  A block
    opens on a line of three or more backticks or tildes, and closes only on a line of the same character, at least
    as long, with nothing after it (CommonMark): inside a tilde block a backtick line is text."""
    kept, opened, start = [], None, 0
    for match in FENCE.finditer(before):
        marks, rest = match.group(1), match.group(2)
        if opened is None:
            if marks[0] != "`" or "`" not in rest:  # a backtick fence's info string holds no backtick
                kept.append(before[start : match.start()])
                opened = (marks[0], len(marks))
        elif marks[0] == opened[0] and len(marks) >= opened[1] and not rest.strip():
            opened, start = None, match.end()
    if opened is None:
        kept.append(before[start:])
    return opened is not None, "".join(kept)


#: The mark that closes each opening quotation mark; straight marks do both.
_CLOSING_MARK = {"“": "”", "「": "」", "『": "』", "‘": "’", '"': '"', "'": "'"}
#: What a straight single quotation mark opens after: the line's start, a space, an opening bracket or mark.
_SINGLE_OPENS_AFTER = frozenset('\n \t([{（【“‘「『"')
#: What follows a straight single quotation mark that closes: a space, punctuation, a closing mark or the end.
_SINGLE_CLOSES_BEFORE = frozenset('\n \t.,!?;:)]}。，！？；：）】」』"”’')
#: A decade or a year cut to two digits, after its apostrophe: the '90s, class of '99.
_ELIDED_YEAR = re.compile(r"\d\ds?\b")
#: Punctuation that ends or parts a clause: Chinese marks anywhere, a comma, colon, semicolon, question or exclamation
#: mark or full stop before a space or the end (not the joints of host:8443 or v3.1).
_CLAUSE_PUNCTUATION = re.compile(r"[。，；：！？、]|[,;:!?.](?=\s|$)")
#: A word of a quoted span: a Latin word or an identifier with its joints, or a run of Chinese characters.
_QUOTED_WORD = re.compile(rf"[A-Za-z0-9][A-Za-z0-9'’._:/-]*|{_CJK_CHAR.pattern}+")
#: Verbs that make even a title-cased English span or an unspaced Chinese span a statement.
_QUOTED_STATEMENT = re.compile(r"\b(?:is|are|was|were|runs?|uses?|listens?)\b|\S(?:是|不吃|使用|改成|设为)\S", re.I)
#: A temporal clause supplies an action beside its time, regardless of the action's spelling or capitalisation.
_QUOTED_TIME_CLAUSE = re.compile(
    r"\S+\s+\S+\s+(?:on\s+)?(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|today|tomorrow)\b"
    r"|(?:周[一二三四五六日天]|今天|明天|昨天)\S+",
    re.I,
)
#: A source introducing quoted words at the end of its clause, or a colon immediately before them.  称 is that verb,
#: not the second character of 名称; a copula may stand between the source and the quotation (通知是).
_QUOTE_INTRODUCER = re.compile(
    r"(?:\b(?:said|says|wrote|writes)\b|写着|说道|表示|公告|通知|(?<!名)称|说)\s*(?:是)?\s*$|[:：]\s*$",
    re.I,
)
#: Explicit naming keeps even a long name in the writer's statement.
_QUOTE_NAMING = re.compile(r"\b(?:call(?:ed)?\s+(?:it|this|that)|nam(?:e|ed))\s*$|(?:叫|名为|称为)\s*$", re.I)
#: A conjunction after a quotation ends its clause rather than opening a possessive's noun phrase.
_AFTER_QUOTE = re.compile(r"\s+(?:and|but|or|so|yet|then)\b", re.I)
#: A determiner or another possessive introduces a plural possessor whose head is a lower-case common noun.  A
#: capitalised name (The Alps) is not one, so the mark after it closes the quotation.
_PLURAL_POSSESSOR = re.compile(
    r"(?:\b(?:[Tt]he|[Tt]hese|[Tt]hose|[Oo]ur|[Tt]heir|[Yy]our|[Ii]ts|[Mm]y|[Aa]ll"
    r"|[Ss]ome|[Mm]any|[Ss]everal|[Bb]oth|[Oo]ther)\s+|['’]s\s+)[a-z]+s$"
)


def _internal_mark(text: str, at: int) -> bool:
    """Whether the ``'`` or ``’`` at ``at`` sits inside a word, or before a decade or a year (``_ELIDED_YEAR``)."""
    before = text[at - 1] if at else ""
    after = text[at + 1 : at + 2]
    return ((before.isalnum() or before == "_") and (after.isalnum() or after == "_")) or (
        _ELIDED_YEAR.match(text, at + 1) is not None
    )


def _later_closer(text: str, at: int, mark: str) -> bool:
    """A matching closer after ``at``.  A nested quotation of the same mark is read past: its opening mark does not
    mean that no closer follows.  A word-internal apostrophe (``_internal_mark``) closes nothing."""
    depth = 0
    for later in range(at + 1, len(text)):
        if text[later] != mark or _internal_mark(text, later):
            continue
        if _opening_quote(text, later):
            depth += 1
            continue
        before, after = text[later - 1], text[later + 1 : later + 2]
        if (not before.isspace() or mark == "’") and (not after or after in _SINGLE_CLOSES_BEFORE):
            if depth:
                depth -= 1
                continue
            return True
    return False


def _apostrophe(text: str, at: int, opening: int | None) -> bool:
    """Whether the ``'`` or ``’`` at ``at`` is an apostrophe rather than a quotation mark: between word characters
    (Orchard's, don't), after the s that ends a plural (the users' choices), or before a decade or a year cut to two
    digits (``_ELIDED_YEAR``).  After s inside a quotation, the possessor's head is a lower-case common noun
    (``_PLURAL_POSSESSOR``), so a capitalised name closes, and a later closer (``_later_closer``) must follow; a
    conjunction (``_AFTER_QUOTE``) resumes the writer's clause instead."""
    before = text[at - 1] if at else ""
    after = text[at + 1 : at + 2]
    if _internal_mark(text, at):
        return True
    if before in ("s", "S") and at >= 2 and text[at - 2].isalnum() and after in (" ", "\t"):
        return opening is None or (
            _PLURAL_POSSESSOR.search(text[opening + 1 : at]) is not None
            and _AFTER_QUOTE.match(text, at + 1) is None
            and _later_closer(text, at, _CLOSING_MARK[text[opening]])
        )
    return False


def _opening_quote(text: str, at: int) -> bool:
    if text[at] != "'":
        return text[at] in _CLOSING_MARK
    before = text[at - 1] if at else "\n"
    after = text[at + 1 : at + 2]
    return before in _SINGLE_OPENS_AFTER and bool(after) and not after.isspace()


def _scan_quotes(text: str, *, through: int | None = None) -> tuple[list[tuple[int, int]], list[tuple[str, int]]]:
    """The outermost spans of ``text`` between quotation marks, each from the mark that opens it to the one that
    closes it.  A mark closes the innermost quotation it can close, so one nested in another ends inside it
    (‘Orchard runs on ‘production’ port 8443’), and what opened inside a quotation that closes is closed with it.
    Marks may run across lines.  A straight double mark closes the one it opened; a
    straight single mark opens after a space or an opening mark (``_SINGLE_OPENS_AFTER``) and closes before a space,
    punctuation or the end (``_SINGLE_CLOSES_BEFORE``).  An apostrophe (``_apostrophe``) is no mark.  Open quotations
    are returned separately so capture and recall read nesting and apostrophes by the same rules.  ``through`` stops
    the scan at a position while keeping the rest of the text available to disambiguate a plural possessive."""
    spans: list[tuple[int, int]] = []
    opened: list[tuple[str, int]] = []  # the mark that closes each open quotation, and where it opened
    for at, char in enumerate(text[:through]):
        closes = next((depth for depth in range(len(opened) - 1, -1, -1) if opened[depth][0] == char), None)
        opening = opened[closes][1] if closes is not None else None
        if char in ("'", "’") and _apostrophe(text, at, opening):
            continue
        before = text[at - 1] if at else "\n"
        after = text[at + 1] if at + 1 < len(text) else "\n"
        if closes is not None and (char != "'" or (not before.isspace() and after in _SINGLE_CLOSES_BEFORE)):
            start = opened[closes][1]
            del opened[closes:]
            if not opened:
                spans.append((start, at + 1))
        elif _opening_quote(text, at):
            opened.append((_CLOSING_MARK[char], at))
    return spans, opened


def _reported(span: str, context: str) -> bool:
    """Whether a quoted span reports someone's words, rather than set off a term of the writer's own.  A clause does:
    it holds a clause's punctuation (``_CLAUSE_PUNCTUATION``), a statement verb (``_QUOTED_STATEMENT``), or several
    words (``_QUOTED_WORD``), or an action beside its time (``_QUOTED_TIME_CLAUSE``).  A source introducing it
    (``_QUOTE_INTRODUCER``) also marks a multiword or Chinese
    clause, regardless of capitals or spacing.  That introducer is the verb or source at the end of the clause, so the
    称 of 名称 introduces nothing.  Explicit naming (``_QUOTE_NAMING``) keeps a name; without an
    introduction, capitalised words can name a thing (Blue Ocean, New York City, Release 2).  A reported
    line says that a thing holds ("Lantern staging database port: 7012", “部署区域 eu-central-1”,
    ‘Orchard runs on port 8443’), and the writer does not say
    so.  One word, a name, an identifier or a value ('Orchard', “果园计划”, "eu-west-1") is the writer's own text:
    the marks set off what it names, and reading it out would leave their own statement without what it is about."""
    if _CLAUSE_PUNCTUATION.search(span):
        return True
    if _QUOTE_NAMING.search(context):
        return False
    words = _QUOTED_WORD.findall(span)
    statement = _QUOTED_STATEMENT.search(span) and (len(words) > 1 or _CJK_CHAR.search(span))
    introduced = _QUOTE_INTRODUCER.search(context) and (len(words) > 1 or _CJK_CHAR.search(span))
    return bool(statement or introduced or _QUOTED_TIME_CLAUSE.search(span)) or (
        len(words) > 1 and not all(word[0].isupper() or word[0].isdigit() for word in words)
    )


def unquoted(text: str) -> str:
    """``text`` without the quotations it holds (``_scan_quotes``) that report someone's words (``_reported``).  A
    term, a name or a value its writer sets off in quotation marks stays (We call it "Orchard"; port 8443)."""
    kept: list[str] = []
    last = 0
    for start, end in _scan_quotes(text)[0]:
        context = CLAUSE_BREAK.split(text[:start])[-1]
        if _reported(text[start + 1 : end - 1], context):
            kept.extend((text[last:start], " "))
            last = end
    kept.append(text[last:])
    return "".join(kept)


def quoted_at(content: str, position: int) -> bool:
    """Whether ``position`` stands in quoted text: inside a fenced block, inside a quotation still open, or on a quoted
    line (``> ...``).  Quotation marks are read outside fenced blocks: code's own (``'"'``) open none."""
    before = content[:position]
    fenced, outside = _outside_fences(before)
    line = before[before.rfind("\n") + 1 :]
    return (
        fenced
        or bool(_scan_quotes(outside + content[position:], through=len(outside))[1])
        or line.lstrip().startswith(">")
    )


def _pasted_at(content: str, position: int) -> bool:
    """Quoted text (``quoted_at``), or text after a speaker's label; labels are read outside fenced blocks too: code's
    own (``x: int``) marks no speaker."""
    return quoted_at(content, position) or _SPEAKER_LABEL.search(_outside_fences(content[:position])[1]) is not None


def in_pasted_text(content: str, quote: str) -> bool:
    """Whether a first person in the quoted words stands in text the owner pasted rather than said, judged in the
    whole message: after a line opening with a speaker's label (``_SPEAKER_LABEL``), inside a fenced block, inside a
    quotation still open across lines, or on a quoted line (``> ...``).  A claim's evidence is read in a context
    narrowed to the quote's sentence, which leaves all of these out (``NameOne:`` ... ``My preference is blue.``), and a
    quote may hold a fence's opening line itself."""
    at = content.rfind(quote) if quote else -1
    if at < 0:
        return False
    persons = [match.start() for match in _FIRST_PERSON.finditer(content, at, at + len(quote))] or [at]
    return any(_pasted_at(content, position) for position in persons)


def _after_a_label(content: str) -> bool:
    """Whether a first person stands after a line opening with a speaker's label (``_SPEAKER_LABEL``): a pasted
    transcript is someone else's words, and the owner's own labelled notes (``Update: … I …``) are refused with it.
    The first label and the first person after it, each found in one pass: one pattern spanning both read a long
    log again from every label."""
    label = _SPEAKER_LABEL.search(content)
    return label is not None and _FIRST_PERSON.search(content, label.end()) is not None


def self_report_bound(content, value_text, *, kind=None):
    """Bind singular self-report to the value's clause, never to a third party.

    This deliberately does not equate a team, a possessive third-party noun,
    or a quoted first-person sentence with the current user.
    """
    unframed = _OWN_FRAMING.sub("", content)
    if not value_text or _REPORTED_SELF.search(unframed) or _after_a_label(unframed):
        return False
    for occurrence in literal_spans(content, value_text):
        before = list(CLAUSE_BREAK.finditer(content, 0, occurrence.start()))
        left = before[-1].end() if before else 0
        if _everyday_self_report(content, left, occurrence):
            return True
        # Include the value: a faithful negative value may itself contain the
        # assertion verb ("我" + "不喜欢蓝色", "I" + "do not like blue").
        prefix = content[left : occurrence.end()]
        # A bare 我 is grammatical first person; 我的X requires the exact
        # preference/decision attribute, not an arbitrary person owned by 我.
        chinese = re.search(
            r"(?:^|[\s：:])(?:本次|这次|今天|平时|通常|目前|现在|本轮)*"
            r"我(?:本次|这次|今天|平时|通常|个人|一直|目前|现在|本轮|更|最|不|很|也|还)*"
            r"(?:在[^，,;；。!?！？\n]{1,80}?)?(?:喜欢|偏好|需要|想要|选择|决定|要求|使用|习惯|希望|认可|接受|禁止|不许|不准)"
            r"|(?:^|[\s：:])我的(?:偏好|喜好|决定|要求|约束|习惯)",
            prefix,
        )
        english = _ENGLISH_SELF_REPORT.search(prefix)
        if kind == "fact":
            chinese = chinese or re.search(r"(?:^|[\s：:])我(?:不|没)?(?:是|有|住在|工作于)", prefix)
            english = english or _ENGLISH_FACT.search(prefix)
        if chinese or english:
            return True
    return False


def _condition_text(text):
    # NFC/whitespace changes preserve identifiers; no case folding or aliases.
    text = " ".join(unicodedata.normalize("NFC", text).split()).strip()
    if text.startswith("当") and text.endswith("时") and len(text) > 3:
        return text[1:-1].strip()
    if text.endswith("时") and len(text) > 2 and not text[-2].isdigit():
        return text[:-1].strip()
    matched = re.fullmatch(r"when\s+(.+)", text, re.I)
    return matched.group(1).strip() if matched else text


def condition_supports_value(content, condition, value_text):
    """Require a proposed condition beside the value it is said to govern.

    Merely finding a condition in one sentence and a value in another does not
    establish their relationship. Commas remain inside the assertion so common
    forms such as ``写小说时，我偏好详细`` stay representable.
    """
    if any(type(value) is not str or not value.strip() for value in (content, condition, value_text)):
        return False
    normalized_content = "\n".join(
        " ".join(unicodedata.normalize("NFC", line).split()) for line in content.splitlines()
    )
    normalized_condition = " ".join(unicodedata.normalize("NFC", condition).split())
    normalized_value = " ".join(unicodedata.normalize("NFC", value_text).split())
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


#: An example, which shows what might be said rather than says it (举例, 示例, "for example", "Example: …").
_EXAMPLE = re.compile(r"举例|示例|\b(?:for example|example:)", re.I)


def attributed_to_another(content: str) -> bool:
    """Whether the text gives what it says to anyone but its writer: reported speech (``REPORTED_SPEECH``), a speaker
    before what they said (``_SAID_BY_ANOTHER``), a line opening with their name (``_SPEAKER_LINE``) or an example
    (``_EXAMPLE``), outside the writer's own framing (``老实说，``, ``That said,``).  The one check that claims and
    corrections are read by; the writer's own ``我表示…`` stays theirs."""
    unframed = _OWN_FRAMING.sub("", content)
    return bool(
        REPORTED_SPEECH.search(content)
        or _EXAMPLE.search(content)
        or _SAID_BY_ANOTHER.search(unframed)
        or _SPEAKER_LINE.search(unframed)
    )


def first_person_reference(content):
    """Return whether direct text refers to its verified speaker in first person."""
    if type(content) is not str or REPORTED_SPEECH.search(content):
        return False
    return re.search(r"(?:\b(?:I|me|my|mine|myself)\b|我|我的|本人)", content, re.I) is not None


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
    context = " ".join(unicodedata.normalize("NFC", context_text).split())
    if UNASSERTED_CONTEXT.search(context):
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
        before = list(CLAUSE_BREAK.finditer(content, 0, at))
        left = before[-1].end() if before else 0
        after = CLAUSE_BREAK.search(content, end)
        right = after.start() if after else len(content)
        # Explicit conditions retain their own negation (e.g. 未授权). They do
        # not justify dropping a separate prohibition from the asserted value.
        covered = [
            match.span()
            for condition in conditions
            for match in literal_spans(content, condition)
            if left <= match.start() and match.end() <= right
        ]
        markers = POLARITY if polarity_only else QUALIFIER
        if any(
            (m.start() < at or m.end() > end) and not any(a <= m.start() and m.end() <= b for a, b in covered)
            for m in markers.finditer(content, left, right)
        ):
            return False
    return bool(occurrences)


def asserted_marker(pattern, content):
    """Whether ``content`` asserts one of ``pattern``'s markers: not asked, not under a condition or a supposition,
    and with the marker's qualifiers kept."""
    return not (AUTHORITY_QUESTION.search(content) or CONDITIONAL.search(content)) and any(
        preserves_qualifiers(content, match.group()) for match in pattern.finditer(content)
    )
