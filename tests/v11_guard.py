import os
import re
from pathlib import Path
import shlex
import sys


_ROOT = Path(__file__).resolve().parents[1]
_ISOLATED = Path(os.environ["SCOPE_RECALL_TEST_BOUNDARY_PARENT"]).resolve()
_RUNTIME = (Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve())
_SYSTEM = Path(os.environ.get("SystemRoot", "/usr")).resolve()
_PYTHON = Path(sys.executable).resolve()
_ALLOW_CHILD_PROCESSES = os.environ.get("SCOPE_RECALL_TEST_ALLOW_OWNED_SUBPROCESSES") == "1"
_PROCESS_TIER = os.environ.get("SCOPE_RECALL_TEST_TIER", "")
_OWNED_SUBPROCESS_TIERS = frozenset({"native", "host", "integration", "migration", "packaging", "release"})
_PACKAGING_HELPER_ROOTS = tuple(
    Path(item).resolve()
    for item in os.environ.get("SCOPE_RECALL_TEST_PACKAGING_HELPER_ROOTS", "").split(os.pathsep)
    if item
)
_PACKAGING_HELPER_TIERS = frozenset({"packaging", "release"})
_PACKAGING_BARE_COMMANDS = frozenset({"cmd.exe", "powershell.exe"})
# Script-gate tests inspect the isolated checkout with Git.  The executable is
# intentionally bare on Windows, so permit only this one tool while retaining
# the owned-test cwd check above; all other bare child commands remain denied.
_CHECKOUT_BARE_COMMANDS = frozenset({"git", "git.exe"})
_CHECKOUT_GIT_SUBCOMMANDS = frozenset({
    "add", "commit", "config", "diff", "hash-object", "init", "ls-files", "rev-parse", "status",
})
_ALLOW_LOOPBACK = os.environ.get("SCOPE_RECALL_TEST_ALLOW_LOOPBACK") == "1"


def _allowed_roots() -> tuple[Path, ...]:
    return (_ROOT, _ISOLATED, *_RUNTIME, _SYSTEM, _PYTHON.parent)


def _is_allowed_path(value) -> bool:
    try:
        path = Path(os.fsdecode(value)).resolve(strict=False)
    except (TypeError, ValueError, OSError):
        return False
    return any(path == root or path.is_relative_to(root) for root in _allowed_roots())


def _command_tokens(command):
    if isinstance(command, (list, tuple)):
        return [os.fsdecode(item) for item in command]
    if isinstance(command, (str, bytes)):
        text = os.fsdecode(command)
        if os.name == "nt":
            return _windows_command_tokens(text)
        try:
            return shlex.split(text, posix=os.name != "nt")
        except ValueError:
            return [text]
    return []


