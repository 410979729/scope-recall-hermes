# P08 证据归档

本目录是 P08 的 TRAIN-only 证据归档，当前状态为 **P08_NOT_CLOSED**。盲验尚未独立追加；本归档不表示 validation 已运行，也不表示完成了语义验收。

`artifact-index.json` 是唯一索引，记录每个归档文件的原始相对路径、归档相对路径、字节数、SHA256、phase 和 status。归档文件在本目录下按 `.execution/...` 或 `verification/P08/...` 的原相对路径保存，并已逐文件做字节与 SHA256 一致性核验。

本次共归档 80 个文件、11,600,893 bytes；另记录 11 个未复制的原始响应引用、20,411,313 bytes。早期 embedding-space / role differential 的 raw response 保留在原 `.execution` 位置，只在索引中记录路径、字节数和 SHA256，避免重复复制向量和响应体。

内容范围包括：

- v3、v4、v5 公开 TRAIN 输入及 label/token/revision 证据；
- 最终 v4 TRAIN 与 v5 composed TRAIN 的 manifest、receipt、request/response、vectors 和 threshold proposal；
- 早期 embedding-space 与 role differential 的 receipt/manifest/proposal，以及 raw response 哈希引用；
- native starvation 首败/修复证据、语义首版/修复版报告与 driver 日志；
- P08 targeted repair test 日志（含 45+17 与 36 passed 记录）。

明确排除 validation、PRIVATE、SEALED、gold、eval、凭据、SQLite/Lance 数据库、未选择的 `.execution` 文件，以及早期错误空间/role probe 的重复 raw response 和 vectors。
