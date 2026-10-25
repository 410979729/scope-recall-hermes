"""The synthetic world the recall benchmark asks about, built from hand-written templates and a seed.

A world is the cast of one shared store (owners, the entries they talk to, the scopes each entry reads), weeks of
sessions in those scopes (the owners' messages, the assistants' replies, tool output), the claims a consolidation
model proposes for them, and questions with the messages that answer them.  ``build_world`` gives the same world for
the same split and scale on every machine: everything is drawn from one ``random.Random`` in a fixed order.  Nothing
in it is real.

Beside its answer a question has hard negatives: the same attribute of another project, a hypothetical, a quotation,
a denial, an assistant's unconfirmed guess and tool output that name another value, the value a later message
replaced, and the same subject on another day.  Claims are proposed the way the consolidation contract asks of a
careful extractor (``careful``): a hypothetical and a quotation carry their statement kind, and a denial quotes its
negation with the value.  An assistant's guess and tool output are proposed as statements, as an extractor may; keeping
them from becoming facts is the store's part.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta, timezone
from itertools import count, cycle
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: Each split's templates and seed.  The held-out templates were written apart from the dev ones, with their own
#: cast, projects, values and phrasings, so a change tuned on dev is also measured on wording it never saw.
SPLITS = {"dev": ("templates_dev.json", 20260917), "heldout": ("templates_heldout.json", 4111)}
TASKS = (
    "single_fact",
    "preference",
    "temporal",
    "knowledge_update",
    "aggregation",
    "abstention",
    "cross_scope",
    "relevance_floor",
    "no_memory",
)
#: Questions of each task at scale 1, the size the CI test runs; a larger scale asks as many more.
QUESTIONS = {
    "single_fact": 36,
    "preference": 24,
    "temporal": 30,
    "knowledge_update": 30,
    "aggregation": 20,
    "abstention": 30,
    "cross_scope": 18,
}
#: Days of conversation at scale 1, ending the evening before the world's now; a larger scale adds as many.
DAYS = 28
#: The kinds of message that look like the answer to a question about a project's attribute and are not one.
NEGATIVE_KINDS = ("other_project", "hypothetical", "quoted", "negated", "assistant", "tool")
#: The statement kind a careful extractor gives a sentence of each template form; any other form is an assertion.
STATEMENT_KINDS = {"hypothetical": "hypothetical", "quoted": "quotation"}
#: Chance that an entry also has a session of ordinary chatter on a day, and how many turns it runs.
CHATTER_ODDS = 0.45
CHATTER_TURNS = (1, 3)
#: Turns of one session; a day with more turns in one place opens another session.
SESSION_TURNS = 5

USER, ASSISTANT, TOOL = "human_direct", "assistant_visible", "tool_observation"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_FULL_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


@dataclass(frozen=True)
class Entry:
    """One agent of the shared store: whose it is and which scopes it reads (and captures into)."""

    id: str
    name: str
    host: str
    owner: str
    reads: frozenset[str]


@dataclass(frozen=True)
class Message:
    """One captured message; ``at`` is its UTC instant."""

    key: str
    entry: str
    scope: str
    session: str
    origin: str
    text: str
    at: str


@dataclass(frozen=True)
class Claim:
    """A claim the stub consolidation proposes for a sentence."""

    kind: str
    subject: str
    predicate: str
    value: str
    statement_kind: str = "assertion"


@dataclass(frozen=True)
class Question:
    """A question one entry asks at the world's now.

    ``gold`` holds the keys of the messages that answer it, each a separate part of the answer (a claim drawn from one
    answers that part too); it is empty when nothing the entry may read answers it.  ``negatives`` are ``(kind, key)``
    of messages that look like an answer and are not one.  ``revoked`` are scopes the entry read until just before it
    asks: the question is asked once with them first, in the same session.
    """

    id: str
    task: str
    form: str
    language: str
    entry: str
    reads: frozenset[str]
    text: str
    gold: tuple[str, ...]
    negatives: tuple[tuple[str, str], ...] = ()
    revoked: frozenset[str] = frozenset()
    #: An existing captured session for a conversational follow-up; other questions open their own session.
    session: str | None = None


@dataclass(frozen=True)
class World:
    split: str
    seed: int
    scale: int
    zone: timezone
    now: str
    entries: tuple[Entry, ...]
    messages: tuple[Message, ...]
    claims: dict[str, tuple[Claim, ...]]
    questions: tuple[Question, ...]

    @property
    def scopes(self) -> frozenset[str]:
        return frozenset(scope for entry in self.entries for scope in entry.reads)

    def fingerprint(self) -> str:
        """A digest of everything the world holds: the baseline names the world it was measured on."""
        claims = {text: [asdict(claim) for claim in claims] for text, claims in self.claims.items()}
        items = [asdict(item) for item in (*self.entries, *self.messages, *self.questions)]
        payload = json.dumps(
            [self.split, self.seed, self.scale, str(self.zone), self.now, items, claims],
            ensure_ascii=False,
            sort_keys=True,
            default=sorted,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_world(split: str = "dev", *, scale: int = 1) -> World:
    if type(scale) is not int or scale < 1:
        raise ValueError("scale is a whole number from 1")
    name, seed = SPLITS[split]
    templates = json.loads((HERE / name).read_text(encoding="utf-8"))
    return _automatic_cases(_Plan(templates, split, seed, scale).world(), templates["automatic_recall"])


def _automatic_cases(world: World, templates: dict) -> World:
    """Add independent gate cases after the original world's random draws.

    Their messages occupy the morning of the query day, outside the historical
    days existing questions enumerate. Both splits provide their own wording.
    """
    # A separate audience keeps added distractors from changing the existing
    # tasks' vector ranks, especially in the small word-hashing space.
    scope = f"{world.split}-gate-scope"
    entry = Entry("gate-entry", "Recall lab", "hermes", world.entries[0].owner, frozenset({scope}))
    session = f"{world.split}-gate-session"
    at = _utc(datetime.fromisoformat(world.now.replace("Z", "+00:00")) - timedelta(minutes=1))
    messages, questions, claims = list(world.messages), list(world.questions), dict(world.claims)
    context_key = f"{world.split}/gate-context"
    messages.append(
        Message(f"{world.split}/gate-prompt", entry.id, scope, session, USER, templates["session_prompt"], at)
    )
    messages.append(Message(context_key, entry.id, scope, session, ASSISTANT, templates["session_context"], at))
    for index, case in enumerate(templates["cases"]):
        gold, negatives = [], []
        for position, source in enumerate(case["sources"]):
            key = f"{world.split}/gate-{index}-{position}"
            messages.append(
                Message(key, entry.id, scope, f"{session}-{index}", source.get("origin", USER), source["text"], at)
            )
            if source["kind"] == "answer":
                gold.append(key)
            else:
                negatives.append((source["kind"], key))
            if source.get("claims"):
                claims[source["text"]] = tuple(Claim(**claim) for claim in source["claims"])
        for query_index, query in enumerate(case.get("queries", [case["query"]])):
            questions.append(
                Question(
                    f"{world.split}-gate-{index}-{query_index}",
                    "relevance_floor",
                    case["form"],
                    case["language"],
                    entry.id,
                    entry.reads,
                    query,
                    tuple(gold),
                    tuple(negatives),
                )
            )
    for index, query in enumerate(templates["no_memory"]):
        questions.append(
            Question(
                f"{world.split}-no-memory-{index}",
                "no_memory",
                "mid_session",
                "mixed",
                entry.id,
                entry.reads,
                query,
                (),
                (("agent_narration", context_key),),
                session=session,
            )
        )
    return replace(
        world,
        entries=(*world.entries, entry),
        messages=tuple(sorted(messages, key=lambda message: (message.at, message.key))),
        questions=tuple(questions),
        claims=claims,
    )


@dataclass
class _Turn:
    day: int
    entry: str
    scope: str
    messages: list[tuple[str, str, str]]


class _Plan:
    """Lays out what is said where and when, and what is asked about it.

    Turns are planned first, each on a day, in one entry and scope; ``world`` then adds the chatter and groups the
    turns into timed sessions.  A day whose conversation a question summarises is set aside in the scopes its asker
    reads (``quiet``): nothing else is said there that day, so the question's gold is that day's whole conversation.
    """

    def __init__(self, templates: dict, split: str, seed: int, scale: int) -> None:
        self.t = templates
        self.split, self.seed, self.scale = split, seed, scale
        self.rng = random.Random(seed)
        self.days = DAYS * scale
        self.zone = timezone(timedelta(hours=templates["zone_hours"]))
        self.now = datetime.fromisoformat(templates["now"]).replace(tzinfo=self.zone)
        if self.now.weekday() != 0:
            # Last week is then the seven days before now, which is where its questions' answers are said.
            raise ValueError(f"the {split} templates' now is not a Monday")
        self.first_day = self.now.date() - timedelta(days=self.days)
        self.owners = {owner["id"]: owner for owner in templates["cast"]["owners"]}
        self.entries = tuple(
            Entry(item["id"], item["name"], item["host"], item["owner"], frozenset(item["reads"]))
            for item in templates["cast"]["entries"]
        )
        self.scopes = sorted({scope for entry in self.entries for scope in entry.reads})
        self.keys = count(1)
        self.turns: list[_Turn] = []
        self.questions: list[Question] = []
        self.claims: dict[str, list[Claim]] = {}
        self.quiet: dict[int, set[str]] = {}
        projects = list(templates["projects"])
        # A project's attribute is said in one scope at most: two entries reading it would otherwise disagree.
        self.free_projects = {index: self._shuffled(projects) for index in range(len(templates["facts"]))}
        self.fact_order = cycle(self._shuffled(range(len(templates["facts"]))))
        self.topics = self._shuffled([(t, p) for t in range(len(templates["topics"])) for p in projects])
        self.collections = {index: self._shuffled(projects) for index in range(len(templates["collections"]))}
        self.collection_order = cycle(self._shuffled(range(len(templates["collections"]))))
        self.ordering_projects = self._shuffled(projects)
        self.free_preferences = {owner: self._shuffled(range(len(templates["preferences"]))) for owner in self.owners}
        self.free_implied = {
            (owner, language): self._shuffled(range(len(items)))
            for owner in self.owners
            for language, items in templates["implied"].items()
        }

    # -- drawing ---------------------------------------------------------------------------------------------------

    def _shuffled(self, items) -> list:
        items = list(items)
        self.rng.shuffle(items)
        return items

    def _pop(self, pool: list, what: str):
        if not pool:
            raise ValueError(f"the {self.split} templates hold too few {what} for scale {self.scale}")
        return pool.pop()

    def _language(self, owner: str) -> str:
        weights = self.owners[owner]["languages"]
        return self.rng.choices(sorted(weights), [weights[name] for name in sorted(weights)])[0]

    def _day(self, scope: str, first: int = 0, last: int | None = None, *, avoid=()) -> int:
        """A day in ``first..last`` (the day before now by default) not set aside in ``scope``, nor in ``avoid``."""
        last = self.days - 1 if last is None else last
        days = [d for d in range(first, last + 1) if scope not in self.quiet.get(d, ()) and d not in avoid]
        return self.rng.choice(days)

    def _reading(self, scope: str) -> Entry:
        """An entry that reads ``scope``: one that captures there, or asks about what was said there."""
        return self.rng.choice([entry for entry in self.entries if scope in entry.reads])

    def _home(self, owner: str) -> str:
        return self.owners[owner]["home"]

    def _values(self, values, language: str, k: int) -> list[str]:
        """``k`` different values in ``language``: a list is the same in every language, a mapping has one each."""
        pool = values[language] if isinstance(values, dict) else values
        return [value[language] if isinstance(value, dict) else value for value in self.rng.sample(pool, k)]

    def _date(self, day: int, language: str, style: int) -> str:
        moment = self.first_day + timedelta(days=day)
        if style % 3 == 2:
            return moment.isoformat()
        if language == "zh":
            return f"{moment.month}月{moment.day}{'日' if style % 3 == 0 else '号'}"
        return f"{(_MONTHS if style % 3 == 0 else _FULL_MONTHS)[moment.month - 1]} {moment.day}"

    # -- turns -----------------------------------------------------------------------------------------------------

    def _turn(self, day: int, entry: Entry, scope: str, *messages: tuple[str, str]) -> list[str]:
        """Plan one turn of ``(origin, text)`` messages; returns their keys."""
        keyed = [(f"{self.split}/{next(self.keys):05d}", origin, text) for origin, text in messages]
        self.turns.append(_Turn(day, entry.id, scope, keyed))
        return [key for key, _origin, _text in keyed]

    def _claim(self, text: str, claim: Claim | None) -> None:
        if claim is not None and claim not in self.claims.setdefault(text, []):
            self.claims[text].append(claim)

    def _ack(self, language: str) -> str:
        return self.rng.choice(self.t["filler"][language]["assistant"])

    def _say(self, day: int, entry: Entry, scope: str, language: str, text: str, claim: Claim | None = None) -> str:
        """The owner says ``text`` and the assistant acknowledges it; returns the key of what the owner said."""
        self._claim(text, claim)
        return self._turn(day, entry, scope, (USER, text), (ASSISTANT, self._ack(language)))[0]

    def _reply(self, day: int, entry: Entry, scope: str, prompt: str, text: str, claim: Claim | None) -> str:
        """The owner asks ``prompt`` and the assistant answers ``text``, unconfirmed; returns the answer's key."""
        self._claim(text, claim)
        return self._turn(day, entry, scope, (USER, prompt), (ASSISTANT, text))[1]

    def _tool(self, day: int, entry: Entry, scope: str, language: str, project: str, output: str, claim) -> str:
        """The owner asks for a look, a tool prints ``output`` and the assistant reports back; returns its key."""
        self._claim(output, claim)
        request = self.rng.choice(self.t["filler"][language]["request"]).format(project=project)
        return self._turn(day, entry, scope, (USER, request), (TOOL, output), (ASSISTANT, self._ack(language)))[1]

    def _ask(self, task: str, form: str, language: str, entry: Entry, text: str, gold, negatives=(), **extra) -> None:
        self.questions.append(
            Question(
                f"{self.split}-{task}-{len(self.questions) + 1:03d}",
                task,
                form,
                language,
                entry.id,
                extra.pop("reads", entry.reads),
                text,
                tuple(gold),
                tuple(negatives),
                **extra,
            )
        )

    # -- project attributes ----------------------------------------------------------------------------------------

    def _slot(self, fact: int | None = None) -> tuple[int, str]:
        """A project attribute nothing has been said about yet: ``fact`` of a fresh project, or any."""
        fact = next(self.fact_order) if fact is None else fact
        return fact, self._pop(self.free_projects[fact], "projects")

    def _fact(self, fact: int, language: str, form: str, project: str, value: str = "") -> str:
        return self.rng.choice(self.t["facts"][fact][language][form]).format(project=project, value=value)

    def _fact_claim(self, fact: int, language: str, project: str, value: str) -> Claim:
        return Claim("fact", project, self.t["facts"][fact]["predicate"][language], value)

    def _negative(self, kind: str, fact: int, project: str, value: str, scope: str, language: str) -> tuple[str, str]:
        """A message about ``project``'s ``fact`` (or, for ``other_project``, another project's) naming ``value``."""
        day, entry = self._day(scope, last=self.days - 2), self._reading(scope)
        if kind == "other_project":
            _fact, other = self._slot(fact)
            text = self._fact(fact, language, "say", other, value)
            return kind, self._say(day, entry, scope, language, text, self._fact_claim(fact, language, other, value))
        text = self._fact(fact, language, kind, project, value)
        text, claim = careful(kind, text, self._fact_claim(fact, language, project, value))
        if kind == "assistant":
            return kind, self._reply(day, entry, scope, self._fact(fact, language, "ask", project), text, claim)
        if kind == "tool":
            return kind, self._tool(day, entry, scope, language, project, text, claim)
        return kind, self._say(day, entry, scope, language, text, claim)

    def single_facts(self, n: int) -> None:
        kinds = cycle(NEGATIVE_KINDS)
        for index in range(n):
            scope = self.scopes[index % len(self.scopes)]
            entry = self._reading(scope)
            language = self._language(entry.owner)
            fact, project = self._slot()
            value, *others = self._values(self.t["facts"][fact]["values"], language, 3)
            day = self._day(scope, last=self.days - 2)
            claim = self._fact_claim(fact, language, project, value)
            gold = self._say(day, entry, scope, language, self._fact(fact, language, "say", project, value), claim)
            negatives = [self._negative(next(kinds), fact, project, other, scope, language) for other in others]
            question = self._fact(fact, language, "ask", project)
            self._ask("single_fact", "attribute", language, self._reading(scope), question, [gold], negatives)

    def updates(self, n: int) -> None:
        """A value said, then replaced days later: by a change (``update``) or by saying another (``contradiction``)."""
        kinds = cycle(NEGATIVE_KINDS[1:])
        owners = cycle(sorted(self.owners))
        for index in range(n):
            if index % 4 == 3:
                self._preference_change(next(owners))
                continue
            scope = self.scopes[index % len(self.scopes)]
            entry = self._reading(scope)
            language = self._language(entry.owner)
            fact, project = self._slot()
            old, new, other = self._values(self.t["facts"][fact]["values"], language, 3)
            first = self._day(scope, last=self.days // 2 - 1)
            later = self._day(scope, first + 3, self.days - 2)
            form = "update" if index % 2 == 0 else "contradiction"
            said = self._fact(fact, language, "say", project, old)
            changed = self._fact(fact, language, "change" if form == "update" else "say", project, new)
            outdated = self._say(first, entry, scope, language, said, self._fact_claim(fact, language, project, old))
            claim = self._fact_claim(fact, language, project, new)
            gold = self._say(later, self._reading(scope), scope, language, changed, claim)
            negatives = [("outdated", outdated), self._negative(next(kinds), fact, project, other, scope, language)]
            question = self._fact(fact, language, "ask", project)
            self._ask("knowledge_update", form, language, self._reading(scope), question, [gold], negatives)

    # -- preferences -----------------------------------------------------------------------------------------------

    def _preference(self, owner: str) -> tuple[dict, str, str, Entry]:
        """A preference ``owner`` has not stated yet, with a language, their home and an entry reading it."""
        preference = self.t["preferences"][self._pop(self.free_preferences[owner], "preferences")]
        language = self._language(owner)
        home = self._home(owner)
        entry = self.rng.choice([entry for entry in self.entries if entry.owner == owner and home in entry.reads])
        return preference, language, home, entry

    def _preferred(self, preference: dict, language: str, form: str, value: str = "") -> tuple[str, Claim]:
        text = self.rng.choice(preference[language][form]).format(value=value)
        return careful(form, text, Claim("preference", "user", preference["predicate"][language], value))

    def preferences(self, n: int) -> None:
        """Half stated in so many words, half implied by a remark that never names the preference."""
        owners = cycle(sorted(self.owners))
        forms = cycle(("hypothetical", "assistant", "negated"))
        for index in range(n):
            owner = next(owners)
            if index % 2:
                self._implied(owner)
                continue
            preference, language, home, entry = self._preference(owner)
            value, other = self._values(preference["values"], language, 2)
            text, claim = self._preferred(preference, language, "say", value)
            gold = self._say(self._day(home, last=self.days - 2), entry, home, language, text, claim)
            kind = next(forms)
            text, claim = self._preferred(preference, language, kind, other)
            day = self._day(home, last=self.days - 2)
            if kind == "assistant":
                ask = self.rng.choice(preference[language]["ask"])
                negative = self._reply(day, entry, home, ask, text, claim)
            else:
                negative = self._say(day, entry, home, language, text, claim)
            question = self.rng.choice(preference[language]["ask"])
            self._ask("preference", "stated", language, entry, question, [gold], [(kind, negative)])

    def _implied(self, owner: str) -> None:
        language = self._language(owner)
        item = self.t["implied"][language][self._pop(self.free_implied[owner, language], "implied preferences")]
        home = self._home(owner)
        entry = self.rng.choice([entry for entry in self.entries if entry.owner == owner and home in entry.reads])
        claim = Claim("preference", "user", item["predicate"], item["value"])
        gold = self._say(self._day(home, last=self.days - 2), entry, home, language, item["say"], claim)
        self._ask("preference", "implied", language, entry, item["ask"], [gold])

    def _preference_change(self, owner: str) -> None:
        preference, language, home, entry = self._preference(owner)
        old, new = self._values(preference["values"], language, 2)
        first = self._day(home, last=self.days // 2 - 1)
        later = self._day(home, first + 3, self.days - 2)
        outdated = self._say(first, entry, home, language, *self._preferred(preference, language, "say", old))
        gold = self._say(later, entry, home, language, *self._preferred(preference, language, "change", new))
        question = self.rng.choice(preference[language]["ask"])
        self._ask("knowledge_update", "preference_change", language, entry, question, [gold], [("outdated", outdated)])

    # -- time ------------------------------------------------------------------------------------------------------

    def day_summaries(self, n: int) -> None:
        """What was said on one day, the day before now or a named one; planned before anything else is placed."""
        entries = cycle(self.entries)
        for index in range(n):
            entry = next(entries)
            # Named days come before last week, which other questions need free.
            free = [d for d in range(self.days - 8 - 7 * self.scale, self.days - 8) if not self.quiet.get(d)]
            day = self.days - 1 if index % 3 == 0 and not self.quiet.get(self.days - 1) else self.rng.choice(free)
            self.quiet.setdefault(day, set()).update(entry.reads)
            home = self._home(entry.owner)
            scope = home if home in entry.reads else sorted(entry.reads)[0]
            language = self._language(entry.owner)
            projects = self.rng.sample(self.t["projects"], 3)
            lines = self.rng.sample(self.t["filler"][language]["user"], 3)
            gold = [
                self._say(day, entry, scope, language, line.format(project=project))
                for line, project in zip(lines[: 2 + index % 2], projects, strict=False)
            ]
            named = {"en": "yesterday", "zh": "昨天"}[language] if day == self.days - 1 else None
            when = named or self._date(day, language, index)
            if language == "en" and named is None:
                when = f"on {when}"
            template = self.rng.choice(self.t["questions"][language]["day_summary"])
            self._ask("temporal", "day_summary", language, entry, template.format(day=when), gold)

    def _topic(self, scope: str) -> tuple[Entry, str, list[str], str]:
        topic, project = self._pop(self.topics, "topics")
        entry = self._reading(scope)
        language = self._language(entry.owner)
        block = self.t["topics"][topic][language]
        notes = [note.format(project=project) for note in self.rng.sample(block["notes"], 3)]
        return entry, language, notes, block["name"].format(project=project)

    def temporal(self, n: int) -> None:
        """Questions about one day, last week, a range of days, or which of two things came first."""
        forms = cycle(("named_day", "last_week", "range", "ordering"))
        for index in range(n):
            form = next(forms)
            scope = self.scopes[index % len(self.scopes)]
            if form == "ordering":
                self._ordering(scope)
                continue
            entry, language, notes, topic = self._topic(scope)
            days = self._topic_days(form, scope)
            keys = [self._say(day, entry, scope, language, note) for note, day in zip(notes, days, strict=True)]
            templates = self.t["questions"][language][form]
            if form == "named_day":
                text = self.rng.choice(templates).format(topic=topic, date=self._date(days[0], language, index))
            elif form == "last_week":
                text = self.rng.choice(templates).format(topic=topic)
            else:
                start, end = (self._date(days[0] + shift, language, index) for shift in (-1, 1))
                text = self.rng.choice(templates).format(topic=topic, start=start, end=end)
            negatives = [("other_day", key) for key in keys[1:]]
            self._ask("temporal", form, language, self._reading(scope), text, keys[:1], negatives)

    def _topic_days(self, form: str, scope: str) -> list[int]:
        """The answer's day first, then two days that are not the one asked about."""
        if form == "last_week":
            first = self._day(scope, 0, self.days - 9)
            return [
                self._day(scope, self.days - 7, self.days - 1),
                first,
                self._day(scope, 0, self.days - 9, avoid=(first,)),
            ]
        answer = self._day(scope, 2, self.days - 3)
        # A range question asks about the day before the answer to the day after it.
        near = range(answer - 2, answer + 3)
        first = self._day(scope, 0, self.days - 2, avoid=near)
        return [answer, first, self._day(scope, 0, self.days - 2, avoid=(*near, first))]

    def _ordering(self, scope: str) -> None:
        project = self._pop(self.ordering_projects, "projects for ordering questions")
        entry = self._reading(scope)
        language = self._language(entry.owner)
        first, second = (item[language] for item in self.rng.sample(self.t["actions"], 2))
        open_days = [d for d in range(self.days - 1) if scope not in self.quiet.get(d, ())]
        earlier = self.rng.choice([d for d in open_days if d + 2 <= open_days[-1]])
        later = self.rng.choice([d for d in open_days if d >= earlier + 2])
        gold = [
            self._say(earlier, entry, scope, language, first["say"].format(project=project)),
            self._say(later, entry, scope, language, second["say"].format(project=project)),
        ]
        asked = [first["as"].format(project=project), second["as"].format(project=project)]
        self.rng.shuffle(asked)
        text = self.rng.choice(self.t["questions"][language]["ordering"]).format(first=asked[0], second=asked[1])
        self._ask("temporal", "ordering", language, self._reading(scope), text, gold)

    # -- lists -----------------------------------------------------------------------------------------------------

    def aggregations(self, n: int) -> None:
        """Two to four things said in separate sessions, asked for together."""
        for index in range(n):
            collection = next(self.collection_order)
            project = self._pop(self.collections[collection], "collections")
            scope = self.scopes[index % len(self.scopes)]
            entry = self._reading(scope)
            language = self._language(entry.owner)
            block = self.t["collections"][collection]
            size = 2 + index % 3
            items = self.rng.sample(block["items"], size + 2)
            days = self.rng.sample([d for d in range(self.days - 1) if scope not in self.quiet.get(d, ())], size + 2)
            # One question's parts are said in different words, as far as the templates go.
            says = cycle(self._shuffled(block[language]["say"]))
            gold = [
                self._say(day, self._reading(scope), scope, language, next(says).format(project=project, item=item))
                for day, item in zip(days, items[:size], strict=False)
            ]
            other = self._pop(self.collections[collection], "collections")
            listed = next(says).format(project=other, item=items[size])
            elsewhere = self._say(days[size], entry, scope, language, listed)
            maybe = self.rng.choice(block[language]["hypothetical"]).format(project=project, item=items[-1])
            negatives = [
                ("other_project", elsewhere),
                ("hypothetical", self._say(days[-1], entry, scope, language, maybe)),
            ]
            question = self.rng.choice(block[language]["ask"]).format(project=project)
            self._ask("aggregation", f"{size}_parts", language, self._reading(scope), question, gold, negatives)

    # -- what nothing answers --------------------------------------------------------------------------------------

    def abstentions(self, n: int) -> None:
        """Never said; said only as a hypothetical, a quotation or an assistant's guess; said only in another scope."""
        forms = cycle(("never_said", "hypothetical", "assistant", "quoted", "other_scope"))
        for index in range(n):
            form = next(forms)
            if form == "other_scope":
                self._elsewhere("abstention", "other_scope", *self.rng.choice(self._other_owner_pairs()))
                continue
            scope = self.scopes[index % len(self.scopes)]
            entry = self._reading(scope)
            language = self._language(entry.owner)
            fact, project = self._slot()
            (value,) = self._values(self.t["facts"][fact]["values"], language, 1)
            kind = "other_project" if form == "never_said" else form
            negative = self._negative(kind, fact, project, value, scope, language)
            question = self._fact(fact, language, "ask", project)
            self._ask("abstention", form, language, entry, question, (), [negative])

    def cross_scope(self, n: int) -> None:
        """Asked by an entry that does not read the one scope where the answer was said, or no longer reads it."""
        forms = cycle(("other_owner", "unread_scope", "revoked"))
        for _index in range(n):
            form = next(forms)
            if form == "other_owner":
                self._elsewhere("cross_scope", form, *self.rng.choice(self._other_owner_pairs()))
            elif form == "unread_scope":
                self._elsewhere("cross_scope", form, *self.rng.choice(self._unread_pairs()))
            else:
                scope, entry = self.rng.choice(self._revocable_pairs())
                self._elsewhere("cross_scope", form, scope, entry, reads=entry.reads - {scope}, revoked={scope})

    def _elsewhere(self, task: str, form: str, scope: str, asker: Entry, **extra) -> None:
        """A project attribute said in ``scope`` only, asked by ``asker``, who does not read it then."""
        speaker = self._reading(scope)
        language = self._language(speaker.owner)
        fact, project = self._slot()
        (value,) = self._values(self.t["facts"][fact]["values"], language, 1)
        claim = self._fact_claim(fact, language, project, value)
        text = self._fact(fact, language, "say", project, value)
        said = self._say(self._day(scope, last=self.days - 2), speaker, scope, language, text, claim)
        question = self._fact(fact, language, "ask", project)
        if "revoked" in extra:
            extra["revoked"] = frozenset(extra["revoked"])
        self._ask(task, form, language, asker, question, (), [("other_scope", said)], **extra)

    def _other_owner_pairs(self) -> list[tuple[str, Entry]]:
        """(an owner's home, an entry of another owner)."""
        return [
            (owner["home"], entry)
            for owner in self.owners.values()
            for entry in self.entries
            if entry.owner != owner["id"]
        ]

    def _unread_pairs(self) -> list[tuple[str, Entry]]:
        """(a scope one of an owner's entries reads, another entry of that owner that does not)."""
        return [
            (scope, entry)
            for entry in self.entries
            for scope in self.scopes
            if scope not in entry.reads
            and any(other.owner == entry.owner and scope in other.reads for other in self.entries)
        ]

    def _revocable_pairs(self) -> list[tuple[str, Entry]]:
        """(a scope an entry reads besides its owner's home, that entry)."""
        return [
            (scope, entry)
            for entry in self.entries
            for scope in sorted(entry.reads)
            if scope != self._home(entry.owner)
        ]

    # -- the world -------------------------------------------------------------------------------------------------

    def world(self) -> World:
        counts = {task: number * self.scale for task, number in QUESTIONS.items()}
        summaries = counts["temporal"] // 5
        self.day_summaries(summaries)
        self.single_facts(counts["single_fact"])
        self.preferences(counts["preference"])
        self.temporal(counts["temporal"] - summaries)
        self.updates(counts["knowledge_update"])
        self.aggregations(counts["aggregation"])
        self.abstentions(counts["abstention"])
        self.cross_scope(counts["cross_scope"])
        self._chatter()
        return World(
            self.split,
            self.seed,
            self.scale,
            self.zone,
            _utc(self.now),
            self.entries,
            self._sessions(),
            {text: tuple(claims) for text, claims in self.claims.items()},
            tuple(self.questions),
        )

    def _chatter(self) -> None:
        """Ordinary work chatter on most days in every entry, around what the questions are about."""
        for day in range(self.days):
            for entry in self.entries:
                places = sorted(entry.reads - self.quiet.get(day, set()))
                if not places or self.rng.random() >= CHATTER_ODDS:
                    continue
                scope = self.rng.choice(places)
                language = self._language(entry.owner)
                for _turn in range(self.rng.randint(*CHATTER_TURNS)):
                    project = self.rng.choice(self.t["projects"])
                    if self.rng.random() < 0.2:
                        output = self.rng.choice(self.t["filler"]["tool"]).format(project=project)
                        self._tool(day, entry, scope, language, project, output, None)
                    else:
                        line = self.rng.choice(self.t["filler"][language]["user"]).format(project=project)
                        self._say(day, entry, scope, language, line)

    def _sessions(self) -> tuple[Message, ...]:
        """The turns of each day, entry and scope in sessions of a few turns, at times of that day, in time order."""
        grouped: dict[tuple[int, str, str], list[_Turn]] = {}
        for turn in self.turns:
            grouped.setdefault((turn.day, turn.entry, turn.scope), []).append(turn)
        messages = []
        for (day, entry, scope), turns in sorted(grouped.items()):
            self.rng.shuffle(turns)
            moment = datetime.combine(self.first_day + timedelta(days=day), datetime.min.time(), self.zone)
            for number, start in enumerate(range(0, len(turns), SESSION_TURNS)):
                session = f"{entry}:{(self.first_day + timedelta(days=day)).isoformat()}:{scope}:{number + 1}"
                opened = moment + timedelta(hours=self.rng.randint(8, 21), minutes=self.rng.randrange(0, 60, 5))
                for place, turn in enumerate(turns[start : start + SESSION_TURNS]):
                    for step, (key, origin, text) in enumerate(turn.messages):
                        at = opened + timedelta(minutes=3 * place, seconds=40 * step)
                        messages.append(Message(key, entry, scope, session, origin, text, _utc(at)))
        return tuple(sorted(messages, key=lambda message: (message.at, message.key)))


def careful(form: str, text: str, claim: Claim) -> tuple[str, Claim]:
    """A sentence of a template ``form`` as it is said, and the claim a careful extractor proposes for it.

    The contract keeps a sentence's negation in what it quotes, so a denial's template marks with ``[...]`` the span
    that holds the negation and the value, and that span is the claim's value.
    """
    if form == "negated":
        if text.count("[") != 1 or text.count("]") != 1:
            raise ValueError(f"a denial template marks the span it quotes with [...]: {text}")
        head, marked = text.split("[")
        span, tail = marked.split("]")
        return head + span + tail, replace(claim, value=span)
    return text, replace(claim, statement_kind=STATEMENT_KINDS.get(form, "assertion"))


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_day(instant: str, zone: timezone) -> date:
    """The world's calendar day of a UTC instant."""
    return datetime.fromisoformat(instant.replace("Z", "+00:00")).astimezone(zone).date()
