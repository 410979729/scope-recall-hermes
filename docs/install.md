# 安装与使用 Scope Recall（3.0.0 本地候选）

> **当前状态：** 版本 `3.0.0` 为本地开发候选，**未发布到 PyPI**，**P18 正式宿主验收尚未通过**。本文只描述仓库内已实现的安装与诊断路径，不表示可公开发布或全部测试已通过。

包名 `hermes-scope-recall`（导入 `scope_recall`），维护 CLI 别名 `scope-recall` 与 `hermes-scope-recall` 相同。v3 **不提供**旧版 `update` / `upgrade` / `rollback` 等自动升级命令；从旧库迁数据见 [`upgrade-guide.zh-CN.md`](upgrade-guide.zh-CN.md)。

## 1. 安装 wheel

在源码目录构建 wheel，并装入**与宿主相同的隔离 Python 环境**（Python 3.11 或 3.12）：

```powershell
cd F:\你的路径\scope-recall-runtime-integration
py -m pip install build
py -m build --wheel
py -m pip install "F:\你的路径\scope-recall-runtime-integration\dist\hermes_scope_recall-3.0.0-py3-none-any.whl[lancedb]"
# Codex MCP 可选：在同一 wheel 后加 [codex]
```

路径含中文或空格时，PowerShell 请用双引号包住整个参数。`lancedb` extra 用于 LanceDB 向量伴生目录；`codex` extra 安装 MCP SDK，供 Codex MCP 适配器使用。

wheel 向 Hermes 0.21+ 声明正式 entry point：组 `hermes_agent.memory_providers`、名称 `scope-recall`、目标 `scope_recall.distribution.hermes:register`（现有绝对导入包装，`register(ctx)` 再调用 `register_adapter(ctx)`）。宿主发现走已安装包的 pip entry point，不依赖把包装目录放进 `$HERMES_HOME/plugins`，也不要用手工 copy/symlink 冒充发现。

安装后可用下列命令确认 CLI 可用（两条等价）：

```powershell
scope-recall --help
hermes-scope-recall --help
```

## 2. 三个状态（不要混为一谈）

| 状态 | CLI 负责 | 完成后标志 |
|------|----------|------------|
| **已安装** | `plan-install` → `apply-install` | 插件包装文件与安装回执已写入 |
| **宿主已启用** | 由 Hermes / Codex 正常配置完成 | 宿主实际加载插件并可使用记忆能力 |
| **钩子已信任**（仅 Codex） | 在 Codex 中批准插件原生钩子 | Codex 允许 `hooks/hooks.json` 中的命令执行 |

`apply-install` **不会**修改 Hermes 的 `config.yaml`，也不会替你在宿主里注册插件或信任钩子。安装回执（`<instance-root>\.scope-recall-install-receipt.json`）记录的是**安装时刻**的状态，其中 `host_registration_pending: true`（Codex 另含 `hook_trust_pending: true`）描述当时宿主尚未完成注册/信任；宿主后续启用或信任**不会改写**这份历史回执。当前是否已注册、钩子是否已信任，请以 `doctor` 输出和宿主实际行为为准。

安装模式是显式边界：`plan-install` 和 `apply-install` 默认以生产模式创建绑定（`test_mode=false`）。只有隔离的 TEST 根目录才应在**两个命令**上同时追加 `--test-mode`；`apply-install` 会把计划中的模式重新校验后再初始化实例，不会隐式改变部署模式。测试模式不是生产安装的默认值，也不会自动探测用户目录。

## 3. Hermes

路径须为**绝对路径**。`instance-root` 可以是**已有** Hermes 主目录（如 `C:\Users\你\.hermes`）：安装器只管理其中的 `scope-recall\` 命名空间与回执文件，不会把 `config.yaml`、`SOUL.md`、会话或其他插件当成 foreign。已有未知/未绑定/冲突的 `scope-recall\` 目录仍会拒绝，也不会收养陌生托管目录。

`target-plugin-dir` **不能**落在 `instance-root` 内部（根重叠守卫未放宽）。因此不要使用 `$HERMES_HOME\plugins\scope-recall` 作为包装目标；把包装写到 HOME 外的目录。发现由已安装 wheel 的 `hermes_agent.memory_providers` entry point 完成。`project-root` 为当前工作区根目录。

`--agent-id` 必须等于宿主 `initialize` 发送的 `agent_identity`（`get_active_profile_name()`）。隔离 HOME 下该函数通常返回 `default`，**不是** `main`。默认 `--agent-workspace` 为 `hermes`，与 Hermes 0.21+ `_memory_provider_init_kwargs` 硬编码值一致；只有宿主实际发送其他值时才应显式覆盖。绑定不一致时安装仍会成功，但捕获会因 audience 无法映射而拒绝（不会放宽 workspace/`scope_mismatch` 守卫）。

```powershell
$Hermes  = "C:\Users\你\.hermes"
$Plugin  = "D:\scope-recall-wrapper\scope-recall"
$Project = "D:\我的项目\repo"
$Python  = "C:\Python312\python.exe"

