# P01 同版冲突裁决材料

状态：待独立裁决；不是 G0 通过。实现起点 `1db6f09c99bd746085eca03c63cd24329a99b7ad`，业务基线 `578b955802df753f2e2208e26eab6f71971285a0`。原 v1.1 包 77 项哈希全部匹配；原包不修改。

## 长来源分段

依据：07 第 4.3 节要求记录原顺序、总段数或真实未知状态，不得截断后标 complete；第 12.2 节要求保留边界和重叠来源 ID。原 `source_event.schema.json` 只允许最多 65,536 字符、capture_state 和 evidence_refs，没有结构化分组、段序与总数。将这些信息藏进 content 会改变来源正文，藏进未规定格式的 key 则使宿主分别发明规则。

最小提案：仅在 SourceEvent 增加可选 `segment`，含 `group_key`（1–512 字符）、`index`（从 0 开始的整数）、`total`（正整数或 null）、`truncated`（布尔）。同一分组修订由 group_key + source_revision 确定；来源修改后整组 revision 增长，source_event_key 保持每段稳定身份。已知 total 时必须 index < total；total 未知或 truncated=true 时 capture_state 必须 partial/gap。缺少 segment 表示单个有界事件；长输入的捕获器必须实际分段，不能直接省略。重叠段引用继续使用 evidence_refs，不能把重复窗口当成新的人类确认。P04 验证完整组覆盖与重试幂等，P10 验证水位，M42 验证尾段。

## 历史展示顺序

依据：07 第 6.2 节要求稳定产物 ID、版本和当时的展示顺序；第 13 节要求精确版本回读。原 `artifact_refs` 只是字符串数组，没有定义版本字段和顺序的可信度；当前目录排序不能填补过去的观察。

最小提案：仅在 SourceEvent 增加可选 `display_snapshot`，含 `order`（observed/unknown）和 `items`（最多 32 个 `{artifact_ref, revision}`）。数组次序仅在 order=observed 时代表宿主实际展示次序；order=unknown 时不能根据位置确定“第二张/中间”。快照隶属于不可变 source_event_key + source_revision，不建立另一份可修改真值。每项 artifact_ref 必须也列入 artifact_refs；不引入 URL、路径、读取授权或自动保存许可。TrustedContext 同样携带最多 32 个有版本的当前可见引用及顺序状态，由适配器构造，模型请求不能填写。P04/P07/P11/P12/P15 与 M13/M46–M50/J01 负责实际捕获、绑定和回读。

## 边界

这两项是表达既有 B01/B03/B09/B10 所需的字段补全，不改变十二组行为、SQLite 权威或宿主范围。先提交独立审计者；裁决前不修改生效 Schema、不执行依赖这两个增量的任务。若接受，主线程同步本工作树 Schema、类型、校验、测试和映射，记录原包与修订哈希；原始规格包保留。

G0 仍依赖 P02 的实际 Hermes/Codex 入口证据与预算、留出规模裁决。合同子项接受不能冒充整体 G0 通过。现有 Python 原型只做 DTO/来源引用边界，不实现巩固、事实裁决或模型语义验证。
