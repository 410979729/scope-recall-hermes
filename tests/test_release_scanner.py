"""Tests for release scanner behavior and sensitive-string classification.

They prevent accidental credential leaks from being normalized as harmless release output."""

from __future__ import annotations

import io
import importlib.util
from pathlib import Path
import tarfile
from types import SimpleNamespace
import zipfile

import pytest


def _load_release_check_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "check.release.py"
    spec = importlib.util.spec_from_file_location("scope_recall_check_release", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_scanner_detects_json_yaml_and_python_secret_assignments(tmp_path):
    module = _load_release_check_module()
    fake_value = "notareal" + "secretvalue12345"
    (tmp_path / "config.json").write_text('{"api_key": "' + fake_value + '"}\n', encoding="utf-8")
    (tmp_path / "config.yaml").write_text("token: " + fake_value + "\n", encoding="utf-8")
    (tmp_path / "settings.py").write_text("password = '" + fake_value + "'\n", encoding="utf-8")

    original_root = getattr(module, "ROOT")
    setattr(module, "ROOT", tmp_path)
    try:
        findings = module.scan_tree()
    finally:
        setattr(module, "ROOT", original_root)

    joined = "\n".join(findings["secrets"])
    assert "config.json" in joined
    assert "config.yaml" in joined
    assert "settings.py" in joined
    assert fake_value not in joined
    assert "[REDACTED_SECRET]" in joined


def test_release_scanner_uses_runtime_home_for_private_paths(tmp_path, monkeypatch):
    module = _load_release_check_module()
    fake_home = tmp_path / "home" / "agent"
    fake_home.mkdir(parents=True)
    source = tmp_path / "source"
    source.mkdir()
    (source / "notes.md").write_text("local file " + str(fake_home / ".hermes-yuheng" / "secret.log") + "\n", encoding="utf-8")

    original_root = getattr(module, "ROOT")
    setattr(module, "ROOT", source)
    monkeypatch.setattr(module.pathlib.Path, "home", staticmethod(lambda: fake_home))
    try:
        findings = module.scan_tree()
    finally:
        setattr(module, "ROOT", original_root)

    assert findings["private_paths"] == ["notes.md:1"]


def test_release_scanner_rejects_tilde_instance_home_paths(tmp_path):
    module = _load_release_check_module()
    (tmp_path / "README.md").write_text(
        'python scripts/doctor.py --hermes-home "~/.hermes-' + 'yuheng"\n',
        encoding="utf-8",
    )

    original_root = getattr(module, "ROOT")
    setattr(module, "ROOT", tmp_path)
    try:
        findings = module.scan_tree()
    finally:
        setattr(module, "ROOT", original_root)

    assert findings["private_paths"] == ["README.md:1"]


def test_release_scanner_limits_reserved_synthetic_identifier_to_marked_test_fixtures(tmp_path):
    module = _load_release_check_module()
    private_like_id = "8123" + "456789"
    reserved_synthetic_id = "900" + "0000001"
    (tmp_path / "private.py").write_text(f'user_id="{private_like_id}"\n', encoding="utf-8")
    (tmp_path / "operator.py").write_text(f'user_id="{reserved_synthetic_id}"\n', encoding="utf-8")
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    (test_dir / "synthetic_fixture.py").write_text(
        f'user_id="{reserved_synthetic_id}"  # fixture\n',
        encoding="utf-8",
    )
    (test_dir / "synthetic_fixture.yaml").write_text(
        f'allowed_chat_ids:\n  - "{reserved_synthetic_id}"  # fixture\n',
        encoding="utf-8",
    )
    (test_dir / "test_provider.py").write_text(
        f'user_id="{reserved_synthetic_id}"\n',
        encoding="utf-8",
    )
    (test_dir / "test_journal_digest.py").write_text(
        f'user_id="{private_like_id}"\n',
        encoding="utf-8",
    )

    original_root = getattr(module, "ROOT")
    setattr(module, "ROOT", tmp_path)
    try:
        findings = module.scan_tree()
    finally:
        setattr(module, "ROOT", original_root)

    assert findings["secrets"] == [
        "operator.py:1: personal_numeric_id: [REDACTED_ID]",
        "private.py:1: personal_numeric_id: [REDACTED_ID]",
        "tests/test_journal_digest.py:1: personal_numeric_id: [REDACTED_ID]",
    ]
    assert private_like_id not in "\n".join(findings["secrets"])
    assert reserved_synthetic_id not in "\n".join(findings["secrets"])


POSITIVE_IDENTIFIER_CONTEXTS = [
    ("python_bare.py", 'chat_id="{identifier}"\n'),
    ("python_user.py", 'user_id="{identifier}"\n'),
    ("python_subscript.py", 'payload["chat_id"] = "{identifier}"\n'),
    ("python_compound.py", 'telegram_chat_id="{identifier}"\n'),
    ("environment.py", 'TELEGRAM_CHAT_ID="{identifier}"\n'),
    ("config.json", '{{"chat_id": "{identifier}"}}\n'),
    ("user.json", '{{"user_id": "{identifier}"}}\n'),
    ("config.yaml", "chat_id: {identifier}\n"),
    ("camel.py", 'chatId="{identifier}"\n'),
    ("command.txt", "--chat-id {identifier}\n"),
    ("python_typed.py", "chat_id: int = {identifier}\n"),
    (
        "python_comparison.py",
        "def is_allowed(chat_id: int) -> bool:\n    return chat_id == {identifier}\n",
    ),
    ("python_plural.py", "ALLOWED_CHAT_IDS = {{{identifier}}}\n"),
    ("allowlist.yaml", "allowed_chat_ids: [{identifier}]\n"),
    ("config_multiline.yaml", 'allowed_chat_ids:\n  - "{identifier}"\n'),
    ("config_unindented.yaml", 'allowed_chat_ids:  # operator list\n- "{identifier}"\n'),
    ("config_flow.yaml", 'allowed_chat_ids: [\n  "{identifier}"\n]\n'),
    (
        "config_sequence_mapping.yaml",
        'profiles:\n  - allowed_chat_ids:\n      - "{identifier}"\n',
    ),
    (
        "config_alias.yaml",
        'identifier_values: &ids ["{identifier}"]\nallowed_chat_ids: *ids\n',
    ),
    (
        "pretty.json",
        '{{\n  "allowed_chat_ids": [\n    "{identifier}"\n  ]\n}}\n',
    ),
    ("multiline.toml", 'allowed_chat_ids = [\n  "{identifier}",\n]\n'),
    (
        "duplicate.json",
        '{{"allowed_chat_ids": ["{identifier}"], "allowed_chat_ids": []}}\n',
    ),
    ("notes.md", "allowed_chat_ids:\n  - {identifier}\n"),
    ("python_alias.py", 'candidate_id = "{identifier}"\nchat_id = candidate_id\n'),
    (
        "python_alias_chain.py",
        'candidate_id = "{identifier}"\nforwarded = candidate_id\nchat_id = forwarded\n',
    ),
    ("python_split.py", 'chat_id = "900" + "0000002"\n'),
    ("python_int_split.py", 'chat_id = int("900" + "0000002")\n'),
]


def test_release_scanner_rejects_identifier_list_nested_in_yaml_sequence_mapping(tmp_path):
    module = _load_release_check_module()
    chat_id = "900" + "0000002"  # fixture
    fixture = tmp_path / "profiles.yaml"
    fixture.write_text(
        f'profiles:\n  - allowed_chat_ids:\n      - "{chat_id}"\n',
        encoding="utf-8",
    )

    original_root = getattr(module, "ROOT")
    setattr(module, "ROOT", tmp_path)
    try:
        findings = module.scan_tree()
    finally:
        setattr(module, "ROOT", original_root)

    joined = "\n".join(findings["secrets"])
    assert "profiles.yaml" in joined
    assert "personal_numeric_id" in joined
    assert chat_id not in joined


def test_release_scanner_yaml_sequence_mapping_fallback_without_parser(tmp_path, monkeypatch):
    module = _load_release_check_module()
    chat_id = "900" + "0000002"  # fixture
    fixture = tmp_path / "profiles.yaml"
    fixture.write_text(
        f'profiles:\n  - allowed_chat_ids:\n      - "{chat_id}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "yaml", None)
    original_root = getattr(module, "ROOT")
    setattr(module, "ROOT", tmp_path)
    try:
        findings = module.scan_tree()
    finally:
        setattr(module, "ROOT", original_root)

    joined = "\n".join(findings["secrets"])
    assert "profiles.yaml" in joined
    assert "personal_numeric_id" in joined
    assert chat_id not in joined


def test_release_scanner_preserves_duplicate_yaml_keys(tmp_path):
    module = _load_release_check_module()
    chat_id = "900" + "0000002"  # fixture
    (tmp_path / "duplicate.yaml").write_text(
        f'allowed_chat_ids:\n  - "{chat_id}"\nallowed_chat_ids: []\n',
        encoding="utf-8",
    )
    original_root = getattr(module, "ROOT")
    setattr(module, "ROOT", tmp_path)
    try:
        findings = module.scan_tree()
    finally:
        setattr(module, "ROOT", original_root)

    joined = "\n".join(findings["secrets"])
    assert "duplicate.yaml" in joined
    assert "personal_numeric_id" in joined
    assert chat_id not in joined


def test_release_gate_stops_before_expensive_checks_for_yaml_sequence_mapping(
    tmp_path,
    monkeypatch,
    capsys,
):
    module = _load_release_check_module()
    chat_id = "900" + "0000002"  # fixture
    (tmp_path / "profiles.yaml").write_text(
        f'profiles:\n  - allowed_chat_ids:\n      - "{chat_id}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(
        module,
        "parse_args",
        lambda: SimpleNamespace(allow_dirty=True, live_dashboard_json="", accept_stale_live_waiver=False),
    )
    monkeypatch.setattr(module, "release_environment_check", lambda: {"ok": True})
    monkeypatch.setattr(module, "git_tree_check", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(module, "metadata_check", lambda: {"ok": True})
    monkeypatch.setattr(module, "live_dashboard_file_check", lambda *_args, **_kwargs: {"ok": True})

    def expensive_check_must_not_run(*_args, **_kwargs):
        raise AssertionError("expensive release checks ran before the source scan")

    monkeypatch.setattr(module, "run", expensive_check_must_not_run)
    assert module.main() == 1
    output = capsys.readouterr().out
    assert "personal_numeric_id" in output
    assert chat_id not in output


def test_release_artifacts_reject_yaml_sequence_mapping_package_data(tmp_path):
    module = _load_release_check_module()
    chat_id = "900" + "0000002"  # fixture
    content = f'profiles:\n  - allowed_chat_ids:\n      - "{chat_id}"\n'.encode()
    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("scope_recall/package-data/profiles.yaml", content)
    sdist = tmp_path / "candidate.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        info = tarfile.TarInfo("scope-recall/package-data/profiles.yaml")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))

    for artifact in (wheel, sdist):
        findings = module.scan_distribution_artifact(artifact)
        joined = "\n".join(findings["secrets"])
        assert "profiles.yaml" in joined
        assert "personal_numeric_id" in joined
        assert chat_id not in joined


def test_release_scanner_blocks_positive_chat_ids_in_supported_contexts(tmp_path):
    module = _load_release_check_module()
    chat_id = "900" + "0000002"  # fixture
    for filename, template in POSITIVE_IDENTIFIER_CONTEXTS:
        (tmp_path / filename).write_text(template.format(identifier=chat_id), encoding="utf-8")

    original_root = getattr(module, "ROOT")
    setattr(module, "ROOT", tmp_path)
    try:
        findings = module.scan_tree()
    finally:
        setattr(module, "ROOT", original_root)

    joined = "\n".join(findings["secrets"])
    for filename, _template in POSITIVE_IDENTIFIER_CONTEXTS:
        assert filename in joined
    assert chat_id not in joined
    assert joined.count("personal_numeric_id") == len(POSITIVE_IDENTIFIER_CONTEXTS)


def test_release_scanner_does_not_flag_nearby_benign_long_numbers(tmp_path):
    module = _load_release_check_module()
    (tmp_path / "benign.py").write_text(
        "order_id = 1234567890\nbuild_number = 2026071122\naccounting_total: 9876543210\n",
        encoding="utf-8",
    )
    (tmp_path / "benign.json").write_text(
        '{"allowed_chat_ids": [], "build_number": 2026071122}\n',
        encoding="utf-8",
    )
    (tmp_path / "benign.toml").write_text(
        "allowed_chat_ids = []\nbuild_number = 2026071122\n",
        encoding="utf-8",
    )
    (tmp_path / "benign.yaml").write_text(
        "allowed_chat_ids: []\nbuild_number: 2026071122\n",
        encoding="utf-8",
    )
    original_root = getattr(module, "ROOT")
    setattr(module, "ROOT", tmp_path)
    try:
        findings = module.scan_tree()
    finally:
        setattr(module, "ROOT", original_root)
    assert findings["secrets"] == []


def test_release_scanner_blocks_signed_telegram_group_ids_in_common_shapes(tmp_path):
    module = _load_release_check_module()
    group_id = "-" + "100" + "1234567890"
    (tmp_path / "python.py").write_text(f'chat_id="{group_id}"\n', encoding="utf-8")
    (tmp_path / "config.json").write_text(
        '{"memory_isolated_chat_ids": ["' + group_id + '"]}\n',
        encoding="utf-8",
    )
    (tmp_path / "config.yaml").write_text(f"chat_id: {group_id}\n", encoding="utf-8")

    original_root = getattr(module, "ROOT")
    setattr(module, "ROOT", tmp_path)
    try:
        findings = module.scan_tree()
    finally:
        setattr(module, "ROOT", original_root)

    joined = "\n".join(findings["secrets"])
    assert "python.py" in joined
    assert "config.json" in joined
    assert "config.yaml" in joined
    assert group_id not in joined


def test_release_scanner_scans_wheel_and_sdist_text_payloads(tmp_path):
    module = _load_release_check_module()
    group_id = "-" + "100" + "1234567890"
    positive_chat_id = "900" + "0000002"
    payload = f'chat_id="{group_id}"\n'.encode()
    synthetic_secret_payload = b'token="notarealsecretvalue12345"\n'
    positive_members = {
        filename: template.format(identifier=positive_chat_id).encode()
        for filename, template in POSITIVE_IDENTIFIER_CONTEXTS
    }

    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("scope_recall/tests/test_provider.py", payload)
        for filename, content in positive_members.items():
            archive.writestr(f"scope_recall/tests/{filename}", content)
        archive.writestr("scope_recall/tests/test_fixture.py", synthetic_secret_payload)

    sdist = tmp_path / "candidate.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        members = {
            "test_provider.py": payload,
            "test_fixture.py": synthetic_secret_payload,
            **positive_members,
        }
        for filename, content in members.items():
            info = tarfile.TarInfo(f"scope-recall/tests/{filename}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))

    for artifact in (wheel, sdist):
        findings = module.scan_distribution_artifact(artifact)
        joined = "\n".join(findings["secrets"])
        assert "test_provider.py" in joined
        for filename in positive_members:
            assert filename in joined
        assert "api_key_assignment" not in joined
        assert group_id not in joined
        assert positive_chat_id not in joined


def test_public_distribution_allows_generic_source_isolation_policy():
    module = _load_release_check_module()
    entries = {
        "scope_recall/provider.py",
        "scope_recall/source_isolation.py",
    }

    assert module.forbidden_distribution_entries(entries) == []


@pytest.mark.parametrize("filename,leak_template", POSITIVE_IDENTIFIER_CONTEXTS)
def test_release_gate_rejects_source_ids_before_expensive_checks(
    tmp_path,
    monkeypatch,
    capsys,
    filename,
    leak_template,
):
    module = _load_release_check_module()
    chat_id = "900" + "0000002"  # fixture
    (tmp_path / filename).write_text(leak_template.format(identifier=chat_id), encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(
        module,
        "parse_args",
        lambda: SimpleNamespace(allow_dirty=True, live_dashboard_json="", accept_stale_live_waiver=False),
    )
    monkeypatch.setattr(module, "release_environment_check", lambda: {"ok": True})
    monkeypatch.setattr(module, "git_tree_check", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(module, "metadata_check", lambda: {"ok": True})
    monkeypatch.setattr(module, "live_dashboard_file_check", lambda *_args, **_kwargs: {"ok": True})

    def expensive_check_must_not_run(*_args, **_kwargs):
        raise AssertionError("expensive release checks ran before the source scan")

    monkeypatch.setattr(module, "run", expensive_check_must_not_run)
    assert module.main() == 1
    output = capsys.readouterr().out
    assert "personal_numeric_id" in output
    assert chat_id not in output