scope-recall plan-install --host hermes `
  --target-plugin-dir $Plugin --instance-root $Hermes `
  --project-root $Project --agent-id default --python $Python

scope-recall apply-install --host hermes `
  --target-plugin-dir $Plugin --instance-root $Hermes `
  --project-root $Project --agent-id default --python $Python
```

显式选择工作区（plan 与 apply 必须一致）：

```powershell
scope-recall plan-install --host hermes `
  --target-plugin-dir $Plugin --instance-root $Hermes `
  --project-root $Project --agent-id default --agent-workspace hermes --python $Python
```

- `plan-install` 输出 JSON；有 `conflicts` 时**退出码为 1**，须先解决冲突再 `apply-install`。
- `apply-install` 成功时退出码为 0，并写出 `files_written`、`installation_id`、`backups`（覆盖前会把旧包装文件备份到 `<instance-root>\.scope-recall-backups\` 下）。
- 计划与回执含 `agent_workspace`。Codex **不接受** `--agent-workspace`。

安装完成后，在**该实例的** Hermes 配置中把记忆提供者设为 `scope-recall`（例如 `memory.provider: scope-recall`）。不要改生产/兄弟 HOME。然后运行诊断：

```powershell
scope-recall doctor --host hermes --instance-root $Hermes --python $Python
```

- `doctor` 仅接受 `--host`、`--instance-root`、`--python`；不接受安装时的 `target-plugin-dir` / `project-root` / `agent-id`。
- `status` 为 `ok` 时退出码 0；存在 `capability_gaps` 时为 `degraded`，退出码 1。
- `host_registration_status` 在只读诊断中通常仍为 `pending`；是否真正接入请以 Hermes 能否加载插件并调用记忆工具为准。

Core 数据目录默认为 `<instance-root>\scope-recall\`（含 `memory.sqlite3` 与可选 `vectors\` 伴生目录）。

## 4. Codex

参数含义相同；`--host codex`。`instance-root` 存放 `codex-installation.json` 与 `data\`；`target-plugin-dir` 为 Codex 插件目录；`project-root` 为工作区根。

```powershell
$Instance = "C:\Users\你\.codex\scope-recall"
$Plugin   = "C:\Users\你\.codex\plugins\scope-recall"
$Project  = "D:\我的项目\repo"
$Python   = "C:\Python312\python.exe"

scope-recall plan-install --host codex `
  --target-plugin-dir $Plugin --instance-root $Instance `
  --project-root $Project --agent-id main --python $Python `
  --env-file "$Instance\embedding.env"

scope-recall apply-install --host codex `
  --target-plugin-dir $Plugin --instance-root $Instance `
  --project-root $Project --agent-id main --python $Python `
  --env-file "$Instance\embedding.env"
