"""Bind ``scope_recall`` to this checkout in every process the gate starts.

``scripts/check.py`` puts ``tests/`` first on PYTHONPATH, so Python imports
this file at interpreter start-up, in pytest itself and in every child it
spawns without ``-I``.  Without it a child resolves ``scope_recall`` through
whatever distribution the interpreter has installed -- on a developer venv
that was an editable install of an older, retired checkout -- and a test that
drives a subprocess quietly exercises code that is not under test.  ``-I``
children are unaffected on purpose: they exist to probe the installed wheel.
"""
import sys
import types
from pathlib import Path

if "scope_recall" not in sys.modules:
    _package = types.ModuleType("scope_recall")
    _package.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules["scope_recall"] = _package
