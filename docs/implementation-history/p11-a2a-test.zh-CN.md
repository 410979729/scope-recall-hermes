# P11/P18 Hermes 真实 A2A TEST 接线

本目录只准备隔离测试，不启动 gateway，不发送模型请求。v4 状态限定在
`.execution/TEST-P11-A2A-v4`；v3 失败证据和 v3 zero-API diagnostic 证据原样保留，
正式五实例和生产聊天库不读取、不修改。Frozen H 只读宿主来自
`<instance root>/TEST-Hermes-runtime-v1`，固定版本 `0.21.0`、HEAD
`79445a496c86a19332ad786494b8384d2167e2d0`。

## 准备与启动

在仓库根目录执行：

```powershell
$HermesPython = '<hermes runtime root>\venv\Scripts\python.exe'
& $HermesPython -B probes/hermes/p11_prepare_a2a_test.py
```

准备脚本只建立 TEST Hermes home、Scope Recall manifest/Core SQLite、
`runtime-config.json`、`config.yaml`、插件 wrapper 和脱敏 receipt；预算账本只读复用
冻结的 `<instance root>/call-budget.sqlite3`，
不在 TEST 家目录创建第二本账。
它会先检查 `127.0.0.1:19921` 与 `127.0.0.1:29991`；任一端口占用时只报告并退出，
不会结束他人进程。

复核准备 receipt 后，完整启动命令为：

```powershell
$HermesPython = '<hermes runtime root>\venv\Scripts\python.exe'
& $HermesPython -B probes/hermes/p11_start_a2a_test.py --duration-seconds 600
```

它启动两个仅属 TEST 的子进程：官方 Hermes `gateway run`（A2A `127.0.0.1:19921`）
和本地 DeepSeek Flash meter/transport bridge（`127.0.0.1:29991`）。启动器自身不做
模型 POST、不重试；实际网路请求只能在下面的 `--run` 显式入口发生。上游 key 只由
allowlisted TEST 环境变量在网络时、进程内读取，绝不写入 config、命令行、日志或 receipt。

主路由是 `deepseek-v4-flash`，完整上游入口为
`https://opencode.ai/zen/go/v1/chat/completions`。v4 初始接线将
`external_consolidation=false`、`external_embedding=false`，因此不触发辅助巩固；保留
`mimo-v2.5` 配置仅用于后续受控阶段，GLM 只是候选，不在本次接线中替换。
未来新建 TEST home 的 custom provider 模板已按 Frozen H 官方字段预置
`extra_body.thinking.type: disabled`；已完成的 v4 `config.yaml` 保持原样。

## 请求、预算与停止

默认请求是 dry-run，不访问 A2A：

```powershell
$HermesPython = '<hermes runtime root>\venv\Scripts\python.exe'
& $HermesPython -B probes/hermes/p11_request_a2a_test.py
```

真实普通 A2A 请求必须显式执行一次：

```powershell
$HermesPython = '<hermes runtime root>\venv\Scripts\python.exe'
& $HermesPython -B probes/hermes/p11_request_a2a_test.py --run
```

共享 Go ledger 沿用全局 8000 次、64M 输入、8M 输出及 $20 上限；账本上的唯一 TEST
触发器 `scope_recall_test_p11_batch_cap_v2` 对 `P11_A2A_V2` 的 main/aux 共用并原子拒绝第 9 条；
本批次另设最多 8 个
`P11_A2A_V2` POST。每次 POST 先 reserve 再加载 key/联网，单次 45 秒、零重试。输入
reserve 为 32768 tokens，主路由输出 reserve 为 4096，MiMo 辅助输出 reserve 为
131072。超过任一全局或本批次调用、token、字节、金额上限会拒绝请求。

停止只创建 TEST stop file，由启动器回收自己创建的两个子进程：

```powershell
$HermesPython = '<hermes runtime root>\venv\Scripts\python.exe'
& $HermesPython -B probes/hermes/p11_stop_a2a_test.py --wait
```

## L1-L4 真实证据与边界

成功的 `archive/a2a-*.json` 应同时给出：L1 agent card 与真实 A2A `message/send`；
L2 TEST Core SQLite 的 `source_events`/词法投影只读计数；L3 本地 bridge 捕获的
脱敏模型请求和 shared ledger；L4 实际 A2A 最终 task reply。L3 中的 recall 注入只能
以 bridge 实际收到的 prompt 作为证据，不能预写答案冒充 hook。

官方 Hermes 在隔离 `HERMES_HOME` 下读取 `HERMES_HOME/config.yaml`；当前官方源码没有
单独的 `--config` gateway 参数，启动器通过 HERMES_HOME 路径让它读取这份文件。

## v4 的 A2A audience 与新宿主 session

Frozen H 的 A2A adapter 在 `plugins/platforms/a2a/adapter.py::_prepare_task` 以请求的
`contextId` 作为 `MessageEvent.source.chat_id`，并使用 `chat_type="dm"`、peer 作为
`user_id`，不填 `thread_id`；因此 v4 manifest 明确使用 `thread_id: ""`，不是把缺失值
冒充 CLI 的 `main`，也不是通配 audience。后续同一 A2A chat 必须复用同一个 `contextId`。

要在不越域的情况下清空并新建宿主 session，官方入口是向同一个 `contextId` 发送
`/new`（别名 `/reset`），让 gateway 的 `_handle_reset_command` 调用
`async_session_store.reset_session(session_key)`；它保留由同一 source 生成的 session key、
只旋转宿主 session id。随后继续用原 `contextId` 发普通消息。不要改 session 数据库，
不要直接手工调用 `on_session_switch`，也不要换 `contextId` 来模拟新会话。

这次 basic 接线明确不执行 native Gem2 查询，因此不能据此声称 P18 的完整语义验收；
它验证的是官方 gateway → 当前 `newHermesMemoryProvider`/TEST wrapper → 同一 Core
capture/prefetch → 本地计量主路由 → 实际 A2A 返回的真实入口链。
