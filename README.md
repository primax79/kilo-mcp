# Kilo MCP Server

**Kilo MCP Server** is a Model Context Protocol (MCP) implementation designed to bridge any orchestrating AI assistant (such as **Claude Code / Desktop**, **Kilo Code**, **Roo Code**, or any MCP client) with **KiloCode** (Kilo CLI).

It empowers the orchestrating AI to act as a **System Architect and Manager**, while delegating the heavy lifting of code implementation, repository exploration, and refactoring to Kilo, which acts as the **Executor**.

## 🌟 Why this exists?

- **The Architect / Orchestrator Role**: An orchestrating assistant (like Claude) excels at high-level reasoning, system design, architectural planning, orchestrating tasks, managing prompts, writing comprehensive specifications, and reviewing generated code. However, executing massive refactors across an entire codebase can be token-heavy and slow.
- **KiloCode's Role (The Executor)**: KiloCode is an incredibly fast, highly capable CLI agent built for execution (default model: Gemini 3.5 Flash — configurable, 500+ models supported). It is equipped with powerful tools for code generation, semantic search (RAG), Agent Manager, worktrees, and native workspace awareness.

By connecting them via MCP, the orchestrator can dynamically spin up Kilo instances in the background, assign them coding tasks or codebase exploration, and review the results—all while focusing purely on orchestration and architecture.

