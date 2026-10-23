"""The secret scan treats an escaped line break as the boundary it is.

Once a source is serialised into a model request its line breaks become the
two characters ``\\n``, which are not whitespace, so an assignment pattern's
value ran on into the next line.  A document template with an empty
credential slot ("AppSecret:" and nothing after it) was clean as stored and
refused as ``sensitive_request`` in every request that carried it: 369
candidate evaluations on one instance, none of which held a secret.
"""

import json

from scope_recall.core.capture_filters import redact_secret_like_text
from scope_recall.core.secret_patterns import (
    contains_secret_like_text,
    secret_like_text_starts_before,
    secret_scan_shadow,
)
from v11_support import slowdown


def test_an_escaped_line_break_ends_a_value_like_a_real_one():
    document = "AppId: 1001\r\nAppSecret: \r\nwhat follows is prose about the interface"
    assert not contains_secret_like_text(document)
    serialised = json.dumps({"content": document})
    assert not contains_secret_like_text(serialised)
    assert len(secret_scan_shadow(serialised)) == len(serialised)


def test_a_real_assignment_is_still_caught_after_serialisation():
    document = "AppSecret: 9f8e7d6c5b4a3f2e\r\nnext line"
    assert contains_secret_like_text(document)
    assert contains_secret_like_text(json.dumps({"content": document}))


def test_a_break_escaped_twice_does_not_leave_a_backslash_as_the_value():
    """A tool output that is JSON holding JSON writes a line break as backslash, backslash, n."""
    once = "AppSecret:" + chr(92) + "n" + "next line of the template"
    twice = "AppSecret:" + chr(92) * 2 + "n" + "next line of the template"
    thrice = "AppSecret:" + chr(92) * 3 + "n" + "next line of the template"
    for text in (once, twice, thrice):
        assert not contains_secret_like_text(text), text
        assert len(secret_scan_shadow(text)) == len(text), "positions in the shadow stay valid"


def test_an_escaped_tab_still_separates_a_key_from_its_secret():
    """A tab is spacing, not a line end: the value after it is still the key's value."""
    for slashes in (1, 2):
        assert contains_secret_like_text("password:" + chr(92) * slashes + "t" + "hunter2-not-a-placeholder")


#: Ordinary text that follows a credential word.  Every one of these was refused as a secret: the message was
#: never stored, and a model request carrying it was refused as ``sensitive_request``.
ORDINARY = [
    "password: reset it from the login page",
    "the password is required for every login",
    "the secret is out",
    "token is expired, sign in again",
    "def login(user: str, password: str) -> bool:",
    "password: Optional[str] = None",
    'api_key = os.environ["API_KEY"]',
    "password = getpass.getpass()",
    "password = settings.DB_PASSWORD",
    "api_key: <your-api-key>",
    "API_KEY=${API_KEY}",
    "set API_KEY=%API_KEY% before the run",
    '{"password": null, "token": ""}',
    "password: ********",
    "token: xxxx",
    "require_password: true",
    "password: see the vault",
    "password: [REDACTED_SECRET]",
    # More code and prose that must not be refused.
    'password = input("Password: ")',
    'token := os.Getenv("TOKEN")',
    "if token == nil {",
    ".then(token => save(token))",
    "password: Yup.string().required()",
    "password: { type: String, required: true }",
    '"credentials": {',
    "the token is sent in the header",
    "token" + chr(0x662F) + chr(0x4EC0) + chr(0x4E48) + chr(0x610F) + chr(0x601D),  # token + "is what meaning"
]

#: Values that are credentials, in the same shapes.
REAL = [
    "password: hunter2",
    "password is hunter2",
    "the password is Tr0ub4dor&3",
    "wifi password: sunshine",
    "password=supersecret",
    '{"password": "P@ssw0rd!"}',
    "api_key: TEST_VALUE_ONLY",
    "secret: 9f8e7d6c5b4a3f2e",
    "token: 8f14e45fceea167a5a36dedd4bea2543",
    "password = hunter2.backup9",
    # Shapes a loose exemption would let through: a password can be any word in any script, so only a placeholder's
    # exact shape is exempt.
    "password: $unshine2024",
    '"password": "$ecret99!"',
    "password: (Summer2024)",
    "password: [hunter2]",
    "password: $2b$12$abcdefghijklmnopqrstuv",
    "password: Hunter2(backup)",
    "api_key=abc123def456[prod]",
    "my password is iloveyou",
    "the wifi password is sunflower",
    "the api key is abcdefghijklmnop",
    "password: correct horse battery staple",
    "password" + chr(0x662F) + chr(0x5929) + chr(0x738B) + chr(0x76D6) + chr(0x5730) + chr(0x864E),
    "password: " + "".join(chr(code) for code in (0x43F, 0x430, 0x440, 0x43E, 0x43B, 0x44C)),
    # An exemption that stops before the value's end lets the rest through: a tag, a word with more after it, a
    # dotted prefix, a quoted passphrase, an adverb after "is", a capital.
    "Your temporary password: <b>Xk9#mP2q</b>",
    "password: Changed!2024",
    "password: none!2024",
    "password: My.Secret.Pass#99",
    "password: letmein(2024)",
    'password = "wrong horse battery staple"',
    "the wifi password is now Sunflower2024",
    "the password is Strong!2024",
    "a" * 100 + "_token = abcdef1234567890",
    # A placeholder's shape with digits in it, a dotted name that is no code's, a null with more after it.
    "password: <hunter2>",
    "password: {hunter2}",
    "password: my.pass.word",
    "api_key: null!",
]


