# P01 合同子项独立裁决与落实

独立审计者：`/root/contract_adjudicator`，未参与实现。裁决对象是提案 SHA256 `854d3b1101d565304ac99917a11c68b0556c59ff57815c9959c02cc03599a6d2`、HEAD `1ea1642e6253211cf0ccb9d02afa93a1e6f9d9c2`。两项结论均为 **accept_with_required_changes**；不是整体 G0 或 P01 实现通过。审计者读取原文和原 Schema，未读取当时的实现原型、未改文件、未运行测试。原包整体哈希本轮由 P00 核对，审计者没有重复核算。

主线程按下列条件实现合同边界；完整捕获、存储与回读仍由对应任务验证。实现完成后提交固定 SHA 给同一独立审计者复核，不把此处记录当作自审通过。

## D01 来源分段

依据为 07 §4.3、§12.2 和 02 §2.2。`segment` 为可选对象，禁止额外字段，出现时四字段均必填：group_key 为 1–512 字符；index 为从零开始的整数；total 为正整数或 null；truncated 为布尔。

冻结约束：

1. 分组身份为 `(installation_id, group_key, source_revision)`。installation_id 来自 TrustedContext；group_key 不能仅由正文哈希生成；不同发生不合并、重试不新建组。
2. 同组同修订 total 必须一致；同一 index 只能对应一个稳定 source_event_key。同事件键／修订或段身份的内容与捕获元数据冲突必须报 VERSION_CONFLICT，不能覆盖。
3. 未知总数转已知、分段边界改变、内容或快照元数据修正均产生新的整组修订；不得原地升级 null、partial、complete。新修订不是新的独立人类支持。
4. 已知 total 时 index < total；total 未知或 truncated=true 时事件不能 complete。已知总数且未截断不自动代表完整。
5. 单段 complete 只说明该段完整；整组完整还需同修订的 0..total-1 全覆盖且没有截断或缺口。乱序与暂时缺段允许，处理水位不能越过空洞。
6. content 的边界就是该段边界；重叠 evidence_refs 必须落到真实来源版本，并按共同证据根去重；不得把段或窗口算作独立确认。
7. 缺少 segment 表示未提供分段元数据。新捕获器仅对未分段有界事件省略；旧记录缺字段不证明原始来源完整。

P01 落实字段、类型和单事件约束；P04 落实组唯一约束、重放、覆盖和修订不可变；P10 落实处理水位；M42 验证真实长来源尾段。组状态规则已冻结，尚未由持久层测试证明。

## D02 展示快照

依据为 07 §6.2、§13。原 artifact_refs 是有序 JSON 数组；缺口是其顺序的可信展示语义和每项精确版本，不能误称数组物理无序。

`display_snapshot` 为可选对象，禁止额外字段，order 与 items 必填。order 为 observed/unknown；items 为 0–32 项数组，每项仅有必填 artifact_ref（1–240 字符）和 revision（正整数）。数值 revision 是本次冻结的合同选择，与产物版本对象共用，不另建展示版本体系，不接受 latest 或自由标签。

冻结约束：

1. observed 承诺指代出现时对应展示列表的完整可信快照，包含实际顺序和精确版本。字符串枚举本身不是可信证明。
2. 不得排序、去重或静默截断后标 observed。不同版本、同一版本的重复位置均保留，items 不设 uniqueItems。
3. 超过 32 项、位置缺失或版本不明时降为 unknown，最多保留 32 个已确认版本候选；遗漏实际捕获内容还需 capture_state=partial/gap。不能再根据数组位置绑定序号。
4. 缺少快照不是空展示；unknown 的数组位置无历史序号含义。其他明确独立证据仍可支持绑定。
5. 每项 artifact_ref 必须位于本事件 artifact_refs；该引用集合不承担展示顺序或精确版本权威。
6. 历史快照绑定 `(installation_id, source_event_key, source_revision)`；不可变，更正产生新修订。当前目录／展示／最新版本不能覆盖历史。
7. TrustedContext 使用同样的字段与语义，以不可变 DisplaySnapshot/ArtifactVersion 表示；SourceEvent 使用对应 JSON DTO。capture 对 observed 快照逐项核对可信上下文，模型请求无法提供这些字段。
8. 引用不授予读取、保存、复制权限；展开仍检查真实版本、作用域、可见性和删除。快照存在不等于附件可访问。

P01 落实结构、可信捕获核对与反例；P04/P07/P11/P12/P15 及 M13/M46–M50/J01 验证真实显示、存储、绑定、删除和原版本回读。宿主无法提供完整可信列表时不能伪造 observed。

## 同步与兼容

原始 v1.1 包保持不变，工作树只修订 source_event Schema。其他六份 Schema 保留原字节；正式外部入口尚未实现，因此没有现有接收端被静默升级。

旧输入仍可通过结构校验，但缺失字段不证明来源或展示完整。旧接收端使用 additionalProperties=false，会拒绝新增字段；禁止删掉这些字段后冒充兼容。能力协商与明确版本错误归 P11/P12/P14，未验证的接收端标 unsupported/unverified。

P01 新函数目前没有接入 CLI/MCP/Hermes/Codex；同一 JSON 解码边界已测试，不意味着三个真实入口均通过。没有模型语义评测或真实宿主回调，L1–L4 仍 not_run。G0 仍等待 P02 实测、辅助模型预算及留出规模裁决。
