"""The memory skill says what the core enforces, and both hosts install it.

``scope-recall-memory`` tells an agent how to answer what is remembered about the user and what
correcting, muting or deleting a memory does.  The part that matters most is the part that can rot:
the words it says the user's message must contain are the core's own patterns, quoted.  If a pattern
changes, or the page is edited, an agent would coach the user into a request the core refuses, so
every quoted word is checked against the pattern it is quoted from.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from scope_recall.adapters.runtime_wiring import FORGET_GUIDANCE, REVISE_GUIDANCE
from scope_recall.core import deletion, mutate
from scope_recall.maintenance.install import apply_install, plan_install, plan_uninstall
from scope_recall.maintenance.install_common import SKILLS

SKILL = SKILLS["scope-recall-memory"]

#: action in the skill -> (the core's pattern, the words the skill quotes for it)
QUOTED = {
    "correct": (mutate._CORRECTION, ("改为", "改成", "换成", "更正", "纠正", "correct", "replace", "change to", "switch to")),
    "withdraw": (mutate._RETRACT, ("撤回", "撤销", "作废", "不再使用", "停止使用", "retract", "withdraw", "stop using")),
    "mute": (deletion._SUPPRESS, ("不要主动提", "别再主动", "不再主动提", "do not mention")),
    "delete": (deletion._DELETE, ("删除", "删掉", "忘掉", "清除", "delete", "erase", "forget")),
}


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_the_skill_has_the_header_a_host_discovers_it_by():
    head = _text().split("---")[1]
    assert re.search(r"^name: scope-recall-memory$", head, re.M) and re.search(r"^description: .{40,}$", head, re.M)


@pytest.mark.parametrize("action", sorted(QUOTED))
def test_every_word_the_skill_quotes_is_one_the_core_accepts(action):
    pattern, words = QUOTED[action]
    text = _text()
    for word in words:
        assert word in text, f"the skill no longer quotes {word!r} for {action}"
        assert pattern.search(word), f"the core's pattern for {action} does not accept {word!r}"


def test_a_refusal_the_skill_explains_is_one_the_core_raises():
    sources = "\n".join(Path(module.__file__).read_text(encoding="utf-8") for module in (deletion, mutate))
    for code in ("forget_not_authorized", "target_not_bound", "explicit_batch_required", "VERSION_CONFLICT"):
        assert code in _text() and code in sources, code


def test_the_skill_and_both_tool_descriptions_say_what_a_deletion_takes_with_it():
    text = _text()
    assert "the whole\n  message it came from, and every other fact taken from that same message" in text
    assert "cannot be undone" in text and "There is no\n  tool that undoes this" in text
    assert "whole message it came from and every other fact taken from that message" in FORGET_GUIDANCE
    assert "nothing undoes this" in FORGET_GUIDANCE and "new_value null withdraws it" in REVISE_GUIDANCE


def _paths(tmp_path: Path):
    instance, plugin, project = (tmp_path / "instance").resolve(), (tmp_path / "plugins" / "scope-recall").resolve(), (tmp_path / "workspace").resolve()
    plugin.mkdir(parents=True)
    project.mkdir()
    return instance, plugin, project


@pytest.mark.parametrize("host", ["hermes", "codex"])
def test_both_hosts_install_the_skill_own_it_and_remove_it(tmp_path, host):
    instance, plugin, project = _paths(tmp_path)
    plan = plan_install(host=host, target_plugin_dir=plugin, instance_root=instance, project_root=project,
                        agent_id="main", python_executable=Path(sys.executable), test_mode=True)
    assert not plan.conflicts, plan.conflicts
    result = apply_install(plan)

    skills = (instance if host == "hermes" else plugin) / "skills"
    for name, source in SKILLS.items():
        installed = skills / name / "SKILL.md"
        assert installed.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
        assert str(installed) in {str(Path(item)) for item in result.files_written} or \
            installed.resolve() in {Path(item).resolve() for item in result.files_written}
    removal = plan_uninstall(instance_root=instance)
    # Receipt paths are normalised, which lowers their case on Windows.
    assert any(Path(item).name.lower() == "skill.md" and "scope-recall-memory" in item for item in removal.files_to_remove), \
        "an uninstall takes the skill the install wrote"


def test_an_existing_installation_gains_the_skill_on_its_next_apply(tmp_path, monkeypatch):
    """The installs in the field have only the setup skill; the new one is a plain write, not a conflict."""
    from scope_recall.maintenance import install_common, install_hermes

    instance, plugin, project = _paths(tmp_path)
    arguments = dict(host="hermes", target_plugin_dir=plugin, instance_root=instance, project_root=project,
                     agent_id="main", python_executable=Path(sys.executable), test_mode=True)
    setup_only = {"scope-recall-setup": SKILLS["scope-recall-setup"]}
    monkeypatch.setattr(install_common, "SKILLS", setup_only)
    monkeypatch.setattr(install_hermes, "SKILLS", setup_only)
    apply_install(plan_install(**arguments))
    assert not (instance / "skills" / "scope-recall-memory").exists()

    monkeypatch.undo()
    plan = plan_install(**arguments)
    assert not plan.conflicts, plan.conflicts
    apply_install(plan)
    assert (instance / "skills" / "scope-recall-memory" / "SKILL.md").is_file()
