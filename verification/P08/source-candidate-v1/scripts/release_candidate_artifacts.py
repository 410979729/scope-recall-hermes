"""Deterministic archive evidence and source-correspondence checks.

The release-candidate build and manifest verifier share this module so an
artifact cannot be accepted through a weaker second implementation.
"""

from __future__ import annotations

import ast
import base64
import csv
from email.parser import Parser
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
from typing import Mapping, Sequence
import zipfile


MEMBER_MANIFEST_ALGORITHM = "archive-regular-files-sha256-v1"

_FORBIDDEN_PARTS = {
    ".execution",
    ".hermes",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "activity-state",
    "activity_state",
    "backups",
    "lancedb",
    "logs",
    "quarantine",
    "scope-recall",
    "venv",
}
_FORBIDDEN_SUFFIXES = (
    ".db",
    ".key",
    ".log",
    ".pem",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
    ".sqlite-shm",
    ".sqlite-wal",
)
_FORBIDDEN_SECRET_NAMES = {
    ".env",
    "credentials.json",
    "private_key",
    "secrets.json",
    "token.json",
    "tokens.json",
}
_SDIST_GENERATED_EXACT = {
    "PKG-INFO",
    "setup.cfg",
}
_SDIST_GENERATED_EGG_INFO_NAMES = {
    "PKG-INFO",
    "SOURCES.txt",
    "dependency_links.txt",
    "entry_points.txt",
    "requires.txt",
    "top_level.txt",
}
_WHEEL_REQUIRED_ROOT_DATA = {
    ".env.example",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "DESIGN.md",
    "LICENSE",
    "MANIFEST.in",
    "README.md",
    "SECURITY.md",
    "config.json",
    "plugin.yaml",
    "py.typed",
    "pyproject.toml",
}
_WHEEL_DIST_INFO_MEMBERS = {
    "METADATA",
    "RECORD",
    "WHEEL",
    "entry_points.txt",
    "licenses/LICENSE",
    "top_level.txt",
}
_WHEEL_DIST_INFO_ROOT = re.compile(
    r"^hermes_scope_recall-(?P<version>[0-9]+(?:\.[0-9]+)+(?:[^/]*)?)\.dist-info$"
)
_UNSAFE_VISUAL_CONSOLE_DOC_PATTERNS = (
    # A release document must not contain a directly usable retired-console
    # port, even in a warning or historical note.  Text cannot prove operator
    # intent reliably, so the conservative contract is deliberately literal.
    re.compile(r"(?<!\d)18766(?!\d)"),
    re.compile(r"(?i)\bscope_recall\.server(?::main)?\b"),
    re.compile(r"(?i)\bpython(?:3)?\s+(?:-m\s+)?(?:scope_recall\.)?server(?:\.py)?\b"),
)
_LEGACY_VISUAL_CONSOLE_PORT = re.compile(r"(?<!\d)18766(?!\d)")
_RAW_SQLITE_CONNECT = re.compile(r"(?i)\bsqlite3\s*\.\s*connect\s*\(")
_WEB_ENDPOINT_SIGNATURES = (
    re.compile(
        r"(?i)@\s*[A-Za-z_]\w*\s*\.\s*(?:route|post|put|patch|delete)\s*\("
    ),
    re.compile(
        r"(?i)\b(?:Flask|FastAPI|BaseHTTPRequestHandler|"
        r"ThreadingHTTPServer|HTTPServer)\b"
    ),
    re.compile(r"(?i)\bdo_(?:POST|PUT|PATCH|DELETE)\s*\("),
)
_SQLITE_MUTATION_SIGNATURES = (
    re.compile(
        r"(?is)\.\s*(?:execute|executemany|executescript)\s*\(\s*"
        r"(?:[rubf]{0,2})?[\"']\s*(?:UPDATE|INSERT|DELETE|REPLACE)\b"
    ),
    re.compile(r"(?i)\.\s*(?:commit|executemany|executescript)\s*\("),
)
_ENTRY_POINT_TARGET = re.compile(
    r"(?m)^\s*[^#;\[\]\n]+?\s*=\s*[\"']?"
    r"(?P<module>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*:"
)
_LEGACY_SCAN_EXCLUDED_PYTHON = {
    # The checker necessarily embeds the signatures it is responsible for
    # detecting.  Release tests are not runtime product modules and likewise
    # carry poison fixtures.  Both are still governed by the ordinary artifact
    # allowlists and source-correspondence checks.
    "scripts/release_candidate_artifacts.py",
}
_SOURCE_METADATA_NAMES = {
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "entry_points.txt",
    "metadata",
    "pkg-info",
}
_ARTIFACT_METADATA_NAMES = {"entry_points.txt", "metadata", "pkg-info"}


