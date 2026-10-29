# AI 能力

本文档覆盖 SKYCloud 的 RAG 检索增强、双模式 AI 助手、全能工作区，以及面向客户端的 MCP 接入方式。

## RAG 检索增强

![RAG Pipeline](../images/rag_pipeline.png)

**离线**：文件上传 → RabbitMQ → Worker 生成描述 + 1024 维 Embedding → pgvector

**在线**：6 维关键词改写 → 多查询生成 → 并行向量召回 → RRF 融合 → Rerank → SSE 流式输出

分块检索同时限定分块与所属文件的工作空间，历史不一致记录不会把其他空间的内容带入回答。

## 双模式 AI 助手

助手入口提供两套互不污染的会话引擎：

- **快速模式**：只读当前工作空间资料，复用 RAG 检索链，不启动 Runtime，适合查找、总结和文档问答。
- **专家模式**：仅向 `editor` / `admin` 开放，按用户和工作空间启动独立 OpenCode Runtime，支持多步骤工具调用、命令权限确认、取消、文件 Diff 和会话恢复。

后端统一入口为 `/api/assistant`，会话、消息和 Run 持久化在 `assistant_conversations`、`assistant_messages`、`assistant_runs` 三张表中。专家 Runtime 使用短期、可撤销的 MCP Token 和 OpenCode Basic Auth；普通 REST 依赖拒绝 Runtime Token，Runtime 凭证只可访问专用 MCP 服务。

默认通过环境变量控制渐进式发布：`ASSISTANT_V2_ENABLED`、`ASSISTANT_EXPERT_ENABLED`、`ASSISTANT_PERMISSION_UI_ENABLED`、`ASSISTANT_RUNTIME_LOCK_TTL`。生产部署应设置独立的 `OPENCODE_SERVER_SECRET`，并使用固定版本或 digest 的 `OPENCODE_IMAGE`。

切换工作空间或卸载助手会关闭旧流，旧会话请求的响应不会修改当前会话。连接中断时会重新查询服务器上的活动任务，状态查询失败时暂停发送；初始化失败后可关闭并重新打开助手重试。取消接口确认收到请求后，前端继续等待任务实际结束；取消失败保留原任务。权限确认仅对当前请求生效，不保存永久允许规则，取消已确认的任务不再接受新的权限答复。

授权等待与中断恢复依赖执行进程持续续写运行心跳：其他进程恢复中断任务时锁定同一条记录并读取最新心跳，避免把长任务误判为服务重启，默认失联宽限期 45 秒（`ASSISTANT_ABANDONED_RUN_GRACE_SECONDS` 最小 30 秒）。恢复出的运行在没有本地 SSE 连接时每 3 秒串行同步状态与历史，确认终态后停止轮询。专家待授权运行只持久化授权 ID、标题与工具说明（标题最多 2000 字符、工具说明最多 4000 字符），不保存原始提供方事件；活动运行接口仅在仍等待该授权且未请求取消时返回 `pending_permission`，前端恢复时核对会话、消息和授权 ID，信息缺失时只保留取消入口。

助手流使用内存 Bearer 凭据；仅在请求被 401 拒绝时续期并重试一次，不重放已经接收的流。解析器支持跨块 UTF-8、多行事件和 CRLF，限制未结束事件大小，并在退出或处理失败时释放读取器。

## 全能工作区

每个工作区运行在独立 Docker 容器中，通过 MCP 协议让 AI 在沙箱内自主执行任务并与云盘交互。

## MCP 接入

MCP Server 独立容器运行，生产部署通过前端同源 **`/mcp`** 访问；开发覆盖可开放本机端口 **5001**。登录后点击右上角头像 → **MCP Token**，显式生成外部客户端凭据。完整 Token 仅在生成时显示，关闭窗口后无法恢复，只能重新生成。凭据的哈希存储、撤销与审计边界见[安全模型](./security.md)。

工作区在创建/启动/重启时会自动注入独立的短期 Runtime Token，无需手动「连接 MCP」。

[← 返回 README](../README.md)