This is the **Kilo-specific binding** of a protocol-agnostic pattern —
the abstract phases (discovery, isolation, delegation, monitoring,
verification, closure) and the headless-worker behavioral contract this
server implements are documented tool-agnostically in
[`ai-architect-executor`](https://github.com/primax79/ai-architect-executor);
this repo is what makes that pattern concrete for Kilo Code over MCP.
General Claude Code/Kilo Code plugin & marketplace concepts (skills, agents,
`.claude-plugin/marketplace.json`, Kilo's Skill URLs) are covered in
[`agentic-coding-kit`'s docs](https://github.com/primax79/agentic-coding-kit/tree/main/docs)
rather than repeated here.

---

## ✨ Features

- **🧠 Specialized Roles**: The orchestrator writes the specs, manages execution flow, and verifies results. Kilo generates the actual code and explores the workspace.
- **🔍 Native RAG & Semantic Search**: The orchestrator can delegate semantic search over Kilo's codebase index to quickly find relevant code snippets without downloading the whole repository into context. The index is a standing resource for the assistant's *own* work — useful for exploration and Q&A even when nothing is delegated to `kilo_implement` — and the server's MCP `instructions` state this to every connected client, so the directive ships with the install.
- **🚀 Native Parallelism**: The orchestrator can emit parallel tool calls to spawn multiple Kilo subprocesses simultaneously across different branches or directories. Subprocesses are launched via `asyncio`, so concurrent tool calls run truly in parallel without blocking the event loop.
- **⏱️ Timeout Protection**: Every Kilo subprocess is bounded by a timeout (default 1800s, configurable via the `KILO_MCP_TIMEOUT` environment variable) so a stuck instance can never hang a tool call indefinitely.
- **💸 Configurable Cost Model & Delegation Policy**: Whether Kilo execution is free depends on the operator — some have provider API keys supplied at no charge, others pay per token. Set `[kilo] input_cost_per_mtok` / `output_cost_per_mtok` (default `0.0` = free) to your real Kilo rates: the built-in delegation policy automatically switches between a free-Kilo model (only orchestrator tokens matter) and a paid-Kilo model (Kilo's own agentic loop joins the comparison), and cost estimates include Kilo's execution cost. The `delegation_policy` setting still overrides the injected policy text entirely, so the orchestrator self-selects *when* delegating (short spec + review) beats doing the work inline; tune it without touching code.
- **📊 Metrics & Feedback Loop**: Every run is recorded to `metrics.jsonl` (duration, outcome parsed from Kilo's Final Report, generated-code volume, real delegation cost vs estimated inline cost). Defects found during review are logged via `kilo_log_issue` to `issues.jsonl`, and `kilo_metrics` aggregates everything — surfacing the exact cost breakdown (orchestrator spec+review vs. Kilo execution) so you can see whether delegation pays off and which defect categories the prompts must start guarding against.
- **🤖 Agent & Skill Discovery**: Exposes tools for the orchestrator to dynamically discover available Kilo agents (e.g., `code`, `explore`) and custom skills configured in your local or global `.kilo/` directories.
- **🛡️ Advanced Orchestration**: Support for git worktrees (`kilo_create_worktree`) to allow parallel feature implementations, advanced workspace checks (`kilo_workspace_status`), provider status checks (`kilo_auth_status`), and custom command execution (`kilo_run_command`). Included are skills (`kilo-mcp-headless-executor`, `kilo-mcp-conflict-resolver`, and `kilo-mcp-rag-explorer`) to improve parallel agent work, headless task completion, and fast semantic search.
- **🎯 Precise Context Injection**: The orchestrator can pass specific files as `focus_files`, ensuring Kilo reads the exact context needed before making changes.
- **⚙️ Dynamic Configuration**: Claude can inject environment variables into the Kilo subprocess at runtime (e.g. debugging flags) to shape Kilo's execution environment; the server itself is configured via a layered env-var/TOML/default scheme.
- **🛡️ Non-Interactive by Default**: Safely runs Kilo in background subprocesses without hanging on interactive CLI prompts.
- **📡 Non-Blocking Delegation with Live Monitoring & Intervention**: `kilo_implement` runs in the background by default — it returns immediately with a `task_id`, never blocking the conversation. `kilo_task_progress` polls Kilo's own live plan/todo list, commentary, and cost straight from its session database; `kilo_task_cancel` stops a task that's drifted off-spec; and `continue_session_id` lets you send corrective instructions into the *same* session instead of starting blind. How closely to watch a task scales with its complexity/risk — small tasks need only a final check, large ones are worth polling periodically.
- **🧟 Detached, Self-Healing Background Execution**: background tasks run as fully detached OS processes (`setsid`) — they survive a server restart or crash instead of dying with it. Any status-reading tool reconciles a stale `"running"` record against real PID liveness and the task's log file, so the outcome is always recoverable, even from a brand-new server instance. See [Reliability](#reliability-background-task-execution).
- **🗂️ Agent Manager Integration**: worktrees and sessions created via `kilo_create_worktree` / `kilo_implement(isolation='worktree')` are also registered (best-effort) in `.kilo/agent-manager.json`, so they can appear in the Kilo VS Code extension's Agent Manager UI without any manual JSON editing. See [Agent Manager Integration](#agent-manager-integration) for the caveats.

---

## 📦 Installation & Setup

### Requirements

- **Python 3.10+** (3.11+ recommended — the optional TOML config file needs stdlib `tomllib`)
- **Kilo CLI** installed and accessible in your system's PATH. On macOS the recommended install is Homebrew:

  ```bash
  brew install Kilo-Org/tap/kilo
  ```

  (other channels: `npm`, `pnpm`, `bun`, or the `curl` installer — see <https://kilo.ai>). Verify with `kilo --version`, and make sure you are logged in to at least one provider: `kilo auth list` (log in with `kilo auth login`).
- **An MCP client**: Claude Code, Claude Desktop, or any MCP-compatible client.
- **uv** (Recommended): A fast Python package installer and resolver. Install it via `curl -LsSf https://astral.sh/uv/install.sh | sh`

### Server Configuration

Settings resolve as **environment variable > config file > built-in default** — env vars keep the standard MCP pattern (the client's `env` block) working, while the TOML file is the comfortable place for everything else.

Config file search order:

1. `$KILO_MCP_CONFIG` (explicit path)
2. `kilo-mcp.toml` next to `server.py` (per-checkout, gitignored)
3. `~/.config/kilo-mcp/config.toml` (per-user)

Copy [kilo-mcp.example.toml](kilo-mcp.example.toml) to one of those locations and edit — it documents every key (`[kilo]` timeout, default model, Kilo token pricing, delegation policy; `[metrics]` data dir, Claude pricing, inline-cost heuristics) and the matching env var names. Config file support requires Python ≥ 3.11 (`tomllib`); on 3.10 only env vars and defaults apply.

### Kilo Configuration & Credentials (shared with the IDE extensions)

The Kilo CLI reads the **same configuration and credentials** as the Kilo VS Code / JetBrains extensions — there is nothing to set up twice:

- `~/.config/kilo/` — `kilo.jsonc` (settings, indexing/RAG, permissions)
- `~/.kilo/` (and legacy `~/.kilocode/`) — custom `agent/`, `skills/`, `command/` (this is where Kilo discovers global customizations)
- `~/.local/share/kilo/auth.json` — provider credentials (created by `kilo auth login`)

If you already use the Kilo extension, the CLI (and therefore this MCP server) will work with your existing login. Check with `kilo auth list`.

### Register the MCP Server

> ⚠️ In every variant below, the `--no-project` flag matters: without it `uv` tries to build/install this folder as a Python project before running, which fails and prevents the server from starting.

#### Option A — Claude Code (recommended)

One command, from anywhere. `--scope user` makes the server available in all your projects (drop it for a per-project registration):

```bash
claude mcp add kilo-mcp --scope user -- \
  uv run --no-project --with mcp python /absolute/path/to/kilo-mcp-server/server.py
```

#### Option B — Claude Desktop

Find your Claude Desktop configuration file:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

Add the following to the `mcpServers` section, replacing `/absolute/path/to/...` with the real path to this folder, then fully quit and reopen Claude Desktop:

```json
{
  "mcpServers": {
    "kilo-mcp": {
      "command": "uv",
      "args": [
        "run",
        "--no-project",
        "--with",
        "mcp",
        "python",
        "/absolute/path/to/kilo-mcp-server/server.py"
      ]
    }
  }
}
```

Server settings can be passed here too via an `"env"` block (e.g. `{"KILO_MCP_TIMEOUT": "3600"}`) — see Server Configuration above.

#### Option C — Any other MCP client

The server speaks standard MCP over **stdio**. Point your client at:

```bash
uv run --no-project --with mcp python /absolute/path/to/kilo-mcp-server/server.py
```

(or plain `python server.py` in any environment where `pip install "mcp>=1.0.0"` has been run).

### Verify the installation

```bash
# Claude Code: the server should report "✔ Connected"
claude mcp get kilo-mcp
```

### Install Companion Skills

Five Kilo-specific skills (`kilo-mcp-headless-executor`, `kilo-mcp-conflict-resolver`,
`kilo-mcp-rag-explorer`, `mcp-orchestrator`, `mcp-metrics-analyst`) plus the
`mcp-orchestrator-agent` subagent live under `plugins/kilo-mcp/` — the concrete
Kilo binding of the generic [Architect/Executor pattern](https://github.com/primax79/ai-architect-executor).
This repo is itself a Claude Code / Kilo Code plugin marketplace
(`.claude-plugin/marketplace.json`), same mechanics as the sibling repos in
this family — see [`agentic-coding-kit`'s docs](https://github.com/primax79/agentic-coding-kit/tree/main/docs)
for the full concepts/authoring/distribution reference if any of this is
unfamiliar.

**One real dependency worth knowing about**: `mcp-orchestrator` and
`mcp-orchestrator-agent` assume the `task/` tree convention (`CONTEXT.md`,
`00-INDEX.md`, `specs/`, `NN-<slug>/plan.md`) owned by
[`agentic-coding-kit`](https://github.com/primax79/agentic-coding-kit)'s
`macroplan-authoring` skill (`common-tools` plugin) — install that
alongside this one if you delegate multi-step/multi-session work through
macroplans. See `plugins/kilo-mcp/dependencies.json` for the machine-readable
record (informational only — neither tool's installer enforces it today).

**Claude Code:**

```bash
claude plugin marketplace add https://github.com/primax79/kilo-mcp.git
/plugin install kilo-mcp
```

**Kilo Code**, either via `kilo-plugin-manager` (covers the agent too):

```bash
python3 ~/.kilo/skills/kilo-plugin-manager/scripts/plugin_manager.py add https://github.com/primax79/kilo-mcp.git --name kilo-mcp
python3 ~/.kilo/skills/kilo-plugin-manager/scripts/plugin_manager.py install kilo-mcp@kilo-mcp
```

or Kilo's native Skill URLs (skills only, no extra tooling — paste into
Settings UI **Local Config** or `.kilo/kilo.jsonc`'s `skills.urls`):

```text
https://raw.githubusercontent.com/primax79/kilo-mcp/main/plugins/kilo-mcp/skills/
```

A legacy path also still works, copying the skills straight from this
checkout without any marketplace registration (`--with mcp` is needed
because `server.py` imports the MCP SDK at module load):

```bash
uv run --no-project --with mcp python server.py --install-skills
```

Then, in a conversation, ask Claude to *"list the available Kilo models"* — a correct setup answers through the `kilo_list_models` tool. If the tools are missing, restart the client (stdio servers are spawned per session) and check that `kilo --version` and `uv --version` work in a fresh terminal.

---

## 🛠️ Exposed MCP Tools

When Claude connects to this server, it gains access to the following tools:

### `kilo_list_agents`

Scans the workspace (`.kilo/agent/`, `.kilocode/agent/`) and global (`~/.kilo/agent/`, `~/.kilocode/agent/`) configurations to return a list of all available Kilo agents. Claude uses this to choose the right agent for the job.

### `kilo_list_skills`

Scans for available Kilo skills (`SKILL.md` files). Claude can discover these to force Kilo to adopt specific workflows or design patterns.

### `kilo_list_models`

Runs `kilo models` and returns the valid `provider/model` ids Claude can pass to `kilo_implement`. Claude uses this to validate the model **before** delegating, avoiding failed runs from stale/unknown ids (e.g. `google/gemini-3.5-pro` does not exist, `google/gemini-3.5-flash` does).

**Parameters:**

- `filter` *(string, optional)*: Case-insensitive substring to narrow the (often large) list, e.g. `gemini`, `claude`.

### `kilo_rag_search`

Delegates codebase semantic search and exploration to Kilo. Claude can use this to quickly find where something is implemented across a large workspace without needing to pull the entire codebase into its own context. This tool is a **standing resource for the assistant's own work**: use it directly for exploration, Q&A, or your own implementation tasks, not just as a scoping step before delegating to `kilo_implement`.

**Parameters:**

- `query` *(string, required)*: Natural language description of what to search for.
- `working_directory` *(string, optional)*: Where Kilo should run. Defaults to the current directory.
- `path` *(string, optional)*: Specific subdirectory to limit the search.

### `kilo_implement`

The core execution tool. Claude uses this to delegate work.

**Parameters:**

- `task_instructions` *(string, required)*: The detailed markdown-formatted specifications. The server routes this through a temporary file bridge (avoids OS argument-size limits and gives Kilo a re-readable spec) and appends a **mandatory Final Report contract** — Outcome / Files changed / Verification / Issues — so Claude can review the run systematically.
- `working_directory` *(string, optional)*: Where Kilo should run. Defaults to the current directory.
- `agent` *(string, optional)*: The Kilo agent to use (e.g., `code`, `explore`).
- `model` *(string, optional)*: Model override in `provider/model` form, as listed by `kilo models` (defaults to `google/gemini-3.5-flash`).
- `focus_files` *(list[str], optional)*: Files Kilo must read before starting.
- `execution_hints` *(string, optional)*: Strategic hints, constraints, or configurations for Kilo's execution style.
- `skills_to_load` *(list[str], optional)*: Specific skills Kilo must utilize.
- `env_vars` *(dict, optional)*: Custom environment variables to inject (e.g., `{"DEBUG": "1"}`).
- `background` *(bool, optional, default `true`)*: Non-blocking by default — the tool returns **immediately** with a `task_id`. Kilo runs as a **fully detached OS process** (`setsid`, output to a log file) — it survives a server restart or crash, not tied to this server call's own lifetime. Monitor it with `kilo_task_progress` (fine-grained: Kilo's own live plan/commentary/cost) or `kilo_task_status` (coarse OS-level heuristic), and collect the outcome with `kilo_task_result` — both self-heal a task's status by checking real PID liveness, so even a task that outlived a server restart resolves correctly instead of staying stuck at `"running"` forever (see [Reliability](#reliability-background-task-execution) below). Set `background=false` only when you deliberately want to block for the full report in the same call (short tasks).
- `continue_session_id` *(string, optional)*: Resume an existing Kilo session (its id is surfaced by `kilo_task_progress` / a prior run's result) instead of starting fresh — the **steering/intervention** lever: after reviewing or `kilo_task_cancel`-ing a delegation that drifted off-spec, send corrective `task_instructions` into the *same* session so Kilo keeps the context of what it already built.
- `isolation` *(`"worktree"`, optional)*: Auto-creates a git worktree + new branch under `working_directory` (`.kilo-worktrees/<branch>`) and runs Kilo there instead of directly in `working_directory` — the one-call equivalent of calling `kilo_create_worktree` yourself first. **`kilo_implement` has no isolation and no locking by default** (see [Concurrency & Isolation](#concurrency--isolation) below) — pass this for anything beyond a single trivial edit, and always for parallel calls against the same repo. The worktree (and later the session, once resolved) are also registered in `.kilo/agent-manager.json` best-effort — see [Agent Manager Integration](#agent-manager-integration).
- `worktree_branch` / `worktree_base_branch` *(string, optional)*: Branch name (default `kilo/<task_id>`) and base branch/commit for the worktree created by `isolation='worktree'`. Ignored unless `isolation` is set.

### `kilo_task_result`

Fetch the outcome of a background delegation by `task_id`: returns the full Final Report (including the resolved `session_id`) once completed, a "still running" pointer to `kilo_task_progress`/`kilo_task_status`, or the cancellation note if it was stopped. If the task's process has already exited but nobody reconciled its record yet (e.g. the server that launched it restarted mid-run), the record is **self-healed from its log file** on this call — reconstructing the Final Report instead of reporting "running" forever. Task records persist under `KILO_MCP_DATA_DIR/tasks/`.

**Parameters:** `task_id` *(required)*.

### `kilo_task_progress`

Fine-grained **live progress** for a running background task, read straight from Kilo's own session database (`kilo.db`) — not a process heuristic: Kilo's own plan/todo list (each step's status), a tail of its most recent commentary, and running cost/tokens. Use this to judge *what* Kilo is actually doing (not just whether the process is alive), to decide whether to keep waiting, `kilo_task_cancel`, or prepare a corrective follow-up with `continue_session_id`. Scale how often you poll to the task's complexity/risk — small tasks rarely need it; large or high-stakes ones are worth checking periodically.

**Parameters:** `task_id` *(required)*.

### `kilo_task_cancel`

**Intervention**: stops a running background task by sending SIGTERM (escalating to SIGKILL after a short grace period) to its tracked process. A hard stop, not a graceful in-session abort — Kilo does not get to write its Final Report, and any partial file changes are left as-is (review with `kilo_workspace_status`). Race-safe against the background runner's own completion write, so the task record correctly ends up `cancelled` rather than being clobbered back to `completed`/`failed`.

**Parameters:** `task_id` *(required)*, `reason` *(optional, recorded on the task)*.

### `kilo_log_issue`

Records a defect found while reviewing Kilo-generated code (linked to the `run_id` printed by `kilo_implement`). This is the **feedback loop for prompt tuning**: recurring categories surface in `kilo_metrics` and indicate what the spec template must start guarding against.

**Parameters:** `run_id` *(required)*, `category` *(kebab-case defect class, e.g. `missing-edge-case`, `ignored-instruction`)*, `description` *(required)*, `severity` *(`minor`|`major`|`critical`)*, `file` *(optional)*.

### `kilo_metrics`

Aggregates usage over a look-back window: runs and outcomes, generated code volume, **real cost of delegating** (surfacing the breakdown: "cost of delegating: $X (Claude spec+review $Y + Kilo execution $Z)", or noting if Kilo execution was at no cost) **vs estimated cost of generating inline**, and logged defects by category. Raw JSONL data stays in `KILO_MCP_DATA_DIR` for ad-hoc analysis (`jq`, pandas, …).

**Parameters:** `days` *(optional, default 30)*.

### `kilo_create_worktree`

Creates a new isolated git worktree (with its own new branch) under `.kilo-worktrees/<branch_name>` — it runs `git worktree` directly, serialized by an in-process lock against other worktree creations in the same repo (no `.git/index.lock` races). This allows parallel `kilo_implement` runs without file collisions. Fails if the branch already exists. Prefer `kilo_implement`'s own `isolation='worktree'` parameter for the common case (one call instead of two); use this standalone tool when you need the worktree to exist before deciding what to delegate into it. Also registers the new worktree in `.kilo/agent-manager.json` best-effort — see [Agent Manager Integration](#agent-manager-integration).

**Parameters:** `branch_name` *(required)*, `base_branch` *(optional)*, `working_directory` *(optional)*.

### `kilo_auth_status`

Check the authentication status of Kilo providers (runs `kilo auth list`).

### `kilo_run_command`

Execute a deterministic, pre-programmed Kilo custom command (e.g., `db-migrate`) via `kilo run --command <name>`; `args` is passed to the command as its message.

**Parameters:** `command_name` *(required)*, `args` *(optional)*, `working_directory` *(optional)*.

### `kilo_workspace_status`

Report the git status (`git status -s`) of a working directory or worktree — useful to review what a delegated run actually touched. A clean tree and git failures are reported explicitly.

**Parameters:** `working_directory` *(optional)*.

### `kilo_task_status`

Coarse, OS-level diagnosis of running `kilo run` tasks — including delegations with no known `task_id` (e.g. launched from *other* sessions, or before this server tracked tasks). For each process it reports elapsed vs CPU time, network activity, the matching Kilo session's last update from `~/.local/share/kilo/kilo.db` (read-only), and a verdict: **WORKING** (recent session writes, or a long model call in flight), **STARTING** (younger than 2 minutes), or **LIKELY STUCK** (minutes old, no CPU, no network, no session — kill the PID and the calling session receives the exit). For a `task_id` you launched yourself, prefer `kilo_task_progress` — it reads Kilo's actual plan and commentary, not just a heuristic. The same diagnostic (plus a standalone kill script) is also available outside the MCP tool call as `scripts/diagnose-kilo-tasks.sh` / `scripts/kill-kilo-task.sh` in the `mcp-orchestrator` skill.

**Parameters:** `working_directory` *(optional — filter to one workspace)*.

---

## 📖 Example Workflow

1. **You ask Claude**: *"I need to implement a new caching layer for the database module. Please find where the database connections are currently handled, design the new architecture, and have Kilo implement it."*
2. **Claude explores via RAG**: Claude calls `kilo_rag_search(query="database connection pool and handlers")` to let Kilo quickly find the relevant files in the workspace.
3. **Claude designs the architecture** (acting as the Architect, using its superior reasoning and the search results).
4. **Claude delegates to Kilo (Generator)**: Claude calls `kilo_implement` with:
   - `task_instructions`: The detailed caching architecture it just designed.
   - `focus_files`: The files identified in the RAG search (e.g., `["src/db/connection.py", "src/db/queries.py"]`).
   - `agent`: `"code"`
   - `model`: `"google/gemini-3.5-flash"`
5. **Execution**: The MCP server writes the full specification to a temporary file — context files first, then instructions, hints, skills, and a **mandatory Final Report contract** — and spawns `kilo run` as a subprocess. Since `background` defaults to `true`, `kilo_implement` returns **immediately** with a `task_id`, and Claude's conversation is never blocked while Kilo works.
6. **Monitoring (proportional to risk)**: for this multi-file caching change, Claude polls `kilo_task_progress(task_id)` a few times — seeing Kilo's live plan/todo list, commentary, and running cost pulled straight from its session database. If it looked stuck or off-spec, Claude could `kilo_task_cancel` it and re-delegate corrective instructions via `continue_session_id` into the same session; for a small one-file fix, Claude would just skip straight to the result instead.
7. **Result**: once done, Kilo has ended with the structured Final Report (*Outcome / Files changed / Verification / Issues*). Claude fetches it with `kilo_task_result(task_id)`; the server has already parsed it, recorded the run to `metrics.jsonl` (including cost estimates and the resolved `session_id`), and returns it together with a `run_id`.
8. **Claude reviews**: Claude (acting as Reviewer) checks the Final Report and the resulting diff. If the review finds defects, Claude logs them with `kilo_log_issue(run_id, ...)` — feeding the metrics that show which defect categories the prompts should start guarding against (`kilo_metrics`).

---

## 🔀 Concurrency & Isolation

`kilo_implement` has **no isolation and no locking by default**: it runs `kilo run` directly in `working_directory` (which defaults to the server's own current directory), and nothing — no lock file, no mutual exclusion — stops a second writer from touching the exact same working tree at the same time. That second writer can be another concurrent `kilo_implement` call, or the orchestrating assistant's own `git` commands run directly against that directory. This is a real, observed failure mode, not a theoretical one: a git branch got reset and an in-progress file edit was nearly lost when an orchestrator ran its own git history surgery in a checkout a background `kilo_implement` task was still committing to.

Two independent safeguards, layered rather than exclusive:

1. **Isolate proactively.** Pass `isolation='worktree'` on `kilo_implement` (or call `kilo_create_worktree` yourself first) to run Kilo in a dedicated git worktree + branch instead of the shared tree. Do this for anything beyond a single trivial edit, and **always** when dispatching multiple `kilo_implement` calls in parallel against the same repository — parallel Kilo subprocesses are a deliberate feature of this server (see [Design & Internals](#-design--internals)), and isolation is what makes that safe rather than a race. When done, review with `kilo_workspace_status` and merge or remove the worktree (`git worktree remove`); `kilo-mcp-conflict-resolver` covers resolving conflicts if two isolated features touched overlapping files at merge time.
2. **Get warned when you don't isolate.** If `working_directory` isn't isolated and another task is already `running` against that exact directory (matched by real path, not raw string), `kilo_implement`'s response is prefixed with a `⚠️ COLLISION WARNING` naming the other `task_id`. This is a warning, not a refusal — it does not block the call — but it should always be treated as a stop sign: wait for the other task, isolate, or use a different directory. The same reasoning applies symmetrically to the orchestrator's own git operations: check `kilo_task_status(working_directory=...)` before running anything hard-to-reverse (`rebase`, `reset --hard`, history rewrites) against a directory that might still have a background Kilo task attached to it.

Neither mechanism replaces normal git discipline (commit or stash before risky operations) — they exist to make an easily-forgotten hazard hard to miss instead of silent.

---

## 🧟 Reliability: background task execution

Background `kilo_implement` tasks are launched as **fully detached OS processes** (`subprocess.Popen(..., start_new_session=True)`, stdin explicitly redirected to `/dev/null`, output captured to a log file under `KILO_MCP_DATA_DIR/tasks/<task_id>.log`) — not an `asyncio` task awaited inside the server's own event loop. This matters because it fixes three real, previously-observed failure modes where a background task got stuck reporting `"running"` forever:

1. **Server-lifetime coupling.** An `asyncio.create_task` awaiting an in-memory-captured subprocess dies with the server process (restart, crash, client disconnect). A `setsid`-detached process does not — it keeps running, and any later call to `kilo_task_status` / `kilo_task_progress` / `kilo_task_result` (even from a *different* server instance) reconciles the record by checking real PID liveness and reading the log file, finalizing `status` to `"completed"`/`"failed"` instead of leaving it stuck.
2. **Inherited stdin.** A subprocess launched without an explicit `stdin` inherits the MCP server's own stdin — which is the client's JSON-RPC pipe, not a terminal or `/dev/null`. If Kilo (or anything downstream) ever probes or reads stdin, it hangs indefinitely waiting on a pipe that will never produce EOF or terminal-like behavior. Fixed by always passing `stdin=DEVNULL` explicitly.
3. **Unreaped zombies reported as "alive".** `os.kill(pid, 0)` returns successfully for a zombie (exited but not yet reaped) process — a naive liveness check would report a genuinely-finished task as still running indefinitely. The liveness check reaps via `os.waitpid(pid, os.WNOHANG)` first; only if that finds no exited child does it fall back to `os.kill(pid, 0)`.

All three were found and fixed by live end-to-end testing through the actual MCP protocol (unit tests mocking at the subprocess-spawn seam did not, and cannot, catch process-lifetime or stdin-inheritance bugs — they're only observable running the real binary through the real transport).

---

## 🗂️ Agent Manager Integration

The Kilo VS Code extension's own "Agent Manager" UI reads its worktree/session registry from `<repo_root>/.kilo/agent-manager.json`. `kilo_create_worktree` and `kilo_implement(isolation='worktree')` write into the same file (same schema, same atomic tmp-file-then-rename scheme the extension itself uses) so worktrees and sessions created through this server can show up there too — no manual JSON editing needed.

**Important caveat, confirmed by reading the extension's own `WorktreeStateManager.ts`:** the extension loads this file **once** at startup into an in-memory map, never watches it for external changes, and every save serializes its *entire* in-memory state (a full overwrite, not a merge). An entry this server adds is therefore only guaranteed to survive until the extension's own next save — any UI action (renaming a tab, toggling a section) while the extension is open on the same repo will silently overwrite the addition with its own (unaware) in-memory snapshot. This is a structural limitation of the extension's design, not something fixable from this side. The write is still worthwhile: it's correct whenever the extension isn't currently running against that repo, and becomes visible on the extension's next startup load in that case. **Confirmed live (2026-07-29):** if a worktree/session doesn't show up despite being correct in `agent-manager.json`, closing and reopening the Agent Manager panel is enough to force a fresh `load()` — a full VS Code window reload isn't necessary.

A second, independent limitation: the `idle`/`busy`/`offline` status shown for a session is **never read from `agent-manager.json`** — the extension computes it live by querying its own running Kilo server for that directory. A session entry written by this server is therefore a bookkeeping pointer (it makes the card appear), not a live/interactive session — don't expect a session launched via `kilo_implement` to become an interactive chat in the Agent Manager UI just because it's registered.

**Two session-linking bugs found and fixed live (2026-07-29):** a session could fail to show up nested under its worktree card even when everything above worked correctly. Root causes (both now fixed, see [ARCHITECTURE.md §9](ARCHITECTURE.md#9-agent-manager-integration-kiloagent-managerjson) for the full detail): (1) linking only trusted a launch-time hint that's empty when a task reuses an already-existing worktree's directory — fixed by always matching the task's `cwd` against registered worktree paths; (2) more fundamentally, Kilo itself records a session's project directory as the **main repo root**, never the specific worktree subdirectory — so `session_id` resolution (`kilo_task_progress`, `continue_session_id` steering, and this linking) silently never worked at all for `isolation='worktree'` tasks, in every case, not intermittently. Fixed with a git-common-dir-based fallback match; verified live end-to-end after the fix.

---

## 🔐 Security Considerations

This server executes **arbitrary code** on your machine: `kilo_implement` runs Kilo in non-interactive (auto-approving) mode, and the `env_vars`, `working_directory`, and `task_instructions` parameters are fully controlled by the calling LLM. Treat it accordingly:

- Run it **only on a trusted, local machine** — never expose it to untrusted clients or networks.
- Kilo will edit files and run commands **without asking for confirmation**. Use version control and review the resulting diffs.
- `env_vars` can override Kilo's runtime configuration; only enable this server for workflows you fully control.

---

## 🧭 Design & Internals

The architectural rationale (architect/executor philosophy, temp-file spec bridge, Final Report contract, async parallelism, RAG delegation) is documented in [ARCHITECTURE.md](ARCHITECTURE.md), including component and sequence diagrams.

---

## ⚖️ License

[MIT](LICENSE)