class ArtifactVerificationError(RuntimeError):
    """Raised when distribution bytes do not prove the candidate boundary."""


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member_name(raw: str) -> str:
    normalized = str(raw).replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or pure.is_absolute()
        or ".." in pure.parts
        or normalized != pure.as_posix()
    ):
        raise ArtifactVerificationError(f"unsafe archive member: {raw!r}")
    return normalized


def read_archive_members(path: Path) -> dict[str, bytes]:
    """Read regular members from a wheel or sdist without extracting them."""

    artifact = Path(path).resolve(strict=True)
    members: dict[str, bytes] = {}
    if zipfile.is_zipfile(artifact):
        with zipfile.ZipFile(artifact) as archive:
            for info in archive.infolist():
                name = _safe_member_name(info.filename)
                if info.is_dir():
                    continue
                mode = (info.external_attr >> 16) & 0xFFFF
                if mode and stat.S_ISLNK(mode):
                    raise ArtifactVerificationError(
                        f"archive contains a symbolic link: {name}"
                    )
                if name in members:
                    raise ArtifactVerificationError(
                        f"archive contains a duplicate member: {name}"
                    )
                members[name] = archive.read(info)
    elif tarfile.is_tarfile(artifact):
        with tarfile.open(artifact, "r:*") as archive:
            for info in archive.getmembers():
                name = _safe_member_name(info.name)
                if info.isdir():
                    continue
                if not info.isfile():
                    raise ArtifactVerificationError(
                        f"archive contains a non-regular member: {name}"
                    )
                if name in members:
                    raise ArtifactVerificationError(
                        f"archive contains a duplicate member: {name}"
                    )
                handle = archive.extractfile(info)
                if handle is None:
                    raise ArtifactVerificationError(
                        f"archive member cannot be read: {name}"
                    )
                members[name] = handle.read()
    else:
        raise ArtifactVerificationError(
            f"unsupported distribution artifact: {artifact.name}"
        )
    if not members:
        raise ArtifactVerificationError("distribution artifact has no regular files")
    return dict(sorted(members.items()))


def member_manifest_from_members(members: Mapping[str, bytes]) -> dict[str, object]:
    entries = [
        {
            "path": name,
            "sha256": sha256_bytes(content),
            "size_bytes": len(content),
        }
        for name, content in sorted(members.items())
    ]
    return {
        "algorithm": MEMBER_MANIFEST_ALGORITHM,
        "file_count": len(entries),
        "member_manifest_sha256": sha256_bytes(canonical_bytes(entries)),
        "files": entries,
    }


def archive_member_manifest(path: Path) -> dict[str, object]:
    return member_manifest_from_members(read_archive_members(path))


def _unsafe_visual_console_doc(content: bytes) -> bool:
    text = content.decode("utf-8", errors="replace")
    return any(pattern.search(text) for pattern in _UNSAFE_VISUAL_CONSOLE_DOC_PATTERNS)


