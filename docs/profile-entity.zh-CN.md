# Profile / Entity 只读视图

本文描述共享 Core 的读视图，以及 Hermes / Codex MCP 上的同名工具。协议版本为 **1.1**。

`profile` 与 `entity` 只读已承认的结构化 claim。空数据或尚未巩固的原始对话必须如实报告缺口，**不会**把原始聊天、USER.md、MEMORY.md 或共现文本自动写成档案。身份、路径与范围只来自已初始化的宿主上下文，工具参数不能传入。

`budget_tokens` 按**最终**规范化 JSON 的 UTF-8 字节计费（含 honesty 标记、身份字符串、provenance），**不是**分词器 token 数。默认 `max_items=16`、`budget_tokens=4096`。请求下限是 64，不会为了塞进空信封而抬高下限。若连最小的诚实空结果 / `unavailable` 信封都放不下，调用以 `ContractError(INPUT_INVALID, budget_tokens)` 拒绝；宿主错误包装**不是**视图结果。扫描封顶（候选并集在发布前裁到 200 对象栅栏内）时，`coverage` **绝不会**是 `complete_for_query`。`no_match` 只表示在当前范围与预算内没有可发布的当前项，**不是**全局不存在的证明。别名枚举触顶时不能证明唯一，返回未解析 / `ambiguous` 的部分缺口，不会按不完整前缀解析。

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
  "subject": "示例项目",
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
  "subject": "示例项目",
  "resolved_subject": "示例项目",
  "alias_resolution": "literal",
  "sections": {
    "facts": [
      {
        "ref": "claim-…",
        "revision": 2,
        "kind": "fact",
        "subject": "示例项目",
        "predicate": "配色",
        "value_text": "蓝色",
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
  "subject": "示例项目",
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
  "subject": "小林",
  "predicate": "负责",
  "value_text": "示例项目",
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

## 已验证的名称关系

读取只使用已生效的 `kind=alias` 记录。开启范围的发布前审查后，自动身份关系也须先批准。明确项目改名需要名称身份事实和完整改名证据；
模型判断的实体关系需要保存的身份判断证据，并绑定两端记录的具体版本。
两类关系都重验来源、范围、项目、分支、有效期、撤回和删除状态，不在读取时推测身份。

| 情况 | 结果 |
| --- | --- |
| 唯一且证据有效的关系 | `alias_resolution=resolved`，读取规范主体及其已验证的等价名称 |
| 同名对应不同主体或不同范围 | `alias_resolution=ambiguous`，不任选一个主体 |
| 枚举触及候选上限 | 返回 `ambiguous` 和部分覆盖缺口 |
| 关系未生效、已撤回或两端证据失效 | 按原名称查询 |


## 部分结果与只读

- `truncated` / `max_items_cap` / `budget_token_cap` / `scan_capped`：有界、如实的部分结果；没有续读游标
- 选择与发布之间数据变化：失败关闭，返回 `unavailable`，不带过期档案 / 边 / 别名正文
- 视图查询不修改事实、不写审计、不入队、不碰向量；不需要 embedding / LLM 配置。访问旧结构前仍可能触发协调升级
- Hermes 与 Codex 共用同一套 Core 方法；适配器只做身份绑定与参数边界

机器可读 schema：`contracts/profile_request.schema.json`、`contracts/entity_request.schema.json`、`contracts/profile_view.schema.json`、`contracts/entity_view.schema.json`。


## 实体名称与合并

提炼可以给出规范实体名，并在 `entity_type` 和 `entity_names` 中保留类型与证据里的原称。
旧记录没有这些可选字段时，类型按 `unknown`、原称按空列表处理。实体范围包括人、动物和物品、地点、组织及项目。
同名人物应使用原文支持的职业、所属组织或地点消歧；仅有名字相同或拼写相似不足以合并。

后台 worker 先通过现有向量搜索和字面相似度查找候选，再通过已配置的辅助模型判断身份。
模型只可选择已提供的记录版本；不确定或不可用时不合并。模型请求受同一预算账本和私密模式约束。
结果保存为带原文证据的别名记录，两端原事实保持原样。范围、项目和分支必须完全相同。
已有的明确项目改名仍然可用。
判断结果按范围内的名字保存，后续事实中的主语和原称均已判断时不再请求模型。
端点改版或删除使对应名字重新进入有界回填；主人明确撤回的关系保持撤回。

`entity` 同时读取已合并名称下的一跳记录；`trace` 沿这些实体的已记录关系行走，条件和跳数限制照常。
召回只把问题中的名称作为同一实体的等价称呼匹配，不额外沿实体关系扩展。
同一位置出现多个已知名称时，优先匹配完整的较长名称，避免短别名改写另一个实体的名称。
撤回别名记录后，查询按原名称工作；两端记录或证据失效时，该别名也停止生效。

首次打开旧库先完成存储升级，实体解析由 worker 分批处理。回填完成前，原名称的查询仍可使用。
Doctor 的 `entity_resolution` 检查显示已处理和仍待处理的记录数。需要 worker 提供身份判断能力；
该能力不可用时进度保留，已有记忆照常按原名称查询。
