"""Running-code records: what a live process loaded vs what is on disk.

Covers ``runtime/running_code.py`` and ``runtime/process_probe.py``.  The
property under test is asymmetric on purpose: a stale process must always be
caught, and a current process must never be accused, because a health check
that reports a healthy instance as degraded stops being read at all.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from scope_recall._version import __version__
from scope_recall.maintenance import doctor
from scope_recall.runtime import running_code as rc
from scope_recall.runtime.process_probe import is_same_process, probe_process
from scope_recall.runtime.model_budget import REFUSAL_WINDOW_SECONDS, provider_refusals
from scope_recall.runtime.running_code import package_modified_at
from test_autonomous_admission import app_at


def _write_record(directory, pid, **overrides):
    """Place a record directly, so a test can choose pid and timestamps."""
    payload = {
        "schema": rc.RECORD_SCHEMA,
        "pid": pid,
        "version": __version__,
        "first_record_at": datetime.now(timezone.utc).isoformat(),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "package_path": "/does/not/matter",
        "start_token": None,
        "host_adapter": "hermes",
        "installation_id": "TEST-install",
        "python_executable": None,
    }
    payload.update(overrides)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{pid}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _dead_pid() -> int:
    """A pid no process can hold.  Windows and Linux both cap below this."""
    return 0xFFFFFFF0


def test_probe_reports_this_process_and_never_signals_a_dead_one():
    state = probe_process(os.getpid())
    assert state.running is True
    assert state.pid == os.getpid()
    # A dead pid must answer "not running" rather than raise, and must never
    # be signalled: on Windows os.kill(pid, 0) terminates the target, so the
    # probe has to take the query-handle path instead.
    assert probe_process(_dead_pid()).running is False
    assert is_same_process(_dead_pid(), None) is False


def test_probe_rejects_values_that_are_not_process_ids():
    for bad in (0, -1, True, "123", 1.0):
        with pytest.raises(ValueError):
            probe_process(bad)  # type: ignore[arg-type]


def test_record_states_the_version_this_process_loaded(tmp_path):
    path = rc.record_running_code(tmp_path, host_adapter="hermes", installation_id="TEST-install")
    assert path is not None and path.is_file()
    live = rc.live_records(tmp_path)
    assert [record.pid for record in live] == [os.getpid()]
    assert live[0].version == __version__
    assert live[0].host_adapter == "hermes"
    assert live[0].installation_id == "TEST-install"
    # No partial file is left behind by the atomic replace.
    assert list(rc.running_code_dir(tmp_path).glob("*.partial")) == []


def test_dead_process_records_are_retired_when_a_live_one_writes(tmp_path):
    directory = rc.running_code_dir(tmp_path)
    stale_path = _write_record(directory, _dead_pid())
    assert stale_path.is_file()
    rc.record_running_code(tmp_path)
    assert not stale_path.exists()
    assert [record.pid for record in rc.live_records(tmp_path)] == [os.getpid()]


def test_unreadable_record_is_skipped_and_retired_rather_than_raising(tmp_path):
    directory = rc.running_code_dir(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    broken = directory / "999999.json"
    broken.write_text("{ truncated", encoding="utf-8")
    assert rc.live_records(tmp_path) == []
    rc.record_running_code(tmp_path)
    assert not broken.exists()


def test_a_current_process_is_never_reported_stale(tmp_path):
    rc.record_running_code(tmp_path)
    package_path = tmp_path / "package"
    package_path.mkdir()
    (package_path / "module.py").write_text("x = 1", encoding="utf-8")
    _age(package_path / "module.py", minutes=30)
    stale = rc.stale_records(tmp_path, disk_version=__version__, package_path=package_path)
    assert stale == []


def test_version_mismatch_is_reported_for_the_live_process(tmp_path):
    directory = rc.running_code_dir(tmp_path)
    _write_record(directory, os.getpid(), version="3.0.0-old")
    stale = rc.stale_records(tmp_path, disk_version=__version__)
    assert [item["reason"] for item in stale] == ["version_mismatch"]
    assert stale[0]["loaded_version"] == "3.0.0-old"
    assert stale[0]["disk_version"] == __version__


def test_same_version_reinstall_is_reported_by_package_mtime(tmp_path):
    """The ordinary development loop: rebuild, reinstall, same version string."""
    directory = rc.running_code_dir(tmp_path)
    loaded_at = datetime.now(timezone.utc) - timedelta(hours=21)
    _write_record(directory, os.getpid(), first_record_at=loaded_at.isoformat())
    package_path = tmp_path / "package"
    package_path.mkdir()
    (package_path / "module.py").write_text("x = 1", encoding="utf-8")
    stale = rc.stale_records(tmp_path, disk_version=__version__, package_path=package_path)
    assert [item["reason"] for item in stale] == ["package_rewritten_after_load"]
    assert stale[0]["pid"] == os.getpid()


def test_dead_process_records_are_not_reported_stale(tmp_path):
    """Only live processes can be running the wrong code."""
    directory = rc.running_code_dir(tmp_path)
    _write_record(directory, _dead_pid(), version="3.0.0-old")
    assert rc.stale_records(tmp_path, disk_version=__version__) == []


def test_record_write_failure_is_silent(tmp_path, monkeypatch):
    """An instance must still open when the data directory cannot be written."""
    monkeypatch.setattr(rc.Path, "mkdir", _raise_os_error)
    monkeypatch.setattr(rc, "_written", None)
    assert rc.record_running_code(tmp_path / "unwritable") is None


def _doctor_instance(tmp_path, monkeypatch):
    """A doctor pointed at a real store, as the diagnostics tests do it."""
    app, ctx = app_at(tmp_path)
    (ctx.binding.data_directory / "installation.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(doctor, "_load_binding", lambda *a: (ctx.binding, ctx.binding.data_directory))
    monkeypatch.setattr(doctor, "_hermes_data_dir", lambda root: ctx.binding.data_directory)
    return app, ctx


def test_doctor_reports_a_stale_process_and_leaves_the_store_untouched(tmp_path, monkeypatch):
    app, ctx = _doctor_instance(tmp_path, monkeypatch)
    _write_record(rc.running_code_dir(ctx.binding.data_directory), os.getpid(), version="3.0.0-old")
    before = app.storage.path.read_bytes()
    result = doctor.run_doctor(host="hermes", instance_root=ctx.binding.data_directory)
    assert "stale_process" in result.capability_gaps
    stale = result.running_code["stale_processes"]
    assert [item["reason"] for item in stale] == ["version_mismatch"]
    assert stale[0]["pid"] == os.getpid()
    assert app.storage.path.read_bytes() == before


def test_doctor_stays_quiet_when_every_live_process_is_current(tmp_path, monkeypatch):
    _app, ctx = _doctor_instance(tmp_path, monkeypatch)
    _write_record(rc.running_code_dir(ctx.binding.data_directory), os.getpid())
    result = doctor.run_doctor(host="hermes", instance_root=ctx.binding.data_directory)
    assert "stale_process" not in result.capability_gaps
    assert [entry["pid"] for entry in result.running_code["live_processes"]] == [os.getpid()]


def test_doctor_does_not_call_an_empty_list_a_clean_bill_of_health(tmp_path, monkeypatch):
    """A process registers when it opens an instance, not when it imports.

    So "no records" means nothing has registered yet, and must not read as
    "checked every process and all are current".
    """
    _app, ctx = _doctor_instance(tmp_path, monkeypatch)
    result = doctor.run_doctor(host="hermes", instance_root=ctx.binding.data_directory)
    assert {"name": "running_code", "result": "no_records", "detail": "0"} in result.checks
    assert "stale_process" not in result.capability_gaps


def test_doctor_survives_an_unreadable_record_directory(tmp_path, monkeypatch):
    _app, ctx = _doctor_instance(tmp_path, monkeypatch)
    monkeypatch.setattr(doctor, "live_records", _raise_runtime_error)
    result = doctor.run_doctor(host="hermes", instance_root=ctx.binding.data_directory)
    assert "stale_process" not in result.capability_gaps
    assert {"name": "running_code", "result": "unreadable", "detail": "RuntimeError"} in result.checks


def _raise_runtime_error(*_args, **_kwargs):
    raise RuntimeError("unreadable")


def _raise_os_error(*_args, **_kwargs):
    raise OSError("read-only file system")


def _age(path, *, minutes: int) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp()
    os.utime(path, (stamp, stamp))


# --------------------------------------------------------------------------
# A stamp in the future is not evidence
# --------------------------------------------------------------------------

def test_a_future_modification_time_is_ignored(tmp_path):
    """An unpacking tool that mishandles the archive's local-time entries would
    otherwise make every process look stale forever.  On Alpha one extraction
    shifted 31 of 135 files four hours ahead."""
    package = tmp_path / "pkg"
    package.mkdir()
    past, future = time.time() - 600, time.time() + 4 * 3600  # the real Alpha skew
    (package / "a.py").write_text("x", encoding="utf-8")
    (package / "b.py").write_text("x", encoding="utf-8")
    os.utime(package / "a.py", (past, past))
    os.utime(package / "b.py", (future, future))

    stamp = package_modified_at(package)
    assert stamp is not None
    assert datetime.fromisoformat(stamp).timestamp() == pytest.approx(past, abs=2), \
        "the future stamp was believed"


def test_a_package_whose_every_stamp_is_future_reports_nothing(tmp_path):
    """Better to say nothing than to pin the instance at degraded forever."""
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "a.py").write_text("x", encoding="utf-8")
    future = time.time() + 3600
    os.utime(package / "a.py", (future, future))

    assert package_modified_at(package) is None


def test_a_stamp_a_moment_ahead_is_still_believed(tmp_path):
    """A file written now can carry a stamp a hair past ``time.time()``;
    discarding that would throw away the rebuild-and-reinstall case."""
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "a.py").write_text("x", encoding="utf-8")
    just_ahead = time.time() + 2
    os.utime(package / "a.py", (just_ahead, just_ahead))

    assert package_modified_at(package) is not None


def test_an_ordinary_package_still_reports_its_newest_stamp(tmp_path):
    """The narrowing must not disable the check it is protecting."""
    package = tmp_path / "pkg"
    package.mkdir()
    older, newer = time.time() - 900, time.time() - 60
    (package / "a.py").write_text("x", encoding="utf-8")
    (package / "b.py").write_text("x", encoding="utf-8")
    os.utime(package / "a.py", (older, older))
    os.utime(package / "b.py", (newer, newer))

    stamp = package_modified_at(package)
    assert datetime.fromisoformat(stamp).timestamp() == pytest.approx(newer, abs=2)


def test_the_ceiling_can_be_supplied_for_a_deterministic_test(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "a.py").write_text("x", encoding="utf-8")
    stamp_at = time.time() - 100
    os.utime(package / "a.py", (stamp_at, stamp_at))

    from scope_recall.runtime.running_code import FUTURE_TOLERANCE_SECONDS

    beyond = FUTURE_TOLERANCE_SECONDS + 10
    assert package_modified_at(package, now=stamp_at - beyond) is None
    assert package_modified_at(package, now=stamp_at + 10) is not None


# --------------------------------------------------------------------------
# Usage is reported in units everyone shares; money is not one of them
# --------------------------------------------------------------------------

def _headroom(tmp_path, *, calls=3, charge=999999, inputs=1200, outputs=340):
    import sqlite3
    from types import SimpleNamespace

    ledger = tmp_path / "auxiliary-budget.sqlite3"
    with sqlite3.connect(ledger) as conn:
        conn.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, charge_micro_usd INTEGER,"
                     " reserved_input INTEGER, actual_input INTEGER,"
                     " reserved_output INTEGER, actual_output INTEGER)")
        for _ in range(calls):
            conn.execute("INSERT INTO requests(charge_micro_usd,reserved_input,actual_input,"
                         "reserved_output,actual_output) VALUES (?,?,?,?,?)",
                         (charge // calls, inputs // calls, inputs // calls,
                          outputs // calls, outputs // calls))
        conn.commit()
    policy = SimpleNamespace(cap_micro_usd=None, total_call_cap=None,
                             total_input_cap=None, total_output_cap=None)
    return doctor._ledger_headroom(ledger, policy)


def test_usage_is_reported_as_calls_and_tokens(tmp_path):
    headroom = _headroom(tmp_path)
    assert headroom["calls"]["used"] == 3
    assert headroom["input_tokens"]["used"] > 0
    assert headroom["output_tokens"]["used"] > 0


def test_no_money_figure_reaches_the_report(tmp_path):
    """A currency amount depends on the reader's own contract, so it means
    something different for every operator and nothing comparable to anyone;
    calls and tokens are the same unit for everybody."""
    headroom = _headroom(tmp_path, charge=999999)
    assert "micro_usd" not in headroom
    assert "999999" not in json.dumps(headroom)


def test_usage_is_recorded_without_being_capped(tmp_path):
    """Recording is the point; a cap would stop work, which is not wanted."""
    headroom = _headroom(tmp_path)
    for unit in ("calls", "input_tokens", "output_tokens"):
        assert headroom[unit]["cap"] is None
        assert "used_ratio" not in headroom[unit]
    assert headroom["worst_used_ratio"] == 0.0


# --------------------------------------------------------------------------
# A provider refusing everything must be said out loud
# --------------------------------------------------------------------------

def _ledger(tmp_path, rows):
    import sqlite3
    import time as _time

    path = tmp_path / "auxiliary-budget.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY,"
                     " model TEXT, status TEXT, started_ns INTEGER,"
                     " charge_micro_usd INTEGER, reserved_input INTEGER, actual_input INTEGER,"
                     " reserved_output INTEGER, actual_output INTEGER)")
        now_ns = int(_time.time() * 1_000_000_000)
        for model, status in rows:
            conn.execute("INSERT INTO requests(model,status,started_ns,charge_micro_usd,"
                         "reserved_input,actual_input,reserved_output,actual_output)"
                         " VALUES (?,?,?,0,1,1,1,1)", (model, status, now_ns))
        conn.commit()
    return path


def test_a_provider_refusing_everything_is_named(tmp_path):
    """Four hours of "degraded" with an empty gap list, while the provider
    answered every call with "monthly usage limit reached"."""
    path = _ledger(tmp_path, [("deepseek-v4-flash",
                               "http_429:GoUsageLimitError_usage_unknown")] * 12)
    assert provider_refusals(path) == \
        ["model_refused:deepseek-v4-flash:GoUsageLimitError"]


def test_a_provider_without_a_code_is_still_named(tmp_path):
    path = _ledger(tmp_path, [("m", "http_429_usage_unknown_reserved_charge_retained")] * 12)
    assert provider_refusals(path) == ["model_refused:m:http_429"]


def test_a_healthy_provider_raises_nothing(tmp_path):
    path = _ledger(tmp_path, [("m", "http_200")] * 12)
    assert provider_refusals(path) == []


def test_an_occasional_failure_is_not_an_outage(tmp_path):
    """A flake must not read as a provider refusing service."""
    path = _ledger(tmp_path, [("m", "http_200")] * 20 + [("m", "http_429_usage")] * 2)
    assert provider_refusals(path) == []


def test_too_few_calls_to_judge(tmp_path):
    path = _ledger(tmp_path, [("m", "http_429_usage")] * 3)
    assert provider_refusals(path) == []


def test_only_the_recent_window_counts(tmp_path):
    """A provider that refused yesterday and answers today is not refusing."""
    path = _ledger(tmp_path, [("m", "http_429_usage")] * 12)
    assert provider_refusals(path, now=time.time() + 2 * REFUSAL_WINDOW_SECONDS) == []


def test_a_missing_ledger_is_not_an_outage(tmp_path):
    assert provider_refusals(None) == []
    assert provider_refusals(tmp_path / "absent.sqlite3") == []


def test_one_model_refusing_does_not_implicate_another(tmp_path):
    path = _ledger(tmp_path, [("chat", "http_429_usage")] * 12 + [("embed", "http_200")] * 12)
    assert provider_refusals(path) == ["model_refused:chat:http_429"]


# --- a refusal the ledger can never show -----------------------------------
#
# ``reserve`` raises before the request is sent, so an unapproved model writes
# no ledger row.  These cover the gap that measurement cannot reach.

def _auxiliary(*, consolidation_model="chat", approved=("chat",), priced=None,
               external_consolidation=True, external_embedding=False,
               embedding_model=None, ledger_path=None):
    from decimal import Decimal

    from scope_recall.adapters.models import ConsolidationRouteConfig, EmbeddingRouteConfig
    from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig
    from scope_recall.runtime.model_budget import BudgetPolicy, ModelPricing

    names = tuple(approved)
    pricing = {name: ModelPricing(input_usd_per_million=Decimal("0.30"),
                                  output_usd_per_million=Decimal("1.20"))
               for name in (names if priced is None else priced)}
    budget = BudgetPolicy(
        batch="test", cap_micro_usd=None, total_input_cap=None, total_output_cap=None,
        total_call_cap=None, max_request_bytes=131072, default_reserve_input=32768,
        default_reserve_output=8192, model_reserve_output={},
        model_token_caps={}, pricing=pricing, approved_models=frozenset(names),
    )
    embedding = None
    if external_embedding:
        embedding = EmbeddingRouteConfig(credential_env="SCOPE_RECALL_EMBED_KEY",
                                         **({} if embedding_model is None else {
                                             "model": embedding_model,
                                             "endpoint": "https://example.invalid/embed",
                                             "dimensions": 8, "dialect": "gemini"}))
    return AuxiliaryRuntimeConfig(
        external_embedding=external_embedding,
        external_consolidation=external_consolidation,
        installation_dir=None, ledger_path=ledger_path, budget=budget, embedding=embedding,
        consolidation=ConsolidationRouteConfig(
            model=consolidation_model, endpoint="https://example.invalid/chat",
            credential_env="SCOPE_RECALL_CHAT_KEY", output_limit_field="max_tokens",
            max_output_tokens=8192),
        consolidation_reserve_input=32768,
    )


def test_a_model_the_budget_will_not_approve_is_named():
    """The live fault: a provider switch registered the endpoint and credential
    but not the name, and nineteen work items deferred hourly in silence."""
    from scope_recall.runtime.model_budget import pre_request_refusals
    aux = _auxiliary(consolidation_model="deepseek-chat", approved=("deepseek-v4-flash",))
    assert pre_request_refusals(aux) == ["model_not_approved:consolidation:deepseek-chat"]


def test_an_approved_model_always_has_a_price():
    """``reserve`` raises the same ``unsupported_model`` for a missing price, so
    it would stall the same way -- but the policy refuses to exist in that state,
    which is why ``pre_request_refusals`` does not check for it."""
    import pytest

    with pytest.raises(ValueError, match="approved_model_pricing"):
        _auxiliary(consolidation_model="chat", approved=("chat",), priced=())


def test_a_fully_registered_model_raises_nothing():
    from scope_recall.runtime.model_budget import pre_request_refusals
    assert pre_request_refusals(_auxiliary()) == []


def test_a_route_that_is_switched_off_is_not_a_gap():
    """An unapproved name on a route nothing calls is not a fault to chase."""
    from scope_recall.runtime.model_budget import pre_request_refusals
    aux = _auxiliary(consolidation_model="unknown", approved=("chat",),
                     priced=("chat",), external_consolidation=False)
    assert pre_request_refusals(aux) == []


def test_the_embedding_default_is_checked_under_the_name_it_will_send():
    """An omitted embedding model still resolves to a concrete name, and that
    name is what ``reserve`` will judge."""
    from scope_recall.adapters.models import EMBEDDING_SPACE
    from scope_recall.runtime.model_budget import pre_request_refusals
    default = EMBEDDING_SPACE["model"]
    approved = _auxiliary(external_embedding=True, approved=("chat", default))
    assert pre_request_refusals(approved) == []
    missing = _auxiliary(external_embedding=True, approved=("chat",))
    assert pre_request_refusals(missing) == [f"model_not_approved:embedding:{default}"]


def test_no_auxiliary_and_no_allowlist_are_not_gaps():
    """An empty allowlist refuses every model; saying so once per route would
    report a single fault as many."""
    from scope_recall.runtime.model_budget import pre_request_refusals
    assert pre_request_refusals(None) == []
    assert pre_request_refusals(_auxiliary(approved=())) == []


def test_a_ledger_that_is_not_there_is_named(tmp_path):
    """``reserve`` raises ``ledger_not_initialized`` before the request, so the
    ledger cannot report its own absence and neither can the headroom read that
    opens it.  Every item then defers hourly in total silence."""
    from scope_recall.maintenance.doctor import _ledger_headroom
    from scope_recall.runtime.model_budget import pre_request_refusals, provider_refusals

    absent = tmp_path / "auxiliary-budget.sqlite3"
    assert provider_refusals(absent) == [], "the ledger cannot report its own absence"
    assert _ledger_headroom(absent, object()) == {}, "nor can the headroom read"
    assert pre_request_refusals(_auxiliary(ledger_path=absent)) ==         ["ledger_missing:auxiliary-budget.sqlite3"]


def test_a_ledger_that_is_there_is_not_a_gap(tmp_path):
    from scope_recall.runtime.model_budget import pre_request_refusals

    present = tmp_path / "auxiliary-budget.sqlite3"
    present.write_bytes(b"")
    assert pre_request_refusals(_auxiliary(ledger_path=present)) == []


def test_an_instance_that_calls_no_model_needs_no_ledger(tmp_path):
    """Nothing reserves anything, so an absent ledger refuses nothing."""
    from scope_recall.runtime.model_budget import pre_request_refusals

    aux = _auxiliary(ledger_path=tmp_path / "absent.sqlite3",
                     external_consolidation=False, external_embedding=False)
    assert pre_request_refusals(aux) == []
