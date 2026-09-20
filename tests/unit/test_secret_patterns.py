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

