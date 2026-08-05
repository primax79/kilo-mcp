---
name: mcp-metrics-analyst
description: Evaluate kilo-mcp delegation efficiency - ROI analysis via kilo_metrics (delegation_cost_usd vs inline_estimate_usd), defect-pattern review from kilo_log_issue, and feedback-loop proposals for better task specifications. Use when asked whether delegation to Kilo is paying off.
---

# mcp-metrics-analyst

This skill guides Claude in evaluating and reporting project delegation efficiency and defect rates based on Kilo MCP telemetry.

## Logic and Behavior

- **ROI Analysis:** Call `kilo_metrics` to compare the actual delegation cost (`delegation_cost_usd`) against the hypothetical inline generation cost (`inline_estimate_usd`). Detail savings and efficiency multipliers to the user. Note that `delegation_cost_usd` includes Kilo's own execution cost (`kilo_execution_cost_usd`) when the operator pays for Kilo tokens.
- **Defect Tracking:** Review issues recorded via `kilo_log_issue`. Group them by severity and category to identify patterns.
- **Cancelled runs:** a task stopped via `kilo_task_cancel` still logs a metrics record (Kilo's subprocess ran and consumed real tokens before being killed), typically with `outcome: unknown` since no Final Report was produced. A high rate of cancellations for a given kind of task is itself a signal worth surfacing — it usually means the spec needs tighter constraints or the task is a poor fit for delegation.
- **Feedback Loop:** If recurring patterns are found (e.g., missed edge cases, styling mismatches, frequent cancellations), propose adding specific guardrails to future task specifications or creating target Kilo skills.
