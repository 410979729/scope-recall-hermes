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
