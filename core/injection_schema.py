"""SQLite definitions and seeded rules for synchronous source screening."""

import json


def _tokens(*slots, span=256):
    """Encode ordered literal alternatives as stored policy data."""
    return json.dumps(dict(slots=slots, span=span), ensure_ascii=False, separators=(",", ":"))


DEFAULT_RULES = (
    (
        "instruction_override_en",
        "instruction",
        "tokens",
        _tokens(
            ["ignore", "disregard", "override"],
            ["instructions", "instruction", "rules", "rule", "guidelines", "guideline", "prompts", "prompt"],
        ),
        "quarantined",
    ),
    (
        "instruction_override_zh",
        "instruction",
        "tokens",
        _tokens(
            ["忽略", "无视", "覆盖", "绕过", "不必遵守", "不要遵守"],
            ["指令", "系统提示", "安全规则", "之前的规则", "此前的规则"],
        ),
        "quarantined",
    ),
    (
        "role_override",
        "instruction",
        "tokens",
        _tokens(
            [
                "[system]",
                "[developer]",
                "<|im_start|>system",
                "<|im_start|>developer",
                "<|start_header_id|>system",
                "<|start_header_id|>developer",
                "system prompt override",
            ],
        ),
        "quarantined",
    ),
    (
        "role_override_zh",
        "instruction",
        "tokens",
        _tokens(
            ["你现在是", "从现在起你是"],
            ["无限制", "不受限制", "系统管理员"],
        ),
        "quarantined",
    ),
    (
        "prompt_disclosure",
        "instruction",
        "tokens",
        _tokens(
            ["reveal", "output", "print", "repeat", "泄露", "输出", "打印", "复述"],
            ["system prompt", "系统提示词"],
        ),
        "quarantined",
    ),
    (
        "hidden_instruction",
        "instruction",
        "tokens",
        _tokens(
            ["<!--", "<div", "<span"],
            [
                "ignore",
                "override",
                "system",
                "忽略",
                "指令",
                "display:none",
                "display: none",
                "visibility:hidden",
                "visibility: hidden",
            ],
            span=1536,
        ),
        "quarantined",
    ),
    (
        "exfiltration_link_en",
        "exfiltration",
        "tokens",
        _tokens(
            ["send", "post", "upload", "transmit", "exfiltrate"],
            ["conversation", "history", "messages", "context", "credentials", "secrets", "memory", "data"],
            ["https://", "http://", "mailto:"],
            span=1536,
        ),
        "quarantined",
    ),
    (
        "exfiltration_link_zh",
        "exfiltration",
        "tokens",
        _tokens(
            ["聊天记录", "对话记录", "会话内容", "系统提示", "上下文", "凭据", "密钥", "记忆内容", "私密数据"],
            ["发送", "上传", "传给", "提交", "外传"],
            ["https://", "http://", "mailto:"],
            span=1536,
        ),
        "quarantined",
    ),
    (
        "exfiltration_link_zh_reverse",
        "exfiltration",
        "tokens",
        _tokens(
            ["发送", "上传", "外传"],
            ["聊天记录", "上下文", "密钥"],
            ["https://", "http://", "mailto:"],
            span=1536,
        ),
        "quarantined",
    ),
    (
        "exfiltration_command",
        "exfiltration",
        "command",
        _tokens(
            ["curl", "wget"],
            ["key", "token", "secret", "password", ".env", "credentials"],
            span=1024,
        ),
        "quarantined",
    ),
    (
        "invisible_unicode",
        "unicode",
        "unicode",
        "controls",
        "quarantined",
    ),
    (
        "join_control_obfuscation",
        "unicode",
        "unicode",
        "join",
        "quarantined",
    ),
)

SCAN_STATEMENT = """CREATE TABLE source_scan_progress (
    source_ref TEXT NOT NULL, source_revision INTEGER NOT NULL,
    input_sha256 TEXT NOT NULL, policy_sha256 TEXT NOT NULL,
    cursor_json TEXT NOT NULL CHECK(json_valid(cursor_json)),
    PRIMARY KEY(source_ref,source_revision),
    FOREIGN KEY(source_ref,source_revision) REFERENCES source_events(event_id,source_revision) ON DELETE CASCADE
) STRICT, WITHOUT ROWID"""

STATEMENTS = (
    """CREATE TABLE injection_rules (
        rule_id TEXT PRIMARY KEY NOT NULL, category TEXT NOT NULL,
        matcher TEXT NOT NULL CHECK(matcher IN ('regex','origin','tokens','unicode','command')),
        pattern TEXT NOT NULL, disposition TEXT NOT NULL CHECK(disposition IN ('quarantined','pending_review')),
        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1))
    ) STRICT""",
    """CREATE TABLE source_safety (
        source_ref TEXT NOT NULL, source_revision INTEGER NOT NULL CHECK(source_revision>=1),
        state TEXT NOT NULL CHECK(state IN ('quarantined','pending_review','released')),
        rules_json TEXT NOT NULL CHECK(json_valid(rules_json)), scanned_at TEXT NOT NULL,
        reviewed_at TEXT, PRIMARY KEY(source_ref,source_revision),
        FOREIGN KEY(source_ref,source_revision) REFERENCES source_events(event_id,source_revision) ON DELETE CASCADE
    ) STRICT, WITHOUT ROWID""",
    "CREATE INDEX source_safety_state ON source_safety(state,source_ref,source_revision)",
    SCAN_STATEMENT,
)


SEED_STATEMENTS = tuple(
    "INSERT INTO injection_rules(rule_id,category,matcher,pattern,disposition) VALUES ("
    + ",".join("'" + value.replace("'", "''") + "'" for value in rule)
    + ")"
    for rule in DEFAULT_RULES
)


def install(connection):
    """Install initial policy and holds; scan checkpoints have their own upgrade step."""
    for statement in (*STATEMENTS[:-1], *SEED_STATEMENTS):
        connection.execute(statement)


def upgrade_policy(connection):
    """Replace executable legacy patterns while retaining owner disable decisions.

    Custom rules are retained for validation; unsupported patterns fail closed.
    Source holds remain intact and can be rescanned or reviewed explicitly.
    """
    connection.execute("ALTER TABLE injection_rules RENAME TO old_injection_rules")
    connection.execute(STATEMENTS[0])
    retired = {"external_source_review", "unattributed_source_review"}
    defaults = {rule[0] for rule in DEFAULT_RULES}
    for row in connection.execute("SELECT * FROM old_injection_rules").fetchall():
        if row["rule_id"] not in defaults | retired:
            connection.execute("INSERT INTO injection_rules VALUES (?,?,?,?,?,?)", tuple(row))
    for rule in DEFAULT_RULES:
        old = connection.execute("SELECT enabled FROM old_injection_rules WHERE rule_id=?", (rule[0],)).fetchone()
        connection.execute("INSERT INTO injection_rules VALUES (?,?,?,?,?,?)", (*rule, old[0] if old else 1))
    connection.execute("DROP TABLE old_injection_rules")
    connection.execute("""UPDATE source_safety SET rules_json='["policy_rescan_required"]'
        WHERE state='pending_review' AND json_array_length(rules_json)>0
        AND NOT EXISTS(SELECT 1 FROM json_each(rules_json)
            WHERE value NOT IN ('external_source_review','unattributed_source_review'))""")