def _runtime_python_relative(relative: str) -> str | None:
    """Return the product-relative Python path, excluding test poison/checker code."""

    pure = PurePosixPath(relative)
    parts = pure.parts
    if pure.suffix.casefold() != ".py" or "tests" in {
        part.casefold() for part in parts
    }:
        return None
    if parts and parts[0].casefold() == "scope_recall":
        pure = PurePosixPath(*parts[1:])
    product_relative = pure.as_posix()
    if product_relative in _LEGACY_SCAN_EXCLUDED_PYTHON:
        return None
    return product_relative


def _python_module_name(relative: str) -> str | None:
    product_relative = _runtime_python_relative(relative)
    if product_relative is None:
        return None
    pure = PurePosixPath(product_relative)
    parts = list(pure.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if not parts:
        return "scope_recall"
    return ".".join(("scope_recall", *parts))


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else ""
    return ""


def _literal_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_text(node.left)
        right = _literal_text(node.right)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        chunks: list[str] = []
        for value in node.values:
            if not isinstance(value, ast.Constant) or not isinstance(
                value.value, str
            ):
                return None
            chunks.append(value.value)
        return "".join(chunks)
    # Named SQL is deliberately unknown.  Resolving it without a complete
    # lexical/data-flow model lets same-name locals or later assignments turn
    # a write into an apparent read.  Unknown SQL is fail-closed below.
    return None


def _sql_is_statically_read_only(sql: str) -> bool:
    """Return true only for one plainly read-only SQLite statement."""

    statement = re.sub(r"(?m)^\s*--[^\n]*(?:\n|$)", "", sql).strip()
    if statement.endswith(";"):
        statement = statement[:-1].rstrip()
    if not statement or ";" in statement:
        return False
    if re.match(r"(?is)^(?:SELECT\b|EXPLAIN(?:\s+QUERY\s+PLAN)?\b)", statement):
        return re.search(
            r"(?is)\b(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|VACUUM|"
            r"REINDEX|ATTACH|DETACH)\b",
            statement,
        ) is None
    pragma = re.match(r"(?is)^PRAGMA\s+([A-Za-z_][A-Za-z0-9_]*)\s*(.*)$", statement)
    if pragma is None:
        return False
    pragma_name = pragma.group(1).casefold()
    pragma_tail = pragma.group(2).strip()
    read_only_pragmas = {
        "collation_list",
        "compile_options",
        "database_list",
        "foreign_key_list",
        "function_list",
        "index_info",
        "index_list",
        "index_xinfo",
        "module_list",
        "pragma_list",
        "query_only",
        "table_info",
        "table_list",
        "table_xinfo",
    }
    return pragma_name in read_only_pragmas and "=" not in pragma_tail


def _python_legacy_features(content: bytes) -> dict[str, bool]:
    """Extract conservative retired-console features without executing code."""

    text = content.decode("utf-8", errors="replace")
    features = {
        "legacy_port": _LEGACY_VISUAL_CONSOLE_PORT.search(text) is not None,
        "web_surface": False,
        "raw_sqlite_mutation": False,
    }
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # Python compilation is an independent release gate.  These fallbacks
        # prevent malformed poison from becoming a blind spot in this scanner.
        has_raw_sqlite = _RAW_SQLITE_CONNECT.search(text) is not None
        features["web_surface"] = any(
            pattern.search(text) for pattern in _WEB_ENDPOINT_SIGNATURES
        )
        has_mutation = any(
            pattern.search(text) for pattern in _SQLITE_MUTATION_SIGNATURES
        )
        features["raw_sqlite_mutation"] = has_raw_sqlite and has_mutation
        return features

    sqlite_modules = {"sqlite3", "sqlite3.dbapi2"}
    sqlite_connectors: set[str] = set()
    web_modules = {"flask", "fastapi"}
    web_constructors = {
        "Flask",
        "FastAPI",
        "BaseHTTPRequestHandler",
        "HTTPServer",
        "ThreadingHTTPServer",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"sqlite3", "sqlite3.dbapi2"}:
                    sqlite_modules.add(alias.asname or alias.name)
                if alias.name in {"flask", "fastapi"}:
                    web_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module in {"sqlite3", "sqlite3.dbapi2"}:
                for alias in node.names:
                    if alias.name in {"connect", "*"}:
                        sqlite_connectors.add(alias.asname or alias.name)
                    elif alias.name == "dbapi2":
                        sqlite_modules.add(alias.asname or alias.name)
            if node.module in {
                "flask",
                "fastapi",
                "http.server",
                "socketserver",
            }:
                for alias in node.names:
                    if alias.name in web_constructors:
                        web_constructors.add(alias.asname or alias.name)
    has_raw_sqlite = False
    has_mutation = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.upper() in {"DO_POST", "DO_PUT", "DO_PATCH", "DO_DELETE"}:
                features["web_surface"] = True
            for decorator in node.decorator_list:
                call = decorator if isinstance(decorator, ast.Call) else None
                target = call.func if call is not None else decorator
                if isinstance(target, ast.Attribute) and target.attr.casefold() in {
                    "route",
                    "post",
                    "put",
                    "patch",
                    "delete",
                }:
                    features["web_surface"] = True
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name):
            if function.id in sqlite_connectors or (
                "*" in sqlite_connectors and function.id == "connect"
            ):
                has_raw_sqlite = True
            if function.id in web_constructors:
                features["web_surface"] = True
        elif isinstance(function, ast.Attribute):
            sqlite_module = _dotted_name(function.value)
            if (
                function.attr == "connect"
                and sqlite_module in sqlite_modules
            ):
                has_raw_sqlite = True
            if (
                function.attr in {"Flask", "FastAPI"}
                and isinstance(function.value, ast.Name)
                and function.value.id in web_modules
            ):
                features["web_surface"] = True
            if function.attr.casefold() in {"execute", "executemany", "executescript"}:
                sql = _literal_text(node.args[0]) if node.args else None
                if sql is None or not _sql_is_statically_read_only(sql):
                    has_mutation = True
    features["raw_sqlite_mutation"] = has_raw_sqlite and has_mutation
    return features


