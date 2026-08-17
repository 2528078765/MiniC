# MiniC

MiniC is a local-first DeepAgent: you ask questions in your local workspace and it helps you complete real local tasks through knowledge-base retrieval, long-term memory, file/Git/command tools, MCP servers, and sub-agents. All data stays on your own machine, and the core service listens only on `127.0.0.1`.

MiniC is primarily a CLI: type `minic` in any directory to start a conversation. The default chat model is DeepSeek, and the default embedding model is Alibaba Cloud Bailian `text-embedding-v3`.

> 中文说明见 [README.md](README.md)。

## Features

- **RAG knowledge-base Q&A**: ingest Markdown knowledge bases, hybrid retrieval with Chroma vectors + BM25 keyword search (0.55 / 0.45 weighting), automatic query rewriting, answers with `file path + section` sources; incremental ingestion with `metadata.json` as the single source of truth.
- **Local tools with approval**: built-in Read / Write / Edit / TextSearch / Lint / Format / Bash / Git tools; write operations back up the file first and support rollback; sensitive operations prompt for inline approval.
- **Sessions & memory**: multi-session create / resume / compress / archive / delete with automatic backup before destructive actions; long-term memory split into global and project scopes — personal information is written to the global memory, project conventions to the project memory, with automatic deduplication and user-content priority.
- **MCP servers**: read `~/.minic/mcp/minic_mcp_settings.json`, inject external MCP tools into the tool registry as `server_name.tool_name`, with exponential-backoff reconnection on failure.
- **SKILLs**: scan user-level and project-level `skills/*/SKILL.md`, declare capabilities and tool allow-lists in frontmatter; when enabled, inject context and enforce `allowed-tools` at the tool layer — approval is a separate gate.
- **Sub-agents**: delegate sub-tasks via `DelegateToSubagent` or `POST /agents`, with isolated context, concurrency and timeout control, read-only long-term memory injection, and per-sub-task approval forwarding.
- **Crash recovery**: `intent` / `result` execution logs plus idempotency keys; on restart, unfinished runs are marked `interrupted` and never auto-replayed; user messages are persisted immediately upon receipt.
- **Middleware**: PII redaction, rate limiting, request logging, long-term memory injection, retry on model network errors only, and automatic summarization when context grows too long.

## Requirements

- Python 3.13 or later (developed on Python 3.14).
- Chat model defaults to DeepSeek (`deepseek-v4-flash`); embedding defaults to Alibaba Cloud Bailian (`text-embedding-v3`). Both require API keys.
- API keys go into the user-level config `~/.minic/minic.json` and **never enter the repository** (excluded by `.gitignore`). See the template at [minic.example.json](minic.example.json).
- CI and unit tests use mock models and mock embeddings, require no external services, and need no keys.

## Installation

```powershell
cd MiniC
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

## Global command install / uninstall

Run the global install once with the venv activated (idempotent; running it again adds nothing). After that, `minic` works from any window in any directory:

```powershell
cd MiniC
powershell -ExecutionPolicy Bypass -File .\scripts\install-global.ps1
```

- After installing, **fully close and reopen your terminal application** (the script broadcasts an environment change so new terminals pick it up).
- If a new window still cannot find `minic`, refresh PATH in the current terminal and retry:

  ```powershell
  $env:Path += ";C:\Users\25280\Desktop\AgentProject\MiniC\.venv\Scripts;C:\Users\25280\.minic\bin"
  ```

To uninstall the global command:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-global.ps1 -Uninstall
```

When you run `minic` from any directory: if the core is not running it starts automatically (with its data directory falling back to a writable location), and when you exit it shuts down the core it started; if the core is already running it simply reuses it.

## Usage

Before first use, put your API keys into the user-level config `~/.minic/minic.json` (copy [minic.example.json](minic.example.json) and fill in the keys):

```json
{
  "model": { "api_key": "sk-deepseek-xxx" },
  "embedding": { "api_key": "sk-dashscope-xxx" }
}
```

Start a conversation directly (auto-starts/reuses the core):

```powershell
minic
minic --workspace D:\your-workspace
```

Start the core service explicitly:

