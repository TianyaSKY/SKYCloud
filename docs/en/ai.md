# AI Capabilities

This document covers the RAG retrieval pipeline, the dual-mode AI assistant, the all-in-one workspace and MCP client connectivity.

## RAG Retrieval Augmentation

![RAG Pipeline](../../images/rag_pipeline.png)

**Offline**: File upload -> RabbitMQ -> Worker generates descriptions + 1024-dimensional embeddings -> pgvector

**Online**: 6-dimensional keyword rewriting -> multi-query generation -> parallel vector recall -> RRF fusion ->
reranking -> SSE streaming output

Chunk retrieval scopes both the chunk and its file to the workspace, so historical inconsistencies cannot pull another workspace's content into an answer.

## Dual-Mode AI Assistant

The assistant offers two conversation engines that never share state:

- **Quick mode**: reads the current workspace's material, reuses the RAG retrieval chain and starts no Runtime - suitable for lookups, summaries and document Q&A.
- **Expert mode**: open to `editor` / `admin` only; starts a per-user, per-workspace OpenCode Runtime with multi-step tool calls, command permission prompts, cancellation, file diffs and session recovery.

The backend entry point is `/api/assistant`; conversations, messages and runs persist in `assistant_conversations`, `assistant_messages` and `assistant_runs`. The expert Runtime uses short-lived, revocable MCP tokens and OpenCode Basic Auth; ordinary REST dependencies reject Runtime tokens, and Runtime credentials can only reach the dedicated MCP service.

Progressive rollout is controlled by `ASSISTANT_V2_ENABLED`, `ASSISTANT_EXPERT_ENABLED`, `ASSISTANT_PERMISSION_UI_ENABLED` and `ASSISTANT_RUNTIME_LOCK_TTL`. Production deployments should set a dedicated `OPENCODE_SERVER_SECRET` and use a version- or digest-pinned `OPENCODE_IMAGE`.

Switching workspaces or unmounting the assistant closes the old stream, and late responses from an old conversation cannot modify the current one. After a connection drop the frontend re-queries the server for active runs and pauses sending when the status query fails; after an initialization failure the assistant can be closed and reopened. Cancellation acknowledges the request, then the frontend keeps waiting until the run actually ends; a failed cancel keeps the original run. Permission answers apply only to the current request, no permanent allow rules are stored, and a cancelled run accepts no further permission replies.

Permission waiting and crash recovery rely on the executing process renewing its run heartbeat: another process recovering an interrupted run locks the same record and reads the latest heartbeat instead of mistaking a long task for a restart, with a default abandoned-run grace period of 45 seconds (`ASSISTANT_ABANDONED_RUN_GRACE_SECONDS`, minimum 30). A recovered run without a local SSE connection syncs status and history serially every 3 seconds and stops polling once it confirms a terminal state. Pending expert permissions persist only the permission ID, title and tool description (titles up to 2000 characters, tool descriptions up to 4000), never raw provider events; the active-run endpoint returns `pending_permission` only while that permission is still awaited and cancellation has not been requested, and the frontend matches conversation, message and permission ID when restoring, leaving only the cancel entry when information is missing.

Assistant streams use in-memory Bearer credentials and refresh only after a 401, retrying once without replaying what was already received. The parser handles split UTF-8 sequences, multi-line events and CRLF, bounds unfinished events, and releases the reader on exit or failure.

## All-In-One Workspace

Each workspace runs in an isolated Docker container. Through the MCP protocol, AI can autonomously execute tasks inside
the sandbox and interact with the cloud drive.

## MCP Access

The MCP Server runs in a standalone container. Production exposes it through the frontend at **`/mcp`**; the development override publishes loopback port **5001**. Open the avatar menu → **MCP Token** to explicitly issue a credential for external clients. The full token is displayed only after issuance and is cleared when the dialog closes; it cannot be recovered. Workspaces use separate short-lived Runtime credentials, provisioned automatically on create/start/restart.

[← Back to README](../../README_EN.md)
