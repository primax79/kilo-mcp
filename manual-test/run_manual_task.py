#!/usr/bin/env python3
"""Standalone reproduction of what kilo_implement does — WITHOUT going through
the MCP server at all. Use this to tell apart "the CLI/environment is slow or
hung" from "something in server.py's invocation is the problem": if this
script hangs the same way a kilo_implement call did, the cause is outside our
server (network, kilo daemon contention, provider-side); if it runs fine, the
bug is in how server.py invokes things.

It mirrors server.py's _execute_implement exactly:
  - writes task-spec.md's content to a temp .md file (the same "temp-file
    bridge" trick, to dodge ARG_MAX and give Kilo a re-readable spec)
  - builds the identical wrapper message and `kilo run` command line
  - launches it as a real subprocess (no asyncio needed for a manual test)
  - polls kilo.db (session/todo/part tables) + ps/lsof for live progress,
    exactly like kilo_task_progress / kilo_task_status do
  - prints the final exit code / stdout / stderr — the "result"

Usage:
    python3 run_manual_task.py [working_directory]

    working_directory defaults to a fresh throwaway repo under manual-test/scratch/.
    Press Ctrl+C at any time to send SIGTERM (then SIGKILL) to the Kilo process —
    the manual equivalent of kilo_task_cancel.
"""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TASK_SPEC_PATH = os.path.join(HERE, "task-spec.md")
KILO_SESSION_DB = os.path.expanduser("~/.local/share/kilo/kilo.db")

AGENT = "code"
MODEL = "google/gemini-3.5-flash"  # keep in sync with server.py's DEFAULT_MODEL


def find_session_id(cwd, started_ms, buffer_ms=5000):
    """Same matching logic as server.py's _find_session_for_task."""
    try:
        con = sqlite3.connect(f"file:{KILO_SESSION_DB}?mode=ro", uri=True, timeout=1)
        rows = con.execute(
            "SELECT s.id, p.worktree FROM session s "
            "JOIN project p ON s.project_id = p.id "
            "WHERE s.time_created >= ? ORDER BY s.time_created ASC LIMIT 50",
            (started_ms - buffer_ms,),
        ).fetchall()
        con.close()
    except Exception:
        return None
    for sid, worktree in rows:
        if worktree and os.path.realpath(worktree) == os.path.realpath(cwd):
            return sid
    return None


def read_todos(session_id):
    try:
        con = sqlite3.connect(f"file:{KILO_SESSION_DB}?mode=ro", uri=True, timeout=1)
        rows = con.execute(
            "SELECT content, status, priority FROM todo WHERE session_id = ? ORDER BY position ASC",
            (session_id,),
        ).fetchall()
        con.close()
        return rows
    except Exception:
        return []


def read_session_summary(session_id):
    try:
        con = sqlite3.connect(f"file:{KILO_SESSION_DB}?mode=ro", uri=True, timeout=1)
        row = con.execute(
            "SELECT title, cost, tokens_input, tokens_output, time_updated FROM session WHERE id = ?",
            (session_id,),
        ).fetchone()
        con.close()
        return row
    except Exception:
        return None


def process_stats(pid):
    """(elapsed_s, cpu_s, has_network) for a pid, via ps/lsof — no deps."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "etime=,time="],
            capture_output=True, text=True, timeout=5,
        ).stdout.split()
        elapsed_str, cpu_str = (out + ["0:00", "0:00"])[:2]
    except Exception:
        return None
    def parse(t):
        days = 0
        if "-" in t:
            d, t = t.split("-", 1)
            days = int(d)
        parts = [float(p) for p in t.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0.0)
        return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    net = subprocess.run(["lsof", "-a", "-p", str(pid), "-i"], capture_output=True, text=True, timeout=5).stdout
    return parse(elapsed_str), parse(cpu_str), bool(net.strip())


def main():
    cwd = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "scratch")
    if not os.path.exists(cwd):
        os.makedirs(cwd, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=cwd)
        subprocess.run(["git", "config", "user.email", "manual-test@example.com"], cwd=cwd)
        subprocess.run(["git", "config", "user.name", "Manual Test"], cwd=cwd)
        with open(os.path.join(cwd, "README.md"), "w") as f:
            f.write("# manual test scratch repo\n")
        subprocess.run(["git", "add", "README.md"], cwd=cwd)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=cwd)
    print(f"working_directory = {cwd}\n")

    with open(TASK_SPEC_PATH) as f:
        full_content = f.read()

    prompt_file = None
    proc = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(full_content)
            prompt_file = f.name

        wrapper = (
            f"The file {prompt_file} contains your full task specification from an "
            "orchestrating architect. Read it now, then execute it completely. Your "
            "final message must be the 'Final Report' described in that file."
        )
        cmd = ["kilo", "run", "--agent", AGENT, "--model", MODEL, wrapper]
        print("Command:", " ".join(cmd[:-1]), f'"{wrapper[:60]}..."\n')

        started_ms = int(time.time() * 1000)
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        print(f"Launched. pid={proc.pid} (this returned immediately — same as kilo_implement's background=true)\n")

        session_id = None
        poll_interval = 3
        elapsed = 0
        while proc.poll() is None:
            time.sleep(poll_interval)
            elapsed += poll_interval
            stats = process_stats(proc.pid)
            if session_id is None:
                session_id = find_session_id(cwd, started_ms)
            line = f"[t={elapsed:>4}s] pid={proc.pid} alive"
            if stats:
                es, cs, net = stats
                line += f" | elapsed={int(es)}s cpu={cs:.1f}s network={'yes' if net else 'no'}"
            if session_id:
                summary = read_session_summary(session_id)
                todos = read_todos(session_id)
                line += f" | session={session_id}"
                if summary:
                    title, cost, tin, tout, _ = summary
                    line += f" cost=${cost:.4f} tokens={tin}/{tout}"
                if todos:
                    done = sum(1 for _, status, _ in todos if status == "completed")
                    line += f" todos={done}/{len(todos)}"
            else:
                line += " | session=not created yet"
            print(line)

        returncode = proc.returncode
        stdout, stderr = proc.communicate()
        print(f"\n=== DONE. exit_code={returncode} ===\n")
        print("--- STDOUT ---")
        print(stdout)
        print("--- STDERR (trace) ---")
        print(stderr)

        hello_path = os.path.join(cwd, "saluto.txt")
        if os.path.exists(hello_path):
            print(f"\nsaluto.txt contents: {open(hello_path).read()!r}")
        else:
            print("\nsaluto.txt was NOT created.")

    except KeyboardInterrupt:
        print("\nCtrl+C — cancelling (manual equivalent of kilo_task_cancel)...")
        if proc and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            print(f"pid={proc.pid} terminated.")
    finally:
        if prompt_file and os.path.exists(prompt_file):
            os.remove(prompt_file)


if __name__ == "__main__":
    main()