```powershell
minic serve
```

Check the connection:

```powershell
minic status
```

Ingest and query a knowledge base:

```powershell
minic ingest D:\knowledge\my-notes
minic query "What is the MiniC architecture" --top-k 10
```

Startup auto-ingest: configure one or more knowledge-base paths (a file or a directory each; default empty array = disabled) in `rag.knowledge_base_paths` in `minic.json`. Each core startup incrementally ingests these paths in the background (unchanged files skipped, new/changed files re-ingested), without blocking startup; per-path results are appended to `<project>/.minic/logs/auto_ingest.jsonl`.

Dual-store RAG (G14): RAG is split into a global store and a project store, each with its own data directory and knowledge-base paths — the global store defaults to `~/.minic/rag-data` and the project store to `<project>/.minic/rag-data` (both can be changed with `rag.data_dir`), each configurable via `rag.knowledge_base_paths`. Startup auto-ingest is graded: global paths go to the global store, project paths to the project store; manual `/rag/ingest` and the IngestDirectory tool route by path ownership (inside the project `knowledge_base_paths` -> project store, otherwise -> global store). Retrieval defaults to merged dual query: query both stores, merge and deduplicate (same document prefers the project one), each result carries a `scope: "project"|"global"` marker, and the other store tops up when one store yields fewer results. Embedding is global-only config (both stores share the same embedding provider, so the vector space stays consistent for merged retrieval); model stays project-first merged. Existing data is not migrated automatically and remains in the original global store.

Conversation summary ingest: after setting `rag.default_directory` (default knowledge base directory), the agent can "write a Markdown summary to the knowledge base directory with Write, then call the IngestDirectory tool to incrementally ingest it" during a chat. IngestDirectory interrupts for approval (allow once / always allow / deny); after a deny the tool returns "用户拒绝入库" (user refused the ingest). `sandbox.allowed_write_dirs` can configure one or more out-of-workspace directories where Write/Edit is allowed (it defaults to include `rag.default_directory`; approval still applies).

Interactive chat (`--path` auto-ingests an un-ingested folder first):

```powershell
minic chat --path D:\knowledge\my-notes
minic chat
```

Session management:

```powershell
minic threads
minic resume <thread_id>
minic compress <thread_id>
minic archive <thread_id>
minic unarchive <thread_id>
minic delete <thread_id>
```

Long-term memory:

