# Scope Recall v1.1 — P00 开发基线

核查时间：2026-09-06 UTC。本文件中的“已有/部分”描述静态能力，不表示 v1.1 行为验收通过。

## 一页功能差异裁决

本项目尚未按下一代 v1.0/P00–P19 开工。现有软件是持续维护的 Hermes 插件主线；`pyproject.toml` 的包版本字段仍为 `2.0.1`，但当前主线不等于 `v2.0.1` 发布标签。未发现下一代 P/G 收据、独立 Codex 适配或 v1.1 公共契约。保留此前正确成果，从 P00 开始，不重做已修复的旧缺陷。

开发基线采用官方仓库 `https://github.com/410979729/scope-recall-hermes.git` 的 `578b955802df753f2e2208e26eab6f71971285a0`，tree 为 `be913d45073d0948acad7d11317cad43db65307b`。本轮 `git ls-remote`、克隆后 `rev-parse` 均核实。它包含 #72 的 `5944060` 及 #73 的 SQLite 写者交接修复。旧文档的 tmp/source 已由旧任务清理；清理收据仍在，故不存在“挑修改日期最近目录”的基线选择。

经用户纠正后再次核实，远端 `main` 仍为上述 SHA；`v2.0.1` 指向 `2f7900d1a482d5dd94295d86fdd28cdc99163784`。两者之间有 15 个提交、78 个变更文件（8,070 行新增、307 行删除），包括 #70 合入的 issue #65–#69 修复及 2026-09-05 合入的 #72、#73；#73 的合并说明明确对应 issue #71。隔离工作树自始基于最新主线，未从发布标签开发。以后分别记录发布标签、包版本字段、源码提交和活动安装指纹，禁止用同一个 `2.0.1` 字符串代替这些身份。复核证据见 `verification/P00/baseline-version-clarification.json`；原 P00 清单哈希对应提交 `1db6f09` 中的历史文件，不追写覆盖旧收据。

路线：目标核心的有界重构，保留 Journal、Fact Ledger、作用域、删除账本、原生隔离与已有预算保护的可验证语义；经验统一进 Claim，恢复状态归 Episode，宿主只做可信事件转换及唯一交付。尚无充分证据选择全核心重写，也不估算虚构复用比例。生产安装不变。

| B | 现状 | 当前代码依据 | v1.1 必须补齐／任务 |
|---|---|---|---|
| B01 | 部分 | `journal_store.py:144/340` 保留日志及长文本分段；`fact_repository.py:338` 有证据摘录 | 独立 origin、来源修订、语用/否定/限定词、EvidenceSpan 支持检查；P04/P05/P10 |
| B02 | 部分 | `sql_store.py:650` 有 task_episodes，`experience_models.py:52` 有目标/步骤 | Episode 来源化 ResumeState、未完成/阻塞、建议与已批准下一步区分、换会话现场复核；P07/P09 |
| B03 | 部分 | `aliases.py`、`artifacts.py:65` 有别名及 GitHub 锚点 | 证据化 alias、展示顺序、ReferenceBinding 歧义/澄清版本；P07/P08 |
| B04 | 部分，实际行为未证实 | `provider.py:465` 接 prefetch；`_internal/recall/prefetch.py:56` 使用当前 query，但忽略 session_id | 条件适用与历史候选同管线、无提示正例和无关反例、新会话证据；P08/P09/P11/P12 |
| B05 | 部分 | 现有召回编排、关系候选和 compiler | 一次定向补查、两跳/24对象上限、比较双方、确定集合分页和 coverage；P08/P09 |
| B06 | 部分 | `fact_repository.py:282` 有 valid/recorded、替代关系与状态，`fact_executor.py` 统一执行 | 条件例外/未知更新与新协议映射；`orchestrator.py:82` 的矛盾图按 score/updated_at 选赢家不能直接保留为事实裁决；P05/P06/P09 |
| B07 | 部分 | `experience_models.py:52`、`sql_store.py:671` 有条件 playbook/结果依据 | 迁入唯一 Claim/procedure 权威，保留根证据、反例和认可范围；P10/P15/P16 |
| B08 | 缺失 | 现有任务/经验状态不是有证据的 intention；当前类型和表映射未提供所需触发/取消/交付结构 | intention 版本、线索适用、取消和仅交付去重；P05/P07/P09/P10 |
| B09 | 部分 | Journal 分块与处理水位，DurableWorkDescriptor/Lease，向量 outbox | 来源修订+提取器版本的增量巩固，统一四类队列、无新增不反思、10k/100k检查；P04/P10/P13 |
| B10 | 部分 | `artifacts.py` 保留 URL/PR/commit 锚点；日志过滤内联 data URL | 获准图/SVG 的稳定版本、受限副本/位置、评价绑定及两宿主回读；P04/P07/P11/P12/P15 |
| B11 | 部分 | `lifecycle_policy.py`、`privacy_purge_schema.py`、`privacy_purge.py` 有生命周期/删除 | auto 抑制与显式查看的独立语义，pin 适用性、虚构/人格边界；P06/P09/P10 |
| B12 | 部分 | `orchestrator.py:44` 有阶段 trace；`compiler.py:266` 有旧 RecallPacket | 新 packet 的 status/answerability/coverage/basis、L1–L4 各层证据和宿主真实采用；P09/P13/P18 |

