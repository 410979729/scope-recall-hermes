from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from probes.hermes.p11_a2a_testkit import (
    A2A_PORT, AUX_RESERVE_OUTPUT, CORE_DIR, HERMES_CONFIG, HERMES_PYTHON, LEDGER, MAIN_BRIDGE_PORT, MAIN_MODEL,
    MAX_MODEL_POSTS, STATE, TEST_AGENT_ID, TEST_CONTEXT, TEST_WORKSPACE,
    AUX_MODEL, UPSTREAM_ENDPOINT, UPSTREAM_KEY_ENV, budget_mapping, port_status,
)
from probes.hermes.p11_prepare_a2a_test import _test_audiences
from probes.hermes.p11_a2a_bridge import Bridge, FORMAL_BATCH_NAME
from probes.hermes.p11_start_a2a_test import (
    ZERO_MODEL_DIAGNOSTIC_DUMMY, _gateway_log_offset, _gateway_processing_ready, _resolve_upstream_key,
)
from scope_recall.adapters.models import AuxiliaryBudgetLedger, OpenAIConsolidationAdapter
from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig
from scope_recall.runtime.model_budget import initialize_auxiliary_budget_ledger


def test_p11_testkit_isolated_and_offline_contract():
    assert str(STATE).endswith(".execution\\TEST-P11-A2A-v4") or str(STATE).endswith(".execution/TEST-P11-A2A-v4")
    assert TEST_AGENT_ID == "default"
    assert TEST_CONTEXT == "TEST-context-v4-1"
    assert TEST_WORKSPACE == "TEST-P11-A2A-v4"
    assert HERMES_CONFIG.name == "config.yaml"
    assert HERMES_CONFIG.parent.name == "hermes-home"
    assert HERMES_PYTHON.name == "python.exe"
    assert LEDGER.name == "call-budget.sqlite3" and "TEST-MODEL-BUDGET-V1" in str(LEDGER)
    assert (A2A_PORT, MAIN_BRIDGE_PORT) == (19921, 29991)
    mapping = budget_mapping()
    assert mapping["total_call_cap"] == 8000
    assert mapping["batch_call_cap"] == MAX_MODEL_POSTS == 8
    assert mapping["approved_models"] == [MAIN_MODEL, AUX_MODEL, "glm-5.3-flash"]
    assert mapping["model_reserve_output"][AUX_MODEL] == AUX_RESERVE_OUTPUT == 131072
    assert AUX_RESERVE_OUTPUT == 131072
    assert port_status().keys() == {"a2a_19921_free", "main_bridge_29991_free"}


def test_p11_audience_is_explicitly_unthreaded():
    audiences = _test_audiences(
        {"conversation": "TEST-conversation", "owner_private": "TEST-owner-private"}
    )
    # The owner_private mapping is threaded ("main") like the installer's own;
    # only the A2A conversation is unthreaded, so select it by kind rather than
    # by position.
    audience = next(item for item in audiences if item["kind"] == "conversation")
    assert audience["thread_id"] == ""
    assert audience["chat_id"] == TEST_CONTEXT
    owner = next(item for item in audiences if item["kind"] == "owner_private")
    assert owner["capture_scope_id"] == "TEST-owner-private"


def test_p11_batch_guard_rejects_main_and_aux_atomically(tmp_path):
    db_path = tmp_path / "shared-test-ledger.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, batch TEXT, model TEXT)")
        db.execute("""CREATE TRIGGER p11_guard BEFORE INSERT ON requests
                     WHEN NEW.batch='P11_A2A_V2' AND (SELECT count(*) FROM requests WHERE batch=NEW.batch)>=8
                     BEGIN SELECT RAISE(ABORT,'p11_batch_call_cap'); END""")
        db.executemany("INSERT INTO requests(batch,model) VALUES (?,?)",
                       [("P11_A2A_V2", "deepseek-v4-flash" if i % 2 == 0 else "mimo-v2.5") for i in range(8)])
        network_attempts = []
        for route in ("main", "aux"):
            try:
                db.execute("INSERT INTO requests(batch,model) VALUES (?,?)",
                           ("P11_A2A_V2", "deepseek-v4-flash" if route == "main" else "mimo-v2.5"))
            except sqlite3.IntegrityError as exc:
                assert str(exc) == "p11_batch_call_cap"
            else:
                network_attempts.append(route)
        assert network_attempts == []


