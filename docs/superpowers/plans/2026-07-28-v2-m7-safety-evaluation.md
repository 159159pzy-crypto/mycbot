# V2-M7 内容安全与评测实施计划

## 目标

在现有 `ModerationHook`、persona 版本、模型路由、工具执行器和标注回复之上，
补齐真实的双向内容审核、逐次工具审批、可重复的影子评测以及消息反馈闭环。
所有安全判定、人工决策和评测结果都必须可审计；默认策略保守，但外部审核服务
不可用时的 fail-open/fail-closed 行为必须显式配置。

## 参考机制与取舍

- Dify `Moderation` 契约（`langgenius/dify@f3f2f63`）：沿用
  `flagged/action/preset_response` 语义和入站、出站双插桩；MyBot 使用异步后端、
  超时策略和逐后端审计，不复制其同步实现。
- Dify keywords/OpenAI moderation：本地后端升级为真正的 Aho-Corasick 自动机；
  远程后端使用 OpenAI 兼容 `/moderations`，密钥只从环境变量解析。
- OpenAI Agents Python HITL（`openai/openai-agents-python@421deb7`）：逐次审批绑定
  invocation id，拒绝和超时作为模型可见的工具错误继续原回合；MyBot 将暂停状态放入
  PostgreSQL，API 与 agent-worker 可跨进程协作。
- FastGPT evaluation（`labring/FastGPT@bebf217`）：评测 run/item 分表、逐项状态、
  分数和错误独立保存；MyBot 的用例保存在 repo，并加入确定性断言。
- Rasa test stories（`RasaHQ/rasa@60a3cff`）：历史与期望行为均声明式保存；MyBot
  使用 question/history/expected 和工具、引用断言，不引入 Rasa 运行时。

## 数据与安全边界

- `moderation_audit` 只保存后端、判定、耗时、错误和截断/哈希摘要，不保存远程密钥。
- 本地词表和审核策略存 `system_kv`，控制台编辑后下一次判定生效。
- `tool_approval_request` 以 invocation id 去重，保存最小必要参数；审批只对该次调用有效，
  常备授权仍由现有 `tools.approved_ids` 控制。
- `evaluation_run/evaluation_result` 关联 profile、persona version、模型渠道和用例快照；
  影子会话标记 ephemeral，生成结果不进入平台出站流。
- `message_feedback` 每条 bot 消息最多一条当前反馈，更新保留 operator 审计。
- 评测用例目录做 realpath containment；运行时新增用例写入挂载的 `evals/`，不允许路径逃逸。

## 测试优先顺序

1. 审核契约、Aho-Corasick 重叠命中、远程响应解析、组合后端超时策略。
2. Agent Worker 入站 direct-output/override、出站拦截、所有结果审计。
3. 工具逐次审批：批准、拒绝、超时、常备授权旁路、跨进程轮询和幂等决定。
4. 评测用例校验与确定性断言；影子消息不进入 outbound，结果关联 persona/model。
5. 反馈 CRUD、差评转用例、差评转标注、差评率指标。
6. Operator API 与 Web：审核策略/审计、待审队列、评测 run/item diff、消息反馈动作。
7. 迁移 upgrade/downgrade、Ruff、Pyright、全量 pytest、Web test/build、Compose 和浏览器验收。

## 运行链路

1. Agent Worker 在创建会话/持久化普通入站消息之前调用审核链。`direct_output` 直接返回
   预设文本并停止，`overridden` 以替换文本继续管道；出站 ReplyPlan 在持久化和发布前复用
   同一契约。
2. 审核链按 local -> api -> plugin 顺序执行。任一后端 flagged 即短路；异常按 fail mode
   生成 allow 或拒绝判定，每个尝试独立写审计。
3. approval-required 工具若无常备授权，executor 创建 pending request 并轮询数据库；
   控制台批准/拒绝后继续该次调用，超时原子改为 EXPIRED 并返回降级工具错误。
4. Operator 创建评测 run 后，为每个用例建立 ephemeral sandbox 会话、注入历史并发布带
   eval 元数据的最终问题。Agent Worker 完整运行现有 pipeline，但把 ReplyPlan 截获到
   evaluation result，不发布平台消息。
5. 会话页提交反馈。差评可把最近上下文和当前回复追加为 repo 用例，或把精修答案写入
   M5 annotation；所有动作写 operator audit。

## 验收

- 入站和出站都能被本地词表、OpenAI 兼容端点或 moderation 插件阻断/替换，超时策略可测。
- `approval_required` 工具在控制台出现，批准后只执行该次调用；拒绝/超时不执行工具。
- repo 用例可跑确定性断言；在线影子 run 不发平台消息，逐项 diff、工具调用和引用可见。
- 评测结果可追溯 persona version 与模型渠道；LLM 裁判默认关闭且可独立开启。
- bot 消息可好评/差评和备注；差评可转用例或标注，差评率进入指标页。

