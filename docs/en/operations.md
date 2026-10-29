# Operations: Quotas, Jobs and Conventions

This document covers upload and quota limits, background jobs and cleanup, time and time-zone handling, and the API and logging conventions.

## Uploads and Quotas

Upload size limits default to 256 MiB per file, 5 MiB per avatar, 512 MiB per request and 64 MiB per part. The backend counts actual request and file bytes, including bodies without Content-Length, returns 413 on overflow and removes partial temporary files. Configure positive `UPLOAD_*` values in `.env.example`; increasing the request limit also requires updating `client_max_body_size` in `frontend/nginx.conf`.

Upload quotas default to 10 GiB per user and 50 GiB per workspace, counting logical files (including instant-upload copies) plus unexpired reservations. Daily UTC admission allows 200 attempts and 5 GiB, charged when reservation succeeds; cancellation and failures do not refund the daily allowance. A user may hold 8 active uploads and a workspace 32. Reservations expire after one hour and the Worker cleans them every minute. File insertion and reservation consumption commit together, and multipart sessions are accessible only to their creator. Each API process accepts up to 8 concurrent upload requests; Nginx limits each client IP to 8 API connections and streams request bodies.

Temporary uploads use a dedicated `.staging` directory with process leases. The Worker preserves live writers and reclaims old directories after process crashes. Object keys are journaled before MinIO writes; failed or uncertain deletion keeps capacity reserved and retries on the next cleanup, so storage faults cannot bypass the quota. Interrupted merges preserve parts and a valid reservation for retry; historical files count toward capacity without retroactive daily charges.

MCP inline uploads are capped at 8 MiB, with encoded lengths checked before Base64 decoding. MCP JSON bodies default to 32 MiB and each process accepts 4 concurrent POST requests via `MCP_MAX_REQUEST_SIZE` and `MCP_REQUEST_CONCURRENCY`; increasing the body cap also requires updating Nginx `/mcp` `client_max_body_size`.

The frontend reads files in 2 MiB chunks with incremental SHA-256, pins each upload to the account, workspace and directory chosen at creation, and runs at most 3 uploads concurrently. Only transient network or server errors retry a part, and progress reaches 100% only after metadata is committed. Resume identifiers cover the file content and its context, the browser stores identifiers only, and an expired session (409) restarts initialization. Changing the directory, workspace, session or write permission discards an open file-picker selection.

## Background Jobs and Cleanup

RabbitMQ publishing uses durable messages and publisher confirms, and consumers acknowledge manually after persisting results; the semantic is at-least-once, so tasks may run twice. Invalid messages and failures that cannot persist results are copied to the durable `failed_task_queue`, which stores only the source queue, the raw message Base64 and the exception type; an administrator verifies the cause before re-publishing the original payload. Re-published organization tasks keep their original task ID, and terminal tasks are skipped.

File indexing uses a database outbox: the file record (single, batch, multipart and failed instant-upload clones) commits together with a pending task. A Worker maintenance thread checks due tasks every second, requires a RabbitMQ confirm before marking a task sent, and retries with backoff from 2 to 300 seconds, coordinating multiple Workers with row locks and SKIP LOCKED. Uploads no longer connect to RabbitMQ directly, so the Worker must run.

AI organization tasks also persist in the database: the API checks current write permission inside a transaction, at most one queued/running task exists per workspace, execution holds a workspace-level advisory lock and rotates credentials per execution so an old executor cannot keep modifying data, and terminal state commits together with the inbox notification; duplicate messages for finished tasks are skipped. Office conversions required by organization run in their own process group with a 120-second timeout that terminates the whole group and logs only a fixed failure type.

Physical object and container cleanup is asynchronous and performed by the Worker: deleting files, workspaces or accounts commits cleanup intents in the same transaction as the business data (PostgreSQL triggers plus migrations `0011`-`0014`), and the Worker performs idempotent deletes after confirming that no references remain, retrying with 2-300 second backoff. Capacity is released only after the physical object is deleted; failed or uncertain deletions stay reserved for the next retry.

## Time and Time Zones

Time API output carries an explicit UTC offset (`+00:00`); business clocks, token deadlines, assistant recovery, share expiry, runtime activity, checkpoints and usage records are stored in UTC, and shares expire exactly at their deadline. The frontend formats public dates and conversation lists in `Asia/Shanghai` and labels notification times explicitly; daily upload quotas use UTC days while usage views group by Beijing time, independent of the database session time zone.

Historical timezone-naive columns are converted to PostgreSQL TIMESTAMPTZ by migration `0009_utc_timestamps`: original Beijing columns use `Asia/Shanghai`, original upload reservations use UTC, and already-aware columns keep their instant. The migration takes table locks and may rewrite data, so stop old API/Worker/MCP/model-gateway processes, back up, and run it in a maintenance window; any failure rolls back atomically, and downgrades that drop timezone semantics are rejected.

## API and Logging Conventions

REST success responses are uniformly `{"success":true,"data":…}` and error responses are `{"success":false,"error":{"code":"…","message":"…"}}` with stable domain codes. The auth/user/workspace/file/folder/share/inbox/settings/usage/assistant JSON endpoints use that shape, while binary downloads, SSE streams, the Runtime UI proxy and the MCP/OpenAI protocols keep their own formats. Errors and logs record only fixed diagnostics, exception types and the necessary safety detail (such as field locations and types for 422), never submitted values, credentials, cache keys or raw exception text.

This shape change requires deploying frontend and backend together: the frontend strictly validates and unwraps the envelope, and an HTTP 200 without an envelope is not treated as normal business data.

[← Back to README](../../README_EN.md)