def _windows_command_tokens(text: str) -> list[str]:
    """Parse the command line form supplied by Windows audit hooks.

    ``subprocess.Popen`` receives a list, but the Windows ``subprocess.Popen``
    audit event exposes the flattened command line.  ``shlex`` cannot safely
    recover a ``-c`` script containing escaped quotes, so use the small pure
    Python equivalent of the Windows CRT quoting rules here.
    """
    tokens: list[str] = []
    token: list[str] = []
    in_quotes = False
    index = 0
    length = len(text)
    while index < length:
        if text[index] in " \t\r\n" and not in_quotes:
            if token:
                tokens.append("".join(token))
                token = []
            index += 1
            continue
        if text[index] == "\\":
            start = index
            while index < length and text[index] == "\\":
                index += 1
            slashes = index - start
            if index < length and text[index] == '"':
                token.extend("\\" * (slashes // 2))
                if slashes % 2:
                    token.append('"')
                    index += 1
                elif in_quotes and index + 1 < length and text[index + 1] == '"':
                    token.append('"')
                    index += 2
                else:
                    in_quotes = not in_quotes
                    index += 1
                continue
            token.extend("\\" * slashes)
            continue
        if text[index] == '"':
            if in_quotes and index + 1 < length and text[index + 1] == '"':
                token.append('"')
                index += 2
            else:
                in_quotes = not in_quotes
                index += 1
            continue
        token.append(text[index])
        index += 1
    if token:
        tokens.append("".join(token))
    return tokens


def _is_allowed_child_path(token: str) -> bool:
    if _is_allowed_path(token):
        return True
    if _PROCESS_TIER not in _PACKAGING_HELPER_TIERS:
        return False
    try:
        path = Path(token).resolve(strict=False)
    except (TypeError, ValueError, OSError):
        return False
    return any(path == root or path.is_relative_to(root) for root in _PACKAGING_HELPER_ROOTS)


def _is_local_git_command(tokens: list[str], cwd=None) -> bool:
    if not tokens or tokens[0].lower() not in _CHECKOUT_BARE_COMMANDS:
        return False
    try:
        git_cwd = Path(os.fsdecode(cwd)).resolve(strict=False) if cwd else Path.cwd().resolve(strict=False)
    except (TypeError, ValueError, OSError):
        return False
    index = 1
    while index < len(tokens) and tokens[index] in {"-c", "-C"}:
        if index + 1 >= len(tokens):
            return False
        value = tokens[index + 1]
        if not value or value.startswith("-"):
            return False
        if tokens[index] == "-C":
            try:
                target = Path(value)
                if not target.is_absolute():
                    target = git_cwd / target
                target = target.resolve(strict=False)
            except (TypeError, ValueError, OSError):
                return False
            if not _is_allowed_path(target):
                return False
            git_cwd = target
        index += 2
    return index < len(tokens) and tokens[index].lower() in _CHECKOUT_GIT_SUBCOMMANDS


def _check_owned_child_process(args) -> None:
    if not _ALLOW_CHILD_PROCESSES or _PROCESS_TIER not in _OWNED_SUBPROCESS_TIERS:
        raise PermissionError("TEST_BOUNDARY: child processes disabled")
    command = args[1] if len(args) > 1 else None
    cwd = args[2] if len(args) > 2 else None
    if cwd and not _is_allowed_path(cwd):
        raise PermissionError("TEST_BOUNDARY: child cwd outside TEST boundary")
    tokens = _command_tokens(command)
    first = tokens[0].lower() if tokens else ""
    if first in _CHECKOUT_BARE_COMMANDS:
        if _PROCESS_TIER not in {"integration", "packaging", "release"}:
            raise PermissionError("TEST_BOUNDARY: child executable/path outside TEST boundary")
        if not _is_local_git_command(tokens, cwd):
            raise PermissionError("TEST_BOUNDARY: child executable/path outside TEST boundary")
        return
    absolute_tokens = [
        token
        for token in tokens
        if Path(token).is_absolute() or (os.name == "nt" and re.match(r"^[A-Za-z]:[\\/]", token))
    ]
    if not absolute_tokens:
        if _PROCESS_TIER in _PACKAGING_HELPER_TIERS and first in _PACKAGING_BARE_COMMANDS:
            return
        raise PermissionError("TEST_BOUNDARY: child executable/path outside TEST boundary")
    if not all(_is_allowed_child_path(token) for token in absolute_tokens):
        raise PermissionError("TEST_BOUNDARY: child executable/path outside TEST boundary")


def _check_path(value, writing=False):
    if not isinstance(value, (str, bytes, os.PathLike)):
        return
    logical=os.fsdecode(value)
    if os.name=='nt' and logical.upper() in {'NUL','\\\\.\\NUL'}:
        return
    if os.name=='nt' and logical.startswith('\\\\?\\'):
        if logical.upper().startswith('\\\\?\\UNC\\'):
            logical='\\\\'+logical[8:]
        elif re.match(r'^[A-Za-z]:\\',logical[4:]):
            logical=logical[4:]
        else:
            raise PermissionError('TEST_BOUNDARY: device namespace denied')
    if os.name=='nt' and logical.startswith('\\\\.\\'):
        raise PermissionError('TEST_BOUNDARY: device namespace denied')
    path = Path(logical).resolve(strict=False)
    if path == Path(os.devnull).resolve():
        return
    if writing and not path.is_relative_to(_ISOLATED):
        raise PermissionError(
            "TEST_BOUNDARY: write outside isolated test directory (%s)" % path
        )
    if any(path.is_relative_to(root) for root in (_ROOT, _ISOLATED, *_RUNTIME, _SYSTEM)):
        return
    raise PermissionError("TEST_BOUNDARY: protected data access")


def _anchored(value, dir_fd=None):
    """Anchor a possibly dir_fd-relative audit path before resolving it.

    The fd-based ``shutil.rmtree`` on POSIX passes bare names plus a
    directory descriptor to ``os.remove``/``os.rmdir``; judging a bare name
    against the process cwd would assess the wrong file. The descriptor is
    re-anchored by reading its ``/proc/self/fd`` link (POSIX only, where the
    fd-based rmtree exists). A directory whose ancestors were removed
    already still owns its children, but its readlink target carries the
    kernel's ``(deleted)`` suffix; stripping that suffix recovers the real
    location of the file being unlinked. An fd that cannot be read is an
    anchor we cannot verify, so the bare name passes through and will be
    denied against the cwd -- fail closed.
    """

    if dir_fd is None or not isinstance(value, (str, bytes, os.PathLike)):
        return value
    logical = os.fsdecode(value)
    if Path(logical).is_absolute():
        return value
    try:
        if isinstance(dir_fd, int):
            anchor = os.readlink(f"/proc/self/fd/{dir_fd}")
            if anchor.endswith(" (deleted)"):
                anchor = anchor[: -len(" (deleted)")]
            return os.path.join(anchor, logical)
        return os.path.join(os.fsdecode(dir_fd), logical)
    except (TypeError, ValueError, OSError):
        return value


def _audit(event, args):
    if event.startswith("socket."):
        if _ALLOW_LOOPBACK and event == "socket.__new__":
            return
        if _ALLOW_LOOPBACK and event in {"socket.connect", "socket.bind"} and len(args) > 1:
            address = args[1]
            host = address[0] if isinstance(address, tuple) and address else ""
            if host in {"127.0.0.1", "::1", "localhost"}:
                return
        raise PermissionError("TEST_BOUNDARY: network access denied")
    if event == "subprocess.Popen":
        _check_owned_child_process(args)
        return
    if event in {"os.system", "os.startfile", "os.startfile/2"}:
        raise PermissionError("TEST_BOUNDARY: network and child processes disabled")
    if event == "open":
        mode, flags = args[1:3]
        writing = bool(isinstance(mode, str) and any(c in mode for c in "wax+"))
        writing |= bool(isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        _check_path(args[0], writing)
    if event in {"os.listdir", "os.scandir", "os.chdir"}:
        _check_path(args[0])
    if event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime"}:
        _check_path(_anchored(args[0], args[1] if event in {"os.remove", "os.rmdir"} and len(args) > 1 else None), True)
    if event in {"os.rename", "os.link", "os.symlink"}:
        _check_path(_anchored(args[0], args[-2] if event == "os.rename" and len(args) > 2 else None), True)
        _check_path(_anchored(args[1], args[-1] if event == "os.rename" and len(args) > 2 else None), True)


sys.addaudithook(_audit)
