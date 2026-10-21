import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
import pytest

import server

# ==============================================================================
# Fixtures and helpers
# ==============================================================================

@pytest.fixture
def fake_data_dir(tmp_path, monkeypatch):
    """Isolate metrics output to a temporary directory."""
    data_dir = tmp_path / "kilo-mcp"
    data_dir.mkdir()
    monkeypatch.setattr(server, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(server, "METRICS_FILE", str(data_dir / "metrics.jsonl"))
    monkeypatch.setattr(server, "ISSUES_FILE", str(data_dir / "issues.jsonl"))
    monkeypatch.setattr(server, "DECISIONS_FILE", str(data_dir / "decisions.jsonl"))
    return data_dir

@pytest.fixture
def mock_run_kilo_custom(monkeypatch):
    """Replaces _run_kilo with an async fake whose behavior can be customized."""
    calls = []

    class FakeRunner:
        def __init__(self):
            self.calls = calls
            self.returncode = 0
            self.stdout = "Outcome: success\nFiles changed:\n- dummy.py"
            self.stderr = ""
            self.fake_pid = 4242

        async def __call__(self, cmd, cwd, env, on_start=None):
            self.calls.append({"cmd": cmd, "cwd": cwd, "env": env})
            if on_start:
                on_start(self.fake_pid)
            return (self.returncode, self.stdout, self.stderr)

    runner = FakeRunner()
    monkeypatch.setattr(server, "_run_kilo", runner)
    return runner


@pytest.fixture
def mock_spawn_kilo_background(monkeypatch):
    """Replaces _spawn_kilo_background (the background-mode detached-process
    launcher) with a fake that never invokes the real `kilo` binary: writes
    `log_content` straight to the given log path and returns the pid of a
    real, short-lived child process — genuinely still running if `alive` is
    True, already exited if False (the default: "the task already finished
    by the time anyone asks about it")."""
    calls = []

    class FakeSpawn:
        def __init__(self):
            self.calls = calls
            self.log_content = "Outcome: success\nFiles changed:\n- dummy.py"
            self.alive = False
            self._procs = []

        def __call__(self, cmd, cwd, env, log_path):
            self.calls.append({"cmd": cmd, "cwd": cwd, "env": env, "log_path": log_path})
            with open(log_path, "w") as f:
                f.write(self.log_content)
            proc = subprocess.Popen(["sh", "-c", "exec -a 'kilo run' sleep 5" if self.alive else "exec -a 'kilo run' sleep 0"])
            if not self.alive:
                proc.wait()
            self._procs.append(proc)
            return proc.pid

    fake = FakeSpawn()
    monkeypatch.setattr(server, "_spawn_kilo_background", fake)
    yield fake
    for p in fake._procs:
        if p.poll() is None:
            try:
                p.kill()
                p.wait()
            except OSError:
                pass


@pytest.fixture
def live_kilo_pid():
    """Provides the PID of a genuinely live process whose command line matches `kilo run`."""
    proc = subprocess.Popen(["sh", "-c", "exec -a 'kilo run' sleep 30"])
    yield proc.pid
    if proc.poll() is None:
        try:
            proc.kill()
            proc.wait()
        except OSError:
            pass

# ==============================================================================
# 1. _cfg resolution order
# ==============================================================================

def test_cfg_resolution(monkeypatch):
    # env var wins over config file value
    monkeypatch.setenv("DUMMY_ENV_KEY", "env_val")
    monkeypatch.setitem(server._CONFIG, "dummy", {"key": "cfg_val"})
    assert server._cfg("DUMMY_ENV_KEY", "dummy", "key", "def_val", str) == "env_val"

    # config file wins over default
    monkeypatch.delenv("DUMMY_ENV_KEY", raising=False)
    assert server._cfg("DUMMY_ENV_KEY", "dummy", "key", "def_val", str) == "cfg_val"

    # bad cast falls back to the default (config-file value)
    monkeypatch.setitem(server._CONFIG, "dummy", {"bad_int": "not_an_int"})
    assert server._cfg("DUMMY_ENV_KEY", "dummy", "bad_int", 99, int) == 99

    # bad cast falls back to the default (env-var value)
    monkeypatch.setenv("DUMMY_ENV_KEY", "also_not_an_int")
    assert server._cfg("DUMMY_ENV_KEY", "dummy", "bad_int", 99, int) == 99
    monkeypatch.delenv("DUMMY_ENV_KEY", raising=False)
    
    # default is used when no env and no config
    monkeypatch.setattr(server, "_CONFIG", {})
    assert server._cfg("DUMMY_ENV_KEY", "dummy", "key", "def_val", str) == "def_val"

# ==============================================================================
# 2. _parse_final_report
# ==============================================================================

def test_parse_final_report():
    # Extracts outcome and changed-file list from a realistic Final Report;
    # tolerates reports with (created) annotations and backticks;
    # returns outcome=None and empty list for unstructured output.
    report = """
# Task Specification from the Orchestrating Architect
Blah blah
## Final Report (mandatory)
- **Outcome**: Partial, some tests failed.
- **Files changed**:
* `src/foo.py` (created)
- src/bar.py
* `docs/README.md`
- **Verification**: tests passed
- **Issues**: none
    """
    res = server._parse_final_report(report)
    assert res["outcome"] == "partial"
    assert res["files_changed"] == ["src/foo.py", "src/bar.py", "docs/README.md"]

    # Single-line form: "Files changed: src/foo.py"
    report2 = """
- **Outcome**: success
- **Files changed**: src/foo.py
- **Verification**: none
    """
    res2 = server._parse_final_report(report2)
    assert res2["outcome"] == "success"
    assert res2["files_changed"] == ["src/foo.py"]

    # Bare (unbulleted) filename lines after the header — the exact shape Kilo
    # produced on the first real run, which the parser originally missed.
    report4 = """
# Final Report
- **Outcome**: success, all good.
- **Files changed**:
test_server.py
- **Verification**: 13 passed
- **Issues**: none
    """
    res4 = server._parse_final_report(report4)
    assert res4["files_changed"] == ["test_server.py"]

    report3 = "Random unstructured string\nno outcome\njust text"
    res3 = server._parse_final_report(report3)
    assert res3["outcome"] is None
    assert res3["files_changed"] == []

    # Wordier phrasing between the label and the value - the previous
    # \W{0,6} pattern required the gap to be *only* non-word characters and
    # missed this outright (root cause of a real outcome=None on a clean
    # process exit, seen in production telemetry with no captured example
    # text to reproduce exactly - these are plausible variants of the same
    # failure mode, not the literal report).
    report5 = "Outcome, in the end, was a success after all.\nFiles changed: src/x.py"
    res5 = server._parse_final_report(report5)
    assert res5["outcome"] == "success"

    report6 = "**Outcome (final status)**: failed - see below\nFiles changed: none"
    res6 = server._parse_final_report(report6)
    assert res6["outcome"] == "failed"

    # Must not match across lines - a later, unrelated "success" many lines
    # down must not get attributed to an "Outcome" label near the top with
    # nothing conclusive right after it.
    report7 = "Outcome: \nFiles changed: none\n" + ("filler\n" * 5) + "the deploy was a success"
    res7 = server._parse_final_report(report7)
    assert res7["outcome"] is None

# ==============================================================================
# 3. _measure_files
# ==============================================================================

def test_measure_files(tmp_path):
    cwd = str(tmp_path)
    (tmp_path / "foo.py").write_text("a\nb\nc") # 5 chars, 3 lines? Wait, "a\nb\nc" is 5 chars, 2 newlines. count("\n")+1 = 3 lines.
    (tmp_path / "bar.py").write_text("x")       # 1 char, 1 line
    (tmp_path / "abs.py").write_text("y\nz")    # 3 chars, 2 lines
    
    files = [
        "foo.py", 
        "missing.py", 
        "bar.py", 
        str(tmp_path / "abs.py"),
        "deleted.py"
    ]
    
    res = server._measure_files(cwd, files)
    assert res["files_measured"] == 3
    assert res["code_chars"] == 5 + 1 + 3
    assert res["code_lines"] == 3 + 1 + 2

# ==============================================================================
# 4. _estimate_costs
# ==============================================================================

def test_estimate_costs():
    # delegation cost grows with spec size
    res1 = server._estimate_costs(1000, 100, 500)
    res2 = server._estimate_costs(2000, 100, 500)
    assert res2["delegation_cost_usd"] > res1["delegation_cost_usd"]
    
    # inline estimate grows with code size
    res3 = server._estimate_costs(1000, 100, 1000)
    assert res3["inline_estimate_usd"] > res1["inline_estimate_usd"]
    
    # savings = inline - delegation
    assert pytest.approx(res1["estimated_savings_usd"]) == res1["inline_estimate_usd"] - res1["delegation_cost_usd"]


def test_estimate_costs_free_kilo_by_default():
    # with default config Kilo execution is free: delegation cost is Claude-only
    res = server._estimate_costs(1000, 100, 500)
    assert res["kilo_execution_cost_usd"] == 0.0
    assert res["delegation_cost_usd"] == res["claude_cost_usd"]


def test_estimate_costs_paid_kilo(monkeypatch):
    # operators who pay for Kilo tokens see Kilo's loop in the delegation cost
    monkeypatch.setattr(server, "KILO_INPUT_PER_MTOK", 0.30)
    monkeypatch.setattr(server, "KILO_OUTPUT_PER_MTOK", 2.50)
    free = server._estimate_costs(1000, 100, 500)
    # claude part unaffected, kilo part now nonzero and included in the total
    assert free["kilo_execution_cost_usd"] > 0
    # fields are rounded independently to 4 decimals, so allow rounding slack
    assert pytest.approx(free["delegation_cost_usd"], abs=2e-4) == free["claude_cost_usd"] + free["kilo_execution_cost_usd"]
    # savings identity still holds
    assert pytest.approx(free["estimated_savings_usd"], abs=2e-4) == free["inline_estimate_usd"] - free["delegation_cost_usd"]
    # kilo execution cost scales with generated code volume
    bigger = server._estimate_costs(1000, 100, 5000)
    assert bigger["kilo_execution_cost_usd"] > free["kilo_execution_cost_usd"]

# ==============================================================================
# 5. kilo_implement
# ==============================================================================

def test_kilo_implement(tmp_path, fake_data_dir, mock_run_kilo_custom):
    cwd = str(tmp_path)
    mock_run_kilo_custom.returncode = 0
    mock_run_kilo_custom.stdout = "Outcome: success\n- **Files changed**:\n- test.py\n"
    
    res = asyncio.run(server.kilo_implement(
        task_instructions="Do the thing",
        working_directory=cwd,
        agent="code",
        model="google/gemini-3.5-flash-preview",
        focus_files=["context.py"],
        execution_hints="Hint hint",
        skills_to_load=["my-skill"],
        background=False,
    ))
    
    # Verify the fake was called
    assert len(mock_run_kilo_custom.calls) == 1
    call = mock_run_kilo_custom.calls[0]
    assert call["cwd"] == cwd
    
    # Check the command
    cmd = call["cmd"]
    assert cmd[0:5] == ["kilo", "run", "--agent", "code", "--model"]
    assert cmd[5] == "google/gemini-3.5-flash-preview"
    wrapper = cmd[6]
    assert "contains your full task specification" in wrapper
    
    # Extract the temp file path from the wrapper
    # "The file /path/to/tmp.md contains..."
    import re
    m = re.search(r"The file (/\S+) contains", wrapper)
    assert m, "Wrapper did not contain file path"
    temp_file = m.group(1)
    
    # Check temp file is deleted after call returns
    assert not os.path.exists(temp_file)
    
    # returns a string containing run_id, exit code, and parsed Final Report (trimmed default)
    assert "run_id:" in res
    assert "Exit Code: 0" in res
    assert "Outcome: success" in res
    assert "Log file:" in res
    assert "STDOUT:" not in res

    # Appends one metrics record
    with open(server.METRICS_FILE) as f:
        lines = f.readlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["outcome"] == "success"
    assert "files_changed" in record
    assert "code_lines" in record
    assert "delegation_cost_usd" in record
    assert "inline_estimate_usd" in record
    
def test_kilo_implement_file_content(tmp_path, monkeypatch, fake_data_dir):
    cwd = str(tmp_path)
    captured_content = []
    
    async def fake_run_kilo(cmd, cwd, env, on_start=None):
        # Read the file content here before it gets deleted!
        import re
        m = re.search(r"The file (/\S+) contains", cmd[-1])
        with open(m.group(1)) as f:
            captured_content.append(f.read())
        return (0, "Outcome: success\nFiles changed:\n- a.py", "")

    monkeypatch.setattr(server, "_run_kilo", fake_run_kilo)

    res = asyncio.run(server.kilo_implement(
        task_instructions="Task instructions",
        working_directory=cwd,
        focus_files=["focus.py"],
        execution_hints="Execution hints",
        skills_to_load=["Skill1"],
        background=False,
    ))
    
    assert captured_content, f"Fake not called? result: {res}"
    content = captured_content[0]
    
    # Check section order
    idx_context = content.find("## Context Files — read these FIRST")
    idx_task = content.find("## Task Instructions")
    idx_hints = content.find("## Execution Hints & Strategy")
    idx_skills = content.find("## Required Skills")
    idx_report = content.find("## Final Report (mandatory)")
    
    assert -1 < idx_context < idx_task < idx_hints < idx_skills < idx_report
    # Protected config files are announced right before the report
    idx_protected = content.find("## Protected files (never edit)")
    assert idx_skills < idx_protected < idx_report
    assert "AGENTS.md.proposed" in content

def test_kilo_implement_bad_dir():
    res = asyncio.run(server.kilo_implement(
        task_instructions="Task",
        working_directory="/does/not/exist/ever"
    ))
    assert "Error: Working directory" in res
    assert "does not exist" in res

# ==============================================================================
# 6. kilo_rag_search
# ==============================================================================

def test_kilo_rag_search(tmp_path, fake_data_dir, mock_run_kilo_custom):
    cwd = str(tmp_path)
    mock_run_kilo_custom.stdout = "Search results"
    
    res = asyncio.run(server.kilo_rag_search(
        query="find stuff",
        working_directory=cwd,
        path="src"
    ))
    
    assert len(mock_run_kilo_custom.calls) == 1
    call = mock_run_kilo_custom.calls[0]
    assert call["cwd"] == cwd
    
    cmd = call["cmd"]
    assert cmd[0:4] == ["kilo", "run", "--agent", "explore"]
    prompt = cmd[4]
    
    assert "find stuff" in prompt
    assert "src" in prompt
    
    # appends a metrics record
    with open(server.METRICS_FILE) as f:
        lines = f.readlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["tool"] == "kilo_rag_search"

def test_kilo_rag_search_bad_dir():
    res = asyncio.run(server.kilo_rag_search(
        query="Task",
        working_directory="/does/not/exist/ever"
    ))
    assert "Error: Working directory" in res
    assert "does not exist" in res

# ==============================================================================
# 7. kilo_log_issue
# ==============================================================================

def test_kilo_log_issue(fake_data_dir):
    res = asyncio.run(server.kilo_log_issue(
        run_id="abcdef",
        category=" BAD-CAT ",
        description="Exploded",
        severity=" MAJOR "
    ))
    
    with open(server.ISSUES_FILE) as f:
        lines = f.readlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["category"] == "bad-cat"
    assert record["severity"] == "major"
    assert record["run_id"] == "abcdef"
    assert "Issue logged for run abcdef" in res

# ==============================================================================
# 8. kilo_metrics
# ==============================================================================

def test_kilo_metrics(fake_data_dir):
    # With no data returns the message
    res = asyncio.run(server.kilo_metrics())
    assert "No kilo-mcp activity recorded" in res
    
    # Seed data
    import time
    from datetime import datetime, timezone
    
    now = datetime.now(timezone.utc).isoformat()
    impl_record = {
        "ts": now,
        "tool": "kilo_implement",
        "outcome": "partial",
        "files_changed": ["a.py", "b.py"],
        "code_lines": 50,
        "delegation_cost_usd": 0.05,
        "inline_estimate_usd": 0.50,
        "duration_s": 20
    }
    rag_record = {
        "ts": now,
        "tool": "kilo_rag_search",
        "duration_s": 5
    }
    server._append_jsonl(server.METRICS_FILE, impl_record)
    server._append_jsonl(server.METRICS_FILE, rag_record)
    
    issue_record = {
        "ts": now,
        "run_id": "123",
        "category": "logic-error",
        "severity": "major",
        "description": "bug"
    }
    server._append_jsonl(server.ISSUES_FILE, issue_record)
    
    res = asyncio.run(server.kilo_metrics())
    
    # The summary includes run counts, outcome counts, cost totals and the defect categories
    assert "kilo_implement: 1 runs" in res
    assert "outcomes: partial=1" in res
    assert "code generated: 50 lines" in res
    assert "$0.05" in res
    assert "$0.50" in res
    assert "kilo_rag_search: 1 runs" in res
    assert "defects logged: 1" in res
    assert "logic-error [major]: 1" in res

# ==============================================================================
# 9. _run_kilo timeout path
# ==============================================================================

def test_run_kilo_timeout(monkeypatch):
    monkeypatch.setattr(server, "KILO_TIMEOUT", 1)
    
    async def run_it():
        # use a real cheap command like sleep 5
        return await server._run_kilo(["sleep", "5"], cwd=os.getcwd(), env=os.environ.copy())
    
    returncode, stdout, stderr = asyncio.run(run_it())
    assert returncode is None
    assert "exceeded the timeout of 1s and was terminated" in stderr

# ==============================================================================
# 10. _find_kilo_files
# ==============================================================================

def test_find_kilo_files(tmp_path, monkeypatch):
    # with a fake HOME (monkeypatch env + tmp dirs), finds agent .md files under .kilo/agent/
    
    # Setup global dir
    home = tmp_path / "home"
    global_agent_dir = home / ".kilo" / "agent"
    global_agent_dir.mkdir(parents=True)
    (global_agent_dir / "global-agent.md").write_text("global agent")
    
    # Setup cwd dir
    cwd = tmp_path / "cwd"
    cwd_agent_dir = cwd / ".kilo" / "agent"
    cwd_agent_dir.mkdir(parents=True)
    (cwd_agent_dir / "local-agent.md").write_text("local agent")
    
    monkeypatch.setattr(os.path, "expanduser", lambda x: str(home) if x == "~" else x)
    monkeypatch.setattr(os, "getcwd", lambda: str(cwd))
    
    files = server._find_kilo_files("agent*/**/*.md")

    assert len(files) == 2
    assert any("global-agent.md" in f for f in files)
    assert any("local-agent.md" in f for f in files)

    # an explicit cwd overrides the process cwd (cross-project listing)
    other = tmp_path / "other_project"
    other_agent_dir = other / ".kilocode" / "agent"
    other_agent_dir.mkdir(parents=True)
    (other_agent_dir / "other-agent.md").write_text("other agent")
    files = server._find_kilo_files("agent*/**/*.md", cwd=str(other))
    assert any("other-agent.md" in f for f in files)
    assert not any("local-agent.md" in f for f in files)
    assert any("global-agent.md" in f for f in files)  # globals always included

# ==============================================================================
# 9. Orchestration tools (worktree, auth, command, workspace status)
# ==============================================================================

def test_kilo_create_worktree(tmp_path, mock_run_kilo_custom):
    cwd = str(tmp_path)
    mock_run_kilo_custom.stdout = "Preparing worktree (new branch 'feat-x')"

    res = asyncio.run(server.kilo_create_worktree(
        branch_name="feat-x", working_directory=cwd,
    ))

    call = mock_run_kilo_custom.calls[0]
    assert call["cwd"] == cwd
    assert call["cmd"] == [
        "git", "worktree", "add", "-b", "feat-x",
        os.path.join(".kilo", "worktrees", "feat-x"),
    ]
    assert "Exit code: 0" in res

    # base_branch is appended as the commit-ish
    asyncio.run(server.kilo_create_worktree(
        branch_name="feat-y", base_branch="main", working_directory=cwd,
    ))
    assert mock_run_kilo_custom.calls[1]["cmd"][-1] == "main"


def test_kilo_auth_status(mock_run_kilo_custom):
    mock_run_kilo_custom.stdout = "google: logged in"
    res = asyncio.run(server.kilo_auth_status())
    assert mock_run_kilo_custom.calls[0]["cmd"] == ["kilo", "auth", "list"]
    assert "google: logged in" in res


def test_kilo_run_command(tmp_path, mock_run_kilo_custom):
    cwd = str(tmp_path)
    res = asyncio.run(server.kilo_run_command(
        command_name="db-migrate", args="--dry-run all", working_directory=cwd,
    ))
    # custom commands go through `kilo run --command <name> [message]`,
    # NOT as free prompt text
    assert mock_run_kilo_custom.calls[0]["cmd"] == [
        "kilo", "run", "--command", "db-migrate", "--dry-run all",
    ]
    assert "Exit code: 0" in res


def test_kilo_workspace_status(tmp_path, mock_run_kilo_custom):
    cwd = str(tmp_path)
    mock_run_kilo_custom.stdout = " M server.py\n?? new_file.py\n"
    res = asyncio.run(server.kilo_workspace_status(working_directory=cwd))
    assert mock_run_kilo_custom.calls[0]["cmd"] == ["git", "status", "-s"]
    assert " M server.py" in res

    # clean tree is reported explicitly, not as empty output
    mock_run_kilo_custom.stdout = ""
    res = asyncio.run(server.kilo_workspace_status(working_directory=cwd))
    assert "clean" in res

    # git failure (e.g. not a repository) surfaces stderr
    mock_run_kilo_custom.returncode = 128
    mock_run_kilo_custom.stderr = "fatal: not a git repository"
    res = asyncio.run(server.kilo_workspace_status(working_directory=cwd))
    assert "failed" in res and "not a git repository" in res


def test_install_skills(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setattr(
        os.path, "expanduser",
        lambda p: p.replace("~", str(home)) if p.startswith("~") else p,
    )
    server.install_skills()
    out = capsys.readouterr().out
    dest = home / ".kilo" / "skills"
    # the two bundled skills ship with the repo and must be copied whole
    assert (dest / "kilo-mcp-executor-contract" / "SKILL.md").exists()
    assert (dest / "kilo-mcp-conflict-resolver" / "SKILL.md").exists()
    assert (dest / "kilo-mcp-rag-explorer" / "SKILL.md").exists()
    assert "kilo-mcp-executor-contract" in out

# ==============================================================================
# 11. kilo_task_status helpers
# ==============================================================================

def test_parse_ps_time():
    assert server._parse_ps_time("05:30") == 330.0
    assert server._parse_ps_time("01:02:03") == 3723.0
    assert server._parse_ps_time("2-01:00:00") == 2 * 86400 + 3600
    assert server._parse_ps_time("0:19.08") == pytest.approx(19.08)
    assert server._parse_ps_time("") == 0.0


def test_assess_kilo_task():
    # recent session activity always wins
    assert server._assess_kilo_task(600, 19, False, 30).startswith("WORKING")
    # no db activity but talking to the model -> long call in progress
    assert server._assess_kilo_task(600, 19, True, None).startswith("WORKING")
    # young process: too early to judge
    assert server._assess_kilo_task(60, 1, False, None).startswith("STARTING")
    # old, silent, offline: stuck (the real-world case this was built for)
    assert server._assess_kilo_task(600, 19, False, None).startswith("LIKELY STUCK")

# ==============================================================================
# 12. background kilo_implement + kilo_task_result
# ==============================================================================

def test_kilo_implement_background(tmp_path, fake_data_dir, mock_spawn_kilo_background, monkeypatch):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    mock_spawn_kilo_background.log_content = "Outcome: success\n- **Files changed**:\n- x.py\n"

    launch = asyncio.run(server.kilo_implement(
        task_instructions="Do it", working_directory=str(tmp_path),
        background=True,
    ))
    assert "task_id:" in launch and "Kilo Execution Finished" not in launch
    task_id = launch.split("task_id: ")[1].split(")")[0]
    # the record exists immediately (the detached process was already launched)
    assert os.path.exists(server._task_record_path(task_id))

    # the fake process has already exited (alive=False, the default) — this
    # is exactly the "server restarted mid-task" scenario in miniature: the
    # record still says "running" until something reads it and self-heals it
    res = asyncio.run(server.kilo_task_result(task_id=task_id))
    assert "Kilo Execution Finished" in res and "Outcome: success" in res
    # and the run was metered, same as a synchronous one
    with open(server.METRICS_FILE) as f:
        assert task_id in f.read()


def test_kilo_task_result_recovers_after_orphaned_process(tmp_path, fake_data_dir, monkeypatch):
    """The actual bug being fixed: a task record stuck at status='running'
    whose process has already exited (e.g. the MCP server that launched it
    restarted before it could finalize the record itself) must self-heal to
    a real result the next time anything reads it — not stay 'running'
    forever, which was the reported failure mode this whole mechanism
    replaces."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    os.makedirs(str(tmp_path / "tasks"), exist_ok=True)

    proc = subprocess.Popen(["sleep", "0"])
    proc.wait()  # guaranteed dead by the time we use its pid below
    dead_pid = proc.pid

    log_path = str(tmp_path / "tasks" / "orphan1.log")
    with open(log_path, "w") as f:
        f.write("Outcome: success\nFiles changed:\n- recovered.py\n")

    server._write_task_record("orphan1", {
        "status": "running",
        "started": "2026-07-14T00:00:00+00:00",
        "working_directory": str(tmp_path),
        "agent": "code",
        "model": "google/gemini-3.5-flash",
        "pid": dead_pid,
        "log_path": log_path,
    })

    res = asyncio.run(server.kilo_task_result(task_id="orphan1"))
    assert "Kilo Execution Finished" in res
    assert "recovered.py" in res

    rec = server._read_task_record("orphan1")
    assert rec["status"] == "completed"


def test_is_pid_alive_treats_unreaped_zombie_as_dead(tmp_path):
    """Real bug found during live testing: a detached child
    (start_new_session=True) is still OUR child for wait() purposes, so once
    it exits it becomes a zombie (<defunct>) until reaped — and plain
    os.kill(pid, 0) reports a zombie as 'alive' forever, which made
    kilo_task_result say a task was still running long after it had actually
    finished. _is_pid_alive must reap-and-detect it via a non-blocking
    waitpid, not just kill(pid, 0)."""
    proc = subprocess.Popen(["sleep", "0"])
    # Deliberately do NOT call proc.wait()/proc.poll() — either would reap
    # the child itself (Popen.poll() is a thin wrapper over waitpid), which
    # would hide the exact bug this test exists to catch. Just wait long
    # enough for `sleep 0` to have genuinely exited on its own, leaving a
    # real, unreaped zombie — exactly as _spawn_kilo_background's
    # fire-and-forget Popen does in production.
    time.sleep(0.5)

    # _is_pid_alive's own os.waitpid(WNOHANG) call above already reaped the
    # child via a raw syscall (bypassing Popen's internal bookkeeping) — no
    # zombie left behind, and calling proc.wait()/poll() now would raise
    # (nothing left for Popen to wait on), so there is nothing to clean up.
    assert server._is_pid_alive(proc.pid) is False


def test_kilo_task_result_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    res = asyncio.run(server.kilo_task_result(task_id="nope123"))
    assert "Unknown task_id" in res


def test_kilo_task_result_still_running(tmp_path, monkeypatch, live_kilo_pid):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    server._write_task_record("abc123", {
        "status": "running", "started": "2026-07-14T00:00:00+00:00",
        "working_directory": "/tmp/w",
        "pid": live_kilo_pid,  # a genuinely live kilo run pid
    })
    res = asyncio.run(server.kilo_task_result(task_id="abc123"))
    assert "still running" in res and "kilo_task_status" in res


# ==============================================================================
# 13. background is the default; continue_session_id steering
# ==============================================================================

def test_kilo_implement_background_is_default(tmp_path, fake_data_dir, mock_run_kilo_custom, monkeypatch):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    res = asyncio.run(server.kilo_implement(
        task_instructions="Do it", working_directory=str(tmp_path),
    ))
    assert "Task started in background" in res
    assert "task_id:" in res
    assert "Kilo Execution Finished" not in res


def test_kilo_implement_continue_session_id(tmp_path, fake_data_dir, mock_run_kilo_custom):
    cwd = str(tmp_path)
    res = asyncio.run(server.kilo_implement(
        task_instructions="Fix the bug you left",
        working_directory=cwd,
        background=False,
        continue_session_id="ses_prev123",
    ))
    assert len(mock_run_kilo_custom.calls) == 1
    cmd = mock_run_kilo_custom.calls[0]["cmd"]
    assert cmd[0:4] == ["kilo", "run", "--session", "ses_prev123"]
    # no DB lookup needed: continue_session_id is used directly
    assert "session_id: ses_prev123" in res


# ==============================================================================
# 14. Kilo session DB helpers (_find_session_for_task, _read_todos, etc.)
# ==============================================================================

def _make_fake_kilo_db(path, worktree, session_id, time_created_ms, todos=None, texts=None,
                       cost=0.01, tokens_in=100, tokens_out=50, time_updated_ms=None):
    """Build a minimal sqlite db matching the slice of Kilo's own kilo.db
    schema our helpers read, so the SQL itself gets exercised end-to-end."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT)")
    con.execute(
        "CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, title TEXT, "
        "time_created INTEGER, time_updated INTEGER, cost REAL, "
        "tokens_input INTEGER, tokens_output INTEGER)"
    )
    con.execute("CREATE TABLE todo (session_id TEXT, content TEXT, status TEXT, priority TEXT, position INTEGER)")
    con.execute("CREATE TABLE part (session_id TEXT, data TEXT, time_created INTEGER)")
    con.execute("INSERT INTO project VALUES (?, ?)", ("proj1", worktree))
    con.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, "proj1", "Test session", time_created_ms,
         time_updated_ms or time_created_ms, cost, tokens_in, tokens_out),
    )
    for i, (content, status, priority) in enumerate(todos or []):
        con.execute("INSERT INTO todo VALUES (?, ?, ?, ?, ?)", (session_id, content, status, priority, i))
    for i, text in enumerate(texts or []):
        con.execute("INSERT INTO part VALUES (?, ?, ?)",
                    (session_id, json.dumps({"type": "text", "text": text}), time_created_ms + i))
    con.commit()
    con.close()


