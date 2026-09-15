# P02 实际接口与未解除的条件

P02 与 G0 已由独立审计在 `17943a0beccf46d5e6616cb6638e5dc3d5a6b552` 通过，实际桌面前置已关闭；P03 开始实现。原始裁决见 `verification/G0/g0-review-17943a0.json`。此前 Hermes SVG、预算、模型与评测冻结均沿用；下文明确的 partial 能力及全部后续产品验收仍然有效。开发基线为主线 `578b955802df753f2e2208e26eab6f71971285a0`，比最后发布的 v2.0.1 多 15 个提交。

## 实测入口

| 入口 | 实际身份 | 已观察 | 未证明或缺口 |
|---|---|---|---|
| Hermes A2A | 天璇当前源码 0.21.0，HEAD `79445a496c86a19332ad786494b8384d2167e2d0`；独立 TEST home / 127.0.0.1:19921 | 普通问题、恢复会话、成功和失败工具、自动前置注入、完成轮记录、新 context、附件、A2A 取消 | raw 图片丢失；A2A cancel 不停止底层 turn；模型拒绝轮缺少 post_llm/sync 回调 |
| Codex CLI | `0.153.4`；模型 `gpt-5.6-luna` | 官方 /hooks 信任六个定义；真实 UserPromptSubmit 注入和 Stop 捕获；输出未预置于提示中的标记 | 结果只证明 CLI |
| Codex Windows 桌面 | 包 `26.901.5280.0`；两个隔离 TEST 任务，Luna | 实际 UI 输入触发 UserPromptSubmit；当前轮唯一标记进入最终答复和 Stop；Bash/MCP 回调；同名 SVG 双版本捕获和新会话读回；Interrupt 及随后恢复 | Bash 结果为字符串，无结构化退出码；Interrupt 不保证停止已启动工具，缺少中断最终文本；附件回调只有路径；桌面正常退出时限仍 partial |

`host-readback.json` 是早期天权源码静态快照，不代表此次运行天璇的 A2A。当前关键源码指纹另见 `hermes-current-interface.json`。五个正式实例的配置、插件和数据库没有作为本次运行目录，也没有读取其会话或记忆行。

## 唯一注入与注册修复

Provider prefetch 在轮首调用，并非每次模型请求都调用；还会跳过 trivial/多模态查询，实际 callback 的 session 参数为空。探针用 Provider 初始化绑定的会话身份记录该事实，prefetch 返回空。唯一注入点为公开 pre_llm_call，其 session_id、turn_id、user_message、conversation_history 在真实 A2A 轮中可见。

首次多 context 实验发现探针反复注册 Hook：当前请求中的标记从 1 份增加到 5 份。原始失败保留在 `hermes-live-readback-before-registration-fix.json`，不能拿它证明唯一注入。

修复通过公开 iter_hook_callbacks 和探针自己的身份标记检查已有注册，保留每个 Agent 独立的 Provider。官方注册接口连续加载五次及第二模块命名空间均保持每类 Hook 一次；真实新 gateway 的成功轮和下一 context 也各只收到一次 pre_llm_call。最后获准模型请求中只有一份当前标记，见 `hermes-live-readback-after-registration-fix.json`。没有修改宿主核心。

模型对早期标记提问回答 NO_MARKER，尽管请求元数据证明标记已进入模型输入。此处只证明注入路径，不把回答当语义记忆通过。普通算术轮输出 5；工具实际结果分别为 exit_code 0 和 7，tool_call_id 与数据库结果可追溯。修复前重复 Hook 不代表执行了多次工具。

## 实测缺口

1. PNG raw 附件：A2A 接收同名、不同 SHA256 的两个 PNG raw parts 后，宿主 user 内容只有两个相同的文件名/长度描述。protocol.py:162–183 将 base64 转为长度，adapter.py:645–656 构造 TEXT MessageEvent，未传 media_urls/media_types。模型回答 UNAVAILABLE。此 PNG 路径仍不支持原图保留；独审确认原 07§13.1 的最低要求是图片或 SVG，允许继续验证已有完整 SVG text/data 路径，无需修改 A2A 或缩减 B10。
2. 取消：tasks/cancel 返回并保持 TASK_STATE_CANCELED，但已发起的模型请求、sleep 12 工具和最终答复仍在 TEST 会话完成。不能将 canceled Task 写成“工具未运行”或“执行中断已保存”。
3. 模型失败：本地预算耗尽后的新 context 收到 pre_llm_call，但没有 post_llm_call 或 provider_sync_turn；A2A 将错误提示包装为 COMPLETED。完整性不能由 A2A 状态推断成功完成。随后已接入现有公开 api_request_error：官方 AIAgent 经纯本地 HTTP 400 fixture 真实触发一次错误观察，包含 session/turn/request/status，外部模型调用为零；该失败路径同样没有 post_llm、on_session_end 或 sync_turn。失败可通过错误事件记录，仍不把它升级为成功完成。
4. Codex 桌面：程序化 app 消息未触发 UserPromptSubmit，这一旧观察仍保留。通过已安装 Computer Use 的实际输入框和发送按钮已触发回调。前三次 UI 测试返回 NO_MARKER，原因是 TEST 探针未识别桌面对下划线的 Markdown 转义；不是桌面缺少 Hook。修正 TEST 前缀识别后，本轮唯一 nonce 在当前提示中不存在、在 Stop 中出现。宿主原文未被改写；没有扫描 transcript_path。

