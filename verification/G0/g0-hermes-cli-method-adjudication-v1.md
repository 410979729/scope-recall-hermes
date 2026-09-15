# G0 Hermes CLI 入口窄修订裁决 v1

**裁决：批准方法修订；真实运行尚未验收。** 审计者 `/root/g0_hermes_entry_adjudication` 与旅程及传输实施者分离。本裁决不代表整个 G0、P11、G2 或 P18 通过，也不批准生产部署或增加预算。

原始启动指令允许隔离 TEST 宿主验证，由独立 G0 裁决接口冲突。P11 要求 Hermes 实际入口、原始来源、新会话与四层证据，没有只允许 A2A。P18 v1 则明确选定 `hermes_a2a`。目前 A2A 在候选的 `adapters/hermes/identity.py:116–125` 保留 `origin_unknown`，`core/worker.py:89,143–148` 将其排除出事实根来源；这是正确的来源保守拒绝，不能为生成 L2 改作人类。尚未正式评测时，用实际本地 CLI 覆盖普通用户输入是有效的入口修订，而非降低来源标准。

仅将 Hermes TEST 方法显式改为 **`hermes_cli_local_input_v1`**，四臂 A/B/C/D 同时使用，保留原协议不覆盖。Codex 的单独 app-server v2 裁决不变。原始数据、原生记忆/旧版/新版/简单搜索定义、主模型与路由、权限和工具、历史与上下文预算、120 核心问题、每宿主 40 查询和 8 旅程、所有阈值、$20 共用账本及原有失败收据均不变。不得拿 CLI 新版对比 A2A 基线，也不得把 CLI 的成绩写成 A2A 通过。

冻结源码支持的入口是 `HermesPython -m hermes_cli.main --cli chat -Q --query-file <UTF-8文件> --model deepseek-v4-flash --provider <冻结的OpenCode Go别名>`。通过无 shell 的参数数组启动隐藏的独占子进程。原始用户文本原样提交；不能加入 origin、gold、期望答案或控制字段。插件安装清单需绑定实际 `cli/local` 与宿主 `agent_workspace=hermes` 的精确 TEST audience，不能把 A2A 元数据伪装为 CLI。自动化提交合成用户回合不代表可以把其中的引用、导入、模型或工具内容提升为人类确认。

新会话不传 `--resume/--continue`，保留同臂独立 HOME 中的正常记忆，并证明实际新 ID 和无旧对话上下文。需要同会话延续时只传 `--resume <上次实际ID>`，保留压缩产生的 lineage；不自行拼接历史。普通 `chat -Q` 使用原生 initialize、prefetch、sync_turn 和 shutdown；四臂冻结同一结束与有界收尾策略，不能额外执行反思、直接写 Claim 或增加模型回合。所有收尾辅助调用都计入现有预算。

**预算捕获有明确限制：** `--usage-file` 只用于顶层 `-z`，对 `chat -Q` 无效；`-z` 又不转交 resume，因此不批准用它拼接本方法。实际回答从 stdout 取得，会话 ID 和错误从 stderr 取得，完整保留退出码与超时。官方 `hermes sessions export <文件> --format jsonl --session-id <精确ID>` 导出会话含 token 和 `api_call_count`；按每个实际会话/压缩分段的前后差值核对，不能重复加累计值。导出只用于事后核账，不能代替网络前的硬预算预留；每次主请求、工具续调、宿主重试和 shutdown 辅助调用都必须由既有账本覆盖。未知或缺失用量保留原预留，不能计零；无法覆盖计费路径就阻止运行。

零模型证据在 `.execution/TEST-G0-HERMES-CLI-METHOD-v1/receipt.json`：用 `python -I -S` 执行冻结的标准库 argparse 模块与原样文本读取函数，验证新会话/精确 resume 参数、中文多行和引号保真；检查实际源码的普通对话、会话持久化、原生 provider 身份、自动 prefetch、晚加载 resume、收尾和官方会话导出。**这只是源码与解析器验证，不是完整 CLI、插件 Hook 或真实 L1–L4 运行证明。** 未读取凭据、活动实例、既有数据库、密封 raw/gold 或私有 J；未调用网络或模型，未修改产品或宿主源码。宿主快照没有 `.git`，上游 commit 是给定身份，审计所读文件的实际 SHA256 已留存。

在正式运行前，沿现有 P11/G2/P18 闭包完成：四臂相同方法及候选/宿主/配置冻结；有界计费及输出/会话/用量的无网络 preflight；授权的公开合成 CLI 完整事实 L2 与新会话行为证据，以及既有失败、隔离、附件与恢复义务。这里不新增 Gate，不重跑无关已通过检查，也不要求对已授权 TEST 工作再次征求用户许可。A2A 已有失败、来源限制、成本和未证明路径单独保留。

机器可执行的精确修订、保留契约和证据哈希见同名 JSON。
