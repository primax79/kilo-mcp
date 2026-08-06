#!/usr/bin/env bash
# Diagnose running `kilo run` processes: elapsed vs CPU time, network activity,
# and the matching Kilo session's last DB update, with a WORKING/STARTING/LIKELY
# STUCK verdict. This is the manual/no-task-id counterpart to the kilo_task_status
# MCP tool — use it when a `kilo run` process has no known task_id (e.g. launched
# by another session or before this MCP server tracked tasks), or from a plain
# terminal without going through the MCP tool-call round trip.
#
# Usage: diagnose-kilo-tasks.sh [working_directory]
#   working_directory   optional; only report processes whose cwd matches this
#                        path exactly. Leave empty to report every `kilo run`.
set -euo pipefail

FILTER_DIR="${1:-}"
KILO_DB="${KILO_SESSION_DB:-$HOME/.local/share/kilo/kilo.db}"

# Parse ps etime/time format ([[dd-]hh:]mm:ss) into whole seconds.
parse_ps_time() {
    local t="$1" days=0
    if [[ "$t" == *-* ]]; then
        days="${t%%-*}"
        t="${t#*-}"
    fi
    IFS=: read -ra parts <<< "$t"
    while [ "${#parts[@]}" -lt 3 ]; do
        parts=("0" "${parts[@]}")
    done
    echo $(( days * 86400 + 10#${parts[0]} * 3600 + 10#${parts[1]} * 60 + 10#${parts[2]%.*} ))
}

procs=$(pgrep -fl "kilo run" || true)
if [ -z "$procs" ]; then
    echo "No 'kilo run' process is currently running."
    if [ -f "$KILO_DB" ]; then
        echo
        echo "Most recent Kilo sessions (finished work):"
        sqlite3 "file:${KILO_DB}?mode=ro" -readonly \
            "SELECT p.worktree, s.title, s.time_updated FROM session s
             JOIN project p ON s.project_id = p.id
             ORDER BY s.time_updated DESC LIMIT 3" 2>/dev/null |
        while IFS='|' read -r worktree title ts; do
            now_ms=$(( $(date +%s) * 1000 ))
            age=$(( (now_ms - ts) / 1000 ))
            echo "- [${age}s ago] ${title} (${worktree})"
        done
    fi
    exit 0
fi

echo "$procs" | while IFS=' ' read -r pid cmdline; do
    [ -z "$pid" ] && continue

    proc_cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | grep '^n' | cut -c2-)
    if [ -n "$FILTER_DIR" ] && [ "$(cd "$proc_cwd" 2>/dev/null && pwd)" != "$(cd "$FILTER_DIR" 2>/dev/null && pwd)" ]; then
        continue
    fi

    ps_out=$(ps -p "$pid" -o etime=,time= 2>/dev/null || echo "0:00 0:00")
    elapsed_str=$(echo "$ps_out" | awk '{print $1}')
    cpu_str=$(echo "$ps_out" | awk '{print $2}')

    net_count=$(lsof -a -p "$pid" -i 2>/dev/null | wc -l | tr -d ' ')
    has_network="no"
    [ "$net_count" -gt 0 ] && has_network="yes"

    elapsed_s=$(parse_ps_time "$elapsed_str")
    cpu_s=$(parse_ps_time "$cpu_str")

    session_age_s=""
    session_age="none for this run"
    if [ -f "$KILO_DB" ] && [ -n "$proc_cwd" ]; then
        real_cwd=$(cd "$proc_cwd" 2>/dev/null && pwd || echo "$proc_cwd")
        last_update=$(sqlite3 "file:${KILO_DB}?mode=ro" -readonly \
            "SELECT s.time_updated FROM session s
             JOIN project p ON s.project_id = p.id
             WHERE p.worktree = '${real_cwd}'
             ORDER BY s.time_updated DESC LIMIT 1" 2>/dev/null || true)
        if [ -n "$last_update" ]; then
            now_ms=$(( $(date +%s) * 1000 ))
            session_age_s=$(( (now_ms - last_update) / 1000 ))
            session_age="${session_age_s}s ago"
        fi
    fi

    # Same heuristic as server.py's _assess_kilo_task: a healthy run creates
    # its session within seconds and keeps network open while the model works.
    if [ -n "$session_age_s" ] && [ "$session_age_s" -lt 120 ]; then
        verdict="WORKING — session updated ${session_age_s}s ago"
    elif [ "$has_network" = "yes" ]; then
        verdict="WORKING — connected to the model API (long model call in progress)"
    elif [ "$elapsed_s" -lt 120 ]; then
        verdict="STARTING — process younger than 2 minutes, judge later"
    else
        verdict="LIKELY STUCK — running for $(( elapsed_s / 60 ))m with only ${cpu_s}s CPU, no network, no session activity"
    fi

    echo "## PID ${pid} — ${proc_cwd}"
    echo "- elapsed: ${elapsed_str} | cpu: ${cpu_str} | network: ${has_network} | session activity: ${session_age}"
    echo "- verdict: ${verdict}"
    echo "- command: $(echo "$cmdline" | cut -c1-160)"
    echo "- to stop it: kill-kilo-task.sh ${pid} \"<reason>\"  (or kilo_task_cancel if you have a task_id)"
    echo
done
