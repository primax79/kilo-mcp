# Manual test: reproduce a kilo_implement call by hand

This replicates exactly what `kilo_implement` does, step by step, without the
MCP server in the loop at all. Use it to isolate whether a problem is in the
`kilo` CLI / environment (network, provider, a competing `kilo serve` daemon)
or in how `server.py` invokes things - if the manual steps hang the same way,
it's not our server's fault.

Either run `run_manual_task.py` (does all of this automatically, with live
progress printed), or follow these steps by hand in a terminal.

## 0. Prerequisites

```bash
# Confirm auth is set up
kilo auth list

# Confirm no stray kilo run / kilo serve processes are already fighting for
# resources (see DIAGNOSTICS.md for what a healthy vs. suspicious list looks like)
ps aux | grep -i kilo | grep -v grep
```

## 1. Prepare a scratch git repo (kilo_implement requires one)

```bash
SCRATCH=/tmp/kilo-manual-test-repo
mkdir -p "$SCRATCH" && cd "$SCRATCH"
git init -q
git config user.email "manual-test@example.com"
git config user.name "Manual Test"
echo "# scratch" > README.md
git add README.md && git commit -q -m init
```

## 2. Write the task spec to a temp file (the "temp-file bridge")

This is `task-spec.md` in this folder - already built with the exact section
structure `kilo_implement` produces (Task Instructions + the mandatory Final
Report contract). Copy it to a temp file, mirroring what the server does:

```bash
PROMPT_FILE=$(mktemp /tmp/kilo-manual-XXXXXX.md)
cp /Users/Alfredo/devel/kilo-mcp-server/manual-test/task-spec.md "$PROMPT_FILE"
echo "$PROMPT_FILE"
```

## 3. Launch `kilo run` - identical invocation to `kilo_implement`

```bash
kilo run --agent code --model google/gemini-3.5-flash \
  "The file $PROMPT_FILE contains your full task specification from an orchestrating architect. Read it now, then execute it completely. Your final message must be the 'Final Report' described in that file." \
  > /tmp/kilo-manual-out.log 2>&1 &
KILO_PID=$!
echo "launched pid $KILO_PID"
```

This returns your shell prompt immediately - the manual equivalent of
`background=true`. `$KILO_PID` is what you'll monitor and, if needed, cancel.

## 4. Monitor it - same checks as `kilo_task_status` / `kilo_task_progress`

```bash
# OS-level heuristic (kilo_task_status equivalent)
ps -p "$KILO_PID" -o etime=,time=          # elapsed vs CPU time
lsof -a -p "$KILO_PID" -i                  # network connections (empty = not talking to the model)
lsof -a -p "$KILO_PID" -d cwd -Fn           # confirm its working directory

# Fine-grained live progress (kilo_task_progress equivalent) - find the session:
sqlite3 "file:$HOME/.local/share/kilo/kilo.db?mode=ro" -readonly \
  "SELECT s.id, s.title, s.cost, s.tokens_input, s.tokens_output
   FROM session s JOIN project p ON s.project_id = p.id
   WHERE p.worktree = '$SCRATCH' ORDER BY s.time_created DESC LIMIT 1"

# Once you have the session id (SESSION_ID=...), see its live plan/todo list:
sqlite3 "file:$HOME/.local/share/kilo/kilo.db?mode=ro" -readonly \
  "SELECT content, status, priority FROM todo WHERE session_id = '<SESSION_ID>' ORDER BY position"

# ...and its most recent commentary:
sqlite3 "file:$HOME/.local/share/kilo/kilo.db?mode=ro" -readonly \
  "SELECT data FROM part WHERE session_id = '<SESSION_ID>' ORDER BY time_created DESC LIMIT 5"
```

Or just use the packaged script, which does all of the above in one shot:

```bash
/Users/Alfredo/devel/kilo-mcp-server/.claude/skills/mcp-orchestrator/scripts/diagnose-kilo-tasks.sh "$SCRATCH"
```

**What to watch for (from the live investigation on 2026-07-14):** a process
can show `network: yes` (an established TCP connection) for minutes while CPU
time barely increases and no session ever appears in `kilo.db` - i.e. the
connection is open but no data is actually flowing. Don't treat "network: yes"
alone as proof of real progress; cross-check CPU time growth and session
creation too.

## 5. Get the result

```bash
wait "$KILO_PID"       # blocks until it exits (Ctrl+C to stop waiting, the process keeps running)
echo "exit code: $?"
cat /tmp/kilo-manual-out.log
ls -la "$SCRATCH"       # did saluto.txt actually get created?
cat "$SCRATCH/saluto.txt" 2>/dev/null
```

## 6. Cancel it if needed (kilo_task_cancel equivalent)

```bash
/Users/Alfredo/devel/kilo-mcp-server/.claude/skills/mcp-orchestrator/scripts/kill-kilo-task.sh "$KILO_PID" "stuck - no session after N minutes"
```

or by hand:

```bash
kill -TERM "$KILO_PID"; sleep 2; kill -0 "$KILO_PID" 2>/dev/null && kill -KILL "$KILO_PID"
```

## 7. Clean up

```bash
rm -f "$PROMPT_FILE" /tmp/kilo-manual-out.log
rm -rf "$SCRATCH"
```
