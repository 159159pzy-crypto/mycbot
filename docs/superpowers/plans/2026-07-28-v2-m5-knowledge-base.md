# V2-M5 知识库实施计划

## 目标

把运营者上传的 Markdown、TXT、PDF 文档转为可审计、可引用、按作用域隔离的知识来源；提供父子分块检索、控制台命中测试，以及高阈值标注回复直返。所有回复继续经过 Agent Worker 的出站审核。

## 参考机制

- Dify ParentChildIndexProcessor：父块保留上下文，仅对子块建立向量索引，召回子块后回填父块。
- Dify HitTestingService：调试检索独立于聊天链路，可调 top_k 与 score threshold，并返回完整召回记录。
- Dify Annotation Reply：Top-1 高阈值命中，记录命中历史；检索异常时回退普通模型回复。
- FastGPT searchTest：检索测试沿用正式检索参数和权限边界，同时写审计。

本项目独立实现，不复制上述项目代码。

## 数据与边界

- `kb_document`：文档元数据、原始内容、内容哈希、generation、处理状态、作用域与可选 conversation_id。
- `kb_chunk`：父子块、自关联 parent_id、内容、token 估算、embedding 与 embedding_model。
- `annotation`：问题、审核答案、作用域、embedding、阈值、启用状态、来源消息与命中计数。
- `annotation_match_audit`：每次标注匹配的 HIT/MISS/ERROR、分数、阈值与会话。
- 仅支持 GLOBAL 和 CONVERSATION。临时沙盒会话只读 GLOBAL。
- 向量比较必须同时匹配 `embedding_model`；作用域由 SQL 条件限制。

## 测试优先顺序

1. 合同校验：文档类型、状态、作用域和标注阈值。
2. 文本/PDF 提取与父子分块：子块均归属父块，边界和重叠稳定。
3. Repository：文档哈希去重、generation 幂等、作用域隔离、子块排序与父块扩展。
4. `kb_search`：能力授权、结果结构、`sources` 引用以及错误回退。
5. 标注匹配：高阈值且 margin 足够才直返；MISS/ERROR 回退；命中仍经过 moderation。
6. Operator API：上传只入队、只读命中测试、标注 CRUD、从 Bot 消息存为标注。
7. Web：知识库页面、参数化检索面板、标注管理、会话回复“存为标注”。
8. 迁移 upgrade/downgrade、全量 pytest、ruff、pyright、Web test/build 和浏览器验收。

## 运行链路

1. API 校验文件并持久化 `QUEUED` 文档，发布 `{document_id,generation}` 到 knowledge stream。
2. Knowledge Worker CAS 抢占当前 generation，解析文件、父子分块、批量生成子块 embedding，并在单事务内替换该 generation 的块。
3. `kb_search` 对 query 生成 embedding，SQL 先约束 model/scope，再按 child cosine distance 排序，返回 child + parent，并用 `kb://document/chunk` 生成引用。
4. Agent Worker 在普通 LLM 前尝试标注匹配；仅 Top-1 达到阈值且与 Top-2 有足够 margin 时生成 ReplyPlan，否则走原 AgentTurnEngine。两条路径最终统一经过 `_moderated()`。

## 验收

- 相同文件重复上传不会生成重复 READY 文档。
- Streams 重投或旧 generation 不会覆盖新摄取结果。
- 会话 A 永远无法召回会话 B 的知识；沙盒只能召回 GLOBAL。
- `kb_search` 引用进入 ReplyPlan.citations 并由现有“参考来源”页脚渲染。
- 标注命中不调用聊天模型，MISS/ERROR 正常回退，命中回复仍可被 moderation 阻断。
- 控制台可上传、观察状态、调试召回、管理标注，并从 Bot 回复创建标注。
