# Scope Recall 恢复与治理规划（2026-09-15 起）

本文是执行清单，不是讨论稿。每一步都有：目标、操作、验收、回滚、负责人。没有验收证据的步骤不算完成。
事实来源：`F:\t\SR-TIANSHU-RECALL-FIX-20260915\deploy-rc26-20260915\GOVERNANCE-ASSESSMENT.md` 及同目录原始输出。

## 0. 出发点（2026-09-15 14:40 的事实）

| 项 | 现状 |
|---|---|
| 生产实例 | 天枢 `hermes-tianshu`：包 3.1.0rc26（今日部署），receipt / wrapper plugin.yaml 仍写 rc5。天玑 `hermes-tianji`：包 3.1.0rc10，receipt rc10。其余四个 Hermes 实例未装。 |
| 源码谱系 | 规范仓库 `F:\SCOPERECALL更新项目\.repos\scope-recall`（远端 `github.com/410979729/scope-recall-hermes`）：远端 `main`=578b955（9/5），远端**没有** `release/3.1.0`；本地 `release/3.1.0`=bf8d6cb（rc22，未推送），checkout 在 `F:\t\SR-310-INTEGRATION-20260915`。生产谱系 rc21→rc26 只在两个无远端的独立仓库里（`F:\t\SR-TIANSHU-RECALL-FIX-20260915\src`、`F:\t\SR-RC11-FIX-20260913\src`），全部文件以 CRLF 提交。 |
| 真实差异 | 忽略换行符后 rc22→rc26 只差 31 个文件（+1553/−177），发布集 12 个文件。 |
| 树 vs 产品 | 378 个模块 / 152k 行在树里，136 个 / 45k 行进 wheel；244 个不发布模块无人引用。460 个测试文件，113 个进门禁，347 个从不运行。 |
| 门禁 | `scripts/check.py --tier {unit,contract,native,host,migration,packaging,integration,...}`，本地按需运行，无 CI。integration 约 10 分钟，1 个 flaky（supervisor）。 |
| 依赖漂移 | 天枢 venv：lancedb 0.37.1（pyproject `<0.31`）、mcp 2.0.0（pyproject `==2.1.0`）；开发 venv 与 pyproject 一致。生产跑的是门禁没测过的版本。 |
| 散落 | `F:\t` 174 个目录（15 个仓库、16 个 venv）；规范仓库注册 18 个 worktree，多数 detached。 |
| 已知缺陷 | 见第 4 节缺陷清单（D-01 … D-12）。 |

## 1. 从今天起生效的规则（写进 AGENTS.md「Release and deployment」）

1. 只有规范仓库的 `release/3.1.0`（以及将来的 `main`）能产出可部署 wheel；每个部署版本对应一个 tag `v3.1.0rcN`，一个 wheel，一个 sha256。
2. 生产只接受 `pip install --no-index --no-deps --force-reinstall <wheel>` + 计划内停机（`gateway.status.write_planned_stop_marker`），**永远不直接编辑 site-packages**。热补一次就必须当天发一个 rcN。
3. 升级后必须运行 `maintenance.cli apply-install`（先 `plan-install`）让 receipt / wrapper 元数据与包一致，再跑 `doctor`，三方版本（receipt、pip、running-code）不一致即为缺陷。
4. 操作 CLI 一律 `python -I -X utf8 -m scope_recall.maintenance.cli ...`，cwd 不得在源码树内。
5. 一个任务一个 linked worktree（`git worktree add`），任务合并后立即 `git worktree remove`；不再复制仓库，不再 `git init` 新的独立历史。
6. 一次只有一个写者改 `release/3.1.0`；其他 agent 在自己的分支上工作并以 PR/补丁形式交付，由写者合并并跑门禁。
7. 提交前跑改动所属层的门禁；合并到 `release/3.1.0` 前跑 `unit`+`contract`+`packaging`；发版前跑 `integration`+`native`。
8. 新 `.py` 必须要么进 `packaging/v11-module-allowlist.json`，要么进 `tests/` 并被某个 tier 选中；两者都不是的文件不允许进树。

## 2. 阶段 0：止血与统一谱系（今天，Cursor 执行）

