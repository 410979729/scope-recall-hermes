"""The secret scan treats an escaped line break as the boundary it is.

Once a source is serialised into a model request its line breaks become the
two characters ``\\n``, which are not whitespace, so an assignment pattern's
value ran on into the next line.  A document template with an empty
credential slot ("AppSecret:" and nothing after it) was clean as stored and
refused as ``sensitive_request`` in every request that carried it: 369
candidate evaluations on one instance, none of which held a secret.
"""
import json

import pytest

from scope_recall.core.capture_filters import redact_secret_like_text
from scope_recall.core.secret_patterns import contains_secret_like_text, secret_scan_shadow


def _zh(*codes):
    """Chinese text from its code points, so that this file stays ASCII."""
    return "".join(chr(code) for code in codes)


IS = chr(0x662F)  # "is", the separator of a Chinese sentence


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
    'set API_KEY=%API_KEY% before the run',
    '{"password": null, "token": ""}',
    "password: ********",
    "token: xxxx",
    "require_password: true",
    "password: see the vault",
    "password: [REDACTED_SECRET]",
    # Code and prose the final review of 2026-09-28 found still refused.
    'password = input("Password: ")',
    'token := os.Getenv("TOKEN")',
    "if token == nil {",
    ".then(token => save(token))",
    "password: Yup.string().required()",
    "password: { type: String, required: true }",
    '"credentials": {',
    "the token is sent in the header",
    "token" + chr(0x662F) + chr(0x4EC0) + chr(0x4E48) + chr(0x610F) + chr(0x601D),   # token + "is what meaning"
    # rc7 let these through and 3.4.0rc8 refused them again (the rc8 review of 2026-09-28).
    'password = data["password"]',
    "password = hash_password(raw)",
    "private_key = load_pem_private_key(data, password=None)",
    "credentials: dict[str, str] = {}",
    "The password is too short",
    "the token is only valid once",
    # Refused by every version: code that names or passes a credential, prose that describes one.
    "self.password = password",
    "this.token = token;",
    "login(username=username, password=password)",
    "api_key=api_key,",
    "fn login(user: &str, password: &str) -> bool {",
    "const token = await getToken();",
    'let token = std::env::var("TOKEN")?;',
    "export TOKEN=$(cat token.txt)",
    "SET password = $1",
    'db.query("UPDATE users SET password = $1 WHERE id = $2", [hash, id])',
    "password: !vault |",
    "The token is JWT-encoded.",
    "token" + IS + _zh(0x7528, 0x6765, 0x8BA4, 0x8BC1, 0x7684),  # token is used to authenticate
    "access_token " + IS + " " + _zh(0x4E00, 0x4E2A) + " JWT" + _zh(0xFF0C, 0x6709, 0x6548, 0x671F) + " 2 "
    + _zh(0x5C0F, 0x65F6),  # access_token is a JWT, valid for 2 hours
    "API key" + IS + _zh(0x5728, 0x63A7, 0x5236, 0x53F0, 0x751F, 0x6210, 0x7684),  # API key is made in the console
    "token" + IS + _zh(0x5426, 0x8FC7, 0x671F),  # token: expired or not
    "token = token or default_token",
]

