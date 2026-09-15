"""Execute generated commands using the real Windows Codex shell shape."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scope_recall.maintenance import install


@pytest.mark.skipif(os.name != 'nt', reason='actual Windows command boundary')
def test_windows_command_preserves_literal_arguments_and_stdin(tmp_path, monkeypatch):
    helper = tmp_path / "目录 with space" / "hook.py"
    helper.parent.mkdir()
    helper.write_text('import json,sys\nprint(json.dumps({"args":sys.argv[1:],"input":sys.stdin.buffer.read().decode("utf-8")}))\n', encoding='utf-8')
    argument = "literal 中文 and spaces"
    monkeypatch.setattr(install, '_hook_argv', lambda *_: [sys.executable, '-I', '-B', str(helper), argument])
    _, command = install._hook_command(Path(sys.executable), tmp_path / 'config.json')
    assert command.endswith("scope-recall-hook.cmd")
    assert "EncodedCommand" not in command
    payload = '{"prompt":"原始输入"}'
    result = subprocess.run('cmd.exe /C "' + command + '"', input=payload.encode('utf-8'), capture_output=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')
    observed = json.loads(result.stdout)
    assert observed == {'args': [argument], 'input': payload}
