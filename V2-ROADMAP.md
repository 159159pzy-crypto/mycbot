# MyBot v2 路线图：从问答机器人到有生命力的常驻智能体

**状态：规划中。** v1 的八个里程碑已全部交付（见 [ROADMAP.md](ROADMAP.md)）。
本文档是 v2 的升级计划：基于对 20 个可比开源项目的调研和对本仓库的
能力缺口分析，规划三个阶段、八个里程碑。每个里程碑动工前，应按
`docs/superpowers/plans/` 的既有格式撰写带测试优先任务拆分的实施计划。

## v2 的主题

v1 回答的问题是"一个自托管运营者能否跑起一个可靠、安全、可运维的
双平台 LLM 机器人"。v2 回答三个新问题：

1. **像活人，而不只是能用** —— 群聊里会读空气、有长期演化的记忆与
   关系、会主动找人聊天（MaiBot、OpenClaw 已验证这是用户留存的核心）。
2. **能力可扩展** —— 看图说话、查资料库、装插件不用改核心代码
   （AstrBot、LangBot、Koishi 已验证生态是竞争力）。
3. **运营可闭环** —— 换模型不怕挂、改人设可回滚、坏答案能变成回归
   用例（Dify、FastGPT 已验证 LLMOps 是长期运营的地基）。

## 继承 v1 的设计原则（不变）

- **契约只做加法。** `src/mybot/contracts` 仍是公共接口面；v2 所有
  新消息段、新配置对象都是判别联合/新字段的纯增量，配迁移测试。
- **隐私在代码和 SQL 中强制，不委托给提示词。** 记忆 2.0 的所有合并、
  失效、反思操作继续走既有隐私过滤路径，并保持对抗性测试。
- **单运营者自托管、回环绑定、最小权限容器。** 不做多租户 SaaS；
  昂贵能力（多模态、反思循环、评测）全部默认关闭、按需开启。
- **测试优先。** RED 证据先行，每个里程碑以 `make verify` 全绿收尾。

## 参考项目

调研于 2026-07（除斯坦福小镇为经典论文项目外均经实时核实）：