## SVG 公开接口补证

`hermes-svg-interface-1788665256441150300.json` 记录实际官方 gateway 的四次 A2A message/send：同名 SVG 第一版经 text 传入、第二版经 JSON data 传入，公开 pre_llm_call 捕获完整内容与各自评价；随后两个全新 context 分别通过公开 MemoryProvider 工具打开两个不可变引用。六次模型协议请求全部发往本机假端点，实际 provider 工具和 post_tool_call 各执行两次，外部调用为零。

新会话首次请求没有原 SVG，真实工具返回之后才出现；两版源码、评价和 SHA256 均与实际输入一致。全部十项接口断言通过。fake model 只选择工具并传递实际工具输出，不能证明模型理解、视觉渲染或完整 B10。所有来源明确为 synthetic_agent_relay，不从 role=user 推定 human_direct。首次 pre_gateway_dispatch 早于外部 Provider 激活，第一版在 pre_llm_call 才可观察；后续原始入口和捕获正文哈希可对应。

前四次失败尝试原样保留：网络守卫误拦 Windows 自有 socketpair；两次在 gateway 启动恢复完成前过早发送；一次 fixture 未处理 SSE、memory 工具集及 data 分隔空行。修复只发生在 TEST 探针。当前启动器等待实际完成日志，支持真实 SSE 协议，限制访问自有回环端口。不是在宿主核心植入测试答案。

独审已解除 Hermes 必要 SVG 公开接口阻断，无需为此追加付费测试。P04 可使用 installation/session/原始 turn ID/事件种类，工具再含 tool_call_id，持久化后重试沿用原信封；这只保证同一已观察发生的身份。caller messageId 重提、宿主重新进入 run_conversation、崩溃后自动补捕及旧快照逐条身份均未证明，缺失时报 capture_gap，不用内容哈希或当前 turn ID 假造发生身份。

## 权限和实际用量

用户先批准 P02 上限，随后明确允许已有便宜路线并从北斗取必要凭据。选用 opencode-go / glm-5.3-flash，Kimi 没有调用。必要的 OpenCode key 通过已声明北斗绑定载入预算代理内存；未写入项目、收据、日志或 TEST 宿主配置，未修改保险库 ACL。宿主仅持随机本地代理令牌。

最终上游请求 12/12，合计请求 JSON 231147/786432 bytes，每次输出上限 1024 tokens；12 次均为 HTTP 200。一次 gateway 自动恢复也计入额度。第 13 次本地拒绝测试没有发往上游；持久账本跨测试 home 共用，没有重置额度。计数限制、并发预留、累计字节、失败和重启记账及单请求单 completion 的 12 项测试通过。实际账单未核实，不声称免费。

以上 12 次为原 P02 接口批次。用户随后批准全程 $20 API 用量折算上限，增量预算见 `verification/G0/evaluation-proposal.md`；预算、模型和评测已在 `ed302077` 独立冻结。所有测试 gateway 和预算代理已停止；退出码 1 来自 Windows terminate，不作为优雅关闭证据。

## 冷启动与其余边界

10 次轻量 command + JSON + SQLite 测量中位数 66.65 ms、最大 97.62 ms，不含真实宿主启动或模型请求。LanceDB 0.30.2 / PyArrow 24.0.0 新进程首次导入与搜索共 7.64 s，后五次 1.36–1.38 s；OS 缓存未受控，不外推生产 p95。

候选继续采用主线现有受控原生工作进程，前台 Hook 不逐轮加载原生库。完整生命周期、共享请求预算与故障恢复仍属 P06/P13；当前未通过。实际 AIAgent 的隔离初始化已同时加载原生 MEMORY 哨兵与外部 Provider，哨兵未变，路径均在 TEST；网络守卫拒绝全部 8 次连接尝试，没有模型调用。两宿主共存的完整模型行为仍未通过。

P02 预检现在 16 passed / 0 failed / 0 skipped，是合成输入和官方抽象类边界验证，这些预检不能代替真实桌面入口；后续独立 UI 证据见下文。L1–L4、B01–B12 语义验收及 G1–G3 尚未完成；G0 的后续独立通过见本文开头。