def test_find_session_for_task(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_SESSION_CLAIMS", {})
    db_path = tmp_path / "kilo.db"
    started = "2026-07-14T10:00:00+00:00"
    started_ms = int(datetime.fromisoformat(started).timestamp() * 1000)
    _make_fake_kilo_db(str(db_path), worktree=str(tmp_path), session_id="ses_abc",
                       time_created_ms=started_ms + 1000)
    monkeypatch.setattr(server, "KILO_SESSION_DB", str(db_path))

    assert server._find_session_for_task(str(tmp_path), started, "run_a") == "ses_abc"
    # a directory that doesn't match any project's worktree finds nothing
    assert server._find_session_for_task(str(tmp_path / "other"), started, "run_b") is None


def test_find_session_for_task_matches_main_repo_root_for_worktree_cwd(tmp_path, monkeypatch):
    """Regression test for a real, systemic bug found live 2026-07-29: Kilo
    records project.worktree as the MAIN repo root, never the specific git
    worktree subdirectory kilo run actually launched in — confirmed by
    inspecting kilo.db directly for isolation='worktree' tasks. Matching
    only against the literal cwd (the worktree path) therefore NEVER
    resolved for any isolated task. This seeds the DB the way Kilo actually
    does (worktree = main repo root) and calls the session lookup with cwd
    set to a real linked worktree subdirectory."""
    monkeypatch.setattr(server, "_SESSION_CLAIMS", {})
    repo = _init_git_repo(tmp_path / "repo")
    worktree_path = os.path.join(repo, ".kilo", "worktrees", "feat-z")
    subprocess.run(
        ["git", "-C", repo, "worktree", "add", "-b", "feat-z", worktree_path],
        check=True, capture_output=True,
    )

    db_path = tmp_path / "kilo.db"
    started = "2026-07-14T10:00:00+00:00"
    started_ms = int(datetime.fromisoformat(started).timestamp() * 1000)
    # Kilo's own behavior: project.worktree is the MAIN repo, not worktree_path.
    _make_fake_kilo_db(str(db_path), worktree=repo, session_id="ses_isolated",
                       time_created_ms=started_ms + 1000)
    monkeypatch.setattr(server, "KILO_SESSION_DB", str(db_path))

    assert server._find_session_for_task(worktree_path, started, "run_iso") == "ses_isolated"


def test_find_session_for_task_dedups_concurrent_runs_against_same_repo(tmp_path, monkeypatch):
    """Regression test for a real bug found live 2026-08-19: three
    near-simultaneous kilo_implement calls against the same repo (two
    isolated in different worktrees, one not) all resolved to the identical
    session_id, because Kilo records every one of them under the same
    collapsed project.worktree row and, before this fix, nothing excluded a
    session already attributed to another run_id — matching degraded to
    'return the first/earliest row', the same row, every time. This seeds
    two distinct session rows for the same worktree, close together in
    time, and asserts two different run_ids each claim a distinct one."""
    monkeypatch.setattr(server, "_SESSION_CLAIMS", {})
    db_path = tmp_path / "kilo.db"
    started = "2026-08-19T10:00:00+00:00"
    started_ms = int(datetime.fromisoformat(started).timestamp() * 1000)
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT)")
    con.execute(
        "CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, title TEXT, "
        "time_created INTEGER, time_updated INTEGER, cost REAL, "
        "tokens_input INTEGER, tokens_output INTEGER)"
    )
    con.execute("INSERT INTO project VALUES (?, ?)", ("proj1", str(tmp_path)))
    # Two sessions, same collapsed worktree, ~200ms apart — exactly what
    # Kilo produces for two concurrent isolation='worktree' tasks.
    con.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("ses_first", "proj1", "t1", started_ms + 500, started_ms + 500, 0.01, 10, 5),
    )
    con.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("ses_second", "proj1", "t2", started_ms + 700, started_ms + 700, 0.01, 10, 5),
    )
    con.commit()
    con.close()
    monkeypatch.setattr(server, "KILO_SESSION_DB", str(db_path))

    first = server._find_session_for_task(str(tmp_path), started, "run_x")
    second = server._find_session_for_task(str(tmp_path), started, "run_y")

    assert first == "ses_first"
    assert second == "ses_second"
    assert first != second

    # Idempotent: re-resolving the SAME run_id returns its own claim, not
    # whatever the DB would now match first.
    assert server._find_session_for_task(str(tmp_path), started, "run_x") == "ses_first"


