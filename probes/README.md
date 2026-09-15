# P02 隔离宿主探针

这些文件只用于工作树 .execution/TEST-P02-*，不是正式适配器。当前证据和缺口见 docs/compatibility.md；P02 尚未通过，不可启动 P03。

## Codex

项目 .execution/TEST-P02-codex-desktop 有独立 Git 根及 .codex/hooks.json。六个精确定义已通过官方 CLI /hooks 审阅信任，没有绕过信任。实际 CLI 使用 Luna，UserPromptSubmit 和 Stop 均成功回调；结果不代表桌面输入。

桌面已打开隔离任务 **Return interface probe marker**。通过 app 工具发送的消息没有 UserPromptSubmit，手动 UI 测试仍待完成。待输入内容：

`TEST_SCOPE_RECALL 只报告本轮额外上下文中的接口探针标记；没有则回答 NO_MARKER，不要调用工具或读取文件。`

完整标记没有写入用户提示。诊断库保存字段类型、长度、摘要、事件次序及标记是否出现，不读取 transcript_path。超过 64 KiB 的回调明确输出 P02_PROBE_INPUT_TOO_LARGE，不静默当作已捕获。

## Hermes

真实运行用天璇当前源码与官方 hermes_cli.main gateway run，独立 HERMES_HOME、loopback A2A 19921、TEST 插件、合成输入及随机本地令牌。五个正式实例未作为启动目录，也未修改其模型配置。

run_gateway_probe.py 只启动 TEST gateway 和预算代理；--fresh-registration-check 另建独立空 TEST home，但共享原有调用账本。exercise_a2a.py --scenario ordinary|attachments|cancel 是显式场景入口，不默认调用。使用过的合成请求和返回保存在 verification/P02。

用户已授权 OpenCode Go 路线和必要的北斗取钥。budget_proxy.py 只允许指定模型/端点、TEST 输入和 1024 输出上限，并在请求前持久预留次数与累计字节。已用 12/12 次、231147/786432 请求字节，不可重置账本或把重试视为免费额度。所有 TEST gateway 已停止；原代理不得增加或重置这 12 次额度。用户随后批准的新 $20 折算预算有独立账本，见下文。

pre_llm_call 是唯一注入点；Provider prefetch 返回空。重复 Provider 激活导致 Hook 叠加已修复，官方注册接口和真实 gateway 都确认回调保持一次。附件 raw 丢失、A2A cancel 不停止底层执行、模型拒绝缺少完成回调均为实测边界。

## 无模型验证

```text
python -I -B probes/check_preflight.py --hermes-source F:\Agents\runtime\windows\hermes-tianxuan\hermes-agent
python -B -m pytest -q -o addopts= --noconftest tests/host/test_p02_budget.py
```

预检和预算测试分别为 16 和 12 项；它们不启动模型对话。native_startup 与 prepare_and_measure 只测各自进程成本，不替代前台全链延迟或语义质量。

`python -I -B probes/hermes/run_svg_probe.py` 使用独立 TEST home、官方 A2A 和公开 Provider 工具，模型端为本机 SSE fixture；不读凭据，不接上游，也不动已耗尽的 P02 账本。依次输入同名 SVG 两版及评价，再在两个新会话打开先前返回的引用。最新实跑十项断言通过，旧版未被新版覆盖。失败运行亦保留；这不是语义质量或完整 B10 的成绩。

## 增量模型预算与路由诊断

`cheap_model_probe.py`、`mimo_compat_probe.py`、`embedding_route_probe.py` 只有带显式 --run-authorized-model-probe 才发起调用，并读取已批准预算。它们共用 `.execution/TEST-MODEL-BUDGET-V1/call-budget.sqlite3`；所有已运行的小批次均已耗尽自身次数，禁止删除账本后复跑。新增必要测试须明确新批次范围、保留旧请求并继续扣同一总帽。

DeepSeek 两项通过；MiMo 初次 0/2、官方参数诊断 3/3；embedding 直连失败后，经现有本机代理取得 3072 维向量。首次错误、未知 usage 预留和复测原样保留。MiMo 使用 max_completion_tokens 与 auto 工具选择；当前请求发送上限和未知 usage 的保守预留是两个不同数值。19 项本地预算回归通过，未执行完整语义验收。

只在进程内读取必要凭据，不保存隐藏推理；实际宿主仍需完成 Codex 桌面入口验证。

Go 两条探针现在共用 `cheap_model_probe.check_go_allowance`：每次新尝试先读取官方 usage，三个窗口均为 ok 且数值低于 80% 才进入项目预算预留。没有缓存、未知或失败即停止。局部回归最终 31 项通过，没有重跑模型；首轮因超长 pytest 用例名称触发 Windows 环境变量长度限制的原始 XML 保留，改用短用例 ID 后通过，测试输入和断言未变。
