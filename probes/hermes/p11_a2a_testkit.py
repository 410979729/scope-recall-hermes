"""TEST-only P11/P18 Hermes A2A wiring constants and redacted helpers.

This module has no network side effects.  The start script owns the gateway and
the local main-model meter bridge; the request script is the only script that
can send an A2A message, and it requires an explicit ``--run`` flag.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import tomllib
from typing import Any

from scope_recall.runtime.model_budget import ModelPricing, BudgetPolicy


REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_RUNTIME_ROOT = Path(os.environ.get("SCOPE_RECALL_P11_RUNTIME_ROOT", REPO_ROOT.parents[1] / "TEST-Hermes-runtime-v1")).resolve()
STATE = Path(os.environ.get("SCOPE_RECALL_P11_STATE", REPO_ROOT / ".execution" / "TEST-P11-A2A-v4")).resolve()
HERMES_HOME = (STATE / "hermes-home").resolve()
CORE_DIR = (HERMES_HOME / "scope-recall").resolve()
CORE_DB = CORE_DIR / "memory.sqlite3"
RUNTIME_CONFIG = CORE_DIR / "runtime-config.json"
# Official Hermes reads this filename from the isolated HERMES_HOME.
HERMES_CONFIG = (HERMES_HOME / "config.yaml").resolve()
BUDGET_CONFIG = (STATE / "budget-policy.json").resolve()
# Default P11 diagnostic/shared-ledger binding. Isolated P18 formal runs must
# resolve the hash-bound TEST config instead of monkeypatching this constant.
LEDGER = Path(r"F:\SCOPERECALL更新项目\worktrees\scope-recall-v1.1\.execution\TEST-MODEL-BUDGET-V1\call-budget.sqlite3").resolve()
LEDGER_BINDING_SCHEMA = "scope-recall.p18-original-ledger.v1"
ARCHIVE = (STATE / "archive").resolve()
STOP_FILE = (STATE / "STOP-P11-A2A").resolve()
RUNTIME_RECORD = (STATE / "runtime.json").resolve()

HERMES_ROOT = Path(os.environ.get("SCOPE_RECALL_P11_HERMES_ROOT", TEST_RUNTIME_ROOT / "hermes-source-79445")).resolve()
HERMES_PYTHON = Path(os.environ.get("SCOPE_RECALL_P11_HERMES_PYTHON", TEST_RUNTIME_ROOT / "venv" / "Scripts" / "python.exe")).resolve()
_ENVIRONMENT_MANIFEST = TEST_RUNTIME_ROOT / "environment-manifest.json"
try:
    _runtime_manifest = json.loads(_ENVIRONMENT_MANIFEST.read_text(encoding="utf-8"))
except (OSError, ValueError, TypeError):
    _runtime_manifest = {}
HOST_HEAD = str(_runtime_manifest.get("requested_hermes_sha") or "runtime-unresolved")
try:
    HOST_VERSION = str(tomllib.loads((HERMES_ROOT / "pyproject.toml").read_text(encoding="utf-8")).get("project", {}).get("version") or "runtime-unresolved")
except (OSError, TypeError, ValueError):
    HOST_VERSION = "runtime-unresolved"
# Do not inspect the official Hermes runtime during module import.  Collection
# runs under the TEST boundary; actual P11 preparation/start explicitly asks
# for this digest during its own TEST runtime phase.
HOST_PYTHON_SHA256 = "runtime-unresolved"


def host_python_sha256() -> str:
    if not HERMES_PYTHON.is_file():
        return "runtime-missing"
    return hashlib.sha256(HERMES_PYTHON.read_bytes()).hexdigest()

A2A_HOST = "127.0.0.1"
A2A_PORT = 19921
MAIN_BRIDGE_HOST = "127.0.0.1"
MAIN_BRIDGE_PORT = 29991
A2A_AGENT_NAME = os.environ.get("SCOPE_RECALL_P11_A2A_AGENT_NAME", "TEST_SCOPE_RECALL_P11_A2A_V4")
# The official gateway resolves this isolated HERMES_HOME to its default profile
# at runtime; the installation binding must match that runtime identity exactly.
TEST_AGENT_ID = "default"
TEST_WORKSPACE = os.environ.get("SCOPE_RECALL_P11_TEST_WORKSPACE", "TEST-P11-A2A-v4")
TEST_CONTEXT = os.environ.get("SCOPE_RECALL_P11_TEST_CONTEXT", "TEST-context-v4-1")
#: The v3 audience declaration names the user explicitly, and it has to be
#: the same string the installation is bound to, so both read it from here.
TEST_USER_ID = "TEST-user"
BATCH_NAME = os.environ.get("SCOPE_RECALL_P11_BATCH_NAME", "P11_A2A_V2")

MAIN_MODEL = "deepseek-v4-flash"
AUX_MODEL = "mimo-v2.5"
UPSTREAM_ENDPOINT = "https://opencode.ai/zen/go/v1/chat/completions"
UPSTREAM_BASE_URL = "https://opencode.ai/zen/go/v1"
LOCAL_BRIDGE_TOKEN_ENV = "SCOPE_RECALL_TEST_LOCAL_BRIDGE_TOKEN"
UPSTREAM_KEY_ENV = "SCOPE_RECALL_TEST_OPENCODE_GO_API_KEY"

MAX_MODEL_POSTS = int(os.environ.get("SCOPE_RECALL_P11_MAX_MODEL_POSTS", "8"))
REQUEST_TIMEOUT_SECONDS = 45.0
MAX_REQUEST_BYTES = 786_432
RESERVE_INPUT = 32_768
MAIN_RESERVE_OUTPUT = 4_096
AUX_RESERVE_OUTPUT = 131_072


def _budget_override() -> dict[str, Any] | None:
    """Read an explicitly hash-bound TEST budget without changing the ledger."""
    name = os.environ.get("SCOPE_RECALL_P18_BUDGET_CONFIG")
    expected = os.environ.get("SCOPE_RECALL_P18_BUDGET_SHA256")
    if name is None and expected is None:
        return None
    if not name or not expected:
        raise ValueError("TEST budget path and hash must be supplied together")
    path = Path(name)
    if not path.is_absolute() or "test" not in str(path).lower():
        raise ValueError("absolute TEST budget path required")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected.lower():
        raise ValueError("TEST budget hash mismatch")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("batch") != "P18_EVALUATION":
        raise ValueError("formal TEST budget batch required")
    from scope_recall.runtime.auxiliary import _budget_policy_from_mapping
    _budget_policy_from_mapping(value)
    if value.get("batch_call_cap") != MAX_MODEL_POSTS:
        raise ValueError("TEST bridge and batch call caps differ")
    return value


def budget_mapping() -> dict[str, Any]:
    """Return the frozen, conservative TEST budget; no account is contacted."""

    override = _budget_override()
    if override is not None:
        return override
    return {
        "batch": BATCH_NAME,
        "cap_micro_usd": 20_000_000,
        "total_input_cap": 64_000_000,
        "total_output_cap": 8_000_000,
        "total_call_cap": 8_000,
        "batch_call_cap": MAX_MODEL_POSTS,
        "max_request_bytes": MAX_REQUEST_BYTES,
        "default_reserve_input": RESERVE_INPUT,
        "default_reserve_output": MAIN_RESERVE_OUTPUT,
        "model_reserve_output": {MAIN_MODEL: MAIN_RESERVE_OUTPUT, AUX_MODEL: AUX_RESERVE_OUTPUT, "glm-5.3-flash": 4_096},
        "model_token_caps": {
            MAIN_MODEL: {"input": 8_000_000, "output": 1_000_000},
            AUX_MODEL: {"input": 52_000_000, "output": 6_500_000},
            "glm-5.3-flash": {"input": 4_000_000, "output": 500_000},
        },
        "pricing": {
            MAIN_MODEL: {"input_usd_per_million": "0.44", "output_usd_per_million": "1.32"},
            AUX_MODEL: {"input_usd_per_million": "0.14", "output_usd_per_million": "0.28"},
            "glm-5.3-flash": {"input_usd_per_million": "0.15", "output_usd_per_million": "0.50"},
        },
        "approved_models": [MAIN_MODEL, AUX_MODEL, "glm-5.3-flash"],
    }


def _require_test_absolute(path: Path, *, label: str) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute() or "test" not in str(resolved).lower():
        raise ValueError(f"absolute TEST {label} required")
    return resolved


def resolve_hash_bound_formal_ledger(formal_config_path: str | Path, expected_sha256: str) -> Path:
    """Resolve the existing isolated TEST ledger from a hash-bound formal config.

    Never creates, copies, or resets a ledger. Old P11 callers that omit a
    formal config keep ``LEDGER``.
    """
    if not expected_sha256 or len(expected_sha256) != 64 or any(
        char not in "0123456789abcdefABCDEF" for char in expected_sha256
    ):
        raise ValueError("TEST formal config hash required")
    path = _require_test_absolute(Path(formal_config_path), label="formal config path")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256.lower():
        raise ValueError("TEST formal config hash mismatch")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("TEST formal config object required")
    reference = value.get("ledger_path")
    binding = value.get("ledger_binding")
    if type(reference) is not str or not reference.strip():
        raise ValueError("TEST formal ledger_path required")
    base = path.parent.resolve()
    ref = Path(reference)
    ledger = ref.resolve() if ref.is_absolute() else (base / ref).resolve()
    if not ref.is_absolute() and not ledger.is_relative_to(base):
        raise ValueError("TEST formal ledger_path escaped config root")
    if binding is None:
        raise ValueError("TEST formal ledger_binding required")
    if (
        type(binding) is not dict
        or set(binding) != {"schema", "canonical_path", "device", "inode"}
        or binding.get("schema") != LEDGER_BINDING_SCHEMA
        or type(binding.get("canonical_path")) is not str
        or not Path(binding["canonical_path"]).is_absolute()
        or type(binding.get("device")) is not int
        or binding["device"] < 0
        or type(binding.get("inode")) is not int
        or binding["inode"] <= 0
    ):
        raise ValueError("TEST formal ledger_binding invalid")
    canonical = Path(binding["canonical_path"])
    if ledger != canonical or ledger != canonical.resolve():
        raise ValueError("TEST formal ledger is not the frozen original")
    if not ledger.is_file():
        raise ValueError("TEST formal ledger file missing")
    if "test" not in str(ledger).lower():
        raise ValueError("absolute TEST ledger path required")
    stat = ledger.stat()
    if (stat.st_dev, stat.st_ino) != (binding["device"], binding["inode"]):
        raise ValueError("TEST formal ledger identity changed")
    return ledger


def active_bridge_ledger(
    *,
    formal_config_path: str | Path | None = None,
    formal_freeze_sha256: str | None = None,
) -> Path:
    """Old P11: no formal config → ``LEDGER``. Isolated P18: hash-bound formal ledger."""
    if formal_config_path is None:
        return LEDGER
    if not formal_freeze_sha256:
        raise ValueError("TEST formal config path and hash must be supplied together")
    return resolve_hash_bound_formal_ledger(formal_config_path, formal_freeze_sha256)


def resolve_hash_bound_runtime_budget(
    runtime_config_path: str | Path,
    expected_sha256: str,
    expected_ledger: str | Path,
) -> BudgetPolicy:
    """Load caps from a hash-bound TEST runtime config; do not create a ledger."""
    if not expected_sha256 or len(expected_sha256) != 64 or any(
        char not in "0123456789abcdefABCDEF" for char in expected_sha256
    ):
        raise ValueError("TEST runtime config hash required")
    path = _require_test_absolute(Path(runtime_config_path), label="runtime config path")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256.lower():
        raise ValueError("TEST runtime config hash mismatch")
    value = json.loads(raw.decode("utf-8"))
    auxiliary = value.get("auxiliary") if isinstance(value, dict) else None
    if not isinstance(auxiliary, dict):
        raise ValueError("TEST runtime auxiliary required")
    raw_ledger = auxiliary.get("ledger_path")
    if type(raw_ledger) is not str or not raw_ledger.strip():
        raise ValueError("TEST runtime ledger_path required")
    runtime_ledger = _require_test_absolute(Path(raw_ledger), label="runtime ledger_path").resolve()
    if runtime_ledger != Path(expected_ledger).expanduser().resolve():
        raise ValueError("TEST runtime ledger does not match formal ledger")
    from scope_recall.runtime.auxiliary import _budget_policy_from_mapping

    return _budget_policy_from_mapping(auxiliary.get("budget"))


def assert_env_budget_matches_isolated(policy: BudgetPolicy) -> None:
    """Reject an env override that disagrees with the isolated TEST budget."""
    name = os.environ.get("SCOPE_RECALL_P18_BUDGET_CONFIG")
    expected = os.environ.get("SCOPE_RECALL_P18_BUDGET_SHA256")
    if name is None and expected is None:
        return
    if not name or not expected:
        raise ValueError("TEST budget path and hash must be supplied together")
    path = _require_test_absolute(Path(name), label="budget path")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected.lower():
        raise ValueError("TEST budget hash mismatch")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("TEST budget object required")
    from scope_recall.runtime.auxiliary import _budget_policy_from_mapping

    env_policy = _budget_policy_from_mapping(value)
    if (
        env_policy.batch != policy.batch
        or env_policy.cap_micro_usd != policy.cap_micro_usd
        or env_policy.total_call_cap != policy.total_call_cap
    ):
        raise ValueError("TEST env budget conflicts with isolated runtime budget")


def budget_policy() -> BudgetPolicy:
    override = _budget_override()
    if override is not None:
        from scope_recall.runtime.auxiliary import _budget_policy_from_mapping
        return _budget_policy_from_mapping(override)
    pricing = {
        MAIN_MODEL: ModelPricing("0.44", "1.32"),
        AUX_MODEL: ModelPricing("0.14", "0.28"),
        "glm-5.3-flash": ModelPricing("0.15", "0.50"),
    }
    return BudgetPolicy(
        batch=BATCH_NAME,
        cap_micro_usd=20_000_000,
        total_input_cap=64_000_000,
        total_output_cap=8_000_000,
        total_call_cap=8_000,
        max_request_bytes=MAX_REQUEST_BYTES,
        default_reserve_input=RESERVE_INPUT,
        default_reserve_output=MAIN_RESERVE_OUTPUT,
        model_reserve_output={MAIN_MODEL: MAIN_RESERVE_OUTPUT, AUX_MODEL: AUX_RESERVE_OUTPUT, "glm-5.3-flash": 4_096},
        model_token_caps={MAIN_MODEL: (8_000_000, 1_000_000), AUX_MODEL: (52_000_000, 6_500_000), "glm-5.3-flash": (4_000_000, 500_000)},
        pricing=pricing,
        approved_models=frozenset({MAIN_MODEL, AUX_MODEL, "glm-5.3-flash"}),
    )


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


_SECRET = re.compile(r"(?i)(password|passwd|api[_-]?key|secret|token|authorization)\s*[:=]\s*[^,\s}\]]+")


def scrub_text(value: str) -> str:
    return _SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)


def scrub(value: object) -> object:
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {str(key): scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def port_status() -> dict[str, bool]:
    return {
        "a2a_19921_free": port_is_free(A2A_HOST, A2A_PORT),
        "main_bridge_29991_free": port_is_free(MAIN_BRIDGE_HOST, MAIN_BRIDGE_PORT),
    }


def assert_test_path(path: Path, *, allow_repo: bool = False) -> None:
    resolved = Path(path).resolve()
    if resolved == STATE or STATE in resolved.parents:
        return
    if allow_repo and (resolved == REPO_ROOT or REPO_ROOT in resolved.parents):
        return
    raise RuntimeError(f"TEST path escaped: {resolved}")


def current_env_presence() -> dict[str, bool]:
    """Report presence only; never return or persist a credential value."""

    return {UPSTREAM_KEY_ENV: bool(os.environ.get(UPSTREAM_KEY_ENV, ""))}


def shared_ledger_status() -> dict[str, int | bool]:
    if not LEDGER.is_file():
        return {"ledger_exists": False, "global_requests": 0, "batch_requests": 0}
    with sqlite3.connect(LEDGER.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
        global_count = int(db.execute("SELECT count(*) FROM requests").fetchone()[0])
        batch_count = int(db.execute("SELECT count(*) FROM requests WHERE batch=?", (BATCH_NAME,)).fetchone()[0])
        guard = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (BATCH_GUARD_NAME,)).fetchone())
    return {"ledger_exists": True, "global_requests": global_count, "batch_requests": batch_count, "batch_guard": guard}


BATCH_GUARD_NAME = "scope_recall_test_p11_batch_cap_" + BATCH_NAME.rsplit("_", 1)[-1].lower()


def install_batch_guard() -> None:
    if not LEDGER.is_file():
        raise RuntimeError("frozen shared ledger missing")
    with sqlite3.connect(LEDGER, timeout=5) as db, db:
        db.execute(
            f'''CREATE TRIGGER IF NOT EXISTS "{BATCH_GUARD_NAME}"
                BEFORE INSERT ON requests
                WHEN NEW.batch = '{BATCH_NAME}'
                 AND (SELECT count(*) FROM requests WHERE batch = NEW.batch) >= {MAX_MODEL_POSTS}
                BEGIN
                  SELECT RAISE(ABORT, 'p11_batch_call_cap');
                END'''
        )


__all__ = [
    "A2A_AGENT_NAME", "A2A_HOST", "A2A_PORT", "ARCHIVE", "AUX_MODEL", "AUX_RESERVE_OUTPUT", "BATCH_GUARD_NAME", "BATCH_NAME",
    "BUDGET_CONFIG", "CORE_DB", "CORE_DIR", "HERMES_CONFIG", "HERMES_HOME", "HERMES_PYTHON",
    "HERMES_ROOT", "HOST_HEAD", "HOST_VERSION", "LEDGER", "LOCAL_BRIDGE_TOKEN_ENV", "MAIN_MODEL",
    "MAIN_BRIDGE_HOST", "MAIN_BRIDGE_PORT", "MAIN_RESERVE_OUTPUT", "MAX_MODEL_POSTS", "MAX_REQUEST_BYTES",
    "REPO_ROOT", "REQUEST_TIMEOUT_SECONDS", "RESERVE_INPUT", "RUNTIME_CONFIG", "RUNTIME_RECORD", "STATE", "TEST_RUNTIME_ROOT",
    "STOP_FILE", "TEST_AGENT_ID", "TEST_CONTEXT", "TEST_WORKSPACE", "UPSTREAM_BASE_URL", "UPSTREAM_ENDPOINT", "UPSTREAM_KEY_ENV",
    "LEDGER_BINDING_SCHEMA", "active_bridge_ledger", "assert_env_budget_matches_isolated", "assert_test_path",
    "budget_mapping", "budget_policy", "current_env_presence", "digest_bytes", "load_json",
    "port_is_free", "port_status", "resolve_hash_bound_formal_ledger", "resolve_hash_bound_runtime_budget",
    "scrub", "scrub_text", "shared_ledger_status", "install_batch_guard", "write_json",
    "host_python_sha256",
]