整组统计：已有完整验收 0，部分 11，缺失 1。所有组的 v1.1 运行效果均未证实；不将源码命名、旧单元测试或公开 M/J 规格当作行为通过。机器映射在 `verification/functional_traceability.json`，沿用原 B→F→M→P/G 关系。

## 路径与源码身份

| 用途 | 位置／身份 |
|---|---|
| 原始同版规格（只读保留） | `F:\SCOPERECALL更新项目\Scope_Recall_架构与实施完整包_v1.1\Scope_Recall_Architecture_v1_1` |
| Git 对象仓库 | `F:\SCOPERECALL更新项目\.repos\scope-recall`，clone --no-checkout |
| 唯一开发工作树 | `F:\SCOPERECALL更新项目\worktrees\scope-recall-v1.1` |
| 开发分支 | `codex/scope-recall-v1.1`，初始 clean，base 为上述 578b9558 |
| 专用 Python | `F:\SCOPERECALL更新项目\.venv\Scripts\python.exe`，由桌面捆绑 CPython 3.12.14 建立 |
| 开发证据 | 工作树 `verification/P00`；本文件和收据为 P00 唯一修改类别 |
| 旧候选包（非本期构建） | `F:\Agents\runtime\windows\hermes-yuheng\workspace\archive\scope-audit-20260905T095029Z\final-508e9918\package` |

原规格 `SHA256SUMS.txt` 的 77 项全部匹配，章节 01–07、20任务卡、7 Schema、C40/M60/J8、映射均存在。分章是规格源。旧候选 wheel SHA-256=`5eab1622fe200cdd24b41f3ba29a45ebfd74844cd51600cdb8bde15957c3cbb2`，sdist=`b304575e3bfa7dffc4ff40ebfba6e85933083f4d5d5fab20837550407a5ae2aa`，本轮只读复核相符；它们不属于 578b9558 的候选产物。

五个活动安装都位于 `F:\Agents\runtime\windows\hermes-<id>\plugins\scope-recall`，配置为相应 home 的 `config.yaml`，记忆位于相应 home 的 `scope-recall`。静态配置均选择 scope-recall，静态版本均为 2.0.1，安装目录无 Git。没有由版本字符串推导字节一致。

| 实例 | 相对开发基线的实质不同/缺少 Python 文件 | SQLite 头部 user_version | 宿主 dirty 路径数 |
|---|---:|---|---:|
| 玉衡 yuheng | 34 / 7 | 文件锁定，未证实 | 33 |
| 天枢 tianshu | 33 / 7 | 文件锁定，未证实 | 7 |
| 天璇 tianxuan | 33 / 7 | 10815 | 10 |
| 天姬 tianji | 34 / 7 | 10815 | 9 |
| 天权 tianquan | 33 / 7 | 10815 | 0 |

比较分母为基线 262 个运行 Python 文件；单独记录原始字节差异与 CRLF/LF 差异，实质不同数排除了仅换行差异。未包含安装额外文件，不能视为整个安装的等价证明。玉衡/天姬的 nightly_llm 另有差异。完整路径/哈希见 P00 安装比较收据。宿主源码均为 0.21.0、HEAD `79445a496c86a19332ad786494b8384d2167e2d0`；不同 dirty 内容全部保留，不拿干净天权目录覆盖其他实例。

SQLite 仅在可读实例读取主文件前100字节；没有连接数据库或读用户行。头部不是一致性 schema 审计，WAL/运行变化仍限制结论。顶层没有 lancedb 目录不等于没有向量投影，vector-generations 需按获准范围进一步定位。没有加载插件、运行 Doctor、复制记忆、调用付费模型或重启。运行进程是否加载当前磁盘字节未验证。

## 旧对象到目标职责和迁移保全

