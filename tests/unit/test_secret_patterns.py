"""The secret scan treats an escaped line break as the boundary it is.

Once a source is serialised into a model request its line breaks become the
two characters ``\\n``, which are not whitespace, so an assignment pattern's
value ran on into the next line.  A document template with an empty
credential slot ("AppSecret:" and nothing after it) was clean as stored and
refused as ``sensitive_request`` in every request that carried it: 369
candidate evaluations on one instance, none of which held a secret.
"""
import json

from scope_recall.core.secret_patterns import contains_secret_like_text, secret_scan_shadow


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

