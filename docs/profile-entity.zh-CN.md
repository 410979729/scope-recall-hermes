# Profile / Entity 只读视图（3.0.0 候选）

> 本文描述**本工作区候选**上的共享 Core 读视图，以及 Hermes / Codex MCP 上的同名工具。协议仍是 **1.1**。这不是已安装生产 wheel 的承诺，也不表示可以发布、启用生产或做历史迁移。

`profile` 与 `entity` 只读已承认的结构化 claim。空数据或尚未巩固的原始对话必须如实报告缺口，**不会**把原始聊天、USER.md、MEMORY.md 或共现文本自动写成档案。

## 安装候选 vs 已安装实例

| 表面 | 有这些工具的条件 |
|------|------------------|
| 本目录候选（import `scope_recall` 指向本工作区） | 源码里已实现 `MemoryCore.profile` / `MemoryCore.entity` |
| 已安装的旧 3.0.0 wheel / 日常解释器 | **没有**这些工具，直到该候选经过复核、打包并安装 |
| 身份 / 路径 / 范围 | 只来自已初始化的宿主上下文，工具参数不能传入 |

`budget_tokens` 按**最终**规范化 JSON 的 UTF-8 字节计费（含 honesty 标记、身份字符串、provenance），**不是**分词器 token 数。默认 `max_items=16`、`budget_tokens=4096`。请求下限仍是 64，不会为了塞进空信封而抬高下限。若连最小的诚实空结果 / `unavailable` 信封都放不下，调用以 `ContractError(INPUT_INVALID, budget_tokens)` 拒绝；宿主错误包装**不是**视图结果。扫描封顶（候选并集在发布前裁到现有 200 对象栅栏内）时，`coverage` **绝不会**是 `complete_for_query`。`no_match` 只表示在当前范围与预算内没有可发布的当前项，**不是**全局不存在的证明。别名枚举触顶时不能证明唯一，返回未解析 / `ambiguous` 的部分缺口，不会按不完整前缀解析。

## 巩固前提

必须先有捕获 + 已接受的巩固（`accept_claim_proposals` / 已承认的 claim）。只有资格为当前有效、且证据仍存活的 claim 才能进入视图。

- `proposed` / `superseded` / `retracted` / 抑制或删除：不是当前事实
- `disputed`：单独放在 `disputed`（profile）或用 `temporal_status=disputed` 标注（entity），不会当成已决
- 意图：仅 `pending` 进入 profile 的 `pending_intentions`；已完成 / 取消 / 过期不与当前工作混写
- 原始事件单独存在、没有任何承认的 claim：`status=no_match`，`gaps` 含 `consolidation_required`，正文不含聊天原文

## `profile`

对**显式写出的主体名**做确定性分类视图。默认当前已确认 / 已合格事实：事实、偏好、约束、决定；待处理意图单独成节。

### 请求（`profile_request`）

必填：`protocol_version`（`1.1`）、`request_id`、`subject`、`max_items`（1–30）、`budget_tokens`（64–8000）。宿主工具可省略后两项，由适配器填默认值。禁止额外字段（含 `scope_id`、`project_id`、`data_directory`、`session_id`）。整数必须是 JSON integer，不能用 `true`/`1.0`。

```json
{
  "protocol_version": "1.1",
  "request_id": "profile-1",
  "subject": "TEST-project",
  "max_items": 16,
  "budget_tokens": 4096
}
```

### 响应要点

每条含 claim `ref`/`revision`、`temporal_status`、`evidence_refs`，以及证据上已承认的 `source_contexts`（`{platform, chat_type}`）。平台只来自已存储证据，不来自本次查询文本。

```json
{
  "protocol_version": "1.1",
  "request_id": "profile-1",
  "status": "ok",
  "memory_epoch": 12,
  "subject": "TEST-project",
  "resolved_subject": "TEST-project",
  "alias_resolution": "literal",
  "sections": {
    "facts": [
      {
        "ref": "claim-…",
        "revision": 2,
        "kind": "fact",
        "subject": "TEST-project",
        "predicate": "配色",
        "value_text": "H200",
        "conditions": [],
        "temporal_status": "current",
        "claim_state": "active",
        "valid_from": "2026-09-03T12:00:00.000000+00:00",
        "valid_to": null,
        "evidence_refs": ["event-…@1"],
        "basis": "direct_report",
        "source_contexts": [{"platform": "telegram", "chat_type": "private"}]
      }
    ],
    "preferences": [],
    "constraints": [],
    "decisions": [],
    "pending_intentions": []
  },
  "disputed": [],
  "gaps": [],
  "coverage": "complete_for_query",
  "answerability": "supported",
  "truncated": false,
  "scan_capped": false,
  "unmet_needs": []
}
```

