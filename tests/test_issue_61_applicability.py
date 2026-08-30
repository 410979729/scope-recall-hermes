"""Issue #61 is absent from 2.0 and stays absent from release artifacts."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from scripts import release_candidate_artifacts as artifacts


ROOT = Path(__file__).resolve().parents[1]
SDIST_ROOT = "hermes_scope_recall-2.0.0"


def _candidate_manifest() -> ModuleType:
    path = ROOT / "scripts" / "report.candidate_manifest.py"
    spec = importlib.util.spec_from_file_location("issue_61_candidate_manifest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_manifest(*paths: str) -> dict[str, object]:
    return {"files": [{"path": path} for path in paths]}


def _write_source(root: Path, relative: str, content: bytes) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def test_source_has_no_legacy_visual_console_writer() -> None:
    source = _candidate_manifest().source_manifest(ROOT)

    assert artifacts.legacy_visual_console_source_findings(ROOT, source) == []
    assert not (ROOT / "server.py").exists()


def test_wheel_has_no_server_py() -> None:
    clean = {"scope_recall/provider.py": b"ready = True\n"}
    poisoned = {**clean, "scope_recall/server.py": b"legacy = True\n"}

    assert artifacts.legacy_visual_console_artifact_findings(
        clean, kind="wheel"
    ) == []
    assert artifacts.legacy_visual_console_artifact_findings(
        poisoned, kind="wheel"
    ) == [
        {
            "path": "scope_recall/server.py",
            "reason": "legacy_server_module",
        }
    ]


def test_sdist_has_no_server_py() -> None:
    clean = {f"{SDIST_ROOT}/provider.py": b"ready = True\n"}
    poisoned = {**clean, f"{SDIST_ROOT}/server.py": b"legacy = True\n"}

    assert artifacts.legacy_visual_console_artifact_findings(
        clean, kind="sdist", sdist_root=SDIST_ROOT
    ) == []
    assert artifacts.legacy_visual_console_artifact_findings(
        poisoned, kind="sdist", sdist_root=SDIST_ROOT
    ) == [
        {
            "path": f"{SDIST_ROOT}/server.py",
            "reason": "legacy_server_module",
        }
    ]


def test_distribution_has_no_port_18766_console_entrypoint() -> None:
    clean = {
        "hermes_scope_recall-2.0.0.dist-info/entry_points.txt": (
            b"[console_scripts]\nhermes-scope-recall = scope_recall.cli:main\n"
        )
    }
    poisoned = {
        **clean,
        "legacy.dist-info/entry_points.txt": (
            b"[console_scripts]\n"
            b"visual-console-18766 = scope_recall.server:main\n"
        ),
    }

    assert artifacts.legacy_visual_console_artifact_findings(
        clean, kind="wheel"
    ) == []
    assert artifacts.legacy_visual_console_artifact_findings(
        poisoned, kind="wheel"
    ) == [
        {
            "path": "legacy.dist-info/entry_points.txt",
            "reason": "legacy_console_port",
        }
    ]


def test_renamed_ui_python_and_neutral_entrypoint_cannot_bypass_wheel_gate() -> None:
    poisoned = {
        "scope_recall/ui.py": (
            b"from flask import Flask\n"
            b"PORT = 18766\n"
            b"def main():\n    Flask(__name__).run(port=PORT)\n"
        ),
        "hermes_scope_recall-2.0.0.dist-info/entry_points.txt": (
            b"[console_scripts]\n"
            b"scope-recall-ui = scope_recall.ui:main\n"
        ),
    }

    assert artifacts.legacy_visual_console_artifact_findings(
        poisoned, kind="wheel"
    ) == [
        {
            "path": "hermes_scope_recall-2.0.0.dist-info/entry_points.txt",
            "reason": "legacy_console_entrypoint",
        },
        {
            "path": "scope_recall/ui.py",
            "reason": "legacy_console_port",
        },
    ]


def test_renamed_ui_python_and_neutral_entrypoint_cannot_bypass_source_gate(
    tmp_path: Path,
) -> None:
    _write_source(
        tmp_path,
        "ui.py",
        b"PORT = 18766\ndef main():\n    return PORT\n",
    )
    _write_source(
        tmp_path,
        "pyproject.toml",
        (
            b"[project.scripts]\n"
            b"scope-recall-ui = \"scope_recall.ui:main\"\n"
        ),
    )

    assert artifacts.legacy_visual_console_source_findings(
        tmp_path,
        _source_manifest("ui.py", "pyproject.toml"),
    ) == [
        {
            "path": "pyproject.toml",
            "reason": "legacy_console_entrypoint",
        },
        {"path": "ui.py", "reason": "legacy_console_port"},
    ]


def test_renamed_raw_console_writer_is_blocked_in_sdist_without_legacy_port() -> None:
    raw_writer = (
        b"from flask import Flask\n"
        b"import sqlite3\n"
        b"app = Flask(__name__)\n"
        b"@app.route('/edit', methods=['POST'])\n"
        b"def edit_memory():\n"
        b"    conn = sqlite3.connect(DB_PATH)\n"
        b"    conn.execute('UPDATE memories SET content = ?', ('new',))\n"
        b"    conn.commit()\n"
    )
    poisoned = {
        f"{SDIST_ROOT}/scope_recall/ui.py": raw_writer,
        f"{SDIST_ROOT}/hermes_scope_recall.egg-info/entry_points.txt": (
            b"[console_scripts]\n"
            b"scope-recall-ui = scope_recall.ui:main\n"
        ),
    }

    assert artifacts.legacy_visual_console_artifact_findings(
        poisoned, kind="sdist", sdist_root=SDIST_ROOT
    ) == [
        {
            "path": (
                f"{SDIST_ROOT}/hermes_scope_recall.egg-info/entry_points.txt"
            ),
            "reason": "legacy_console_entrypoint",
        },
        {
            "path": f"{SDIST_ROOT}/scope_recall/ui.py",
            "reason": "raw_console_writer",
        },
    ]


def test_distribution_metadata_cannot_hide_legacy_console_port() -> None:
    wheel = {
        "hermes_scope_recall-2.0.0.dist-info/METADATA": (
            b"Metadata-Version: 2.4\n"
            b"Project-URL: Local UI, http://localhost:18766\n"
        )
    }
    sdist = {
        f"{SDIST_ROOT}/PKG-INFO": (
            b"Metadata-Version: 2.4\n"
            b"Project-URL: Local UI, http://127.0.0.1:18766\n"
        )
    }

    assert artifacts.legacy_visual_console_artifact_findings(
        wheel, kind="wheel"
    ) == [
        {
            "path": "hermes_scope_recall-2.0.0.dist-info/METADATA",
            "reason": "legacy_console_port",
        }
    ]
    assert artifacts.legacy_visual_console_artifact_findings(
        sdist, kind="sdist", sdist_root=SDIST_ROOT
    ) == [
        {
            "path": f"{SDIST_ROOT}/PKG-INFO",
            "reason": "legacy_console_port",
        }
    ]


def test_embedded_product_prose_is_not_mistaken_for_an_entrypoint() -> None:
    product_statement = (
        b"Scope Recall 2.0 does not ship the retired standalone "
        b"visual-console writer.\n"
    )
    wheel = {
        "hermes_scope_recall-2.0.0.dist-info/METADATA": (
            b"Metadata-Version: 2.4\nDescription: Safe boundary.\n\n"
            + product_statement
        )
    }
    sdist = {
        f"{SDIST_ROOT}/PKG-INFO": (
            b"Metadata-Version: 2.4\nDescription: Safe boundary.\n\n"
            + product_statement
        )
    }
    actual_entrypoint = {
        "hermes_scope_recall-2.0.0.dist-info/entry_points.txt": (
            b"[console_scripts]\n"
            b"visual-console = scope_recall.cli:main\n"
        )
    }

    assert artifacts.legacy_visual_console_artifact_findings(
        wheel, kind="wheel"
    ) == []
    assert artifacts.legacy_visual_console_artifact_findings(
        sdist, kind="sdist", sdist_root=SDIST_ROOT
    ) == []
    assert artifacts.legacy_visual_console_artifact_findings(
        actual_entrypoint, kind="wheel"
    ) == [
        {
            "path": "hermes_scope_recall-2.0.0.dist-info/entry_points.txt",
            "reason": "legacy_console_entrypoint",
        }
    ]


def test_source_metadata_cannot_hide_legacy_console_port(tmp_path: Path) -> None:
    _write_source(
        tmp_path,
        "pyproject.toml",
        (
            b"[project.urls]\n"
            b"LocalUI = \"http://localhost:18766\"\n"
        ),
    )

    assert artifacts.legacy_visual_console_source_findings(
        tmp_path,
        _source_manifest("pyproject.toml"),
    ) == [
        {"path": "pyproject.toml", "reason": "legacy_console_port"},
    ]


def test_isolated_sqlite_writer_and_read_only_web_are_not_false_positives() -> None:
    sqlite_writer = {
        "scope_recall/repository.py": (
            b"import sqlite3\n"
            b"def update(path):\n"
            b"    conn = sqlite3.connect(path)\n"
            b"    conn.execute('UPDATE memories SET content = ?', ('new',))\n"
            b"    conn.commit()\n"
        ),
    }
    read_only_web = {
        "scope_recall/read_ui.py": (
            b"from flask import Flask\n"
            b"import sqlite3\n"
            b"app = Flask(__name__)\n"
            b"@app.route('/health')\n"
            b"def health():\n"
            b"    conn = sqlite3.connect('file:memory.sqlite3?mode=ro', uri=True)\n"
            b"    conn.execute('PRAGMA table_info(memories)').fetchall()\n"
            b"    return conn.execute('SELECT 1').fetchone()\n"
        ),
    }

    assert artifacts.legacy_visual_console_artifact_findings(
        sqlite_writer, kind="wheel"
    ) == []
    assert artifacts.legacy_visual_console_artifact_findings(
        read_only_web, kind="wheel"
    ) == []


def test_tree_aggregate_blocks_split_web_surface_and_sqlite_writer() -> None:
    split_console = {
        "scope_recall/storage.py": (
            b"import sqlite3 as db\n"
            b"SQL_UPDATE = 'UPDATE memories SET content = ?'\n"
            b"def mutate(path):\n"
            b"    with db.connect(path) as conn:\n"
            b"        conn.execute(SQL_UPDATE, ('new',))\n"
            b"        conn.commit()\n"
        ),
        "scope_recall/ui.py": (
            b"from flask import Flask\n"
            b"from . import storage\n"
            b"app = Flask(__name__)\n"
            b"@app.post('/edit')\n"
            b"def edit():\n"
            b"    return storage.mutate(DB_PATH)\n"
        ),
    }

    assert artifacts.legacy_visual_console_artifact_findings(
        split_console, kind="wheel"
    ) == [
        {
            "path": "scope_recall/storage.py",
            "reason": "raw_console_writer_storage",
        },
        {
            "path": "scope_recall/ui.py",
            "reason": "raw_console_writer_web_surface",
        },
    ]
    split_sdist = {
        f"{SDIST_ROOT}/{path}": content for path, content in split_console.items()
    }
    assert artifacts.legacy_visual_console_artifact_findings(
        split_sdist, kind="sdist", sdist_root=SDIST_ROOT
    ) == [
        {
            "path": f"{SDIST_ROOT}/scope_recall/storage.py",
            "reason": "raw_console_writer_storage",
        },
        {
            "path": f"{SDIST_ROOT}/scope_recall/ui.py",
            "reason": "raw_console_writer_web_surface",
        },
    ]


def test_sqlite_connect_alias_and_named_sql_constant_are_blocked() -> None:
    raw_writer = (
        b"from flask import Flask\n"
        b"from sqlite3 import connect as open_db\n"
        b"app = Flask(__name__)\n"
        b"SQL_UPDATE = 'UPDATE memories SET content = ?'\n"
        b"@app.post('/edit')\n"
        b"def edit():\n"
        b"    with open_db(DB_PATH) as conn:\n"
        b"        conn.execute(SQL_UPDATE, ('new',))\n"
    )
    wheel = {"scope_recall/renamed_ui.py": raw_writer}
    sdist = {f"{SDIST_ROOT}/scope_recall/renamed_ui.py": raw_writer}

    assert artifacts.legacy_visual_console_artifact_findings(
        wheel, kind="wheel"
    ) == [
        {"path": "scope_recall/renamed_ui.py", "reason": "raw_console_writer"}
    ]
    assert artifacts.legacy_visual_console_artifact_findings(
        sdist, kind="sdist", sdist_root=SDIST_ROOT
    ) == [
        {
            "path": f"{SDIST_ROOT}/scope_recall/renamed_ui.py",
            "reason": "raw_console_writer",
        }
    ]


def test_source_gate_blocks_alias_and_split_module_bypasses(tmp_path: Path) -> None:
    _write_source(
        tmp_path,
        "ui.py",
        (
            b"from flask import Flask\n"
            b"from . import storage\n"
            b"app = Flask(__name__)\n"
            b"@app.post('/edit')\n"
            b"def edit():\n"
            b"    return storage.mutate(DB_PATH)\n"
        ),
    )
    _write_source(
        tmp_path,
        "storage.py",
        (
            b"from sqlite3 import connect as open_db\n"
            b"SQL_UPDATE = 'UPDATE memories SET content = ?'\n"
            b"def mutate(path):\n"
            b"    with open_db(path) as conn:\n"
            b"        conn.execute(SQL_UPDATE, ('new',))\n"
            b"        conn.commit()\n"
        ),
    )

    assert artifacts.legacy_visual_console_source_findings(
        tmp_path, _source_manifest("storage.py", "ui.py")
    ) == [
        {"path": "storage.py", "reason": "raw_console_writer_storage"},
        {"path": "ui.py", "reason": "raw_console_writer_web_surface"},
    ]


def test_dynamic_sql_cannot_bypass_split_tree_writer_gate(tmp_path: Path) -> None:
    dynamic_writer = {
        "scope_recall/storage.py": (
            b"import sqlite3 as db\n"
            b"def mutate(path, sql):\n"
            b"    with db.connect(path) as conn:\n"
            b"        conn.execute(sql)\n"
        ),
        "scope_recall/ui.py": (
            b"from flask import Flask\n"
            b"from . import storage\n"
            b"app = Flask(__name__)\n"
            b"@app.post('/edit')\n"
            b"def edit():\n"
            b"    return storage.mutate(DB_PATH, request.form['sql'])\n"
        ),
    }

    assert artifacts.legacy_visual_console_artifact_findings(
        dynamic_writer, kind="wheel"
    ) == [
        {
            "path": "scope_recall/storage.py",
            "reason": "raw_console_writer_storage",
        },
        {
            "path": "scope_recall/ui.py",
            "reason": "raw_console_writer_web_surface",
        },
    ]
    dynamic_sdist = {
        f"{SDIST_ROOT}/{path}": content for path, content in dynamic_writer.items()
    }
    assert artifacts.legacy_visual_console_artifact_findings(
        dynamic_sdist, kind="sdist", sdist_root=SDIST_ROOT
    ) == [
        {
            "path": f"{SDIST_ROOT}/scope_recall/storage.py",
            "reason": "raw_console_writer_storage",
        },
        {
            "path": f"{SDIST_ROOT}/scope_recall/ui.py",
            "reason": "raw_console_writer_web_surface",
        },
    ]
    for path, content in dynamic_writer.items():
        _write_source(tmp_path, path.removeprefix("scope_recall/"), content)
    assert artifacts.legacy_visual_console_source_findings(
        tmp_path, _source_manifest("storage.py", "ui.py")
    ) == [
        {"path": "storage.py", "reason": "raw_console_writer_storage"},
        {"path": "ui.py", "reason": "raw_console_writer_web_surface"},
    ]


def test_interpolated_f_string_sql_is_unknown_and_fail_closed() -> None:
    poisoned = {
        "scope_recall/ui.py": (
            b"from flask import Flask\n"
            b"import sqlite3\n"
            b"app = Flask(__name__)\n"
            b"@app.post('/edit')\n"
            b"def edit(fragment):\n"
            b"    conn = sqlite3.connect(DB_PATH)\n"
            b"    conn.executescript(f'SELECT {fragment}')\n"
        )
    }

    assert artifacts.legacy_visual_console_artifact_findings(
        poisoned, kind="wheel"
    ) == [{"path": "scope_recall/ui.py", "reason": "raw_console_writer"}]


def test_same_named_function_local_sql_cannot_cross_scope_bypass() -> None:
    poisoned = {
        "scope_recall/ui.py": (
            b"from flask import Flask\n"
            b"import sqlite3\n"
            b"app = Flask(__name__)\n"
            b"@app.post('/edit')\n"
            b"def write():\n"
            b"    SQL = 'UPDATE memories SET content = 1'\n"
            b"    sqlite3.connect(DB_PATH).execute(SQL)\n"
            b"def read():\n"
            b"    SQL = 'SELECT 1'\n"
            b"    return sqlite3.connect(DB_PATH).execute(SQL).fetchone()\n"
        )
    }

    assert artifacts.legacy_visual_console_artifact_findings(
        poisoned, kind="wheel"
    ) == [{"path": "scope_recall/ui.py", "reason": "raw_console_writer"}]


def test_all_sqlite_dbapi2_import_forms_are_recognized(tmp_path: Path) -> None:
    variants = {
        "dbapi_alias.py": (
            b"from flask import Flask\n"
            b"import sqlite3.dbapi2 as db\n"
            b"app = Flask(__name__)\n"
            b"@app.post('/edit')\n"
            b"def edit():\n"
            b"    db.connect(DB_PATH).execute('UPDATE memories SET content = 1')\n"
        ),
        "dbapi_connect_alias.py": (
            b"from flask import Flask\n"
            b"from sqlite3.dbapi2 import connect as open_db\n"
            b"app = Flask(__name__)\n"
            b"@app.post('/edit')\n"
            b"def edit():\n"
            b"    open_db(DB_PATH).execute('UPDATE memories SET content = 1')\n"
        ),
        "dbapi_qualified.py": (
            b"from flask import Flask\n"
            b"import sqlite3.dbapi2\n"
            b"app = Flask(__name__)\n"
            b"@app.post('/edit')\n"
            b"def edit():\n"
            b"    sqlite3.dbapi2.connect(DB_PATH).execute(\n"
            b"        'UPDATE memories SET content = 1'\n"
            b"    )\n"
        ),
    }
    wheel = {f"scope_recall/{path}": content for path, content in variants.items()}
    assert artifacts.legacy_visual_console_artifact_findings(
        wheel, kind="wheel"
    ) == [
        {"path": f"scope_recall/{path}", "reason": "raw_console_writer"}
        for path in sorted(variants)
    ]

    for path, content in variants.items():
        _write_source(tmp_path, path, content)
    assert artifacts.legacy_visual_console_source_findings(
        tmp_path, _source_manifest(*variants)
    ) == [
        {"path": path, "reason": "raw_console_writer"}
        for path in sorted(variants)
    ]


def test_all_retired_port_addresses_and_negative_wording_are_blocked(
    tmp_path: Path,
) -> None:
    docs = {
        "docs/all-interfaces.md": b"Open http://0.0.0.0:18766/ locally.\n",
        "docs/ipv6.md": b"Open http://[::1]:18766/ locally.\n",
        "README.md": b"Do not use http://127.0.0.1:18766/ anymore.\n",
    }
    for path, content in docs.items():
        _write_source(tmp_path, path, content)

    assert artifacts.legacy_visual_console_source_findings(
        tmp_path, _source_manifest(*docs)
    ) == [
        {
            "path": "README.md",
            "reason": "unsafe_visual_console_advertisement",
        },
        {
            "path": "docs/all-interfaces.md",
            "reason": "unsafe_visual_console_advertisement",
        },
        {
            "path": "docs/ipv6.md",
            "reason": "unsafe_visual_console_advertisement",
        },
    ]
    wheel_docs = {f"scope_recall/{path}": content for path, content in docs.items()}
    assert artifacts.legacy_visual_console_artifact_findings(
        wheel_docs, kind="wheel"
    ) == [
        {
            "path": "scope_recall/README.md",
            "reason": "unsafe_visual_console_advertisement",
        },
        {
            "path": "scope_recall/docs/all-interfaces.md",
            "reason": "unsafe_visual_console_advertisement",
        },
        {
            "path": "scope_recall/docs/ipv6.md",
            "reason": "unsafe_visual_console_advertisement",
        },
    ]
    sdist_docs = {
        f"{SDIST_ROOT}/{path}": content for path, content in docs.items()
    }
    assert artifacts.legacy_visual_console_artifact_findings(
        sdist_docs, kind="sdist", sdist_root=SDIST_ROOT
    ) == [
        {
            "path": f"{SDIST_ROOT}/README.md",
            "reason": "unsafe_visual_console_advertisement",
        },
        {
            "path": f"{SDIST_ROOT}/docs/all-interfaces.md",
            "reason": "unsafe_visual_console_advertisement",
        },
        {
            "path": f"{SDIST_ROOT}/docs/ipv6.md",
            "reason": "unsafe_visual_console_advertisement",
        },
    ]


def test_server_named_test_fixtures_are_not_runtime_server_modules(
    tmp_path: Path,
) -> None:
    _write_source(tmp_path, "tests/server.py", b"poison fixture only\n")
    source = artifacts.legacy_visual_console_source_findings(
        tmp_path, _source_manifest("tests/server.py")
    )
    wheel = artifacts.legacy_visual_console_artifact_findings(
        {"tests/server.py": b"poison fixture only\n"}, kind="wheel"
    )
    packaged_wheel_test = artifacts.legacy_visual_console_artifact_findings(
        {"scope_recall/tests/server.py": b"poison fixture only\n"}, kind="wheel"
    )
    sdist = artifacts.legacy_visual_console_artifact_findings(
        {f"{SDIST_ROOT}/tests/server.py": b"poison fixture only\n"},
        kind="sdist",
        sdist_root=SDIST_ROOT,
    )
    _write_source(tmp_path, "server.py", b"retired product module\n")
    product_source = artifacts.legacy_visual_console_source_findings(
        tmp_path, _source_manifest("server.py")
    )

    assert source == []
    assert wheel == []
    assert packaged_wheel_test == []
    assert sdist == []
    assert product_source == [
        {"path": "server.py", "reason": "legacy_server_module"}
    ]


def test_docs_do_not_advertise_removed_unsafe_console() -> None:
    source = _candidate_manifest().source_manifest(ROOT)

    assert artifacts.legacy_visual_console_source_findings(ROOT, source) == []
    assert "does not ship the retired standalone visual-console writer" in (
        ROOT / "README.md"
    ).read_text(encoding="utf-8")
