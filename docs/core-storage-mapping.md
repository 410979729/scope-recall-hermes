# P03：单一核心存储与旧数据映射

本地目标基线为 `8c24a5e`，G0 独立通过候选为 `17943a0`。此映射在核心编码前记录；它是实施草案，不是已经运行的迁移收据。活动实例和旧数据库不在本次修改范围。

## 采用的边界

新入口使用显式 `InstanceBinding`、`TrustedContext`、存储、时钟、ID 生成器、向量端口与辅助模型端口。构造和 import 无文件、数据库或网络副作用。初始化是显式维护动作，正常运行仅核对已有 schema 和身份，不自动迁移。

继续使用经过旧测试验证的 `truth_connection.connect_truth_database`：路径保护、FK、只读连接与连接级 writer lease。每次短事务单独打开并关闭连接，外部模型调用不持有连接或 lease。SQL 仅在 `core/storage.py` 及其后续存储职责文件内；应用只接触受控事务对象，不能执行任意 SQL、commit 或 rollback。借用事务使用受控 savepoint。释放成功后立即注销 savepoint，commit 失败不清理已释放的 savepoint；清理异常作为主异常的附加诊断保留，连接弃用。

选择版本化新目标库而不直接复用旧 DDL，原因有三个：旧 Journal 唯一键包含正文哈希，不能约束同发生身份同版本的冲突；旧 fact_claims 混合事实身份与版本且关联 memories，不能让它与新 claims.current_revision 同时作为可写或可查询的当前权威（旧表可作只读迁移输入和历史存档）；Journal/nightly/vector 三套任务原生表需要收敛为一个 work 状态所有者。复用这些表的来源、时间、删除、幂等语义和已验证连接保护，不把两个 schema 同时接入新运行入口。旧运行入口暂留外层，P16 退出。

P03 首先落实 instance_meta、授权 scope、source_events、work_items 的目标 DDL、索引和受控访问；其他逻辑对象在各自任务增加同一数据库的版本化表和访问方法。每次结构变更提升 schema version，并只在显式维护入口升级隔离目标库。不存在先落到第二数据库、以后再同步的过渡路径。

## 唯一目标与保全内容

| 旧结构 | 唯一目标 / 实施任务 | 必须保全 |
|---|---|---|
| 安装配置与身份记录 | instance_meta、instance_scopes / P03 | agent、installation、固定规范化目录、schema/config version、epoch、测试模式；复制目录不取得原身份 |
| journal_entries | source_events / P03–P04，旧数据 P14 | 原始 ID 映射、稳定发生键/修订、scope/shared scope、session/turn/platform/user/chat/thread/agent/workspace、角色、origin、正文、内容哈希、发生与记录时间、捕获完整性；旧表缺 origin 时从可核验导入上下文判定，未知保持 origin_unknown，不从 role 猜测；原始 metadata 经受限迁移存档 |
| memories | 来源或有根证据的 Claim / P14 | 原 ID、正文、summary、来源、时间、scope、旧元数据。无可靠来源或语义资格的旧正文保留为 imported/origin_unknown，不自动升级 active |
| fact_claims | claims + claim_versions / P05、P14 | 身份与版本分离；scope、subject、predicate、kind、cardinality、assertion_kind、value、conditions、有效时间、recorded_from/to、status、supersession、confidence 与原始键。仅 claims.current_revision 一个指针 |
| memory_journal_sources、fact_claim_evidence、memory_digest_sources | evidence_links / P04–P07、P14 | 派生对象及版本到真实来源及版本、relation、quote/原位置、证据根、原 run/时间；反向索引用于删除传播 |
| task_episodes | episodes + episode_versions + episode_events / P07、P14 | scope、project/branch/session、目标、状态、结果、事件顺序、时间、失败/取消/中断、证据和未完成事项 |
| ResumeState | episode_versions 内受 schema 限制的结构 / P07 | goal、decisions、verified_progress、open_items、blockers、next_step_basis、artifact refs、source watermark；所有摘要仍依赖来源 |
| 旧产物引用、memory metadata 中产物 | artifact_refs 的版本记录 / P07、P14 | 稳定 ref、revision、hash、媒体、获准位置/副本、关联事件、保留权限与缺失状态；不把路径作为内容版本 |
| ReferenceBinding、旧 alias | 事件派生的版本化引用记录；稳定别名为 kind=alias / P07 | mention、候选、resolved_ref、resolution、证据、scope；不从目录排序推断展示顺序 |
| procedural_playbooks、playbook_versions、experience_runs | kind=procedure 的 claim_versions / P05、P10、P14 | conditions、non_applicable、method、support、counterexamples、verification_basis、运行结果证据、历史版本；退出独立 Experience/Reflection/Skill 真值运行面 |
| intention、preference、constraint、decision | 同一 claims/claim_versions / P05、P07 | 意图 cue/target/state/conditions/completion_or_cancel_evidence（协议字段 state_evidence_refs）；pending 不能因交付提醒变 completed；各类保留条件、来源和时间 |
| journal_digest_runs、Journal 处理列、nightly_digest_runs、vector_outbox | work_items / P03、P10、P14 | 逻辑唯一键、subject/version、pending/leased/done/failed/obsolete、attempt、available_at、lease token/until、输入水位、错误/进度。旧任务只由迁移器转换，不由三套 worker 并行继续写 |
| vector_generations、vector_generation_state、向量状态 | projection_state / P08、P14 | 对象/版本、空间（模型/维度/预处理/距离/归一化）、generation、就绪/错误。可重建，不持有当前正文权威 |
| memories_fts、fact_claims_fts | lexical_projection / P04、P08 | 只保留对象/版本与可重建词项；返回前仍查 SQLite 当前 scope/版本/删除 |
| privacy_purge_operations/tombstones/source_tombstones/vector_intents | deletion_operations 与目标栅栏 / P06、P14 | op、目标及依赖、scope、epoch、deny_active、各层清理状态与时间；尽量不保留正文，迁移和恢复必须重放栅栏 |
| fact_action_receipts | operation_receipts / P05–P06、P14 | 幂等键、请求哈希、scope、动作、结果状态、版本、错误与时间；收据不是证据，删除时清除含正文的旧收据 |