#: Code 3.4.0rc8 let through, which the whole-value rules keep letting through: rc8 passed each of them because
#: it looked only at how the value began, and each came back refused while this screen was written.
ORDINARY_KEPT = [
    'token = getpass.getpass("Password: ")',
    'api_key = input("Enter api_key: ")',
    "password = secrets.token_urlsafe(32)",
    "password = base64.b64encode(raw).decode()",
    'password = kwargs.pop("password", None)',
    "password: z.string().min(8)",
    'api_key: Yup.string().required("API_KEY is required"),',
    'token = request.headers.get("Authorization", "").removeprefix("Bearer ")',
    "token = jwt.encode(\n    payload,\n    key,\n    algorithm=\"HS256\",\n)",
    'password = (\n    os.environ["DB_PASSWORD"]\n)',
    "password: { type: String, required: true, minlength: 8 }",
    "if token == expected_token:",
    "password: None, token: None",
    "api_key: <your-api-key> from the dashboard",
    "$config = ['password' => $password];",
    "token" + IS + _zh(0x4EC0, 0x4E48, 0xFF0C, 0x600E, 0x4E48, 0x83B7, 0x53D6),  # token is what, how to get it
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
    # Shapes the first narrowing let through (the core review of 2026-09-28): a password can be any word in any
    # script, so only a placeholder's exact shape is exempt.
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
    # An exemption that stopped before the value's end let the rest through (the final review): a tag, a word
    # with more after it, a dotted prefix, a quoted passphrase, an adverb after "is", a capital.
    "Your temporary password: <b>Xk9#mP2q</b>",
    "password: Changed!2024",
    "password: none!2024",
    "password: My.Secret.Pass#99",
    "password: letmein(2024)",
    'password = "wrong horse battery staple"',
    "the wifi password is now Sunflower2024",
    "the password is Strong!2024",
    "a" * 100 + "_token = abcdef1234567890",
    # Found by running a generated corpus through the 3.3.0 screen and this one: a placeholder's shape with digits
    # in it, a dotted name that is no code's, a null with more after it.
    "password: <hunter2>",
    "password: {hunter2}",
    "password: my.pass.word",
    "api_key: null!",
    # The rc8 review of 2026-09-28: each exemption looked at how the value began, never at the whole of it.
    "password: !@#$%^&*",
    'password = "!@#$%^&*"',
    'password: "!! my secret phrase !!"',
    "password: Mr.Smith(1985)",
    "password: J.Doe[2020]",
    "password: john.doe(42)",
    "password: This.Is.Sparta",
    "password: State.Of.Mind",
    "password: love()you",
    "password: xXxXxXxX",
    "password: -_-_-_-",
    "password: <correct horse battery staple>",
    "WiFi password" + IS + _zh(0x4E0D, 0x662F) + "Xk9#mP2q",  # WiFi password is-it Xk9#mP2q
    "token" + IS + _zh(0x4EC0, 0x4E48) + "Xk9#mP2q",  # token is-what Xk9#mP2q
    "password" + IS + _zh(0x5FC5, 0x987B) + "Xk9#mP2q",  # password must-be Xk9#mP2q
    "Cookie:\nsession=Xk9#mP2q",
    "cookie:\n  sessionid=Xk9#mP2q; csrftoken=abc",
    "-----BEGIN" + "  PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END" + "  PRIVATE KEY-----",
    # A typed declaration assigns its literal; a fallback or a default is the value too.
    'DB_PASSWORD: str = "Xk9#mP2q"',
    'password: Optional[str] = "Xk9#mP2q"',
    'private readonly password: string = "Xk9#mP2q";',
    'val password: String = "Xk9#mP2q"',
    'password = os.environ.get("DB_PASSWORD", "Xk9#mP2q")',
    'const password = process.env.DB_PASSWORD || "Xk9#mP2q";',
    'password = settings.DB_PASSWORD or "Xk9#mP2q"',
    'api_key = config.get("api_key", "Xk9#mP2q")',
    'password: env.str("DB_PASSWORD", default="Xk9#mP2q")',
    # Prose: a word after "is" that does not end its clause gives the value.
    "my password is this: Xk9#mP2q",
    "The password is the following: Xk9#mP2q",
    "the password is correct horse battery staple",
    "the wifi password is here: Xk9#mP2q",
    "the api key is in Xk9#mP2q",
    # More that rc8 let through, found by probing this screen.
    "**Password:** hunter2",
    "password: none, sunshine",
    "password: *** correct horse battery staple",
    "the password is set to hunter",
    "password: reset to Sunflower2024",
    'if token == "Xk9#mP2q":',
    "'password' => 'Xk9#mP2q'",
    'password => "hunter2"',
    'password = os.getenv("token: sunshine")',
    'password = str("Xk9#mP2q")',
    'password = "" + "sunshine"',
    "password: { value: sunshine }",
    "password: ${DB_PASSWORD:-postgres}",
    "export TOKEN=$(base64 -d c3Vuc2hpbmU=)",
    'password = (\n    "Xk9#mP2q"\n)',
    "password: |\n  Xk9#mP2q",
    "password" + IS + _zh(0x4E0D, 0x662F) + "-_-_-_-",  # password is-it -_-_-_-
    # Keys 3.3.0 did not know either.
    "SECRET_KEY=Xk9#mP2q",
    "secret_key: Xk9#mP2q",
    "JWT_SECRET_KEY='Xk9#mP2q'",
    "app.config['SECRET_KEY'] = 'Xk9#mP2q'",
    'accessToken: "Xk9#mP2q"',
    "const authToken = 'Xk9#mP2q';",
]


@pytest.mark.parametrize("text", ORDINARY + ORDINARY_KEPT)
def test_ordinary_text_after_a_credential_word_is_not_a_secret(text):
    assert not contains_secret_like_text(text), text
    assert redact_secret_like_text(text) == text, text


@pytest.mark.parametrize("text", REAL)
def test_a_credential_after_the_same_words_is_still_caught_and_redacted(text):
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
    for text in (token, f"TELEGRAM_BOT_TOKEN={token}", f'{{"token": "{token}"}}', f"token: {token} in the log",
                 f"https://api.telegram.org/bot{token}/getMe", f"https://api.telegram.org/BOT{token}/getMe"):
        assert contains_secret_like_text(text), text



def test_a_long_hyphenated_line_scans_in_linear_time():
    """The name before ``token`` was unbounded: a 60,000-character kebab-case line was tried from every hyphen to
    its end, 18 s in one scan, inside a capture or a model request."""
    import time

    line = "a-" * 30000
    started = time.monotonic()
    assert not contains_secret_like_text(line)
    assert time.monotonic() - started < 2.0