def test_ordinary_text_after_a_credential_word_is_not_a_secret():
    for text in ORDINARY:
        assert not contains_secret_like_text(text), text
        assert redact_secret_like_text(text) == text, text


def test_a_credential_after_the_same_words_is_still_caught_and_redacted():
    for text in REAL:
        assert contains_secret_like_text(text), text
        assert "[REDACTED_SECRET]" in redact_secret_like_text(text), text


def _token():
    # Built here so that nothing token-shaped is written into the repository.
    return "1234567890" + ":" + "AAE" + "x7Q" * 10 + "k2"


def test_a_digit_run_glued_to_an_id_is_not_a_telegram_token():
    """A Codex source key: a hex installation id that happens to end in eight digits, then a session UUID.
    About one installation in 45 has such an id, and every one of its captures was refused as a secret."""
    key = "codex:codex-install:51777e7e4a0083087baf00de02721985:92051813-c57b-4903-badb-22a200155f71:user:turn-1@1"
    assert not contains_secret_like_text(key)
    assert not contains_secret_like_text("commit 4a0083087baf00de02721985:92051813-c57b-4903-badb-22a200155f71")


def test_a_telegram_token_is_still_caught_where_one_appears():
    token = _token()
    for text in (
        token,
        f"TELEGRAM_BOT_TOKEN={token}",
        f'{{"token": "{token}"}}',
        f"token: {token} in the log",
        f"https://api.telegram.org/bot{token}/getMe",
        f"https://api.telegram.org/BOT{token}/getMe",
    ):
        assert contains_secret_like_text(text), text


def _scanned(text):
    contains_secret_like_text(text)
    redact_secret_like_text(text)


def test_a_long_hyphenated_line_scans_in_linear_time():
    """The name before ``token`` is bounded: unbounded, a kebab-case line would be tried from every hyphen to its end,
    seconds for 60,000 characters in one scan, inside a capture or a model request.  Four times the line takes about
    four times as long (``slowdown``)."""
    line = "a-" * 30_000
    assert not contains_secret_like_text(line)
    assert slowdown(contains_secret_like_text, "a-" * 7_500, line) < 8


def test_adversarial_text_scans_in_linear_time():
    """No pattern backtracks over a long run: ``^\\s*`` over blank lines, a backslash run with no break after it,
    repeated "credential" and repeated PEM BEGIN markers each take seconds for 20-100 kB in a pattern that does, inside
    a capture or a model request.  Four times the run takes about four times as long (``slowdown``)."""
    for run, count in (("\n", 20_000), (chr(92), 20_000), ("credential", 2_000), ("-----BEGIN ", 9_000)):
        assert slowdown(_scanned, run * (count // 4), run * count) < 8, run


def test_a_run_of_punctuation_after_a_key_word_scans_in_linear_time():
    """A value's end is not greedy over dots, and neither is a mask: given back one at a time, a run of dots after a
    key word takes seconds for 20 kB, and longer with 64 key starts before it.  Four times the run takes about four
    times as long (``slowdown``)."""
    for text in (
        lambda count: "password: " + "." * count + "a",
        lambda count: "a-" * 63 + "token: " + "." * (count // 5) + "! " + "the quick brown fox. " * (count // 25),
        lambda count: "token: " + "*.-_x" * (count // 5) + "a",
        lambda count: "secret = " + "!" * count + "a",
    ):
        assert slowdown(_scanned, text(5_000), text(20_000)) < 8, text(20)


def test_two_secrets_side_by_side_are_both_redacted():
    """One pattern at a time, the password's match swallowed the token's key and left its value."""
    for text in ('{"password":"x","api_token": "abc123def"}', "secret:x;auth_token = abc123def"):
        redacted = redact_secret_like_text(text)
        assert "abc123def" not in redacted, redacted
        assert not contains_secret_like_text(redacted), redacted


def test_a_run_counts_by_where_it_starts_and_is_judged_with_the_rest_of_its_line():
    """For a caller that hands on only a text's first characters, as the embedding input bound does."""
    exempt = "TEST credential_env: null"
    cut = exempt.index("ll")
    assert contains_secret_like_text(exempt[:cut]), "alone, the start of null reads as a password"
    assert not secret_like_text_starts_before(exempt, cut)
    long = "TEST credential_env: null" + "." * 1000 + "X\nTEST next line"
    assert secret_like_text_starts_before(long, long.index("ll")), "a value is read to its end, however long"
    key = "TEST sk-proj-" + "Ab1" * 10
    assert not contains_secret_like_text(key[:20]), "too few of the key's characters for its pattern"
    assert secret_like_text_starts_before(key, 20)
    later = "TEST line\npassword: Xk9#mP2q"
    assert not secret_like_text_starts_before(later, later.index("password"))
    assert secret_like_text_starts_before(later, later.index("password") + 1)
    spaced = "TEST Authorization: Bearer\n" + "Ab1" * 10
    assert contains_secret_like_text(spaced)
    assert not secret_like_text_starts_before(spaced, spaced.index("\n")), "the token on the next line is not sent"
    hidden = "\u200b" * 3 + "password: Xk9#mP2q"
    assert not secret_like_text_starts_before(hidden, 3), "the characters the scan drops still count as characters"
    assert secret_like_text_starts_before(hidden, 4)
