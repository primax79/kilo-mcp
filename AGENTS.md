# AGENTS.md — kilo-mcp

MCP server (`server.py`) that lets an orchestrating AI (Claude, Kilo, Roo
Code, any MCP client) delegate implementation work to Kilo Code, plus the
Kilo-specific `architect-side`/`executor-side` skill plugins. This is the
concrete **Kilo binding** of the protocol-agnostic pattern documented in
[`ai-architect-executor`](../ai-architect-executor) — see that repo's
`AGENTS.md` before editing methodology, not just tool names, here. Full
tool reference, config, and design rationale: [`README.md`](README.md),
[`ARCHITECTURE.md`](ARCHITECTURE.md). Part of the `primax79/*` family — see
the workspace root's `AGENTS.md` (one level up) for the sibling-repo map.

## Layout

- `server.py` — the MCP server itself. All tools (`kilo_implement`,
  `kilo_task_progress`, `kilo_create_worktree`, etc.) live here.
- `test_server.py` — pytest suite (fixtures isolate `DATA_DIR`/metrics into
  `tmp_path`, mock `_run_kilo` for subprocess-level tests).
- `plugins/architect-side/`, `plugins/executor-side/` — the concrete Kilo
  skills, most of which are **generated**, not hand-authored (see below).
- `bindings/*.json` — per-skill data (real tool names, concrete examples)
  fed into the template from `ai-architect-executor` to produce the bound
  skill.
- `scripts/regenerate_bound_skills.py` — regenerates
  `orchestration-methodology`, `headless-executor-contract`,
  `conflict-resolver`, `delegation-roi-analysis` from
  `ai-architect-executor`'s templates + this repo's `bindings/*.json`.
- `scripts/generate_skill_indices.py` — regenerates every `index.json`.
- `kilo-mcp.example.toml` — documents every config key; the real
  `kilo-mcp.toml` is gitignored per-checkout.

## Mandatory rules

- **Never hand-edit a bound skill's methodology directly.** `orchestration-methodology`,
  `headless-executor-contract`, `conflict-resolver`, and
  `delegation-roi-analysis` are generated from `ai-architect-executor`'s
  `SKILL.template.md` sources. If the methodology itself needs to change,
  edit it there and re-run `scripts/regenerate_bound_skills.py` here — a
  direct edit to the generated file will be silently overwritten on the
  next regen. Binding-specific detail (real tool names, this server's own
  parameters) lives in `bindings/*.json`, not in prose you'd hand-edit.
  `task-spec-authoring`, `task-delegation`, `interactive-role-setup`,
  `mcp-orchestrator`, `mcp-metrics-analyst`, `kilo-mcp-conflict-resolver`,
  `kilo-mcp-headless-executor`, `kilo-mcp-rag-explorer` have no generated
  counterpart — edit those directly.
- **Adding/renaming a tool in `server.py`** (new `@mcp.tool()`) means
  updating: the tool's docstring (surfaced to the connected client), the
  relevant `bindings/*.json` entry if a skill references it by name, and
  `README.md`'s "Exposed MCP Tools" section.
- **Run tests before claiming a server change works.** `pytest test_server.py`
  from this directory (a `.venv` with `mcp` installed is checked in at
  `.venv/` — activate it or use `uv run --no-project --with "mcp>=2" pytest test_server.py`).
- **Skill manifest.** New/changed skills need valid YAML frontmatter
  (`name`, `description`) in `SKILL.md`.
- **Regenerate indices.** After any skill add/remove/rename, run
  `python3 scripts/generate_skill_indices.py` and commit the result.
- **Concurrency safety in `server.py` is load-bearing, not incidental.**
  Background tasks are launched fully detached (`setsid`, explicit
  `stdin=DEVNULL`, PID liveness reaped via `waitpid` before `kill(pid, 0)`)
  specifically because three real hangs were found and fixed this way (see
  README "Reliability" section) — don't simplify this back to an
  `asyncio.create_task` or drop the explicit `stdin` redirect without
  re-reading why it's there.

## Before committing

Only when explicitly asked to commit: run `pytest test_server.py`, check
`git status`/`git diff` for scope and secrets (`kilo-mcp.toml` itself is
gitignored — make sure it never gets force-added), and confirm
`index.json` was regenerated if a skill changed.
