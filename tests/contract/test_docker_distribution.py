"""Contracts for the container entry and its deployment assets."""

import ast
import configparser
import errno
import importlib.metadata
import json
import re
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_container_entry_is_packaged():
    """The image must be able to start its installed entry module."""
    from scope_recall.distribution.docker import entry

    assert callable(entry.main)


@pytest.mark.parametrize("name", ["Dockerfile", "compose.yaml", "supervisord.conf"])
def test_deployment_assets_exist(name):
    """Each deployment asset is included in the source checkout."""
    assert (ROOT / "distribution" / "docker" / name).is_file()


def _entry(home: Path, host: str, listen: str = "100.64.0.10", port: int = 18765) -> None:
    config = home / "scope-recall" / "remote-server.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "schema": "scope-recall.remote-server/1",
                "host": host,
                "listen": listen,
                "port": port,
                "token_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )


def test_two_entries_have_independent_restarting_servers(tmp_path):
    from scope_recall.distribution.docker.entry import discover_entries, render_programs

    _entry(tmp_path / "homes" / "first", "codex")
    _entry(tmp_path / "homes" / "second", "claude-code", port=18766)
    configs = discover_entries(tmp_path / "homes")
    secret = tmp_path / "credentials"
    rendered = render_programs(configs, secret)
    assert len(configs) == 2
    assert {config.port for config in configs} == {18765, 18766}
    assert rendered.count("autorestart=true") == 2
    assert rendered.count("scope_recall.adapters.codex.remote_server serve") == 2
    assert "--host codex" in rendered
    assert "--host claude-code" in rendered
    assert rendered.count(f"--env-file {shlex.quote(str(secret))}") == 2
    sections = configparser.ConfigParser(interpolation=None)
    sections.read_string(rendered)
    for index, config in enumerate(configs):
        assert f"--home {shlex.quote(str(config.home))}" in rendered
        program = sections[f"program:remote-{index}"]
        assert shlex.split(program["command"]) == [
            "/usr/local/bin/python",
            "-m",
            "scope_recall.adapters.codex.remote_server",
            "serve",
            "--home",
            str(config.home),
            "--host",
            config.host,
            "--env-file",
            str(secret),
        ]
        assert program.getboolean("autostart") and program.getboolean("autorestart")
        assert program.getboolean("stopasgroup") and program.getboolean("killasgroup")
        assert program["stdout_logfile"] == "/dev/fd/1"
        assert program["stderr_logfile"] == "/dev/fd/2"


def test_missing_entry_is_an_error(tmp_path):
    from scope_recall.distribution.docker.entry import discover_entries

    with pytest.raises(ValueError, match="docker compose run --rm"):
        discover_entries(tmp_path)


def test_supervisor_accepts_parentheses_in_home_name(tmp_path):
    from scope_recall.distribution.docker.entry import discover_entries, render_programs

    _entry(tmp_path / "client(test)", "codex")
    assert "client(test)" in render_programs(discover_entries(tmp_path), tmp_path / "credentials")


def test_supervisor_reload_keeps_a_low_lock_descriptor(monkeypatch):
    from scope_recall.distribution.docker import entry

    calls = []

    def probe(fd):
        if fd == 3:
            return object()
        raise OSError(errno.EBADF, "closed")

    os_stub = SimpleNamespace(
        fstat=probe, dup2=lambda source, target, inheritable: calls.append((source, target, inheritable))
    )
    monkeypatch.setattr(entry, "os", os_stub)
    entry.preserve_lock_for_exec(7)
    assert calls == [(7, 4, True)]


def test_supervisor_lock_refuses_to_replace_occupied_descriptors(monkeypatch):
    from scope_recall.distribution.docker import entry

    monkeypatch.setattr(entry, "os", SimpleNamespace(fstat=lambda _fd: object()))
    with pytest.raises(RuntimeError, match="cannot preserve"):
        entry.preserve_lock_for_exec(7)


def test_unspecified_listen_address_is_rejected(tmp_path):
    from scope_recall.distribution.docker.entry import discover_entries

    _entry(tmp_path / "first", "codex", listen="0.0.0.0")
    with pytest.raises(ValueError):
        discover_entries(tmp_path)


def test_compose_uses_host_network_volume_and_secret():
    compose = yaml.safe_load((ROOT / "distribution/docker/compose.yaml").read_text(encoding="utf-8"))
    service = compose["services"]["scope-recall"]
    assert service["network_mode"] == "host"
    assert "ports" not in service
    assert "scope-recall-data:/data" in service["volumes"]
    assert "scope-recall-data" in compose["volumes"]
    assert "runtime" in service["secrets"] and "runtime" in compose["secrets"]


def test_dockerfile_uses_nonroot_tini_and_checked_version():
    from scope_recall._version import __version__

    dockerfile = (ROOT / "distribution/docker/Dockerfile").read_text(encoding="utf-8")
    assert "USER 10001:10001" in dockerfile
    assert 'ENTRYPOINT ["tini", "--",' in dockerfile
    assert "LABEL org.opencontainers.image.version=$SCOPE_RECALL_VERSION" in dockerfile
    assert 'importlib.metadata.version("hermes-scope-recall") == os.environ["SCOPE_RECALL_VERSION"]' in dockerfile
    tree = ast.parse((ROOT / "_version.py").read_text(encoding="utf-8"))
    source_version = next(
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
    )
    assert importlib.metadata.version("hermes-scope-recall") == __version__ == source_version
    assert re.search(r"FROM python:3\.12\.\d+-slim", dockerfile)
    for path in (ROOT / "distribution/docker/compose.yaml", ROOT / "docs/docker.md"):
        dockerfile += path.read_text(encoding="utf-8")
    assert not re.search(r"(?i)(?:api[_-]?key|secret|token)\s*[:=]\s*[\"']?[A-Za-z0-9_-]{16,}", dockerfile)


def test_initialize_attach_and_configure_remote_entry(tmp_path):
    """Exercise the documented initial registration against a synthetic store."""
    from scope_recall.adapters.clients.remote_server import write_server_config
    from scope_recall.maintenance.shared import attach, init_shared

    store = tmp_path / "store"
    home = tmp_path / "homes" / "codex"
    home.mkdir(parents=True)
    init_shared(root=store, test_mode=True)
    attach(host="codex", instance_root=home, root=store, entry_id="codex", display_name="Codex")
    write_server_config(home, "codex", listen="127.0.0.1", port=18765, token_sha256="0" * 64)