def test_formal_bridge_uses_p18_batch_and_not_p11_diagnostic_cap(monkeypatch, tmp_path):
    import probes.hermes.p11_a2a_bridge as bridge_module
    import probes.hermes.p11_a2a_testkit as testkit_module

    # ``p11_a2a_bridge`` imports ``LEDGER`` by value, so it holds its own binding
    # while ``active_bridge_ledger()`` still reads the testkit's. Patching only
    # one desynchronises the two, and ``Bridge.__init__`` then reads the mismatch
    # as an isolated ledger and demands a hash-bound runtime budget.
    isolated_ledger = tmp_path / "shared-test-ledger.sqlite3"
    monkeypatch.setattr(bridge_module, "LEDGER", isolated_ledger)
    monkeypatch.setattr(testkit_module, "LEDGER", isolated_ledger)
    formal = Bridge(tmp_path / "formal-state", 29991, formal_active_operation=True, formal_freeze_sha256="a" * 64)
    diagnostic = Bridge(tmp_path / "diagnostic-state", 29991)
    assert formal.ledger.policy.batch == FORMAL_BATCH_NAME == "P18_EVALUATION"
    assert formal.ledger.policy.total_call_cap == 8000
    assert formal.ledger.policy.cap_micro_usd == 20_000_000
    assert formal.batch_has_room() is True
    assert diagnostic.ledger.policy.batch == "P11_A2A_V2"


def _synthetic_prepare_layout(tmp_path: Path):
    from probes.hermes.p11_a2a_testkit import budget_policy
    from probes.hermes.p11_prepare_a2a_test import PrepareLayout
    from scope_recall.runtime.model_budget import initialize_auxiliary_budget_ledger

    state = tmp_path / "isolated-prepare-state"
    ledger = tmp_path / "synthetic-fixture-ledger.sqlite3"
    initialize_auxiliary_budget_ledger(ledger, budget_policy())
    sentinel = tmp_path / "SYNTHETIC-P15-SENTINEL-NOT-A-HISTORICAL-P11-REPORT.json"
    sentinel.write_text(
        json.dumps({
            "fixture_kind": "synthetic_readonly_p15_sentinel",
            "note": "Not a historical P11 report. Digest is observed before and after prepare.",
        }, sort_keys=True),
        encoding="utf-8",
    )
    return PrepareLayout(
        state=state,
        hermes_home=state / "hermes-home",
        core_dir=state / "core",
        archive=state / "archive",
        ledger=ledger,
        budget_config=state / "budget.json",
        runtime_config=state / "runtime.json",
        hermes_config=state / "hermes-home" / "config.yaml",
        p15_sentinel=sentinel,
        kind="synthetic_isolated_prepare_fixture",
    )


def test_p11_prepare_writes_isolated_artifacts_and_observes_zero_post(monkeypatch, tmp_path):
    import urllib.request
    from probes.hermes.p11_prepare_a2a_test import run_prepare

    http_attempts = []

    def forbid_urlopen(*args, **kwargs):
        http_attempts.append(("urlopen", args, kwargs))
        raise AssertionError("prepare must not POST")

    monkeypatch.setattr(urllib.request, "urlopen", forbid_urlopen)
    layout = _synthetic_prepare_layout(tmp_path)
    sentinel_before = layout.p15_sentinel.read_bytes()
    code, receipt = run_prepare(layout)
    assert code == 0
    assert receipt["prepared"] is True
    assert receipt["layout_kind"] == "synthetic_isolated_prepare_fixture"
    assert "prepared_only" not in receipt
    observed = receipt["observed"]
    assert observed["model_posts_in_this_prep"] == observed["ledger_batch_after"] - observed["ledger_batch_before"] == 0
    assert observed["ledger_global_before"] == observed["ledger_global_after"] == 0
    assert observed["p15_unchanged"] is True
    assert observed["p15_sentinel_digest_before"] == observed["p15_sentinel_digest_after"]
    assert layout.p15_sentinel.read_bytes() == sentinel_before
    assert http_attempts == []
    prepare_artifact = layout.archive / "prepare.json"
    assert prepare_artifact.is_file()
    written = json.loads(prepare_artifact.read_text(encoding="utf-8"))
    assert written["observed"]["model_posts_in_this_prep"] == 0
    assert (layout.hermes_home / "plugins" / "scope_recall" / "__init__.py").is_file()
    assert Path(receipt["core_db"]).is_file()
    assert layout.runtime_config.is_file()
    assert layout.hermes_config.is_file()


