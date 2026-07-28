# V2-M8 规模化与运维 2.0 实施计划

## 实施状态（2026-07-28）

已完成。Redis 会话租约、插件注册恢复、共享鉴权限流、全进程 OTel、
Prometheus、运维中心、死信原子重放、人工发信、会话式 proactive 配置、
Agent 快照与 rclone 备份脚本均已落地；Satori 按路线图的默认非目标未实现。
验收证据：真实 PostgreSQL/Redis 全量测试 `486 passed`，Web `29 passed`，
生产构建、Ruff、Pyright、Compose 配置校验通过，Compose 已以两个
agent-worker 副本运行并通过健康检查，桌面与 390px 移动端页面已实测。

## 目标

消除 agent-worker、插件 broker 与 operator 鉴权限流中的单进程状态，补齐跨进程
遥测、死信处置、人工发信、主动消息会话选择以及 Agent 快照迁移能力。默认仍面向
单机自托管，但同一角色扩为多个副本时不得打乱会话顺序，也不得因 API 重启丢失
插件目录或鉴权限流状态。

## 参考机制与取舍

- Redis 官方 distributed locks：采用随机 fencing token、`SET NX PX`、持有者校验的
  Lua 续租/释放；单 Redis 已是本项目的既有基础设施，因此不引入 Redlock 多主复杂度。
- OpenTelemetry Python manual instrumentation：非 API 进程显式初始化 TracerProvider，
  gateway/worker/maintenance、流消费、LLM 和工具执行均发 span；已有 UUID trace_id
  转为 OTel trace id，使外部链路与 `trace_spans` 表可对齐。
- Letta Agent File：快照使用带 schema/version/created_at 的可移植 JSON 文档；MyBot
  只导出自己的稳定契约，不复制 Letta 私有运行时对象。
- Prometheus text exposition：作为可选开关提供 `/metrics`，从 PostgreSQL/Redis 的共享
  运维数据生成 gauges/counters，避免只暴露某个 Python 进程的局部内存指标。

## 数据与安全边界

- 会话锁 key 只含 stable_key 的 SHA-256，不把聊天标识明文写进 Redis；租约超时、等待
  上限和续租周期显式配置，释放必须校验 token。
- 插件注册快照保存在 Redis，带最后心跳与 TTL；API 启动恢复目录，runner 重连时覆盖。
  待执行调用仍是短生命期请求，不承诺跨 API 崩溃恢复。
- operator 失败计数移入 Redis，key 使用客户端标识哈希；token 继续 env-only。
- 死信列表只在受 bearer 保护的 operator API 返回；重放会原子清理相应 dedupe key、
  投回源流并删除死信项，所有动作写 operator audit。
- 快照不含 API key、bot token、模型密钥或数据库凭证。import 默认 merge，按稳定 id/名称/
  内容边界去重；失效记忆只有显式 `--include-history` 才导出。
- rclone 备份脚本只消费本地快照路径和用户提供的 remote，不把凭据写入仓库。

## 测试优先顺序

1. Redis/内存租约：互斥、等待、续租、非持有者不可释放、取消后清理。
2. 两个 agent-worker 共享租约时同一会话串行、不同会话可并行。
3. 插件注册状态持久化/恢复/TTL；operator auth 失败计数跨实例共享。
4. OTel trace_id 对齐、流消费/LLM/工具 span，以及可选 Prometheus 输出。
5. 死信列表/重放、operator 发信、会话式 proactive opt-in API。
6. 快照 export/import：schema 校验、密钥不落盘、active/history 过滤、幂等 merge、技能与
   常备审批恢复；rclone 命令契约。
7. Web 运维页、迁移 upgrade/downgrade、Ruff、Pyright、全量 pytest、Web test/build、
   Compose 与浏览器验收。

## 运行链路

1. agent-worker 解析 envelope 后按 stable_key 获取 Redis 租约，后台续租；处理完成或取消
   时通过 token 校验释放。租约丢失会终止当前处理，使未 ack 的 stream entry 可重投。
2. plugin runner 注册时 broker 更新内存工作队列并写 Redis 注册快照；API lifespan 启动时
   恢复未过期 manifest 目录，runner 的轮询心跳刷新 TTL。
3. 所有非 API 角色在进程入口初始化 OTel。StreamConsumer 记录队列等待/处理 span，
   agent/gateway/maintenance 添加角色阶段，LLM 与 ToolExecutor 添加下游 span。
4. 运维页从受保护 API读取死信和会话；重放、人工发信和 proactive 变更均经服务端校验、
   持久化/发布并写审计，不要求操作员使用 redis-cli 或手填 stable_key。
5. `mybot export` 从 PostgreSQL、技能目录和 `system_kv` 生成 JSON；`mybot import` 验证
   schema 后在事务内 merge 数据，再原子写技能文件。`scripts/backup-agent.sh` 可选调用
   rclone 推送生成的快照。

## 验收

- 两个 agent-worker 副本处理同一会话时严格串行，进程取消/租约过期后可恢复。
- API 重启后插件目录仍可见，鉴权失败限流无法通过切换 API 副本绕过。
- gateway、agent-worker、maintenance 的关键 span 可导出且与 envelope trace_id 对齐；
  开启开关后 `/metrics` 返回 Prometheus 文本。
- 控制台可查看并重放单条死信、向真实会话发消息、通过会话选择器配置 proactive。
- 一条 CLI 命令可导出可迁移快照；重复 import 不产生重复 profile、记忆或技能；快照中
  不出现任何运行时密钥。
