"""Codex-launched processes receive credentials from a file, not from Codex.

Codex starts the MCP server and every hook with its own environment, which
carries none of the credential names the runtime config declares.  The worker
already reads them from its autostart control file; these tests pin the same
contract for the two host entries: only declared names, absolute paths only,
and a missing key costs the semantic channel but never the process.
"""
from __future__ import annotations

import io
import os
import types
from pathlib import Path

import pytest

from scope_recall.adapters.codex import hook_entry, mcp_entry
from scope_recall.runtime import resume_entry

KEY = "SCOPE_RECALL_TEST_EMBED_KEY"


def _stub_runtime_config(*names: str):
    routes = [types.SimpleNamespace(credential_env=name) for name in names]
    auxiliary = types.SimpleNamespace(
        embedding=routes[0] if routes else None,
        consolidation=routes[1] if len(routes) > 1 else None,
    )
    return types.SimpleNamespace(auxiliary=auxiliary)


def test_host_process_credential_environment_reads_only_declared_names(tmp_path: Path, monkeypatch) -> None:
    runtime_config = tmp_path / "runtime-config.json"
    runtime_config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(resume_entry, "load_config", lambda path: _stub_runtime_config(KEY))
    env_file = tmp_path / "embedding.env"
    env_file.write_text(
        "# comment\n"
        f"export {KEY}='secret-value'  \n"
        "SCOPE_RECALL_UNDECLARED=leak\n"
        "PATH=/tmp/not-touched\n",
        encoding="utf-8",
    )

    loaded = resume_entry.host_process_credential_environment(runtime_config, env_file)

    assert loaded == {KEY: "secret-value"}
    with pytest.raises(ValueError, match="runtime_config_path_not_absolute"):
        resume_entry.host_process_credential_environment(Path("relative/runtime-config.json"), env_file)
    with pytest.raises(ValueError, match="autostart_environment_invalid"):
        resume_entry.host_process_credential_environment(runtime_config, Path("relative.env"))


def _install(tmp_path: Path):
    from scope_recall.adapters.codex import install_codex_scope_recall

    project_root = tmp_path / "TEST-project"
    project_root.mkdir()
    config, _core = install_codex_scope_recall(tmp_path / "install", project_root=project_root)
    return config, project_root


def test_mcp_entry_env_file_populates_environment_before_server_build(tmp_path: Path, monkeypatch, capsys) -> None:
    config, project_root = _install(tmp_path)
    env_file = tmp_path / "embedding.env"
    env_file.write_text(f"{KEY}=from-file\n", encoding="utf-8")
    monkeypatch.setenv(KEY, "placeholder")  # so teardown restores the pre-test state
    seen: dict[str, object] = {}

    def fake_credentials(runtime_config_path, env_path):
        seen["runtime_config_path"] = Path(runtime_config_path)
        seen["env_path"] = Path(env_path)
        return {KEY: "from-file"}

    def fake_build_server(*_args, **_kwargs):
        seen["environment_at_build"] = os.environ.get(KEY)
        return types.SimpleNamespace(server=types.SimpleNamespace(run=lambda transport: seen.setdefault("transport", transport)))

    monkeypatch.setattr(mcp_entry, "host_process_credential_environment", fake_credentials)
    monkeypatch.setattr(mcp_entry, "build_server", fake_build_server)

    code = mcp_entry.main([
        "--config", str(config.config_path),
        "--workspace", str(project_root),
        "--env-file", str(env_file),
    ])

    assert code == 0
    assert seen["runtime_config_path"] == config.data_directory / "runtime-config.json"
    assert seen["env_path"] == env_file.resolve()
    assert seen["environment_at_build"] == "from-file"
    assert seen["transport"] == "stdio"
    assert capsys.readouterr().err == ""


def test_mcp_entry_unreadable_env_file_is_reported_and_server_still_starts(tmp_path: Path, monkeypatch, capsys) -> None:
    config, project_root = _install(tmp_path)
    monkeypatch.setenv(KEY, "placeholder")
    monkeypatch.delenv(KEY)
    started: list[str] = []

    def failing_credentials(runtime_config_path, env_path):
        raise ValueError("autostart_environment_invalid")

    monkeypatch.setattr(mcp_entry, "host_process_credential_environment", failing_credentials)
    monkeypatch.setattr(
        mcp_entry,
        "build_server",
        lambda *a, **k: types.SimpleNamespace(server=types.SimpleNamespace(run=lambda transport: started.append(transport))),
    )

    code = mcp_entry.main([
        "--config", str(config.config_path),
        "--workspace", str(project_root),
        "--env-file", str(tmp_path / "missing.env"),
    ])

    assert code == 0
    assert started == ["stdio"]
    assert KEY not in os.environ
    assert "credential environment unavailable (autostart_environment_invalid)" in capsys.readouterr().err

    with pytest.raises(SystemExit, match="env-file must be absolute"):
        mcp_entry.main([
            "--config", str(config.config_path),
            "--workspace", str(project_root),
            "--env-file", "relative.env",
        ])


def _stub_hook_handler(monkeypatch, seen: dict[str, object]) -> None:
    class Handler:
        diagnostics = None  # emit_result treats a missing diagnostics object as "nothing to report"

        def handle_bytes(self, raw: bytes) -> dict:
            seen["environment_at_handle"] = os.environ.get(KEY)
            return {}

    monkeypatch.setattr(hook_entry.CodexHookHandler, "from_config_path", classmethod(lambda cls, *a, **k: Handler()))
    monkeypatch.setattr(hook_entry.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"{}")))


def test_hook_entry_env_file_loads_credentials_and_never_fails_the_hook(tmp_path: Path, monkeypatch, capsys) -> None:
    config, _project_root = _install(tmp_path)
    env_file = tmp_path / "embedding.env"
    env_file.write_text(f"{KEY}=from-file\n", encoding="utf-8")
    monkeypatch.setenv(KEY, "placeholder")
    seen: dict[str, object] = {}
    _stub_hook_handler(monkeypatch, seen)
    monkeypatch.setattr(hook_entry, "host_process_credential_environment", lambda runtime, env: {KEY: "from-file"})

    assert hook_entry.main(["--config", str(config.config_path), "--env-file", str(env_file)]) == 0
    assert seen["environment_at_handle"] == "from-file"
    assert capsys.readouterr().err == ""

    # An unusable file is a diagnostic, not a failed hook: Codex still gets its JSON answer.
    monkeypatch.delenv(KEY)
    _stub_hook_handler(monkeypatch, seen)

    def failing(runtime, env):
        raise OSError("unreadable")

    monkeypatch.setattr(hook_entry, "host_process_credential_environment", failing)
    assert hook_entry.main(["--config", str(config.config_path), "--env-file", str(tmp_path / "missing.env")]) == 0
    captured = capsys.readouterr()
    assert "CODEX_HOOK:credential_environment_unavailable" in captured.err
    assert captured.out.strip().startswith("{")
    assert seen["environment_at_handle"] is None

    _stub_hook_handler(monkeypatch, seen)
    assert hook_entry.main(["--config", str(config.config_path), "--env-file", "relative.env"]) == 0
    assert "CODEX_HOOK:env_file_not_absolute" in capsys.readouterr().err