def test_adversarial_text_scans_in_linear_time():
    """Four patterns backtracked quadratically: ``^\\s*`` over blank lines (4.3 s for 20 kB), a backslash run with
    no break after it (1.4 s), repeated "credential" (1.0 s), and repeated PEM BEGIN markers (14 s for 100 kB),
    inside a capture or a model request."""
    import time

    for text in ("\n" * 20000, chr(92) * 20000, "credential" * 2000, "-----BEGIN " * 9000):
        started = time.monotonic()
        contains_secret_like_text(text)
        redact_secret_like_text(text)
        assert time.monotonic() - started < 1.5, (text[:20], time.monotonic() - started)


def test_two_secrets_side_by_side_are_both_redacted():
    """One pattern at a time, the password's match swallowed the token's key and left its value."""
    for text in ('{"password":"x","api_token": "abc123def"}', "secret:x;auth_token = abc123def"):
        redacted = redact_secret_like_text(text)
        assert "abc123def" not in redacted, redacted
        assert not contains_secret_like_text(redacted), redacted


def _values_after_keys():
    prose = "The deploy finished and the report is attached below. " * 300
    texts = [
        "password: " + "." * 20000 + "a",
        "a-" * 63 + "token: " + "." * 4000 + "! " + prose,
    ]
    for unit in (".", "-", "_", "x", "*", "!@#$%^&*()-_=+[]{};:,.<>/?|~"):
        for key in ("password: ", "token=", "api_key = ", "the secret is ", "password" + IS):
            texts.append(key + unit * (20000 // len(unit) + 1) + "a")
            texts.append(key + unit * (4000 // len(unit) + 1) + "! " + prose)
        texts.append("a-" * 63 + "token: " + unit * (4000 // len(unit) + 1) + "! " + prose)
        texts.append("a-" * 63 + "token: " + unit * (20000 // len(unit) + 1))
    texts += [
        "password: none " * 1400,
        "password=" * 2300,
        "if token == expected_token: " * 750,
        'api_key = os.environ.get("API_KEY", None) ' * 500,
        "password: { type: String, required: true } " * 480,
        "the password is required for every login. " * 500,
        "password: " + "(" * 20000,
        "password: " + "f(" * 10000,
        "password: <" + "your " * 4000 + ">",
        "password: " + "Optional[" * 2300,
        "password: |\n  none\n" * 1200,
    ]
    return texts


def test_a_value_after_a_key_scans_in_linear_time():
    """3.4.0rc8 decided in a lookahead whether a value was exempt, and its end repeated a class of punctuation that
    its mask matched too: ``password: `` and 20,000 dots took 2.9 s, and 4,000 dots after 64 starts of a token key,
    then 16 kB of prose, 7.5 s to scan and 14.9 s to redact.  Every captured message and model request is scanned, up
    to a million characters, and ``re`` holds the GIL.  A value is now judged in Python, a bounded stretch of it."""
    import time

    for text in _values_after_keys():
        assert len(text) >= 20000
        started = time.monotonic()
        contains_secret_like_text(text)
        scanned = time.monotonic() - started
        started = time.monotonic()
        redact_secret_like_text(text)
        redacted = time.monotonic() - started
        assert scanned < 0.3 and redacted < 0.3, (text[:40], scanned, redacted)


def test_json_held_by_a_message_is_read_through_its_escapes():
    """A tool's output that is JSON writes a quote as a backslash and a quote: the key's closing quote hid
    ``{"password": "P@ssw0rd!"}`` from every version, and code quoted that way read as a value."""
    for text in ('{"password": "P@ssw0rd!"}', '"api_key": "Xk9#mP2q"', 'password = "Xk9#mP2q"'):
        assert contains_secret_like_text(json.dumps({"content": text})), text
    for text in ('{"password": null, "token": ""}', 'api_key = os.environ["API_KEY"]', 'password = input("Password: ")',
                 '"credentials": {', "token" + IS + _zh(0x4EC0, 0x4E48, 0x610F, 0x601D)):
        assert not contains_secret_like_text(json.dumps({"content": text}, ensure_ascii=False)), text


def test_redaction_covers_the_whole_value():
    """rc8's span ended at the first space, so a quoted passphrase or a value with spaces after ``:`` was redacted in
    part (``password = "correct horse battery staple"`` kept "horse battery staple"), and a literal on the line below
    ``decrypt(`` was kept whole."""
    for text, kept in (
        ('password = "correct horse battery staple"', "horse"),
        ('{"password": "my dog has fleas 42"}', "fleas"),
        ("token: abc123 def456", "def456"),
        ("password: correct horse battery staple", "staple"),
        ('api_key = decrypt(\n    "Xk9#mP2q"\n)', "Xk9"),
    ):
        redacted = redact_secret_like_text(text)
        assert "[REDACTED_SECRET]" in redacted and kept not in redacted, redacted
