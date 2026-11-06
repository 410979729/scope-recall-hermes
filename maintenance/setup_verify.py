"""Exercise capture, processing and recall without adding test messages to the owner's store."""

from __future__ import annotations

import copy
import io
import json
import os
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from ..adapters.clients import CodexHookHandler
from ..adapters.hermes.audiences import LOCAL_USER_ID
from ..adapters.hermes.provider import ScopeRecallHermesAdapter
from ..core.recall_log_writer import close_writers
from ..runtime.instance import build_runtime_instance
from ..runtime.instance_config import RuntimeInstanceConfig
from ..runtime.worker_entry import credential_environment, run_worker
from .doctor import load_binding
from .install_common import HostChoice
from .install_hermes import profile_identity
from .shared import attach, fresh_installation, init_shared

MARKER = "amber-orchid"
STATEMENT = "The synthetic bluebird project marker is amber-orchid."
QUESTION = "What is the synthetic bluebird project marker?"


def _client(home: Path, host: str, session: str, prompt: str) -> dict:
    with closing(CodexHookHandler.from_home(str(home), host)) as handler:
        return handler.handle_payload(
            dict(
                hook_event_name="UserPromptSubmit",
                session_id=session,
                turn_id=session + "-turn",
                prompt_id=session + "-prompt",
                generation_id=session,
                cwd=str(home.parent),
                prompt=prompt,
            )
        )


def _hermes(home: Path, session: str, prompt: str, *, capture: bool, agent_identity: str):
    provider = ScopeRecallHermesAdapter()
    try:
        provider.initialize(
            session,
            hermes_home=str(home),
            platform="cli",
            agent_context="primary",
            agent_identity=agent_identity,
            agent_workspace="hermes",
            user_id=LOCAL_USER_ID,
            parent_session_id="",
        )
        if capture:
            provider.on_turn_start(1, prompt, turn_id=session + "-turn", session_id=session)
            provider.observe_pre_llm(session_id=session, turn_id=session + "-turn", user_message=prompt)
            provider.sync_turn(prompt, "Recorded.", session_id=session)
            return ""
        return provider.prefetch(prompt)
    finally:
        provider.shutdown()


def _clean_owned_check(root: Path) -> None:
    """Remove only enumerated files in this call's new directory, without recursive deletion."""
    # Host shutdown can detach its runtime. Retire only this probe's diagnostics
    # before enumerating files so a late WAL or health receipt cannot race removal.
    if not close_writers(root):
        raise OSError("verification diagnostics still active; directory retained")
    paths = list(root.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise ValueError("verification directory contains a link; retain it for inspection")
    for path in paths:
        if path.is_file():
            path.unlink()
    for path in sorted((path for path in paths if path.is_dir()), key=lambda path: len(path.parts), reverse=True):
        path.rmdir()
    root.rmdir()


@contextmanager
def temporary_credentials(config: RuntimeInstanceConfig, env_file: str | None):
    """Apply only declared credential references during verification and restore the caller's environment."""
    values = credential_environment(config, env_file) if env_file else {}
    original = {name: os.environ.get(name) for name in values}
    try:
        for name, value in values.items():
            os.environ.setdefault(name, value)
        yield
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def verify_memory(parent: Path, host: HostChoice, source: dict, *, env_file: str | None = None) -> dict:
    """Run the host adapter and real worker on an independent synthetic store, then remove it."""
    from .setup_configuration import runtime_payload

    if host == "hermes":
        from ..adapters.hermes.identity import bind_hermes_identity

        # Check the selected entry with the real host identity before probing a disposable store.
        bind_hermes_identity(
            "setup-check",
            hermes_home=str(parent),
            platform="cli",
            user_id=LOCAL_USER_ID,
            agent_identity=profile_identity(parent),
            agent_workspace="hermes",
        )

    probe_parent = parent.parent
    # Named profiles sit inside another home; disposable stores must be outside every host home.
    for ancestor in parent.parents:
        if (ancestor / "scope-recall").is_dir() or (ancestor / "codex-installation.json").is_file():
            probe_parent = ancestor.parent
    root = probe_parent / ("setup-check-" + uuid.uuid4().hex)
    root.mkdir(parents=True)
    try:
        store, home = root / "store", root / "host"
        init_shared(root=store, agent_id="setup-check", test_mode=True)
        identity = profile_identity(parent)
        grants = (
            fresh_installation(home, {"agent_id": "setup-check", "test_mode": True}, identity)
            if host == "hermes"
            else None
        )
        attach(
            host=host,
            instance_root=home,
            root=store,
            entry_id="setup-check",
            display_name="Setup check",
            grants_from=grants,
        )
        if host == "hermes":
            _hermes(home, "setup-first", STATEMENT, capture=True, agent_identity=identity)
        else:
            _client(home, host, "setup-first", STATEMENT)
        binding, _directory = load_binding(host, home)
        models = {
            key: copy.deepcopy(source[key]) for key in ("auxiliary", "vector", "vector_threshold") if key in source
        }
        raw = runtime_payload(binding, models=models, worker="manual")
        # A non-default name keeps host hooks from launching another worker during the probe.
        selected = RuntimeInstanceConfig.from_mapping(source)
        if selected.auxiliary and selected.auxiliary.ledger_path:
            # Verification spends against the selected ledger, including its existing usage.
            # Only the synthetic truth and vector stores are disposable.
            raw["auxiliary"]["ledger_path"] = str(selected.auxiliary.ledger_path)
        config = RuntimeInstanceConfig.from_mapping(raw)
        config_path = root / "check-worker.json"
        config_path.write_text(json.dumps(raw), encoding="utf-8")
        with temporary_credentials(config, env_file):
            stream = io.StringIO()
            code = run_worker(str(config_path), output=stream)
            worker = json.loads(stream.getvalue())
            answer = (
                _hermes(home, "setup-second", QUESTION, capture=False, agent_identity=identity)
                if host == "hermes"
                else _client(home, host, "setup-second", QUESTION)
            )
            recalled = MARKER in json.dumps(answer, ensure_ascii=False)
            auxiliary = raw.get("auxiliary", {})
            configured = auxiliary.get("external_embedding") and auxiliary.get("external_consolidation")
            model_recall = False
            if configured:
                with closing(build_runtime_instance(config)) as runtime:
                    packet = runtime.recall(
                        dict(
                            protocol_version="1.1",
                            request_id="setup-second",
                            query=QUESTION,
                            mode="current",
                            max_items=6,
                            budget_tokens=1200,
                        )
                    )
                    model_recall = MARKER in str(packet.items) and not packet.gaps
            full = bool(
                configured
                and model_recall
                and code == 0
                and not worker.get("capability_gaps")
                and worker.get("processed", 0)
            )
            return dict(
                capture=True,
                worker_exit=code,
                worker=worker,
                cross_session_recall=recalled,
                models_verified=full,
                isolation="independent synthetic store; no test messages enter the selected memory store",
            )
    finally:
        _clean_owned_check(root)
