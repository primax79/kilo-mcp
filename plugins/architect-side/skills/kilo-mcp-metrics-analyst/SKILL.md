---
name: kilo-mcp-metrics-analyst
description: "Evaluate kilo-mcp delegation efficiency - ROI analysis via kilo_metrics (delegation_cost_usd vs inline_estimate_usd), decision telemetry via kilo_log_decision, defect-pattern review from kilo_log_issue, and feedback-loop proposals for better task specifications. Use when asked whether delegation to Kilo is paying off."
---
<!-- GENERATED FROM SKILL.template.md — DO NOT EDIT BY HAND. Run generate_binding.py to regenerate. -->

# kilo-mcp-metrics-analyst

## Quick Reference

| Field | Value |
| --- | --- |
| **Audience / Role** | Architect / Orchestrator (`deep-reasoning` profile) |
| **When to Load** | Evaluating delegation efficiency, cancellation/bounce rates, and recurring defect patterns |
| **Execution Modes Supported** | **Single-Framework Native** (parallel native worktrees/subagents) and **Multi-Framework MCP / CLI** |
| **Companion Scripts** | `bin/ai-orch` |
| **Related Skills** | `orchestration-workflow`, `task-spec-authoring`, `conflict-resolver` |

This skill guides Claude in evaluating and reporting project delegation efficiency, decision branch telemetry, and defect rates based on Kilo MCP telemetry.

## Logic and Behavior

- **ROI Analysis:** Call `kilo_metrics` to compare the actual delegation cost (`delegation_cost_usd`) against the hypothetical inline generation cost (`inline_estimate_usd`), and inspect orchestration decision branches recorded via `kilo_log_decision` (`delegate`, `inline`, `escalate`, `discarded`, and `inline_followup_needed`). Detail savings, efficiency multipliers, and discarded-run rates to the user. Note that `delegation_cost_usd` includes Kilo's own execution cost (`kilo_execution_cost_usd`) when the operator pays for Kilo tokens; when Kilo runs at zero marginal cost, evaluate ROI in terms of Architect context/quota preservation and bounce/discard rates.
- **Defect Tracking:** Review issues recorded via `kilo_log_issue` and discarded/escalated decisions logged via `kilo_log_decision`. Group them by severity and category to identify patterns.
- **Cancelled and discarded runs:** a task stopped via `kilo_task_cancel` still logs a metrics record (Kilo's subprocess ran and consumed real tokens before being killed), typically with `outcome: unknown` since no Final Report was produced; similarly, a completed run whose output is thrown away and redone should be recorded via `kilo_log_decision(branch='discarded', ...)`. A high rate of cancellations or discarded runs for a given kind of task is itself a signal worth surfacing — it usually means the spec needs tighter constraints or the task is a poor fit for delegation.
- **Feedback Loop:** If recurring patterns are found (e.g., missed edge cases, styling mismatches, frequent cancellations, or high `inline_followup_needed` / `discarded` counts in `kilo_log_decision`), propose adding specific guardrails to future task specifications or creating target Kilo skills — see `task-spec-authoring` for how task-spec quality itself is the main lever on defect rate.