def test_read_todos_and_texts_and_summary(tmp_path, monkeypatch):
    db_path = tmp_path / "kilo.db"
    now_ms = int(time.time() * 1000)
    _make_fake_kilo_db(
        str(db_path), worktree=str(tmp_path), session_id="ses_xyz",
        time_created_ms=now_ms - 5000, time_updated_ms=now_ms,
        todos=[("step one", "completed", "high"), ("step two", "in_progress", "medium")],
        texts=["first message", "second message", "third message"],
        cost=0.0123, tokens_in=111, tokens_out=222,
    )
    monkeypatch.setattr(server, "KILO_SESSION_DB", str(db_path))

    assert server._read_todos("ses_xyz") == [
        {"content": "step one", "status": "completed", "priority": "high"},
        {"content": "step two", "status": "in_progress", "priority": "medium"},
    ]
    # most recent `limit` texts, oldest-first
    assert server._read_recent_texts("ses_xyz", limit=2) == ["second message", "third message"]

    summary = server._read_session_summary("ses_xyz")
    assert summary["title"] == "Test session"
    assert summary["cost"] == 0.0123
    assert summary["tokens_input"] == 111
    assert summary["tokens_output"] == 222

    assert server._read_todos("nonexistent") == []
    assert server._read_recent_texts("nonexistent") == []
    assert server._read_session_summary("nonexistent") is None


# ==============================================================================
# 15. kilo_task_progress
# ==============================================================================

def test_kilo_task_progress_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    res = asyncio.run(server.kilo_task_progress(task_id="nope"))
    assert "Unknown task_id" in res


def test_kilo_task_progress_already_ended(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    server._write_task_record("t1", {"status": "completed", "result": "done"})
    res = asyncio.run(server.kilo_task_progress(task_id="t1"))
    assert "already ended" in res


def test_kilo_task_progress_no_session_yet(tmp_path, monkeypatch, live_kilo_pid):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setattr(server, "KILO_SESSION_DB", str(tmp_path / "does-not-exist.db"))
    server._write_task_record("t2", {
        "status": "running", "working_directory": str(tmp_path),
        "started": "2026-07-14T00:00:00+00:00",
        "pid": live_kilo_pid,  # a genuinely live kilo run pid
    })
    res = asyncio.run(server.kilo_task_progress(task_id="t2"))
    assert "no matching Kilo session" in res


def test_kilo_task_progress_full(tmp_path, monkeypatch, live_kilo_pid):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    db_path = tmp_path / "kilo.db"
    started = "2026-07-14T10:00:00+00:00"
    started_ms = int(datetime.fromisoformat(started).timestamp() * 1000)
    now_ms = int(time.time() * 1000)
    _make_fake_kilo_db(
        str(db_path), worktree=str(tmp_path), session_id="ses_live",
        time_created_ms=started_ms + 500, time_updated_ms=now_ms,
        todos=[("do the thing", "completed", "high"), ("verify it", "in_progress", "medium")],
        texts=["still working on it"],
        cost=0.05, tokens_in=500, tokens_out=200,
    )
    monkeypatch.setattr(server, "KILO_SESSION_DB", str(db_path))
    server._write_task_record("t3", {
        "status": "running", "working_directory": str(tmp_path), "started": started,
        "pid": live_kilo_pid,  # a genuinely live kilo run pid
    })

    res = asyncio.run(server.kilo_task_progress(task_id="t3"))
    assert "ses_live" in res
    assert "[x]" in res and "do the thing" in res
    assert "[~]" in res and "verify it" in res
    assert "still working on it" in res
    assert "0.0500" in res

    # session_id gets cached on the task record so later polls skip the lookup
    rec = server._read_task_record("t3")
    assert rec["session_id"] == "ses_live"


# ==============================================================================
# 16. kilo_task_cancel
# ==============================================================================

def test_kilo_task_cancel_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    res = asyncio.run(server.kilo_task_cancel(task_id="nope"))
    assert "Unknown task_id" in res


def test_kilo_task_cancel_already_ended(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    server._write_task_record("t1", {"status": "completed"})
    res = asyncio.run(server.kilo_task_cancel(task_id="t1"))
    assert "already ended" in res


def test_kilo_task_cancel_no_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    server._write_task_record("t2", {"status": "running"})
    res = asyncio.run(server.kilo_task_cancel(task_id="t2"))
    assert "no recorded process id" in res


def test_kilo_task_cancel_process_already_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    # a pid essentially guaranteed not to exist
    server._write_task_record("t3", {"status": "running", "pid": 999999})
    res = asyncio.run(server.kilo_task_cancel(task_id="t3", reason="stale pid"))
    assert "already gone" in res
    rec = server._read_task_record("t3")
    assert rec["status"] == "cancelled"
    assert "stale pid" in rec["result"]


def test_kilo_task_cancel_real_process(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setattr(server, "_CANCEL_GRACE_S", 0.05)

    async def scenario():
        proc = await asyncio.create_subprocess_exec(
            "sh", "-c", "exec -a 'kilo run' sleep 30", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        server._write_task_record("t4", {"status": "running", "pid": proc.pid})
        res = await server.kilo_task_cancel(task_id="t4", reason="going off spec")
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)  # reap, avoid a zombie
        except asyncio.TimeoutError:
            pass
        return res, proc.pid

    res, pid = asyncio.run(scenario())
    assert "terminated" in res
    assert "going off spec" in res
    rec = server._read_task_record("t4")
    assert rec["status"] == "cancelled"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_kilo_task_cancel_ignores_dead_pid_collision(tmp_path, fake_data_dir, mock_spawn_kilo_background, monkeypatch):
    """A 'running' record whose pid is already dead (e.g. because the launch
    already finished, or the record is stale) must not be treated as a real
    collision by a subsequent kilo_implement call against the same directory."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    mock_spawn_kilo_background.alive = False  # spawned "process" exits immediately

    launch1 = asyncio.run(server.kilo_implement(task_instructions="Do it", working_directory=str(tmp_path)))
    task_id_1 = launch1.split("task_id: ")[1].split(")")[0]
    # nobody has read task_id_1 yet, so its record is still (stale) "running"
    # with an already-dead pid — a second call to the same directory must not
    # warn about a collision with it.
    launch2 = asyncio.run(server.kilo_implement(task_instructions="Do it again", working_directory=str(tmp_path)))
    assert "COLLISION WARNING" not in launch2
    assert task_id_1 not in launch2 or "COLLISION" not in launch2


# ==============================================================================
# 14. isolation='worktree' and the same-directory collision guard
# ==============================================================================

def test_create_worktree_serializes_concurrent_calls(tmp_path, monkeypatch):
    """Two concurrent _create_worktree calls against the same repo must not
    run `git worktree add` at the same moment — mirrors the mutex the real
    Kilo VS Code extension uses internally to avoid .git/index.lock races
    (discovered while reading its source; this server had no such guard)."""
    cwd = str(tmp_path)
    concurrency = {"current": 0, "max": 0}

    async def slow_run_kilo(cmd, cwd, env, on_start=None):
        concurrency["current"] += 1
        concurrency["max"] = max(concurrency["max"], concurrency["current"])
        await asyncio.sleep(0.05)
        concurrency["current"] -= 1
        return (0, "", "")

    monkeypatch.setattr(server, "_run_kilo", slow_run_kilo)

    async def scenario():
        await asyncio.gather(
            server._create_worktree(cwd, "branch-a", None, {}),
            server._create_worktree(cwd, "branch-b", None, {}),
        )

    asyncio.run(scenario())
    assert concurrency["max"] == 1


def test_kilo_implement_isolation_worktree(tmp_path, fake_data_dir, mock_run_kilo_custom):
    cwd = str(tmp_path)
    res = asyncio.run(server.kilo_implement(
        task_instructions="Do it",
        working_directory=cwd,
        isolation="worktree",
        worktree_branch="feat-z",
        background=False,
    ))

    assert len(mock_run_kilo_custom.calls) == 2
    worktree_call = mock_run_kilo_custom.calls[0]
    assert worktree_call["cwd"] == cwd
    assert worktree_call["cmd"] == [
        "git", "worktree", "add", "-b", "feat-z",
        os.path.join(".kilo", "worktrees", "feat-z"),
    ]
    kilo_call = mock_run_kilo_custom.calls[1]
    assert kilo_call["cwd"] == os.path.join(cwd, ".kilo", "worktrees", "feat-z")
    assert "Outcome: success" in res


def test_kilo_implement_isolation_worktree_default_branch_name(tmp_path, fake_data_dir, mock_run_kilo_custom):
    cwd = str(tmp_path)
    asyncio.run(server.kilo_implement(
        task_instructions="Do it", working_directory=cwd,
        isolation="worktree", background=False,
    ))
    worktree_call = mock_run_kilo_custom.calls[0]
    branch = worktree_call["cmd"][4]
    assert branch.startswith("kilo/")
    # Directory under .kilo/worktrees/ must be flat (no slash), otherwise the
    # VS Code extension flags `.kilo/worktrees/kilo` as a leftover folder.
    assert worktree_call["cmd"][-1] == os.path.join(".kilo", "worktrees", branch.replace("/", "-"))


def test_kilo_implement_isolation_worktree_base_branch(tmp_path, fake_data_dir, mock_run_kilo_custom):
    asyncio.run(server.kilo_implement(
        task_instructions="Do it", working_directory=str(tmp_path),
        isolation="worktree", worktree_branch="feat-z", worktree_base_branch="main",
        background=False,
    ))
    assert mock_run_kilo_custom.calls[0]["cmd"][-1] == "main"


def test_kilo_implement_isolation_worktree_failure(tmp_path, fake_data_dir, monkeypatch):
    async def fail_worktree(cmd, cwd, env, on_start=None):
        return (1, "", "fatal: a branch named 'dup' already exists")
    monkeypatch.setattr(server, "_run_kilo", fail_worktree)

    res = asyncio.run(server.kilo_implement(
        task_instructions="Do it", working_directory=str(tmp_path),
        isolation="worktree", worktree_branch="dup",
    ))
    assert "Error: could not create isolated worktree" in res
    assert "already exists" in res


def test_kilo_implement_collision_warning(tmp_path, fake_data_dir, mock_run_kilo_custom, monkeypatch, live_kilo_pid):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    cwd = str(tmp_path)
    server._write_task_record("other111", {
        "status": "running", "started": "2026-07-16T00:00:00+00:00",
        "working_directory": cwd, "agent": "code",
        "pid": live_kilo_pid,  # a genuinely live kilo run pid
    })

    res = asyncio.run(server.kilo_implement(
        task_instructions="Do it", working_directory=cwd, background=False,
    ))
    assert "COLLISION WARNING" in res
    assert "other111" in res


def test_kilo_implement_no_collision_warning_for_different_dir(tmp_path, fake_data_dir, mock_run_kilo_custom, monkeypatch):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    server._write_task_record("other222", {
        "status": "running", "started": "2026-07-16T00:00:00+00:00",
        "working_directory": str(other_dir), "agent": "code",
    })

    res = asyncio.run(server.kilo_implement(
        task_instructions="Do it", working_directory=str(tmp_path), background=False,
    ))
    assert "COLLISION WARNING" not in res


def test_kilo_implement_no_collision_warning_for_non_running_task(tmp_path, fake_data_dir, mock_run_kilo_custom, monkeypatch):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    cwd = str(tmp_path)
    server._write_task_record("done333", {
        "status": "completed", "started": "2026-07-16T00:00:00+00:00",
        "working_directory": cwd, "agent": "code",
    })

    res = asyncio.run(server.kilo_implement(
        task_instructions="Do it", working_directory=cwd, background=False,
    ))
    assert "COLLISION WARNING" not in res


def test_kilo_implement_isolation_bypasses_collision_warning(tmp_path, fake_data_dir, mock_run_kilo_custom, monkeypatch):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    cwd = str(tmp_path)
    server._write_task_record("other444", {
        "status": "running", "started": "2026-07-16T00:00:00+00:00",
        "working_directory": cwd, "agent": "code",
    })

    res = asyncio.run(server.kilo_implement(
        task_instructions="Do it", working_directory=cwd,
        isolation="worktree", background=False,
    ))
    assert "COLLISION WARNING" not in res


def test_kilo_implement_background_collision_warning_and_isolation_note(tmp_path, fake_data_dir, mock_spawn_kilo_background, mock_run_kilo_custom, monkeypatch, live_kilo_pid):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    cwd = str(tmp_path)
    server._write_task_record("other555", {
        "status": "running", "started": "2026-07-16T00:00:00+00:00",
        "working_directory": cwd, "agent": "code",
        "pid": live_kilo_pid,  # a genuinely live kilo run pid
    })

    launch = asyncio.run(server.kilo_implement(task_instructions="Do it", working_directory=cwd))
    assert "COLLISION WARNING" in launch
    assert "other555" in launch

    launch2 = asyncio.run(server.kilo_implement(
        task_instructions="Do it", working_directory=cwd,
        isolation="worktree", worktree_branch="feat-bg",
    ))
    assert "COLLISION WARNING" not in launch2
    assert "Isolated in worktree" in launch2
    assert "feat-bg" in launch2


def test_find_running_tasks_for_cwd_realpath_normalization(tmp_path, monkeypatch, live_kilo_pid):
    """A trailing slash or a symlinked path must still match — same-directory
    collision detection compares real paths, not raw strings."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    server._write_task_record("t1", {
        "status": "running", "working_directory": str(real_dir),
        "pid": live_kilo_pid,  # a genuinely live kilo run pid
    })

    matches = server._find_running_tasks_for_cwd(str(real_dir) + os.sep)
    assert [tid for tid, _ in matches] == ["t1"]

    matches_none = server._find_running_tasks_for_cwd(str(tmp_path / "unrelated"))
    assert matches_none == []


# ==============================================================================
# 18. Agent Manager (.kilo/agent-manager.json) integration
# ==============================================================================

def _init_git_repo(path):
    """Create a minimal real git repo with one commit at `path` (a str or
    Path), so `git worktree add` and `git rev-parse --git-common-dir` behave
    like a real repository instead of failing on a bare tmp_path."""
    path = str(path)
    subprocess.run(["git", "init", "-q", path], check=True)
    subprocess.run(["git", "-C", path, "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", path, "config", "user.name", "Test"], check=True)
    with open(os.path.join(path, "README.md"), "w") as f:
        f.write("test\n")
    subprocess.run(["git", "-C", path, "add", "README.md"], check=True)
    subprocess.run(["git", "-C", path, "commit", "-q", "-m", "init"], check=True)
    return path


def test_agent_manager_root_resolves_main_repo_for_worktree(tmp_path):
    """_agent_manager_root must resolve to the MAIN repo root even when called
    from inside a linked worktree — the Agent Manager state file always lives
    at the main repo's .kilo/, never inside a worktree (confirmed by reading
    WorktreeStateManager.ts: it's constructed once with the workspace root)."""
    repo = _init_git_repo(tmp_path / "repo")
    worktree_path = os.path.join(repo, ".kilo", "worktrees", "feat-x")
    subprocess.run(
        ["git", "-C", repo, "worktree", "add", "-b", "feat-x", worktree_path],
        check=True, capture_output=True,
    )

    root_from_main = server._agent_manager_root(repo)
    root_from_worktree = server._agent_manager_root(worktree_path)

    assert os.path.realpath(root_from_main) == os.path.realpath(repo)
    assert os.path.realpath(root_from_worktree) == os.path.realpath(repo)


def test_agent_manager_root_none_for_non_git_dir(tmp_path):
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    assert server._agent_manager_root(str(plain_dir)) is None


def test_create_worktree_registers_in_agent_manager_json(tmp_path):
    """The real _create_worktree path (real git, not mocked) must add a
    worktree entry to .kilo/agent-manager.json in the MAIN repo root, and
    return that entry's synthetic id so callers can link a session to it."""
    repo = _init_git_repo(tmp_path / "repo")

    worktree_path, returncode, _, _, wt_id = asyncio.run(
        server._create_worktree(repo, "feat-y", None, os.environ.copy())
    )

    assert returncode == 0
    assert wt_id is not None
    state_file = os.path.join(repo, ".kilo", "agent-manager.json")
    assert os.path.exists(state_file)
    with open(state_file) as f:
        data = json.load(f)
    assert data["worktrees"][wt_id]["branch"] == "feat-y"
    assert os.path.realpath(data["worktrees"][wt_id]["path"]) == os.path.realpath(worktree_path)
    assert data["worktrees"][wt_id]["parentBranch"]  # resolved from current branch
    assert wt_id in data.get("worktreeOrder", [])


def test_agent_manager_register_session_idempotent(tmp_path):
    root = str(tmp_path)
    server._agent_manager_register_session(root, "ses_abc", None)
    state_file = os.path.join(root, ".kilo", "agent-manager.json")
    with open(state_file) as f:
        first = json.load(f)
    created_at_1 = first["sessions"]["ses_abc"]["createdAt"]

    # Second call for the same session_id must not overwrite the existing entry.
    server._agent_manager_register_session(root, "ses_abc", "wt-does-not-exist")
    with open(state_file) as f:
        second = json.load(f)
    assert second["sessions"]["ses_abc"]["createdAt"] == created_at_1
    assert second["sessions"]["ses_abc"]["worktreeId"] is None


def test_agent_manager_register_session_drops_unknown_worktree_id(tmp_path):
    """A worktreeId that doesn't exist in the worktrees map must be stored as
    null rather than a dangling reference — mirrors the extension's own
    pruning of sessions referencing a deleted worktree on load."""
    root = str(tmp_path)
    server._agent_manager_register_session(root, "ses_orphan", "wt-nonexistent")
    with open(os.path.join(root, ".kilo", "agent-manager.json")) as f:
        data = json.load(f)
    assert data["sessions"]["ses_orphan"]["worktreeId"] is None


def test_agent_manager_register_session_links_by_cwd_path_match(tmp_path):
    """A session whose task reused an already-registered worktree's path as
    working_directory (no isolation on that particular call, so no explicit
    worktree_id hint) must still link to that worktree by matching cwd
    against the worktrees map — this is the actual bug seen live: a task
    that reused an existing worktree's directory left its session dangling
    under "local" in the Agent Manager UI instead of nested under the
    worktree."""
    root = str(tmp_path)
    wt_path = os.path.join(root, ".kilo", "worktrees", "feat-x")
    wt_id = server._agent_manager_register_worktree(
        root, branch="feat-x", path=wt_path, parent_branch="main"
    )

    # No worktree_id hint (simulates a call that didn't create the worktree
    # itself), but cwd matches the registered worktree's path.
    server._agent_manager_register_session(root, "ses_reused", None, cwd=wt_path)

    with open(os.path.join(root, ".kilo", "agent-manager.json")) as f:
        data = json.load(f)
    assert data["sessions"]["ses_reused"]["worktreeId"] == wt_id


def test_agent_manager_register_session_upgrades_stale_null_link(tmp_path):
    """A session first registered with worktreeId=null (e.g. before its cwd
    was known, or before the worktree existed yet) must be upgradeable to
    the real worktree link by a later call — but a session already linked
    to a real worktree must never be touched again."""
    root = str(tmp_path)
    wt_path = os.path.join(root, ".kilo", "worktrees", "feat-y")

    # First call: session registered before we know its worktree (worktreeId ends up null).
    server._agent_manager_register_session(root, "ses_late", None)
    with open(os.path.join(root, ".kilo", "agent-manager.json")) as f:
        assert json.load(f)["sessions"]["ses_late"]["worktreeId"] is None

    # Worktree registered afterwards, matching the session's actual cwd.
    wt_id = server._agent_manager_register_worktree(
        root, branch="feat-y", path=wt_path, parent_branch="main"
    )

    # Second call, now with the matching cwd: must upgrade the link.
    server._agent_manager_register_session(root, "ses_late", None, cwd=wt_path)
    with open(os.path.join(root, ".kilo", "agent-manager.json")) as f:
        data = json.load(f)
    assert data["sessions"]["ses_late"]["worktreeId"] == wt_id

    # Third call with a bogus worktree_id hint must NOT clobber the real link.
    server._agent_manager_register_session(root, "ses_late", "wt-bogus")
    with open(os.path.join(root, ".kilo", "agent-manager.json")) as f:
        assert json.load(f)["sessions"]["ses_late"]["worktreeId"] == wt_id


def test_agent_manager_note_session_skips_when_not_git_repo(tmp_path):
    """_agent_manager_note_session must be a silent no-op outside a git repo
    (e.g. a scratch working_directory) — it must never raise or create a
    stray .kilo/ directory where the Agent Manager concept doesn't apply."""
    plain_dir = tmp_path / "scratch"
    plain_dir.mkdir()
    server._agent_manager_note_session(str(plain_dir), "ses_x", None)
    assert not os.path.exists(os.path.join(str(plain_dir), ".kilo"))


def test_kilo_task_progress_registers_session_in_agent_manager(tmp_path, monkeypatch, live_kilo_pid):
    """Once kilo_task_progress resolves a session_id for a running task, it
    must also register that session into the working_directory's
    .kilo/agent-manager.json (the actual feature this section covers)."""
    repo = _init_git_repo(tmp_path / "repo")
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setattr(server, "_find_session_for_task", lambda cwd, started, run_id: "ses_progress")
    server._write_task_record("t9", {
        "status": "running", "working_directory": repo,
        "started": "2026-07-14T00:00:00+00:00",
        "pid": live_kilo_pid,  # a genuinely live kilo run pid
    })

    asyncio.run(server.kilo_task_progress(task_id="t9"))

    state_file = os.path.join(repo, ".kilo", "agent-manager.json")
    with open(state_file) as f:
        data = json.load(f)
    assert "ses_progress" in data["sessions"]
    assert data["sessions"]["ses_progress"]["worktreeId"] is None


# ---------------------------------------------------------------------------
# Model tiers config (default_model/complex_model): fallback TOML reader,
# targeted upsert writer, --configure-models interactive flow.
# ---------------------------------------------------------------------------

def test_minimal_toml_load_parses_strings_numbers_and_sections(tmp_path):
    """Regression test for a real, silent bug found live 2026-08-11: `uv run
    --no-project` (required to avoid uv trying to build this checkout as a
    package) ignores pyproject.toml's requires-python and can resolve to a
    pre-3.11 interpreter with no `tomllib` — at which point the old
    tomllib-or-{} loader silently dropped every config file setting with no
    error. _minimal_toml_load is the fallback that keeps config files working
    on those interpreters; this pins its actual parsing behavior."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '# a comment\n'
        '\n'
        '[kilo]\n'
        'timeout = 1800\n'
        'default_model = "google/gemini-3.5-flash"\n'
        'output_cost_per_mtok = 0.0\n'
        'delegation_policy = """\n'
        'line one\n'
        'line two\n'
        '"""\n'
        '\n'
        '[metrics]\n'
        'chars_per_token = 4.0\n'
    )
    cfg = server._minimal_toml_load(str(cfg_path))
    assert cfg["kilo"]["timeout"] == 1800
    assert cfg["kilo"]["default_model"] == "google/gemini-3.5-flash"
    assert cfg["kilo"]["output_cost_per_mtok"] == 0.0
    assert cfg["kilo"]["delegation_policy"] == "line one\nline two"
    assert cfg["metrics"]["chars_per_token"] == 4.0


