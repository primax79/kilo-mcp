#!/usr/bin/env bash
# Cleanly stop a `kilo run` process found via diagnose-kilo-tasks.sh: SIGTERM,
# escalating to SIGKILL after a grace period if it hasn't exited. This is the
# manual/no-task-id counterpart to the kilo_task_cancel MCP tool — prefer
# kilo_task_cancel whenever you have a task_id, since it also keeps this
# server's task record in sync (kilo_task_result / kilo_task_progress will
# then correctly report "cancelled" instead of leaving state inconsistent).
#
# Usage: kill-kilo-task.sh <pid> [reason]
set -euo pipefail

PID="${1:?Usage: kill-kilo-task.sh <pid> [reason]}"
REASON="${2:-no reason given}"
GRACE_S="${KILO_MCP_CANCEL_GRACE_S:-2}"

if ! kill -0 "$PID" 2>/dev/null; then
    echo "PID ${PID} is already gone; nothing to do."
    exit 0
fi

echo "Sending SIGTERM to PID ${PID} (reason: ${REASON})..."
kill -TERM "$PID"
sleep "$GRACE_S"

if kill -0 "$PID" 2>/dev/null; then
    echo "Still alive after ${GRACE_S}s grace period; sending SIGKILL."
    kill -KILL "$PID" 2>/dev/null || true
    sleep 1
fi

if kill -0 "$PID" 2>/dev/null; then
    echo "WARNING: PID ${PID} is still alive — it may be unkillable (zombie/permissions)."
    exit 1
fi

echo "PID ${PID} terminated."