def _legacy_python_reasons(
    features_by_path: Mapping[str, Mapping[str, bool]],
) -> dict[str, str]:
    """Classify both local and cross-module retired raw-console surfaces."""

    has_tree_web = any(item["web_surface"] for item in features_by_path.values())
    has_tree_mutation = any(
        item["raw_sqlite_mutation"] for item in features_by_path.values()
    )
    reasons: dict[str, str] = {}
    for path, features in features_by_path.items():
        if features["legacy_port"]:
            reasons[path] = "legacy_console_port"
        elif features["web_surface"] and features["raw_sqlite_mutation"]:
            reasons[path] = "raw_console_writer"
        elif has_tree_web and has_tree_mutation and features["web_surface"]:
            reasons[path] = "raw_console_writer_web_surface"
        elif has_tree_web and has_tree_mutation and features["raw_sqlite_mutation"]:
            reasons[path] = "raw_console_writer_storage"
    return reasons


def _entry_point_modules(text: str) -> set[str]:
    return {match.group("module") for match in _ENTRY_POINT_TARGET.finditer(text)}


def _visual_console_entrypoint_assignment(line: str) -> bool:
    left, separator, right = line.partition("=")
    if not separator or not left.strip() or not right.strip():
        return False
    lowered_left = left.casefold()
    return (
        "visual" in lowered_left
        and "console" in lowered_left
        and _ENTRY_POINT_TARGET.search(line) is not None
    )


def _legacy_metadata_reason(text: str, unsafe_modules: set[str]) -> str:
    if _LEGACY_VISUAL_CONSOLE_PORT.search(text):
        return "legacy_console_port"
    lowered = text.casefold()
    modules = _entry_point_modules(text)
    unsafe_entrypoint = (
        "scope_recall.server" in lowered
        or any(_visual_console_entrypoint_assignment(line) for line in text.splitlines())
        or bool(modules & unsafe_modules)
    )
    return "legacy_console_entrypoint" if unsafe_entrypoint else ""