def test_load_config_file_uses_fallback_when_tomllib_missing(tmp_path, monkeypatch):
    """_load_config_file must fall back to _minimal_toml_load (instead of
    silently returning {}) when tomllib cannot be imported."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[kilo]\ndefault_model = "google/gemini-4-argon"\n')
    monkeypatch.setattr(server, "_config_file_candidates", lambda: [str(cfg_path)])

    real_import = __import__
    def blocked_import(name, *a, **kw):
        if name == "tomllib":
            raise ImportError("simulated: no tomllib on this interpreter")
        return real_import(name, *a, **kw)
    monkeypatch.setattr("builtins.__import__", blocked_import)

    cfg = server._load_config_file()
    assert cfg["kilo"]["default_model"] == "google/gemini-4-argon"
    assert cfg["_source"] == str(cfg_path)


def test_upsert_toml_string_key_creates_file_and_section(tmp_path):
    path = str(tmp_path / "new.toml")
    server._upsert_toml_string_key(path, "kilo", "default_model", "google/gemini-3.8-flash")
    cfg = server._minimal_toml_load(path)
    assert cfg["kilo"]["default_model"] == "google/gemini-3.8-flash"


def test_upsert_toml_string_key_replaces_existing_and_preserves_rest(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[kilo]\n'
        'timeout = 1800\n'
        'default_model = "google/gemini-3.8-flash"\n'
        '\n'
        '[metrics]\n'
        'chars_per_token = 4.0\n'
    )
    server._upsert_toml_string_key(str(path), "kilo", "default_model", "google/gemini-4-argon")
    text = path.read_text()
    assert 'default_model = "google/gemini-4-argon"' in text
    assert "timeout = 1800" in text  # untouched sibling key
    assert "[metrics]" in text and "chars_per_token = 4.0" in text  # untouched section

    cfg = server._minimal_toml_load(str(path))
    assert cfg["kilo"]["default_model"] == "google/gemini-4-argon"
    assert cfg["kilo"]["timeout"] == 1800


def test_upsert_toml_string_key_adds_new_key_to_existing_section(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[kilo]\ndefault_model = "google/gemini-3.8-flash"\n')
    server._upsert_toml_string_key(str(path), "kilo", "complex_model", "google/gemini-4-argon")
    cfg = server._minimal_toml_load(str(path))
    assert cfg["kilo"]["default_model"] == "google/gemini-3.8-flash"
    assert cfg["kilo"]["complex_model"] == "google/gemini-4-argon"


def test_configure_models_interactive_writes_both_tiers(tmp_path, monkeypatch, capsys):
    """End-to-end: lists whatever `kilo models` reports (kilo-mcp never
    hardcodes a provider), takes the two tier answers, and persists them to
    the target config file so kilo_implement's tool description reflects
    them on next server start — without touching unrelated existing keys."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[kilo]\ntimeout = 1800\n')
    monkeypatch.setenv("KILO_MCP_CONFIG", str(cfg_path))
    monkeypatch.setattr(server, "_CONFIG", {})

    fake_models = "google/gemini-3.8-flash\ngoogle/gemini-4-argon\nnot-a-model-line\n"
    def fake_run(cmd, **kwargs):
        assert cmd == ["kilo", "models"]
        return subprocess.CompletedProcess(cmd, 0, stdout=fake_models, stderr="")
    monkeypatch.setattr(server.subprocess, "run", fake_run)

    answers = iter(["google/gemini-3.8-flash", "google/gemini-4-argon"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    server.configure_models_interactive()

    cfg = server._minimal_toml_load(str(cfg_path))
    assert cfg["kilo"]["default_model"] == "google/gemini-3.8-flash"
    assert cfg["kilo"]["complex_model"] == "google/gemini-4-argon"
    assert cfg["kilo"]["timeout"] == 1800  # untouched pre-existing key
    assert str(cfg_path) in capsys.readouterr().out


def test_configure_models_interactive_reprompts_on_unlisted_id(tmp_path, monkeypatch):
    """An id the user types that isn't in `kilo models`' output must be
    re-prompted, not silently accepted or written — unless explicitly
    confirmed, since it would otherwise make every kilo_implement call fail."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[kilo]\n')
    monkeypatch.setenv("KILO_MCP_CONFIG", str(cfg_path))
    monkeypatch.setattr(server, "_CONFIG", {})

    fake_models = "google/gemini-3.8-flash\n"
    monkeypatch.setattr(
        server.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=fake_models, stderr="")
    )

    # First answer for the "simple" prompt is a typo'd id -> declines to use
    # it anyway -> re-prompted -> gives the real id. Second question: blank
    # (keeps current/default).
    answers = iter(["google/gemini-typo", "n", "google/gemini-3.8-flash", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    server.configure_models_interactive()

    cfg = server._minimal_toml_load(str(cfg_path))
    assert cfg["kilo"]["default_model"] == "google/gemini-3.8-flash"


# ==============================================================================
# 19. kilo_task_wait (T001) and kilo_log_decision + kilo_metrics (T002)
# ==============================================================================

def test_kilo_task_wait_normal_completion(tmp_path, monkeypatch, live_kilo_pid):
    """kilo_task_wait polls until the task transitions to a terminal state and
    returns the kilo_task_result payload."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    server._write_task_record("wait_ok", {
        "status": "running",
        "started": "2026-10-15T00:00:00+00:00",
        "working_directory": str(tmp_path),
        "pid": live_kilo_pid,
    })

    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        server._write_task_record("wait_ok", {
            "status": "completed",
            "result": "Kilo Execution Finished.\nOutcome: success\nFiles changed:\n- waited.py",
        })

    monkeypatch.setattr(server.asyncio, "sleep", fake_sleep)

    res = asyncio.run(server.kilo_task_wait(task_id="wait_ok", timeout_seconds=10))
    assert sleep_calls == [2.0]
    assert "Kilo Execution Finished" in res
    assert "waited.py" in res


def test_kilo_task_wait_timeout_does_not_raise(tmp_path, monkeypatch, live_kilo_pid):
    """When timeout_seconds elapses while the task is still running,
    kilo_task_wait returns a status message with live progress and elapsed
    seconds instead of raising."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setattr(server, "KILO_SESSION_DB", str(tmp_path / "missing.db"))
    server._write_task_record("wait_slow", {
        "status": "running",
        "started": "2026-10-15T00:00:00+00:00",
        "working_directory": str(tmp_path),
        "pid": live_kilo_pid,
    })

    clock = {"now": 100.0}
    monkeypatch.setattr(server.time, "monotonic", lambda: clock["now"])

    async def fake_sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(server.asyncio, "sleep", fake_sleep)

    res = asyncio.run(server.kilo_task_wait(task_id="wait_slow", timeout_seconds=4))
    assert "still running" in res
    assert "4s" in res
    assert "wait_slow" in res


def test_kilo_task_wait_recovers_orphaned_process_from_log(tmp_path, fake_data_dir, monkeypatch):
    """If the task process already exited (orphaned record still marked
    'running'), kilo_task_wait reconciles it from its log file via
    _reconcile_task_if_orphaned and returns the recovered Final Report."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    os.makedirs(str(tmp_path / "tasks"), exist_ok=True)

    proc = subprocess.Popen(["sleep", "0"])
    proc.wait()
    dead_pid = proc.pid

    log_path = str(tmp_path / "tasks" / "wait_orphan.log")
    with open(log_path, "w") as f:
        f.write("Outcome: success\nFiles changed:\n- orphan_waited.py\n")

    server._write_task_record("wait_orphan", {
        "status": "running",
        "started": "2026-10-15T00:00:00+00:00",
        "working_directory": str(tmp_path),
        "agent": "code",
        "model": "google/gemini-3.5-flash",
        "pid": dead_pid,
        "log_path": log_path,
    })

    res = asyncio.run(server.kilo_task_wait(task_id="wait_orphan", timeout_seconds=5))
    assert "Kilo Execution Finished" in res
    assert "orphan_waited.py" in res
    assert server._read_task_record("wait_orphan")["status"] == "completed"


def test_kilo_log_decision_and_metrics_reporting(fake_data_dir):
    """kilo_log_decision validates branch names, persists JSONL records to
    DATA_DIR / 'decisions.jsonl', and kilo_metrics reports branch counts and
    inline_followup_needed."""
    with pytest.raises(ValueError):
        server.kilo_log_decision(branch="invalid-branch", reason="nope")

    msg1 = server.kilo_log_decision(
        branch="delegate", reason="multi-file feature", task_ref="T001", run_id="run_1"
    )
    assert "delegate" in msg1
    server.kilo_log_decision(
        branch="inline", reason="one-line typo fix", task_ref="T002", followup_needed=False
    )
    server.kilo_log_decision(
        branch="inline", reason="small edit that broke a test", task_ref="T003", followup_needed=True
    )
    server.kilo_log_decision(
        branch="escalate", reason="failed twice", task_ref="T004", run_id="run_2"
    )
    server.kilo_log_decision(
        branch="discarded", reason="wrong approach, redone", task_ref="T005", run_id="run_3"
    )

    decisions_file = fake_data_dir / "decisions.jsonl"
    assert decisions_file.exists()
    rows = [json.loads(line) for line in decisions_file.read_text().splitlines()]
    assert len(rows) == 5
    assert [r["branch"] for r in rows] == ["delegate", "inline", "inline", "escalate", "discarded"]

    res = asyncio.run(server.kilo_metrics())
    assert "decisions logged: 5" in res
    assert "delegate=1" in res
    assert "inline=2" in res
    assert "escalate=1" in res
    assert "discarded=1" in res
    assert "inline_followup_needed: 1" in res


# ==============================================================================
# 28. Lingering kilo run reaper (T012)
# ==============================================================================

def test_reap_lingering_process_after_final_report(tmp_path, fake_data_dir, monkeypatch):
    """A stub kilo process that prints a Final Report then sleeps forever is
    detected as finished; kilo_task_progress reports 'finished; process lingering
    (grace period)' during grace, and kilo_task_wait reaps the process once grace
    expires, recording status 'completed' with note 'reaped lingering process after completion'."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_kilo = bin_dir / "kilo"
    stub_kilo.write_text(f"""#!{sys.executable}
import sys
import time

