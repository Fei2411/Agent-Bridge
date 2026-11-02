# Agent Bridge V2：TypeScript + Node.js 中心运行时

当前实现为可执行 alpha：Hub 管理 Worker、任务、会话和通信，MCP 宿主只是
连接到 Hub 的客户端。真实产品 CLI 尚待环境验证；协议夹具测试已经覆盖主要
生命周期。Python 发布入口与数据保持独立，兼容差异见 [MIGRATION.md](MIGRATION.md)。

## 结构

一个 npm 包、一个常驻进程、一份本机 SQLite WAL 数据库。HTTP 接收命令，SSE
补收事件，stdio MCP 代理供宿主使用。没有 Redis、消息中间件、微服务或通用
插件框架。管理界面、托盘与远程节点不属于本轮运行时迁移。

| 模块 | 职责 |
| --- | --- |
| `contracts.ts`、`config.ts` | 领域对象、输入校验、TOML 覆盖与环境规则 |
| `store.ts` | SQLite 迁移、事务、去重、任务队列、事件与恢复 |
| `runtime.ts` | 会话串行调度、跨会话并发、取消、依赖等待、空闲卸载 |
| `adapters/` | ACP、Codex exec、Antigravity 的启动、选择、续接与结果转换 |
| `process.ts`、`worker-host.ts` | 命令发现、环境、Windows shim、进程树清理 |
| `api.ts`、`hub.ts` | 身份与项目约束、HTTP、SSE、单实例锁、生命周期 |
| `client.ts`、`tools.ts`、`mcp.ts` | 动态发现、工具契约、固定代理身份 |
| `daemon.ts`、`cli.ts` | 并发自动启动、后台/前台运行、命令行操作 |
| `workspace.ts` | Git 文件快照与变更归集 |

## 对象和交付语义

- Agent 是稳定的通信身份；Session 是 Hub 管理的原生会话，具有自己的 Agent。
  注册不等于在线。外部桌面会话与 Hub 管理的 Worker 能力不同。
- Task 是持久化执行请求，状态为 queued、running、completed、failed、cancelled
  或 interrupted。每个会话只有一个活动 turn，不同会话可并发。
- Message 记录发送者、接收者、线程和消费确认。发给托管会话且 `wake=true`
  时，消息、对应任务和事件在一个事务里提交；忙碌会话排队，不偷换为实时插话。
- 消息以 senderId + requestId 去重，任务以 requesterId + requestId 去重。
  相同键的冲突内容拒绝；幂等状态跨重启持久化。
- 读取不等于消费，消费不等于完成。外部身份显式确认收件，托管任务成功后
  确认其源消息。回复需要显式发送，避免结果自动回流造成无限对话。
- 事件序号在 Hub 内单调递增。客户端保存已消费游标，通过查询或 SSE 重连补收；
  不承诺外部模型执行恰好一次。

## 生命周期与委派

MCP 代理退出只断开自身连接。Hub 独立后台运行，所有代理共享同一 profile 的
地址发现和独占锁。每个 Worker 的守护进程观察 Hub IPC；Hub 崩溃也清理它负责的
进程树。Hub 恢复将未完成任务标记为 interrupted，不重放可能已修改文件的请求。

Worker 获取绑定身份、项目的 MCP 凭证与同一 Hub 地址，能发现同伴、发消息、
委派任务并查询结果。无法创建另一个中心，也不会被一刀切禁止委派。等待自身
会话或已知循环依赖会返回错误；消息可以异步发送。协议不支持唤醒外部桌面模型
时如实保留收件箱语义。

模型选择依据运行时返回的选项，切换模型后重新应用 effort；恢复时不把历史重放
混入本轮结果。Codex、Antigravity 各用其原生协议。原生会话不能恢复时返回明确
警告。取消终止执行，不撤销已发生的文件编辑。

## 约束与实现取舍

默认没有任务时长、并发量、消息大小、嵌套层数上限。页大小只是读取参数；完整
数据持久化。可选超时仅来自用户配置，退出宽限用于清理进程。Windows、macOS、
Linux 统一通过 Node 24 运行；Windows 命令 shim 使用 argv 执行，不拼 shell 字符串。

范围凭证约束 API 身份和项目，不能替代操作系统沙箱。运行环境属于同一受信任 OS
用户，服务只监听 loopback，不接受浏览器 Origin 请求。数据留在本机，不共享 SQLite
到网络目录。凭证保存在私有 profile 中，日志对已知环境凭证做脱敏。

同步 SQLite 保持短事务；输出以收到的协议块递增存储。事件当前不自动清理。是否
需要存储线程或批量写入，留给实际性能测量决定。额度查询、Python 历史导入和
管理界面不伪装为已完成，详见迁移说明及验证门槛。
