# v1.1设计契约

这些JSON Schema是实施前目标格式，不是已部署接口。身份、scope、路径来自可信上下文，模型字段不能授予权限。

recall_packet新增answerability、coverage、unmet_needs及每项basis；recall_request可有经过校验的focus_refs。consolidation_result仅表示辅助模型提议，不能写active状态、权限或直接提交SQL。

Schema通过只代表结构有效，不证明证据支持、来源版本、删除、受众、指代或实际语义正确。quote必须在实际来源匹配，resolved_ref必须属于合法候选，时间顺序、依赖和结果证据均由核心交叉校验。

intention/procedure/alias是已有Claim的受限payload，不要求单独真值表。ResumeState是Episode派生结构。examples仅包含合成内容，未执行插件。
