# Final Report: T054 kilo-mcp: 12 test rossi su Linux (dash senza exec -a, percorsi macOS)

- **Outcome:** delivered
- **Files changed:**
  - `test_server.py`
  - `docs/reports/T054-final-report.md`

## Root Causes & Diagnosis

The test suite reproduction on Linux (`alf_ubuntu`, Ubuntu 24.04 with `/bin/sh` pointing to `dash`) confirmed 12 failures out of 181 tests (12 failed, 169 passed):

1. **11 tests failed due to `/bin/sh` (dash) rejecting `exec -a`:**
   - **Captured stderr:** `sh: 1: exec: -a: not found` (exit code 127).
   - **Root cause:** `subprocess.Popen(["sh", "-c", "exec -a 'kilo run' sleep ..."])` relies on `exec -a`, a bash extension to specify `argv[0]`. On macOS, `/bin/sh` is bash 3.2, which supports `-a`. On Debian/Ubuntu, `/bin/sh` is dash, which strictly adheres to POSIX and does not support `exec -a`.
   - The shell command immediately exited with return code 127, causing mock processes to die instantly. Consequently:
     - `test_kilo_task_result_still_running`: `kilo_task_result` saw the process had already exited (`Exit Code: 127`).
     - `test_kilo_task_progress_no_session_yet`: returned `Task t2 already ended (status: failed)`.
     - `test_kilo_task_progress_full`: returned `Task t3 already ended (status: failed)`.
     - `test_kilo_task_cancel_real_process`: returned `process was already gone; marked cancelled` instead of terminating a live process.
     - `test_kilo_implement_collision_warning`: collision detection ignored the dead PID.
     - `test_kilo_implement_background_collision_warning_and_isolation_note`: dead PID was ignored.
     - `test_find_running_tasks_for_cwd_realpath_normalization`: dead PID was pruned.
     - `test_kilo_task_wait_normal_completion`: wait loop stopped immediately because the task had exited with failure.
     - `test_kilo_task_wait_timeout_does_not_raise`: task exited immediately with code 127.
     - `test_kilo_task_wait_accepts_long_timeout_and_returns_on_completion`: task exited immediately with code 127.
     - `test_t037_worktree_state_commits_ahead_task_state_and_live_progress_timeout`: returned `status=failed_with_uncommitted_work, ended=true` instead of running.

2. **1 test failed due to OS-specific path (`/Users`):**
   - **Test:** `test_external_paths_warning_ignores_url_routes`
   - **Captured error:** `AssertionError: assert 'EXTERNAL PATHS' in ''`
   - **Root cause:** `_looks_like_route(cand)` distinguishes URL routes (`/api/fleet`) from filesystem paths by checking `os.path.isdir("/" + first)`. On macOS, `/Users` is an existing directory, so `/Users/someone/.config` was identified as a filesystem path. On Linux, `/Users` does not exist on the root filesystem, so `_looks_like_route` evaluated `not os.path.isdir("/Users")` as `True`, treating `/Users/...` as a route and skipping the warning.

## Fixes Applied

1. **Portable mock process creation:**
   - Introduced `_fake_kilo_cmd(sleep_s)` in `test_server.py`.
   - If `shutil.which("bash")` is available (Linux/macOS), it invokes `bash -c "exec -a 'kilo run' sleep {sleep_s}"`.
   - If `bash` is not present, it falls back to Python: `[sys.executable, "-c", "import os; os.execvp('sleep', ['kilo run', '{sleep_s}'])"]`.
   - Replaced all invocations of `["sh", "-c", "exec -a 'kilo run' ..."]` in `mock_spawn_kilo_background`, `live_kilo_pid`, and `test_kilo_task_cancel_real_process` with `_fake_kilo_cmd(...)`.
   - Added unit test `test_fake_kilo_cmd_portability` verifying both the bash and Python fallback branches.

2. **OS-independent external path test:**
   - In `test_external_paths_warning_ignores_url_routes`, changed `/Users/someone/.config` to `/etc/someone/.config`.
   - `/etc` exists as a directory or symlink on all POSIX operating systems (Linux, macOS, BSD), ensuring consistent detection without OS dependency.

## Verification

- **La suite completa su alf_ubuntu è verde:** done.
  - Command: `uv run --no-project --python 3.12 --with "mcp>=2.0.0" --with pytest pytest -q -p no:cacheprovider test_server.py`
  - Result: `182 passed in 15.05s` (0 failed, 0 errors).
- **Il diff non contiene `skip`, `xfail` né asserzioni rimosse:** done.
  - Verified via `git diff`: no `@pytest.mark.skip`, `@pytest.mark.xfail`, or assertion removals.
- **Verifica dell'orchestratore: la suite mirata gira anche sul Mac:** ready for orchestrator verification. Both `bash -c 'exec -a ...'` and `/etc/...` are fully supported on macOS.

## Full suite

- Command: `uv run --no-project --python 3.12 --with "mcp>=2.0.0" --with pytest pytest -q -p no:cacheprovider test_server.py`
- Summary: `182 passed in 15.05s`

## Deviations from the task text

None. All steps and guidelines followed as specified.

## Open issues

None.
