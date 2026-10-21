---
name: kilo-mcp-orchestrator
description: "Tool-level binding for orchestrating Kilo via the kilo-mcp server - kilo_rag_search, kilo_implement with worktree isolation, deterministic wait (kilo_task_wait), progress monitoring, and defect telemetry (kilo_log_issue). Use specifically when executing tasks through kilo-mcp tools. For generic multi-agent coordination or native worktrees, use orchestration-workflow."
---
<!-- GENERATED FROM SKILL.template.md — DO NOT EDIT BY HAND. Run generate_binding.py to regenerate. -->

<!--
TEMPLATE SLOTS (for binding maps in bindings/*.json):
  Top-level:
    NAME                - Skill name (e.g. orchestration-workflow, kilo-mcp-orchestrator)
    DESCRIPTION         - Frontmatter description
  Tools:
    implement_call      - Primary delegation call (e.g. `kilo_implement`)
    edit_tools          - Orchestrator's edit tools
    executor_name       - Executor name (e.g. Kilo)
    executor_poss       - Executor possessive (e.g. Kilo's)
    spec_skill_link     - Link/name for task-spec-authoring skill
  Blocks:
    intro               - Skill intro block under Quick Reference
    precedence_note     - Precedence relationship between generic and bound skills (under Role discipline)
    corrective_call     - Phrase for follow-up delegation into existing session
    phase1_suffix       - Suffix to Phase 1 heading (e.g. " & RAG")
    phase1_body         - Phase 1 Discovery body
    phase2_body         - Phase 2 Isolation body
    phase3_body         - Phase 3 Delegation body
    phase4_body         - Phase 4 Monitoring body
    phase5_tool_notes   - Phase 5 Tool-specific inspection / execution notes (Verification Modes & Decision Table remain invariant)
    phase6_body         - Phase 6 Closure & Telemetry body
    closing_note        - Final closing reminder (Scaffolding & Task State Table remain invariant)
  Additions:
    phase4_extra        - Extra items under Phase 4
    end                 - Extra sections at the end of the skill
-->

# kilo-mcp-orchestrator

## Quick Reference

| Field | Value |
| --- | --- |
| **Audience / Role** | Architect / Orchestrator (`orchestration` profile) |
| **When to Load** | Coordinating multi-step or parallel development, discovery, worktree isolation, monitoring, and verification |
| **Execution Modes Supported** | **Single-Framework Native** (in-framework parallel worktrees & subagents, zero MCP required) and **Multi-Framework MCP / CLI** (cross-framework delegation) |
| **Companion Scripts** | `bin/ai-orch`, `scripts/tasks_engine.py`, `scripts/orchestration_config.py` |
| **Related Skills** | `task-pipeline`, `task-spec-authoring`, `task-worktree-ops`, `conflict-resolver`, `orchestration-metrics` |

This skill is designed for an Orchestrating AI (e.g. Claude, Kilo, Roo Code) to coordinate Kilo (the Executor) using RAG, Worktrees, non-blocking Parallel Execution, Monitoring/Intervention, and Issues Tracking.

## Role discipline: delegate, don't implement

You are the architect/orchestrator, not the implementer. When a task is
scoped for delegation (a plan/macroplan task, or any request to "implement
X" that this skill's description matches), implementation work goes
through `kilo_implement` - not your own `Edit`/`Write` tools. This holds
even when a task looks small or fast to do yourself; "it's simple, I'll
just do it" is exactly how delegation gets silently skipped. If `kilo_implement`
is unavailable or fails, stop and say so instead of implementing in its
place - don't silently fall back to doing the work yourself.

The one exception is **verification-phase minor fixes**: while reviewing a
Final Report (Phase 5 below), you may correct a small, obvious issue
yourself directly in Kilo's worktree - a typo, a wrong import, a
misnamed variable - without a full round-trip. Anything bigger than a
handful of lines, or that touches actual logic, goes back to Kilo via
`kilo_implement(continue_session_id=..., task_instructions="<corrective instructions>")`, not a direct edit. When
in doubt, delegate rather than fix it yourself.

### Precedence with Generic Skills

This skill provides the concrete `kilo-mcp` tool bindings for orchestrating Kilo. When `orchestration-workflow` is also loaded, this skill specializes the tool calls (`kilo_implement`, `kilo_task_wait`, etc.), while `orchestration-workflow` governs the overarching architecture: task state transitions, verification modes, the Post-Verification Decision Table, and harness safety.

## Logic and Behavior

- **Phase 1 - Discovery & RAG:** Explore the repository using `kilo_rag_search` before designing solutions. Do not guess filenames. Use `kilo_list_models` and `kilo_auth_status` to ensure Kilo's engine and models are ready once per session or after a dispatch failure (do not re-run health/config checks before every single task).
- **Phase 2 - Isolation:** `kilo_implement` has **no isolation and no locking by default** — it runs directly in `working_directory` (defaulting to the server's own cwd), and nothing stops a second concurrent writer — another `kilo_implement` call, or your own `git` commands — from racing it in the exact same tree. This is not hypothetical: it has caused a real incident (a live git race that reset a branch and nearly dropped in-progress work while the orchestrator was doing its own git surgery in the same checkout Kilo was still committing to). Default to isolating:
  - Pass `isolation='worktree'` directly on the `kilo_implement` call (auto-creates `.kilo-worktrees/<branch>` and runs there) — prefer this over the two-step `kilo_create_worktree` + `working_directory` combo, it's one step and harder to forget.
  - Use `kilo_create_worktree` standalone only when you need the worktree to exist before deciding what to delegate into it.
  - Skip isolation only for a single trivial, quick edit — and even then, if `kilo_implement`'s response is prefixed with a **⚠️ COLLISION WARNING** (another task already running against that exact directory), treat it as a stop sign: wait for the other task (`kilo_task_status`/`kilo_task_result`), isolate, or pick a different directory. Never proceed past it on the assumption it's a false alarm.
  - The same risk runs in the other direction too: before running your **own** `git` commands (rebase, reset, checkout, history rewrites) against a directory, check `kilo_task_status` for that `working_directory` first — a still-running `kilo_implement` task there is exactly the same hazard.
  - **Git worktree isolation does NOT isolate shared host resources.** Two parallel `isolation='worktree'` tasks (e.g. comparing two models on the same feature) still run on the same machine, sharing ports, Docker container names, and any file each task's own tooling might write outside its worktree. Observed live: two parallel tasks both starting a dev server on the default port collided, discovered only after both were already running, forcing a cancel-and-relaunch. Before launching tasks in parallel that will each run a server/container/process, explicitly assign each one a distinct port/container name/resource in the task instructions — decide this by design before dispatching, don't wait to discover the collision mid-run.
  - **Worktree isolation does NOT isolate the repository's own configuration.**
    `.git/config` is shared by every worktree of a repo, so a delegated task that
    runs `git config` - or any tool that runs it on your behalf, e.g. `husky
    init` setting `core.hooksPath` - writes into the configuration the user's
    *own* checkout reads (see `references/incidents.md` #3). Any task that would touch
    repo-level configuration must be instructed to create the files and **document
    the activation command instead of running it**, and to prove it didn't by
    showing `git config --get <key>` returning empty. Activation is the user's
    call, not the task's.
  - **Track Branch and Physical Worktree Path on the Task:** When dispatching a
    task to an isolated worktree, record `- branch: <branch-name>` and
    `- worktree: <physical-worktree-path>` on the task file (or in its `PROGRESS.md`).
    This guarantees that the Architect, the UI dashboard, and the reviewer always know
    the exact physical checkout path and git ref without searching.
- **Phase 3 - Delegation:** Use `kilo_implement` to delegate implementation work. It runs in the **background by default** and returns a `task_id` immediately — never block the conversation waiting on it. Pass `focus_files` found during search, configure any `skills_to_load`, and dispatch tasks in parallel across worktrees. Before each `kilo_implement`, move the task to `in progress` (`ai-orch tasks transition <id> "in progress" --actor <you> --reason "kilo, worktree <branch>"`); when its Final Report arrives with a commit, move it to `delivered`. A dispatched task left `open` is a bookkeeping bug. Keep at most ~4 concurrent `kilo run`s on the machine (count other sessions' with `kilo_task_status`): more make Kilo's shared session database fail with 'Database is busy'. For work that spans multiple dependent steps or sessions, write it up first as a `tasks/` pipeline (see the `task-pipeline` skill) and delegate one task file at a time — each task file is already scoped, self-contained, and has its own verification section, which maps directly onto one well-formed `kilo_implement` call.
- **Phase 4 - Monitoring & Intervention:** Scale how closely you watch a task to its complexity/risk:
  - **Small, well-scoped tasks:** when the host supports blocking MCP calls, await completion deterministically with `kilo_task_wait(task_id=..., timeout_seconds=...)` (returns the full `kilo_task_result` payload as soon as the task reaches a terminal state, or a clear status message with live progress on timeout without raising) instead of guessing timer intervals. Otherwise, use detached monitoring (PID watcher / `kilo_task_progress`).
  - **Large, multi-file, or high-risk tasks:** use bounded `kilo_task_wait` slices (e.g. `timeout_seconds=120`) or poll `kilo_task_progress` periodically and inspect Kilo's live checklist with `kilo_get_session_todo`. You can also directly inject or update the plan using `kilo_update_session_todo` to guide execution.
  - **A "WORKING" verdict from `kilo_task_status` is not enough on its own — always cross-check it against `kilo_task_progress`'s cost/tokens/plan before trusting it.** That heuristic proves a socket is open to the model, not that the call is converging: a task can sit "connected, long call in progress" for many minutes while burning real cost on every turn. The tell is a low CPU-time/elapsed-time ratio (seconds of CPU across many minutes of wall clock — waiting on the network, not computing) combined with a ballooning cost/token count and no recorded plan, especially after a `continue_session_id` call resuming a prior session: it usually means the task is re-reading its own accumulated context every turn instead of acting on it. Observed live: a corrective `kilo_implement` resuming a failed session ran 50+ minutes and $7+ in cost with under two minutes of CPU time and no plan ever recorded — cancelled via `kilo_task_cancel` and redone directly rather than left to keep burning. Make this cross-check routine, not something reached for only once a task already looks slow.
  - **If a task drifts off-spec or looks stuck:** `kilo_task_cancel` stops it (attempts a graceful REST abort, then hard kill by tracked PID). Alternatively, use `kilo_session_revert` to rewind to a previous checkpoint, or `kilo_session_fork` to test an alternative path in parallel. If Kilo pauses asking a question, answer with `kilo_respond_question`. Review `kilo_workspace_status` for partial changes, then either revert or continue with a corrective `kilo_implement` call using `continue_session_id` — Kilo resumes the SAME session with full memory of what it already built, instead of starting blind. If a cancelled or failed run is abandoned (`discarded`) rather than resumed, **clean up its isolated worktree and branch immediately** (`git worktree remove <path>`) so orphan worktrees do not accumulate.
  - **Don't go idle after dispatching — await completion deterministically, don't guess sleep timers.** When the host supports blocking MCP calls, prefer `kilo_task_wait(task_id=...)` to block until the task completes (reconciling orphaned processes from their log files automatically). In hosts where a long blocking MCP call isn't desirable and background shell processes notify on exit, use detached monitoring with the zero-token OS PID watcher fallback against the detached `pid` reported by `kilo_implement`: `while kill -0 <pid> 2>/dev/null; do sleep 10; done; echo "task finished"` (or poll `kilo_task_progress`) — then call `kilo_task_result(task_id)` immediately when notified.
  - **Detect Delivery via On-Branch Task Annotation:** The Executor contract mandates updating the task file to `- state: delivered` with `verification pending: <command>` directly on its worktree branch before committing. When the branch updates or the task report arrives, immediately transition the task to `delivered` in the main registry and proceed to Phase 5 Independent Verification.
  - Ready-to-invoke scripts for the manual/no-task-id case live in `scripts/` (see below) — use them instead of re-deriving the pgrep/ps/lsof/sqlite3 diagnostic pipeline from scratch each session.
- **Phase 5 - Verification & Review: NEVER trust the Final Report alone.**
  Inspect results with `kilo_workspace_status`, then actually **execute** what was built — reading the report or the diff is not verification, it's a summary. Across real delegation rounds, deviations that never surfaced from the report alone only showed up by running the code: a file committed that should have been gitignored, a cited source path that didn't exist anywhere in the codebase (invented for plausibility), a citation pointing at the right method but the wrong line, DB column types guessed from convention instead of read from the real DDL already available. Calibrate to what was built:
  - **Backend**: actually start the app against a real seeded database, don't just read the JPA annotations — a schema validation mode that passes silently is much stronger evidence than a visual read. Hit the endpoints with real `curl` calls (200/401/etc.), don't assume them from the controller code.
  - **Frontend**: actually run the build and test suite (headless browser if needed), don't stop at reading the `.ts` files.
  - **Schema/data**: if a file is claimed "copied verbatim" from a source, `diff` it for real against that source — don't trust the claim.
  - **Infra**: if there's a `docker-compose.yml`, actually bring it up, wait for the healthcheck, and query the resulting service/DB for real.
  - See the `task-spec-authoring` skill for calibrating verification depth to task risk in more detail.
  - **Verification Modes (`delivered` → `verified`):** An Executor can only
    mark a task `delivered`. Moving to `verified` follows the configured
    verification mode (`orchestration.toml` `[verification].default_mode` or
    per-task `verification-mode`):
    - `independent-session`: spawn a fresh, context-blind subagent/session
      inside the **same** framework (on the `deep-reasoning` profile) to
      re-run the `Verification` checks and inspect the diff without
      authorship bias (Single-Framework Native default).
    - `cross-agent`: delegate verification to a **different** framework/engine
      (`verifier != executor`) on the `deep-reasoning` profile (Multi-Framework
      default).
    - `panel`: run multiple independent reviewers (keeping at least one
      blind on subsequent cycles) for high-risk specs or architectural
      changes.
    - `owner`: require explicit human approval recorded in `PROGRESS.md`.
    - `self`: when no independent verifier or owner approval is used, close
      directly as `done with a caveat: self-verified only` rather than
      claiming `verified`.

  ### Post-Verification Decision Table

  After inspecting the diff and running the verification checks, take
  exactly one of these four actions:

  | Finding | Action |
  | --- | --- |
  | **All acceptance criteria pass** and diff is within scope | **Merge**: transition to `verified`, proceed to Phase 5b/6 integration merge, and close as `done`. |
  | **Trivial mechanical defect (`<= 5` lines)** — typo, missing import, misnamed local variable, zero logic/signature impact | **Fix inline**: apply the fix directly in the Executor's worktree, re-run the check, and proceed to merge. |
  | **Logic, test, or scope defect (`> 5` lines or behavioral)** where the spec itself is still sound | **Bounce to same Executor session**: transition task to `bounced`, record `B1..Bn` findings in `PROGRESS.md`, and send the exact failing criterion back to the **same** Executor session. |
  | **Ambiguous requirement, incompatible design conflict, or repeated (2+) bounce failure** | **Stop and ask user** (or escalate to `deep-reasoning` re-architecture): record the blocker in the task file and surface the concrete decision fork. |
- **Phase 5b - Verify the integration, not only the tasks.** Every task in a
  batch can be green in its own worktree and the merged result still be broken:
  tasks branched from the same commit cannot see each other (see `references/incidents.md` #6
  for the duplicate symbol collision incident). The integration gate is therefore
  not the union of the tasks' own criteria: after every merge run lint, tests,
  **and a full build of a consuming application**, and treat that as the batch's real acceptance.
- **Phase 6 - Closure & Telemetry:** Record the orchestration branch taken with `kilo_log_decision` (`delegate`, `inline`, `escalate`, or `discarded`), and if defects are found during review, log them with `kilo_log_issue` (vital for continuous prompt/specification tuning — see `kilo-mcp-metrics-analyst`) and request Kilo to fix them.
  - **Merging: always checkout the target branch explicitly first.**
    Running `git merge --no-ff <feature-branch> -m "..." <target>` without
    checking out `<target>` first is a real, observed mistake (see `references/incidents.md` #7).
    Correct sequence, every time:
    `git status --short` (clean) → `git checkout <target>` (explicit,
    never implicit) → `git merge --no-ff <feature-branch> -m "..."` →
    `git log --oneline --graph -6` (confirm the merge landed where
    intended).
  - **Worktree & Branch Lifecycle Cleanup (Every Terminal Path):** Remove
    the isolated worktree (`git worktree remove <path>`) not only after a
    successful merge, but also whenever a run is **cancelled** or
    **discarded** (`dropped`), so stale worktrees never linger on disk.

## Economic Rules and Cost Control in Orchestration

Software engineering agents operate in multi-turn loops where context accumulates rapidly (30k–200k tokens per turn). Empirical production data across 573 runs ($532 total shadow spend) proves that an unconstrained agent can burn $200+/day on routine tasks. Always enforce these **7 binding economic rules**:

1. **Incremental Dispatch, Not Scheduled Full Passes:**
   Only dispatch an agent task on a component or repository when its pinned Git commit hash has changed. Never re-scan or re-delegate across a repository corpus on a timer (a full pass on 640 repos costs ~$1,440–$2,470; commit-triggered runs reduce annual spend by 98%).
2. **Flash / Bulk Model by Default, Frontier on Demand:**
   Use the `bulk-execution` model (e.g. `Gemini 3.8 Flash`, `Claude Sonnet`, `GPT-5-Codex-Mini`) by default for implementation and exploration (~$2.25/component). Promote to `deep-reasoning` (e.g. `Gemini 4 Argon`, `Claude Opus`) only where the stronger model materially changes the verdict: independent verification of high/critical findings, complex architectural specs, and subtle semantic conflict resolution. Empirical production data shows a real-world ~1.7× cost difference per component.
3. **Maximize Implicit Cache via Byte-Stable Prompt Prefixes:**
   In real production runs, fresh (uncached) input accounted for **47% of total model spend**, while implicit caching served 89% of read tokens at 20× less cost. Keep invariant prompt text (grounding rules, methodology, templates) strictly at the beginning of prompts, byte-identical across runs. Put variable context (file paths, diffs, task details) at the very end.
4. **Session Reuse for Review → Refutation / Fixes (`continue_session_id`):**
   When an agent reviews code and then refutes or fixes findings, reuse the same session (`continue_session_id` / `--resume`) rather than spawning a fresh sandbox. Running review and refutation in separate sandboxes re-reads the entire codebase from scratch, wasting ~$67 in duplicate uncached input per 100 components.
5. **Driver-Level Budget Guardrails:**
   Monitor cumulative session cost (`kilo.db`, `kilo_metrics`) and pause dispatch when a configured daily or per-run cost ceiling is reached. Never let an autonomous loop run unmonitored.
6. **Asynchronous Batch Path for Latency-Tolerant Bulk Scans:**
   For large-scale, non-interactive sweeps, prefer asynchronous Batch APIs (-50% cost reduction). Split "bulk background scan" (batchable, asynchronous) from "interactive developer orchestration" (synchronous).
7. **Factor in Scheduled Price Step-Ups:**
   Track model pricing deadlines (e.g. `Gemini 4 Argon` doubles on 2026-12-01 to $4 in / $20 out; `Gemini 3.8 Flash` holds its promotional $0.75 in / $3.75 out rate until 2026-12-31, providing a **5.33× price advantage** during December 2026).

### Model Pricing Reference Table (USD per 1M Tokens)

| Provider & Model | Cognitive Profile | Input (Uncached) | Output + Thinking | Context Cache Read | Notes & Promo Deadlines |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **Google Gemini 4 Argon** | `deep-reasoning` / `orchestration` | **$2.00** *(thru Nov 30)* → **$4.00** *(Dec 1)* | **$10.00** *(thru Nov 30)* → **$20.00** *(Dec 1)* | **$0.10** → **$0.20** | Flagship reasoning; NOT on Free Tier; doubles 2026-12-01 |
| **Google Gemini 3.8 Flash** | `bulk-execution` / `exploration` | **$0.75** *(thru Dec 31)* → **$1.50** *(Jan 1)* | **$3.75** *(thru Dec 31)* → **$7.50** *(Jan 1)* | **$0.075** → **$0.15** | Agent workhorse; 2.67× cheaper than Argon (5.33× in Dec 2026) |
| **Anthropic Claude 3.5 Opus** | `deep-reasoning` | **$15.00** | **$75.00** | **$1.50** | 3.75×-7.5× more expensive than Argon; cache write: $18.75 |
| **Anthropic Claude 3.5 Sonnet** | `bulk-execution` / `orchestration` | **$3.00** | **$15.00** | **$0.30** | Standard workhorse; 2×-4× more expensive than Flash; cache write: $3.75 |
| **Anthropic Claude 3.5 Haiku** | `exploration` | **$0.80** *(≤128k)* | **$4.00** *(≤128k)* | **$0.08** | Near price-parity with Flash for high-volume read-only |
| **OpenAI GPT-5-Codex** | `deep-reasoning` / `orchestration` | **$5.00**–**$10.00** *(est)* | **$20.00**–**$40.00** *(est)* | **$1.25**–**$2.50** | Flagship codex engine |
| **OpenAI GPT-5-Codex-Mini** | `bulk-execution` / `exploration` | **$1.10** | **$4.40** | **$0.55** | Fast lightweight codex engine |

### Suggested Models & Effort per Activity / Mode

| Activity / Mode | Cognitive Profile | Recommended Model & Effort/Variant | Rationale |
| :--- | :--- | :--- | :--- |
| **Architect / Plan / Design** | `orchestration` / `deep-reasoning` | **Gemini 4 Argon** (`medium`/`high`) or **Claude Opus** (`high`/`xhigh`) | Error is silent: flawed architecture poisons all downstream code. |
| **Independent Verification / Code Review** | `deep-reasoning` | **Gemini 4 Argon** (`high`) or **Claude Opus** (`high`) | Must catch subtle logic/security flaws without authorship bias. |
| **Root-Cause Debug / Tricky Bug** | `deep-reasoning` | **Gemini 4 Argon** (`high`) or **Claude Opus** (`xhigh`) | Ambiguous by nature; wrong hypothesis costs hours. |
| **Semantic Conflict Resolution** | `deep-reasoning` | **Gemini 4 Argon** (`high`) or **Claude Opus** (`high`) | Design decision disguised as merge; mechanical tools compile broken logic. |
| **Bulk Implementation (Worktree)** | `bulk-execution` | **Gemini 3.8 Flash** (`medium`) or **Claude Sonnet** (`high`) | Tests and linters catch noisy errors; Flash cuts token cost by 60%–80%. |
| **Mechanical Refactor / Tests / UI** | `bulk-execution` | **Gemini 3.8 Flash** (`medium`) or **Claude Sonnet** (`medium`) | High volume, spec-bounded, automated verification. |
| **Exploration / RAG / Q&A / Commit** | `exploration` | **Gemini 3.8 Flash** (`low`) or **Claude Haiku** (`low`) | Read-only, large context, fast deterministic response. |

### Logic & Commands to Recreate and Keep the Table Updated

1. **Online Pricing Sources to Check for Rate Changes:**
   - **Google Gemini API:** [`https://ai.google.dev/gemini-api/docs/pricing`](https://ai.google.dev/gemini-api/docs/pricing)
   - **Anthropic Claude API:** [`https://www.anthropic.com/pricing`](https://www.anthropic.com/pricing)
   - **OpenAI API:** [`https://openai.com/api/pricing/`](https://openai.com/api/pricing/)
   - Comprehensive comparative report: [`docs/07-gemini-pricing-and-role-cost-analysis.md`](../../../docs/07-gemini-pricing-and-role-cost-analysis.md)
2. **Commands to Update Mappings without Manual File Edits:**
   - Update global model assignments:
     `./bin/ai-orch config set-profile <tool> <profile> --model <id> [--variant <v>] [--effort <e>]`
   - Propagate updated model matrix into the project's `AGENTS.md`:
     `./bin/ai-orch render-agents-md --apply`
   - In Kilo Code, configure `~/.config/kilo/kilo.jsonc` setting `"subagent_model": "google/gemini-flash-latest"` and `"compaction": { "model": "google/gemini-flash-latest" }` to prevent the expensive default chat model from leaking into background workers.

## Analysis/exploration requests need the same specification rigor as implementation requests

"Analyze this project" is not a complete task spec - it's exactly as
underspecified as "improve the code," and an Executor handed either will
return the cheapest thing that technically satisfies it. Observed live: a
request to analyze a project came back as a few-line document restating
what the project is about and summarizing task-progress status - content
already sitting in the README and the status file, not analysis. The
Executor didn't fail the request; the request never said what analysis
meant.

Before dispatching an analysis/exploration task (report-shaped output, no
code change), state at least:

- **Scope, explicitly bounded.** Not "the project" - name the
  subsystem(s)/directories/entry points in scope, and whether the
  boundary is static (read the source) or behavioral (also run it, hit
  its endpoints, execute its tests). An unbounded scope invites a
  README-level summary because that's the only thing that fits any
  possible boundary.
- **Depth, named on a scale, not left implicit.** "Skim and summarize" and
  "trace every call site of X across the codebase" are both legitimate
  asks, but they produce completely different reports and cost - pick one
  and say so. Silence defaults to the shallow end, every time.
- **The problem categories actually wanted**, by name: correctness bugs,
  security exposure, performance/scaling limits, architectural coupling,
  test-coverage gaps, dependency/supply-chain risk, dead code, API
  inconsistency, whatever is relevant - a generic "find issues" produces a
  generic wrap-up. This is the same negative-claim/grounding discipline
  `task-spec-authoring` requires for code-changing
  tasks, applied to findings instead of diffs: every claimed problem must
  cite the real file/line/symbol it comes from, not a generality inferred
  from the README or a directory listing.
- **The deliverable's shape.** How many findings, what evidence per
  finding (citation, reproduction, severity), whether a verdict/priority
  ranking is expected, whether comparison against a baseline/spec/other
  codebase is in scope. If you want the same structure a spec/task review
  uses (executive assessment, findings by severity, per-area
  recommendations, final verdict - see
  `task-spec-authoring`'s review format), say
  that explicitly rather than assuming the Executor will reach for it on
  its own.
- **A negative instruction against restating existing docs.** Explicitly
  tell the Executor not to reproduce content already available in the
  README/status file/docs as if it were a finding - that's the specific
  failure mode observed live, and it's cheap to rule out by name.

Verification for an analysis deliverable follows the same Phase 5
discipline as code: skim-checking the report's own claims isn't
verification. Spot-check a sample of its findings against the real
file/line it cites, the same way a code diff gets actually executed
rather than just read.

## Work that emerges mid-session: capture it, whoever will do it

New work keeps appearing while you orchestrate: the user asks for something
aside from the current task, a review finds a defect out of scope, an
Executor's report raises a follow-up, a decision waits on someone, a peer
session hands something over. Work that exists only in the conversation is
lost at the end of the session or at compaction. Nobody else can pick it up,
and it occupies your context while it waits.

- **Capture first, then continue.** As soon as the work will not be finished
  and verified in the current turn, write it down as a task in the repository's
  `tasks/` tree, following the emergent-task rules of the `task-pipeline`
  convention: a task file (a folder with its context and progress once it has
  any), a unique id, a priority with a reason, and a one-line entry in the
  registry. Then go back to what you were doing.
  The reply cites the task by id and path; a "next steps" paragraph in the chat
  is not a substitute.
- **Not only for delegation.** The same capture applies to work you will do
  yourself, and to work for a person. The executor is a field of the task, not
  a reason to skip writing it. Your own tasks go through the same states and
  are not done until their Verification has been run.
- **The task is the hand-off.** When the task is later given to an Executor
  or to a fresh session, give it the task's `handoff` line and point it at the
  task's files. Do not re-explain the task from your memory: if something is
  missing, add it to the task first.
- **Verified is not yours to set on your own work, unless the user approves.**
  Someone other than the executor marks a task verified, after re-running its
  Verification; the executor may do it only once the user has explicitly
  approved the result in conversation (record when and what in the task's
  PROGRESS.md). This applies to the tasks you executed yourself too.
- **Keep your context lean.** After capturing a task, keep only its one-line
  entry in the registry. Read its files again only when you pick it up.

## Reaching the task engine

The `tasks/` tree is plain, Git-tracked Markdown by design: every file (`00-INDEX.md`, `CONTEXT.md`, `AGENTS.md`, `NN-<slug>/plan.md`, `<category>/T###-<slug>.md`, `questions/Q###-<slug>.md`, `_inbox/`) is usable directly by any developer or AI assistant — even on a machine without the VS Code extension, the broker MCP server, or the `ai-orch` CLI installed. Use the highest tier available in the current environment, and never run the engine from a versioned plugin-cache path or a stale vendored copy:

1. **Tier 1 — Broker MCP tools (`ai-orch mcp`, preferred when registered):**
   - `whoami` — Report broker identity (`pid`, `version`, `started_at`) and `unread_mail`.
   - `workspaces.list` — List registered workspaces (`uuid`, `slug`, `title`, `path`, `orchestrator`, `unread_mail`), paged.
   - `projects.list` — List projects (`uuid`, `slug`, `path`, `kind`) in a `workspace`, paged.
   - `tasks.list` — Return compact, paged task rows for `workspace` and `project` (filterable by `state`, `kind`, `label`, `text`).
   - `tasks.show` — Return full details (`header`, `goal`, `steps`, `verification`, `children`, `attachments`, `hash`, `path`) for one task `(workspace, project, id)`.
   - `tasks.next` — Return actionable ready-queue tasks for `workspace` (and optional `project`), paged.
   - `tasks.graph` — Return the dependency graph (`nodes`, `edges`, `ready` queue) for `workspace` and `project`.
   - `tasks.new` — Create an emergent task in `(workspace, project)` (`title`, `short`, `goal`, optional `category`, `priority`, `priority_reason`, `kind`, `parent`, `idempotency_key`, `expect_hash`, `preview`).
   - `tasks.transition` — Transition task state in `(workspace, project, id, to)` (optional `note`, `idempotency_key`, `expect_hash`, `preview`).
   - `tasks.set` — Update a task field (`priority`, `size`, `deps`, `kind`, `labels`, `short`) on `(workspace, project, id)` (optional `idempotency_key`, `expect_hash`, `preview`).
   - `questions.list` — List blocking questions for `workspace` (and optional `project`, `status`), paged.
   - `questions.ask` — File a blocking question in `(workspace, project)` (`question`, `short`, `context`, `options`, optional `recommended`, `blocks`, `idempotency_key`, `expect_hash`, `preview`).
   - `questions.answer` — Record an answer to a blocking question `(workspace, project, id, choice)` and unblock waiting tasks (optional `text`, `idempotency_key`, `expect_hash`, `preview`).
   - `inbox.add_note` — Add an inbox note in `(workspace, project)` (`title`, `body`, optional `kind`, `idempotency_key`, `expect_hash`, `preview`).
   - `orchestrator.status` — Inspect workspace orchestrator status (`state`, `tool`, `model`, `generation`, `queue_length`, `worker`, `unread_mail`).
   - **Broker MCP conventions:**
     - **Addressing:** Every tool takes explicit `workspace` (slug or uuid) and, where it acts on a task tree, `project` (slug, or `_workspace` for the workspace-level coordination tree). Inside a project, task/question/note IDs are bare (`T###`, `NN.T`, `Q###`, `N###`).
     - **Mutations:** Every mutating tool (`tasks.new`, `tasks.transition`, `tasks.set`, `questions.ask`, `questions.answer`, `inbox.add_note`) accepts `expect_hash` (optimistic CAS against the hash from the last read), `idempotency_key` (safe retry returning the stored result), and `preview: true` (dry-run without writing), and returns the new `hash`.
     - **Paging:** List tools (`workspaces.list`, `projects.list`, `tasks.list`, `tasks.next`, `questions.list`) return compact rows with `cursor` / `next_cursor` (`limit` default 50, max 200; responses capped at 256 KiB with `truncated: true`); fetch full task bodies on demand with `tasks.show`.
     - **No nulls:** Tool arguments and responses never carry `null` fields (omitted instead of `null`).
   - **Setup:** The Environment Doctor (`ai-orch env status`) detects missing or stale broker MCP registrations across Claude Code, Kilo, and Codex and offers a preview/apply plan (`ai-orch env register-broker --assistants all --apply --expect-hash <hash>`). `ai-orch install-launcher` (`--apply --expect-hash <hash>`) writes the version-free launcher at `~/.local/bin/ai-orch` so MCP registrations (`~/.local/bin/ai-orch mcp`) survive plugin updates.
2. **Tier 2 — Standalone CLI (no MCP server and no VS Code extension required):**
   - When a session has no broker MCP tools, run the CLI through the version-free launcher path (`~/.local/bin/ai-orch tasks …`, `ai-orch questions …`, `ai-orch inbox …`), or invoke the bundled stdlib-only script (`python3 <skill-dir>/scripts/tasks_engine.py …`) when only `task-pipeline` is installed.
3. **Tier 3 — Direct Markdown + Git (zero install: no extension, no MCP, and no CLI installed):**
   - When neither the VS Code extension, nor the MCP server, nor the `ai-orch` CLI is installed, read, create, transition, and close tasks directly in Markdown + Git (`git mv` to `tasks/done/<category>/`) following the format rules, header field order, section skeletons, and `00-INDEX.md` table columns documented in `task-pipeline` and in `tasks/AGENTS.md`. Preserve the `<!-- ai-orch:generated ... -->` HTML comment markers in `tasks/00-INDEX.md` when updating tables by hand.
- **Citation rule:** Always cite tasks and questions as `ID (short title)`, qualified as `project/ID (short title)` across projects (see "Citing tasks and questions: id plus a short title" below).

## Citing tasks and questions: id plus a short title

The owner cannot map bare ids to work. Whenever you mention a task, plan task,
initiative, use case or question in chat, in a summary, in a Final Report or in
a question's text, write the id followed by a 3-4 word short title in
parentheses: `T007 (shared release manager)`, `12.16 (UI proposal confirm)`,
`Q007 (security-audit release)`. Across projects, qualify the id as
`project/ID (short title)` (and across workspaces as `workspace:project/ID (short title)`).
Never use a bare id, tables included (add a short-title column or put the title
next to the id).

- Take the short title from the task's `Short:` / `short:` field when it exists;
  otherwise derive 3-4 words from its full title (keep them stable within a session).
- When you create a task or question, give it a short title as well, so every
  agent cites it the same way.

## Blocking questions: never decide on the owner's behalf

When an architectural, security, scope, or financial fork requires a decision,
the orchestrator must never guess or decide on the owner's behalf.

- **File questions with `questions.ask` (or `ai-orch questions ask` fallback):** Create a question file in
  `tasks/questions/Q###-<slug>.md` using the broker MCP `questions.ask` tool (or `ai-orch questions ask` CLI fallback). Provide a
  standalone question (≤160 chars, ending with `?`), a 3-4 word `short` title, complete context,
  clear options with consequences, and link the blocked tasks (`blocks`).
  The blocked tasks automatically transition to `waiting` (`decider: Q###`).
- **File questions from executors or runs:** When an Executor or headless run
  finishes and its Final Report contains a fenced ` ```ai-orch-question ` JSON
  block, the orchestrator (or the run supervisor) files it immediately via
  `questions.ask` (or `ai-orch questions ask`), ensuring the question is recorded in `tasks/questions/`
  and the blocked task is marked `waiting`.
- **Batching rules:** Batch multiple questions together into one turn or review
  only when they are strictly independent. If question B depends on the answer
  to question A, ask question A first to avoid wasted effort and confusion.

## Scaffolding steps can write configuration for *your* harness

When a delegated task runs a project generator, review what it actually
committed file by file, not just the files you asked for. Observed live: a
mainstream framework's `create-workspace` command emitted 51 files of agent
configuration for six different AI harnesses, including a settings file for the
orchestrator's own harness that registered an external plugin marketplace,
enabled a plugin from it, and added a third-party analytics domain to the
sandbox's allowed-domains list.

Treat tool-authored harness, permission, or sandbox configuration as something
to remove, not to silently adopt: the user didn't ask for it, and you cannot
consent to a change in your own permissions on their behalf. Removing is the
conservative direction; keeping it requires asking. Then say what you removed
and why, rather than quietly cleaning up.

## Track task state as a path, not a label

"Done" is the state that lies. Keep the unified state machine below (aligned
with `task-pipeline` and `tasks_engine.py`), and let only the
orchestrator/verifier advance a task past `delivered` — an Executor may only
*deliver*:

| state | means |
| --- | --- |
| `open` | captured and specified in `tasks/`, not yet started |
| `in progress` | the Executor is actively working |
| `waiting` | waiting on a blocking question (`decider: Q###`) or external party |
| `blocked` | an unresolved upstream question or dependency makes it unspecifiable or unrunnable |
| **`delivered`** | the Executor committed, ran its self-checks, and emitted its `Final Report` (`verification pending: <what, by whom>`) |
| `bounced` | an acceptance criterion failed during verification; findings (`B1..Bn`) are logged in `PROGRESS.md` and sent back into the *same* Executor session |
| **`verified`** | an independent verifier (`verifier != executor`, such as a fresh blind subagent in `independent-session` mode or a second engine in `cross-agent` / `panel` mode) re-ran the `Verification` checks and they passed, OR the owner explicitly approved the result in `PROGRESS.md` |
| **`done`** | merged into the target branch with the post-merge integration gate (lint + tests + build) green, and moved via `git mv` to `tasks/done/<category>/` |
| **`done with a caveat`** | works and is closed, but carries a known limitation or `self-verified only` status recorded in `PROGRESS.md` |
| `dropped` | cancelled or superseded with the reason recorded and worktree cleaned up |

The rule carrying the weight: **there is no direct path from `open` or `in
progress` to `done`.** `delivered` always sits in between, and leaving it
requires independent verification (or explicit owner sign-off). Publish this
state in `tasks/00-INDEX.md` (via `tasks_engine.py`) whenever work spans
multiple tasks or sessions.

**The state follows the work, at the moment it happens** (owner rule, 2026-10-17: "chi è assegnato deve finire in progress"). Every step below is an `ai-orch tasks transition <id> <state> --actor <you> --reason "..."`, which works for emergent `T###` and plan `NN.T` tasks alike:

| When | Transition |
| --- | --- |
| You assign/dispatch a task (`kilo_implement`, a subagent, `ai-orch run start`, or you start it yourself) | `open`/`bounced` → `in progress`, **before** the executor starts; reason names the executor and the branch/worktree |
| The executor committed and returned its Final Report | `in progress` → `delivered` (reason: commit + "review pending") |
| Your review passes and it is merged | `delivered` → `verified` (independent check or owner approval) → `done` |
| Review fails | `delivered` → `bounced`, then `in progress` again when re-dispatched |
| The run died or you cancelled it, work kept on a branch | stays `in progress`; reason records where the work is |
| You abandon it | `waiting`/`blocked` with the reason, or `dropped` |

A task being worked on while still `open`, or finished while still `in progress`, is a bookkeeping bug: fix it before anything else. `ai-orch run start` performs the dispatch transition itself; for every other way of dispatching, you do.

## Tooling feedback

When you hit friction, a defect, a missing feature or a wrong default in `ai-orch`, its UI, the VS Code extension or the delegation tooling, or when a new need appears, report it at once with `ai-orch feedback`:

```bash
ai-orch feedback --kind bug|feature|idea|request --title "<short>" --body "<what, where, expected vs actual>"
```

- **Report immediately:** do not work around tooling issues silently.
- **One note per item:** capture what you ran, what you expected, and what happened.
- **No sensitive data:** never include secrets, private hostnames, or audit findings in notes.
- **Triage:** maintainers triage notes into tasks and reply with the task ID and short title.

Remember: the RAG index behind `kilo_rag_search` is a standing resource for your own exploration and Q&A too — use it even when you are not delegating anything.

**Agent Manager visibility is automatic, not something you manage.** `kilo_create_worktree` and `kilo_implement(isolation='worktree')` register the worktree — and later its session, once resolved — into `.kilo/agent-manager.json` on their own (as of 2026-07-29). You don't need to ask whether to do this, back up the file, or hand-edit it. The one caveat: a task launched before a kilo-mcp-server fix/restart doesn't benefit retroactively once it's already `completed` — if a worktree shows without a linked session, check whether it predates the current server process rather than assuming the mechanism is broken.

## Scripts

- `scripts/diagnose-kilo-tasks.sh [working_directory]` — the OS-level diagnostic used when you don't have a `task_id` (e.g. a `kilo run` process from another session/tool, or one started before this MCP server tracked tasks): lists every running `kilo run` process with elapsed/CPU time, network activity, and its matching Kilo session's last DB update, plus a WORKING/STARTING/LIKELY STUCK verdict. Equivalent to the `kilo_task_status` MCP tool, packaged as a standalone script for direct terminal use or when MCP tools aren't the right fit.
- `scripts/kill-kilo-task.sh <pid> [reason]` — cleanly stops a `kilo run` process found via the script above (SIGTERM, escalating to SIGKILL after a grace period). Use `kilo_task_cancel` instead whenever you have a `task_id` from this server — it does the same thing but keeps the task record in sync.
