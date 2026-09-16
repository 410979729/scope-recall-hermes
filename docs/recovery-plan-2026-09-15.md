# Scope Recall 恢复与治理规划（2026-09-15 起）

## 交接卡（接手的 agent 先读这一节；2026-09-16 01:45Z）

| 项 | 值 |
|---|---|
| 唯一源码真相 | 规范仓库 `F:\SCOPERECALL更新项目\.repos\scope-recall`，分支 `release/3.1.0`，远端 `github.com/410979729/scope-recall-hermes`（`main` 只是 9/5 的旧祖先，落后 16 个提交，勿在其上开发）。 |
| 干活的目录 | linked worktree `F:\SCOPERECALL更新项目\worktrees\release-3.1.0`（= `release/3.1.0`，最新提交见 `git log -1`）。新任务用 `git worktree add`，合并后 `git worktree remove`；不复制目录，不 `git init`。 |
| 当前版本 | `3.1.0rc28`（tag `v3.1.0rc28`）。版本只改 `_version.py`，然后 `python -I scripts/build.package_manifest.py --write` 盖章清单。 |
| 门禁 | 在 worktree 里 `python -X utf8 scripts/check.py --tier <unit|contract|host|packaging|native|integration>`；今天用的解释器 `F:\t\SR-TIANSHU-RECALL-FIX-20260915\.venv\Scripts\python.exe`（3.11）。发版前六层全绿。 |
| 三套安装 | 三台都是 `3.1.0rc28`（2026-09-16 核过：import = dist = receipt = wrapper）。天枢 `F:\Agents\runtime\windows\hermes-tianshu`；天玑 `F:\Agents\runtime\windows\hermes-tianji`；Codex `F:\ScopeRecall\codex`（venv `F:\ScopeRecall\codex-venv`，插件目录 `C:\Users\w4109\plugins\scope-recall-codex`）。回执：`deploy-rc27-20260915\`、`deploy-rc28-20260916\`。 |
| 树的现状（不要误以为已经干净） | 版本谱系与发布流程已治理；**树本身还没瘦身**：git 里 810 个非测试 `.py`，wheel 只装 140 个，672 个不进 wheel（`verification/` 387 是证据脚本、`_internal/` 58、`probes/` 33、`benchmarks/` 7，以及根目录约 180 个游离模块）。这是 1.2/D2，等用户拍板。 |
| 机器上的散落 | hub 仍注册 17 个其他 worktree（多数 detached）、`F:\t` 170 余个目录、两个已退役独立仓库——1.3/D6，用户勾选后才删。 |
| 已停用的路径 | `F:\t\SR-310-INTEGRATION-20260915`（已迁到上面的 worktree 路径）；`F:\t\SR-TIANSHU-RECALL-FIX-20260915\src` 与另一独立仓库（根有 `RETIRED.md`）。 |

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

## 0.1 执行记录（2026-09-16 01:30Z 更新）

阶段 0 已完成的步骤与结果；证据在 `F:\t\SR-TIANSHU-RECALL-FIX-20260915\deploy-rc27-20260915\`。

| 步 | 结果 |
|---|---|
| 0.1 谱系 | `release/3.1.0` = `b8b408b`：`6d37525` 规范化换行（512 个索引对象 CRLF→LF）、`0313bfe`/`6fa48d9`/`dd8a6c0` 回放 rc23–rc26、`15ffea7` 补交 Codex 清单与文档整理、`b8b408b` `verification/** -text` 并按独立树字节存回 279 个证据文件。回放后按内容比对独立树：只剩 `.gitattributes` 与 3 个脚本的可执行位。 |
| 0.2 顺手修 | `102d79e` autostart 默认参数 + JSON 契约错误 + 删 shim + AGENTS.md 规则；`590a270` manifest 工具写 LF。新增 `tests/contract/test_autostart_cli.py` 进 `contract`。 |
| 0.3 rc27 | `37b75e8` 盖章；门禁 unit 29 / contract 96 / native 48 / packaging 118 / integration 1684，0 failed；wheel sha256 `B342D692…A4832`，158 个文件，0 个 CRLF。 |
| 0.4 推送 | 远端 `release/3.1.0` = `b8b408b`，tag `v3.1.0rc27`。 |
| 0.5 天枢 | rc27 装入，`apply-install` 后 receipt = wrapper = pip = rc27（原 rc5/rc5/rc26）；planned-stop 3 s 退出，新 gateway pid 31260；doctor `attention`（仅 68 条终态 `derivation_invalid`）。 |
| 0.6 退役 | 两个独立仓库写入 `RETIRED.md`；hub 删除临时远端；目录保留待用户确认删除（1.3）。 |
| 0.7 → 1.7 天玑 | 评估后直接执行：schema 同为 1108，纯代码替换。rc10 → rc27 装入，三方版本 rc27，planned-stop 6 s，新 gateway pid 30872。doctor 仍 `degraded`，但原因不是代码：`worker_capability_unavailable`（外部整合未获批准，20,644 条 consolidate 待处理）、`source_processing_deferred` 940、`capture_ingress_blocked`（D-13）。 |

执行中发现并补进清单的事实：

- **第三套安装**：Codex 宿主 `F:\ScopeRecall\codex`（venv `F:\ScopeRecall\codex-venv`，包 3.1.0rc5，项目根 `F:\SCOPERECALL更新项目`，worker 任务 `ScopeRecall-a432855c…` 每 5 分钟运行）。统一到 rc27 列为 1.8。
- **D-13** 天玑 `capture_inbox` 4 条采集卡在 `VERSION_CONFLICT`（2026-09-14 两条、09-15 两条），worker `ingress_replayed=0`，doctor 因此永远 `degraded`。需查重放路径为何不处理该错误码。
- **D-14** 原地 `pip install --force-reinstall` 在 gateway 运行时可能因目录句柄失败（天玑：WinError 32，pip 已把旧包挪到 `~cope_recall` 暂存后停下，包处于半拆状态）。恢复办法：立刻再执行一次不带 `--force-reinstall` 的 `pip install`，然后删除 `site-packages` 下 `~*` 暂存目录。写进 0.5 的操作步骤，并在阶段 1 把部署脚本化。
- 证据摘要依赖检出换行：`scripts/model_receipt_evidence.py` 钉住的 sha256 是 CRLF 字节，而仓库一直存 LF，只在 Windows 检出上恰好通过；已用 `verification/** -text` 修正。
- 天玑机器上还挂着 9 个历史计划任务（`Tianji Scope Recall Closeout 20260828`、`Exclusive Maintenance`、`Upgrade Controller`、`Yuheng ScopeRecall Deploy 20260823` 等）和一份失败的旧 supervisor 文件（`runtime-supervisor-1314a4fc…`，exit 124）。列入 1.3 清理清单。

### 1.8 Codex 安装升级 rc5 → rc28（阶段 1 新增；2026-09-16 01:25Z 完成）
- 操作：确认 Codex 插件目录（`~/.codex/config.toml` 中 `scope-recall-codex@personal`）与 `codex-installation.json` 的绑定；`pip install` 到 `F:\ScopeRecall\codex-venv`；`plan-install --host codex` 无 conflicts 后 `apply-install`；worker 任务 pause/enable；MCP 服务进程随下一次 Codex 会话自然换代。
- 验收：`doctor --host codex` 三方版本一致；一次 Codex 会话内 recall 有结果。

执行记录（证据在 `F:\t\SR-TIANSHU-RECALL-FIX-20260915\deploy-rc28-20260916\codex\`）：

| 步 | 结果 |
|---|---|
| 侦察 | 插件目录 `C:\Users\w4109\plugins\scope-recall-codex`；schema 1108（与 rc27/rc28 相同，无迁移）；venv 是 Codex 自带运行时 3.12.14（uv 建、无 pip），mcp 2.1.0 / lancedb 0.30.2 正好落在 `pyproject` 范围内。无 MCP/worker 进程在跑。 |
| 根因 | rc5 装完 40 秒后，有人在插件目录手写 `scripts/local_runtime.py` + `hooks/codex-local.cmd`，并改写 `.mcp.json`/`hooks.json`/`plugin.json`——receipt 里 4 个文件 3 个被改、2 个文件不在 receipt。原因是**产品缺口**：Codex 用自己的环境拉起 MCP 服务与 hook 进程，发布代码里只有 worker（`resume_entry.py`）会从 env 文件读嵌入凭据，所以安装器生成的包装层拿不到 API key，只能靶向热补。这就是 D-15 的真面目。 |
| 修法 | rc28（`4b2a921`）：`plan-install/apply-install --host codex --env-file`，写进 `.mcp.json`、`hooks.json`、`scope-recall-hook.cmd` 与 receipt；`mcp_entry`/`hook_entry` 接 `--env-file`，通过 `runtime.resume_entry.host_process_credential_environment` 复用 worker 的 `credential_environment` 契约（只读配置声明的凭据名，不解释 dotenv）；文件不可读只写 stderr、进程照常启动，hook 永不因此失败；`--host hermes` 拒绝该参数。新测试 `tests/host/codex/test_env_file_credentials.py` 进 `host`，`test_install_v11` 加包装层/receipt 用例。门禁 unit 29 / contract 96 / host 122 / packaging 119 / native 48 / integration 1688，0 failed。tag `v3.1.0rc28` 已推送。 |
| 部署 | 备份插件目录、rc5 包与 dist-info、receipt、`codex-installation.json`、两份 sqlite（sha256 校验一致）→ `autostart pause` → `uv pip install --reinstall` rc5→rc28（0 个残留文件）→ `apply-uninstall`（保留数据）→ 删除 3 个被改文件与 2 个热补文件 → `plan-install --env-file F:\ScopeRecall\codex\embedding.env` 0 conflicts、`reuse_instance=true` → `apply-install` → `autostart enable --env-file`。receipt = wrapper = 包 = rc28，`installation_id` 不变。 |
| 验收 | doctor：package ok rc28、binding ok、schema 1108。MCP stdio 冒烟（客户端环境里去掉全部 `SCOPE_RECALL_*`，只靠 `--env-file`）：initialize 成功、9 个工具、3 次 recall 1.6–2.3 s 无 vector 缺口，预算账本同时刻出现 3 条 `http_200` 嵌入请求——凭据链路成立。worker：`resume_entry` 返回 `capability_unavailable / launched=false`，supervisor `blocked`，与 rc5 最后一轮相同（见下）。 |

由此新增的事实：

- **Codex hooks 需要重新信任**：`hooks.json` 内容变了，`config.toml` 里 `hooks.state."scope-recall-codex@personal:hooks/hooks.json:*"` 的 `trusted_hash` 已不匹配，而且这些条目原本就没有 `enabled = true`（同机另一个测试项目的条目有）。下次打开 Codex 时按提示批准，然后 `doctor` 的 `hook_trust` 才会离开 `pending`。
- **Codex 实例积压与天玑同病**：199 条 `consolidate` 待处理且外部整合未批准（D5 的第三个实例）、24 条 `embed` 失败码 `http_400`（嵌入 API 拒绝请求体，非终态，需查是超长还是空内容）。supervisor 因此 `blocked` 到 `deadline_at`，resume 不拉起 worker——这是设计行为，不是故障，但 doctor 会一直 `degraded`。
- **D8 已关**：用户 2026-09-16 说天玑 Grok 用完、三台对齐。核验结果：天枢/天玑 receipt 已于 22:05/22:07 写成 rc28（Grok 治理期间已装上），import = dist = wrapper = receipt = `3.1.0rc28`，与 Codex 相同。未再重启 gateway（包已在跑）。doctor：天枢 `attention`（68 条终态 `derivation_invalid`，D4）；天玑 `degraded`（外部整合未批准 + 延迟采集，D5）。
- `pip` 路径在 uv venv 上不可用（无 pip 模块）；部署脚本化（1.3）时用 `uv pip install --python <venv python>`，它按 RECORD 卸旧装新，比 `pip --force-reinstall` 少一个 D-14 的坑。

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
  3. `pip install --no-index --no-deps --force-reinstall <rc27.whl>`；若报 `WinError 32`（gateway 持有包目录句柄，pip 已把旧包挪进 `~cope_recall`），立刻再执行一次不带 `--force-reinstall` 的 `pip install`，再删除 `site-packages\~*`（D-14）；
  4. 校验：`pip show`=rc27、安装文件哈希 == 树、`-I` 导入；
  5. `plan-install --host hermes --instance-root <天枢> --target-plugin-dir F:\Agents\shared\scope-recall\tianshu-installed-wrapper --project-root F:\t\SR-310-INTEGRATION-20260915 --python <天枢 venv python> --agent-id default`，无 conflicts 后 `apply-install`；
  6. `write_planned_stop_marker(<gateway pid>)`，等监督任务 15 秒内拉起新 gateway；
  7. `autostart enable`（同主体/同 env-file）；
  8. `doctor`：`package_version`=rc27、receipt=rc27、wrapper plugin.yaml=rc27、无 stale process；
  9. 只读 live 冒烟（1600/4096）。
- 验收：doctor 三方版本一致；gateway `running`、telegram/a2a `connected`；worker 一轮后 `idle`。
- 回滚：`predeploy-backup` 覆盖回去 + planned-stop；receipt 由 `apply-install` 自己备份在 `<instance>\.scope-recall-install-backups\`。

### 0.6 退役独立仓库、切换工作区
- 操作：在两个独立仓库根写 `RETIRED.md`（指向规范仓库、说明其历史已回放为哪些提交）；不删除目录（删除由用户确认后在 1.3 执行）。Cursor 工作区切到规范仓库的 worktree（2026-09-16 已执行：`git worktree move` 把 `F:\t\SR-310-INTEGRATION-20260915` 迁为 `F:\SCOPERECALL更新项目\worktrees\release-3.1.0`，同时清掉 `build/`、`dist/`、egg-info 与 `__pycache__`；`F:\t` 下不再有活动 worktree）。
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

本轮 D2 接手候选仅执行只读清单 `D2-deletion-boundary.json` 的 `delete_recommended`（3 个 shim、182 个未入 tier 的 legacy 测试、15 个 probe）；有新保留方引用则留存并记录。先备份本次编辑/删除文件，使用 `relative_path` 限定 detached runtime 工作树，未触碰 `verification/`、`_internal/` 或 D6。验收缩为 D1/D4 整合后的实际 `AllowlistedBuildPy` shipping path+bytes 前后恒等、tier 引用不丢失；不构建 untagged wheel、不跑全 gate。确切删除与闭包回执归入本任务 `integration/` 证据目录。

### 1.3 清理散落（决策 D6，用户确认后执行）
- 操作：生成清单（路径、类型：linked worktree / 独立仓库 / 无 git 的快照、最后修改时间、是否有未提交改动、大小），用户勾选后删除；`git worktree prune`；`F:\SCOPERECALL更新项目\worktrees` 只保留有活跃分支的。
- 验收：`git worktree list` ≤ 3 条；`F:\t` 只剩本周仍在用的目录。

### 1.4 依赖版本决策（D1）
- 操作：scratch venv 装生产版本（lancedb 0.37.1、mcp 2.0.0）跑 `native`+`host` tier。通过则 pyproject 放宽并记录；不通过则把天枢/天玑 venv 钉回 pyproject 版本（`pip install "lancedb>=0.30.2,<0.31" "mcp==2.1.0"`，需 planned-stop）。
- 验收：doctor `dependency_drift` 为空。

2026-09-16 D1 接手候选（未部署）：按本轮限定只跑决定兼容性的 native / MCP 最小检查，不扩成完整 tier 或全项目矩阵。基线 `b74c02a` / rc28；独立 Windows CPython 3.11.15 venv 实装 `lancedb==0.37.1`、`mcp==2.0.0`、`pyarrow==24.0.0`、`pydantic==2.13.4`，依赖一致性检查通过。

- 实测 native：真实 Lance 写入、SQLite 真相回填、跨项目/嵌入空间预过滤、全局分区权限隔离、读者跟随新提交、物理删除确认，5 个所选检查通过。MCP：真实 stdio initialize / 工具发现 / 调用、宿主线程绑定修改与删除、默认 4096 预算和非法类型拒绝，3 个所选检查通过。
- 首轮命令 exit 1（5 passed / 3 failed）：MCP 子进程找不到 `scope_recall`，原因是 flat-layout 源码仅在 pytest 父进程注册 alias，隔离 venv 尚无包入口。仅在任务 venv 增加指向本候选的源码 symlink，核对解析路径后重跑这 3 个 MCP 检查，exit 0；没有修改产品逻辑或测试断言。
- 决定：不建议回退，候选声明放宽到 `lancedb>=0.30.2,<=0.37.1`、`mcp>=2.0.0,<=2.1.0`，其余依赖不变。区间保留原合同并容纳本次验证端点；未逐版验证区间内所有版本，不能外推后续版本、Python 3.12 或其他平台的兼容性。
- 边界：合成 TEST 数据、真实本地库与 MCP 协议，无模型/API 调用；源码候选检查不是 clean-wheel 安装或 live 服务验收。本轮不修改 live 依赖、不发版、不重启；`doctor dependency_drift` 的 live 验收仍未执行。
- 原始证据：`F:\Agents\runtime\windows\hermes-yuheng\workspace\tmp\SR-CURSOR-HANDOFF-20260915\deps\` 下 `environment*.json`、`requirements-frozen.txt`、`focused-checks.{log,json,xml}`、`mcp-checks.{log,json,xml}`、`pip-check.log`；精确命令保存在 JSON receipt 的 `command` 字段。

### 1.5 doctor 与 scheduler 共享 `settled_waiting_sweep` 谓词（D-05）
- 操作：把 `schedule_settled` 的过滤条件抽成 `core/candidate_sweeps.py` 一个查询，doctor 调同一函数。
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
| D-12 | 天玑停留在 rc10 | 已修：2026-09-15 升到 rc27 |
| D-13 | 天玑 4 条采集卡在 `VERSION_CONFLICT`，从不重放 | 阶段 1（新） |
| D-14 | 原地 pip 升级在 gateway 运行时可能半途失败（目录句柄） | 阶段 1：部署脚本化（新） |
| D-15 | Codex 宿主安装停留在 rc5，且靠插件目录里的热补启动器给 MCP/hook 注入凭据 | 已修：rc28 `--env-file`，2026-09-16 升到 rc28 |
| D-16 | Codex 实例 24 条 `embed` 失败 `http_400`（非终态，从不再试也不落终态） | 阶段 1（新，随 D5 一起看） |
| 已修 | recall 有候选返回 0 条；lance helper 超时中毒；五个热补文件未入库；两个回归测试不在 tier；8 个可重试失败 | rc26 |
| 已修 | D-08 天枢/天玑三方版本不一致；D-09 autostart 缺参；D-10 shim 遮蔽；Codex 清单未入库；manifest 工具 CRLF；证据摘要依赖检出换行 | rc27 |
| 已修 | D-15 Codex 包装层凭据缺口（安装器 `--env-file`、入口读同一契约） | rc28 |

## 7. 需要用户拍板的决策

- D1 依赖：放宽 pyproject 到生产版本，还是把生产钉回 pyproject？（1.4 的门禁结果出来后决定）
- D2 树瘦身：`attic/` 还是直接删除？（推荐删除；历史在 git）
- D3 已批准同权限常驻 helper 持有嵌入凭据。候选实现仅 query embedding 复用私有管道子进程及 HTTPS 连接；无监听端口、无凭据日志。截止时间包含等待与管道 I/O；超时、取消或协议错误回收 helper，不自动重放失败 POST；下一请求可重建，runtime close 释放。source embedding / consolidation 保持一次性 transport。未部署，未验证生产 TLS/代理延迟。
- D4 已批准每项 `derivation_invalid` 自动额外一次，然后待人工。候选复用现有 work/evaluation/lifecycle 事务和错误字段的 `derivation_retry:1` 标记，无 schema 迁移；旧 failed 条目也仅补一次。仍失败保留 `failed`、原内容及错误记录，doctor 显示 `needs_review_work` / `work_needs_review`，总失败数不扣除、不会报 `ok`；不再把提炼失败改写成 `done/source_only`。显式 `retry-failures --include-terminal` 仍为人工操作，不重置自动额度。已成为 `done/source_only` 的历史数据不自动回写；生产数据未处理。第二次 consolidate / evaluate_candidate 沿现有 port、runtime wrapper 与 formatter 接口附带安全的 `validation_error={code,field}` 修复提示；只取现有 `work_error_details` 中校验符号，历史无明细时用 `DERIVATION_INVALID/payload`，不带失败正文或凭据。
- D5 外部整合：用户已拍板「Codex 批准、天玑等 Grok」。Grok 已用完；下一步按建议给 Codex 开整合（预算上限），天玑积压以 Grok 治理结果为准，未处理完的再标不适用。
- D6 清理清单：用户已拍板「出清单再勾选」。清单生成被中断，待续。
- D7 Codex 宿主安装：已升级到 rc28。遗留：下次打开 Codex 时重新信任 hooks。
- D8 三台同版：用户 2026-09-16 拍板立刻对齐。核验已同版 rc28，无需再部署。

## 8. 禁止事项

- 不在 site-packages 里改文件；不在没有 tag 的提交上构建 wheel；不 `git init` 新仓库；不把 `F:\t` 下的目录当长期工作区。
- 不在 integration 门禁红的情况下部署；不用 1200 之类非生产预算做验收（生产 `budget_tokens`=4096）。
- 多跳查询不推断身份、不放宽 audience；后台任务不无限重试（AGENTS.md）。
