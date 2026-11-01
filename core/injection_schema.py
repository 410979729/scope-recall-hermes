"""SQLite definitions and seeded rules for synchronous source screening."""

DEFAULT_RULES = (
    (
        "instruction_override_en",
        "instruction",
        "regex",
        "\\b(?:ignore|disregard|override)\\s+(?:(?:all|any|the|your|previous|prio"
        "r|above|earlier|system|safety)\\s+){0,6}(?:instructions?|rules?|guideli"
        "nes?|prompts?)\\b",
        "quarantined",
    ),
    (
        "instruction_override_zh",
        "instruction",
        "regex",
        "(?:忽略|无视|覆盖|绕过|不必遵守|不要遵守)[^。！？\\n]{0,24}(?:指令|系统提示|安全规则|之前的规则|此前的规则)",
        "quarantined",
    ),
    (
        "role_override",
        "instruction",
        "regex",
        "(?:\\[\\s*(?:system|developer)\\s*\\]|<\\|(?:im_start|start_header_id)\\|>\\s"
        "*(?:system|developer)|(?:system\\s+prompt\\s+override)|(?:你现在是|从现在起你是)[^"
        "。！？\\n]{0,32}(?:无限制|不受限制|系统管理员))",
        "quarantined",
    ),
    (
        "prompt_disclosure",
        "instruction",
        "regex",
        "(?:(?:reveal|output|print|repeat)\\s+(?:(?:your|the|entire|original|ini"
        "tial)\\s+){0,4}system\\s+prompt|(?:泄露|输出|打印|复述)[^。！？\\n]{0,12}系统提示词)",
        "quarantined",
    ),
    (
        "hidden_instruction",
        "instruction",
        "regex",
        "<!--[^>]{0,512}(?:ignore|override|system|忽略|指令)[^>]{0,512}-->|<\\s*(?:d"
        "iv|span)[^>]{0,256}(?:display\\s*:\\s*none|visibility\\s*:\\s*hidden)",
        "quarantined",
    ),
    (
        "exfiltration_link_en",
        "exfiltration",
        "regex",
        "\\b(?:send|post|upload|transmit|exfiltrate)\\b[^\\n]{0,512}\\b(?:conversat"
        "ion|history|messages|context|credentials|secrets|memory|data)\\b[^\\n]{0"
        ",512}(?:https?://|mailto:)",
        "quarantined",
    ),
    (
        "exfiltration_link_zh",
        "exfiltration",
        "regex",
        "(?:(?:把|将)?[^。！？\\n]{0,32}(?:聊天记录|对话记录|会话内容|系统提示|上下文|凭据|密钥|记忆内容|私密数据)[^"
        "。！？\\n]{0,32}(?:发送|上传|传给|提交|外传)[^\\n]{0,128}(?:https?://|mailto:)|(?:发送|"
        "上传|外传)[^\\n]{0,32}(?:聊天记录|上下文|密钥)[^\\n]{0,128}https?://)",
        "quarantined",
    ),
    (
        "exfiltration_command",
        "exfiltration",
        "regex",
        "\\b(?:curl|wget)\\b[^\\n]{0,512}(?:\\$(?:\\{)?[A-Z_]*(?:KEY|TOKEN|SECRET|PA"
        "SSWORD)\\b|@[^\\s]*(?:\\.env|credentials))",
        "quarantined",
    ),
    (
        "invisible_unicode",
        "unicode",
        "regex",
        "[\\u00ad\\u034f\\u061c\\u180e\\u200b\\u200e\\u200f\\u202a-\\u202e\\u2060-\\u2064\\"
        "u2066-\\u206f\\ufeff\\ufff9-\\ufffb\\U000e0000-\\U000e007f]",
        "quarantined",
    ),
    (
        "external_source_review",
        "provenance",
        "origin",
        "external_document",
        "pending_review",
    ),
    (
        "join_control_obfuscation",
        "unicode",
        "regex",
        "(?<=[A-Za-z\\u4e00-\\u9fff])[\\u200c\\u200d]|[\\u200c\\u200d](?=[A-Za-z\\u4e00-\\u9fff])",
        "quarantined",
    ),
    ("unattributed_source_review", "provenance", "origin", "origin_unknown", "pending_review"),
)

STATEMENTS = (
    """CREATE TABLE injection_rules (
        rule_id TEXT PRIMARY KEY NOT NULL, category TEXT NOT NULL,
        matcher TEXT NOT NULL CHECK(matcher IN ('regex','origin')),
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
)


SEED_STATEMENTS = tuple(
    "INSERT INTO injection_rules(rule_id,category,matcher,pattern,disposition) VALUES ("
    + ",".join("'" + value.replace("'", "''") + "'" for value in rule)
    + ")"
    for rule in DEFAULT_RULES
)


def install(connection):
    """Create and seed screening tables inside the caller's upgrade transaction."""
    for statement in (*STATEMENTS, *SEED_STATEMENTS):
        connection.execute(statement)