`complete_for_query` 仅表示：**在当前允许的 scope / project / branch 内**，对这个显式主体的有界枚举没有触顶、也没有截断。它不是全库普查。

## `entity`

精确、证据接地的一跳视图。

| `action` | 含义 |
|----------|------|
| `probe` | 关于该主体的当前 **fact**（出边） |
| `related` | 直接记录的语句 / 关系 |

`direction`：`outgoing` | `incoming` | `both`。`probe` 固定作出边事实，忽略把方向理解成多跳。

- **出边**：库中记录的 `subject / predicate / value_text`
- **入边**：`value_text` **整段标量相等**于查询名（以及在别名已唯一解析时的规范主体）。不用共现、子串、逗号列表、自由文本枚举或改写来发明边
- 返回的 `value_text` 就是当时写下的宾语文本，**不**假装它有规范实体 ID
- `predicate` 若出现，必须完全相等
- 不递归、不画图、不自动合并身份

```json
{
  "protocol_version": "1.1",
  "request_id": "entity-1",
  "subject": "TEST-project",
  "action": "related",
  "direction": "incoming",
  "max_items": 16,
  "budget_tokens": 4096
}
```

入边命中示例（整段相等）：

```json
{
  "direction": "incoming",
  "subject": "TEST-alice",
  "predicate": "负责",
  "value_text": "TEST-project",
  "ref": "claim-…",
  "revision": 1,
  "kind": "fact",
  "conditions": ["未授权"],
  "temporal_status": "current",
  "claim_state": "active",
  "valid_from": "2026-09-01T12:00:00.000000+00:00",
  "valid_to": null,
  "evidence_refs": ["event-…@1"],
  "basis": "direct_report"
}
```

有条件的直接关系会保留 `conditions` 与 `valid_from` / `valid_to`，不会被渲染成无条件边。若记录的是 `value_text: "Bob, Carol"`，查询 `Bob` **不会**得到入边。入边枚举匹配**仍保留的版本**上的标量，再由 `select_effective` 与整段相等复检决定是否当前；过期 / 被替代的旧值不会当成当前。

## 显式别名（现有项目名契约，不放宽）

只复用**已经承认**的 `kind=alias` claim，并在读取时再次做：

- `validate_alias_target`：目标必须是仍存活的名称身份事实（谓词限于 `项目名称` / `项目名` / `名称` / `project_name` / `project name` / `name`）
- `validate_alias_source`：源证据仍绑定完整改名关系
- 别名与目标的证据都参与权限、删除、epoch 重验

当前 Core **有意**只认证显式项目名别名。读取路径不把任意人名别名放宽为可解析身份。字面主体字符串不会被静默正规化成另一个实体。

| 情况 | 结果 |
|------|------|
| 唯一、仍存活的项目别名 | `alias_resolution=resolved`，改用规范 `subject`；等价别名证明栅栏在确认唯一后去重并收成一对 alias+target |
| 同一别名指向多个主体，或字面主体与别名目标不一致 | `alias_resolution=ambiguous`，不合并、不任选；`gaps` 含 `alias_ambiguous` |
| 别名枚举触到候选上限 | 前缀不能证明唯一，返回 `ambiguous` 部分缺口，不按不完整前缀解析 |
| 人名别名或目标不是名称身份 | 保持不解析（多为 `proposed`，读视图当字面名） |

## 部分结果与只读

- `truncated` / `max_items_cap` / `budget_token_cap` / `scan_capped`：有界诚实部分结果，足够本切片；续读可选，本切片不实现游标
- 选择与发布之间数据变化：失败关闭，返回 `unavailable`，不带过期档案 / 边 / 别名正文
- 只读：不写 SQLite、不写审计、不入队、不碰向量；不需要 embedding / LLM 配置
- Hermes 与 Codex 共用同一套 Core 方法；适配器只做身份绑定与参数边界

机器可读 schema：`contracts/profile_request.schema.json`、`contracts/entity_request.schema.json`、`contracts/profile_view.schema.json`、`contracts/entity_view.schema.json`。