官方参考：[Codex Hooks](https://learn.chatgpt.com/docs/hooks)、[Hermes Memory Provider](https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin)、[OpenCode Go](https://opencode.ai/docs/go/)。接口判断以本机固定源码和实际回调优先。

追加错误观察使用公开 api_request_error；元数据不保存 request/error 正文。宿主 agent.api_max_retries 最小为 1，含义是一次尝试；启动器已显式使用 1，外发重试仍受同一持久上限控制。

## 已批准低成本路线的实测补充

增量预算使用独立持久账本，累计 9 次模型尝试：Go 7 次、Gemini embedding 2 次，合计已知用量与未知用量预留 $0.044603，仍在 $20 折算上限内。这不是实际账单；没有购买额度或修改付费设置。独立目标模式已建立。

DeepSeek V4 Flash 的 JSON 与工具选择各通过一次。MiMo V2.5 初次返回错误当前颜色，另一次工具请求超时；原结果完整保留。查官方 Chat 文档后，使用明确的 max_completion_tokens、thinking disabled 和 auto 工具选择完成三次公开诊断：同一纠正题、工具选择和 16-token 截断均通过。不能把重复公开题当成独立留出，也不能证明参数变化是颜色答对的原因。

MiMo 原超时未取得 usage，已把输出预留提高至文档最大 131072；校正前后另有审计记录，未知费用不返还。Gemini 首次直连长时间未返回，停止了已核对身份的两个 TEST Python 进程并保留预留；随后沿现有本机 HTTPS 代理取得 3072 维、有限且非零的向量。代理复测也没有返回 token usage，仍保留最大输入预留；未改变系统代理或生产配置。两次都是路由验证，不是检索质量验收。

官方开源 Go GET /zen/go/v1/usage 已核实并实际读到 rolling/weekly/monthly 均 ok；这些只读请求另记，不产生模型输出。此接口不返回余额补扣开关，浏览器未取得可用的账户页面。第一批四次调用早于账户余量读取，执行顺序遗漏保留。API 折算预算与实际账单边界不能混同。

## 实际桌面补证及保留边界

`verification/P02/codex-desktop-interface.json` 保留 34 个实际公开回调、九轮桌面输入、独立上下文的 CLI 初始化、全部失败和五份合成夹具。前三次标记失败不删除；其中两个中间未提交探针版本当时没有记录源码哈希，明确为未知。后续成功轮分别带执行时探针 SHA256。七项当前探针单元测试与十七项实际记录复核断言通过；复核是读取已保存回调，不是再次执行宿主。

普通工具轮首先因 PATH 不含 python/py/python3 而失败三次，随后宿主自行使用已有 uv 的离线运行方式执行 exit-0/7 夹具，两类回调和完整 stdout 均到达。Bash 的 tool_response 没有结构化 exit_code；此字段不可由回调存在与否推定。MCP 文件预览回调则返回结构化 content/isError。

实际 UI 文件选择器发送两份同名 diagram.svg。UserPromptSubmit 保留按版本区分的完整路径及“认可 v1 圆形结构、否定 v2 橙色”的原话。探针仅对预先获准的两个 TEST 夹具路径保存内容寻址副本，不宣称已经实现产品级附件解析器。原始字节 SHA256 分别为 64c3d4cfbe24bd096ac63a64cad4099b019101e0c0471085ed29e4b7f7c53eaa 与 33422e89191741035274c42f22f87ce5f6503532cbfc5ca9623738668e64cd0a。

另一个完全独立的 TEST 上下文只由官方 CLI 初始化为 TEST_CONTEXT_READY，不包含旧 SVG 历史；随后通过实际桌面 UI 发起回读。公开 Bash 工具从两个不可变引用读回原文及相同哈希，公开 open_in_codex 工具排队打开第一版，切回该 TEST 任务后实际显示蓝色圆形预览。初始化、桌面恢复、工具读取、预览显示分别归因，不能把 CLI 初始化当成桌面交互。当前轮独立 nonce 再次进入最终 Stop。

实际停止按钮触发 Interrupt，宿主 turn 状态 interrupted，未收到该轮 Stop 或等待工具的 PostToolUse；被启动的 TEST PowerShell 进程仍在 45 秒后写入 finished，随后进程退出。产品必须将这类经历记为中断且结果捕获不完整，不断言工具未执行。下一桌面轮正常收到新的 UserPromptSubmit/Stop 和本轮 nonce，没有重跑等待夹具。未为证明 SessionEnd 而关闭正在使用的主桌面应用，其正常退出时限仍 partial。

所有输入均为主线程经真实 UI 转交的 synthetic_agent_relay，不能标为 human_direct。两个宿主接口补证都不能替代后续自动提取、纠正、删除、来源资格、权限或最终 L1–L4 语义验收。本批没有 Go/嵌入调用；新增十个 Codex 模型轮包含九个实际 UI 轮与一个 CLI 初始化轮，早期另有两个模型轮。这批证据随后获独立 G0 通过，允许开始 P03。