print(\"\"\"# Task Execution
Working on task...
## Final Report (mandatory)
- **Outcome**: success
- **Files changed**:
- test_output.py
- **Verification**: tests passed
- **Issues**: none
\"\"\", flush=True)

while True:
    time.sleep(1)
""")
    stub_kilo.chmod(0o755)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setattr(server, "KILO_LINGER_GRACE", 0.5)

    launch = asyncio.run(server.kilo_implement(
        task_instructions="Implement feature",
        working_directory=str(tmp_path),
        background=True,
    ))
    task_id = launch.split("task_id: ")[1].split(")")[0]

    # Wait until the stub has written its output to the log
    log_file = tmp_path / "tasks" / f"{task_id}.log"
    for _ in range(50):
        if log_file.exists() and log_file.stat().st_size > 0:
            break
        time.sleep(0.02)

    # During the grace period, kilo_task_progress reports finished & lingering
    prog = asyncio.run(server.kilo_task_progress(task_id=task_id))
    assert "finished; process lingering (grace period)" in prog

    # Await completion via kilo_task_wait (timeout 5s)
    res = asyncio.run(server.kilo_task_wait(task_id=task_id, timeout_seconds=5))
    assert "Kilo Execution Finished" in res
    assert "reaped lingering process after completion" in res

    record = server._read_task_record(task_id)
    assert record["status"] == "completed"
    assert record.get("note") == "reaped lingering process after completion"
    pid = record["pid"]
    assert not server._is_pid_alive(pid), f"Process {pid} should have been terminated and reaped"


def test_stub_keeps_printing_output_is_not_reaped(tmp_path, fake_data_dir, monkeypatch):
    """A stub kilo process that keeps writing new output is NOT reaped even after
    the grace period duration has elapsed from launch, because time_since_last_output
    remains smaller than the grace window."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_kilo = bin_dir / "kilo"
    stub_kilo.write_text(f"""#!{sys.executable}
import sys
import time

print(\"\"\"# Task Execution
## Final Report
- **Outcome**: success
- **Files changed**:
- ongoing.py
\"\"\", flush=True)

# Continuously emit new output every 0.05s
for i in range(20):
    time.sleep(0.05)
    print(f"Still working on step {{i}}...", flush=True)

while True:
    time.sleep(1)
""")
    stub_kilo.chmod(0o755)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setattr(server, "KILO_LINGER_GRACE", 0.3)

    launch = asyncio.run(server.kilo_implement(
        task_instructions="Implement ongoing task",
        working_directory=str(tmp_path),
        background=True,
    ))
    task_id = launch.split("task_id: ")[1].split(")")[0]

    # Wait until the stub starts printing output
    log_file = tmp_path / "tasks" / f"{task_id}.log"
    for _ in range(50):
        if log_file.exists() and log_file.stat().st_size > 0:
            break
        time.sleep(0.02)

    # Sleep 0.35s (longer than the 0.3s grace period from launch), but the stub has been
    # actively printing output every 0.05s, so idle time is ~0.05s < 0.3s.
    time.sleep(0.35)

    rec = server._read_task_record(task_id)
    rec = server._reconcile_task_if_orphaned(task_id, rec)
    assert rec["status"] == "running", "Task should NOT be reaped while still printing output"
    pid = rec["pid"]
    assert server._is_pid_alive(pid), "Process should still be alive"

    # Clean up the running stub
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def test_reap_lingering_process_after_json_idle_event(tmp_path, fake_data_dir, monkeypatch):
    """A stub process emitting Kilo's JSON stream ending in a session.idle event
    is detected as finished and reaped after grace."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_kilo = bin_dir / "kilo"
    stub_kilo.write_text(f"""#!{sys.executable}
import sys
import time
import json

print(json.dumps({{"type": "step_start", "part": {{"id": "1"}}}}), flush=True)
print(json.dumps({{"type": "text", "part": {{"text": "Work done"}}}}), flush=True)
print(json.dumps({{"type": "session.idle", "sessionID": "ses-json-test"}}), flush=True)

while True:
    time.sleep(1)
""")
    stub_kilo.chmod(0o755)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setattr(server, "KILO_LINGER_GRACE", 0.2)

    launch = asyncio.run(server.kilo_implement(
        task_instructions="Run JSON stream task",
        working_directory=str(tmp_path),
        background=True,
    ))
    task_id = launch.split("task_id: ")[1].split(")")[0]

    log_file = tmp_path / "tasks" / f"{task_id}.log"
    for _ in range(50):
        if log_file.exists() and log_file.stat().st_size > 0:
            break
        time.sleep(0.02)

    res = asyncio.run(server.kilo_task_wait(task_id=task_id, timeout_seconds=5))
    assert "reaped lingering process after completion" in res

    record = server._read_task_record(task_id)
    assert record["status"] == "completed"
    assert not server._is_pid_alive(record["pid"])


def test_reap_lingering_process_sigkill_escalation(tmp_path, fake_data_dir, monkeypatch):
    """A lingering stub process that ignores SIGTERM escalates to SIGKILL after
    the sigkill timeout and is successfully reaped."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_kilo = bin_dir / "kilo"
    stub_kilo.write_text(f"""#!{sys.executable}
import sys
import time
import signal

signal.signal(signal.SIGTERM, signal.SIG_IGN)

print(\"\"\"# Task Execution
## Final Report
- **Outcome**: success
- **Files changed**:
- stubborn.py
\"\"\", flush=True)

while True:
    time.sleep(1)
""")
    stub_kilo.chmod(0o755)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setattr(server, "KILO_LINGER_GRACE", 0.2)
    monkeypatch.setattr(server, "_REAP_SIGKILL_TIMEOUT_S", 0.2)

    launch = asyncio.run(server.kilo_implement(
        task_instructions="Run stubborn task",
        working_directory=str(tmp_path),
        background=True,
    ))
    task_id = launch.split("task_id: ")[1].split(")")[0]

    log_file = tmp_path / "tasks" / f"{task_id}.log"
    for _ in range(50):
        if log_file.exists() and log_file.stat().st_size > 0:
            break
        time.sleep(0.02)

    res = asyncio.run(server.kilo_task_wait(task_id=task_id, timeout_seconds=5))
    assert "reaped lingering process after completion" in res

    record = server._read_task_record(task_id)
    assert record["status"] == "completed"
    assert not server._is_pid_alive(record["pid"])


def test_kilo_task_status_reaps_lingering_tasks(tmp_path, fake_data_dir, monkeypatch):
    """kilo_task_status reconciles running tasks, reaping any lingering task whose
    grace period has elapsed, so it is not reported as running."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_kilo = bin_dir / "kilo"
    stub_kilo.write_text(f"""#!{sys.executable}
import sys
import time

print(\"\"\"# Task Execution
## Final Report
- **Outcome**: success
- **Files changed**:
- file.py
\"\"\", flush=True)

while True:
    time.sleep(1)
""")
    stub_kilo.chmod(0o755)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setattr(server, "KILO_LINGER_GRACE", 0.2)

    launch = asyncio.run(server.kilo_implement(
        task_instructions="Run task for status",
        working_directory=str(tmp_path),
        background=True,
    ))
    task_id = launch.split("task_id: ")[1].split(")")[0]

    log_file = tmp_path / "tasks" / f"{task_id}.log"
    for _ in range(50):
        if log_file.exists() and log_file.stat().st_size > 0:
            break
        time.sleep(0.02)

    # Wait past grace period
    time.sleep(0.3)

    status_out = asyncio.run(server.kilo_task_status(working_directory=str(tmp_path)))
    assert "No `kilo run` process" in status_out

    rec = server._read_task_record(task_id)
    assert rec["status"] == "completed"
    assert not server._is_pid_alive(rec["pid"])


# ==============================================================================
# kilo_verify_worktree tests
# ==============================================================================

def test_kilo_verify_worktree_all_pass(tmp_path):
    repo = _init_git_repo(tmp_path / "repo")
    res_str = asyncio.run(server.kilo_verify_worktree(
        working_directory=repo,
        commands=["echo check1", "echo check2"],
    ))
    res = json.loads(res_str)
    assert res["ok"] is True
    assert res["working_directory"] == repo
    assert res["head"] is not None and len(res["head"]) == 40
    assert res["dirty"] is False
    assert len(res["checks"]) == 2
    for check in res["checks"]:
        assert check["passed"] is True
        assert check["exit_code"] == 0
        assert check["timed_out"] is False
        assert check["output_tail"] == ""
        assert check["duration_s"] >= 0.0


def test_kilo_verify_worktree_one_failing(tmp_path):
    repo = _init_git_repo(tmp_path / "repo")
    # Make worktree dirty by writing an untracked file
    with open(os.path.join(repo, "dirty.txt"), "w") as f:
        f.write("uncommitted")

    res_str = asyncio.run(server.kilo_verify_worktree(
        working_directory=repo,
        commands=[
            "echo check1_ok",
            "echo fail_error_line && exit 1",
            "echo check3_ok",
        ],
        tail_lines=10,
    ))
    res = json.loads(res_str)
    assert res["ok"] is False
    assert res["dirty"] is True
    assert len(res["checks"]) == 3

    assert res["checks"][0]["passed"] is True
    assert res["checks"][0]["output_tail"] == ""

    assert res["checks"][1]["passed"] is False
    assert res["checks"][1]["exit_code"] == 1
    assert res["checks"][1]["timed_out"] is False
    assert "fail_error_line" in res["checks"][1]["output_tail"]

    assert res["checks"][2]["passed"] is True
    assert res["checks"][2]["output_tail"] == ""


def test_kilo_verify_worktree_timeout(tmp_path):
    repo = _init_git_repo(tmp_path / "repo")
    res_str = asyncio.run(server.kilo_verify_worktree(
        working_directory=repo,
        commands=["sleep 5"],
        timeout_seconds=1,
    ))
    res = json.loads(res_str)
    assert res["ok"] is False
    assert len(res["checks"]) == 1
    c = res["checks"][0]
    assert c["timed_out"] is True
    assert c["passed"] is False
    assert c["exit_code"] is None


def test_kilo_verify_worktree_config_file(tmp_path):
    repo = _init_git_repo(tmp_path / "repo")
    # Test kilo-mcp.toml
    cfg_file = os.path.join(repo, "kilo-mcp.toml")
    with open(cfg_file, "w") as f:
        f.write("[verify]\ncommands = [\"echo from_kilo_mcp_toml\"]\n")

    res_str = asyncio.run(server.kilo_verify_worktree(working_directory=repo))
    res = json.loads(res_str)
    assert res["ok"] is True
    assert len(res["checks"]) == 1
    assert res["checks"][0]["command"] == "echo from_kilo_mcp_toml"
    assert res["checks"][0]["passed"] is True

    # Test .kilo-mcp.toml fallback
    os.remove(cfg_file)
    dot_cfg_file = os.path.join(repo, ".kilo-mcp.toml")
    with open(dot_cfg_file, "w") as f:
        f.write("[verify]\ncommands = [\"echo from_dot_kilo_mcp_toml\"]\n")

    res_str2 = asyncio.run(server.kilo_verify_worktree(working_directory=repo))
    res2 = json.loads(res_str2)
    assert res2["ok"] is True
    assert len(res2["checks"]) == 1
    assert res2["checks"][0]["command"] == "echo from_dot_kilo_mcp_toml"
    assert res2["checks"][0]["passed"] is True


def test_kilo_verify_worktree_no_commands_no_config(tmp_path):
    repo = _init_git_repo(tmp_path / "repo")
    res_str = asyncio.run(server.kilo_verify_worktree(working_directory=repo))
    res = json.loads(res_str)
    assert res["ok"] is False
    assert "error" in res
    assert "commands" in res["error"]
    assert "verify" in res["error"]
    assert "kilo-mcp.toml" in res["error"]


def test_kilo_verify_worktree_invalid_args(tmp_path):
    repo = _init_git_repo(tmp_path / "repo")

    # Nonexistent working directory
    with pytest.raises(ValueError) as exc:
        asyncio.run(server.kilo_verify_worktree(working_directory=str(tmp_path / "nonexistent")))
    assert "does not exist" in str(exc.value)

    # File instead of directory
    file_path = tmp_path / "somefile.txt"
    file_path.write_text("hello")
    with pytest.raises(ValueError) as exc:
        asyncio.run(server.kilo_verify_worktree(working_directory=str(file_path)))
    assert "not a directory" in str(exc.value)

    # timeout_seconds outside 1..3600
    with pytest.raises(ValueError) as exc:
        asyncio.run(server.kilo_verify_worktree(working_directory=repo, timeout_seconds=0))
    assert "timeout_seconds" in str(exc.value)

    with pytest.raises(ValueError) as exc:
        asyncio.run(server.kilo_verify_worktree(working_directory=repo, timeout_seconds=3601))
    assert "timeout_seconds" in str(exc.value)

    # tail_lines outside 1..500
    with pytest.raises(ValueError) as exc:
        asyncio.run(server.kilo_verify_worktree(working_directory=repo, tail_lines=0))
    assert "tail_lines" in str(exc.value)

    with pytest.raises(ValueError) as exc:
        asyncio.run(server.kilo_verify_worktree(working_directory=repo, tail_lines=501))
    assert "tail_lines" in str(exc.value)


def test_kilo_cleanup_dry_run_and_apply(tmp_path):
    repo = _init_git_repo(tmp_path / "cleanup_repo")
    base_branch = "master" if subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "master"], capture_output=True).returncode == 0 else "main"

    # Create clean merged worktree
    wt_merged = os.path.join(repo, ".kilo", "worktrees", "feat-clean-merged")
    subprocess.run(["git", "-C", repo, "worktree", "add", "-b", "feat-clean-merged", wt_merged], check=True, capture_output=True)
    with open(os.path.join(wt_merged, "file.txt"), "w") as f:
        f.write("clean\n")
    subprocess.run(["git", "-C", wt_merged, "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", wt_merged, "commit", "-q", "-m", "feat clean"], check=True)
    subprocess.run(["git", "-C", repo, "merge", "--no-ff", "feat-clean-merged", "-m", "merge clean"], check=True, capture_output=True)

    # Create dirty worktree
    wt_dirty = os.path.join(repo, ".kilo", "worktrees", "feat-dirty")
    subprocess.run(["git", "-C", repo, "worktree", "add", "-b", "feat-dirty", wt_dirty], check=True, capture_output=True)
    with open(os.path.join(wt_dirty, "uncommitted.txt"), "w") as f:
        f.write("dirty\n")

    # Create unmerged worktree
    wt_unmerged = os.path.join(repo, ".kilo", "worktrees", "feat-unmerged")
    subprocess.run(["git", "-C", repo, "worktree", "add", "-b", "feat-unmerged", wt_unmerged], check=True, capture_output=True)
    with open(os.path.join(wt_unmerged, "unmerged.txt"), "w") as f:
        f.write("unmerged\n")
    subprocess.run(["git", "-C", wt_unmerged, "add", "unmerged.txt"], check=True)
    subprocess.run(["git", "-C", wt_unmerged, "commit", "-q", "-m", "unmerged feat"], check=True)

    # 1. Dry run
    res_dry_raw = asyncio.run(server.kilo_cleanup(working_directory=repo, dry_run=True, base_branch=base_branch))
    res_dry = json.loads(res_dry_raw)

    assert res_dry["dry_run"] is True
    assert "plan_hash" in res_dry
    assert len(res_dry["plan_hash"]) == 16
    assert res_dry["candidates_count"] >= 1  # feat-clean-merged worktree and branch
    
    # Verify leftover protection
    assert res_dry["leftovers_count"] == 2
    leftover_reasons = [item["reason"] for item in res_dry["leftover_work"]]
    assert "dirty_files" in leftover_reasons
    assert "unmerged_commits" in leftover_reasons

    # 2. Apply without plan_hash should fail
    res_no_hash = json.loads(asyncio.run(server.kilo_cleanup(working_directory=repo, dry_run=False, base_branch=base_branch)))
    assert "error" in res_no_hash
    assert "plan_hash is required" in res_no_hash["error"]

    # 3. Apply with wrong plan_hash should fail
    res_wrong_hash = json.loads(asyncio.run(server.kilo_cleanup(working_directory=repo, dry_run=False, plan_hash="wrong_hash_1234", base_branch=base_branch)))
    assert "error" in res_wrong_hash
    assert "plan_hash mismatch" in res_wrong_hash["error"]

    # 4. Apply with correct plan_hash
    res_apply = json.loads(asyncio.run(server.kilo_cleanup(
        working_directory=repo,
        dry_run=False,
        plan_hash=res_dry["plan_hash"],
        base_branch=base_branch
    )))
    assert res_apply["dry_run"] is False
    assert res_apply["executed_count"] >= 1
    assert os.path.exists(res_apply["audit_file"])
    assert not os.path.exists(wt_merged)
    # Dirty and unmerged worktrees MUST survive
    assert os.path.exists(wt_dirty)
    assert os.path.exists(wt_unmerged)

    # 5. Idempotence: re-running dry-run has zero candidates for the cleaned worktree
    res_idempotent = json.loads(asyncio.run(server.kilo_cleanup(working_directory=repo, dry_run=True, base_branch=base_branch)))
    assert not any(c["id"] == wt_merged for c in res_idempotent["candidates"])


def test_path_normalisation_matches_kilo():
    """Verify workspace path normalization and Qdrant collection hash calculation
    matches Kilo's qdrant-client.ts and FSUtil.resolve exactly."""
    # Test 1: gcube_repos known hash
    c1 = server._kilo_workspace_collection_name("/Users/Alfredo/works/gcube_repos")
    assert c1 == "ws-21327b19f043c6a7"

    # Test 2: gcube_atlas known hash
    c2 = server._kilo_workspace_collection_name("/Users/Alfredo/works/gcube_atlas")
    assert c2 == "ws-e2499c9ce8d6a71c"

    # Test 3: trailing slash stripped
    c3 = server._kilo_workspace_collection_name("/Users/Alfredo/works/gcube_repos/")
    assert c3 == "ws-21327b19f043c6a7"

    # Test 4: known worktree path
    c4 = server._kilo_workspace_collection_name(
        "/Users/Alfredo/works/ai-coding-tools/ai-swissknife/.kilo/worktrees/kilo/29.5-object-shaped-config-and-command"
    )
    assert c4 == "ws-00356f2dacdbf06d"


def test_cleanup_default_deletes_collection(tmp_path, monkeypatch):
    """Default kilo_cleanup (delete_collection=True) sends DELETE /collections/ws-<hash>."""
    repo = _init_git_repo(tmp_path / "repo")
    base_branch = "master" if subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "master"], capture_output=True).returncode == 0 else "main"

    wt_clean = os.path.join(repo, ".kilo", "worktrees", "feat-clean")
    subprocess.run(["git", "-C", repo, "worktree", "add", "-b", "feat-clean", wt_clean], check=True, capture_output=True)
    with open(os.path.join(wt_clean, "f.txt"), "w") as f:
        f.write("clean\n")
    subprocess.run(["git", "-C", wt_clean, "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", wt_clean, "commit", "-q", "-m", "feat clean"], check=True)
    subprocess.run(["git", "-C", repo, "merge", "--no-ff", "feat-clean", "-m", "merge clean"], check=True, capture_output=True)

    expected_coll = server._kilo_workspace_collection_name(wt_clean)
    http_calls = []

    class FakeResp:
        def __init__(self):
            self.status = 200
        def read(self):
            return b'{"result": true}'
        def getcode(self):
            return 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    def fake_urlopen(req, timeout=10):
        http_calls.append({"url": req.full_url, "method": req.get_method()})
        return FakeResp()

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)

    # Dry-run
    res_dry = json.loads(asyncio.run(server.kilo_cleanup(working_directory=repo, dry_run=True, base_branch=base_branch)))
    assert len(http_calls) == 0
    assert any(c["id"] == wt_clean and c.get("collection") == expected_coll for c in res_dry["candidates"])

    # Apply
    res_apply = json.loads(asyncio.run(server.kilo_cleanup(
        working_directory=repo,
        dry_run=False,
        plan_hash=res_dry["plan_hash"],
        base_branch=base_branch,
    )))

    assert res_apply["dry_run"] is False
    assert not os.path.exists(wt_clean)
    assert any(call["method"] == "DELETE" and expected_coll in call["url"] for call in http_calls)
    assert expected_coll in res_apply["deleted_collections"]


