# G0 Codex app-server 入口方法裁决 v2

裁决：**CONDITIONALLY_ACCEPTED_METHOD_REVISION，方法层面有条件认可；P12/G2/P18 均未通过，正式运行仍禁止。** 本文件仅冻结本地 TEST 的 Codex 测试入口修订，不修改 W 中 `p18-evaluation-protocol-v1.json`，不把既有后台回合改名为 actualDesktopUI，不改变产品目标、来源要求、质量门槛或预算。执行授权来自主线程转述的当前用户自主推进指令；本裁决不创设生产、发布、信任绕过或 sealed 数据访问授权。

## 裁决依据与修订范围

S 的 P12 目标是实际 Codex 入口可靠自动记录、隐式回忆及可诊断信任状态；P18 要求真实新会话、来源到最终行为和两宿主独立数据。`docs/05` 第 146 行要求各有一个用户实际使用且端到端验证的入口，第 163 行将接口与范围归 G0、真实使用归 G2；`START_HERE.md` 第 30、34 行允许在 G0 统一处理冲突并按实际启动指令推进。这允许记录一个明确的新宿主入口方法，但不允许用方法说明替代实际使用证据。

W 的 v1 已明确冻结桌面 UI，且禁止 CLI 替代。因此，**在 v1 原标签下，app-server 结果仍不能计作桌面 UI 通过**。本 v2 另立 `codex_windows_appserver_native_hooks_v2`，供用户已授权的本地实际 Codex 插件验证使用；后续 runner/支持矩阵必须显式引用本 v2 并采用新标签，不能默默读取 v1 的 UI 字段却走后台协议。方法变更后的结果只能声称“Windows Codex 官方 app-server 原生 Hook 入口”，不得声称已经验证鼠标输入、桌面发送控件、附件上传控件或所有 Codex 客户端。方法声明可落地到本用户真实使用入口；若只有一次性探针而没有可重复的会话及实际行为闭环，仍不满足 P12/G2。

