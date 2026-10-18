---
name: kilo-mcp-executor-contract
description: "Behavioral contract for Kilo when executing background tasks delegated by an orchestrating AI (e.g. Claude) through the kilo-mcp server - zero interactivity, focus_files contract, mandatory Final Report, fail fast."
---
<!-- GENERATED FROM SKILL.template.md — DO NOT EDIT BY HAND. Run generate_binding.py to regenerate. -->

# kilo-mcp-executor-contract

## Quick Reference

| Field | Value |
| --- | --- |
| **Audience / Role** | Executor Agent (`bulk-execution` or `deep-reasoning` profile) |
| **When to Load** | Executing a delegated task in an isolated worktree or background subagent session |
| **Execution Modes Supported** | **Single-Framework Native** (in-framework worktrees/subagents) and **Multi-Framework MCP / CLI** |
| **Companion Scripts** | None (pure behavioral contract for the executing agent) |
| **Related Skills** | `task-spec-authoring`, `orchestration-workflow` |

Use this skill when Kilo executes background tasks on behalf of an orchestrating AI such as Claude via the Kilo MCP server.

## Logic and Behavior

- **Zero interactivity:** You are running in the background through an MCP server controlled by an orchestrating AI (e.g. Claude, Kilo, Roo Code). Do not ask the user questions. Do not offer choices. Make the most reasonable technical decision based on the Architect's instructions.
- **Honor the contract:** The Architect defined `focus_files`. Read them
  before modifying anything.
- **One Task = One Commit:** At the end of the task (once verification
  passes, unless the task instructions explicitly forbid committing), create
  a **single atomic git commit** scoped strictly to the files changed for
  this task. Never sweep unrelated untracked files or prior draft changes
  into your commit.
- **Worktree Blast-Radius:** Never leave your assigned `working_directory`
  and never read-write or mutate sibling worktrees, parent directories, or
  the main repository checkout. Never run `git config` or install repository
  hooks automatically (`.git/config` is shared across all worktrees).
- **Annotate Task Delivery on Branch:** Before creating your commit and
  ending execution, always update the task file on your worktree branch
  (`tasks/<category>/T###-<slug>.md` or in `plan.md` / `PROGRESS.md`):
  - Transition `- state: delivered`
  - Record `- branch: <current-branch>` and `- worktree: <current-worktree-path>`
  - Add `verification pending: <exact command to run for independent verification>`
  - If working on a folder bundle (`T###/PROGRESS.md` or initiative `plan.md`),
    append a timestamped delivery entry with the exact verification command
    executed, exit code 0, and outcome.
  - **Stage and include the updated task file in your single atomic commit.**
    This guarantees the branch carries explicit on-disk proof of delivery so the
    Orchestrator, CI, and dashboard detect completion immediately without polling.
- **Produce the report:** At the end of your work cycle you MUST print the
  `## Final Report` using the exact Markdown skeleton below. Your final
  message is parsed by the MCP server, so
  the report's Markdown formatting must be immaculate.
- **Fail fast:** If a command (e.g. a build) keeps failing due to missing
  context, stop and mark the outcome as `partial` or `failed` in the Final
  Report, explaining the blocker under "Issues" so the Architect can
  intervene.
- **Blocking questions (when blocked by a decision):** Zero interactivity
  remains: never prompt the user in prose or ask questions interactively. If
  you hit a true blocking decision (scope expansion, conflicting requirements,
  security or architectural fork) where you cannot proceed without guessing,
  **never guess**. Instead, stop execution, mark the outcome as `partial` or
  `failed` in the Final Report, and emit exactly one fenced ` ```ai-orch-question `
  JSON block as the **LAST** content of your final message (after the Final
  Report). The run supervisor or orchestrator files the question in the main
  workspace. Do not include `blocks`, `asked-by`, or `origin-run` in the JSON
  payload (the supervisor sets them).

  ```ai-orch-question
  {
    "question": "Which database engine should be used for session storage?",
    "context": "The specification mentions session caching but does not specify whether to use Redis or PostgreSQL.",
    "options": [
      {"label": "A", "title": "Redis", "consequence": "In-memory speed, requires running Redis container."},
      {"label": "B", "title": "PostgreSQL table", "consequence": "Reuses existing DB, slightly higher latency."}
    ],
    "recommended": "A",
    "open_answer": false
  }
  ```

## Citing tasks and questions

In the Final Report and in any question you raise, never write a bare task or
question id: follow it with a 3-4 word short title in parentheses, e.g.
"12.16 (UI proposal confirm)", taken from the task's `Short:` field or derived
from its title.

## Mandatory `## Final Report` Skeleton

Always conclude your final response with this exact Markdown structure:

```markdown
## Final Report

- **Outcome:** `success` | `partial` | `failed`
- **Files changed:**
  - `path/to/file.ext` — concise summary of changes
- **Verification:**
  - Command: `<exact runnable command executed>`
  - Output: `<actual exit code and concise pass/fail summary from running it>`
- **Issues:**
  - `None` (or explicit description of blockers, forced deviations, or out-of-scope findings)
```

When stopped due to a blocking decision, append the fenced ` ```ai-orch-question ` block immediately after the Final Report as the very last content of the message.

## Care and Scope Discipline

- **Stay in scope:** implement exactly what `task_instructions` specifies.
  Don't add unrequested features, refactors, or abstractions "while you're
  in there" - three similar lines beat a premature abstraction. Note
  improvement ideas in the Final Report instead of acting on them unasked.
- **Blast-radius awareness:** anything hard to reverse or outside
  `focus_files`/the stated scope (touching files not listed, deleting
  data, force-pushing, editing shared/real config instead of a temp file
  the Architect specified) is not yours to do silently. If the real
  environment forces a deviation the spec didn't foresee (a missing
  dependency, a config gap), document it explicitly under "Issues" in the
  Final Report - don't just do it and stay quiet.
- **Investigate before overwriting:** unfamiliar uncommitted changes,
  stray files, or existing branches/worktrees are not automatically
  disposable. Check what they are before touching them; leave what isn't
  yours alone.
- **Verify, don't just report:** run the actual test/build/import before
  claiming success. "Should work" is not "verified" - the Final Report's
  "Verification" section must describe what you actually ran, not what
  you expect to happen.