def test_cleanup_collection_404_tolerated(tmp_path, monkeypatch):
    """404 when deleting Qdrant collection is treated as already gone and not fatal."""
    repo = _init_git_repo(tmp_path / "repo")
    base_branch = "master" if subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "master"], capture_output=True).returncode == 0 else "main"

    wt_clean = os.path.join(repo, ".kilo", "worktrees", "feat-404")
    subprocess.run(["git", "-C", repo, "worktree", "add", "-b", "feat-404", wt_clean], check=True, capture_output=True)
    with open(os.path.join(wt_clean, "f.txt"), "w") as f:
        f.write("test\n")
    subprocess.run(["git", "-C", wt_clean, "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", wt_clean, "commit", "-q", "-m", "feat 404"], check=True)
    subprocess.run(["git", "-C", repo, "merge", "--no-ff", "feat-404", "-m", "merge"], check=True, capture_output=True)

    expected_coll = server._kilo_workspace_collection_name(wt_clean)

    import io
    def fake_urlopen_404(req, timeout=10):
        if req.get_method() == "DELETE":
            fp = io.BytesIO(b'{"status": "error", "message": "Not found"}')
            raise server.urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, fp)
        raise server.urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b""))

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen_404)

    res_dry = json.loads(asyncio.run(server.kilo_cleanup(working_directory=repo, dry_run=True, base_branch=base_branch)))
    res_apply = json.loads(asyncio.run(server.kilo_cleanup(
        working_directory=repo,
        dry_run=False,
        plan_hash=res_dry["plan_hash"],
        base_branch=base_branch,
    )))

    assert not os.path.exists(wt_clean)
    assert expected_coll in res_apply["deleted_collections"]
    coll_action = next(a for a in res_apply["executed_actions"] if a.get("action") == "delete_collection")
    assert coll_action["status"] == "already_deleted"
    assert coll_action["success"] is True


def test_cleanup_collection_error_reported_but_worktree_still_removed(tmp_path, monkeypatch):
    """Qdrant error (500 or connection error) is reported, never fatal to worktree removal."""
    repo = _init_git_repo(tmp_path / "repo")
    base_branch = "master" if subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "master"], capture_output=True).returncode == 0 else "main"

    wt_clean = os.path.join(repo, ".kilo", "worktrees", "feat-err")
    subprocess.run(["git", "-C", repo, "worktree", "add", "-b", "feat-err", wt_clean], check=True, capture_output=True)
    with open(os.path.join(wt_clean, "f.txt"), "w") as f:
        f.write("err\n")
    subprocess.run(["git", "-C", wt_clean, "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", wt_clean, "commit", "-q", "-m", "feat err"], check=True)
    subprocess.run(["git", "-C", repo, "merge", "--no-ff", "feat-err", "-m", "merge"], check=True, capture_output=True)

    expected_coll = server._kilo_workspace_collection_name(wt_clean)

    def fake_urlopen_err(req, timeout=10):
        raise server.urllib.error.URLError("Connection refused")

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen_err)

    res_dry = json.loads(asyncio.run(server.kilo_cleanup(working_directory=repo, dry_run=True, base_branch=base_branch)))
    res_apply = json.loads(asyncio.run(server.kilo_cleanup(
        working_directory=repo,
        dry_run=False,
        plan_hash=res_dry["plan_hash"],
        base_branch=base_branch,
    )))

    # Worktree was still removed!
    assert not os.path.exists(wt_clean)
    # Collection error is recorded in executed_actions and skipped
    coll_action = next(a for a in res_apply["executed_actions"] if a.get("action") == "delete_collection")
    assert coll_action["success"] is False
    assert "Connection refused" in coll_action["error"]
    assert expected_coll not in res_apply["deleted_collections"]
    assert any(s["id"] == expected_coll for s in res_apply["skipped"])


def test_cleanup_delete_sessions_false_never_calls_session_delete(tmp_path, monkeypatch):
    """delete_sessions=False never calls `kilo session delete`."""
    repo = _init_git_repo(tmp_path / "repo")
    base_branch = "master" if subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "master"], capture_output=True).returncode == 0 else "main"

    wt_clean = os.path.join(repo, ".kilo", "worktrees", "feat-no-sess")
    subprocess.run(["git", "-C", repo, "worktree", "add", "-b", "feat-no-sess", wt_clean], check=True, capture_output=True)
    with open(os.path.join(wt_clean, "f.txt"), "w") as f:
        f.write("x\n")
    subprocess.run(["git", "-C", wt_clean, "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", wt_clean, "commit", "-q", "-m", "clean"], check=True)
    subprocess.run(["git", "-C", repo, "merge", "--no-ff", "feat-no-sess", "-m", "merge"], check=True, capture_output=True)

    session_delete_calls = []
    original_run_kilo = server._run_kilo

    async def fake_run_kilo(cmd, cwd, env, on_start=None):
        if len(cmd) >= 3 and cmd[0] == "kilo" and cmd[1] == "session" and cmd[2] == "delete":
            session_delete_calls.append(cmd)
            return (0, "", "")
        return await original_run_kilo(cmd, cwd, env, on_start=on_start)

    monkeypatch.setattr(server, "_run_kilo", fake_run_kilo)

    # Dry-run with delete_sessions=False (default)
    res_dry = json.loads(asyncio.run(server.kilo_cleanup(working_directory=repo, dry_run=True, base_branch=base_branch, delete_sessions=False)))
    assert not any(c.get("kind") == "session" for c in res_dry["candidates"])

    # Apply
    res_apply = json.loads(asyncio.run(server.kilo_cleanup(
        working_directory=repo,
        dry_run=False,
        plan_hash=res_dry["plan_hash"],
        base_branch=base_branch,
        delete_sessions=False,
    )))

    assert len(session_delete_calls) == 0
    assert res_apply["deleted_sessions"] == []


def test_cleanup_delete_sessions_true_calls_only_for_worktree_sessions(tmp_path, monkeypatch):
    """delete_sessions=True deletes sessions matching removed worktree and leaves others alone."""
    repo = _init_git_repo(tmp_path / "repo")
    base_branch = "master" if subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "master"], capture_output=True).returncode == 0 else "main"

    wt_clean = os.path.join(repo, ".kilo", "worktrees", "feat-sess")
    subprocess.run(["git", "-C", repo, "worktree", "add", "-b", "feat-sess", wt_clean], check=True, capture_output=True)
    with open(os.path.join(wt_clean, "f.txt"), "w") as f:
        f.write("x\n")
    subprocess.run(["git", "-C", wt_clean, "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", wt_clean, "commit", "-q", "-m", "clean"], check=True)
    subprocess.run(["git", "-C", repo, "merge", "--no-ff", "feat-sess", "-m", "merge"], check=True, capture_output=True)

    sessions_db = [
        {"id": "ses_wt_1", "directory": wt_clean},
        {"id": "ses_wt_2", "directory": os.path.join(wt_clean, "sub")},
        {"id": "ses_other", "directory": "/some/other/repo/.kilo/worktrees/task"},
        {"id": "ses_main", "directory": repo},
    ]

    deleted_session_ids = []
    original_run_kilo = server._run_kilo

    async def fake_run_kilo(cmd, cwd, env, on_start=None):
        if len(cmd) >= 4 and cmd[:4] == ["kilo", "session", "list", "--format"]:
            return (0, json.dumps(sessions_db), "")
        if len(cmd) >= 3 and cmd[:3] == ["kilo", "session", "delete"]:
            deleted_session_ids.append(cmd[3])
            return (0, f"Deleted session {cmd[3]}", "")
        return await original_run_kilo(cmd, cwd, env, on_start=on_start)

    monkeypatch.setattr(server, "_run_kilo", fake_run_kilo)

    res_dry = json.loads(asyncio.run(server.kilo_cleanup(
        working_directory=repo,
        dry_run=True,
        base_branch=base_branch,
        delete_sessions=True,
    )))

    session_cands = [c["id"] for c in res_dry["candidates"] if c.get("kind") == "session"]
    assert "ses_wt_1" in session_cands
    assert "ses_wt_2" in session_cands
    assert "ses_other" not in session_cands
    assert "ses_main" not in session_cands

    # Apply
    res_apply = json.loads(asyncio.run(server.kilo_cleanup(
        working_directory=repo,
        dry_run=False,
        plan_hash=res_dry["plan_hash"],
        base_branch=base_branch,
        delete_sessions=True,
    )))

    assert "ses_wt_1" in deleted_session_ids
    assert "ses_wt_2" in deleted_session_ids
    assert "ses_other" not in deleted_session_ids
    assert "ses_main" not in deleted_session_ids
    assert set(res_apply["deleted_sessions"]) == {"ses_wt_1", "ses_wt_2"}


def test_cleanup_prune_dry_run_deletes_nothing(tmp_path, monkeypatch):
    """prune mode dry-run lists identified removed-worktree collections and deletes nothing."""
    repo = _init_git_repo(tmp_path / "repo")
    removed_wt = os.path.join(repo, ".kilo", "worktrees", "removed-task")
    existing_wt = os.path.join(repo, ".kilo", "worktrees", "existing-task")
    os.makedirs(existing_wt, exist_ok=True)

    coll_removed = server._kilo_workspace_collection_name(removed_wt)
    coll_existing = server._kilo_workspace_collection_name(existing_wt)
    coll_unknown = "ws-1111222233334444"

    qdrant_collections = [
        {"name": coll_removed},
        {"name": coll_existing},
        {"name": coll_unknown},
        {"name": "not-a-ws-collection"},
    ]

    http_calls = []

    class FakeResp:
        def __init__(self, data):
            self.data = json.dumps(data).encode("utf-8")
            self.status = 200
        def read(self):
            return self.data
        def getcode(self):
            return 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    def fake_urlopen(req, timeout=10):
        http_calls.append({"url": req.full_url, "method": req.get_method()})
        if req.get_method() == "GET" and "/collections" in req.full_url:
            return FakeResp({"result": {"collections": qdrant_collections}})
        raise RuntimeError(f"Unexpected HTTP call: {req.get_method()} {req.full_url}")

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)

    sessions_db = [
        {"id": "ses_rem", "directory": removed_wt},
        {"id": "ses_ext", "directory": existing_wt},
    ]

    original_run_kilo = server._run_kilo
    async def fake_run_kilo(cmd, cwd, env, on_start=None):
        if len(cmd) >= 4 and cmd[:4] == ["kilo", "session", "list", "--format"]:
            return (0, json.dumps(sessions_db), "")
        return await original_run_kilo(cmd, cwd, env, on_start=on_start)

    monkeypatch.setattr(server, "_run_kilo", fake_run_kilo)

    res_raw = asyncio.run(server.kilo_cleanup(
        working_directory=repo,
        prune_orphans=True,
        dry_run=True,
    ))
    res = json.loads(res_raw)

    assert res["prune_orphans"] is True
    assert res["dry_run"] is True
    # Prune dry-run must NOT perform any DELETE
    assert not any(c["method"] == "DELETE" for c in http_calls)
    assert res["deleted_collections"] == []

    # Candidates only has the removed worktree collection
    cand_ids = [c["id"] for c in res["candidates"]]
    assert coll_removed in cand_ids
    assert coll_existing not in cand_ids
    assert coll_unknown not in cand_ids

    # Kept has the existing worktree
    kept_ids = [k["id"] for k in res["kept"]]
    assert coll_existing in kept_ids

    # Unknown hashes has the unknown collection
    unk_ids = [u["id"] for u in res["unknown_hashes"]]
    assert coll_unknown in unk_ids


def test_cleanup_prune_deletes_only_identified_removed_worktree_collections(tmp_path, monkeypatch):
    """prune mode execution deletes only identified removed-worktree collections, never unknown hashes."""
    repo = _init_git_repo(tmp_path / "repo")
    removed_wt = os.path.join(repo, ".kilo", "worktrees", "task-to-prune")
    existing_wt = os.path.join(repo, ".kilo", "worktrees", "live-task")
    os.makedirs(existing_wt, exist_ok=True)
    non_wt_path = os.path.join(tmp_path, "deleted-regular-folder")

    coll_removed = server._kilo_workspace_collection_name(removed_wt)
    coll_existing = server._kilo_workspace_collection_name(existing_wt)
    coll_non_wt = server._kilo_workspace_collection_name(non_wt_path)
    coll_unknown = "ws-9999888877776666"

    qdrant_collections = [
        {"name": coll_removed},
        {"name": coll_existing},
        {"name": coll_non_wt},
        {"name": coll_unknown},
    ]

    deleted_urls = []

    class FakeResp:
        def __init__(self, data):
            self.data = json.dumps(data).encode("utf-8")
            self.status = 200
        def read(self):
            return self.data
        def getcode(self):
            return 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    def fake_urlopen(req, timeout=10):
        if req.get_method() == "GET" and "/collections" in req.full_url:
            return FakeResp({"result": {"collections": qdrant_collections}})
        if req.get_method() == "DELETE":
            deleted_urls.append(req.full_url)
            return FakeResp({"result": True})
        raise RuntimeError(f"Unexpected HTTP call: {req.get_method()} {req.full_url}")

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)

    sessions_db = [
        {"id": "ses_rem", "directory": removed_wt},
        {"id": "ses_ext", "directory": existing_wt},
        {"id": "ses_non_wt", "directory": non_wt_path},
    ]

    original_run_kilo = server._run_kilo
    async def fake_run_kilo(cmd, cwd, env, on_start=None):
        if len(cmd) >= 4 and cmd[:4] == ["kilo", "session", "list", "--format"]:
            return (0, json.dumps(sessions_db), "")
        return await original_run_kilo(cmd, cwd, env, on_start=on_start)

    monkeypatch.setattr(server, "_run_kilo", fake_run_kilo)

    # 1. Preview (dry_run=True)
    res_dry = json.loads(asyncio.run(server.kilo_cleanup(
        working_directory=repo,
        prune_orphans=True,
        dry_run=True,
    )))

    assert len(deleted_urls) == 0
    assert len(res_dry["candidates"]) == 1
    assert res_dry["candidates"][0]["id"] == coll_removed

    # 2. Apply (dry_run=False)
    res_apply = json.loads(asyncio.run(server.kilo_cleanup(
        working_directory=repo,
        prune_orphans=True,
        dry_run=False,
        plan_hash=res_dry["plan_hash"],
    )))

    assert res_apply["dry_run"] is False
    assert res_apply["deleted_collections"] == [coll_removed]
    # Check that ONLY coll_removed was requested for DELETE
    assert len(deleted_urls) == 1
    assert coll_removed in deleted_urls[0]
    # Confirm unknown and non-wt were never deleted
    assert not any(coll_unknown in u for u in deleted_urls)
    assert not any(coll_existing in u for u in deleted_urls)
    assert not any(coll_non_wt in u for u in deleted_urls)


def test_worktree_git_metadata_and_cleanup_prune(tmp_path, fake_data_dir):
    """Verify that worktree creation + session registration write
    `.git/worktrees/<name>/kilo-agent-manager-metadata.json` and `tabOrder`,
    and that `_agent_manager_prune_stale` removes deleted worktrees and their
    sessions from `.kilo/agent-manager.json`."""
    repo = _init_git_repo(tmp_path)
    wt_dir = tmp_path / ".kilo" / "worktrees" / "kilo-feat-meta"
    wt_gitdir = tmp_path / ".git" / "worktrees" / "kilo-feat-meta"
    wt_dir.mkdir(parents=True)
    wt_gitdir.mkdir(parents=True)
    (wt_dir / ".git").write_text(f"gitdir: {wt_gitdir}\n")

    wt_id = server._agent_manager_register_worktree(
        repo, branch="kilo/feat-meta", path=str(wt_dir),
        parent_branch="main", remote="origin",
    )
    server._agent_manager_register_session(repo, "ses_meta123", wt_id, cwd=str(wt_dir))

    meta_file = wt_gitdir / server.GIT_METADATA_FILE
    assert meta_file.exists()
    meta = json.loads(meta_file.read_text())
    assert meta == {
        "parentBranch": "main",
        "remote": "origin",
        "sessionId": "ses_meta123",
    }

    am_data = json.loads((tmp_path / ".kilo" / "agent-manager.json").read_text())
    assert am_data["tabOrder"][wt_id] == ["ses_meta123"]

    # Now simulate worktree removal and prune stale entries
    import shutil
    shutil.rmtree(wt_dir)
    dropped = server._agent_manager_prune_stale(repo)
    assert dropped == [wt_id]
    am_after = json.loads((tmp_path / ".kilo" / "agent-manager.json").read_text())
    assert wt_id not in am_after["worktrees"]
    assert "ses_meta123" not in am_after["sessions"]
    assert wt_id not in am_after.get("tabOrder", {})
    assert wt_id not in am_after.get("worktreeOrder", [])


# ==============================================================================
# 28. Model guidance (flash/argon only, banned refused) and agent parameter
# ==============================================================================

def test_default_model_is_flash():
    assert server.DEFAULT_MODEL == "google/gemini-3.8-flash"
    assert server.COMPLEX_MODEL == "google/gemini-4-argon"
    assert "google/gemini-3.8-flash" in server.ALLOWED_MODELS
    assert "google/gemini-4-argon" in server.ALLOWED_MODELS


