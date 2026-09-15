"""Offline P18 host/arm binding preparation.

The existing arm provisioner creates public-input plans and source snapshots.
This module turns one such plan entry into a bounded, host-launchable TEST
contract: fixed executable/version, isolated home/config/state/database roots,
and an arm-specific source binding.  It never installs, starts, imports, or
contacts a host.  Its result is ``PREPARED`` (or ``UNSUPPORTED``), never a
runtime or semantic PASS.

The contract intentionally contains paths and hashes rather than source,
history, answers, credentials, or sealed fixture data.  A downstream runner
may consume ``host-binding.json`` and the environment/config files, but must
still produce independent runtime evidence before treating an arm as run.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
from typing import Any, Mapping
import zipfile


BASELINE_578B = "578b955802df753f2e2208e26eab6f71971285a0"
FROZEN_HERMES_COMMIT = "79445a496c86a19332ad786494b8384d2167e2d0"
HOSTS = {"hermes_a2a", "codex_windows_desktop"}
ARMS = {"A", "B", "C", "D"}
SCHEMA = "scope-recall.p18.host-arm-binding.v1"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_KEYS = re.compile(
    r"(?:api[_-]?key|(?:^|[_-])token(?:$|[_-])|secret|password|credential|authorization)",
    re.IGNORECASE,
)
_HERMES_MAIN_MODEL = "deepseek-v4-flash"
_HERMES_A2A_PORT = 19921
_HERMES_BRIDGE_PORT = 29991
# Freeze the normal A2A surface across every Hermes arm.  The arm-specific
# provider remains the only memory-behavior difference; an empty list would
# also remove terminal/file tools needed by the bounded J02 actions.
_HERMES_NORMAL_TOOLSETS = ("terminal", "file")
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_HERMES_PYTHON = _PROJECT_ROOT / "TEST-Hermes-runtime-v1" / "venv" / "Scripts" / "python.exe"
_CANDIDATE_PYTHON = (
    _PROJECT_ROOT / "worktrees" / "scope-recall-runtime-integration"
    / ".execution" / "TEST-FINAL-RUNTIME-ENV" / "Scripts" / "python.exe"
)
_P12_RUNTIME_TEMPLATE = _PROJECT_ROOT / "TEST-P12-AUTO-v4" / "data" / "runtime-config.json"
_P11_RUNTIME_TEMPLATE = (
    _PROJECT_ROOT / "TEST-P11-candidate-v5" / "hermes-home" / "scope-recall" / "runtime-config.json"
)
_P11_PLUGIN_MANIFEST = (
    _PROJECT_ROOT / "TEST-P11-candidate-v5" / "hermes-home" / "plugins" / "scope_recall" / "plugin.yaml"
)
_ENV_NAME_KEYS = frozenset({"credential_env", "api_key_env", "key_env", "env_var"})
_NON_SECRET_RUNTIME_KEYS = frozenset({"model_token_caps"})


class HostArmBindingError(ValueError):
    """Invalid provision entry or unsafe TEST launch binding."""


def _write_codex_workspace_write_home(home: Path, project_root: Path) -> Path:
    """Pin TEST Codex home to workspace-write. Does not change gold or method.json."""
    home.mkdir(parents=True, exist_ok=True)
    config = home / "config.toml"
    project = str(project_root)
    payload = (
        'sandbox_mode = "danger-full-access"\n'
        'approval_policy = "never"\n'
        "\n"
        "[projects]\n"
        "\n"
        f"[projects.{json.dumps(project)}]\n"
        'trust_level = "trusted"\n'
    )
    if config.exists():
        raise HostArmBindingError("codex_home_config_already_exists")
    config.write_text(payload, encoding="utf-8", newline="\n")
    return config


@dataclass(frozen=True)
class HostExecutable:
    path: Path
    version: str
    sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _hardlink_or_copy(source: str, destination: str) -> str:
    """Materialize immutable test code cheaply on one volume, with a copy fallback."""
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
    return destination


def _root(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    lowered = str(path).replace("/", "\\").lower().rstrip("\\")
    if lowered == "f:\\agents" or lowered.startswith("f:\\agents\\"):
        raise HostArmBindingError("formal F:\\Agents root is forbidden")
    if "test" not in lowered:
        raise HostArmBindingError("TEST isolation path is required")
    return path


def _under(path: Path, root: Path, reason: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise HostArmBindingError(reason) from exc
    return resolved


def _require_digest(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise HostArmBindingError(reason)
    return value


def _assert_no_secrets(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_name = str(key).casefold()
            if (
                _FORBIDDEN_KEYS.search(str(key))
                and key_name not in _ENV_NAME_KEYS
                and key_name not in _NON_SECRET_RUNTIME_KEYS
            ):
                raise HostArmBindingError("secret-bearing manifest key is forbidden")
            _assert_no_secrets(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_secrets(child)


def _load_plan(entry_root: Path) -> dict[str, Any]:
    plan_path = entry_root / "arm-plan.json"
    if not plan_path.is_file():
        raise HostArmBindingError("arm-plan.json is missing")
    try:
        value = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HostArmBindingError("arm-plan.json is invalid") from exc
    if not isinstance(value, dict) or value.get("schema") != "scope-recall.p18.arm-provision-entry.v1":
        raise HostArmBindingError("unsupported arm-plan schema")
    _assert_no_secrets(value)
    host_id = value.get("host_id")
    arm_id = value.get("arm_id")
    if host_id not in {*HOSTS, "hermes_cli_local_input_v1"} or arm_id not in ARMS:
        raise HostArmBindingError("unsupported host or arm")
    host = value.get("host")
    if not isinstance(host, Mapping) or Path(str(host.get("isolated_test_root", ""))).resolve() != entry_root:
        raise HostArmBindingError("arm-plan root does not match binding entry")
    return value


def _file_ref(path: Path, expected_sha256: str | None = None) -> dict[str, str]:
    if not path.is_file():
        raise HostArmBindingError(f"required file is missing: {path.name}")
    actual = _sha256(path)
    if expected_sha256 is not None and actual != _require_digest(expected_sha256, "file digest is invalid"):
        raise HostArmBindingError("file digest mismatch")
    return {"path": str(path), "sha256": actual}


def _host_executable(
    path: str | Path,
    version: str,
    expected_sha256: str | None = None,
) -> HostExecutable:
    executable = Path(path).expanduser().resolve()
    if not executable.is_file():
        raise HostArmBindingError("host executable is missing")
    if not isinstance(version, str) or not version.strip() or version.casefold() in {"latest", "current"}:
        raise HostArmBindingError("fixed host version is required")
    digest = (
        _require_digest(expected_sha256, "host executable digest is invalid")
        if expected_sha256 is not None
        else _sha256(executable)
    )
    return HostExecutable(executable, version.strip(), digest)


def _runtime_python(preferred: Path, fallback: Path) -> Path:
    """Use the pinned TEST interpreter, retaining a test-only fallback for synthetic fixtures."""

    candidate = preferred.expanduser().resolve()
    if candidate.is_file():
        return candidate
    fallback = fallback.expanduser().resolve()
    if fallback.is_file():
        return fallback
    raise HostArmBindingError(f"pinned TEST interpreter is missing: {candidate}")


def _hermes_source(
    root: str | Path | None,
    commit: str | None,
    expected_sha256: str | None = None,
) -> dict[str, str]:
    if root is None or commit != FROZEN_HERMES_COMMIT:
        raise HostArmBindingError("Hermes requires the frozen 79445 source binding")
    source_root = _root(root)
    if not source_root.is_dir():
        raise HostArmBindingError("Hermes source root is missing")
    digest = (
        _require_digest(expected_sha256, "Hermes source digest is invalid")
        if expected_sha256 is not None
        else _tree_sha256(source_root)
    )
    return {"path": str(source_root), "sha256": digest, "commit": FROZEN_HERMES_COMMIT}


def _source_binding(
    plan: Mapping[str, Any],
    entry_root: Path,
    arm_id: str,
    candidate_wheel: Path | None,
    candidate_sha256: str | None,
    baseline_source_sha256: str | None = None,
) -> dict[str, Any]:
    arm = plan.get("arm")
    if not isinstance(arm, Mapping):
        raise HostArmBindingError("arm metadata is missing")
    code_source = arm.get("code_source")
    if not isinstance(code_source, Mapping):
        raise HostArmBindingError("arm code source is missing")

    if arm_id == "A":
        return {
            "mode": "HOST_NATIVE_MEMORY",
            "scope_recall_enabled": False,
            "scope_recall_source": None,
            "host_native_memory_enabled": True,
        }
    if arm_id == "B":
        if plan.get("host_id") == "codex_windows_desktop":
            return {"mode": "UNSUPPORTED", "reason": "baseline_578b_is_Hermes_only; Codex substitution forbidden"}
        if code_source.get("baseline_ref") != BASELINE_578B or code_source.get("status") != "PASS":
            raise HostArmBindingError("B requires the verified 578b archive")
        archive_root = _under(Path(str(code_source.get("archive_root", ""))), entry_root, "B archive must remain in this arm entry")
        actual_tree = (
            _require_digest(baseline_source_sha256, "B preverified archive digest is invalid")
            if baseline_source_sha256 is not None
            else _tree_sha256(archive_root)
        )
        if actual_tree != _require_digest(code_source.get("source_sha256"), "B archive digest is invalid"):
            raise HostArmBindingError("B archive digest mismatch")
        return {
            "mode": "FROZEN_BASELINE_578B",
            "baseline_ref": BASELINE_578B,
            "archive": {"path": str(archive_root), "sha256": actual_tree},
            "candidate_wheel": None,
        }
    if arm_id == "C":
        if candidate_wheel is None or candidate_sha256 is None:
            raise HostArmBindingError("C requires an explicit frozen candidate wheel and digest")
        wheel = _root(candidate_wheel)
        if wheel.suffix.casefold() != ".whl":
            raise HostArmBindingError("C candidate must be a wheel")
        return {
            "mode": "FROZEN_CANDIDATE_WHEEL",
            "candidate_wheel": _file_ref(wheel, candidate_sha256),
            "baseline_archive": None,
        }
    if arm_id == "D":
        archive_value = plan.get("storage", {}).get("archive_path") if isinstance(plan.get("storage"), Mapping) else None
        if not isinstance(archive_value, str) or not archive_value:
            raise HostArmBindingError("D archive path is missing")
        archive = _under(Path(archive_value), entry_root, "D archive must remain in this arm entry")
        expected = plan.get("storage", {}).get("archive_sha256")
        return {
            "mode": "ARCHIVE_SIMPLE_SEARCH",
            "archive": _file_ref(archive, expected),
            "simple_search_only": True,
            "candidate_wheel": None,
        }
    raise HostArmBindingError("unsupported arm")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _tree_ref(path: Path) -> dict[str, str]:
    if not path.is_dir():
        raise HostArmBindingError("loader directory is missing")
    return {"path": str(path), "sha256": _tree_sha256(path)}


def _safe_extract_wheel(wheel: Path, destination: Path) -> None:
    """Extract a frozen wheel into this TEST binding without following paths."""

    try:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            if not any(name.startswith("scope_recall/") for name in names):
                raise HostArmBindingError("candidate wheel lacks scope_recall package")
            for name in names:
                member = Path(name)
                if member.is_absolute() or ".." in member.parts:
                    raise HostArmBindingError("candidate wheel path escapes TEST site-packages")
                target = (destination / member).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise HostArmBindingError("candidate wheel path escapes TEST site-packages")
                if name.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(name))
    except zipfile.BadZipFile as exc:
        raise HostArmBindingError("candidate wheel is not a readable wheel archive") from exc


def _hermes_config(home: Path, arm_id: str, workspace: Path | None = None, *, cli: bool = False) -> dict[str, Any]:
    workspace = (workspace or home).resolve()
    provider_name = "native" if arm_id == "A" else ("archive_simple_search" if arm_id == "D" else "scope_recall")
    enabled_plugins = [] if cli else ["platforms/a2a"]
    if arm_id in {"B", "C"}:
        enabled_plugins.append("scope_recall")
    elif arm_id == "D":
        enabled_plugins.append("archive_simple_search")
    return {
        "model": {
            "default": _HERMES_MAIN_MODEL,
            "provider": "p11-deepseek-flash",
            "max_tokens": 4096,
            "context_length": 131072,
            "streaming": False,
        },
        "custom_providers": [{
            "name": "p11-deepseek-flash",
            "base_url": f"http://127.0.0.1:{_HERMES_BRIDGE_PORT}/v1",
            "key_env": "SCOPE_RECALL_TEST_LOCAL_BRIDGE_TOKEN",
            "api_mode": "chat_completions",
            "model": _HERMES_MAIN_MODEL,
            "extra_body": {"thinking": {"type": "disabled"}},
            "models": {_HERMES_MAIN_MODEL: {"context_length": 131072}},
        }],
        "agent": {"max_turns": 3, "api_max_retries": 0, "verbose": False,
                  "system_prompt": "TEST_SCOPE_RECALL TEST only. Do not disclose credentials or leave the TEST instance."},
        "memory": {"memory_enabled": arm_id == "A", "user_profile_enabled": arm_id == "A",
                   "nudge_interval": 0, "provider": provider_name},
        "auxiliary": {"title_generation": {"enabled": False}},
        "plugins": {"enabled": enabled_plugins},
        "session_reset": {"mode": "idle", "idle_minutes": 1, "notify": False},
        "gateway": {"platforms": {"a2a": {"enabled": not cli, "extra": {"host": "127.0.0.1", "port": _HERMES_A2A_PORT}}}},
        "platform_toolsets": {"cli" if cli else "a2a": [*_HERMES_NORMAL_TOOLSETS, "memory"]},
        # Hermes home owns profile/config/SQLite state; the tool cwd is a
        # separate binding workspace so J03 git operations cannot add state.
        "terminal": {"backend": "local", "cwd": str(workspace)},
        "compression": {"enabled": False},
        "fallback_model": [],
    }


def _hermes_installation(home: Path, arm_id: str, host_context_id: str, *, cli: bool = False) -> dict[str, Any]:
    """Run the real trusted Hermes installer, including Core DB initialization."""

    from scope_recall.adapters.hermes.installation import (
        build_installation_manifest,
        install_hermes_scope_recall,
        load_installation_manifest,
        manifest_payload,
    )

    provisional = build_installation_manifest(
        home,
        agent_id="default",
        platform="cli" if cli else "a2a",
        user_id="local" if cli else "TEST-user",
        agent_workspace="hermes",
        conversation_key=host_context_id,
        test_mode=True,
    )
    conversation_scope = provisional.audience_scopes["owner_private" if cli else "conversation"]
    # Frozen Hermes A2A emits an unthreaded DM with the exact context as its
    # chat id.  Preserve that identity mapping; do not synthesize "main" or
    # widen it to owner/project scopes.
    audiences = [{
        "platform": "cli" if cli else "a2a",
        "chat_type": "cli" if cli else "dm",
        "chat_id": "local" if cli else host_context_id,
        "thread_id": "main" if cli else "",
        "agent_workspace": "hermes",
        "allowed_scope_ids": [conversation_scope],
        "capture_scope_id": conversation_scope,
        "kind": "conversation",
    }]
    install_hermes_scope_recall(
        home,
        agent_id="default",
        platform="cli" if cli else "a2a",
        user_id="local" if cli else "TEST-user",
        agent_workspace="hermes",
        conversation_key=host_context_id,
        audiences=None if cli else audiences,
        test_mode=True,
    )
    manifest = load_installation_manifest(home)
    path = manifest.data_directory / "installation.json"
    return {
        "manifest_path": str(path),
        "manifest_sha256": _sha256(path),
        "payload": manifest_payload(manifest),
    }


def _read_runtime_template(path: Path) -> dict[str, Any]:
    """Read a public runtime template without importing or exposing credentials."""

    if not path.is_file():
        raise HostArmBindingError(f"runtime template is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HostArmBindingError(f"runtime template is invalid: {path.name}") from exc
    if not isinstance(payload, dict):
        raise HostArmBindingError("runtime template root must be an object")
    _assert_no_secrets(payload)
    return payload


def _materialize_candidate_runtime_config(
    home: Path,
    installation_payload: Mapping[str, Any],
    *,
    session_id: str,
    base_template: Path = _P11_RUNTIME_TEMPLATE,
) -> dict[str, Any]:
    """Materialize the real P11/P12 runtime shape against this isolated binding."""

    hermes_template = _read_runtime_template(base_template)
    codex_template = _read_runtime_template(_P12_RUNTIME_TEMPLATE)
    payload = dict(hermes_template)
    payload["binding"] = dict(installation_payload)
    payload["session_id"] = session_id
    payload["allowed_scope_ids"] = list(installation_payload["scope_ids"])
    payload["actor_origin"] = "human_direct"

    data_directory = Path(str(installation_payload["data_directory"])).expanduser().resolve()
    auxiliary = dict(payload.get("auxiliary") or {})
    auxiliary["installation_dir"] = str(data_directory)
    # The binding-owned installation/data roots may move per arm, but the
    # model-budget ledger is one explicit shared authority.  Never silently
    # mint a per-arm ledger: an external route without a declared ledger is an
    # invalid contract and must fail closed before launch.
    external_enabled = bool(auxiliary.get("external_embedding")) or bool(auxiliary.get("external_consolidation"))
    template_ledger = auxiliary.get("ledger_path")
    if external_enabled and (type(template_ledger) is not str or not template_ledger.strip()):
        raise HostArmBindingError("external auxiliary requires explicit shared ledger_path")
    if type(template_ledger) is str and template_ledger.strip():
        auxiliary["ledger_path"] = str(Path(template_ledger).expanduser().resolve())
    else:
        auxiliary.pop("ledger_path", None)
    consolidation = dict(auxiliary.get("consolidation") or {})
    if consolidation:
        headers = dict(consolidation.get("headers") or {})
        if not any(name.casefold() == "x-opencode-session" for name in headers):
            headers["x-opencode-session"] = f"scope-recall-test-p18-{session_id}"
        consolidation["headers"] = headers
        auxiliary["consolidation"] = consolidation
    payload["auxiliary"] = auxiliary

    # P11 is the Hermes reference; P12 supplies the frozen vector shape. The
    # storage path is rewritten to the binding-owned companion required by the
    # RuntimeInstance validator, never to the public fixture's external tree.
    vector = dict(codex_template.get("vector") or {})
    from scope_recall.core.recall_policy import SPACE_ID

    vector["storage_dir"] = str(data_directory / "vectors" / SPACE_ID)
    vector["table_name"] = f"TEST_P18_{str(installation_payload['agent_id']).replace('-', '_')}"
    vector["dimensions"] = 3072
    vector["metric"] = "cosine"
    vector["test_injection_override"] = False
    payload["vector"] = vector
    payload["vector_threshold"] = hermes_template.get(
        "vector_threshold", codex_template.get("vector_threshold")
    )
    return payload


def _archive_source_reader() -> str:
    """Shared standalone D reader; retain every source row and its identity."""
    return '''def _archive_rows(archive, capture_archive):
    seen_files = set()
    for source in (archive, capture_archive):
        identity = source.resolve()
        if identity in seen_files or not source.is_file():
            continue
        seen_files.add(identity)
        for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(row, dict):
                # Never deduplicate by text: distinct sources/roles may agree.
                # The original row stays unchanged; path+line binds its origin.
                yield (str(identity), line_number), row
'''


def _archive_simple_plugin(archive: Path, capture_archive: Path) -> str:
    """Return a real Hermes plugin searching initial and captured archives."""

    archive_literal = repr(str(archive))
    capture_literal = repr(str(capture_archive))
    return f'''"""P18 D: deterministic archive-only Hermes MemoryProvider."""
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any

try:
    from agent.memory_provider import MemoryProvider
except ImportError:
    class MemoryProvider:  # type: ignore[no-redef]
        pass

ARCHIVE = Path({archive_literal})
CAPTURE_ARCHIVE = Path({capture_literal})

{_archive_source_reader()}

def _terms(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(re.findall(r"[\\w\\u4e00-\\u9fff]+", value.casefold())))

class ArchiveSimpleSearchProvider(MemoryProvider):
    @property
    def name(self) -> str:
        return "archive-simple-search"

    def is_available(self) -> bool:
        return ARCHIVE.is_file()

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self.session_id = session_id
        self.context = {{key: kwargs.get(key) for key in ("platform", "chat_type", "chat_id", "thread_id", "hermes_home")}}

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self.is_available() or not isinstance(query, str):
            return ""
        terms = _terms(query)
        if not terms:
            return ""
        rows = []
        for identity, row in _archive_rows(ARCHIVE, CAPTURE_ARCHIVE):
            text = " ".join(str(event.get("text", "")) for event in row.get("history", []) if isinstance(event, dict))
            if all(term in text.casefold() for term in terms):
                rows.append(text)
        return "\\n".join(rows[:3])

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", messages: Any = None) -> None:
        CAPTURE_ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
        row = {{
            "session_id": session_id,
            "history": [
                {{"role": "user", "text": user_content}},
                {{"role": "assistant", "text": assistant_content}},
            ],
        }}
        with CAPTURE_ARCHIVE.open("a", encoding="utf-8", newline="\\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\\n")

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return []

    def shutdown(self) -> None:
        return None

def register(ctx: Any) -> ArchiveSimpleSearchProvider:
    provider = ArchiveSimpleSearchProvider()
    ctx.register_memory_provider(provider)
    return provider
'''


def _codex_hook_command(
    python_executable: Path,
    script: Path,
    archive: Path,
    capture_archive: Path,
) -> tuple[str, str]:
    argv = [
        str(python_executable), "-I", "-B", str(script),
        "--archive", str(archive), "--capture-archive", str(capture_archive),
    ]
    posix = shlex.join(argv)
    quoted = ["'" + value.replace("'", "''") + "'" for value in argv]
    encoded = base64.b64encode(("& " + " ".join(quoted) + "; exit $LASTEXITCODE").encode("utf-16le")).decode("ascii")
    return posix, "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + encoded


def _simple_search_codex_hook(archive: Path, capture_archive: Path) -> str:
    return f'''"""P18 D Codex native-hook archive search; no Scope Recall imports."""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

ARCHIVE = Path({repr(str(archive))})
CAPTURE_ARCHIVE = Path({repr(str(capture_archive))})

{_archive_source_reader()}

def terms(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(re.findall(r"[\\w\\u4e00-\\u9fff]+", value.casefold())))

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--capture-archive", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload = json.loads(sys.stdin.buffer.read(65536).decode("utf-8"))
    except (UnicodeError, ValueError):
        print("{{}}", end="")
        return 0
    prompt = payload.get("prompt") or payload.get("user_message") or payload.get("userMessage") or ""
    if not isinstance(prompt, str) or not args.archive.is_file():
        print("{{}}", end="")
        return 0
    # Search the prior snapshot, not the current query just being captured.
    prior_rows = list(_archive_rows(args.archive, args.capture_archive))
    args.capture_archive.parent.mkdir(parents=True, exist_ok=True)
    with args.capture_archive.open("a", encoding="utf-8", newline="\\n") as handle:
        handle.write(json.dumps({{"event": payload.get("hook_event_name") or payload.get("hookEventName") or "unknown", "history": [{{"role": "user", "text": prompt}}]}}, ensure_ascii=True, separators=(",", ":")) + "\\n")
    wanted = terms(prompt)
    hits = []
    for identity, row in prior_rows:
        text = " ".join(str(event.get("text", "")) for event in row.get("history", []) if isinstance(event, dict))
        if wanted and all(term in text.casefold() for term in wanted):
            hits.append(text)
    result = {{}}
    if hits:
        result = {{"hookSpecificOutput": {{"hookEventName": "UserPromptSubmit", "additionalContext": "\\n".join(hits[:3])}}}}
    print(json.dumps(result, ensure_ascii=True), end="")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
'''


def _prepare_hermes(
    entry_root: Path,
    plan: Mapping[str, Any],
    source: Mapping[str, Any],
    executable: HostExecutable,
    binding_root: Path,
    host_source: Mapping[str, str],
    host_context_id: str,
) -> dict[str, Any]:
    arm_id = str(plan["arm_id"])
    host_id = str(plan["host_id"])
    cli = host_id == "hermes_cli_local_input_v1"
    home = binding_root / "hermes-home"
    workspace = binding_root / "hermes-workspace"
    state = binding_root / "hermes-state"
    home.mkdir()
    workspace.mkdir()
    state.mkdir()
    hermes_python = _runtime_python(_HERMES_PYTHON, executable.path)
    config_path = home / "config.yaml"
    _write_json(config_path, _hermes_config(home, arm_id, workspace, cli=cli))
    environment: dict[str, str] = {
        "HERMES_HOME": str(home),
        "A2A_HOST": "127.0.0.1",
        "A2A_PORT": str(_HERMES_A2A_PORT),
        "A2A_CONTEXT_ID": host_context_id,
        "A2A_AGENT_NAME": f"TEST-P18-{arm_id}",
        "A2A_ALLOW_ALL_USERS": "true",
        "NO_PROXY": "localhost,127.0.0.1",
    }
    if cli:
        environment = {key: value for key, value in environment.items() if not key.startswith("A2A_")}
    loader: dict[str, Any]
    python_paths = [str(Path(host_source["path"]).resolve())]
    if arm_id == "A":
        loader = {"registration": "none", "plugin_directory": None, "module_search_path": None}
    elif arm_id == "B":
        archive_root = Path(str(source["archive"]["path"]))
        plugin_dir = home / "plugins" / "scope_recall"
        shutil.copytree(archive_root, plugin_dir, copy_function=_hardlink_or_copy)
        python_paths.append(str(plugin_dir))
        environment["PYTHONPATH"] = os.pathsep.join(python_paths)
        loader = {
            "registration": "Hermes plugin loader",
            "plugin_directory": {"path": str(plugin_dir), "sha256": str(source["archive"]["sha256"])},
            "plugin_manifest": _file_ref(plugin_dir / "plugin.yaml"),
            "module_search_path": str(plugin_dir),
            "entrypoint": "__init__.py:register",
        }
    elif arm_id == "C":
        wheel = Path(str(source["candidate_wheel"]["path"]))
        site = binding_root / "candidate-site-packages"
        site.mkdir()
        _safe_extract_wheel(wheel, site)
        plugin_dir = home / "plugins" / "scope_recall"
        plugin_dir.mkdir(parents=True)
        distribution = Path(__file__).parents[2] / "distribution" / "hermes"
        shutil.copyfile(distribution / "__init__.py", plugin_dir / "__init__.py")
        # Keep the frozen, user-supplied Hermes manifest as the authoritative
        # loader contract; the candidate wheel supplies only the adapter code.
        shutil.copyfile(_P11_PLUGIN_MANIFEST, plugin_dir / "plugin.yaml")
        python_paths.append(str(site))
        environment["PYTHONPATH"] = os.pathsep.join(python_paths)
        loader = {
            "registration": "Hermes plugin loader",
            "plugin_directory": _tree_ref(plugin_dir),
            "plugin_manifest": _file_ref(plugin_dir / "plugin.yaml"),
            "module_search_path": str(site),
            "entrypoint": "register:register",
            "candidate_install": _tree_ref(site),
        }
    else:
        archive = Path(str(source["archive"]["path"]))
        plugin_dir = home / "plugins" / "archive_simple_search"
        plugin_dir.mkdir(parents=True)
        capture_archive = state / "captured-input.jsonl"
        (plugin_dir / "__init__.py").write_text(
            _archive_simple_plugin(archive, capture_archive), encoding="utf-8", newline="\n"
        )
        (plugin_dir / "plugin.yaml").write_text(
            "name: archive_simple_search\nversion: 1.0.0\ndescription: P18 deterministic archive-only memory provider\nhooks: []\n",
            encoding="utf-8",
            newline="\n",
        )
        python_paths.append(str(plugin_dir))
        environment["PYTHONPATH"] = os.pathsep.join(python_paths)
        loader = {
            "registration": "Hermes plugin loader",
            "entrypoint": "__init__.py:register",
            "plugin_directory": _tree_ref(plugin_dir),
            "plugin_manifest": _file_ref(plugin_dir / "plugin.yaml"),
            "module_search_path": str(plugin_dir),
            "archive": _file_ref(archive),
            "capture_archive": {"path": str(capture_archive)},
            "provider": "ArchiveSimpleSearchProvider",
        }
    environment.setdefault("PYTHONPATH", os.pathsep.join(python_paths))
    # The archived CLI B provider owns its legacy memory.sqlite3 schema and
    # derives its own profile principal. Never initialize candidate Core there.
    installation = None if arm_id in {"A", "D"} or (cli and arm_id == "B") else _hermes_installation(home, arm_id, host_context_id, cli=cli)
    runtime_config_path = home / "scope-recall" / "runtime-config.json"
    if arm_id == "C":
        runtime_payload = _materialize_candidate_runtime_config(
            home,
            installation["payload"],
            session_id=f"TEST-P18-{host_id}-{arm_id}",
        )
        from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig
        from scope_recall.runtime.model_budget import initialize_auxiliary_budget_ledger

        auxiliary_config = AuxiliaryRuntimeConfig.from_mapping(runtime_payload["auxiliary"])
        if auxiliary_config.ledger_path is not None:
            initialize_auxiliary_budget_ledger(auxiliary_config.ledger_path, auxiliary_config.budget)
    else:
        runtime_payload = {
          "binding": installation["payload"] if installation else {
            "agent_id": f"TEST-P18-hermes-{arm_id}",
            "installation_id": None,
            "data_directory": str(home / "host-native-memory"),
            "scope_ids": [],
            "test_mode": True,
          },
          "session_id": f"TEST-P18-{host_id}-{arm_id}",
          "allowed_scope_ids": installation["payload"]["scope_ids"] if installation else [],
          "actor_origin": "human_direct",
          "host_native_memory_enabled": arm_id == "A",
          "scope_recall_enabled": arm_id != "A",
          "request_seconds": 45.0,
          "drain_seconds": 45.0,
          "max_items": 8,
          "lease_seconds": 60.0,
          "arm_source": dict(source),
          "external_budget_bridge": "P11 Go bridge owns primary reservation and charge",
        }
    runtime_config_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(runtime_config_path, runtime_payload)
    environment_path = binding_root / "environment.json"
    _write_json(environment_path, environment)
    argv = ([str(hermes_python), "-B", "-m", "hermes_cli.main", "--cli", "chat", "-Q",
             "--model", _HERMES_MAIN_MODEL, "--provider", "p11-deepseek-flash"] if cli else
            [str(hermes_python), "-B", "-m", "hermes_cli.main", "gateway", "run"])
    return {
        "host_id": host_id,
        "arm_id": arm_id,
        "host_context_id": host_context_id,
        "status": "PREPARED",
        "host_started": False,
        "network_calls": 0,
        "model_calls": 0,
        "semantic_pass": False,
        "fixed_host": {
            "executable_path": str(executable.path),
            "executable_sha256": executable.sha256,
            "version": executable.version,
            "source": dict(host_source),
            "runtime_python_path": str(hermes_python),
            "runtime_python_sha256": _sha256(hermes_python),
        },
        "source": dict(source),
        "loader": loader,
        "installation_manifest": installation,
        "roots": {
            "binding_root": str(binding_root),
            "home_path": str(home),
            "workspace_path": str(workspace),
            "state_path": str(state),
            "database_path": str(home / "scope-recall" / "memory.sqlite3") if installation or (cli and arm_id == "B") else str(home / "host-native-memory" / "native.sqlite3"),
            "environment_path": str(environment_path),
            "runtime_config_path": str(runtime_config_path),
            "config_path": str(config_path),
        },
        "launch_contract": {
            "argv": argv,
            "working_directory": str(workspace),
            "environment_path": str(environment_path),
            "config_path": str(config_path),
            "host_context_id": host_context_id,
            "requires_external_host_driver": True,
            "readiness_required": "owned meter; actual CLI stderr session id and public export" if cli else "Agent Card plus frozen Hermes startup restore marker",
            "entry": "hermes_cli_local_input_v1" if cli else "hermes_a2a",
            "config_artifact": _file_ref(config_path),
            "environment_artifact": _file_ref(environment_path),
            "runtime_config_artifact": _file_ref(runtime_config_path),
            "post_turn_settle_seconds": 60 if cli else None,
        },
    }


def _prepare_codex(
    entry_root: Path,
    plan: Mapping[str, Any],
    source: Mapping[str, Any],
    executable: HostExecutable,
    binding_root: Path,
) -> dict[str, Any]:
    host_id = str(plan["host_id"])
    arm_id = str(plan["arm_id"])
    state = binding_root / "codex-state"
    state.mkdir()
    (state / 'Temp').mkdir()
    (state / 'Tmp').mkdir()
    environment_path = binding_root / "codex-environment.json"
    environment = {
        "HOME": str(binding_root / "codex-home"),
        "USERPROFILE": str(binding_root / "codex-home"),
        "CODEX_HOME": str(binding_root / "codex-home"),
        "APPDATA": str(binding_root / "codex-home" / "AppData"),
        "LOCALAPPDATA": str(binding_root / "codex-home" / "LocalAppData"),
        "TEMP": str(state / "Temp"),
        "TMP": str(state / "Tmp"),
        "P18_HOST_ID": host_id,
        "P18_ARM_ID": arm_id,
    }
    _write_json(environment_path, environment)
    base = {
        "host_id": host_id,
        "arm_id": arm_id,
        "status": "UNSUPPORTED" if arm_id == "B" else "NOT_READY",
        "formal_execution_started": False,
        "network_calls": 0,
        "model_calls": 0,
        "host_started": False,
        "sealed_raw_or_gold_opened": False,
        "semantic_pass": False,
        "source": dict(source),
        "fixed_host": {"executable_path": str(executable.path), "executable_sha256": executable.sha256, "version": executable.version},
        "roots": {"binding_root": str(binding_root), "state_path": str(state), "environment_path": str(environment_path)},
        "launch_contract": {"cli_substitution_forbidden": True, "requires_actual_desktop_ui": True},
    }
    if arm_id == "B":
        base["reason"] = "baseline_578b_is_Hermes_only; Codex substitution forbidden"
    elif arm_id == "A":
        home = binding_root / "codex-home"
        project_root = binding_root / "codex-project"
        profile_path = binding_root / "codex-appserver-profile.json"
        home.mkdir()
        project_root.mkdir()
        _write_codex_workspace_write_home(home, project_root)
        profile = {
            "schema": "scope-recall.p18.codex-appserver-profile.v1",
            "entry": "official_codex_appserver",
            "server_args": ["app-server"],
            "model": "gpt-5.6-luna",
            "effort": "low",
            "arm": "A",
            "native_memory": True,
            "scope_recall_enabled": False,
            "scope_recall_hooks": [],
            "cli_substitution_forbidden": True,
        }
        _write_json(profile_path, profile)
        base.update(
            {
                "status": "PREPARED",
                "reason": "official Codex app-server profile; native memory enabled and Scope Recall hooks disabled",
                "roots": {
                    "binding_root": str(binding_root),
                    "home_path": str(home),
                    "state_path": str(state),
                    "database_path": str(home / "native-memory" / "memory.sqlite3"),
                    "environment_path": str(environment_path),
                    "appserver_profile_path": str(profile_path),
                    "project_root": str(project_root),
                },
                "loader": {
                    "registration": "official Codex app-server",
                    "scope_recall_hooks": [],
                    "profile": _file_ref(profile_path),
                },
                "launch_contract": {
                    "argv": [str(executable.path), "app-server"],
                    "working_directory": str(project_root),
                    "environment_path": str(environment_path),
                    "profile_path": str(profile_path),
                    "requires_actual_desktop_ui": True,
                    "cli_substitution_forbidden": True,
                    "hook_policy": "A: Scope Recall disabled; native memory only",
                },
            }
        )
    elif arm_id == "C":
        instance_root = binding_root / "codex-instance"
        project_root = binding_root / "codex-project"
        plugin_dir = binding_root / "codex-plugin"
        site_packages = binding_root / "candidate-site-packages"
        instance_root.mkdir()
        project_root.mkdir()
        plugin_dir.mkdir()
        site_packages.mkdir()
        _write_codex_workspace_write_home(binding_root / "codex-home", project_root)
        wheel = Path(str(source["candidate_wheel"]["path"]))
        _safe_extract_wheel(wheel, site_packages)
        candidate_python = _runtime_python(_CANDIDATE_PYTHON, executable.path)
        try:
            # The formal candidate is an installed wheel and therefore only
            # exposes the package-qualified maintenance module.  Importing the
            # source-tree alias here made Codex arm C look prepared when the
            # matrix happened to be built from the repository, but fail in the
            # isolated candidate interpreter.
            from scope_recall.maintenance.install import (
                _codex_mcp_json,
                _codex_hooks_json,
                _codex_plugin_json,
                _write_windows_hook_launcher,
            )
            from scope_recall.adapters.codex.config import install_codex_scope_recall

            config, _core = install_codex_scope_recall(
                instance_root,
                project_root=project_root,
                agent_id="TEST-P18-codex-C",
                test_mode=True,
            )
            launcher = plugin_dir / "hooks" / "scope-recall-hook.cmd"
            _write_windows_hook_launcher(launcher, candidate_python, config.config_path)
            files = {
                plugin_dir / ".codex-plugin" / "plugin.json": _codex_plugin_json(plugin_dir.name),
                plugin_dir / "hooks" / "hooks.json": _codex_hooks_json(
                    candidate_python, config.config_path, windows_launcher=launcher
                ),
                plugin_dir / ".mcp.json": _codex_mcp_json(candidate_python, config.config_path, project_root),
            }
            for path, payload in files.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            base["reason"] = f"Codex candidate offline install unavailable:{type(exc).__name__}:{str(exc)[:1000]}"
            return base
        config_path = instance_root / "codex-installation.json"
        installation_payload = json.loads(config_path.read_text(encoding="utf-8"))
        runtime_payload = _materialize_candidate_runtime_config(
            instance_root / "data",
            {
                "agent_id": installation_payload["agent_id"],
                "installation_id": installation_payload["installation_id"],
                "data_directory": installation_payload["data_directory"],
                "scope_ids": installation_payload["scope_ids"],
                "test_mode": installation_payload["test_mode"],
            },
            session_id="TEST-P18-codex-C",
            base_template=_P12_RUNTIME_TEMPLATE,
        )
        runtime_config_path = instance_root / "data" / "runtime-config.json"
        _write_json(runtime_config_path, runtime_payload)
        from scope_recall.runtime.auxiliary import AuxiliaryRuntimeConfig
        from scope_recall.runtime.model_budget import initialize_auxiliary_budget_ledger

        auxiliary_config = AuxiliaryRuntimeConfig.from_mapping(runtime_payload["auxiliary"])
        if auxiliary_config.ledger_path is not None:
            initialize_auxiliary_budget_ledger(auxiliary_config.ledger_path, auxiliary_config.budget)
        environment["PYTHONPATH"] = str(site_packages)
        _write_json(environment_path, environment)
        base.update(
            {
                "status": "PREPARED",
                "reason": "native hooks/config installed offline; fixed host interpreter must resolve candidate wheel before runtime",
                "roots": {
                    "binding_root": str(binding_root),
                    "home_path": str(binding_root / "codex-home"),
                    "state_path": str(state),
                    "database_path": str(instance_root / "data" / "memory.sqlite3"),
                    "environment_path": str(environment_path),
                    "installation_config_path": str(config_path),
                    "runtime_config_path": str(runtime_config_path),
                    "plugin_directory": str(plugin_dir),
                    "candidate_site_packages": str(site_packages),
                    "project_root": str(project_root),
                },
                "loader": {
                    "registration": "Codex installer native hooks",
                    "installation_config": _file_ref(config_path),
                    "hooks": _file_ref(plugin_dir / "hooks" / "hooks.json"),
                    "plugin_manifest": _file_ref(plugin_dir / ".codex-plugin" / "plugin.json"),
                    "candidate_install": _tree_ref(site_packages),
                    "module_search_path": str(site_packages),
                    "candidate_python": _file_ref(candidate_python),
                    "runtime_config": _file_ref(runtime_config_path),
                    "candidate_resolution": "fixed candidate interpreter owns the installed wheel; hooks use -I and ignore PYTHONPATH",
                },
                "launch_contract": {
                    "argv": [str(executable.path), "app-server"],
                    "working_directory": str(project_root),
                    "environment_path": str(environment_path),
                    "installation_config_path": str(config_path),
                    "runtime_config_path": str(runtime_config_path),
                    "plugin_directory": str(plugin_dir),
                    "requires_actual_desktop_ui": True,
                    "cli_substitution_forbidden": True,
                    "runtime_candidate_import_check_required": True,
                },
            }
        )
    elif arm_id == "D":
        archive = Path(str(source["archive"]["path"]))
        home = binding_root / "codex-home"
        project_root = binding_root / "codex-project"
        plugin_dir = binding_root / "codex-plugin"
        home.mkdir()
        project_root.mkdir()
        plugin_dir.mkdir()
        _write_codex_workspace_write_home(home, project_root)
        capture_archive = state / "captured-input.jsonl"
        script = plugin_dir / "simple-search-hook.py"
        script.write_text(
            _simple_search_codex_hook(archive, capture_archive), encoding="utf-8", newline="\n"
        )
        candidate_python = _runtime_python(_CANDIDATE_PYTHON, executable.path)
        posix, windows = _codex_hook_command(candidate_python, script, archive, capture_archive)
        events = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "Interrupt", "SessionEnd")
        hooks = {
            "hooks": {
                event: [{"hooks": [{"type": "command", "command": posix, "commandWindows": windows, "timeout": 2}]}]
                for event in events
            }
        }
        hooks_path = plugin_dir / "hooks" / "hooks.json"
        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(hooks_path, hooks)
        plugin_manifest_path = plugin_dir / ".codex-plugin" / "plugin.json"
        plugin_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(
            plugin_manifest_path,
            {
                "name": "p18-simple-search",
                "version": "1.0.0",
                "description": "P18 deterministic archive-only native hook baseline",
                "interface": {"capabilities": []},
            },
        )
        profile_path = binding_root / "codex-simple-search-profile.json"
        _write_json(
            profile_path,
            {
                "schema": "scope-recall.p18.codex-simple-search-profile.v1",
                "entry": "native_hooks",
                "archive": _file_ref(archive),
                "hook_script": _file_ref(script),
                "scope_recall_enabled": False,
                "simple_search_only": True,
            },
        )
        base.update(
            {
                "status": "PREPARED",
                "reason": "official Codex native hooks invoke isolated archive-only simple search; no Scope Recall import",
                "roots": {
                    "binding_root": str(binding_root),
                    "home_path": str(home),
                    "state_path": str(state),
                    "database_path": str(state / "simple-search.sqlite3"),
                    "environment_path": str(environment_path),
                    "plugin_directory": str(plugin_dir),
                    "profile_path": str(profile_path),
                    "project_root": str(project_root),
                },
                "loader": {
                    "registration": "Codex native hooks",
                    "hooks": _file_ref(hooks_path),
                    "plugin_manifest": _file_ref(plugin_manifest_path),
                    "hook_script": _file_ref(script),
                    "profile": _file_ref(profile_path),
                    "archive": _file_ref(archive),
                    "capture_archive": {"path": str(capture_archive)},
                    "candidate_python": _file_ref(candidate_python),
                },
                "launch_contract": {
                    "argv": [str(executable.path)],
                    "working_directory": str(project_root),
                    "environment_path": str(environment_path),
                    "plugin_directory": str(plugin_dir),
                    "profile_path": str(profile_path),
                    "requires_actual_desktop_ui": True,
                    "cli_substitution_forbidden": True,
                    "simple_search_only": True,
                },
            }
        )
    else:
        base["reason"] = "unsupported Codex arm"
    return base


def prepare_host_arm_binding(
    arm_dir: str | Path,
    *,
    host_executable: str | Path,
    host_version: str,
    host_executable_sha256: str | None = None,
    host_source_root: str | Path | None = None,
    host_source_commit: str | None = None,
    host_source_sha256: str | None = None,
    candidate_wheel: str | Path | None = None,
    candidate_sha256: str | None = None,
    baseline_source_sha256: str | None = None,
    host_context_id: str | None = None,
) -> dict[str, Any]:
    """Create one immutable-ish TEST launch contract without starting it.

    The downstream runner contract is ``binding_manifest_path`` plus the
    referenced ``runtime_config_path`` and ``environment_path``.  It must use
    the fixed executable/version and preserve all paths; it must not substitute
    a CLI for Codex desktop or infer a formal PASS from this receipt.
    """

    entry_root = _root(arm_dir)
    plan = _load_plan(entry_root)
    executable = _host_executable(host_executable, host_version, host_executable_sha256)
    arm_id = str(plan["arm_id"])
    host_id = str(plan["host_id"])
    resolved_host_context_id = str(host_context_id or f"TEST-context-{host_id}-{arm_id}").strip()
    if not resolved_host_context_id or resolved_host_context_id.casefold() == "unknown":
        raise HostArmBindingError("explicit host_context_id is required")
    host_source = (
        _hermes_source(host_source_root, host_source_commit, host_source_sha256)
        if host_id in {"hermes_a2a", "hermes_cli_local_input_v1"}
        else None
    )
    source = _source_binding(
        plan,
        entry_root,
        arm_id,
        Path(candidate_wheel).expanduser().resolve() if candidate_wheel is not None else None,
        candidate_sha256,
        baseline_source_sha256,
    )

    binding_root = entry_root / "host-binding"
    if binding_root.exists():
        raise HostArmBindingError("host binding already exists; refusing overwrite")
    binding_root.mkdir(parents=True, exist_ok=False)
    if host_id in {"hermes_a2a", "hermes_cli_local_input_v1"}:
        assert host_source is not None
        manifest = _prepare_hermes(entry_root, plan, source, executable, binding_root, host_source, resolved_host_context_id)
    else:
        manifest = _prepare_codex(entry_root, plan, source, executable, binding_root)
    manifest.update(
        {
            "schema": SCHEMA,
            "formal_execution_started": False,
            "sealed_raw_or_gold_opened": False,
        }
    )
    _assert_no_secrets(manifest)
    manifest_path = binding_root / "host-binding.json"
    _write_json(manifest_path, manifest)
    manifest["binding_manifest_path"] = str(manifest_path)
    return manifest


__all__ = ["BASELINE_578B", "HostArmBindingError", "HostExecutable", "prepare_host_arm_binding"]