| 旧权威／入口 | 目标职责 | 保全与限制 |
|---|---|---|
| plugin.yaml、__init__.register、provider.ScopeRecallMemoryProvider | adapters/hermes | 公开注册可复用；P02 验证实际官方签名，不改宿主私有核心 |
| journal_entries、memory_journal_sources、journal_digest_runs | source_events/evidence_links/consolidate | 保留原文、真实未知时间、部分状态；现有 turn/role/content_hash 键须映射 occurrence/revision，不能伪造来源精度 |
| memories、fact_claims、fact_claim_evidence、fact_action_receipts | 唯一 claims/claim_versions/mutate | 保留当前/历史/uncertain/替代、有效与记录时间、scope；旧 memories 投影不能变为第二可写权威 |
| task_episodes、procedural_playbooks、playbook_versions、experience_runs | Episode + Claim/procedure | 保留结果依据、条件、反例和版本；ResumeState 不独立编辑事实 |
| privacy_purge_operations/tombstones/source_tombstones/vector_intents | deletion_operations及删除栅栏 | 保留读阻断和恢复账本；旧摘要/附件/任务载荷不能复活内容 |
| DurableWorkDescriptor/Lease、journal状态、vector_outbox | 单 work_items/worker | 保留重试类别、租约代次和版本；收敛为 consolidate/embed/rebuild_projection/purge |
| vector_generations/state/migration_receipts、Lance | projection_state | 空间与来源版本可追溯，可重建；不从向量正文绕过 SQLite |
| 单 recall orchestrator、deadline、weights、compiler | application.recall + domain规则 | 保留已修正确的预算/零权重/CJK边界；移除 Provider 私有状态依赖，新增同版 packet 评估字段 |
| PG/Bridge/独立Reflection/Skill写入/兼容shim | P15迁移 + P16退出 | 现在不删；需调用/注册/配置/兼容/迁移证据后退出新发行面 |

以上为源码语义映射，不声称已穷尽生产数据库字段或完成迁移。任何无法映射的真实旧字段必须保留未知，P03/P15 不得猜测丢弃。

## 规模、依赖和验证边界

只统计 Git 跟踪文件，排除环境、缓存、生成物、归档及活动数据。运行代码 262文件/114032行；测试 308文件/140126行；维护脚本 68文件/29140行；文档 76文件/9702行；其他33文件。行数包括空行/注释，不是复杂度评分。

AST 静态相对/绝对 import 映射得到964条仓内边、6个强连通候选组（17/6/4/4/2/2模块）。包含条件导入和延迟导入，不解析动态注册；它们不是6个运行时故障，也不据此报告死代码或重复率。完整图见 `verification/P00/imports.json`。

Python 支持范围来自 pyproject 为 >=3.11,<3.13；SQLite+Lance 为当前结构。`constraints/release-hashed.txt` 是旧完整发布闭包，`constraints/runtime-min.txt`/`runtime-max.txt` 是测试边界，不能以旧锁中的 Hermes 0.19.1 证明活动 0.21.0 接口兼容。P01 新环境实际依赖单独留证。

本轮 P00 **未运行插件测试**。读取旧 `scope-merge-72-73-20260905/combined-tests.json`：与相同 tree 绑定的19文件选择集是343 passed /1 skipped，79.10秒；这是历史证据。本轮不把早期4310项、旧wheel的9/9或历史CI移植成当前运行结果。

已静态审查 tests/conftest.py、tests/plugin_source.py、scripts/execution_boundary.py：测试会建立临时HOME/HERMES_HOME并复制插件树，autouse会导入truth_connection；不得直接对活动安装运行。未来通过专用环境和显式TEST父目录运行最小集合。scripts/check.release.py 包含大范围检查及构建，installer/managed_upgrade会改目标配置和插件，当前均不执行。规格里的 `scope-recall worker drain`、新 `scripts/check.py` 尚未实现。

历史问题的当前处置：统一 RequestDeadline、剩余预算封顶、candidate no-touch、事务所有权、SQL/Python生命周期与零权重已有实现和对应历史回归；保留，不按旧报告重复立Bug。B06矛盾图裁决、新会话绑定、原始来源到结构化提取、Codex Hook、附件精确版本仍须按新合同验证。

## P00 结论和后续

P00 已完成基线隔离、版本/路径核对、B/迁移映射及测试副作用调查，允许进入 P01。P00 不授予 G0 通过。

P01 固定 v1.1 单一 DTO、可信上下文、合成输入/断言隔离、最小测试选择器及接口差异；P02 验证两个实际宿主入口。G0 之前不进入 P03。120核心留出、每宿主40查询/8旅程及建议质量阈值仍待独立 G0 结合实测、模型和预算冻结，尚未批准也尚未执行。额外辅助/评测 API 无授权，真实记忆复制无授权，生产安装/迁移/重启/发布无授权。
