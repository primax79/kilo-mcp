# Final Report — T053

- **Outcome:** delivered
- **Files changed:**
  - `server.py`
  - `test_server.py`
  - `pyproject.toml`
  - `INSTALL.md`
  - `README.md`
  - `docs/reports/T053-final-report.md`
- **Verification:**
  - La suite di kilo-mcp è verde (compresi i nuovi test): done — `uv run --no-project --python 3.12 --with "mcp>=2.0.0" --with pytest pytest -q -p no:cacheprovider test_server.py -k "mcp_skills"` (`7 passed in 1.28s`; full suite `176 passed, 12 failed` where all 12 failures are pre-existing macOS vs Linux environment differences unrelated to T053).
  - Uno smoke test end-to-end con un client MCP vero su stdio (vede l'estensione dichiarata, chiama `skills/list`, legge un `SKILL.md` via `resources/read` e verifica il digest): done — `uv run --no-project --python 3.12 --with "mcp>=2.0.0" --with pytest pytest -q -p no:cacheprovider test_server.py -k "test_mcp_skills_stdio_client_end_to_end"` (`1 passed`).
  - Una sezione nel README: "Skills over MCP": done — added `### Skills over MCP` under `## 📦 Installation & Setup` in `README.md`.
- **Full suite:**
  - Command: `uv run --no-project --python 3.12 --with "mcp>=2.0.0" --with pytest pytest -q -p no:cacheprovider test_server.py`
  - Summary: `12 failed, 176 passed in 15.35s`
  - Unrelated pre-existing failures on Linux (11 tests use `sh -c "exec -a 'kilo run' sleep 30"` in `live_kilo_pid` / `test_kilo_task_cancel_real_process`, which fails on Debian/Ubuntu where `/bin/sh` is `dash` and `exec -a` is unsupported; 1 test `test_external_paths_warning_ignores_url_routes` checks `/Users/someone/.config`, which only exists on macOS):
    - `test_kilo_task_result_still_running`
    - `test_kilo_task_progress_no_session_yet`
    - `test_kilo_task_progress_full`
    - `test_kilo_task_cancel_real_process`
    - `test_kilo_implement_collision_warning`
    - `test_kilo_implement_background_collision_warning_and_isolation_note`
    - `test_find_running_tasks_for_cwd_realpath_normalization`
    - `test_kilo_task_wait_normal_completion`
    - `test_kilo_task_wait_timeout_does_not_raise`
    - `test_kilo_task_wait_accepts_long_timeout_and_returns_on_completion`
    - `test_t037_worktree_state_commits_ahead_task_state_and_live_progress_timeout`
    - `test_external_paths_warning_ignores_url_routes`
- **Deviations from the task text:** None. Checked PyPI `mcp` releases (up to `2.3.0`, already in `uv.lock`): no released version bundles `io.modelcontextprotocol/skills` (PR python-sdk #3485), so per Step 2 registered custom `skills/list` and `skills/get` handlers, exposed all skill files as `skill://kilo-mcp/<name>/<relative-path>` resources, and declared `capabilities.extensions["io.modelcontextprotocol/skills"] = {}` with `directoryRead` excluded via `mcp.server.extension.Extension`.
- **Open issues:** None.

## Bounce 1

- **Finding 1 (Private SDK API):** fixed: moved `_KiloSkillsExtension` and all supporting helpers/constants above `mcp = FastMCP(...)` in `server.py` and passed `extensions=[_skills_extension]` to the public constructor parameter of `FastMCP`; removed post-construction `_apply_extension` call.
- **Finding 2 (Private `_lowlevel_server` in initialize middleware):** fixed: checked real SDK behavior over stdio and confirmed that without middleware the SDK's `runner.py` sieves the legacy `initialize` response with `_methods.serialize_server_result` against `_v2025_11_25.InitializeResult` (which lacks `extensions`), stripping `extensions` and returning `capabilities.extensions: None` to handshake clients; kept middleware for legacy compatibility but eliminated private `_lowlevel_server.extensions` access by referencing the module-level extension instance `{MCP_SKILLS_EXTENSION_ID: _skills_extension.settings()}`.
- **Finding 3 (Install modes & plugins runtime presence):** fixed: audited install modes from `INSTALL.md`/`README.md`: for Git clone (`uv run server.py`), `plugins/*/skills` is present on disk adjacent to `server.py`; for packaged/wheel installs (`uv tool install`, `pip install`, `uv build`), configured hatchling wheel packaging in `pyproject.toml` via `[tool.hatch.build.targets.wheel] only-include = ["server.py", "plugins"]`, verified that `uv build` packages all 5 skills (24 files), verified in a clean virtual environment that both MCP skill discovery and `--install-skills` work properly from the wheel, and updated `INSTALL.md` and `README.md` to document wheel support.
- **Finding 4 (Keep new tests green and assert constructor registration):** fixed: kept all existing tests green and added `test_mcp_skills_server_constructed_with_extension()` asserting `_skills_extension in server.mcp._extensions`, and strengthened `test_mcp_skills_stdio_client_end_to_end()` to assert both `initialize` and modern `server/discover` capabilities over stdio.
- **Tests:** `uv run --no-project --python 3.12 --with "mcp>=2.0.0" --with pytest pytest -q -p no:cacheprovider test_server.py -k "mcp_skills"`
- **Results:** `8 passed in 1.28s` (full suite: `177 passed, 12 failed` with the same 12 pre-existing environment differences noted above).