未解释的旧 metadata 不参与权限或当前事实判定。P14 对每个字段分类为结构化转换、受限原始存档或因删除栅栏擦除，给出数量、引用和内容校验；不默认丢弃。旧共享范围不得通过目标 Agent 身份自动变成私有范围，映射需由显式迁移配置给出。

## 索引与事务草案

来源唯一键为 `(source_event_key, source_revision)`，installation 固定在 instance_meta。event_id 由 installation 与稳定发生键确定，版本不是新发生。source_events 的 scope/session/project/branch/origin/time/state/revision 使用受约束列。正文保留原字节对应的 UTF-8 文本与指纹；额外协议字段使用有界 JSON。

按 scope 与发生时间扫描、按来源反向引用、按主张 scope/subject/predicate、work 唯一键和 ready 顺序、projection 对象/版本/空间建立必要索引。各自查询实现时保存 EXPLAIN QUERY PLAN 证据，不凭表数量增加索引。

capture 的线性化点是 source + lexical + 必需 work 的同事务 commit。后续 mutate、delete、consolidate 共用事务入口。写入 busy timeout 受调用者剩余预算限制；读连接用 mode=ro + query_only，不修索引或更新可信度。

## 迁移与兼容退出

P14 在独立目标目录读取获准旧快照，创建目标 schema，转换根来源→派生版本→证据→删除栅栏→工作/投影。完整校验前不激活；原库保持可恢复。回滚使用经过核验的旧快照及旧运行包，不让旧包写新 schema。P15 处理备份、恢复、删除清单重放与身份再绑定。

P16 必须移除新运行链对 provider.py、recall.py 中 Provider 私有字段、recall_source_adapters、_internal/journal/runtime、_internal/experience/runtime 的依赖；旧 provider、playbook/reflection/skill、旧存储写入口及 pgvector 运行支持不进入最终新入口。暂留文件用于旧数据解释和回归，不视为两套同时支持的插件。具体退出清单在 P16 逐项核验。

旧 fact_claims.recorded_at 映射 recorded_from；可核验 retired_at 映射 recorded_to，否则保持 NULL，不以导入时间伪造历史结束。instance_scopes 只保存安装受信 scope_id 清单，不把它视为会话 ACL；每次访问仍要求 TrustedContext.allowed_scope_ids 的子集。旧 shared_scope_id、范围种类和主体绑定由 P14 的显式旧范围映射及 P11/P12 的可信宿主上下文保全，模型不参与授权判定。
