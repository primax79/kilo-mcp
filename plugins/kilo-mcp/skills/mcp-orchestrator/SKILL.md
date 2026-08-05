---
name: mcp-orchestrator
description: Orchestrate Kilo (the Executor) through the kilo-mcp server - RAG-first discovery, worktree isolation (kilo_implement's own isolation='worktree', or kilo_create_worktree), non-blocking parallel delegation via kilo_implement, complexity-scaled monitoring/intervention (kilo_task_progress/kilo_task_cancel/continue_session_id), verification of Final Reports, and defect telemetry. Use when coordinating development work through the kilo-mcp tools.
---

# mcp-orchestrator

This skill is designed for Claude (the Orchestrator) to coordinate Kilo (the Executor) using RAG, Worktrees, non-blocking Parallel Execution, Monitoring/Intervention, and Issues Tracking.

## Role discipline: delegate, don't implement

You are the architect/orchestrator, not the implementer. When a task is scoped
for delegation (a plan/macroplan task, or any request to "implement X" that
this skill's description matches), implementation work goes through
`kilo_implement` — not your own `Edit`/`Write` tools. This holds even when a
task looks small or fast to do yourself; "it's simple, I'll just do it" is
exactly how delegation gets silently skipped. If `kilo_implement` is
unavailable or fails, stop and say so instead of implementing in its place —
don't silently fall back to doing the work yourself.

The one exception is **verification-phase minor fixes**: while reviewing a
Final Report (Phase 5 below), you may correct a small, obvious issue
yourself directly in Kilo's worktree — a typo, a wrong import, a
misnamed variable — without a full round-trip. Anything bigger than a
handful of lines, or that touches actual logic, goes back to Kilo via
`kilo_implement(continue_session_id=..., task_instructions="<corrective
instructions>")`, not a direct edit. When in doubt, delegate rather than
fix it yourself.

## Logic and Behavior

- **Phase 1 — Discovery & RAG:** Explore the repository using `kilo_rag_search` before designing solutions. Do not guess filenames. Use `kilo_list_models` and `kilo_auth_status` to ensure Kilo's engine and models are ready.
- **Phase 2 — Isolation:** `kilo_implement` has **no isolation and no locking by default** — it runs directly in `working_directory` (defaulting to the server's own cwd), and nothing stops a second concurrent writer — another `kilo_implement` call, or your own `git` commands — from racing it in the exact same tree. This is not hypothetical: it has caused a real incident (a live git race that reset a branch and nearly dropped in-progress work while the orchestrator was doing its own git surgery in the same checkout Kilo was still committing to). Default to isolating:
  - Pass `isolation='worktree'` directly on the `kilo_implement` call (auto-creates `.kilo-worktrees/<branch>` and runs there) — prefer this over the two-step `kilo_create_worktree` + `working_directory` combo, it's one step and harder to forget.
  - Use `kilo_create_worktree` standalone only when you need the worktree to exist before deciding what to delegate into it.
  - Skip isolation only for a single trivial, quick edit — and even then, if `kilo_implement`'s response is prefixed with a **⚠️ COLLISION WARNING** (another task already running against that exact directory), treat it as a stop sign: wait for the other task (`kilo_task_status`/`kilo_task_result`), isolate, or pick a different directory. Never proceed past it on the assumption it's a false alarm.
  - The same risk runs in the other direction too: before running your **own** `git` commands (rebase, reset, checkout, history rewrites) against a directory, check `kilo_task_status` for that `working_directory` first — a still-running `kilo_implement` task there is exactly the same hazard.
  - **Git worktree isolation does NOT isolate shared host resources.** Two parallel `isolation='worktree'` tasks (e.g. comparing two models on the same feature) still run on the same machine, sharing ports, Docker container names, and any file each task's own tooling might write outside its worktree. Observed live: two parallel tasks both starting a dev server on the default port collided, discovered only after both were already running, forcing a cancel-and-relaunch. Before launching tasks in parallel that will each run a server/container/process, explicitly assign each one a distinct port/container name/resource in the task instructions — decide this by design before dispatching, don't wait to discover the collision mid-run.
- **Phase 3 — Delegation:** Use `kilo_implement` to delegate implementation work. It runs in the **background by default** and returns a `task_id` immediately — never block the conversation waiting on it. Pass `focus_files` found during search, configure any `skills_to_load`, and dispatch tasks in parallel across worktrees. For work that spans multiple dependent steps or sessions, write it up first as a `plans/` macroplan (see the `macroplan-authoring` skill) and delegate one task file at a time — each task file is already scoped, self-contained, and has its own verification section, which maps directly onto one well-formed `kilo_implement` call.
- **Phase 4 — Monitoring & Intervention:** Scale how closely you watch a task to its complexity/risk:
  - **Small, well-scoped tasks:** a single `kilo_task_result` check at the end is enough.
  - **Large, multi-file, or high-risk tasks:** poll `kilo_task_progress` periodically — it reads Kilo's own live plan/todo list, recent commentary, and running cost directly from its session database, not a heuristic. Use `kilo_task_status` for a quicker but coarser OS-level signal (also useful for tasks with no known `task_id`, e.g. legacy or foreground runs from another session — see `scripts/diagnose-kilo-tasks.sh`).
  - **If a task drifts off-spec or looks stuck:** `kilo_task_cancel` stops it (hard kill by tracked PID, no graceful in-session abort). Review `kilo_workspace_status` for partial changes, then either revert or continue with a corrective `kilo_implement` call using `continue_session_id` — Kilo resumes the SAME session with full memory of what it already built, instead of starting blind.
  - **Don't go idle after dispatching — check back proactively, don't wait to be asked.** `kilo_implement` returning a `task_id` immediately means control comes back to you well before the work is done; if you just end your turn there, nothing polls the task until the user happens to ask "how's it going?" — observed live: a task that had already completed sat unreported until the user prompted a check. If your host exposes a self-scheduled wakeup (e.g. Claude Code's `ScheduleWakeup`), use it to check `kilo_task_progress`/`kilo_task_result` again after a delay sized to the task (don't poll every few seconds — that's wasted cycles for a multi-minute build; a longer interval, or a couple of spaced checks, is enough). If no such mechanism exists in your host, at minimum say explicitly in your response that the task is still running and how the user can check on it themselves, instead of implying you'll follow up when you have no way to.
  - Ready-to-invoke scripts for the manual/no-task-id case live in `scripts/` (see below) — use them instead of re-deriving the pgrep/ps/lsof/sqlite3 diagnostic pipeline from scratch each session.
- **Phase 5 — Verification & Review: NEVER trust the Final Report alone.** Inspect results with `kilo_workspace_status`, then actually **execute** what was built — reading the report or the diff is not verification, it's a summary. Across real delegation rounds, deviations that never surfaced from the report alone only showed up by running the code: a file committed that should have been gitignored, a cited source path that didn't exist anywhere in the codebase (invented for plausibility), a citation pointing at the right method but the wrong line, DB column types guessed from convention instead of read from the real DDL already available. Calibrate to what was built:
  - **Backend**: actually start the app against a real seeded database, don't just read the JPA annotations — a schema validation mode that passes silently is much stronger evidence than a visual read. Hit the endpoints with real `curl` calls (200/401/etc.), don't assume them from the controller code.
  - **Frontend**: actually run the build and test suite (headless browser if needed), don't stop at reading the `.ts` files.
  - **Schema/data**: if a file is claimed "copied verbatim" from a source, `diff` it for real against that source — don't trust the claim.
  - **Infra**: if there's a `docker-compose.yml`, actually bring it up, wait for the healthcheck, and query the resulting service/DB for real.
  - See the `kilo-task-delegation` skill for calibrating verification depth to task risk in more detail.
- **Phase 6 — Closure & Telemetry:** If defects are found, log them with `kilo_log_issue` (vital for continuous prompt/specification tuning) and request Kilo to fix them.
  - **Merging: always checkout the target branch explicitly first.** Running `git merge --no-ff <feature-branch> -m "..." <target>` without checking out `<target>` first is a real, observed mistake — the trailing `<target>` argument gets interpreted as another branch to merge IN, not as the destination, so the merge silently lands on whatever branch you happened to be on. Correct sequence, every time: `git status --short` (clean) → `git checkout <target>` (explicit, never implicit) → `git merge --no-ff <feature-branch> -m "..."` → `git log --oneline --graph -6` (confirm the merge landed where intended). Remove the worktree once merged and no longer needed (`git worktree remove <path>`).

Remember: the RAG index behind `kilo_rag_search` is a standing resource for your own exploration and Q&A too — use it even when you are not delegating anything.

**Agent Manager visibility is automatic, not something you manage.** `kilo_create_worktree` and `kilo_implement(isolation='worktree')` register the worktree — and later its session, once resolved — into `.kilo/agent-manager.json` on their own (as of 2026-07-29). You don't need to ask whether to do this, back up the file, or hand-edit it. The one caveat: a task launched before a kilo-mcp-server fix/restart doesn't benefit retroactively once it's already `completed` — if a worktree shows without a linked session, check whether it predates the current server process rather than assuming the mechanism is broken.

## Scripts

- `scripts/diagnose-kilo-tasks.sh [working_directory]` — the OS-level diagnostic used when you don't have a `task_id` (e.g. a `kilo run` process from another session/tool, or one started before this MCP server tracked tasks): lists every running `kilo run` process with elapsed/CPU time, network activity, and its matching Kilo session's last DB update, plus a WORKING/STARTING/LIKELY STUCK verdict. Equivalent to the `kilo_task_status` MCP tool, packaged as a standalone script for direct terminal use or when MCP tools aren't the right fit.
- `scripts/kill-kilo-task.sh <pid> [reason]` — cleanly stops a `kilo run` process found via the script above (SIGTERM, escalating to SIGKILL after a grace period). Use `kilo_task_cancel` instead whenever you have a `task_id` from this server — it does the same thing but keeps the task record in sync.
