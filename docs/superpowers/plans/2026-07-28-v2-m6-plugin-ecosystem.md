# V2-M6 插件生态 2.0 实施计划

## 目标

在现有 broker、隔离 `plugin-runner`、工具目录和 operator 审计链路上完成六组能力：Markdown 技能、manifest 配置与定时任务、单插件热重载、服务注入与版本协商、插件脚手架与可信索引、陌生私聊配对。任何插件内容、技能正文、远程安装包和陌生私聊都按不可信输入处理。

## 参考实现与取舍

- OpenClaw skills（`openclaw/openclaw@61bd5af`）：提示词只注入技能名称、描述和位置，完整 `SKILL.md` 在需要时加载；MyBot 采用相同的渐进披露方式，但只实现路线图要求的 `name`、`description`、`trigger`，不引入宿主命令执行或环境变量注入。
- OpenClaw pairing（`openclaw/openclaw@61bd5af`）：未知私聊不进入 agent，短码有期限、每账号限制待审批数量、同一请求只发送一次提示；MyBot 使用 PostgreSQL 保存请求、批准和平台策略，并将群聊授权保持为独立边界。
- Koishi schema/HMR（`koishijs/koishi@fb6e2c0`）：schema 变化通知控制台；热重载按插件边界替换，失败保留旧状态。MyBot 不操作 Python 全局 import cache，而是让每个插件独占子进程，通过 kill/respawn 得到更清晰的隔离和回滚边界。
- nb-cli template（`nonebot/nb-cli@ad7a6bd`）：生成最小 manifest、配置、入口和测试骨架。MyBot 的脚手架固定生成 `plugin.json`、`src/<name>/plugin.py` 和 pytest，避免隐式修改主项目依赖。

## 安全边界

- `skills/` 与插件数据目录必须做 realpath containment；不跟随逃逸根目录的符号链接。
- registry 安装只接受索引中声明的 HTTPS 源，限制下载体积，拒绝 zip-slip、符号链接和 manifest 哈希不匹配；安装到版本目录后再原子更新受管索引。
- 服务目录首批固定为 `memory.search@1`、`llm.complete@1`、`kv.store@1`。握手精确协商主版本；服务调用验证 runner/plugin 身份、capability、分钟配额并写审计。
- 配对默认保持兼容的 `open` 策略；切换到 `paired` 后，未知私聊在创建会话、持久化消息和调用 LLM 之前被截断。`allowlist` 不通过配对批准扩大静态名单。
- 插件进程继续只连接内部 `plugin-control` 网络。enable/disable/reload 只影响目标子进程。

## 测试优先顺序

1. 合同：`PluginTaskSpec`、`requires` 服务版本、旧字符串 tasks 向后兼容、非法 schema/服务拒绝。
2. 技能：安全发现、frontmatter 校验、启停、编辑、目录提示词预算、`load_skill` 正文读取与 capability。
3. broker：服务握手拒载、服务身份/授权/配额/审计、配置定向通知、任务定向派发、runner 注销。
4. runner：单插件加载、配置与 task 回调、目标进程 reload 不影响其他插件、指数退避和熔断、状态落盘。
5. 维护 worker：按 `interval_seconds` 计算到期任务并经 broker 派发；失败不阻塞下一任务。
6. 脚手架/索引：生成文件可被 pytest 收集；下载大小、HTTPS、manifest 哈希、zip-slip、版本目录和受管索引。
7. 配对：open/paired/allowlist；短码去歧义、过期、每账号上限、一次提示、批准后放行、群聊不受影响。
8. Operator API/Web：技能 CRUD/启停；插件配置表单与动作；索引安装；服务/加载错误；配对策略和审批。
9. 全量：迁移 upgrade/downgrade、pytest、ruff、pyright、Web test/build、Compose 配置和本地浏览器验收。

## 运行链路

1. Agent Worker 每回合扫描启用技能，只把受预算限制的目录块附加到 system prompt；`load_skill` 作为内建只读工具返回正文，工具结果自然进入该回合 token 统计。
2. Operator 保存插件配置后先按 manifest JSON Schema 子集校验，再写共享控制状态并通过 broker 向目标 runner 发送 `plugin.config.changed`。
3. Maintenance Worker 拉取 broker 任务目录，按任务间隔计算到期项，调用定向 dispatch；runner 仅执行所属插件声明的 task handler。
4. Plugin Supervisor 合并环境配置和可信安装索引，为每个插件启动一个子进程。源码或 generation 变化只重启目标插件；连续崩溃指数退避，达到阈值后熔断，人工 reload 才复位。
5. 子进程握手携带 manifest `requires`。broker 只在服务版本、capability grant 全部满足时注册插件，并给该 runner 绑定服务客户端。
6. Operator 安装插件时下载到临时目录，验证 archive 与 `plugin.json`，安装到 `<plugin-data>/installed/<id>/<version>`，最后更新 `managed.json`；supervisor 发现后加载。
7. Agent Worker 在任何会话/消息写入前执行私聊准入。paired 未知用户只创建或复用短期请求；仅新请求发布一次配对提示，不运行 agent。

## 验收

- 新建一个 `skills/daily-summary/SKILL.md` 后控制台可见，关闭后不进入目录，编辑后下一回合生效；模型可调用 `load_skill` 获取全文。
- 脚手架生成带配置和定时任务的插件；配置在控制台保存后插件收到更新；任务由 maintenance 派发；修改代码或点击 reload 后仅该插件 PID 改变。
- 缺少或版本不匹配的服务使插件拒载，控制台显示原因；合法服务调用可见审计且超过配额被拒绝。
- registry 安装只有哈希完全一致才成功，安装后 supervisor 自动发现；失败不会留下半安装状态。
- paired 策略下陌生 QQ/TG 私聊不创建 conversation、不写 inbound message、不消耗 LLM；同一有效请求只提示一次，控制台批准后正常进入 agent。
