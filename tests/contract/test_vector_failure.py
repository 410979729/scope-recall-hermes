"""An optional channel that fails must say which fault, not which family."""
import ast
import pathlib

import pytest

from scope_recall.core.vector_failure import (
    NATIVE_VECTOR_FAULTS, native_vector_fault, vector_failure_label,
)

# The native helper's own vocabulary. Every one of these reached the operator as
# the single word "RuntimeError" before this module existed.
NATIVE_FAULTS = [
    ("native vector helper lock timeout; SQLite truth is intact and the active helper was not interrupted",
     "RuntimeError:helper_lock_timeout"),
    ("native vector helper request deadline exhausted", "RuntimeError:helper_request_deadline"),
    ("native vector helper teardown failed; SQLite truth is intact", "RuntimeError:helper_teardown_failed"),
    ("native vector helper teardown is still pending; SQLite truth is intact",
     "RuntimeError:helper_teardown_pending"),
    ("native vector worker exited or returned an invalid frame", "RuntimeError:worker_exited_mid_frame"),
    ("native vector worker is closed; reopen the vector runtime explicitly", "RuntimeError:worker_closed"),
    ("native vector worker is not running", "RuntimeError:worker_not_running"),
    ("native vector worker unresponsive; SQLite truth is intact and unacknowledged outbox work remains pending",
     "RuntimeError:worker_unresponsive"),
    ("native vector worker failed; SQLite truth is intact", "RuntimeError:worker_failed"),
    ("native vector fence handshake send failed", "RuntimeError:fence_send_failed"),
    ("native vector fence final response mismatch", "RuntimeError:fence_mismatch"),
    ("native vector fence guard request mismatch", "RuntimeError:fence_mismatch"),
    ("native vector fence deadline exhausted before guard", "RuntimeError:fence_deadline"),
    ("native vector fence failed; physical outcome is uncertain", "RuntimeError:fence_failed"),
    ("native vector purge deadline exhausted", "RuntimeError:purge_deadline"),
    ("vector table is not open", "RuntimeError:table_not_open"),
    ("LanceDB cannot prove an indexed id lookup", "RuntimeError:lance_no_indexed_lookup"),
    ("LanceDB table does not support row iteration", "RuntimeError:lance_no_row_iteration"),
    ("lancedb/pyarrow is not installed", "RuntimeError:lance_not_installed"),
    ("pyarrow is not installed", "RuntimeError:lance_not_installed"),
]


@pytest.mark.parametrize("message,expected", NATIVE_FAULTS)
def test_each_native_fault_gets_its_own_name(message, expected):
    assert vector_failure_label(RuntimeError(message)) == expected


def test_the_faults_an_operator_must_tell_apart_are_told_apart():
    """A helper that is busy, one that is gone, and one that never started need
    three different answers; before this they had one label between them."""
    labels = {
        vector_failure_label(RuntimeError("native vector helper lock timeout; x")),
        vector_failure_label(RuntimeError("native vector worker is closed; x")),
        vector_failure_label(RuntimeError("native vector worker is not running")),
        vector_failure_label(RuntimeError("native vector worker exited or returned an invalid frame")),
        vector_failure_label(RuntimeError("native vector helper request deadline exhausted")),
    }
    assert len(labels) == 5


def test_a_fixed_vocabulary_still_wins_over_a_message():
    class Auxiliary(Exception):
        error_type = "timeout"

    exc = Auxiliary("native vector helper lock timeout; must not be used")
    assert vector_failure_label(exc) == "Auxiliary:timeout"


def test_an_unrecognised_message_invents_no_category():
    """The first version derived the token from the text, and a test's own
    wording promptly became a category: a gap is grouped on, so its values must
    be enumerable."""
    assert vector_failure_label(TimeoutError("synthetic embedding request used its entire allowance")) \
        == "TimeoutError"
    assert vector_failure_label(TypeError("internal vector implementation failure")) == "TypeError"
    assert vector_failure_label(RuntimeError("boom")) == "RuntimeError"
    assert vector_failure_label(RuntimeError()) == "RuntimeError"


def test_a_message_never_reaches_the_gap_verbatim():
    """A gap list is reported; a message is the one place a path or a credential
    can ride along."""
    exc = RuntimeError("connect failed for sk-ant-api03-AAAABBBBCCCC at C:/Users/someone/secret")
    assert vector_failure_label(exc) == "RuntimeError"
    tokens = {token for _, token in NATIVE_VECTOR_FAULTS}
    for message, expected in NATIVE_FAULTS:
        assert expected.split(":", 1)[1] in tokens


def test_an_exception_whose_str_raises_is_survivable():
    class Hostile(Exception):
        def __str__(self):
            raise ValueError("no")

    assert vector_failure_label(Hostile()) == "Hostile"


def _runtime_error_messages(module_name: str) -> list[str]:
    import importlib
    import inspect

    module = importlib.import_module(module_name)
    path = pathlib.Path(inspect.getsourcefile(module))
    messages = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "RuntimeError":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    messages.append(arg.value)
    return messages


@pytest.mark.parametrize("module_name", ["scope_recall.vector.process_store", "scope_recall.vector.store"])
def test_the_vocabulary_cannot_silently_fall_behind_the_code(module_name):
    """A new RuntimeError in the vector path that nothing here names would arrive
    as a bare class again, which is the fault this module exists to remove."""
    unnamed = [m for m in _runtime_error_messages(module_name) if not native_vector_fault(m)]
    assert unnamed == [], (
        "these vector RuntimeErrors have no token in NATIVE_VECTOR_FAULTS: %r" % unnamed)


def test_recall_uses_this_and_not_its_own_copy():
    """Two spellings of the same idea drift; this one already did."""
    import inspect

    from scope_recall.core import recall

    source = inspect.getsource(recall)
    assert "vector_failure_label(exc)" in source
    assert "label = type(exc).__name__" not in source


# --- the other half: a remote failure already knows its own name --------------

def test_a_remote_failure_keeps_the_name_the_helper_gave_it():
    """``_lance_worker`` reports ``error_type``; the parent read it, handled two
    classes, and dropped the rest -- so every other subprocess failure arrived as
    the bare word RuntimeError with the diagnosis stranded in a discarded
    message."""
    from scope_recall.vector.process_store import _remote_failure

    assert vector_failure_label(_remote_failure("ValueError", "table missing")) \
        == "RuntimeError:ValueError"
    assert vector_failure_label(_remote_failure("OSError", "pipe closed")) == "RuntimeError:OSError"


@pytest.mark.parametrize("bogus", [None, 123, "", "bad type!", "A" * 80, "sk-ant-api03-AAAA!"])
def test_a_malformed_remote_type_is_refused_rather_than_reported(bogus):
    from scope_recall.vector.process_store import _remote_failure

    assert vector_failure_label(_remote_failure(bogus, "something went wrong")) == "RuntimeError"


def test_the_one_remote_fallback_carries_the_type():
    """Plain and fenced responses are judged by one function.  Two copies, one
    fixed and one left behind, is how this drifted before."""
    import inspect

    from scope_recall.vector import process_store as lance_process_store

    source = inspect.getsource(lance_process_store)
    assert source.count("raise _remote_failure(error_type, message)") == 1
    # The bare fallback must be gone, or the fix is half applied.
    assert "raise RuntimeError(message)" not in source