| 集群 | 项目 | 借鉴点 |
| --- | --- | --- |
| 直接竞品 | [AstrBot](https://github.com/AstrBotDevs/AstrBot) | 插件市场、WebChat 调试页、知识库、多提供商管理 |
| | [LangBot](https://github.com/langbot-app/LangBot) | 多流水线（会话×人格×模型绑定）、运营仪表盘、内容安全 |
| | [MaiBot](https://github.com/MaiM-with-u/MaiBot) | 回复意愿系统（读空气）、表达学习、关系系统 |
| | [chatgpt-on-wechat](https://github.com/zhayujie/chatgpt-on-wechat) | 全模态消息、自动整理的个人知识库 |
| | [ChatLuna](https://github.com/ChatLunaLab/chatluna) | 按房间预设、预设市场、冷却限流 |
| 框架与协议 | [NoneBot2](https://github.com/nonebot/nonebot2) | 适配器分层、Rule/Permission 谓词组合、插件商店 |
| | [Koishi](https://github.com/koishijs/koishi) | 插件热重载、Context/Service 依赖注入、控制台沙盒 |
| | [Satori](https://satori.js.org/) / [OneBot v12](https://12.onebot.dev/) | 平台中立的类型化消息元素模型 |
| | [NapCatQQ](https://github.com/NapNeko/NapCatQQ) / [Lagrange](https://github.com/LagrangeDev/Lagrange.Core) / [Milky](https://milky.ntqqrev.org/) | QQ 协议端漂移风险与热备选型 |
| 记忆与智能体 | [Letta (MemGPT)](https://github.com/letta-ai/letta) | 核心记忆块、智能体自编辑记忆、睡眠期整理 |
| | [mem0](https://github.com/mem0ai/mem0) | 抽取-合并管道（ADD/UPDATE/DELETE 决策）、混合检索 |
| | [OpenClaw](https://github.com/openclaw/openclaw) | heartbeat 主动回合、SKILL.md 技能、配对码门控 |
| | [Graphiti](https://github.com/getzep/graphiti) | 双时间轴失效模型（失效而非删除） |
| | [Generative Agents](https://github.com/joonspk-research/generative_agents) | 记忆流打分检索与周期性反思的思想源头 |
| LLMOps 平台 | [Dify](https://github.com/langgenius/dify) | 审核扩展点契约、标注回复、父子分块 RAG、提示词版本化 |
| | [FastGPT](https://github.com/labring/FastGPT) | 模型渠道池、按调用记账、批量评测 |
| | [Rasa](https://github.com/RasaHQ/rasa) | 对话回归测试（断言式 test stories） |

## 缺口 → 里程碑总览

代码缺口分析（带文件证据，完整结论存档于调研记录）归纳为 11 项，
映射到八个里程碑、三个阶段：

| # | 里程碑 | 阶段 | 解决的缺口 |
| --- | --- | --- | --- |
| V2-M1 | 消息段与模型路由地基 | 一：地基 | 富媒体缺失的契约层前提；单模型无路由；NapCat 单点依赖 |
| V2-M2 | 多模态与沙盒调试 | 一：地基 | 不能看图/发图；调试必须真上 QQ/TG；全链路不可观测 |
| V2-M3 | 记忆 2.0 | 二：生命力 | 记忆只增不并、矛盾积累、纯向量检索中文召回弱 |
| V2-M4 | 群聊性格与主动性 | 二：生命力 | 群里存在感弱；主动消息是固定模板；全局单人格 |
| V2-M5 | 知识库 | 二：生命力 | 无文档 RAG，答不了"投喂的资料" |
| V2-M6 | 插件生态 2.0 | 三：平台化 | manifest 声明字段未消费；无热重载；扩展只有重量级插件一条路 |
| V2-M7 | 内容安全与评测 | 三：平台化 | 审核是无条件放行的占位；改配置无回归防线 |
| V2-M8 | 规模化与运维 2.0 | 三：平台化 | 单进程锁/内存态 broker 不能多副本；worker 无遥测；死信靠 CLI |

阶段一是纯地基（后续一切依赖它），阶段二是用户可感知的"生命力"
主线（v2 的灵魂），阶段三是平台化收尾。阶段内里程碑可并行度见各节。

---

## V2-M1 — 消息段与模型路由地基

**目标：** 为 v2 全部特性打两个地基——平台中立的富媒体消息段，和
多提供商模型路由。本里程碑结束时用户看不到新功能，但契约面已就绪。

**交付：**

- **类型化消息段（参考 Satori / OneBot v12）。** 在
  `contracts/messages.py` 的 `MessageSegment` 判别联合上纯增量补充
  `at/sticker/voice` 段（回复引用复用既有 `ReferenceSegment` 与
  `reply_to_message_id`，不再新增第三种表达）；`ReplyPlan` 增加媒体
  段（图片、表情意图落地既有 `meme_intent` 字段）；QQ/Telegram 两端
  翻译器（均为纯函数）实现新段的 encode/decode，`PlatformCapabilities`
  新增对应能力布尔位（纯增量）做协商，不支持的段降级为文本描述。
- **插件边界的契约兼容策略（前置任务）。** 契约基于
  `FrozenModel(extra="forbid")`，而信封会整体 dump 推给插件事件钩
  子——给 `MessageEnvelope`/`PluginManifest` 加字段会让持旧版 SDK 的
  树外插件校验直接报错。先定义 SDK 边界的容错读取或契约版本协商，
  v2 全部契约增量都以此为前提。
- **模型 provider 注册表与按用途路由（参考 AstrBot / Dify / FastGPT）。**
  现有 `llm_*` 与 `embedding_*` 两组静态配置（无路由、无降级）升级
  为渠道列表（name/base_url/model_map/priority/weight），按用途路
  由：chat、记忆提取、embedding、vision 各自可指定档位（提取类任务
  用便宜小模型）；429/5xx 时渠道进 Redis 冷却键并顺延下一渠道，全部
  失败才走既有优雅降级。渠道的非密钥部分经 `system_kv` + TTL 链路下
  发；**api_key 保持 env-only**（渠道条目只存环境变量引用名），延续
  v1 的密钥纪律与日志脱敏。控制台加"模型管理"页（连通性测试、渠道
  健康状态）。
- **按调用记账。** 新表 `llm_call_log`（usage、渠道、模型、时延、触
  发类型、关联会话），配静态单价表折算成本；控制台总览页加按天/按
  会话/按渠道的用量卡片。
- **QQ 协议端漂移对冲（参考 NapCat/Lagrange/Milky 调研结论）。**
  OneBot v11 仍是事实标准但漂移信号明确（NapCat 授权收紧、Lagrange
  主推 Milky）。不换协议，把可替换性做成资产：录制 OneBot v11 事件
  夹具建立适配器一致性回放测试；NapCat 特有行为隔离到独立模块；文档
  登记 Lagrange.OneBot 为热备协议端（同为 v11，理论上仅换 Compose
  服务）。

**依托扩展点：** `MessageSegment` 判别联合、`ReplyPlan`/`shape_reply`
单一出口、`system_kv` 运行时配置链路、适配器纯翻译器 + fixture 测试。

**风险：** 消息段是 v2 最大的契约变更，必须一次设计到位（加段容易删
段难）；先写 Satori/OneBot v12 段模型的对照评审再定契约。

## V2-M2 — 多模态与沙盒调试

**目标：** 机器人能看图、能发图；运营者不开 QQ/TG 就能测试和排查。

**交付：**

- **图片理解。** 入站图片段经 vision 档位模型描述或直接多模态对话
  （按渠道能力配置）；`agent_turns.py`/`memory_service.py` 不再把非
  文本消息整体降级为英文占位符（`NON_TEXT_PLACEHOLDER`），提示装配
  保留图片引用。
- **富媒体回复。** 模型输出经 `shape_reply` 支持图片/表情段；QQ 表
  情包（meme_intent）按平台能力渲染。语音（ASR/TTS）列为本里程碑的
  可选项，默认不做。
- **控制台 WebChat 沙盒（参考 Koishi Sandbox / AstrBot ChatUI）。**
  新增 `sandbox` 虚拟适配器：控制台经 bearer 鉴权提交合成
  `MessageEnvelope`，走完全相同的 Streams 管道与 agent 流程，回复回
  推控制台；沙盒会话打 ephemeral 标记，跳过记忆提取与主动策略，避免
  污染真实数据。
- **全链路追踪视图（参考 FastGPT 调用链）。** `MessageEnvelope` 增加
  `trace_id`；各 worker 在关键阶段（检索/工具/LLM/审核/出站）写
  `trace_span` 表；控制台会话详情页加"追踪"展开：分段耗时、召回的
  记忆、工具调用、审核判定。这同时是排障工具和"回复为什么是这样"
  的可解释性面板。
- **中文体验修正。** 降级/回退文案全部中文化并可配置；token 估算按
  中英文混合校正（chars/4 对中文严重高估，等于变相缩小历史窗口）。

**依托扩展点：** V2-M1 的消息段与 vision 路由、`StreamBackend` 管道
原语、`AgentTurnEngine` 的 Protocol 注入（沙盒替换出站投递即可）。

## V2-M3 — 记忆 2.0

**目标：** 记忆从"只增不并的向量库"升级为"会整理、可追溯、召回准
的长期记忆"。这是"数字生命"的内核，也是后续性格系统的地基。

**交付：**

- **抽取-合并管道（参考 mem0）。** 每条候选事实先做同 scope + 隐私
  过滤的相似检索，LLM 输出结构化操作（ADD/UPDATE/DELETE/NOOP），单
  事务执行；被 `/forget` 撤销的主题禁止 UPDATE 复活。
- **双时间轴失效模型（参考 Graphiti）。** 记忆契约已有
  `valid_from/valid_until/supersedes/conflicts_with/source_message_ids`
  且检索已过滤有效期外与被取代项——本项不是从零建设，而是统一语义：
  补 `invalid_at/invalidated_by` 并把合并管道的 UPDATE/DELETE 收敛到
  这一套失效机制上（严禁两套并行），控制台记忆页加历史版本视图。
  `/forget` 保持立即生效（失效 + 隐私路径硬删除并存）。
- **混合检索（参考 mem0 / OpenClaw）。** pgvector 向量 + Postgres 全
  文（中文按 pg_jieba/zhparser 可用性择一，退化方案 pg_trgm）双路
  top-k，RRF 融合，保留既有 scope/隐私过滤；命中来源记入审计便于控
  制台调优。QQ 场景的专名、黑话、缩写召回是主要受益者。
- **核心记忆块与自编辑（参考 Letta）。** 新增 `core_block`
  （persona/用户画像，带 token 预算），每回合固定注入而非靠检索命
  中；新增 `memory_append/memory_replace` 内建工具，走既有工具策略
  （可设 approval_required）。
- **睡眠期整理（参考 Letta dreaming / 斯坦福小镇反思）。** 维护
  worker 新增周期任务：回顾近期对话与记忆，LLM 输出合并/改写/晋升
  （CONVERSATION→SUBJECT/GLOBAL）/洞察生成操作，事务化执行并写操作
  审计。默认低频（如每日一次），可关。
- **截断前记忆冲刷（参考 OpenClaw）。** 历史越过 token 上限被裁剪
  前，先用便宜档位模型从将丢弃的消息中抽取值得持久化的事实，走合并
  管道；按会话去抖。

**依托扩展点：** `MaintenanceWorkerService` 周期任务骨架、记忆隐私
的 SQL/代码双重强制、V2-M1 的模型分档路由（整理任务用便宜模型）。

**风险：** LLM 驱动的合并/反思可能误删好记忆——双时间轴失效模型正是
兜底（可回滚、可审计）；反思频率与预算须有硬上限。

## V2-M4 — 群聊性格与主动性

**目标：** 机器人在群里从"被 @ 才答题的工具"变成"有分寸的群成员"，
在私聊里从"从不主动"变成"偶尔惦记你"。

**交付：**

- **回复意愿系统（参考 MaiBot 的"读空气"）。** 在 agent worker 之
  前加意愿评分阶段：被 @/关键词/话题与 persona 及记忆的向量相关度/
  群热度/冷却时间加权打分，过阈值才进 LLM；每群开关与灵敏度进控制
  台，天然受既有限流约束。评分拦截数入指标。
- **表达学习（参考 MaiBot / AstrBot 自学习插件）。** 新增 EXPRESSION
  类记忆：周期性从群消息中抽取高频表达与语气模板（走既有抽取管道，
  带隐私级别与衰减）；prompt 按会话注入 top-N 风格样例。`/forget`
  与撤销机制直接覆盖。
- **关系系统（参考 MaiBot）。** SUBJECT 记忆上加 relationship 记录
  （熟悉度分值 + 印象摘要），抽取 worker 增量更新、随 decay 回落；
  prompt 注入"你与此人的关系"影响称呼与语气；控制台可查可改。
- **heartbeat 主动回合（参考 OpenClaw，落地 v1 backlog 的 LLM 主动
  内容）。** 升级现有 proactive 任务：注入核心记忆块 + 近期话题的精
  简上下文，LLM 判断"是否值得打扰"，约定 `HEARTBEAT_OK` 哨兵输出
  即静默丢弃；保留既有 policy 门控、频控、静默时段，生成内容入审计。
- **会话档位 profile（参考 LangBot 流水线 / ChatLuna 房间预设）。**
  引入 profile 对象（persona × 模型档位 × 工具授权 × 记忆策略 × 意
  愿参数），会话→profile 绑定表；一个实例同时"客服群严肃、闲聊群
  卖萌"。控制台人设页升级为 profile 管理页。
- **persona 版本化（参考 Dify）。** `persona_version` 追加表 + 文本
  diff + 一键回滚（回滚即新版本，延续不可变契约风格）；版本 id 关联
  `llm_call_log` 与后续评测，"哪版人设效果好"可量化。

**依托扩展点：** `system_kv` 配置链路（profile/意愿参数免重启生
效）、Guards 单方法协议（意愿评分作为新守卫插入）、V2-M3 的记忆能力。

**风险：** 意愿系统调不好会变成刷屏或装死——默认保守阈值 + 每群灰度
开启；表达学习须尊重隐私级别（PRIVATE 表达不得跨会话泄漏，沿用对抗
测试）。

## V2-M5 — 知识库

**目标：** 运营者投喂的资料（群规、文档、FAQ）成为可引用的回答来源。

**交付：**

- **文档摄取与父子分块（参考 Dify / FastGPT）。** 新表
  `kb_document/kb_chunk`（含 parent_id：子块匹配、父块作上下文）；
  摄取作为独立任务走既有 Streams；首版只支持 md/txt/pdf。
- **`kb_search` 内建工具。** 纳入既有工具策略与审计，命中来源进
  `ReplyPlan.citations`，与搜索引用同一渲染路径（`Sources:` 页脚）。
- **检索测试面板（参考 Dify）。** 只读调试端点 + 控制台测试框：任意
  query 观察召回块、分数、作用域，滑杆调 top_k/阈值即时重查——同时
  服务于记忆检索调优。
- **标注回复（参考 Dify Annotation Reply）。** 运营者精修过的答案存
  `annotation` 表（复用 pgvector），高阈值语义命中直接返回（仍过出
  站审核），命中率入指标；控制台会话页每条 bot 回复加"存为标注"。

**依托扩展点：** `ToolCatalog` 协议（kb_search 即一个新 Tool）、
pgvector 基础设施、引用渲染链路。与 V2-M4 无依赖，可并行。

## V2-M6 — 插件生态 2.0

**目标：** 把 v1 打好的隔离地基长成好用的扩展系统：轻量能力不用写
代码，重量插件改完即生效，配置不用手改文件。

**交付：**

- **Markdown 技能体系（参考 OpenClaw SKILL.md）。** `skills/` 目录 +
  SKILL.md 清单（名称/描述/触发说明）；提示词注入技能目录，LLM 经内
  建 `load_skill` 工具按需读入全文（计入预算）；控制台可启停可编辑。
  "每日总结格式"这类需求从此不用写代码插件。
- **落地 manifest 已声明未消费的字段。** `config_schema`：控制台按
  JSON Schema 自动渲染插件配置表单（参考 Koishi schemastery），保存
  后经 broker 通知插件；`tasks`：插件定时任务由维护 worker 调度，经
  broker 派发到隔离 runner。
- **单插件热重载（参考 Koishi HMR）。** runner 改为 per-plugin 子进
  程监督：单独 kill+respawn 目标插件并重放握手，其余插件不受影响；
  控制台加 enable/disable/reload 与最近加载错误；崩溃循环指数退避 +
  熔断。
- **服务注入与版本协商（参考 Koishi Context / NoneBot2 DI）。**
  manifest 增加 `requires: {service: 版本}`（如 `memory.search@1`、
  `llm.complete@1`、`kv.store@1`）；broker 发布服务目录，握手时协
  商，不满足则拒载并在控制台标红；每个服务是带审计与配额的类型化端
  点，沿用能力授权。
- **插件脚手架与可信索引（参考 nb-cli / Koishi 市场）。**
  `mybot plugin new <name>` 生成 manifest/入口/pytest 骨架；仓库内
  `registry.json`（名称/版本/源 URL/manifest 哈希）作可信索引，控制
  台支持从索引安装（下载→校验→哈希比对→加载）。
- **陌生私聊配对码（参考 OpenClaw pairing）。** 未知 subject 的私聊
  不进 agent 管道，回复一次性配对提示；控制台批准/生成配对码，策略
  （open/paired/allowlist）按平台配置。防陌生人加好友消费额度。

**依托扩展点：** `PluginManifest` 未消费字段、broker 隔离拓扑、
`ToolCatalog` 合并模式、operator 审计链路。

**风险：** 服务面一旦发布就是对插件的兼容承诺——首批服务只开
memory.search/llm.complete/kv.store 三个，宁少勿滥。

## V2-M7 — 内容安全与评测

**目标：** 补上 v1 两笔明账：真实审核后端（公开群的生存需求），和
改配置的回归防线（改人设/换模型不再靠手感）。

**交付：**

- **双向审核扩展点（契约参考 Dify，可整体照抄）。** 入站（用户消
  息）与出站（生成文本）两处插桩；请求 `{point, params}`、响应
  `{flagged, action: direct_output|overridden, preset_response}`。
  三种后端：本地词表（Aho-Corasick，控制台可编辑）、可选 OpenAI 兼
  容 `/moderations` 端点、经插件 broker 的审核插件（新 capability：
  moderation）。超时按配置 fail-open/fail-closed，判定全部入审计。
- **逐次工具审批（落地 v1 backlog 的 interactive per-call
  approvals）。** `approval_required` 工具在无常备授权时不再直接拒
  绝，而是挂起进待审队列；控制台实时批准/拒绝（复用 broker 的
  pending invocation 机制），超时自动拒绝并走既有降级路径，全程入
  operator 审计。
- **回归评测集与影子重放（参考 FastGPT 评测 / Rasa test stories）。**
  用例存 repo（question/history/expected/断言：must_contain、
  must_not_contain、must_call_tool、must_cite）；eval runner 以影子
  模式注入既有管道但拦截出站；LLM 裁判打分可关。控制台评测页看逐条
  diff，CI 可选跑纯断言子集（不耗付费 token）。评测结果关联 persona
  版本与模型渠道。
- **反馈闭环（参考 FastGPT）。** 会话页每条 bot 消息加好评/差评 + 备
  注；差评两个一键动作：转回归用例（预填上下文）、精修后存为标注回
  复。差评率进指标页，作为改动效果的粗粒度信号。

**依托扩展点：** `ModerationHook` 单方法协议（v1 预留的占位正是插桩
点）、V2-M4 的 persona 版本、V2-M5 的标注表。

## V2-M8 — 规模化与运维 2.0

**目标：** 消除"单进程假设"，补齐观测与运维闭环。多数用户单机即
够，本里程碑保证"想扩就能扩、出事看得见"。

**交付：**

- **会话锁与状态外置。** agent worker 的进程内 `asyncio.Lock` 字典
  改为 Redis 租约锁（含清理），worker 可多副本；插件 broker 注册状
  态与 operator 鉴权限流移入 Redis/Postgres，API 重启不再丢插件注册。
- **全进程遥测。** OTel span 从仅 API 扩展到 gateway/agent-worker/
  maintenance（LLM 调用、工具执行、流消费延迟），trace_id 与 V2-M2
  的追踪表对齐；可选 Prometheus `/metrics`。
- **控制台运维页。** 死信流检查与一键重放（替代 RUNBOOK 的手动
  redis CLI 流程）、以 bot 身份向指定会话发消息、proactive opt-in
  从手填 stable_key 改为会话选择器。
- **Agent 快照与离站备份（参考 Letta Agent File，落地 v1 backlog）。**
  `mybot export/import`：persona、核心记忆块、有效记忆（可含失效历
  史）、技能与授权序列化为带 schema 版本的 JSON 包，import 走合并管
  道防重复；备份脚本支持推送远端存储（rclone 约定）。"陪伴一年的
  bot"变成一个可携带文件。
- **（可选）Satori 客户端适配器。** 若届时确有第三平台需求，接一个
  自托管 Satori server 换取 Discord/飞书等生态；依赖 V2-M1 消息段，
  作为"二等平台"降级支持。默认不做。

**依托扩展点：** `ProcessMode/LifecycleService` 进程角色模型（新
worker 免费获得优雅停机）、`StreamBackend` 原语、备份脚本。

---

## 排序依据

- **契约先行**（V2-M1）：消息段与模型路由被多模态、意愿系统、知识
  库、heartbeat 全部依赖，且是最难返工的部分。
- **先可见后可扩**（阶段二在阶段三前）：记忆/性格/知识库是用户可感
  知的差异化主线；插件生态与评测体系服务于长期，但没有好的核心体验
  就没有长期。
- **记忆先于性格**（V2-M3 → V2-M4）：表达学习、关系系统、heartbeat
  个性化全部消费记忆 2.0 的合并管道与核心块。
- **安全评测在生态后**（V2-M7）：审核插件后端复用 V2-M6 的插件事件
  派发通道（软依赖，词表与 API 后端不受影响）；评测依赖 V2-M4 的
  persona 版本。
- **规模化最后**（V2-M8）：单机假设在此前不阻塞任何功能，最后一次
  性偿还。

## 非目标（v2 仍然不做）

- 多租户 SaaS、公网直接暴露（仍要求运营者自备 TLS/鉴权代理）。
- 把 NapCat 捆进 Compose；把容器当作对恶意插件代码的充分沙箱。
- 语音通话、视频理解；自训练/微调模型。
- 真正的公共插件市场（只做可信索引 + 安装通道；市场需要多运营者生
  态，超出单运营者产品定位）。
- 工作流可视化编排（Dify/n8n 的领域；MyBot 的扩展面是工具/技能/插
  件，不是流程图）。

## 风险

- **范围失控。** 八个里程碑约等于再造一个 v1 的工作量。对策：每个里
  程碑保持 v1 式"not in this milestone"清单；阶段二的三个里程碑各
  自可独立发布、独立产生价值。
- **LLM 成本放大。** 反思、heartbeat、意愿评分、表达学习都是新增
  LLM 调用。对策：V2-M1 的分档路由让便宜模型承担后台任务；一切后台
  智能默认关闭；`llm_call_log` 从第一天就能看到每项功能烧多少钱。
- **记忆系统复杂化。** 合并/失效/反思引入新的正确性面。对策：双时间
  轴保证可回滚；隐私对抗测试作为每个记忆改动的验收门。
- **QQ 协议端漂移。** NapCat 授权收紧、Milky 兴起。对策：V2-M1 的一
  致性夹具与热备预案；QQ 侧任何断裂优先按"换协议端"处理而非改架构。
- **性格系统的社交风险。** 读空气失准、主动消息骚扰。对策：全部默认
  保守、按群灰度、频控与静默时段硬约束、内容入审计可回看。

## 完成标准

v2 "完成"当一个自托管运营者可以（每项有测试与 CI 证据）：

- [x] 1. 发一张图给机器人并得到理解后的回复；机器人在合适时机回以
  表情包（V2-M1/M2）。
- [x] 2. 配置两个以上模型渠道，拔掉其一，对话无感切换；控制台能看到
  每个渠道本月花了多少钱（V2-M1）。
- [x] 3. 在控制台沙盒里与 bot 对话，展开任意一条回复看到完整的检索/
  工具/延迟追踪（V2-M2）。
- [x] 4. 告诉 bot "我换工作了"，旧工作记忆被失效而非并存，且能在控
  制台看到失效历史（V2-M3）。
- [x] 5. 在一个测试群里观察到：相关话题不 @ 也会插话，无关话题保持
  沉默；一段时间后 bot 的说话方式带上群内风格（V2-M4）。
- [x] 6. 收到一条引用最近话题的个性化主动消息，且夜间从不打扰
  （V2-M4）。
- [x] 7. 上传一份 PDF 后提问，回答带该文档的引用（V2-M5）。
- [x] 8. 不写前端、不改核心代码：用 SKILL.md 加一个轻量能力，用脚手
  架生成一个带定时任务与配置表单的插件，改完 reload 即生效（V2-M6）。
- [x] 9. 词表命中的消息被按策略拦截并留痕；需审批的工具调用挂起后
  能在控制台实时批准；改一版 persona 后跑评测集，得分对比上一版可
  见，不满意一键回滚（V2-M7）。
- [x] 10. agent worker 跑两个副本顺序不乱；死信在控制台一键重放；一
  条命令导出"整个 bot"为可迁移快照（V2-M8）。

### 验收证据（2026-07-28）

验收使用确定性模型/Embedding 模拟端点，避免付费 token，并在真实
PostgreSQL、Redis、Streams、Gateway、双 Agent Worker 与 Web 控制台上
走完整链路。GitHub Actions 的 `integration` job 会设置独立测试数据库与
Redis DB，执行全部 `integration` 标记测试；其余契约、单元、前端测试由
`python` 与 `frontend` jobs 覆盖。

| # | 自动化与实机证据 |
| --- | --- |
| 1 | `test_telegram_update_reaches_direct_vision_as_data_url`、`test_direct_multimodal_turn_uses_vision_route_not_chat_route`、QQ/TG meme 渲染测试；实机沙盒留下 `vision.prepare=ok`。 |
| 2 | `test_retryable_failure_cools_channel_and_fails_over`、成本记账与 `ModelsPanel` 测试；实机双渠道验证首渠道 503 后由次渠道成功，控制台显示按渠道成本。 |
| 3 | `test_sandbox_uses_real_streams_persistence_gateway_and_trace_path`；实机沙盒记录 ingest、decision、vision、LLM、tool、moderation、publish、delivery 追踪。 |
| 4 | 记忆 ADD/UPDATE/DELETE/NOOP、时态失效、隐私过滤、历史版本与 `/forget` 集成测试。 |
| 5 | `test_profile_willingness_can_admit_an_unmentioned_group_message`、保守静默测试、群表达学习与关系版本测试。 |
| 6 | `test_llm_heartbeat_uses_profile_and_recent_topic_then_audits_send`、`HEARTBEAT_OK` 静默、频控与跨午夜 quiet-hours 测试。 |
| 7 | PDF 文本提取回归测试、父子分块与 `kb_search` 引用测试；实机上传 PDF 后状态为 READY，回答带 `kb://` 引用。 |
| 8 | SKILL.md 目录/按需加载测试、`mybot plugin new` 脚手架测试、定时任务/配置下发测试；实机单插件 reload 后 PID 更新且其余服务不中断。 |
| 9 | 本地/API/plugin 审核、逐次审批、评测断言、反馈闭环与 persona 回滚测试；实机词表命中留痕、审批转 APPROVED、评测由 0/1 变为 1/1、persona v1→v2→回滚 v3。 |
| 10 | 真实 Redis 双 worker 租约与原子死信重放测试、100 消息顺序/无死信负载测试、快照幂等导入测试；Compose 双 Agent Worker 运行，实机死信重放成功，导出 `mybot.agent.snapshot` v1 且无密钥字段。 |