def _read_source_member(root: Path, relative: str) -> bytes:
    pure = PurePosixPath(relative)
    candidate = root.joinpath(*pure.parts).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ArtifactVerificationError(
            "source manifest path escapes candidate root"
        ) from exc
    return candidate.read_bytes()


def legacy_visual_console_source_findings(
    source_root: Path,
    source_manifest: Mapping[str, object],
) -> list[dict[str, str]]:
    """Return content-free findings for the retired standalone console surface."""

    root = Path(source_root).resolve(strict=True)
    source = _source_entries(source_manifest)
    findings: list[dict[str, str]] = []
    unsafe_modules: set[str] = set()
    python_features: dict[str, Mapping[str, bool]] = {}
    for relative in sorted(source):
        module = _python_module_name(relative)
        if module is None:
            continue
        python_features[relative] = _python_legacy_features(
            _read_source_member(root, relative)
        )
    python_reasons = _legacy_python_reasons(python_features)
    for relative in python_reasons:
        module = _python_module_name(relative)
        if module is not None:
            unsafe_modules.add(module)

    for relative in sorted(source):
        pure = PurePosixPath(relative)
        if _runtime_python_relative(relative) == "server.py":
            findings.append({"path": relative, "reason": "legacy_server_module"})
            continue
        python_reason = python_reasons.get(relative)
        if python_reason:
            findings.append({"path": relative, "reason": python_reason})
            continue
        if pure.name.casefold() in _SOURCE_METADATA_NAMES:
            text = _read_source_member(root, relative).decode(
                "utf-8", errors="replace"
            )
            metadata_reason = _legacy_metadata_reason(text, unsafe_modules)
            if metadata_reason:
                findings.append({"path": relative, "reason": metadata_reason})
                continue
        if relative not in {"README.md", "CHANGELOG.md"} and not (
            pure.parts and pure.parts[0] == "docs" and pure.suffix.casefold() == ".md"
        ):
            continue
        if _unsafe_visual_console_doc(_read_source_member(root, relative)):
            findings.append(
                {"path": relative, "reason": "unsafe_visual_console_advertisement"}
            )
    return findings


def legacy_visual_console_artifact_findings(
    members: Mapping[str, bytes],
    *,
    kind: str,
    sdist_root: str = "",
) -> list[dict[str, str]]:
    """Reject the retired server module, its entrypoint, and unsafe docs."""

    normalized: dict[str, tuple[str, bytes]] = {}
    unsafe_modules: set[str] = set()
    python_features: dict[str, Mapping[str, bool]] = {}
    for member_name, content in sorted(members.items()):
        relative = (
            _strip_sdist_root(member_name, sdist_root)
            if kind == "sdist"
            else member_name
        )
        normalized[member_name] = (relative, content)
        module = _python_module_name(relative)
        if module is None:
            continue
        python_features[member_name] = _python_legacy_features(content)
    python_reasons = _legacy_python_reasons(python_features)
    for member_name in python_reasons:
        relative = normalized[member_name][0]
        module = _python_module_name(relative)
        if module is not None:
            unsafe_modules.add(module)

    findings: list[dict[str, str]] = []
    for member_name, (relative, content) in normalized.items():
        pure = PurePosixPath(relative)
        product_relative = _runtime_python_relative(relative)
        package_relative = PurePosixPath(product_relative or relative)
        if product_relative == "server.py":
            findings.append(
                {"path": member_name, "reason": "legacy_server_module"}
            )
            continue
        python_reason = python_reasons.get(member_name)
        if python_reason:
            findings.append({"path": member_name, "reason": python_reason})
            continue
        if pure.name.casefold() in _ARTIFACT_METADATA_NAMES:
            text = content.decode("utf-8", errors="replace")
            metadata_reason = _legacy_metadata_reason(text, unsafe_modules)
            if metadata_reason:
                findings.append(
                    {"path": member_name, "reason": metadata_reason}
                )
            continue
        if package_relative.suffix.casefold() == ".md" and _unsafe_visual_console_doc(
            content
        ):
            findings.append(
                {
                    "path": member_name,
                    "reason": "unsafe_visual_console_advertisement",
                }
            )
    return findings


