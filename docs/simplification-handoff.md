# 3.1 简化系列：交接计划（2026-09-16）

接手的 agent 先读这一份。目标是用户的原话：**LOC 大幅下降（至少 30%）、拆掉 god file、统一可复用的 helper、减少 if-if-else 路由、提高可读性与可解释性、删掉冗余代码，全部做完，最后以一个或一组 PR 交付。** 行为不能变：门禁测试就是契约。

## 0. 现状（每完成一步就更新本节）

| 项 | 值 |
|---|---|
| 工作目录 | `F:\SCOPERECALL更新项目\worktrees\yuheng-closeout-integration-20260916`（`src` 符号链接指向它） |
| 分支 | `simplify/3.1-prune`（PR 1，已推送，完成）→ `simplify/3.1-structure`（PR 2，基于 PR 1，进行中） |
| 起点 | `b74c02a`（`release/3.1.0` 当时的 HEAD） |
| 门禁 | `F:/t/SR-TIANSHU-RECALL-FIX-20260915/.venv/Scripts/python.exe -X utf8 scripts/check.py --tier <unit|contract|host|migration|native|packaging|integration>`，在工作目录根运行；packaging 前先 `rm -rf build hermes_scope_recall.egg-info`；uv 在 `F:/Agents/runtime/windows/hermes-tianji/bin/uv.exe`；ruff 用 `uv tool run ruff`。 |
| 基线（b74c02a） | unit 29 / contract 96 / host 122 / migration 7 / native 48 / packaging 125 / integration 1721；PR 1 之后 integration 是 1644（少的 77 个正是删掉的 9 个只测 2.x 引擎的旧测试）。 |
| 本机没有 `gh` | PR 用推送后的 compare 链接开：`https://github.com/410979729/scope-recall-hermes/compare/release/3.1.0...simplify/3.1-prune` 与 `https://github.com/410979729/scope-recall-hermes/compare/simplify/3.1-prune...simplify/3.1-structure`。 |

### 已完成并合入 `simplify/3.1-structure`

1. `d96a159` **prune**（PR 1）：删除退役 2.x 引擎及只有它引用的一切，1,023 个文件、−412,789 行；树从 480k 行降到 82k 行。
2. `b400596` **structure**：根目录 17 个模块搬进 `core/` 与新包 `vector/`，删除 `_internal/`。
3. 核心簇（本人）：`core/claims.py::qualify` 改成有序规则流水线；`contracts.py::validate_payload` 表驱动；`core/episode_storage.py`、`core/mutate.py`、`core/storage.py`、`core/truth_connection.py` 大函数拆成命名步骤；删除 1,600 行无人引用代码；`tests/sitecustomize.py` 修正门禁子进程加载旧 rc11 可编辑安装的漏洞；`scripts/check.py::main` 拆分。
4. 合入的代理分支：宿主工具面（`adapters/tool_common.py`）、doctor 命名检查、worker 分发表 + `core/worker_projection.py`、legacy 转换流水线（`maintenance/legacy_plan/sources/claims/deletions.py`）、候选生命周期（`core/candidate_tables/intake/evaluations/sweeps.py`）、安装器拆分（`maintenance/install_common/receipt/codex/hermes/purge.py`）+ CLI 表路由。

### 尚未合入（代理 worktree，位于 `F:\SCOPERECALL更新项目\.repos\scope-recall\.claude\worktrees\`）

| 代理 | worktree 目录 | 分支 | 任务 | 状态 |
|---|---|---|---|---|
| vector | `agent-af5332ff1aedd8592` | `worktree-agent-af5332ff1aedd8592` | `vector/*.py`、`adapters/lance.py`、`_lance_worker.py`：三个 store 共用一个接口/协议、去掉逐方法包装重复、Lance 表 helper 移出 `store.py`、`process_store` 的 `_invoke_locked/_invoke_fenced_locked` 合一、`LanceEmbedPort` 的 source/claim 双胞胎合一；保留 `tests/contract/test_vector_failure.py` 解析的 RuntimeError 文案 | 进行中 |
| recall | `agent-ad3c6b18b31c0c8f1` | `worktree-agent-ad3c6b18b31c0c8f1` | `core/recall_packet.py`（`compile` 291 行拆阶段）、`core/recall.py`（`search` 拆步骤）、`core/retrieval_storage.py::hydrate`、`core/read_views.py` profile/entity 合并、统一 `_effective_limits`/token 估算；删除无引用的 `RetrievalPort` | 进行中（曾整文件删掉待重写，务必检查文件是否齐全） |
| runtime | `agent-a37a64389e4852215` | `worktree-agent-a37a64389e4852215` | `runtime/*.py`、`adapters/models.py`、`adapters/codex_cli.py`、三个 `runtime_wiring.py`：统一 `_strict_*` 校验 helper、config 类改字段表、`HttpsTransport._post`/`propose`/`codex_cli._run` 拆步骤、`attach_trusted_host_runtime` 三份合一 | 进行中 |

## 1. 接手步骤（按顺序）