### 0.1 把 rc23→rc26 落到规范仓库（LF）
- 目标：`release/3.1.0` 成为唯一生产谱系；独立仓库退役。
- 操作：在 hub 里已 `fetch` 独立仓库为 `refs/tmp/standalone-master`。在 `F:\t\SR-310-INTEGRATION-20260915` 上分三次提交回放（把 CRLF 转 LF 写入工作树再 commit）：
  1. rc23→rc25 修复（独立仓库 bf8d6cb-等价点 → `d3ba62b`）；
  2. 玉衡 9/15 上午热补的五个文件（`557b1af`）；
  3. rc26：recall_packet 精确测量 + lance helper 生命周期 + vector_failure 词表（`0d2a785`、`ec44139`）。
  然后补交 allowlist 要求但从未入库的 `distribution/codex/scope-recall/.codex-plugin/plugin.json`、`.mcp.json`，和玉衡的文档整理（三份报告改名进 `docs/implementation-history/`、两份 README、`.gitignore` 的 `/scope-recall/`）。
  加 `.gitattributes`：`* text=auto eol=lf`（保留现有 whitespace 规则）。
- 验收：`git diff --ignore-cr-at-eol release/3.1.0 refs/tmp/standalone-master -- <发布集>` 为空；`git ls-files --eol` 无 CRLF 索引对象；`packaging` tier 通过（allowlist 文件全部入库）。
- 回滚：`git reset --hard bf8d6cb`（推送前）。

### 0.2 顺手修（小、可测）
- D-09 `autostart plan/enable` 缺 `--python` 抛 TypeError：`--python` 默认 `sys.executable`，`--user-id` 默认当前登录用户；缺失时给出 `autostart_*_required` 而不是 Traceback。加单元测试。
- D-10 根目录 `scope_recall.py` 自引导 shim 导致 cwd 遮蔽安装包：删除（门禁通过 `plugin_source` 暂存树导入，不依赖它；唯一引用是 `probes/hermes/p11_test_wrapper.py`，随阶段 1 归档）。
- 验收：`unit`+`contract` tier 通过；`python -m scope_recall.maintenance.cli` 在树根目录运行时报 `No module named scope_recall`，而不是静默跑源码树。

### 0.3 发 rc27（第一个从规范仓库产出的版本）
- 操作：`_version.py` → `3.1.0rc27`；`python -m build.package_manifest` 盖章；CHANGELOG 写明 rc26 来自已退役的独立树、rc27 与 rc26 的代码差异仅为 0.2 两项 + 换行符；门禁 `unit`、`contract`、`native`、`packaging`、`integration`；`uv build --wheel`；记录 sha256。
- 验收：五个 tier 全绿（integration 允许重跑一次 flaky supervisor 用例并在 CHANGELOG 记录）；`build.package_manifest --check` 通过；wheel 内文件集合 == allowlist。
- 回滚：不部署即无影响。

### 0.4 推送
- 操作：`git tag v3.1.0rc27`；`git push origin release/3.1.0 v3.1.0rc27`。
- 验收：`git ls-remote origin` 出现 `refs/heads/release/3.1.0` 与 `refs/tags/v3.1.0rc27`，指向本地同一提交。
- 回滚：`git push origin :refs/tags/v3.1.0rc27`；分支保留。

