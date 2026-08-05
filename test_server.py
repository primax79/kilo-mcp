import asyncio
import json
import os
import sqlite3
import subprocess
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
            proc = subprocess.Popen(["sleep", "5" if self.alive else "0"])
            if not self.alive:
                proc.wait()
            self._procs.append(proc)
            return proc.pid

    fake = FakeSpawn()
    monkeypatch.setattr(server, "_spawn_kilo_background", fake)
    yield fake
    for p in fake._procs:
        if p.poll() is None:
            p.kill()

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
    
    # returns a string containing run_id, exit code, STDOUT and STDERR
    assert "run_id:" in res
    assert "Exit Code: 0" in res
    assert "STDOUT:\nOutcome: success" in res
    assert "STDERR:\n" in res
    
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
        os.path.join(".kilo-worktrees", "feat-x"),
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
    assert (dest / "kilo-mcp-headless-executor" / "SKILL.md").exists()
    assert (dest / "kilo-mcp-conflict-resolver" / "SKILL.md").exists()
    assert (dest / "kilo-mcp-rag-explorer" / "SKILL.md").exists()
    assert "kilo-mcp-headless-executor" in out

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


def test_kilo_task_result_still_running(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    server._write_task_record("abc123", {
        "status": "running", "started": "2026-07-14T00:00:00+00:00",
        "working_directory": "/tmp/w",
        "pid": os.getpid(),  # a genuinely live pid — this test's own process
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
    db_path = tmp_path / "kilo.db"
    started = "2026-07-14T10:00:00+00:00"
    started_ms = int(datetime.fromisoformat(started).timestamp() * 1000)
    _make_fake_kilo_db(str(db_path), worktree=str(tmp_path), session_id="ses_abc",
                       time_created_ms=started_ms + 1000)
    monkeypatch.setattr(server, "KILO_SESSION_DB", str(db_path))

    assert server._find_session_for_task(str(tmp_path), started) == "ses_abc"
    # a directory that doesn't match any project's worktree finds nothing
    assert server._find_session_for_task(str(tmp_path / "other"), started) is None


def test_find_session_for_task_matches_main_repo_root_for_worktree_cwd(tmp_path, monkeypatch):
    """Regression test for a real, systemic bug found live 2026-07-29: Kilo
    records project.worktree as the MAIN repo root, never the specific git
    worktree subdirectory kilo run actually launched in — confirmed by
    inspecting kilo.db directly for isolation='worktree' tasks. Matching
    only against the literal cwd (the worktree path) therefore NEVER
    resolved for any isolated task. This seeds the DB the way Kilo actually
    does (worktree = main repo root) and calls the session lookup with cwd
    set to a real linked worktree subdirectory."""
    repo = _init_git_repo(tmp_path / "repo")
    worktree_path = os.path.join(repo, ".kilo-worktrees", "feat-z")
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

    assert server._find_session_for_task(worktree_path, started) == "ses_isolated"


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


def test_kilo_task_progress_no_session_yet(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setattr(server, "KILO_SESSION_DB", str(tmp_path / "does-not-exist.db"))
    server._write_task_record("t2", {
        "status": "running", "working_directory": str(tmp_path),
        "started": "2026-07-14T00:00:00+00:00",
        "pid": os.getpid(),  # a genuinely live pid — this test's own process
    })
    res = asyncio.run(server.kilo_task_progress(task_id="t2"))
    assert "no matching Kilo session" in res


def test_kilo_task_progress_full(tmp_path, monkeypatch):
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
        "pid": os.getpid(),  # a genuinely live pid — this test's own process
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
            "sleep", "30", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
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
        os.path.join(".kilo-worktrees", "feat-z"),
    ]
    kilo_call = mock_run_kilo_custom.calls[1]
    assert kilo_call["cwd"] == os.path.join(cwd, ".kilo-worktrees", "feat-z")
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
    assert worktree_call["cmd"][-1] == os.path.join(".kilo-worktrees", branch)


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


def test_kilo_implement_collision_warning(tmp_path, fake_data_dir, mock_run_kilo_custom, monkeypatch):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    cwd = str(tmp_path)
    server._write_task_record("other111", {
        "status": "running", "started": "2026-07-16T00:00:00+00:00",
        "working_directory": cwd, "agent": "code",
        "pid": os.getpid(),  # a genuinely live pid — this test's own process
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


def test_kilo_implement_background_collision_warning_and_isolation_note(tmp_path, fake_data_dir, mock_spawn_kilo_background, mock_run_kilo_custom, monkeypatch):
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    cwd = str(tmp_path)
    server._write_task_record("other555", {
        "status": "running", "started": "2026-07-16T00:00:00+00:00",
        "working_directory": cwd, "agent": "code",
        "pid": os.getpid(),  # a genuinely live pid — this test's own process
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


def test_find_running_tasks_for_cwd_realpath_normalization(tmp_path, monkeypatch):
    """A trailing slash or a symlinked path must still match — same-directory
    collision detection compares real paths, not raw strings."""
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    server._write_task_record("t1", {
        "status": "running", "working_directory": str(real_dir),
        "pid": os.getpid(),  # a genuinely live pid — this test's own process
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
    worktree_path = os.path.join(repo, ".kilo-worktrees", "feat-x")
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
    wt_path = os.path.join(root, ".kilo-worktrees", "feat-x")
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
    wt_path = os.path.join(root, ".kilo-worktrees", "feat-y")

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


def test_kilo_task_progress_registers_session_in_agent_manager(tmp_path, monkeypatch):
    """Once kilo_task_progress resolves a session_id for a running task, it
    must also register that session into the working_directory's
    .kilo/agent-manager.json (the actual feature this section covers)."""
    repo = _init_git_repo(tmp_path / "repo")
    monkeypatch.setattr(server, "TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setattr(server, "_find_session_for_task", lambda cwd, started: "ses_progress")
    server._write_task_record("t9", {
        "status": "running", "working_directory": repo,
        "started": "2026-07-14T00:00:00+00:00",
        "pid": os.getpid(),  # a genuinely live pid — this test's own process
    })

    asyncio.run(server.kilo_task_progress(task_id="t9"))

    state_file = os.path.join(repo, ".kilo", "agent-manager.json")
    with open(state_file) as f:
        data = json.load(f)
    assert "ses_progress" in data["sessions"]
    assert data["sessions"]["ses_progress"]["worktreeId"] is None