def test_is_banned_model():
    assert server._is_banned_model("google/gemini-3.1-pro")
    assert server._is_banned_model("google/gemini-3.1-pro-preview")
    assert server._is_banned_model("google/gemini-3.1-pro-preview-customtools")
    assert server._is_banned_model("google/gemini-flash-latest")
    assert not server._is_banned_model("google/gemini-3.8-flash")
    assert not server._is_banned_model("google/gemini-4-argon")


@pytest.mark.parametrize("banned_model", [
    "google/gemini-3.1-pro",
    "google/gemini-3.1-pro-preview",
    "google/gemini-3.1-pro-preview-customtools",
    "google/gemini-flash-latest",
])
def test_banned_model_refused(tmp_path, banned_model):
    res = asyncio.run(server.kilo_implement(
        task_instructions="Perform test task",
        working_directory=str(tmp_path),
        model=banned_model,
    ))
    assert "Error:" in res
    assert "banned by owner policy" in res
    assert "google/gemini-3.8-flash" in res
    assert "google/gemini-4-argon" in res


def test_descriptions_mention_only_allowed_models():
    # Tools should mention allowed models and not mention banned ones
    tools = getattr(server.mcp, "_tool_manager", None)
    if tools and hasattr(tools, "_tools"):
        for name, tool in tools._tools.items():
            desc = getattr(tool, "description", "") or ""
            assert "gemini-flash-latest" not in desc, f"{name} mentions gemini-flash-latest"
            assert "3.1-pro" not in desc, f"{name} mentions 3.1-pro"
            assert "customtools" not in desc, f"{name} mentions customtools"

    # kilo_implement and kilo_list_models docstrings/descriptions check
    impl_doc = server.kilo_implement.__doc__ or ""
    assert "gemini-flash-latest" not in impl_doc
    assert "3.1-pro" not in impl_doc
    assert "customtools" not in impl_doc

    list_models_doc = server.kilo_list_models.__doc__ or ""
    assert "google/gemini-3.8-flash" in list_models_doc
    assert "google/gemini-4-argon" in list_models_doc
    assert "gemini-flash-latest" not in list_models_doc
    assert "3.1-pro" not in list_models_doc


def test_build_kilo_run_argv_agent_explore():
    cmd = server._build_kilo_run_argv("/path/to/spec.md", agent="explore")
    assert "--agent" in cmd
    idx = cmd.index("--agent")
    assert cmd[idx + 1] == "explore"
    # Verify 'code' is not in the built argv when agent='explore'
    assert "code" not in cmd


def test_build_kilo_run_argv_default_stays_code():
    cmd_none = server._build_kilo_run_argv("/path/to/spec.md", agent=None)
    assert "--agent" in cmd_none
    idx = cmd_none.index("--agent")
    assert cmd_none[idx + 1] == "code"

    cmd_default = server._build_kilo_run_argv("/path/to/spec.md")
    assert "--agent" in cmd_default
    idx = cmd_default.index("--agent")
    assert cmd_default[idx + 1] == "code"


def test_kilo_implement_refuses_subagent_explore(tmp_path):
    res = asyncio.run(server.kilo_implement(
        task_instructions="Read-only explore task",
        working_directory=str(tmp_path),
        agent="explore",
    ))
    assert "Error:" in res
    assert "subagent" in res.lower()
    assert "explore" in res
    assert "kilo_rag_search" in res






def test_protected_paths_warning_detects_config_files():
    w = server._protected_paths_warning("Create AGENTS.md with the build commands", None)
    assert "PROTECTED FILES" in w and "AGENTS.md" in w
    w = server._protected_paths_warning("Edit the agent definition", [".kilo/agent/x.md"])
    assert ".kilo/ or .kilocode/" in w
    w = server._protected_paths_warning("Update kilo.jsonc permissions", None)
    assert "kilo.jsonc" in w


def test_protected_paths_warning_ignores_plans_and_other_files():
    assert server._protected_paths_warning("Write the plan to .kilo/plans/p.md", None) == ""
    assert server._protected_paths_warning("Edit README.md and docs/agents-notes.md", ["src/a.py"]) == ""
    assert server._protected_paths_warning("Use MY_AGENTS.mdx naming", None) == ""


def test_kilo_implement_warns_on_protected_files(tmp_path, fake_data_dir, mock_run_kilo_custom, monkeypatch):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    res = asyncio.run(server.kilo_implement(
        task_instructions="Create AGENTS.md at the repository root", working_directory=str(tmp_path),
        background=False,
    ))
    assert "PROTECTED FILES" in res


# ==============================================================================
# 26. T018: Trimmed task results, reliable completion, per-item reports, neutral wording
# ==============================================================================

def test_kilo_task_result_trimmed_default_and_include_trace(tmp_path, monkeypatch):
    """By default, kilo_task_result returns a trimmed summary (<MCP tool limit);
    with include_trace=True it returns the full trace."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    task_id = "trim123"
    log_path = tasks_dir / f"{task_id}.log"
    huge_trace = "Running step: build...\n" * 6000  # ~144KB
    final_report = (
        "## Final Report\n"
        "- **Outcome**: success\n"
        "- **Files changed**:\n"
        "  - src/feature.py\n"
        "- **Verification**: pytest passed\n"
        "- **Issues**: none\n"
    )
    log_path.write_text(huge_trace + "\n" + final_report)

    server._write_task_record(task_id, {
        "status": "completed",
        "exit_code": 0,
        "session_id": "ses_trim1",
        "log_path": str(log_path),
        "working_directory": "/tmp/worktree",
        "isolated_branch": "kilo/feature-branch",
    })

    # Default trimmed result
    res_trimmed = asyncio.run(server.kilo_task_result(task_id=task_id))
    assert len(res_trimmed) < 5000
    assert "- Status: completed" in res_trimmed
    assert "- Exit Code: 0" in res_trimmed
    assert "- Session ID: ses_trim1" in res_trimmed
    assert "- Branch: kilo/feature-branch" in res_trimmed
    assert "- Worktree: /tmp/worktree" in res_trimmed
    assert f"- Log file: {log_path}" in res_trimmed
    assert "## Final Report" in res_trimmed
    assert "src/feature.py" in res_trimmed
    assert "Running step: build" not in res_trimmed

    # With include_trace=True
    res_trace = asyncio.run(server.kilo_task_result(task_id=task_id, include_trace=True))
    assert len(res_trace) > 100000
    assert "LOG:\n" in res_trace
    assert "Running step: build" in res_trace
    assert "src/feature.py" in res_trace


def test_kilo_task_result_fallback_tail_when_no_final_report(tmp_path, monkeypatch):
    """When no Final Report is found in the output, kilo_task_result falls back
    to returning the last ~4000 characters with a clear truncation notice."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    task_id = "tail123"
    log_path = tasks_dir / f"{task_id}.log"
    raw_content = "Line of output: " + ("x" * 80) + "\n"
    log_path.write_text(raw_content * 120 + "LAST_LINE_OF_OUTPUT\n")

    server._write_task_record(task_id, {
        "status": "completed",
        "exit_code": 0,
        "session_id": "ses_tail",
        "log_path": str(log_path),
    })

    res = asyncio.run(server.kilo_task_result(task_id=task_id))
    assert "[... output truncated to last 4000 characters ...]" in res
    assert "LAST_LINE_OF_OUTPUT" in res
    assert len(res) < 5000