### 0.5 部署天枢 rc27，并统一安装元数据
- 操作（与 rc26 相同，全部输出存 `F:\t\SR-TIANSHU-RECALL-FIX-20260915\deploy-rc27-20260915\`）：
  1. 备份 `site-packages\scope_recall` + dist-info、receipt、wrapper；
  2. `autostart pause`；
  3. `pip install --no-index --no-deps --force-reinstall <rc27.whl>`；
  4. 校验：`pip show`=rc27、安装文件哈希 == 树、`-I` 导入；
  5. `plan-install --host hermes --instance-root <天枢> --target-plugin-dir F:\Agents\shared\scope-recall\tianshu-installed-wrapper --project-root F:\t\SR-310-INTEGRATION-20260915 --python <天枢 venv python> --agent-id default`，无 conflicts 后 `apply-install`；
  6. `write_planned_stop_marker(<gateway pid>)`，等监督任务 15 秒内拉起新 gateway；
  7. `autostart enable`（同主体/同 env-file）；
  8. `doctor`：`package_version`=rc27、receipt=rc27、wrapper plugin.yaml=rc27、无 stale process；
  9. 只读 live 冒烟（1600/4096）。
- 验收：doctor 三方版本一致；gateway `running`、telegram/a2a `connected`；worker 一轮后 `idle`。
- 回滚：`predeploy-backup` 覆盖回去 + planned-stop；receipt 由 `apply-install` 自己备份在 `<instance>\.scope-recall-install-backups\`。

### 0.6 退役独立仓库、切换工作区
- 操作：在两个独立仓库根写 `RETIRED.md`（指向规范仓库、说明其历史已回放为哪些提交）；不删除目录（删除由用户确认后在 1.3 执行）。Cursor 工作区切到 `F:\t\SR-310-INTEGRATION-20260915`（或新建 `F:\SCOPERECALL更新项目\worktrees\release-3.1.0`）。
- 验收：`git -C hub remote remove tmp-standalone`；工作区打开的是规范仓库 worktree。

### 0.7 天玑评估（只读）
- 操作：天玑 `doctor`；比较 rc10 与 rc27 的 schema 版本与迁移路径（`maintenance.cli setup/migrate` 干跑）；记录 gateway 状态。
- 验收：产出「天玑升级方案」小节写入本文 1.7；不改动天玑。

## 3. 阶段 1：真相可见、树等于产品（本周）

### 1.1 doctor 三项新检查（D-06、D-07、D-08）
- `hot_patched`：对比 site-packages 文件哈希与 dist-info `RECORD`；任一不符即 gap。
- `dependency_drift`：对比已装 lancedb/pyarrow/mcp/jsonschema/PyYAML 与包内声明区间。
- `version_mismatch`：receipt.package_version、pip 版本、running-code 记录版本三方一致。
- 验收：在天枢上人为改一个字节 → doctor 报 `hot_patched`；还原后消失。契约测试各一个。

### 1.2 树瘦身（决策 D2）
- 操作：新建 `attic/`，`git mv` 244 个不发布模块（含 `_internal/` 中未发布的 55 个、根目录 185 个）与 347 个不进门禁测试及 `probes/`、`verification/`、`benchmarks/` 中无 tier 引用者；或直接删除（历史在 git）。allowlist 的 `python_modules` 不变。
- 验收：五个 tier 全绿；`packaging` 校验 wheel 文件集不变；仓库根目录只剩发布代码、`tests/`、`scripts/`、`packaging/`、`docs/`、`distribution/`、`build/`。
- 回滚：`git revert`。

### 1.3 清理散落（决策 D6，用户确认后执行）
- 操作：生成清单（路径、类型：linked worktree / 独立仓库 / 无 git 的快照、最后修改时间、是否有未提交改动、大小），用户勾选后删除；`git worktree prune`；`F:\SCOPERECALL更新项目\worktrees` 只保留有活跃分支的。
- 验收：`git worktree list` ≤ 3 条；`F:\t` 只剩本周仍在用的目录。

### 1.4 依赖版本决策（D1）
- 操作：scratch venv 装生产版本（lancedb 0.37.1、mcp 2.0.0）跑 `native`+`host` tier。通过则 pyproject 放宽并记录；不通过则把天枢/天玑 venv 钉回 pyproject 版本（`pip install "lancedb>=0.30.2,<0.31" "mcp==2.1.0"`，需 planned-stop）。
- 验收：doctor `dependency_drift` 为空。

### 1.5 doctor 与 scheduler 共享 `settled_waiting_sweep` 谓词（D-05）
- 操作：把 `schedule_settled` 的过滤条件抽成 `core/candidate_storage.py` 一个查询，doctor 调同一函数。
- 验收：doctor 计数 == 实际可调度数；契约测试。

### 1.6 CI
- 操作：GitHub Actions：push/PR → `unit`+`contract`+`packaging`；每日 → `integration`+`native`（Windows runner）；flaky supervisor 用例注入可控时钟或标记 `@pytest.mark.flaky(reruns=1)` 并开 issue。
- 验收：`release/3.1.0` 上连续 3 次绿。

### 1.7 天玑升级 rc10 → rc27
- 操作：按 0.7 的评估结果：备份 `memory.sqlite3`（`maintenance.cli backup`）→ `migrate` 干跑 → 停 worker 任务 → pip → `apply-install` → planned-stop → `migrate` 应用 → doctor。
- 验收：doctor `ok/attention`；召回冒烟有结果；`retry-failures` 干跑为 0。
- 回滚：`maintenance.cli rollback --snapshot`。

## 4. 阶段 2：召回质量（下两周，各需拍板）

| 编号 | 缺陷 | 方案 | 验收 |
|---|---|---|---|
| D-01 | 冷连接嵌入 ≈3.1s，超过向量预算 ≈3.3s，语义通道多数退化为词法 | 持久化 HTTPS helper 复用连接（凭据不离开 helper 进程，需安全评审）；嵌入 deadline 用 RTT EMA 自适应；先临时把冷请求的向量份额提到 90% | 冷启动向量超时率 <10%，中位延迟 <1.2s，`vector_error:*timeout` 在 24h 日志中 <5% |
| D-02 | admission 非单调：3398B 重复导入事件挤掉 773B 命中项 | 排序加体积惩罚（占预算比例）；导入去重在写入时做 | 1600/3200/4096 三档均命中 `a4b26d20`；新增 fixture |
| D-03 | `derivation_invalid` 一次即终态（68 条） | 一次 schema 修复重试（把校验错误回填提示词），再失败进 `needs_review` 桶并从 `work_failed` 中分离 | doctor 不再因历史终态失败停在 `attention`；桶大小可见 |
| D-04 | CJK 文本 token 估算约 3 倍惩罚 | 按字符类别校准估算（CJK 每字 ≈1 token） | 同一中文 packet 在 4096 下条目数提升且 `bytes/budget` 报告一致 |

## 5. 阶段 3：结构与宿主（持续）

- 拆分 `maintenance/migrate_v2.py`（3719 行）为编排 / 旧数据转换 / 宿主激活 / 后台索引四段，各自有边界测试；`adapters/hermes/provider.py`、`core/worker.py` 同理。
- 宿主任务（gateway、worker）改为非交互主体（S4U 或服务），注销不再杀 gateway；或在 Hermes 文档明确该约束。
- 3.1.0 转正条件：连续 7 天 doctor 无 `hot_patched`/`version_mismatch`/`dependency_drift`；CI 连续绿；D-01～D-03 关闭。

## 6. 缺陷清单

| 编号 | 描述 | 状态 |
|---|---|---|
| D-01 | 嵌入冷连接超时导致语义通道退化 | 阶段 2 |
| D-02 | admission 非单调 | 阶段 2 |
| D-03 | `derivation_invalid` 一次终态 | 阶段 2 |
| D-04 | CJK token 估算惩罚 | 阶段 2 |
| D-05 | doctor/scheduler `settled_waiting_sweep` 计数不一致 | 阶段 1.5 |
| D-06 | 热补不可见 | 阶段 1.1 |
| D-07 | 依赖漂移不可见 | 阶段 1.1 / 1.4 |
| D-08 | receipt / wrapper / pip 版本三方不一致 | 阶段 0.5 修数据，1.1 加检查 |
| D-09 | `autostart` 缺参 Traceback | 阶段 0.2 |
| D-10 | 根目录 shim 遮蔽安装包 | 阶段 0.2 |
| D-11 | flaky supervisor 测试 | 阶段 1.6 |
| D-12 | 天玑停留在 rc10 | 阶段 0.7 评估，1.7 执行 |
| 已修 | recall 有候选返回 0 条；lance helper 超时中毒；五个热补文件未入库；两个回归测试不在 tier；8 个可重试失败 | rc26 |

## 7. 需要用户拍板的决策

- D1 依赖：放宽 pyproject 到生产版本，还是把生产钉回 pyproject？（1.4 的门禁结果出来后决定）
- D2 树瘦身：`attic/` 还是直接删除？（推荐删除；历史在 git）
- D3 嵌入传输：是否接受一个常驻 helper 进程持有嵌入凭据？（需安全评审）
- D4 `derivation_invalid`：重试一次还是保持终态只加 review 桶？
- D5 天玑：升级到 rc27，还是卸载？（天玑是否还在用记忆）
- D6 清理清单：哪些目录可以删除。

## 8. 禁止事项

- 不在 site-packages 里改文件；不在没有 tag 的提交上构建 wheel；不 `git init` 新仓库；不把 `F:\t` 下的目录当长期工作区。
- 不在 integration 门禁红的情况下部署；不用 1200 之类非生产预算做验收（生产 `budget_tokens`=4096）。
- 多跳查询不推断身份、不放宽 audience；后台任务不无限重试（AGENTS.md）。
