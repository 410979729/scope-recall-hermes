"""
Manual tests for capture_llm.py — no pytest required.
Runs against the code directly in the plugin directory.
"""
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from capture_llm import (
    _truthy,
    _VALID_MEMORY_TYPES,
    _parse_response,
    _resolve_api_key,
    _repair_truncated,
    extract_capture_candidates,
    Candidate,
    EXTRACT_SYSTEM_PROMPT,
)

PASS = 0
FAIL = 0


def check(description, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {description}")
    else:
        FAIL += 1
        print(f"  ✗ {description}")


# ── Test _truthy ──
print("\n=== _truthy ===")
check("True → True", _truthy(True))
check("False → False", not _truthy(False))
check("'true' → True", _truthy("true"))
check("'false' → False", not _truthy("false"))
check("'1' → True", _truthy("1"))
check("'on' → True", _truthy("on"))
check("'yes' → True", _truthy("yes"))
check("None → False", not _truthy(None))
check("0 → False", not _truthy(0))
check("0.0 → False", not _truthy(0.0))
check("1 → True", _truthy(1))

# ── Test _VALID_MEMORY_TYPES ──
print("\n=== _VALID_MEMORY_TYPES ===")
check("'workflow' in set", "workflow" in _VALID_MEMORY_TYPES)
check("'pitfall' in set", "pitfall" in _VALID_MEMORY_TYPES)
check("'decision' in set", "decision" in _VALID_MEMORY_TYPES)
check("'preference' in set", "preference" in _VALID_MEMORY_TYPES)
check("'factual' in set", "factual" in _VALID_MEMORY_TYPES)
check("'bogus' not in set", "bogus" not in _VALID_MEMORY_TYPES)

# ── Test _parse_response ──
print("\n=== _parse_response ===")

# Basic parse
result = _parse_response('[{"action":"insert","content":"User prefers dots over underscores","target":"user","memory_type":"preference","entities":[],"tags":["naming"]}]')
check("basic parse: 1 candidate", len(result) == 1)
if result:
    check("basic parse: content correct", result[0].content == "User prefers dots over underscores")
    check("basic parse: target=user", result[0].target == "user")
    check("basic parse: memory_type=preference", result[0].memory_type == "preference")
    check("basic parse: tags correct", result[0].tags == ["naming"])

# Empty array
result = _parse_response("[]")
check("empty array: 0 candidates", len(result) == 0)

# Skip action
result = _parse_response('[{"action":"skip"}]')
check("skip action: 0 candidates", len(result) == 0)

# Markdown code fence
result = _parse_response('```json\n[{"action":"insert","content":"test content here ok","target":"memory","memory_type":"factual","entities":["test"],"tags":[]}]\n```')
check("fenced JSON: 1 candidate", len(result) == 1)
if result:
    check("fenced JSON: content correct", result[0].content == "test content here ok")
    check("fenced JSON: entities", result[0].entities == ["test"])

# Multiple candidates
result = _parse_response("""[
  {"action":"insert","content":"First memory item here ok","target":"memory","memory_type":"factual","entities":[],"tags":[]},
  {"action":"insert","content":"Second memory item here ok","target":"ops","memory_type":"workflow","entities":["server01"],"tags":["deploy"]}
]""")
check("multi candidate: 2 items", len(result) == 2)
if len(result) >= 2:
    check("multi candidate: first target=memory", result[0].target == "memory")
    check("multi candidate: second target=ops", result[1].target == "ops")
    check("multi candidate: second type=workflow", result[1].memory_type == "workflow")

# Text mixed with JSON (realistic LLM output)
result = _parse_response("Here is the extraction:\n[{\"action\":\"insert\",\"content\":\"User asked about SSH into server 0007 and found OpenClaw was down. Assistant connected via SSH, checked status, and restarted the service. Service came back online successfully.\",\"target\":\"ops\",\"memory_type\":\"workflow\",\"entities\":[\"0007\",\"OpenClaw\"],\"tags\":[\"ssh\",\"restart\",\"ops\"]}]")
check("mixed text+JSON: 1 candidate", len(result) == 1)
if result:
    check("mixed: content has details", "0007" in result[0].content and "OpenClaw" in result[0].content)
    check("mixed: entities captured", result[0].entities == ["0007", "OpenClaw"])

# Invalid target → general
result = _parse_response('[{"action":"insert","content":"test content here ok","target":"bogus","memory_type":"factual","entities":[],"tags":[]}]')
check("invalid target → general", result[0].target == "general") if result else check("invalid target → general", False)

# Invalid memory_type → factual
result = _parse_response('[{"action":"insert","content":"test content here ok ok","target":"memory","memory_type":"bogus","entities":[],"tags":[]}]')
check("invalid memory_type → factual", result[0].memory_type == "factual") if result else check("invalid memory_type → factual", False)

# Content too short (< 10 chars)
result = _parse_response('[{"action":"insert","content":"short","target":"memory","memory_type":"factual","entities":[],"tags":[]}]')
check("short content filtered", len(result) == 0)

# Garbage input
result = _parse_response("not json at all, just some text")
check("garbage input: 0 candidates", len(result) == 0)

# Empty string
result = _parse_response("")
check("empty string: 0 candidates", len(result) == 0)

# ── Test _resolve_api_key ──
print("\n=== _resolve_api_key ===")
os.environ["TEST_LLM_KEY"] = "sk-test-123"
check("env var resolved", _resolve_api_key({"api_key_env": ["TEST_LLM_KEY"]}) == "sk-test-123")
del os.environ["TEST_LLM_KEY"]
check("direct config key", _resolve_api_key({"api_key": "direct-key-456"}) == "direct-key-456")
check("no key returns empty", _resolve_api_key({"api_key_env": []}) == "")
check("direct key via api_key_env", _resolve_api_key({"api_key_env": ["NONEXISTENT_KEY_XYZ"]}) == "")

# ── Test _repair_truncated ──
print("\n=== _repair_truncated ===")
check("empty string → empty", _repair_truncated("") == "")
check("too short → empty", _repair_truncated("ab") == "")
check("complete JSON → empty", _repair_truncated('[{"a":1}]') == "")
check("truncated array: adds ]", _repair_truncated('[{"a":1},{"b":2').endswith("}]"))
check("truncated string: adds quote+bracket", _repair_truncated('[{"content":"test').endswith('"}]'))
check("truncated nested: closes both", _repair_truncated('[{"a":[1,2').endswith("]}]"))
check("partial repair → parses via _parse_response", len(_parse_response('[{"action":"insert","content":"test test test ok","target":"mem')) == 1)

# ── Test extract_capture_candidates (no API key path) ──
print("\n=== extract_capture_candidates (no API) ===")
# Disabled
result = extract_capture_candidates("user msg", "asst reply", {"capture_llm": {"enabled": False}})
check("disabled: empty list", len(result) == 0)

# Enabled but no API key
result = extract_capture_candidates("user msg", "asst reply", {"capture_llm": {"enabled": True}})
check("enabled no key: empty list", len(result) == 0)

# Missing config section
result = extract_capture_candidates("user msg", "asst reply", {})
check("missing config: empty list", len(result) == 0)

# String "true" for enabled but no key
result = extract_capture_candidates("user msg", "asst reply", {"capture_llm": {"enabled": "true"}})
check("string true no key: empty list", len(result) == 0)

# ── Content quality tests (simulated LLM responses) ──
print("\n=== Content Quality (simulated LLM responses) ===")

# Scenario: remote ops debugging
ops_response = """[
  {
    "action": "insert",
    "content": "用户要求检查 0160 机器上 OpenClaw 网关的崩溃情况。助手通过 SSH 登录 0160，发现 gateway 日志中记录了 1000+ 次崩溃，根因是 v2.1.3 版本的一个内存泄漏 bug。",
    "target": "ops",
    "memory_type": "workflow",
    "entities": ["0160", "OpenClaw", "gateway"],
    "tags": ["ssh", "crash", "debug"]
  },
  {
    "action": "insert",
    "content": "修复方案：将 OpenClaw 从 v2.1.3 降级到 v2.1.1，降级后服务正常启动，崩溃停止。验证方式：观察日志 5 分钟无新崩溃。",
    "target": "ops",
    "memory_type": "pitfall",
    "entities": ["OpenClaw", "v2.1.3", "v2.1.1"],
    "tags": ["downgrade", "fix", "memory-leak"]
  },
  {
    "action": "insert",
    "content": "用户确认 OpenClaw 回滚最佳实践：先备份当前配置和二进制，再执行降级，最后验证健康检查和日志。",
    "target": "memory",
    "memory_type": "procedure",
    "entities": ["OpenClaw"],
    "tags": ["rollback", "best-practice", "ops"]
  }
]"""

result = _parse_response(ops_response)
check("ops scenario: 3 candidates", len(result) == 3)
if len(result) >= 3:
    check("ops: workflow has entities", len(result[0].entities) >= 2)
    check("ops: pitfall has version entities", "v2.1.3" in result[1].entities)
    check("ops: procedure is target=memory", result[2].target == "memory")
    # Check extraction depth
    check("ops: workflow mentions crash count", "1000" in result[0].content or "1000+" in result[0].content)
    check("ops: pitfall mentions fix action", "降级" in result[1].content)
    check("ops: procedure is self-contained", "先备份" in result[2].content)

# Scenario: user preference correction
pref_response = """[
  {
    "action": "insert",
    "content": "用户纠正助手：目录命名不要用下划线 _，改用点号 . 作为分隔。此偏好已应用于所有项目目录。",
    "target": "user",
    "memory_type": "preference",
    "entities": [],
    "tags": ["naming", "convention", "correction"]
  }
]"""

result = _parse_response(pref_response)
check("pref scenario: 1 candidate", len(result) == 1)
if result:
    check("pref: target=user", result[0].target == "user")
    check("pref: type=preference", result[0].memory_type == "preference")
    check("pref: captures correction detail", "纠正" in result[0].content or "correction" in result[0].content)

# Scenario: code change + commit
code_response = """[
  {
    "action": "insert",
    "content": "在 scope-recall 插件中新增 capture_llm.py 模块，实现基于 LLM 的对话语义提取。修改了 config.py 和 provider.py 接入四层捕捉流水线。所有改动已通过语法检查。",
    "target": "project",
    "memory_type": "project",
    "entities": ["scope-recall", "capture_llm.py", "config.py", "provider.py"],
    "tags": ["feature", "memory", "llm-integration"]
  }
]"""

result = _parse_response(code_response)
check("code scenario: 1 candidate", len(result) == 1)
if result:
    check("code: target=project", result[0].target == "project")
    check("code: mentions files changed", all(f in result[0].content for f in ["capture_llm.py", "config.py", "provider.py"]))
    check("code: entities list includes files", len(result[0].entities) >= 3)

# ── Prompt quality check ──
print("\n=== Prompt Quality ===")
check("prompt mentions user question", "user's question/intent" in EXTRACT_SYSTEM_PROMPT.lower())
check("prompt mentions assistant actions", "approach/actions/results" in EXTRACT_SYSTEM_PROMPT.lower())
check("prompt mentions mistakes+fixes", "mistake" in EXTRACT_SYSTEM_PROMPT.lower() and "fix" in EXTRACT_SYSTEM_PROMPT.lower())
check("prompt mentions successful execution", "successfully completed" in EXTRACT_SYSTEM_PROMPT.lower())
check("prompt mentions secret redaction", "passwords" in EXTRACT_SYSTEM_PROMPT.lower())
check("prompt mentions target meanings", "ops\": operations" in EXTRACT_SYSTEM_PROMPT.lower())
check("prompt mentions self-contained", "self-contained" in EXTRACT_SYSTEM_PROMPT.lower())

# ── Summary ──
print(f"\n{'='*40}")
print(f"Results: {PASS} passed, {FAIL} failed")
if FAIL > 0:
    sys.exit(1)
else:
    print("ALL TESTS PASSED ✓")
