# Architectural Spec: Agent & Worktree Orchestration via MCP Server

## 0. Implementation status (updated 2026-07-29)

This document proposed a `kilo_orchestrator_*` toolset that was not yet
implemented when it was written. Since then `~/devel/kilo-mcp-server`
(`server.py`) has covered most of that toolset under different names, plus
three background-execution reliability bugs have been fixed and verified
live — updated map so as not to restart from scratch:

| Proposed here                       | Status                                                                                                                                                                                                                                                                                                                          |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `kilo_orchestrator_create_worktree` | ✅ Implemented — `kilo_create_worktree` / `kilo_implement(isolation='worktree')`, with an internal anti-race lock on `.git/index.lock`. In addition (not proposed here): automatically registers the worktree in `.kilo/agent-manager.json`.                                                                                    |
| `kilo_orchestrator_remove_worktree` | ❌ Not implemented — no tool removes a worktree; `git worktree remove` remains manual (documented in `KILO_CLI_WORKTREE_GUIDE.md` §2.5).                                                                                                                                                                                        |
| `kilo_orchestrator_spawn_agent`     | ✅ Implemented — `kilo_implement(background=true)`, the default. Synchronous session creation via Kilo Server REST API with SQLite fallback; runs as a detached OS process (`setsid`).                                                                                                                                          |
| `kilo_orchestrator_list_sessions`   | ✅ Implemented — `kilo_task_status` (OS-level heuristic) and `kilo_task_progress` (live plan/commentary) plus `kilo_get_session_todo` / `kilo_update_session_todo` for active session checklist inspection and injection.                                                                                                |
| `kilo_orchestrator_prompt_agent`    | ✅ Implemented — `kilo_implement(continue_session_id=...)` and REST-based prompt injection via `_kilo_server_prompt_async`. Additional control via `kilo_session_revert`, `kilo_session_fork`, `kilo_respond_question`.                                                                                                          |
| `kilo_orchestrator_get_logs`        | ✅ Implemented — `kilo_task_progress` returns live commentary/todo progress, and `_kilo_server_fetch_session_events` connects to the native SSE stream (`GET /event`).                                                                                                                                                         |
| `kilo_orchestrator_merge_task`      | ❌ Not implemented, deliberately — merging stays a manual step with explicit verification (real build/tests, not just Kilo's report) before merging, per the discipline described in `KILO_CLI_WORKTREE_GUIDE.md` §2.4/§2.5. Automating it would risk skipping exactly that verification.                                     |

Not proposed here, added on 2026-07-29: automatic registration of
worktrees/sessions in `.kilo/agent-manager.json` (visibility in VS Code's
Agent Manager UI, best-effort — see `README.md` §Agent Manager
Integration and `ARCHITECTURE.md` §9). This partially covers the
"visible orchestration" goal that this document originally set out as its
motivation (§1), without needing the `kilo_orchestrator_*` toolset
proposed below — that toolset remains an option if an explicit
orchestration API is ever needed instead of just cosmetic visibility.

---

## 1. Context & Purpose

The goal of this spec is to define the architecture for an **MCP Server** capable of managing and orchestrating concurrent **Kilo CLI** sessions based on **Git Worktree**.
Currently, multi-task/multi-worktree orchestration is delegated to Kilo's VS Code extension (via the Agent Manager interface). Moving or replicating this logic inside a custom MCP Server makes the orchestration **headless**, IDE-independent, and integrable with external tools (e.g. CLI, higher-level agents, CI/CD, ticketing systems).

---

## 2. Current State & Reference Primitives (Agent Manager)

In the VS Code context, orchestration relies on the following primitives:

- **`mode: "worktree"`**: Creates an isolated git worktree and starts a dedicated session.
- **`action: "list"`**: Asynchronous session inspection (`idle`, `busy`, `waiting` state, git diff, branch).
- **`action: "prompt"`**: Asynchronously sends further instructions to a specific running session via `sessionID`.
- **`action: "stop"`**: Stops and cleans up the session/worktree.

---

## 3. MCP Server Architecture for Orchestration

The MCP Server will expose a suite of **MCP Tools** to let an Orchestrator Agent (or external clients) manage the entire lifecycle of Kilo sub-agents.

### 3.1. Proposed Toolset for the MCP Server

#### A. Workspace & Worktree Management

1. **`kilo_orchestrator_create_worktree`**
   - **Input:** `task_id` (string), `branch_name` (string), `base_branch` (optional, default: current/main).
   - **Action:**
     - Runs `git worktree add .kilo/worktrees/<branch_name> -b <branch_name>`.
     - Initializes the environment by invoking the setup script if present (e.g. `.kilo/setup-script.sh`).
   - **Output:** Absolute path of the created worktree.

2. **`kilo_orchestrator_remove_worktree`**
   - **Input:** `branch_name` (string), `force` (boolean).
   - **Action:** Cleans up the worktree via `git worktree remove` and optionally deletes the branch if merged.

#### B. Process & Agent Supervision

1. **`kilo_orchestrator_spawn_agent`**
   - **Input:**
     - `worktree_path` (string)
     - `prompt` (string): Instruction/task to execute.
     - `model` (optional string): Override the model used for the sub-session.
     - `auto_verify_command` (optional string): Test/validation command (e.g. `npm test`, `pytest`).
   - **Action:**
     - Spawns a child Kilo CLI process in headless mode inside the `worktree_path` folder.
     - Registers the process in an internal state map (PID, `session_id`, log stream, status).
   - **Output:** Unique `session_id` for monitoring.

2. **`kilo_orchestrator_list_sessions`**
   - **Input:** None (or status filters).
   - **Action:** Returns the up-to-date list of active/completed sessions with:
     - `session_id`
     - `branch` / `worktree_path`
     - `status` (`running`, `idle`, `completed`, `failed`)
     - Pending Git changes (reference to `git status` / `git diff --stat`)
     - Latest log output.

3. **`kilo_orchestrator_prompt_agent`**
   - **Input:** `session_id` (string), `prompt` (string).
   - **Action:** Sends a new message/instruction to the running session (via stdin/IPC or a controlled CLI re-launch on the worktree).

4. **`kilo_orchestrator_get_logs`**
   - **Input:** `session_id` (string), `lines` (optional int).
   - **Action:** Returns the tail of the agent's output (stdout/stderr).

#### C. Integration & Merge

1. **`kilo_orchestrator_merge_task`**
   - **Input:** `branch_name` (string), `target_branch` (optional string).
   - **Action:**
     - Runs validation/test checks.
     - Merges the worktree's branch into the target branch.
     - Removes the worktree.

---

## 4. Orchestrator Operational Flow (Self-Verification Loop)

```text
[Client / External Trigger / Master Agent]
              │
              ├─► Call: kilo_orchestrator_create_worktree
              │
              ├─► Call: kilo_orchestrator_spawn_agent (with prompt + test command)
              │
              ├─► Polling via kilo_orchestrator_list_sessions / get_logs
              │
              ├─► [If tests fail] ──► Call: kilo_orchestrator_prompt_agent (with error stacktrace)
              │
              └─► [If completed successfully] ──► Call: kilo_orchestrator_merge_task
```

---

## 5. Technical Implementation Considerations

1. **State & Persistence:**
   - Maintain lightweight in-memory/file state (e.g. `.kilo/mcp-orchestrator-state.json`) to track the correspondence between `session_id`, the Kilo CLI process PID, the log file path, and the git worktree.
2. **Concurrency & Locking:**
   - Isolating each execution in its own worktree avoids filesystem conflicts.
   - Avoid using `git stash` shared across worktrees, to prevent race conditions.
3. **Environment & Variable Management:**
   - Pass the correct environment variables (`WORKTREE_PATH`, `REPO_PATH`) to the child Kilo process, consistent with what's already structured in Kilo's conventions (`.kilo/setup-script`).