公开 `codex app-server` 与 `codex exec` 可明确区分：前者运行有状态 JSON-RPC 协议，公开 `initialize`、`hooks/list`、`thread/start`/`thread/resume`、`turn/start` 以及原生 Hook 通知；后者是不同的非交互命令。使用同一个 exe 不使两者相同，也不使独立 app-server 子进程等于正在运行的 Desktop UI 会话。官方文档将 app-server 描述为 Codex 丰富客户端接口，并记录同步 Hook 通知；这些定义支持方法分类，不能单独证明插件效果。[Codex App Server](https://learn.chatgpt.com/docs/app-server)

官方 UserPromptSubmit 的输出可作为额外 developer context。客户端 `turn/start.additionalContext` 是另外的输入；不得由测试器复制 RecallPacket 填入它来制造原生 Hook 通过。公开文档同时指出 transcript 格式不稳定，故不以私有 transcript 解析补足必要能力。[Hooks](https://learn.chatgpt.com/docs/hooks)

v1 supplement 中“官方 appsend 等价”的假设没有获得证明；主线程报告其仅触发 Stop，没有 UserPromptSubmit。保留该失败，不将它与本次独立 app-server 回调合并。Hermes 方法与验收不由本次 Codex 裁决重新签署。

## 已有证据与精确边界

本审计直接读取 `.execution/TEST-CURSOR-APP-PROTOCOL-v1/callback-receipt.json`、`discovery.json`、公开生成 schema、候选 transport 与 fixture 检查代码；没有调用模型、操作 UI、读取 sealed raw/gold 或修改产品。

| 证据 | 已成立 | 不成立或尚未证明 |
| --- | --- | --- |
| exe SHA-256 `e5aa76d19c7c94e2e9ef9b707d590206a73ac0e97c8ddc8382181242494bef75`，schema 0.153.4，Windows，既有 CodexHome | 可定位独立 owned 子进程和实际 TEST cwd；已信任的 project hooks 被公开列出 | 与现有 Desktop 进程同一会话、所有桌面功能等价 |
| session `01a07883-202e-7c21-bdc7-b5daa14f0272`，turn `01a07883-2098-7c63-a6a7-0771115d438b` | 一次 Luna/low turn；SessionStart、UserPromptSubmit、Stop 各 started/completed，同一 session/turn | 新会话回忆、轮间恢复、各信任失败分支 |
| UserPromptSubmit 一份 context，937 字符，SHA-256 `c3b1f049e9d28cb80e5d1194de21f3d03bb96322e511ab2abd7e5a94b621434a` | 原生 Hook 输出已被公开通知观察到；探针没有传客户端 additionalContext | 模型输入边界恰好交付一次、内容真正被采用、语义正确 |
| completed 通知 durationMs=7398，同时外层 90 秒 timeout、退出码 1、cleanup=terminated | 宿主真实回合完成与驱动等待缺陷可分层识别 | 不能把整份原收据改成绿色 PASS；fixture 修复不追溯治愈真实首次运行 |
| 本次只读查询 TEST P12 SQLite 的该 session 两条 source_events | user/human_direct 60 字符与 prompt hash 一致；assistant/assistant_visible 3 字符与公开输出 hash 一致；均 complete，同一 TEST scope | 角色标签本身不是任意 RPC 调用的人类身份授权；没有从该合成文本证明真实自动提取、L2–L4 |
| live runtime-config 与 before-callback 备份 hash 均 `39e55d88849d26bbed96e31ddf4842c1217fdc7729e64d07573672a0fb80032f` | 审计时文件已按字节恢复 | 本次暂关 auxiliary 的回调探针不能证明完整提取链 |
| 历史 installed-v4 收据记录 source b8276187177871d67786a797b2a8308041901768 | 旧 wheel/安装快照可追踪；主线程报告该探针当时运行旧包 | 当前新 wheel 的完整行为通过；历史安装收据不单独证明每次调用的运行时包绑定 |

fixture 修复可验证等待/EOF/通知解析，现有候选 `tests/eval/p18_codex_appserver_transport.py` 仍 `formal_supported=False` 且正式模式硬拒，故只是候选实现。源码快照发生并行修改，审计记录 HEAD 和文件哈希，不把 HEAD 当最终候选内容冻结。

## 冻结的具体方法

1. Windows 上解析并核验实际官方 Codex exe 的版本与哈希，由测试器启动独立、隐藏、可清理的 owned `app-server` stdio 子进程。仅使用公开协议；禁止 `codex exec`、直接调用 Hook 模拟宿主、私有 UI/RPC、鼠标绕道及信任绕过。版本变化只复核受影响接口，不自动重开全部 Gate。
2. `initialize`/`initialized` 后用 `hooks/list` 检查所用 cwd 的实际配置与信任；用 `thread/start` 建立真正新会话，用 `thread/resume` 或同线程 turn 完成冻结旅程。没有既有 trust 时记录并阻断，不自动写授信。当前 user CodexHome 仅是诊断身份事实，不能解除正式四臂独立 TEST home/database 约束；账号认证可沿用获准路线，记忆状态必须隔离。
3. 使用固定 gpt-5.6-luna、low 和原 Codex 账号路线。模型、工具、权限、原生记忆开关、上下文及记忆预算按同宿主四臂一致规则冻结。A 按原协议启用宿主原生记忆并关闭 Scope Recall；B 固定 578b955802df753f2e2208e26eab6f71971285a0；C 使用最终候选；D 同档案简单搜索。候选 transport 当前要求 UserPromptSubmit hook 不能强套到 A：每臂应验证其本来应启用的能力，不能给 A 安装 Scope Recall 才让 admission 变绿。
4. 普通用户资料仅通过真实公开 turn 输入进入宿主，由原生 UserPromptSubmit 记录并只读 recall，再由 Hook 输出交付。禁止测试器预写 Claim/答案、复制 recall packet 到客户端 context、直接灌库或用另一宿主/臂记忆补齐。`human_direct` 只适用于本地用户授权的合成/原始输入来源，并记录调度来源；来自模型、工具、远端或转发的内容不得因 JSON role=user 自动升级。
5. 保留 session/turn/源事件/对象版本/packet/交付/最终公开输出的关联证据。从公开 item/turn 事件收集最终输出，不读取隐藏思维链。图片/SVG 按实际获准文件输入或该宿主可用的文件工具展开具体版本，保留内容摘要、版本、用户评价和访问结果；不得以贴入预写描述替代附件能力。会话生命周期、错误、超时与清理均留收据。
6. 所有真实主调用、辅助、失败、重跑、诊断和并行调用共用原持久账本；在可能触发网络的动作前完成额度预留，未知用量保留预留，不重置、不自动重跑。临时关 auxiliary 只能标入口诊断，不能计入真实提取或正式四臂结果。

## 正式启用前最小闭包（归原 Gate，不新增 Gate）

**P12/G2 真实闭环：** 用待验 wheel/安装绑定跑既有规格中的有界新会话/续作切片，形成“真实用户输入→持久捕获→真实自动提取→对象/来源→新会话无提醒 recall→原生交付→可见输出”链；覆盖已要求的纠正、隔离及获准附件版本。补齐同 turn 唯一交付的独立边界证据：关联公开 Hook 输出、适配器交付去重回执和宿主实际交付记录，排除客户端额外 context；单个 context 通知计数或最终答对都不足以独立证明模型输入只收一次。无需获取隐藏推理，无法观察时如实保留该项未验证。P12 原有信任状态、路径、绑定、多窗口与失败覆盖不删除；可重用相同版本的既有实证，不要求全部再跑一轮付费模型。

**P18 runner/冻结闭包：** 以本 v2 显式标记新入口，绑定最终源码/wheel/hash、每臂安装和独立 home/database、原生记忆能力及固定工具/输入预算、准确 auxiliary 分配、原账本和 sealed 哈希检查；独立执行者仅可读 raw、独立评分者按原程序读 gold。完成现有最低 runner 的无网络 preflight，证明模型/路径/trust/来源混入/账本缺失时 fail closed，以及输出、通知、错误和未知用量不被丢弃。现有 fixture 测试不等于完成这些集成条件；没有实证前不得只翻转 `formal_supported`。

这两项是 P12/G2 与现有 P18 前置的收口，不要求再造大型 harness、增加模型、增加留出样本、付费重跑所有确定性案例或恢复被用户停止的 CUA。已有明确执行授权持续有效；满足剩余技术前置后由主线程按授权推进，无需仅因 v1 中旧审批措辞再问一次。独立 G2/P18 裁决仍须依据真实结果，不能由此文件自签。

## 完整保留的验收与预算

120 独立核心留出、240 成对变体（不是独立 n=240）、12 组；每宿主 40 查询×2 条件和 8 旅程×8 轮；四臂、两宿主、1152 正式主提交上界不变。L3 必要证据覆盖≥90%、每组≥80%；L4 证据支持与适用性≥90%，每宿主查询至少36/40、旅程8/8完整；来源/当前历史/授权/删除/隔离等确定性断言全部正确，安全违反即阻断。完整分子分母、Wilson 区间、失败和所有重跑仍须报告。

总 $20、原共享 SQLite 账本、Go 8000 次/6400万输入/800万输出、Codex 1500 提交/4000万规划输入/200万规划输出、每主请求32768输入+4096输出预留、mimo auxiliary 输出131072预留均不变。原估算 $19.59 不是实际账单；诊断和辅助也占剩余额度。B 不可运行则记缺项，不编分数。保留原协议、旧样本、原 appsend/回调失败、来源边界和四臂公平；本裁决不签署全体 G0、G2 或 P18 PASS。

机器可读记录、文件哈希和审计时直接查询到的两条来源摘要见同名 JSON。
