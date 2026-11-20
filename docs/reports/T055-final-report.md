# Final Report: T055 kilo-mcp: Kilo scrive nella directory del server invece che in working_directory

- **Outcome:** delivered — synced `env["PWD"]` with `os.path.abspath(cwd)` across every subprocess launch path in `server.py`, verified with 11 new unit/integration tests and live end-to-end `kilo_implement` runs across foreground, background, and queue runner paths.
- **Files changed:**
  - `server.py`
  - `test_server.py`
  - `docs/reports/T055-final-report.md`

## Root Cause & Reproduction Evidence

### 1. Live reproduction on `main` (Kilo CLI v7.8.18)

Starting the server code from git repo `dir_a` (`os.chdir(dir_a)` and `os.environ["PWD"] = dir_a`) and invoking `kilo_implement(working_directory=dir_b, ...)` reproduced the bug on all three execution paths:

- **`background=False` (foreground):** Kilo logged `Write /tmp/kilo/t055-server-repro/dir_a/fg.txt` and declared `Outcome: success`. Result: `dir_a` contained `fg.txt`; `dir_b` was untouched.
- **`background=True` (detached Popen):** Kilo logged `Write /tmp/kilo/t055-server-repro/dir_a/bg.txt` and declared `Outcome: success`. Result: `dir_a` contained `bg.txt`; `dir_b` was untouched.
- **Queue runner (`queue="repro_q"` + `run_queue_once()`):** Kilo logged `Write /tmp/kilo/t055-server-repro/dir_a/q.txt` and declared `Outcome: success`. Result: `dir_a` contained `q.txt`; `dir_b` was untouched.

### 2. True root cause (verified empirically with `--print-logs --log-level DEBUG`)

Python's `subprocess.Popen(..., cwd=B, env=env)` and `asyncio.create_subprocess_exec(..., cwd=B, env=env)` call `chdir(B)` in the child process without updating the inherited `PWD` environment variable. Because `server.py` passed `os.environ.copy()`, the child inherited `PWD=A` from the MCP server process.

Diagnostic runs against `kilo 7.8.18` proved how Kilo (Bun) handles this:

1. **`cwd=B`, `PWD=A`, no `--dir`:**
   - Kilo first logs `message="creating instance" directory=<B>` (from OS `process.cwd()`).
   - Kilo then immediately logs a second `message="creating instance" directory=<A>` (from `process.env.PWD`) and creates the session with `directory=<A>`, executing all `Read`/`Write`/`bash` tool calls in `A` instead of `B`.
2. **`cwd=B`, `PWD=B`, no `--dir`:**
   - Kilo creates a single instance and session for `directory=<B>` and writes only to `B` (also verified for symlinked paths with trailing slash and git worktrees).
3. **`cwd=B`, `PWD` unset, no `--dir`:**
   - Kilo falls back to `process.cwd()` (`B`) and creates a single instance and session for `B`.
4. **`cwd=B`, `PWD=B`, `INIT_CWD=A`:**
   - `INIT_CWD` has no effect; Kilo operates strictly in `B`.
5. **Explicit CLI flag check (`kilo run --help`):**
   - `kilo 7.8.18` exposes `--dir <string>` (`directory to run in, path on remote server if attaching`). Testing `cwd=B, PWD=A, --dir B` also kept Kilo in `B`.
   - Syncing `env["PWD"] = os.path.abspath(cwd)` was chosen as the primary fix (rather than injecting `--dir` into every argv) because it eliminates the root POSIX mismatch for the entire child process tree (Kilo itself, nested shell commands, `git`, verification scripts), remains backward-compatible with older Kilo CLI versions lacking `--dir`, and preserves all existing argv contracts and tests without modification.

## Fixes Applied

- Added `_env_for_cwd(env, cwd)` in `server.py`, returning a shallow copy of `env` (or `os.environ`) with `PWD` set to `os.path.abspath(cwd)` (falling back to `os.getcwd()` if `cwd` is falsy), never mutating the caller's dict in place.
- Applied `_env_for_cwd` at every subprocess launch choke point and caller constructing child environments:
  - `_run_kilo` (`asyncio.create_subprocess_exec` — covers `kilo_implement(background=False)`, `kilo_rag_search`, `kilo_run_command`, `kilo_list_models`, `kilo_auth_status`, `kilo_workspace_status`, `_create_worktree`, `kilo_task_status`, `kilo_cleanup`)
  - `_spawn_kilo_background` (`subprocess.Popen` — covers immediate `kilo_implement(background=True)` and queued runs)
  - `kilo_implement` (after applying `env_vars` and after worktree `cwd` resolution)
  - `_execute_implement`
  - `_start_task_from_record` (after applying `env_overrides`, covering both immediate background and queue runner `run_queue_once()`)
  - `_spawn_queue_runner_detached` (syncing `PWD` to `runner_cwd`)
  - `kilo_rag_search`, `kilo_run_command`, `kilo_workspace_status`, `_create_worktree`
  - `_run_verify_check` (`asyncio.create_subprocess_shell`)
- Added 11 regression tests (`test_t055_*`) in `test_server.py` covering all paths (`_run_kilo`, `_spawn_kilo_background`, foreground `kilo_implement`, worktree `kilo_implement`, background `kilo_implement`, queue runner `run_queue_once`, `_spawn_queue_runner_detached`, `kilo_run_command`, `kilo_rag_search`, `_run_verify_check`, and `env_vars` preservation). Confirmed all failed against unmodified `main` before the fix and pass after the fix.

## Verification

- **Suite completa verde su alf_ubuntu:** done — `uv run --no-project --python 3.12 --with 'mcp>=2.3.0' --with pytest --with pytest-asyncio pytest -q -p no:cacheprovider test_server.py` -> `207 passed in 16.04s` (0 failed, 0 skipped; 196 existing + 11 new tests).
- **Prova reale ripetuta dall'orchestratore sul Mac (server avviato da A, file creato in B e non in A, per background False e True):** not done (Mac environment belongs to the orchestrator); equivalent live end-to-end check on `alf_ubuntu` with real `kilo 7.8.18` passed for `background=False`, `background=True`, and queue runner (`fg.txt`, `bg.txt`, `q.txt` all created in `dir_b`, `dir_a` untouched).

## Full suite

- Command: `uv run --no-project --python 3.12 --with 'mcp>=2.3.0' --with pytest --with pytest-asyncio pytest -q -p no:cacheprovider test_server.py`
- Summary: `207 passed in 16.04s`

## Deviations from the task text

- None. Note on Step 3 option choice: `kilo run --dir` exists in `kilo 7.8.18`, but fixing `env["PWD"] = os.path.abspath(cwd)` directly resolves the root cause across all Kilo CLI versions and child tools without altering command argv shapes asserted by existing tests (e.g., `test_kilo_run_command`).

## Open issues

- None.
