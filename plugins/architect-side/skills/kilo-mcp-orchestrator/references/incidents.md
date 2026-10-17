# Incident Catalog & Empirical Failure Cases

> **Companion reference for:** `orchestration-workflow` (in `ai-orchestration`) and `kilo-mcp-orchestrator` (in `kilo-mcp`).  
> **Purpose:** Detailed operational war stories, root-cause analyses, and reproduction traces behind the imperative rules in the orchestration workflow.

---

## 1. Concurrent Git Race on Non-Isolated Working Directory (Phase 2)
- **Incident:** An orchestrator dispatched a background implementation task into the main working tree. While the background worker was actively running, the orchestrator performed its own local git operations (`git checkout`, `git reset`) in that same directory.
- **Consequence:** The branch was reset mid-run, nearly dropping in-progress work that the background worker had staged and committed.
- **Rule Enforced:** Always isolate parallel or non-trivial tasks in dedicated Git worktrees (`isolation='worktree'`). Never run manual git surgery in a directory where a delegated session is active.

---

## 2. Shared Port Collision Between Parallel Worktrees (Phase 2)
- **Incident:** Two parallel tasks were dispatched to test competing approaches on the same feature in separate worktrees. Both tasks started a development server on the default port (`:3000` / `:8765`).
- **Consequence:** The second task failed with `EADDRINUSE`, forcing a manual cancellation and restart after 10 minutes of elapsed run time.
- **Rule Enforced:** Git worktrees do **not** isolate host network ports or Docker containers. The Architect must pre-assign distinct ports (from `worker_port_range`) in the task specification before dispatching parallel server tasks.

---

## 3. Shared `.git/config` Mutation Leaking to Production Checkout (Phase 2)
- **Incident:** A lint-and-hooks task ran in an isolated worktree. As part of its setup, it ran `husky init`, which modified `core.hooksPath` inside `.git/config`.
- **Consequence:** Because `.git/config` is shared by all worktrees of a Git repository, the pre-commit hook was silently installed across the user's primary working tree, where a broken hook would have blocked real developer commits.
- **Rule Enforced:** Tasks modifying repo-level configuration must be instructed to create the configuration files and **document the activation command without running it**, and prove compliance by showing `git config --get <key>` is empty.

---

## 4. Context-Rereading Loop on Resumed Failed Sessions (Phase 4)
- **Incident:** A corrective delegation resumed a failed session via `continue_session_id`. The worker ran for 50+ minutes and accumulated over $7 in model cost, but consumed under two minutes of actual CPU time and recorded no plan.
- **Consequence:** The model was stuck in an ungrounded context-rereading loop, reading its own accumulated conversation history on every turn instead of executing code.
- **Rule Enforced:** Always cross-check an OS-level "WORKING" verdict against CPU time, token burn, and live plan progress. A low CPU/elapsed ratio combined with rising cost is a signature of a stuck session: cancel immediately and intervene.

---

## 5. Unreported Completed Task after Non-Blocking Dispatch (Phase 4)
- **Incident:** An orchestrator launched a non-blocking background task that returned immediately with a task handle. The orchestrator ended its turn without scheduling a follow-up.
- **Consequence:** The background task finished in 3 minutes, but sat unreported for over 30 minutes until the user happened to ask "how's it going?".
- **Rule Enforced:** If the host exposes a deterministic completion-wait mechanism (`kilo_task_wait`, `claude_task_wait`), await it. Otherwise, schedule a wakeup or provide the exact check command in the response.

---

## 6. Duplicate Symbol Collision on Parallel Worktrees (Phase 5b)
- **Incident:** Two tasks in a parallel batch each independently declared an identical `BBox` type in the same library.
- **Consequence:** Both worktrees had green linter and unit test suites. Even after running `git merge --no-ff`, the library's internal unit tests passed. Only running a full build of a downstream consuming application failed due to a duplicate-export conflict on the barrel file.
- **Rule Enforced:** The Phase 5b Integration Gate mandates running lint, tests, **and a full build of a consuming application** after every merge of parallel worktrees.

---

## 7. Octopus Merge Footgun via Trailing Branch Argument (Phase 6)
- **Incident:** An orchestrator attempted to merge a feature branch into `main` by running `git merge --no-ff <feature-branch> -m "..." main` while still standing on the feature branch.
- **Consequence:** Git interprets trailing branch arguments as additional branches to merge *into* the current checkout, causing an accidental merge in the wrong direction.
- **Rule Enforced:** Always follow the explicit 4-step sequence: `git status --short` → `git checkout <target>` → `git merge --no-ff <feature>` → `git log --oneline --graph -5`.
