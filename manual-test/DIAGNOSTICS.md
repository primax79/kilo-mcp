# Diagnosing kilo job state - ready-to-invoke scripts and commands

Three ways to check on a `kilo run` job, from quickest to most detailed. All
are read-only except the kill commands at the end.

## 1. Packaged scripts (recommended - no need to remember any of this)

```bash
# List every running `kilo run` process with elapsed/CPU/network + a
# WORKING / STARTING / LIKELY STUCK verdict. Optionally filter by workspace:
.claude/skills/mcp-orchestrator/scripts/diagnose-kilo-tasks.sh [working_directory]

# Cleanly stop one (SIGTERM, then SIGKILL after a grace period):
.claude/skills/mcp-orchestrator/scripts/kill-kilo-task.sh <pid> "<reason>"
```

## 2. Raw bash one-liners

```bash
# Any kilo run process at all?
pgrep -fl "kilo run"

# Any other kilo-related process (daemons, MCP server instances, etc.) -
# useful to spot contention, e.g. multiple `kilo serve` daemons running:
ps aux | grep -i kilo | grep -v grep

# For a given PID: elapsed vs CPU time (big gap + no growth = suspicious)
ps -p <PID> -o etime=,time=,pcpu=

# Is it actually talking to the model right now? (established connections)
lsof -a -p <PID> -i

# What working directory is it running in?
lsof -a -p <PID> -d cwd -Fn

# Most recently created/updated Kilo sessions (across ALL workspaces):
sqlite3 "file:$HOME/.local/share/kilo/kilo.db?mode=ro" -readonly -json "
  SELECT p.worktree, s.title, s.time_created, s.time_updated
  FROM session s JOIN project p ON s.project_id = p.id
  ORDER BY s.time_updated DESC LIMIT 10"

# Sessions for one specific workspace:
sqlite3 "file:$HOME/.local/share/kilo/kilo.db?mode=ro" -readonly -json "
  SELECT s.id, s.title, s.cost, s.tokens_input, s.tokens_output, s.time_updated
  FROM session s JOIN project p ON s.project_id = p.id
  WHERE p.worktree = '<ABSOLUTE_WORKSPACE_PATH>'
  ORDER BY s.time_created DESC LIMIT 5"

# Live plan/todo list for a session id:
sqlite3 "file:$HOME/.local/share/kilo/kilo.db?mode=ro" -readonly \
  "SELECT content, status, priority FROM todo WHERE session_id = '<SESSION_ID>' ORDER BY position"

# Recent commentary (raw JSON parts) for a session id:
sqlite3 "file:$HOME/.local/share/kilo/kilo.db?mode=ro" -readonly \
  "SELECT data FROM part WHERE session_id = '<SESSION_ID>' ORDER BY time_created DESC LIMIT 5"

# Kill it (graceful, then forced):
kill -TERM <PID>; sleep 2; kill -0 <PID> 2>/dev/null && kill -KILL <PID>
```

## 3. Python (same helpers server.py's tools use - for scripting/automation)

Run these from the repo root with the project's venv active
(`source .venv/bin/activate` or prefix with `.venv/bin/python3`).

```python
import asyncio, sys
sys.path.insert(0, "/Users/Alfredo/devel/kilo-mcp-server")
import server

# OS-level heuristic across every kilo run process (kilo_task_status logic):
print(asyncio.run(server.kilo_task_status()))

# ...filtered to one workspace:
print(asyncio.run(server.kilo_task_status(working_directory="/path/to/workspace")))

# If you have a task_id from a kilo_implement call made THIS server process
# (i.e. in the same running MCP server, or via manual _write_task_record):
print(asyncio.run(server.kilo_task_progress(task_id="<TASK_ID>")))
print(asyncio.run(server.kilo_task_result(task_id="<TASK_ID>")))
print(asyncio.run(server.kilo_task_cancel(task_id="<TASK_ID>", reason="stuck")))

# Lower-level building blocks, usable without any task_id at all - just a
# workspace path and a rough start time (ISO string or "now"):
from datetime import datetime, timezone
started = datetime.now(timezone.utc).isoformat()
session_id = server._find_session_for_task("/path/to/workspace", started)
print("session_id:", session_id)
if session_id:
    print("todos:", server._read_todos(session_id))
    print("recent text:", server._read_recent_texts(session_id))
    print("summary:", server._read_session_summary(session_id))
```

### One-shot CLI invocations (no REPL needed)

```bash
.venv/bin/python3 -c "
import asyncio, sys
sys.path.insert(0, '.')
import server
print(asyncio.run(server.kilo_task_status()))
"
```

## What "stuck" actually looks like (learned from a real hang on 2026-07-14)

A process can report `network: yes` (an open TCP connection) for several
minutes while:
- CPU time barely increases (e.g. 2.8s → 2.9s over 70+ seconds - essentially
  idle, not computing)
- no session ever appears in `kilo.db` for that workspace (i.e. not even the
  first token of a model response has arrived)

`network: yes` on its own is **not proof of real progress** - it only means a
TCP handshake completed, not that data is flowing. Cross-check CPU time growth
and session/todo activity before concluding a task is healthy. Conversely,
don't conclude "stuck" from a single heuristic either - see
[MANUAL_STEPS.md](MANUAL_STEPS.md) for a way to reproduce and confirm outside
the MCP server entirely, and check `ps aux | grep -i kilo` for contention from
other `kilo serve` daemons or stray processes before assuming the CLI itself
is at fault.