def test_kilo_task_result_no_null_fields_omits_absent_keys(tmp_path, monkeypatch):
    """Tool results must not contain null fields — absent keys are omitted."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    task_id = "omit_absent"
    server._write_task_record(task_id, {
        "status": "completed",
        # exit_code, session_id, log_path, isolated_branch are all absent / None
        "result": "## Final Report\n- **Outcome**: success\n- **Files changed**: none",
    })

    res = asyncio.run(server.kilo_task_result(task_id=task_id))
    assert "- Status: completed" in res
    assert "- Exit Code:" not in res
    assert "- Session ID:" not in res
    assert "- Branch:" not in res
    assert "- Worktree:" not in res
    assert "- Log file:" not in res
    assert "None" not in res
    assert "null" not in res


def test_is_pid_alive_detects_dead_and_reused_pid(monkeypatch):
    """_is_pid_alive rejects dead PIDs, zombies, and reused PIDs running other processes."""
    # 1. Non-existent PID
    assert server._is_pid_alive(None) is False
    assert server._is_pid_alive(9999999) is False

    # 2. Reused PID (PID exists in OS, but runs an unrelated process)
    monkeypatch.setattr(server.os, "kill", lambda pid, sig: None)  # os.kill succeeds
    monkeypatch.setattr(server, "_get_process_cmdline", lambda pid: "/usr/sbin/cron -f")
    assert server._is_pid_alive(12345) is False

    # 3. Another unrelated process (e.g. nginx or pytest)
    monkeypatch.setattr(server, "_get_process_cmdline", lambda pid: "python -m pytest test_server.py")
    assert server._is_pid_alive(12345) is False

    # 4. Zombie process
    monkeypatch.setattr(server, "_get_process_cmdline", lambda pid: "<defunct>")
    assert server._is_pid_alive(12345) is False

    # 5. Legitimate live kilo process
    monkeypatch.setattr(server, "_get_process_cmdline", lambda pid: "kilo run --agent code")
    assert server._is_pid_alive(12345) is True

    # 6. Legitimate live kilo process with full path
    monkeypatch.setattr(server, "_get_process_cmdline", lambda pid: "/usr/local/bin/kilo run --session s123")
    assert server._is_pid_alive(12345) is True


def test_reconcile_reused_pid_treated_as_exited(tmp_path, monkeypatch):
    """When a task has status='running' but its PID belongs to an unrelated reused process,
    _reconcile_task_if_orphaned reconciles the task from its log file instead of leaving it running."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    task_id = "reused123"
    log_path = tasks_dir / f"{task_id}.log"
    log_path.write_text(
        "Working...\n"
        "## Final Report\n"
        "- **Outcome**: success\n"
        "- **Files changed**:\n"
        "  - foo.py\n"
    )

    monkeypatch.setattr(server.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(server, "_get_process_cmdline", lambda pid: "/bin/bash")

    record = {
        "status": "running",
        "pid": 88888,
        "log_path": str(log_path),
        "working_directory": str(tmp_path),
    }
    server._write_task_record(task_id, record)

    updated = server._reconcile_task_if_orphaned(task_id, record)
    assert updated["status"] == "completed"
    assert "Result read from the run log (the Kilo process has exited" in updated["note"]


def test_collision_warning_ignores_reused_pid(tmp_path, monkeypatch):
    """Collision detection ignores tasks whose PID is a reused PID of an unrelated process."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    cwd = str(tmp_path)

    monkeypatch.setattr(server.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(server, "_get_process_cmdline", lambda pid: "/usr/sbin/syslogd")

    server._write_task_record("reused_task", {
        "status": "running",
        "working_directory": cwd,
        "pid": 77777,
    })

    running = server._find_running_tasks_for_cwd(cwd)
    assert running == []


def test_kilo_task_wait_accepts_long_timeout_and_returns_on_completion(tmp_path, monkeypatch, live_kilo_pid):
    """kilo_task_wait accepts timeout_seconds up to at least 3600 (or more),
    polls cheaply, and returns as soon as the task ends."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    task_id = "long_wait_ok"
    log_path = tasks_dir / f"{task_id}.log"
    log_path.write_text("## Final Report\n- **Outcome**: success\n- **Files changed**: - done.py\n")

    server._write_task_record(task_id, {
        "status": "running",
        "started": "2026-10-15T00:00:00+00:00",
        "working_directory": str(tmp_path),
        "pid": live_kilo_pid,
        "log_path": str(log_path),
    })

    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        server._write_task_record(task_id, {
            "status": "completed",
            "exit_code": 0,
            "result": "Kilo Execution Finished.\nOutcome: success\nFiles changed:\n- done.py",
        })

    monkeypatch.setattr(server.asyncio, "sleep", fake_sleep)

    res = asyncio.run(server.kilo_task_wait(task_id=task_id, timeout_seconds=3600))
    assert sleep_calls == [2.0]
    assert "Kilo Execution Finished" in res
    assert "done.py" in res

    res_7200 = asyncio.run(server.kilo_task_wait(task_id=task_id, timeout_seconds=7200))
    assert "Kilo Execution Finished" in res_7200


def test_kilo_task_wait_include_trace(tmp_path, monkeypatch):
    """kilo_task_wait respects include_trace=True vs include_trace=False."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    task_id = "wait_trace"
    log_path = tasks_dir / f"{task_id}.log"
    log_path.write_text("DETAILED_STEP_LOG_CONTENT\n\n## Final Report\n- **Outcome**: success\n- **Files changed**: none\n")

    server._write_task_record(task_id, {
        "status": "completed",
        "exit_code": 0,
        "log_path": str(log_path),
    })

    res_trimmed = asyncio.run(server.kilo_task_wait(task_id=task_id, include_trace=False))
    assert "DETAILED_STEP_LOG_CONTENT" not in res_trimmed
    assert "- Status: completed" in res_trimmed

    res_trace = asyncio.run(server.kilo_task_wait(task_id=task_id, include_trace=True))
    assert "DETAILED_STEP_LOG_CONTENT" in res_trace
    assert "LOG:\n" in res_trace


def test_kilo_implement_per_item_prompt_text(tmp_path, fake_data_dir):
    """The delegated prompt includes the per-item Final Report rule."""
    captured_content = []

    async def fake_run_kilo(cmd, cwd, env, on_start=None):
        import re
        m = re.search(r"The file (/\S+) contains", cmd[-1])
        with open(m.group(1)) as f:
            captured_content.append(f.read())
        return (0, "Outcome: success\nFiles changed:\n- x.py", "")

    from unittest.mock import patch

    with patch.object(server, "_run_kilo", side_effect=fake_run_kilo):
        asyncio.run(server.kilo_implement(
            task_instructions="1. Deliverable A\n2. Deliverable B",
            working_directory=str(tmp_path),
            background=False,
        ))

    assert len(captured_content) == 1
    prompt = captured_content[0]
    expected_rule = (
        'when the task instructions enumerate items (a numbered or bulleted list of deliverables), '
        'the report includes one line per item ("done" or "not done: why"), in the same order.'
    )
    assert expected_rule.lower() in prompt.lower()


def test_new_neutral_recovery_wording(tmp_path, monkeypatch):
    """When a task process has exited, results use neutral wording instead of error-like text."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    task_id = "neutral_word"
    log_path = tasks_dir / f"{task_id}.log"
    log_path.write_text("## Final Report\n- **Outcome**: success\n- **Files changed**: - a.py\n")

    server._write_task_record(task_id, {
        "status": "running",
        "pid": 9999999,
        "log_path": str(log_path),
    })

    res = asyncio.run(server.kilo_task_result(task_id=task_id))
    assert "Result read from the run log (the Kilo process has exited" in res
    assert "recovered by kilo-mcp after its process exited" not in res


# ==============================================================================
# T019: per-workspace default Kilo model
# ==============================================================================

def test_resolve_kilo_model_nearest_config_walk_up(tmp_path):
    # Root config
    root_cfg = tmp_path / "orchestration.toml"
    root_cfg.write_text("""
[tools.kilo.profiles.bulk-execution]
model = "google/gemini-4-argon"
""")

    # Subproject directory with its own nearer config
    sub_dir = tmp_path / "packages" / "sub"
    sub_dir.mkdir(parents=True)
    sub_cfg = sub_dir / "orchestration.toml"
    sub_cfg.write_text("""
[tools.kilo.profiles.bulk-execution]
model = "google/gemini-3.8-flash"
""")

    leaf_dir = sub_dir / "src" / "deep"
    leaf_dir.mkdir(parents=True)

    # Resolving from leaf_dir should find nearest config (sub_cfg)
    res = server.resolve_kilo_model(str(leaf_dir))
    assert res.model == "google/gemini-3.8-flash"
    assert res.config_path == str(sub_cfg)
    assert res.source == f"workspace config: {sub_cfg}"

    # Resolving from another subfolder without its own config finds root_cfg
    other_dir = tmp_path / "other" / "leaf"
    other_dir.mkdir(parents=True)
    res_other = server.resolve_kilo_model(str(other_dir))
    assert res_other.model == "google/gemini-4-argon"
    assert res_other.config_path == str(root_cfg)
    assert res_other.source == f"workspace config: {root_cfg}"

    # Also test .ai-orchestration/orchestration.toml variant
    third_dir = tmp_path / "third"
    (third_dir / ".ai-orchestration").mkdir(parents=True)
    dot_cfg = third_dir / ".ai-orchestration" / "orchestration.toml"
    dot_cfg.write_text("""
[tools.kilo.profiles.bulk-execution]
model = "google/gemini-3.8-flash"
""")
    res_dot = server.resolve_kilo_model(str(third_dir / "sub"))
    assert res_dot.model == "google/gemini-3.8-flash"
    assert res_dot.config_path == str(dot_cfg)


def test_resolve_kilo_model_default_profile_selection(tmp_path):
    cfg_file = tmp_path / "orchestration.toml"
    cfg_file.write_text("""
[routing]
default_profile = "deep-reasoning"

[tools.kilo.profiles.deep-reasoning]
model = "google/gemini-4-argon"

[tools.kilo.profiles.bulk-execution]
model = "google/gemini-3.8-flash"
""")
    res = server.resolve_kilo_model(str(tmp_path))
    assert res.model == "google/gemini-4-argon"
    assert res.config_path == str(cfg_file)

    # When default_profile is not set, defaults to bulk-execution
    cfg_file.write_text("""
[tools.kilo.profiles.deep-reasoning]
model = "google/gemini-4-argon"

[tools.kilo.profiles.bulk-execution]
model = "google/gemini-3.8-flash"
""")
    res2 = server.resolve_kilo_model(str(tmp_path))
    assert res2.model == "google/gemini-3.8-flash"


def test_resolve_kilo_model_default_model_override(tmp_path):
    cfg_file = tmp_path / "orchestration.toml"
    cfg_file.write_text("""
[routing]
default_profile = "deep-reasoning"

[tools.kilo]
default_model = "override-kilo-model"

[tools.kilo.profiles.deep-reasoning]
model = "google/gemini-4-argon"

[tools.kilo.profiles.bulk-execution]
model = "google/gemini-3.8-flash"
""")
    res = server.resolve_kilo_model(str(tmp_path))
    assert res.model == "override-kilo-model"
    assert res.config_path == str(cfg_file)


def test_kilo_implement_explicit_argument_wins(tmp_path, fake_data_dir, mock_run_kilo_custom):
    cfg_file = tmp_path / "orchestration.toml"
    cfg_file.write_text("""
[tools.kilo.profiles.bulk-execution]
model = "google/gemini-3.8-flash"
""")
    res = asyncio.run(server.kilo_implement(
        task_instructions="Test explicit argument",
        working_directory=str(tmp_path),
        model="google/gemini-4-argon",
        background=False,
    ))
    cmd = mock_run_kilo_custom.calls[0]["cmd"]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "google/gemini-4-argon"
    assert "- Model: google/gemini-4-argon (argument)" in res


def test_kilo_implement_banned_model_in_config_refused(tmp_path, fake_data_dir):
    cfg_file = tmp_path / "orchestration.toml"
    cfg_file.write_text("""
[tools.kilo.profiles.bulk-execution]
model = "google/gemini-3.1-pro-preview"
""")
    res = asyncio.run(server.kilo_implement(
        task_instructions="Test banned model",
        working_directory=str(tmp_path),
        background=False,
    ))
    assert res.startswith(f"Error: Model 'google/gemini-3.1-pro-preview' configured in {cfg_file} is banned by owner policy.")
    assert "Allowed models:" in res
    assert str(cfg_file) in res


def test_kilo_implement_malformed_toml_ignored_with_warning(tmp_path, fake_data_dir, mock_run_kilo_custom):
    # Parent valid config
    root_cfg = tmp_path / "orchestration.toml"
    root_cfg.write_text("""
[tools.kilo.profiles.bulk-execution]
model = "google/gemini-4-argon"
""")
    # Child malformed config
    sub_dir = tmp_path / "sub"
    sub_dir.mkdir()
    bad_cfg = sub_dir / "orchestration.toml"
    bad_cfg.write_text("invalid = toml = [broken")

    res = asyncio.run(server.kilo_implement(
        task_instructions="Test malformed toml",
        working_directory=str(sub_dir),
        background=False,
    ))
    assert f"Warning: malformed TOML in {bad_cfg}" in res
    assert "- Model: google/gemini-4-argon" in res
    assert f"(workspace config: {root_cfg})" in res
    cmd = mock_run_kilo_custom.calls[0]["cmd"]
    assert cmd[cmd.index("--model") + 1] == "google/gemini-4-argon"

    # Also check background mode includes the warning and model info
    res_bg = asyncio.run(server.kilo_implement(
        task_instructions="Test malformed toml background",
        working_directory=str(sub_dir),
        background=True,
    ))
    assert f"Warning: malformed TOML in {bad_cfg}" in res_bg
    assert "- Model: google/gemini-4-argon" in res_bg
    assert f"(workspace config: {root_cfg})" in res_bg


def test_kilo_implement_isolation_worktree_uses_original_directory(tmp_path, fake_data_dir, mock_run_kilo_custom):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    cfg_file = repo_dir / "orchestration.toml"
    cfg_file.write_text("""
[tools.kilo.profiles.bulk-execution]
model = "google/gemini-4-argon"
""")

    res = asyncio.run(server.kilo_implement(
        task_instructions="Worktree isolation test",
        working_directory=str(repo_dir),
        isolation="worktree",
        worktree_branch="feat-t019",
        background=False,
    ))

    # Kilo was run with model from repo_dir config, even though cwd was switched to worktree
    kilo_call = mock_run_kilo_custom.calls[1]
    cmd = kilo_call["cmd"]
    assert cmd[cmd.index("--model") + 1] == "google/gemini-4-argon"
    assert f"- Model: google/gemini-4-argon (workspace config: {cfg_file})" in res


def test_resolve_kilo_model_no_config_falls_back_to_server_default(tmp_path, fake_data_dir, mock_run_kilo_custom):
    empty_dir = tmp_path / "empty_workspace"
    empty_dir.mkdir()
    res = server.resolve_kilo_model(str(empty_dir))
    assert res.model == server.DEFAULT_MODEL
    assert res.source == "server default"
    assert res.config_path is None
    assert res.warnings == []

    # Also test via kilo_implement
    run_res = asyncio.run(server.kilo_implement(
        task_instructions="Default model run",
        working_directory=str(empty_dir),
        background=False,
    ))
    cmd = mock_run_kilo_custom.calls[0]["cmd"]
    assert cmd[cmd.index("--model") + 1] == server.DEFAULT_MODEL
    assert f"- Model: {server.DEFAULT_MODEL} (server default)" in run_res


# ==============================================================================
# T020 tests: worktree hygiene, base refresh, running session refusal, notes
# ==============================================================================

def test_kilo_implement_running_session_refusal(tmp_path, fake_data_dir, monkeypatch):
    """If continue_session_id targets a session with an active running task,
    kilo_implement must refuse to spawn a second process and return a clear refusal."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    session_id = "ses_running_active_1"
    running_task_id = "task_running_123"

    # Write a running task record
    server._write_task_record(running_task_id, {
        "status": "running",
        "started": "2026-10-21T07:00:00Z",
        "working_directory": str(tmp_path),
        "session_id": session_id,
        "pid": 98765,
    })

    # Mock _is_pid_alive to report PID 98765 as alive
    monkeypatch.setattr(server, "_is_pid_alive", lambda pid: pid == 98765)

    res = asyncio.run(server.kilo_implement(
        task_instructions="Follow-up instructions",
        working_directory=str(tmp_path),
        continue_session_id=session_id,
        background=False,
    ))

    assert not res.startswith("Error:")
    assert "Refusal: session 'ses_running_active_1' already has a running task (task_running_123)" in res
    assert "kilo_task_wait" in res
    assert "kilo_task_cancel" in res


def test_exclude_worktrees_from_git(tmp_path):
    """_ensure_worktrees_excluded appends /.kilo/ to <git common dir>/info/exclude
    only once, does not duplicate, skips when already ignored, and never touches .gitignore."""
    repo = _init_git_repo(tmp_path / "repo1")

    # Before exclude: check-ignore returns non-zero
    chk1 = subprocess.run(["git", "-C", repo, "check-ignore", "-q", ".kilo/worktrees"])
    assert chk1.returncode != 0

    # Ensure excluded
    ok = server._ensure_worktrees_excluded(repo)
    assert ok is True

    # Now check-ignore returns 0
    chk2 = subprocess.run(["git", "-C", repo, "check-ignore", "-q", ".kilo/worktrees"])
    assert chk2.returncode == 0

    exclude_file = os.path.join(repo, ".git", "info", "exclude")
    assert os.path.exists(exclude_file)
    with open(exclude_file, "r", encoding="utf-8") as f:
        content = f.read()
    assert "/.kilo/" in content

    # Call a second time: must not duplicate
    server._ensure_worktrees_excluded(repo)
    with open(exclude_file, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f.read().splitlines() if ln.strip() == "/.kilo/"]
    assert len(lines) == 1

    # Check gitignore was not created or modified
    gitignore_file = os.path.join(repo, ".gitignore")
    assert not os.path.exists(gitignore_file)

    # In a repo where .gitignore already ignores .kilo/, info/exclude is skipped
    repo2 = _init_git_repo(tmp_path / "repo2")
    with open(os.path.join(repo2, ".gitignore"), "w") as f:
        f.write(".kilo/\n")
    subprocess.run(["git", "-C", repo2, "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", repo2, "commit", "-m", "ignore .kilo"], check=True)

    exclude_file2 = os.path.join(repo2, ".git", "info", "exclude")
    initial_content = ""
    if os.path.exists(exclude_file2):
        with open(exclude_file2, "r", encoding="utf-8") as f:
            initial_content = f.read()

    server._ensure_worktrees_excluded(repo2)
    if os.path.exists(exclude_file2):
        with open(exclude_file2, "r", encoding="utf-8") as f:
            assert f.read() == initial_content


def test_worktree_base_branch_and_commit_recorded(tmp_path, fake_data_dir, mock_spawn_kilo_background, mock_run_kilo_custom, monkeypatch):
    """When creating a worktree (isolation='worktree'), base_branch and base_commit
    are recorded in the task record."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    repo = _init_git_repo(tmp_path / "repo_base")
    head_sha = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    current_branch = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    res = asyncio.run(server.kilo_implement(
        task_instructions="Isolated work",
        working_directory=repo,
        isolation="worktree",
        worktree_branch="feat-t020-rec",
        background=True,
    ))
    assert "feat-t020-rec" in res

    # Find the task record
    tasks = [f for f in os.listdir(server.TASKS_DIR) if f.endswith(".json")]
    assert len(tasks) == 1
    record = server._read_task_record(tasks[0][:-5])
    assert record["base_branch"] == current_branch
    assert record["base_commit"] == head_sha
    assert record["isolated_branch"] == "feat-t020-rec"


def test_refresh_base_merge_success(tmp_path, fake_data_dir, mock_run_kilo_custom, monkeypatch):
    """refresh_base='merge' merges changes from base_branch into the worktree."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    repo = _init_git_repo(tmp_path / "repo_merge")
    base_branch = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    wt_path = os.path.join(repo, ".kilo", "worktrees", "feat-merge-test")
    subprocess.run(
        ["git", "-C", repo, "worktree", "add", "-b", "feat-merge-test", wt_path],
        check=True, capture_output=True,
    )

    # Make a commit on base branch
    with open(os.path.join(repo, "new_on_base.txt"), "w") as f:
        f.write("from base\n")
    subprocess.run(["git", "-C", repo, "add", "new_on_base.txt"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-m", "base update"], check=True)

    session_id = "ses_merge_1"
    server._write_task_record("task_merge_0", {
        "status": "completed",
        "working_directory": wt_path,
        "session_id": session_id,
        "base_branch": base_branch,
        "isolated_branch": "feat-merge-test",
    })

    res = asyncio.run(server.kilo_implement(
        task_instructions="Continue session with merge",
        working_directory=wt_path,
        continue_session_id=session_id,
        refresh_base="merge",
        background=False,
    ))
    assert not res.startswith("Error:")
    # Verify that new_on_base.txt now exists in the worktree
    assert os.path.exists(os.path.join(wt_path, "new_on_base.txt"))


def test_refresh_base_rebase_success(tmp_path, fake_data_dir, mock_run_kilo_custom, monkeypatch):
    """refresh_base='rebase' rebases the worktree onto base_branch."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    repo = _init_git_repo(tmp_path / "repo_rebase")
    base_branch = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    wt_path = os.path.join(repo, ".kilo", "worktrees", "feat-rebase-test")
    subprocess.run(
        ["git", "-C", repo, "worktree", "add", "-b", "feat-rebase-test", wt_path],
        check=True, capture_output=True,
    )

    # Commit on base branch
    with open(os.path.join(repo, "rebase_base.txt"), "w") as f:
        f.write("rebase base\n")
    subprocess.run(["git", "-C", repo, "add", "rebase_base.txt"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-m", "rebase base update"], check=True)

    # Commit on feature worktree
    with open(os.path.join(wt_path, "feature.txt"), "w") as f:
        f.write("feature work\n")
    subprocess.run(["git", "-C", wt_path, "add", "feature.txt"], check=True)
    subprocess.run(["git", "-C", wt_path, "commit", "-m", "wt commit"], check=True)

    session_id = "ses_rebase_1"
    server._write_task_record("task_rebase_0", {
        "status": "completed",
        "working_directory": wt_path,
        "session_id": session_id,
        "base_branch": base_branch,
        "isolated_branch": "feat-rebase-test",
    })

    res = asyncio.run(server.kilo_implement(
        task_instructions="Continue session with rebase",
        working_directory=wt_path,
        continue_session_id=session_id,
        refresh_base="rebase",
        background=False,
    ))
    assert not res.startswith("Error:")
    assert os.path.exists(os.path.join(wt_path, "rebase_base.txt"))
    assert os.path.exists(os.path.join(wt_path, "feature.txt"))


def test_refresh_base_merge_conflict_rollback(tmp_path, fake_data_dir, mock_run_kilo_custom, monkeypatch):
    """When merge conflicts occur during refresh_base='merge', abort and leave worktree untouched."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    repo = _init_git_repo(tmp_path / "repo_conflict_m")
    base_branch = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    wt_path = os.path.join(repo, ".kilo", "worktrees", "feat-cm-test")
    subprocess.run(
        ["git", "-C", repo, "worktree", "add", "-b", "feat-cm-test", wt_path],
        check=True, capture_output=True,
    )

    # Conflicting changes in README.md
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("Base version of conflict\n")
    subprocess.run(["git", "-C", repo, "commit", "-am", "base conflict"], check=True)

    with open(os.path.join(wt_path, "README.md"), "w") as f:
        f.write("Worktree version of conflict\n")
    subprocess.run(["git", "-C", wt_path, "commit", "-am", "wt conflict"], check=True)

    session_id = "ses_conf_m"
    server._write_task_record("task_conf_m_0", {
        "status": "completed",
        "working_directory": wt_path,
        "session_id": session_id,
        "base_branch": base_branch,
        "isolated_branch": "feat-cm-test",
    })

    res = asyncio.run(server.kilo_implement(
        task_instructions="Continue with conflict merge",
        working_directory=wt_path,
        continue_session_id=session_id,
        refresh_base="merge",
        background=False,
    ))

    assert res.startswith("Error:")
    assert "merge conflicts encountered" in res
    assert "README.md" in res

    # Verify rollback: status must be clean and file has wt version
    st = subprocess.run(["git", "-C", wt_path, "status", "--porcelain"], capture_output=True, text=True)
    assert st.stdout.strip() == ""
    with open(os.path.join(wt_path, "README.md")) as f:
        assert f.read() == "Worktree version of conflict\n"


def test_refresh_base_rebase_conflict_rollback(tmp_path, fake_data_dir, mock_run_kilo_custom, monkeypatch):
    """When rebase conflicts occur during refresh_base='rebase', abort and leave worktree untouched."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    repo = _init_git_repo(tmp_path / "repo_conflict_r")
    base_branch = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    wt_path = os.path.join(repo, ".kilo", "worktrees", "feat-cr-test")
    subprocess.run(
        ["git", "-C", repo, "worktree", "add", "-b", "feat-cr-test", wt_path],
        check=True, capture_output=True,
    )

    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("Base version of conflict for rebase\n")
    subprocess.run(["git", "-C", repo, "commit", "-am", "base conflict r"], check=True)

    with open(os.path.join(wt_path, "README.md"), "w") as f:
        f.write("Worktree version of conflict for rebase\n")
    subprocess.run(["git", "-C", wt_path, "commit", "-am", "wt conflict r"], check=True)

    session_id = "ses_conf_r"
    server._write_task_record("task_conf_r_0", {
        "status": "completed",
        "working_directory": wt_path,
        "session_id": session_id,
        "base_branch": base_branch,
        "isolated_branch": "feat-cr-test",
    })

    res = asyncio.run(server.kilo_implement(
        task_instructions="Continue with conflict rebase",
        working_directory=wt_path,
        continue_session_id=session_id,
        refresh_base="rebase",
        background=False,
    ))

    assert res.startswith("Error:")
    assert "rebase conflicts encountered" in res
    assert "README.md" in res

    st = subprocess.run(["git", "-C", wt_path, "status", "--porcelain"], capture_output=True, text=True)
    assert st.stdout.strip() == ""
    with open(os.path.join(wt_path, "README.md")) as f:
        assert f.read() == "Worktree version of conflict for rebase\n"


def test_base_moved_note_in_task_result_and_workspace_status(tmp_path, fake_data_dir, monkeypatch):
    """kilo_task_result and kilo_workspace_status note when base_branch moved since creation."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    repo = _init_git_repo(tmp_path / "repo_moved")
    base_branch = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    c0 = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    wt_path = os.path.join(repo, ".kilo", "worktrees", "feat-moved")
    subprocess.run(
        ["git", "-C", repo, "worktree", "add", "-b", "feat-moved", wt_path],
        check=True, capture_output=True,
    )

    task_id = "task_moved_test_1"
    server._write_task_record(task_id, {
        "status": "completed",
        "working_directory": wt_path,
        "isolated_branch": "feat-moved",
        "base_branch": base_branch,
        "base_commit": c0,
        "exit_code": 0,
    })

    # Before base moves: no note
    st_initial = asyncio.run(server.kilo_workspace_status(working_directory=wt_path))
    assert "base moved by" not in st_initial

    res_initial = asyncio.run(server.kilo_task_result(task_id))
    assert "base moved by" not in res_initial

    # Advance base branch by 2 commits
    for i in range(2):
        with open(os.path.join(repo, f"step_{i}.txt"), "w") as f:
            f.write(f"step {i}\n")
        subprocess.run(["git", "-C", repo, "add", "."], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-m", f"base step {i}"], check=True)

    # After base moves: note must appear
    st_after = asyncio.run(server.kilo_workspace_status(working_directory=wt_path))
    expected_note = f"base moved by 2 commits; merge with refresh_base='merge' or review with git diff {base_branch}...feat-moved"
    assert expected_note in st_after

    res_after = asyncio.run(server.kilo_task_result(task_id))
    assert expected_note in res_after