### 1.1 收尾未完成的代理分支
对上表每个 worktree：
```
cd <worktree>
git status --short          # 未提交的就是断掉时的半成品
git log --oneline b400596..HEAD
```
- 若有未提交改动：先让树可导入（`PYTHONPATH=. python -c "import scope_recall.<模块>"` 逐个检查），再按上表的任务描述补完；每完成一个文件就 `git commit`（作者 `Codex`，末尾加 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`）。
- 若新增/改名了发布模块：`python scripts/build.package_manifest.py --write`，把 `packaging/v11-module-allowlist.json` 一起提交。
- 跑该代理相关的 tier（vector：native+integration；recall：retrieval+integration；runtime：integration+host+native），再跑 unit、contract、packaging。
- 只改代理自己那组文件，不改测试列表、不加新测试文件、不动 CHANGELOG/README/check.py。

### 1.2 合入主分支
在工作目录：
```
git merge --no-ff <该分支 HEAD sha> -m "merge: <一句话>"
python scripts/build.package_manifest.py --check     # "ok": true
```
冲突基本只会在 `packaging/v11-module-allowlist.json`：直接 `--write` 重新生成即可。合完跑七层门禁，全绿后 `git push origin simplify/3.1-structure`。

### 1.3 合并后的统一清理（本人尚未做）
1. **跨模块死代码扫描到不动点**：`python scripts/dead_code.py <模块...>` 报告，`--apply` 删除；对整个发布集跑：`python scripts/dead_code.py --apply $(python -c "import json;print(' '.join(json.load(open('packaging/v11-module-allowlist.json'))['python_modules']))")`，重复到报告为 0。规则：从其他发布模块/测试/配置文件被引用的顶层名字是根，模块内可达的活，其余删；`__all__` 同步裁剪；然后 `uv tool run ruff check . --select F --fix`。
2. **重复 helper 统一**：`_now/utc_now`（runtime/running_code、vector_upkeep、worker_entry、codex handler、core/capture/composition）、`_json`（core/*storage、migration_records）、`_absolute`（codex/mcp_entry 与 install_common）、`_digest`（fact_identity、legacy_tianshu_compat、migration_records、upgrade）、`_bounded_text`（attachments、fact_actions 两份行为不同，需判断）。
3. **剩余 if 阶梯**：`adapters/codex/handler.py::handle_payload`（代理判断表驱动会掩盖四种事件尾部差异，保留即可）、`core/delete_storage.py::physical_members/purge_sqlite`（5 层 elif → dict）、`core/episodes.py::state_from_sources`、`runtime/scheduling.py::next_wake`（若 runtime 代理未做）。
4. **测试树里的旧路径**：`grep -rn "scope_recall\.\(writer_lease\|vector_store\|lance_process_store\|_internal\)" tests` 应为空。

### 1.4 文档
- `CONTRIBUTING.md` 的布局表加 `vector/` 与新模块家族；`docs/recovery-plan-2026-09-15.md:168` 提到的 settle 过滤现在在 `core/candidate_sweeps.py::_settling_rows`；`packaging_hooks/module_inventory.py` 文档串里的 `maintenance/install.py::_manifest_version` 现在在 `install_common.py`。
- `CHANGELOG.md` 的 `3.1.0rc29` 段落补一条 PR 2 的结构说明（prune 那条已写）。
- `README.md` 加一小节"树的布局"（core / vector / adapters / runtime / maintenance 各一句）。

### 1.5 交付
1. 最终树跑七层门禁，全绿；`python scripts/build.package_manifest.py --check` ok；记录行数：`git ls-files '*.py' | grep -v ^verification | xargs wc -l | tail -1`（PR 1 后为 81,667；目标：发布代码再降 10–20%）。
2. `git push origin simplify/3.1-prune simplify/3.1-structure`。
3. 开 PR：PR 1 base `release/3.1.0`；PR 2 base `simplify/3.1-prune`（叠在 PR 1 之上，diff 只含结构改动）。`main` 是 9/5 的旧祖先，勿以它为 base。PR 正文写：删除了什么、搬了什么、每个 god file 拆成什么、门禁计数、以及"行为差异"一节（各代理报告里点名的几处边角：doctor `to_dict` 字段顺序、worker 已删 claim 的 embed 现在报 `obsolete/authority_revoked`、候选 `defer_budget` 加了 blocked 栅栏、安装器 manifest 校验先报身份错误）。
4. 清理代理 worktree：合入后 `git worktree remove <路径>`（在 `.repos/scope-recall` 下执行 `git worktree list` 查看）。

### 1.6 死代码扫描脚本说明
已入库为 `scripts/dead_code.py`（本节说明其规则）：对每个发布模块，用 `ast` 收集顶层 def/class/Assign 名字；根 = 在其他文件（allowlist 模块 + `tests/**` + `probes/**` + json/yaml/toml/cmd）里以单词边界出现的名字，加 `main` 与 dunder；在模块内沿 `ast.Name`/`ast.Attribute` 引用做闭包；不可达的按行号区间删除，`__all__` 剔除同名项；循环到不动点。每次删完跑 unit+contract 与相关 tier。

## 2. 不要做的事
- 不在 `release/3.1.0` 或 `main` 上直接开发；不 `git init`、不复制目录。
- 不为旧形状留兼容 shim；不新增框架/注册表，dict 即可。
- 不删 `verification/` 里剩余的证据文件（`functional_traceability.json`、`G0/*`、`P07/*` 被测试与收据引用）。
- 不改 `tests/` 里的断言来迁就重构；重构后测试红了先怀疑重构。