```powershell
minic memory get --scope merged --workspace .
minic memory set "preference: Chinese" --scope project --workspace .
minic memory set "# topic`nnew content" --scope project --mode merge --workspace .
```

Tools and approval:

```powershell
minic tool Read '{"path":"README.md"}'
minic tool Write '{"path":"notes.txt","content":"new content"}'
minic tool Bash '{"command":"echo hi"}'
minic approve <thread_id> <approval_id> allow_once
```

MCP / SKILL / sub-agent:

```powershell
minic mcp
minic skills list
minic skills enable <skill-name> --confirm
minic agent run "Count the total lines of all .py files in the current directory"
minic agent list
```

## Sessions and commands

The following commands are available inside `minic chat` / `minic` (all also accept Chinese aliases):

| Command | Description |
| --- | --- |
| `/help`, `/` , `?` | Show command list / shortcut help |
| `/new`, `/clear` | Archive the current session and start a new one (with backup) |
| `/resume` | Session-list panel; restore by number or thread_id |
| `/compress` | Compress the current session; backs up first, rollback supported |
| `/memory` | Long-term memory panel (global / project / merged) |
| `/rag` | RAG status panel (documents / chunks / embedding model / last ingest) |
| `/settings` | Show current model and embedding configuration |
| `/skills`, `/mcp`, `/agent` | SKILL / MCP server / sub-agent status panels |
| `/quit` | Exit the session |

## Tools and approval

Built-in tool registry (invoked automatically by the model in conversation, or manually via `minic tool`):

| Tool | Description | Permission |
| --- | --- | --- |
| `Read` / `ReadMemory` / `TextSearch` / `Lint` | read file / read memory / text search / syntax check | read-only |
| `GitStatus` / `GitDiff` / `GitLog` | Git status / diff / log | read-only |
| `Write` / `Edit` / `Format` | write file / incremental edit / format | write |
| `GitCommit` / `GitBranch` | commit changes / create branch | write |
| `Bash` | run terminal commands | exec (full permission) |
| `DelegateToSubagent` | delegate a sub-task to a sub-agent | exec |

Approval rules:

- Read-only tools inside the workspace are **auto-approved**; reads outside the workspace require confirmation.
- Write operations require approval, supporting four decisions:
  - `allow_once`: approved for this invocation only; not persisted.
  - `allow_session`: approved for the current session; not persisted.
  - `allow_always`: persisted to `permissions.json`.
  - `deny`: reject execution; in the same scope, `deny` takes priority over `allow_always`.
- **Bash always requires manual approval**; the approval menu only offers `allow_once` / `deny`, and submitting `allow_always` / `allow_session` returns a validation error.
- Every write operation and `Format` backs up the target file to `<project>/.minic/backups/` before execution and supports rollback.
- API keys, phone numbers, and ID numbers in tool output and logs are redacted by PII filtering.

## MCP servers

MCP server configuration lives in the user-level `~/.minic/mcp/minic_mcp_settings.json`:

```json
{
  "mcpServers": {
    "roymcp": {
      "transport": "streamable-http",
      "url": "http://127.0.0.1:8000/mcp",
      "timeout": 60,
      "disabled": false,
      "autoApprove": [],
      "headers": {}
    },
    "local-tool": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@some/local-mcp-server"],
      "env": {},
      "timeout": 60,
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

- Tools of connected servers are registered into the tool registry as `server_name.tool_name`; both `/tools/run` and in-conversation tool calls can execute them.
- Servers with `disabled: true` are not connected; on connection failure the client retries with 0.5s / 1s / 2s exponential backoff and marks the server unavailable after the limit, with manual reconnect available.
- Tools listed in `autoApprove` skip approval (wildcards such as `server.*` and `*` are supported); all other MCP tools go through the normal approval flow.
- APIs: `GET /mcp` for server status; `POST /mcp/{name}/connect` for manual reconnect. The `/mcp` panel in the CLI and the `minic mcp` command show the status.
- The `headers` field may contain keys directly, but they are stored in plaintext in the config file — **do not commit config files containing keys to the repository**; secure storage via keyring is planned for a later release.

## SKILLs

SKILLs live in the user-level `~/.minic/skills` and the project-level `<project>/.minic/skills`, laid out as `skills/<skill-name>/SKILL.md`:

```markdown
---
name: guard
description: Project guard skill that reminds before sensitive operations
when_to_use: file operations inside the project, before commits
allowed-tools:
  - Read
  - Write
---
Body: detailed skill instructions...
```

- On startup both levels are scanned; the frontmatter fields `name` / `description` / `when_to_use` / `allowed-tools` are parsed. Files without frontmatter or without a `name` are skipped.
- On name conflict the project-level entry takes priority, and the first enable requires confirmation (`POST /skills/{name}/enable` returns 409; confirm with `{"confirm": true}`).
- Enabled state is persisted in `<project>/.minic/skills_state.json`.
- When enabled and `when_to_use` matches, the SKILL name and description are injected into the model context; `allowed-tools` is enforced by the tool-call layer (tools outside the allow-list are rejected outright), and **approval is a separate gate — both must pass**.
- APIs: `GET /skills`, `POST /skills/{name}/enable`, `POST /skills/{name}/disable`. The `/skills` panel in the CLI and the `minic skills` sub-command manage them.

## Sub-agents

- Sub-tasks are delegated via the built-in `DelegateToSubagent` tool or `POST /agents`.
- Every sub-task has its own `subagent_id` and message context, fully isolated from the main session (it neither reads nor writes the main session's short-term memory).
- Long-term memory is injected read-only; sub-tasks never trigger memory-extraction writes.
- Concurrency is limited by `subagent.max_concurrent` (default 3); timeouts are governed by `subagent.timeout_seconds` (default 120s).
- Tool calls inside a sub-task go through the existing approval flow; the `approval_requested` event carries a `subagent_id` field (no new event names).
- APIs: `POST /agents` (synchronous execution), `GET /agents` (recent sub-tasks), `GET /agents/{subagent_id}` (single sub-task status). The `/agent` panel and `minic agent run/list` are available in the CLI.

## Crash recovery

- Tool execution uses `intent` / `result` logs (`<project>/.minic/logs/tool_execution.jsonl`): an `intent` is written before the call and a `result` after completion; `idempotency_key` is a normalized hash of `thread_id + tool + args`.
- On core restart, `intent` entries without a matching `result` are marked `interrupted` and never auto-recovered.
- When a `run_id + tool_call_id` already has a result, it is simply replayed without re-executing the tool.
- User messages are persisted immediately upon receipt; the GUI/CLI detect a core restart through `/health`'s `pid` / `started_at`.
- SSE events of `/chat/stream` are appended to `<project>/.minic/logs/sse_events.jsonl` by `id=run_id:seq` (args/token PII is redacted first); events are kept for 24 hours after the run ends and cleaned up on the next startup. After a disconnect you can replay / resume with `GET /chat/stream/{run_id}/events` and cancel the stream with `POST /chat/stream/{run_id}/cancel`. After a core restart, unfinished runs are marked `interrupted` and never auto-recovered.
- The super graph mounts LangGraph's in-memory `MemorySaver` checkpointer: graph state is resumable within the process lifetime, but it is only runtime-recoverable state — it can be rebuilt from the message JSON and tool execution logs and is not a source of truth. Approval interruption still uses the existing in-memory `asyncio.Event` mechanism.

## HTTP API

The core service listens only on `127.0.0.1`. Every endpoint except `/health` requires `Authorization: Bearer <token>`; the token is generated at core startup and written to `~/.minic/runtime.json`. Errors follow a uniform shape: `{"error": {"code", "message", "detail"}}`.

| Method | Path | Description |
| --- | --- | --- |
| GET | `/health` | Health check (no auth), returns pid / started_at / version |
| POST | `/chat/stream` | Send a message; SSE streaming response |
| GET | `/chat/stream/{run_id}/events` | Replay the run's SSE events (`last_event_id` to resume; forwards live until `done` while the run is running) |
| POST | `/chat/stream/{run_id}/cancel` | Cancel a running stream (closes with `message_end`/`done` set to `cancelled`) |
| GET | `/threads` | Session list for the workspace (archive filter, paging) |
| POST | `/threads/{id}/resume` | Resume a session and return its messages |
| POST | `/threads/{id}/compress` | Compress a session (backs up first) |
| POST | `/threads/{id}/archive` | Archive a session (backs up first) |
| POST | `/threads/{id}/unarchive` | Unarchive a session |
| DELETE | `/threads/{id}` | Delete a session (backs up first) |
| POST | `/threads/{id}/approve` | Submit an approval decision (allow_once / allow_session / allow_always / deny) |
| GET | `/memory` | Read long-term memory (scope=global / project / merged) |
| POST | `/memory` | Write long-term memory (replace / merge; version conflict returns 409) |
| POST | `/rag/ingest` | Ingest a file or folder (routes to project/global store by path ownership) |
| GET | `/rag/query` | Hybrid retrieval (q / top_k / source / scope=both\|global\|project; default both = merged dual query) |
| GET | `/rag/status` | Ingestion and index status (scope=both returns combined totals + per-store detail) |
| GET | `/rag/documents` | List ingested documents (scope / source filter, cursor/page_size pagination) |
| DELETE | `/rag/documents/{doc_id}` | Delete an ingested document (default scope project-first, global fallback) |
| POST | `/tools/run` | Execute a tool; SSE events tool_call / approval_requested / approval_result / tool_result / done |
| GET | `/permissions` | List persisted permissions (scope=global / project) |
| DELETE | `/permissions/{id}` | Revoke a persisted permission |
| GET | `/settings` | Read merged settings (API keys excluded) |
| PUT | `/settings` | Partially update and persist settings to minic.json (nested merge, array replace, rejects api_key) |
| GET | `/backups` | Backup list (session / file) |
| POST | `/backups/{id}/restore` | Restore a backup (session rollback / file to original path) |
| GET | `/mcp` | MCP server status list |
| POST | `/mcp/{name}/connect` | Manually reconnect an MCP server |
| GET | `/skills` | SKILL list (with enabled / scope / conflict flags) |
| POST | `/skills/{name}/enable` | Enable a SKILL (requires confirm on conflict) |
| POST | `/skills/{name}/disable` | Disable a SKILL |
| POST | `/agents` | Synchronously run a sub-agent task |
| GET | `/agents` | Recent sub-task list |
| GET | `/agents/{subagent_id}` | Single sub-task status |

The SSE event sequence of `POST /chat/stream` is: `message_start` → `token` → `tool_call` →（`approval_requested` → `approval_result`）→ `tool_result` → `message_end` → `done`; the `sources` field of the `done` event drives the `file path + section` citations.

> Note: `PUT /settings` performs a partial update and persists to the target `minic.json`; most settings in the current process take effect on the next request.

## Configuration

Configuration is merged with project-level settings taking priority over `~/.minic/minic.json`. The full field list is in [minic.example.json](minic.example.json); copy it to `~/.minic/minic.json` and fill in the API keys:

```json
{
  "model": {
    "provider": "deepseek",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-v4-flash",
    "temperature": 0.7
  },
  "embedding": {
    "provider": "dashscope",
    "model": "text-embedding-v3",
    "base_url": "https://dashscope.aliyuncs.com"
  },
  "rag": {
    "chunk_size": 500,
    "chunk_overlap": 100,
    "top_k": 5,
    "bm25_weight": 0.45,
    "vector_weight": 0.55,
    "auto_ingest_paths": [],
    "default_directory": null,
    "data_dir": null,
    "knowledge_base_paths": []
  },
  "approval": {
    "workspace_read_auto_approve": true,
    "workspace_write_auto_approve": false,
    "expiry_seconds": 300
  },
  "sandbox": {
    "command_timeout": 60,
    "model_api_whitelist": [
      "http://127.0.0.1:11434",
      "api.deepseek.com",
      "dashscope.aliyuncs.com"
    ],
    "default_level": "workspace-write",
    "summarize_threshold_chars": 12000,
    "high_risk_patterns": [
      "rm -rf /",
      "rm -rf ~",
      "format c:",
      "del /f /s /q c:",
      "shutdown /",
      "taskkill /f /im"
    ],
    "allowed_write_dirs": []
  },
  "memory": {
    "long_term_inject_threshold_tokens": 4000
  },
  "rate_limit": {
    "max_requests": 120,
    "window_seconds": 60
  },
  "subagent": {
    "max_concurrent": 3,
    "timeout_seconds": 120
  }
}
```

Data directory conventions:

- User-level `~/.minic/`: `minic.json` (config), `runtime.json` (port and access token), `rag-data/` (global RAG index and `metadata.json`), `memory/minic.md` (global long-term memory), `permissions.json` (global permissions), `skills/` (global SKILLs), `mcp/minic_mcp_settings.json` (MCP config).
- Project-level `<project>/.minic/`: `minic.json` (project config), `rag-data/` (project RAG index and `metadata.json`), `memory/minic.md` (project long-term memory), `memory/short_memory/` (session short-term memory), `backups/` (backups), `logs/` (`tool_execution.jsonl`, `requests.jsonl`, `auto_ingest.jsonl`), `permissions.json` (project permissions), `skills_state.json` (SKILL switches).

## Testing

```powershell
cd MiniC
.\.venv\Scripts\python.exe -m pytest -q
```

Tests cover the core service, RAG, sessions, long-term memory, tools and approval, crash recovery, middleware, MCP, SKILLs, sub-agents, CLI UI, and global installation. They use mock models, mock embeddings, temporary directories, and fixtures — **no external API keys are required**, so the suite runs right after `git clone`.

## Open source

MiniC is licensed under the [MIT License](LICENSE).

- Contribution guidelines: [CONTRIBUTING.md](CONTRIBUTING.md).
- Security notes: [SECURITY.md](SECURITY.md).
- Example knowledge base: [examples/](examples/README.md).