```

`--env-file`（仅 Codex）：Codex 用自己的环境变量拉起 MCP 服务与钩子进程，其中没有 `runtime-config.json` 声明的凭据名（如嵌入 API key）。给出该文件后，安装器把 `--env-file` 写进 `.mcp.json`、`hooks.json` 与 hook 启动器，入口进程只读取配置声明的那几个名字（与 worker 的 `autostart --env-file` 同一契约，不解释 dotenv）。不给则 MCP 内的 recall 退化为纯词法。同一份文件通常也传给 `autostart enable --env-file`。Hermes 进程继承 gateway 环境，`--host hermes` 拒绝该参数。

`apply-install` 会在插件目录写入：

- `.codex-plugin\plugin.json`
- `hooks\hooks.json`（六个原生钩子事件，见下）与 `hooks\scope-recall-hook.cmd`（Windows 启动器）
- `.mcp.json`（MCP 服务定义；需安装 `[codex]` extra 才能实际启动 MCP）

这些文件由安装器独占：不要手工改写或在插件目录放自己的启动脚本，否则下次 `plan-install` 会把它们报成 `edited prior file` / `unrelated plugin file` 并拒绝。需要改行为就改安装器。

六个钩子事件（与 `maintenance/install.py` 中 `CODEX_HOOK_EVENTS` 一致）：`SessionStart`、`UserPromptSubmit`、`PostToolUse`、`Stop`、`Interrupt`、`SessionEnd`。每条钩子通过隔离 Python 调用 `scope_recall.adapters.codex.hook_entry`。

**接入步骤：**

1. 在 Codex 的宿主配置中启用已写入的 `hooks\hooks.json`；安装器只生成文件，不替宿主完成信任或批准。
2. 若使用 MCP 工具，确认 wheel 带 `[codex]` extra，并按 Codex 实际支持的 MCP 配置流程允许服务 `scope-recall`。
3. 运行诊断：

```powershell
scope-recall doctor --host codex --instance-root $Instance --python $Python
```

Codex 的 `hook_trust_status` 在只读诊断中通常仍为 `pending`；本项目不提供桌面 GUI 或独立信任命令，是否真正接入请以 Codex 实际是否执行 hooks 文件中的命令为准。

Core 数据目录为 `<instance-root>\data\`（含 `memory.sqlite3`）。

## 5. 向量与平台边界

- **LanceDB**：安装时带 `[lancedb]` extra；数据目录路径宜短（如 `C:\ScopeRecall\my-agent`）。LanceDB 会在该路径下追加表名与临时文件；过长可能触发 `native_vector_path_too_long`。
- **PostgreSQL / pgvector**：**不在** v3 发行物内；若配置 pgvector 会报错，请保留旧安装并参阅迁移指南。
- **runtime-config**：不会从当前目录自动生成；需要时在 Core 数据目录下自行提供 `runtime-config.json`。缺失时 Core 以基础能力运行，并在诊断中报告能力缺口。省略的自动召回时限默认为 `auto_recall_seconds=5.0`、钩子兜底 `hook_processing_seconds=6.0`（均为现有上限）；超时后词法降级，显式更短时限仍生效。

## 6. 从旧版迁移

自旧 Hermes Scope Recall（官方 578b SQLite 基线）迁数据为**离线、显式**操作，与日常 `apply-install` 分离：

1. 停止旧插件写入，备份旧 `memory.sqlite3`（及可选 `vectors\` 目录）。
2. 在新实例目录完成本节第 3 或第 4 步的空实例安装。
3. 按 [`upgrade-guide.zh-CN.md`](upgrade-guide.zh-CN.md) 执行 `python -m scope_recall.maintenance.migrate`。
4. 核对迁移报告与抽样记忆后，再切换宿主到新插件；**旧库与旧安装保留**，确认无误后再手动清理。

v3 不承诺一键自动升级，也不保留旧版长期兼容层。

## 7. 卸载（默认保留记忆）

卸载依据安装回执，**默认只移除插件包装文件，保留 Core 数据库**。先检查计划，无冲突后再应用：

```powershell
scope-recall plan-uninstall --instance-root $Hermes
scope-recall apply-uninstall --instance-root $Hermes
```

- `plan-uninstall` 有 `conflicts` 时退出码为 1。
- `--target-plugin-dir` 可省略（从回执读取）。
- 普通 `plan-uninstall` **不会**评估 purge，输出中 `purge_allowed` 恒为 `false`。

若要删除经回执校验的、安装器拥有的 Core 数据，须**单独**用 `--purge` 做显式检查与应用（两步都带 `--purge`）：

```powershell
scope-recall plan-uninstall --instance-root $Hermes --purge
# 仅当上一步 purge_allowed 为 true 且无 conflicts 时：
scope-recall apply-uninstall --instance-root $Hermes --purge
```

purge 会校验安装身份、数据目录归属、是否存在活跃写入方、`restore-required.json` 等；不满足则 `purge_refused:*` 并拒绝删除。**不要把 purge 当作常规卸载步骤**；日常卸载无需 `--purge`。

## 命名对照

| 概念 | 值 |
|------|-----|
| PyPI 包名 | `hermes-scope-recall` |
| Python 导入 | `scope_recall` |
| 宿主插件 ID | `scope-recall` |
| 安装回执 | `<instance-root>\.scope-recall-install-receipt.json` |
| Hermes 安装清单 | `<instance-root>\scope-recall\installation.json` |
| Codex 安装清单 | `<instance-root>\codex-installation.json` |