def test_p11_main_asserts_default_paths_before_run_prepare(monkeypatch, capsys):
    import probes.hermes.p11_prepare_a2a_test as prepare

    seen: list[Path] = []
    prepare_started = {"seen_before": None}

    def record_assert(path, **_kwargs):
        seen.append(Path(path))

    def fake_prepare(layout):
        prepare_started["seen_before"] = list(seen)
        return 2, {"prepared": False, "reason": "fixture_stop_before_write"}

    monkeypatch.setattr(prepare, "assert_test_path", record_assert)
    monkeypatch.setattr(prepare, "port_status", lambda: {"a2a_19921_free": True, "main_bridge_29991_free": True})
    monkeypatch.setattr(prepare, "shared_ledger_status", lambda: {"ledger_exists": True, "batch_requests": 0})
    monkeypatch.setattr(prepare, "install_batch_guard", lambda: None)
    monkeypatch.setattr(prepare, "run_prepare", fake_prepare)

    assert prepare.main() == 2
    assert prepare_started["seen_before"] == [prepare.STATE, prepare.HERMES_HOME, prepare.CORE_DIR, prepare.ARCHIVE]
    assert json.loads(capsys.readouterr().out)["prepared"] is False


def test_p11_main_success_receipt_keeps_host_source_and_wrapper_hashes(monkeypatch, tmp_path, capsys):
    import hashlib
    import probes.hermes.p11_prepare_a2a_test as prepare

    hermes_root = tmp_path / "frozen-h-fixture"
    adapter = hermes_root / "plugins" / "platforms" / "a2a" / "adapter.py"
    adapter.parent.mkdir(parents=True)
    adapter.write_bytes(b"FROZEN-H-A2A-ADAPTER-FIXTURE")
    hermes_home = tmp_path / "hermes-home"
    archive = tmp_path / "archive"
    plugin = hermes_home / "plugins" / "scope_recall" / "__init__.py"
    wrapper_bytes = b"WRITTEN-PLUGIN-INIT-FIXTURE\n"
    ledger = tmp_path / "fixture-ledger.sqlite3"
    ledger.write_bytes(b"")

    def fake_prepare(layout):
        plugin.parent.mkdir(parents=True)
        plugin.write_bytes(wrapper_bytes)
        archive.mkdir(parents=True)
        return 0, {
            "prepared": True,
            "observed": {
                "ledger_global_before": 2,
                "ledger_global_after": 2,
                "ledger_batch_before": 2,
                "ledger_batch_after": 2,
                "model_posts_in_this_prep": 0,
            },
        }

    monkeypatch.setattr(prepare, "assert_test_path", lambda path, **_kwargs: None)
    monkeypatch.setattr(prepare, "port_status", lambda: {"a2a_19921_free": True, "main_bridge_29991_free": True})
    monkeypatch.setattr(prepare, "shared_ledger_status", lambda: {"ledger_exists": True, "batch_requests": 2})
    monkeypatch.setattr(prepare, "install_batch_guard", lambda: None)
    monkeypatch.setattr(prepare, "run_prepare", fake_prepare)
    monkeypatch.setattr(prepare, "host_python_sha256", lambda: "a" * 64)
    monkeypatch.setattr(prepare, "read_auxiliary_budget_status", lambda _path: {"ledger_exists": True, "requests": 2})
    monkeypatch.setattr(prepare, "HERMES_ROOT", hermes_root)
    monkeypatch.setattr(prepare, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(prepare, "ARCHIVE", archive)
    monkeypatch.setattr(prepare, "LEDGER", ledger)
    monkeypatch.setattr(prepare, "STATE", tmp_path / "state")
    monkeypatch.setattr(prepare, "CORE_DIR", tmp_path / "core")

    assert prepare.main() == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["prepared"] is True
    assert receipt["host"]["source_sha256"] == hashlib.sha256(b"FROZEN-H-A2A-ADAPTER-FIXTURE").hexdigest()
    assert receipt["host"]["wrapper_sha256"] == hashlib.sha256(wrapper_bytes).hexdigest()
    assert receipt["model_posts_used"] == receipt["observed"]["ledger_batch_after"] == 2
    assert (archive / "prepare.json").is_file()


def test_p11_prepare_batch_cap_does_not_emit_prepared_success(tmp_path):
    from probes.hermes.p11_a2a_testkit import BATCH_NAME, MAX_MODEL_POSTS
    from probes.hermes.p11_prepare_a2a_test import run_prepare

    layout = _synthetic_prepare_layout(tmp_path)
    with sqlite3.connect(layout.ledger) as db:
        db.executemany(
            "INSERT INTO requests(batch, model) VALUES (?, ?)",
            [(BATCH_NAME, "deepseek-v4-flash") for _ in range(MAX_MODEL_POSTS)],
        )
        db.commit()
    code, receipt = run_prepare(layout)
    assert code == 2
    assert receipt["prepared"] is False
    assert receipt["reason"] == "P11 batch cap already reached"
    assert not (layout.archive / "prepare.json").exists()
    assert not (layout.hermes_home / "plugins" / "scope_recall" / "__init__.py").exists()
    assert not layout.state.exists()


def test_p11_real_aux_config_reserves_main_and_aux_without_transport(monkeypatch, tmp_path):
    # ``propose`` loads the credential before it reserves, so without one the
    # run dies as ``credential_missing`` and never reaches the reservation this
    # test exists to observe. The value is never sent anywhere: ``NoTransport``
    # asserts if the HTTP path is entered at all.
    monkeypatch.setenv(UPSTREAM_KEY_ENV, "TEST-scope-recall-unused-credential")
    config = AuxiliaryRuntimeConfig.from_mapping({
        "external_embedding": False,
        "external_consolidation": True,
        "installation_dir": str(CORE_DIR),
        "ledger_path": str(LEDGER),
        "budget": budget_mapping(),
        "consolidation": {
            "model": AUX_MODEL,
            "endpoint": UPSTREAM_ENDPOINT,
            "credential_env": UPSTREAM_KEY_ENV,
            "output_limit_field": "max_completion_tokens",
            "max_output_tokens": 4096,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "stream": False,
            "n": 1,
        },
        "consolidation_reserve_input": 32768,
    })
    ledger_path = tmp_path / "reserve-only.sqlite3"
    initialize_auxiliary_budget_ledger(ledger_path, config.budget)
    main_ledger = AuxiliaryBudgetLedger(ledger_path, config.budget)
    messages = [{"role": "system", "content": "TEST_SCOPE_RECALL system"},
                {"role": "user", "content": "TEST_SCOPE_RECALL reserve"}]
    main_body = json.dumps({"model": MAIN_MODEL, "messages": messages, "max_tokens": 4096,
                            "stream": False, "n": 1}, separators=(",", ":")).encode()
    main_id = main_ledger.reserve(MAIN_MODEL, main_body, reserved_input=32768,
                                   reserved_output=4096, timeout_seconds=45)

    class StopAfterReserve(RuntimeError):
        pass

    class ReserveOnlyLedger(AuxiliaryBudgetLedger):
        def reserve(self, *args, **kwargs):
            request_id = super().reserve(*args, **kwargs)
            self.seen_id = request_id
            raise StopAfterReserve(request_id)

    class NoTransport:
        def post(self, *args, **kwargs):
            raise AssertionError("network transport must not be reached")

    aux_ledger = ReserveOnlyLedger(ledger_path, config.budget)
    adapter = OpenAIConsolidationAdapter(config.consolidation, ledger=aux_ledger,
                                         reserve_input=config.consolidation_reserve_input,
                                         transport=NoTransport())
    try:
        adapter.propose(messages, remaining_seconds=45)
    except StopAfterReserve as exc:
        aux_id = int(str(exc))
    else:
        raise AssertionError("auxiliary reservation did not stop before transport")
    with sqlite3.connect(ledger_path) as db:
        rows = db.execute("SELECT model,reserved_input,reserved_output,status FROM requests ORDER BY id").fetchall()
    assert main_id != aux_id
    assert rows == [(MAIN_MODEL, 32768, 4096, "reserved_before_network"),
                    (AUX_MODEL, 32768, 131072, "reserved_before_network")]


def test_p11_prepare_uses_clean_distribution_wrapper():
    from probes.hermes.p11_prepare_a2a_test import _plugin_files

    init, _metadata = _plugin_files()
    product = (ROOT / "distribution/hermes/__init__.py").read_text(encoding="utf-8")
    assert init == product
    assert "p11-lifecycle-trace" not in init
    assert "register_memory_provider" in init


def test_p11_future_main_route_disables_thinking_via_custom_provider_extra_body():
    from probes.hermes.p11_prepare_a2a_test import _json_config

    entry = _json_config()["custom_providers"][0]
    assert entry["extra_body"] == {"thinking": {"type": "disabled"}}


def test_p11_future_test_home_uses_short_official_idle_session_reset():
    from probes.hermes.p11_prepare_a2a_test import _json_config

    assert _json_config()["session_reset"] == {"mode": "idle", "idle_minutes": 1, "notify": False}


def test_p11_zero_model_diagnostic_skips_vault_loader():
    assert _resolve_upstream_key(zero_model_diagnostic=True) == ZERO_MODEL_DIAGNOSTIC_DUMMY


def test_p11_gateway_processing_ready_requires_frozen_restore_marker(tmp_path):
    log = tmp_path / "gateway.log"
    log.write_text("A2A: serving Agent Card\nPress Ctrl+C to stop\n", encoding="utf-8")
    start_offset = _gateway_log_offset(log)
    assert _gateway_processing_ready(log, start_offset) is False
    with log.open("ab") as stream:
        stream.write("startup restore complete: Press Ctrl+C to stop\n".encode("utf-8"))
    assert _gateway_processing_ready(log, start_offset) is True


def test_p11_gateway_processing_ready_missing_log_is_closed(tmp_path):
    assert _gateway_processing_ready(tmp_path / "missing.log") is False