def _strip_sdist_root(name: str, expected_root: str) -> str:
    parts = PurePosixPath(name).parts
    if len(parts) < 2 or parts[0] != expected_root:
        raise ArtifactVerificationError(
            f"sdist member is outside expected root {expected_root!r}: {name}"
        )
    return PurePosixPath(*parts[1:]).as_posix()


def _source_entries(source_manifest: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    raw_files = source_manifest.get("files")
    if not isinstance(raw_files, list):
        raise ArtifactVerificationError("source manifest files must be an array")
    entries: dict[str, Mapping[str, object]] = {}
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise ArtifactVerificationError("source manifest entry must be an object")
        path = str(raw.get("path") or "")
        if not path or path in entries:
            raise ArtifactVerificationError("source manifest has invalid paths")
        entries[path] = raw
    return entries


def _expected_wheel_source_paths(
    source: Mapping[str, Mapping[str, object]],
) -> set[str]:
    expected: set[str] = set()
    for path in source:
        pure = PurePosixPath(path)
        parts = pure.parts
        if len(parts) == 1 and (path.endswith(".py") or path in _WHEEL_REQUIRED_ROOT_DATA):
            expected.add(path)
        elif parts and parts[0] == "_internal" and path.endswith(".py"):
            expected.add(path)
        elif len(parts) == 2 and parts[0] == "scripts" and (
            path.endswith(".py") or path.endswith(".json")
        ):
            expected.add(path)
        elif len(parts) == 2 and parts[0] == "docs" and path.endswith(".md"):
            expected.add(path)
        elif (
            len(parts) == 3
            and parts[:2] == ("docs", "benchmarks")
            and path.endswith(".md")
        ):
            expected.add(path)
        elif len(parts) == 2 and parts[0] == "benchmarks" and path.endswith(".json"):
            expected.add(path)
        elif (
            len(parts) == 3
            and parts[:2] == ("examples", "external_bridge")
            and pure.suffix in {".jsonl", ".sql"}
        ):
            expected.add(path)
    return expected


def _wheel_dist_info_root(members: Mapping[str, bytes]) -> tuple[str, str]:
    roots = {
        PurePosixPath(name).parts[0]
        for name in members
        if PurePosixPath(name).parts
        and PurePosixPath(name).parts[0].endswith(".dist-info")
    }
    if len(roots) != 1:
        raise ArtifactVerificationError("wheel must contain exactly one dist-info root")
    root = next(iter(roots))
    match = _WHEEL_DIST_INFO_ROOT.fullmatch(root)
    if match is None:
        raise ArtifactVerificationError(f"unexpected wheel dist-info root: {root}")
    return root, str(match.group("version"))


def _record_digest(content: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode("ascii")
    return "sha256=" + encoded.rstrip("=")


def _validate_wheel_generated_members(
    members: Mapping[str, bytes],
    *,
    dist_info_root: str,
    version: str,
    source: Mapping[str, Mapping[str, object]],
) -> None:
    relative_generated = {
        PurePosixPath(name).relative_to(dist_info_root).as_posix()
        for name in members
        if PurePosixPath(name).parts[0] == dist_info_root
    }
    if relative_generated != _WHEEL_DIST_INFO_MEMBERS:
        raise ArtifactVerificationError(
            "wheel generated-member contract failed: "
            f"missing={sorted(_WHEEL_DIST_INFO_MEMBERS - relative_generated)!r}, "
            f"unexpected={sorted(relative_generated - _WHEEL_DIST_INFO_MEMBERS)!r}"
        )

    metadata = Parser().parsestr(
        members[f"{dist_info_root}/METADATA"].decode("utf-8")
    )
    if metadata.get("Name") != "hermes-scope-recall" or metadata.get(
        "Version"
    ) != version:
        raise ArtifactVerificationError("wheel METADATA identity contract failed")
    wheel_lines = {
        line.strip()
        for line in members[f"{dist_info_root}/WHEEL"].decode("utf-8").splitlines()
    }
    if "Root-Is-Purelib: true" not in wheel_lines or "Tag: py3-none-any" not in wheel_lines:
        raise ArtifactVerificationError("wheel WHEEL portability contract failed")
    entry_point_lines = {
        line.strip()
        for line in members[f"{dist_info_root}/entry_points.txt"]
        .decode("utf-8")
        .splitlines()
    }
    if "hermes-scope-recall = scope_recall.cli:main" not in entry_point_lines:
        raise ArtifactVerificationError("wheel console entry-point contract failed")
    if members[f"{dist_info_root}/top_level.txt"].decode("utf-8").strip() != "scope_recall":
        raise ArtifactVerificationError("wheel top-level package contract failed")
    license_entry = source.get("LICENSE")
    if (
        license_entry is None
        or license_entry.get("sha256")
        != sha256_bytes(members[f"{dist_info_root}/licenses/LICENSE"])
    ):
        raise ArtifactVerificationError("wheel generated license differs from source")

    record_name = f"{dist_info_root}/RECORD"
    rows = list(
        csv.reader(
            io.StringIO(members[record_name].decode("utf-8", errors="strict"))
        )
    )
    if any(len(row) != 3 for row in rows):
        raise ArtifactVerificationError("wheel RECORD rows must have three columns")
    record = {row[0]: (row[1], row[2]) for row in rows}
    if len(record) != len(rows) or set(record) != set(members):
        raise ArtifactVerificationError("wheel RECORD member set mismatch")
    for name, content in members.items():
        digest, size = record[name]
        if name == record_name:
            if digest or size:
                raise ArtifactVerificationError("wheel RECORD self row must be unhashed")
        elif digest != _record_digest(content) or size != str(len(content)):
            raise ArtifactVerificationError(f"wheel RECORD hash/size mismatch: {name}")


def verify_wheel_source_correspondence(
    members: Mapping[str, bytes],
    source_manifest: Mapping[str, object],
) -> dict[str, object]:
    """Enforce the explicit, bidirectional wheel/source member policy."""

    source = _source_entries(source_manifest)
    expected = _expected_wheel_source_paths(source)
    packaged: dict[str, str] = {}
    mismatches: list[str] = []
    unexpected: list[str] = []
    for member_name, content in sorted(members.items()):
        parts = PurePosixPath(member_name).parts
        if len(parts) < 2 or parts[0] != "scope_recall":
            continue
        source_path = PurePosixPath(*parts[1:]).as_posix()
        if source_path not in expected:
            unexpected.append(member_name)
            continue
        packaged[source_path] = member_name
        wanted = source.get(source_path)
        if wanted is None or wanted.get("sha256") != sha256_bytes(content):
            mismatches.append(member_name)

    missing = sorted(expected - set(packaged))
    dist_info_root, version = _wheel_dist_info_root(members)
    unknown_roots = sorted(
        name
        for name in members
        if PurePosixPath(name).parts[0] not in {"scope_recall", dist_info_root}
    )
    _validate_wheel_generated_members(
        members,
        dist_info_root=dist_info_root,
        version=version,
        source=source,
    )
    if mismatches or missing or unexpected or unknown_roots:
        raise ArtifactVerificationError(
            "wheel/source correspondence failed: "
            f"mismatched={sorted(mismatches)!r}, missing={missing!r}, "
            f"unexpected={sorted(unexpected)!r}, unknown={unknown_roots!r}"
        )
    policy = {
        "source_paths": sorted(expected),
        "generated_members": sorted(_WHEEL_DIST_INFO_MEMBERS),
    }
    member_manifest = member_manifest_from_members(members)
    return {
        "policy_sha256": sha256_bytes(canonical_bytes(policy)),
        "wheel_version": version,
        "expected_source_member_count": len(expected),
        "verified_runtime_python_files": sum(path.endswith(".py") for path in packaged),
        "verified_package_data_count": sum(not path.endswith(".py") for path in packaged),
        "generated_allowlist_count": len(_WHEEL_DIST_INFO_MEMBERS),
        "missing_expected_count": 0,
        "mismatched_source_count": 0,
        "unexpected_member_count": 0,
        "unknown_generated_count": 0,
        "wheel_member_manifest_sha256": member_manifest["member_manifest_sha256"],
        "source_manifest_sha256": source_manifest.get("manifest_sha256"),
        "status": "passed",
    }


def _is_generated_sdist_member(relative: str) -> bool:
    pure = PurePosixPath(relative)
    if relative in _SDIST_GENERATED_EXACT:
        return True
    return (
        len(pure.parts) == 2
        and pure.parts[0].endswith(".egg-info")
        and pure.name in _SDIST_GENERATED_EGG_INFO_NAMES
    )


def verify_sdist_source_correspondence(
    members: Mapping[str, bytes],
    source_manifest: Mapping[str, object],
    *,
    expected_root: str,
) -> dict[str, object]:
    """Require every non-generated sdist member to equal a tracked source file."""

    source = _source_entries(source_manifest)
    verified = 0
    untracked: list[str] = []
    mismatches: list[str] = []
    for member_name, content in sorted(members.items()):
        relative = _strip_sdist_root(member_name, expected_root)
        wanted = source.get(relative)
        if wanted is None:
            if not _is_generated_sdist_member(relative):
                untracked.append(member_name)
            continue
        verified += 1
        if wanted.get("sha256") != sha256_bytes(content):
            mismatches.append(member_name)
    if untracked or mismatches:
        raise ArtifactVerificationError(
            "sdist/source correspondence failed: "
            f"untracked={untracked!r}, mismatched={mismatches!r}"
        )
    return {
        "verified_tracked_files": verified,
        "source_manifest_sha256": source_manifest.get("manifest_sha256"),
    }


def artifact_name_findings(
    members: Mapping[str, bytes],
    *,
    kind: str,
    sdist_root: str = "",
    allowed_sdist_tests: Sequence[str] = (),
) -> list[dict[str, str]]:
    """Return content-free path-policy findings for actual archive members."""

    allowed_tests = {PurePosixPath(item).as_posix() for item in allowed_sdist_tests}
    findings: list[dict[str, str]] = []
    for member_name in sorted(members):
        relative = (
            _strip_sdist_root(member_name, sdist_root)
            if kind == "sdist"
            else member_name
        )
        pure = PurePosixPath(relative)
        lowered_parts = {part.casefold() for part in pure.parts}
        lowered_name = pure.name.casefold()
        reason = ""
        if lowered_parts & _FORBIDDEN_PARTS:
            reason = "forbidden_runtime_or_local_path"
        elif lowered_name in _FORBIDDEN_SECRET_NAMES:
            reason = "secret_or_live_configuration_name"
        elif lowered_name.endswith(_FORBIDDEN_SUFFIXES):
            reason = "state_log_key_or_cache_file"
        elif "tests" in lowered_parts:
            if kind != "sdist" or relative not in allowed_tests:
                reason = "arbitrary_test_not_allowlisted"
        if reason:
            findings.append({"path": member_name, "reason": reason})
    return findings
