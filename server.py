from mcp.server.mcpserver import MCPServer as FastMCP
import asyncio
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
import glob
from datetime import datetime, timezone
from typing import Annotated, Literal, Optional
from pydantic import Field

# stderr only: stdout is the MCP JSON-RPC framing channel for a stdio server,
# so anything written there would corrupt the protocol stream.
logger = logging.getLogger("kilo-mcp")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(asctime)s [kilo-mcp] %(levelname)s %(message)s"))
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)

# Server-level instructions are delivered to every MCP client at initialize
# time, so directives here reach any connected assistant on install — no
# per-user memory or prompt setup needed.
_SERVER_INSTRUCTIONS = """kilo-mcp exposes the local Kilo Code CLI: kilo_implement delegates development work, kilo_rag_search queries the semantic index of the workspace.

IMPORTANT — the indexing resource is yours to use directly: kilo_rag_search reads the codebase index maintained by the Kilo IDE extension, and it is available for YOUR OWN exploration and development work, not only as a scoping step before delegating to kilo_implement. Whether or not you delegate the implementation, prefer kilo_rag_search over grep-style search for conceptual queries ('where is X handled?') on indexed workspaces — it is cheaper than pulling files into your context and works from intent rather than exact strings.

## Orchestration workflow (for the connected assistant)
- Phase 1 — Discovery & RAG: explore with kilo_rag_search; check capabilities with kilo_list_models and kilo_auth_status.
- Phase 2 — Isolation: kilo_implement has NO isolation and NO locking by default — it runs directly in working_directory (defaulting to the server's own cwd) with nothing preventing a concurrent writer (another kilo_implement call, or your own git commands) from racing it in the same tree. Pass isolation='worktree' on kilo_implement (or call kilo_create_worktree yourself first) for anything beyond a single trivial edit, and always when dispatching parallel tasks against the same repo. If you skip isolation, kilo_implement still warns you when another task is already running against the exact same working_directory — do not proceed past that warning without a plan (wait, isolate, or pick a different directory).
- Phase 3 — Delegation: call kilo_implement in parallel across the worktrees. It runs in the BACKGROUND BY DEFAULT and returns a task_id immediately — the conversation is never blocked waiting for Kilo.
- Phase 4 — Monitoring & intervention: scale how closely you watch a task to its complexity/risk. Small, well-scoped delegations need only a final kilo_task_result check. Large, multi-file, or high-risk ones are worth polling periodically with kilo_task_progress (Kilo's own live plan/todo list, recent commentary, running cost — read from its session database, not a heuristic); kilo_task_status gives a quicker but coarser OS-level signal. If a task drifts off-spec or looks stuck, kilo_task_cancel stops it, and a corrective kilo_implement call with continue_session_id resumes the same session with corrective instructions instead of starting blind.
- Phase 5 — Verification & review: inspect results with kilo_workspace_status and review every Final Report.
- Phase 6 — Closure & telemetry: log defects found during review with kilo_log_issue.

## Metrics analysis (for the connected assistant)
- Call kilo_metrics to analyze ROI (delegation_cost_usd vs inline_estimate_usd).
- Review kilo_log_issue defects to improve future specs."""

_SERVER_INSTRUCTIONS_RAG_ONLY = """kilo-mcp (RAG-Only Search Engine) exposes the local Kilo Code semantic codebase index.

IMPORTANT — This server is running in READ-ONLY RAG mode. You can query the workspace's semantic index with kilo_rag_search for your own conceptual Q&A, codebase exploration, and location mapping. No execution or file modification tools are enabled on this server."""

_IS_RAG_ONLY = (
    "--rag-only" in sys.argv or
    os.environ.get("KILO_MCP_RAG_ONLY", "").lower() in ("1", "true", "yes")
)

_active_instructions = _SERVER_INSTRUCTIONS_RAG_ONLY if _IS_RAG_ONLY else _SERVER_INSTRUCTIONS
mcp = FastMCP("KiloCode Server" if not _IS_RAG_ONLY else "KiloCode RAG Server", instructions=_active_instructions)

# ---------------------------------------------------------------------------
# Configuration
#
# Resolution order (first hit wins): environment variable > config file >
# built-in default. Env-first keeps the standard MCP pattern working (clients
# pass settings via the `env` block of their server registration); the TOML
# file is the comfortable place for everything else.
#
# Config file search order:
#   1. $KILO_MCP_CONFIG (explicit path)
#   2. kilo-mcp.toml next to this server.py
#   3. ~/.config/kilo-mcp/config.toml
# ---------------------------------------------------------------------------
def _config_file_candidates() -> list[str]:
    candidates = []
    if os.environ.get("KILO_MCP_CONFIG"):
        candidates.append(os.environ["KILO_MCP_CONFIG"])
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "kilo-mcp.toml"))
    candidates.append(os.path.expanduser("~/.config/kilo-mcp/config.toml"))
    return candidates


def _minimal_toml_load(path: str) -> dict:
    """Fallback reader for when `tomllib` is unavailable (Python <3.11 and no
    `tomli` installed) — real case observed: `uv run --no-project` (required
    to avoid uv trying to install this checkout as a package — see README)
    ignores pyproject.toml's `requires-python` and can silently resolve to a
    pre-3.11 interpreter, at which point the previous tomllib-or-nothing
    loader silently returned {} and every config file setting was ignored
    with no error at all.

    Only covers what this server's own config keys actually use: `[section]`
    headers, `key = "quoted string"`, `key = \"\"\"triple-quoted\"\"\"`
    (for `delegation_policy`), and bare int/float — no arrays, inline
    tables, or nested sections. That is deliberately not a general TOML
    parser; if the config file needs more than this, install on Python 3.11+
    instead of extending it."""
    result: dict = {}
    section = result
    with open(path, "r") as f:
        lines = f.readlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        i += 1
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r"^\[([^\]]+)\]$", stripped)
        if m:
            section = result.setdefault(m.group(1).strip(), {})
            continue
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$', stripped)
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip()
        if raw.startswith('"""'):
            body = raw[3:]
            if body.endswith('"""'):
                value = body[:-3]
            else:
                parts = [body]
                while i < len(lines) and '"""' not in lines[i]:
                    parts.append(lines[i].rstrip("\n"))
                    i += 1
                if i < len(lines):
                    parts.append(lines[i].split('"""')[0])
                    i += 1
                value = "\n".join(parts).strip("\n")
        elif raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            value = raw[1:-1]
        else:
            try:
                value = int(raw)
            except ValueError:
                try:
                    value = float(raw)
                except ValueError:
                    value = raw
        section[key] = value
    return result


def _load_config_file() -> dict:
    try:
        import tomllib  # Python 3.11+
        def _read(path):
            with open(path, "rb") as f:
                return tomllib.load(f)
    except ImportError:
        _read = _minimal_toml_load
    for path in _config_file_candidates():
        try:
            cfg = _read(path)
            cfg["_source"] = path
            return cfg
        except FileNotFoundError:
            continue
        except Exception as e:  # malformed file: fail loudly, not silently
            raise RuntimeError(f"Invalid kilo-mcp config file {path}: {e}") from e
    return {}


def _upsert_toml_string_key(path: str, section: str, key: str, value: str) -> None:
    """Set `[section]\\nkey = "value"` in the TOML file at `path`, creating the
    file/section/key as needed and leaving everything else untouched.

    Deliberately not a general TOML writer (the stdlib has no TOML writer,
    and pulling in a dependency for this single call site isn't worth it):
    only handles the flat `key = "quoted string"` shape this server's own
    config keys use, via targeted line insertion/replacement on the raw
    text so existing comments and ordering survive."""
    try:
        with open(path, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []

    section_header = f"[{section}]"
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    section_re = re.compile(r"^\s*\[([^\]]+)\]\s*$")
    new_line = f'{key} = "{value}"\n'

    section_start = None
    for i, line in enumerate(lines):
        m = section_re.match(line)
        if m and m.group(1).strip() == section:
            section_start = i
            break

    if section_start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if lines:
            lines.append("\n")
        lines.append(section_header + "\n")
        lines.append(new_line)
    else:
        section_end = len(lines)
        for j in range(section_start + 1, len(lines)):
            if section_re.match(lines[j]):
                section_end = j
                break
        for j in range(section_start + 1, section_end):
            if key_re.match(lines[j]):
                lines[j] = new_line
                break
        else:
            lines.insert(section_start + 1, new_line)

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.writelines(lines)


_CONFIG = _load_config_file()


def _cfg(env_name: str, section: str, key: str, default, cast=str):
    """Resolve one setting: env var > config file [section].key > default."""
    if os.environ.get(env_name) is not None:
        try:
            return cast(os.environ[env_name])
        except ValueError:
            return default
    try:
        return cast(_CONFIG[section][key])
    except (KeyError, TypeError, ValueError):
        return default


# Timeout (seconds) for a single Kilo subprocess before we give up.
KILO_TIMEOUT = _cfg("KILO_MCP_TIMEOUT", "kilo", "timeout", 1800, int)

# Server API Endpoint Configuration (for communicating with running kilo serve instance)
KILO_SERVER_URL = _cfg("KILO_SERVER_URL", "kilo", "server_url", "")
KILO_SERVER_PASSWORD = _cfg("KILO_SERVER_PASSWORD", "kilo", "server_password", "")


def _resolve_kilo_server_port(pid: str) -> Optional[str]:
    """Resolve the real listening TCP port for a `kilo serve --port 0` process,
    where the OS assigns an ephemeral port that `ps aux` cannot show."""
    try:
        res = subprocess.run(["lsof", "-Pan", "-p", pid, "-i", "tcp"],
                              capture_output=True, text=True, timeout=5)
    except Exception:
        logger.warning("lsof port resolution failed for kilo serve pid=%s", pid, exc_info=True)
        return None
    if res.returncode != 0:
        return None
    for line in res.stdout.splitlines():
        if "LISTEN" not in line:
            continue
        m = re.search(r":(\d+)\s*\(LISTEN\)", line)
        if m:
            return m.group(1)
    return None


def _list_active_kilo_servers() -> list[tuple[str, Optional[str]]]:
    """Enumerate every running `kilo serve` instance on this machine. There can
    be more than one at a time — observed in practice as one per open VS Code
    window — and each is its own daemon with its own in-memory session state
    even though they all persist to the same shared `kilo.db`. Returns a list
    of (url, password), in `ps aux` order."""
    if KILO_SERVER_URL:
        return [(KILO_SERVER_URL.rstrip("/"), KILO_SERVER_PASSWORD)]

    try:
        res = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
    except Exception:
        logger.warning("`ps aux` discovery of kilo serve failed", exc_info=True)
        return []

    if res.returncode != 0:
        return []

    servers = []
    for line in res.stdout.splitlines():
        if "kilo serve" not in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        pid = parts[1]
        m_port = re.search(r"--port\s+(\d+)", line)
        port = m_port.group(1) if m_port else None
        if port and port != "0":
            servers.append((f"http://127.0.0.1:{port}", KILO_SERVER_PASSWORD))
        elif port == "0":
            real_port = _resolve_kilo_server_port(pid)
            if real_port:
                servers.append((f"http://127.0.0.1:{real_port}", KILO_SERVER_PASSWORD))
            else:
                logger.warning(
                    "kilo serve pid=%s runs with --port 0 but lsof could not resolve its bound port", pid
                )
    return servers


def _discover_active_kilo_server() -> tuple[Optional[str], Optional[str]]:
    """Discover a single running `kilo serve` instance — only safe to use when
    no specific existing session/request is involved (e.g. creating a brand
    new session, where any live instance is equally valid). For an operation
    scoped to an EXISTING session_id or request_id, use
    `_try_all_kilo_servers` instead: with several instances running, 'the
    first one found' is not necessarily the one that owns that session, and
    calling the wrong one 404s silently even though the right instance is
    simultaneously live."""
    servers = _list_active_kilo_servers()
    return servers[0] if servers else (None, None)


async def _try_all_kilo_servers(op, *args):
    """Try a session/request-scoped REST operation against every live `kilo
    serve` instance in turn, returning the first (result, server_url) where
    result is truthy, or (None, None) if none succeeded. `op` is one of the
    `_kilo_server_*` async helpers below, called as `op(server_url, password,
    *args)`."""
    for srv_url, password in _list_active_kilo_servers():
        result = await op(srv_url, password, *args)
        if result:
            return result, srv_url
    return None, None


def _kilo_server_auth_header(password: Optional[str]) -> dict:
    """Build the Basic-Auth header `kilo serve` expects, if a password is configured."""
    if not password:
        return {}
    import base64
    auth = base64.b64encode(f":{password}".encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {auth}"}


def _kilo_server_request(server_url: str, password: Optional[str], path: str,
                          method: str = "GET", payload: Optional[dict] = None,
                          timeout: float = 10, extra_headers: Optional[dict] = None) -> tuple[int, bytes]:
    """Shared HTTP helper for the `kilo serve` REST API. Returns (status, body).
    Raises on transport/HTTP failure — callers log and translate to their own
    return-value contract (None/False on failure)."""
    import urllib.request

    headers = {"Content-Type": "application/json"}
    headers.update(_kilo_server_auth_header(password))
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(f"{server_url}{path}", data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


async def _kilo_server_create_session(server_url: str, password: Optional[str],
                                    title: Optional[str] = None) -> Optional[str]:
    """Create a new session directly on a running `kilo serve` instance
    via `POST /session`. Returns the created session_id."""
    payload = {"title": title} if title else {}
    try:
        def _do_req():
            status, body = _kilo_server_request(server_url, password, "/session", method="POST", payload=payload)
            if status in (200, 201):
                res_data = json.loads(body.decode("utf-8"))
                return res_data.get("id") or res_data.get("sessionID")
            return None
        return await asyncio.to_thread(_do_req)
    except Exception:
        logger.warning("kilo serve create_session failed (url=%s)", server_url, exc_info=True)
        return None


async def _kilo_server_prompt_async(server_url: str, password: Optional[str],
                                    session_id: str, prompt_text: str) -> bool:
    """Send a prompt instruction directly to a running `kilo serve` session
    via `POST /session/:sessionID/prompt_async`. Returns True if accepted."""
    payload = {"parts": [{"type": "text", "text": prompt_text}]}
    try:
        def _do_req():
            status, _ = _kilo_server_request(server_url, password, f"/session/{session_id}/prompt_async",
                                               method="POST", payload=payload)
            return status in (200, 201, 202, 204)
        return await asyncio.to_thread(_do_req)
    except Exception:
        logger.warning("kilo serve prompt_async failed (session=%s, url=%s)", session_id, server_url, exc_info=True)
        return False


async def _kilo_server_stop_session(server_url: str, password: Optional[str],
                                    session_id: str) -> bool:
    """Stop/abort a running session directly via `POST /session/:sessionID/abort`.
    Returns True on success."""
    try:
        def _do_req():
            status, _ = _kilo_server_request(server_url, password, f"/session/{session_id}/abort", method="POST")
            return status in (200, 201, 202, 204)
        return await asyncio.to_thread(_do_req)
    except Exception:
        logger.warning("kilo serve stop_session failed (session=%s, url=%s)", session_id, server_url, exc_info=True)
        return False


async def _kilo_server_revert_session(server_url: str, password: Optional[str],
                                      session_id: str, message_id: Optional[str] = None) -> bool:
    """Revert a session to a previous state via `POST /session/:sessionID/revert`."""
    payload = {"messageID": message_id} if message_id else {}
    try:
        def _do_req():
            status, _ = _kilo_server_request(server_url, password, f"/session/{session_id}/revert",
                                               method="POST", payload=payload)
            return status in (200, 201, 202, 204)
        return await asyncio.to_thread(_do_req)
    except Exception:
        logger.warning("kilo serve revert_session failed (session=%s, url=%s)", session_id, server_url, exc_info=True)
        return False


async def _kilo_server_fork_session(server_url: str, password: Optional[str],
                                    session_id: str, message_id: Optional[str] = None) -> Optional[str]:
    """Fork a session via `POST /session/:sessionID/fork`. Returns the new session_id."""
    payload = {"messageID": message_id} if message_id else {}
    try:
        def _do_req():
            status, body = _kilo_server_request(server_url, password, f"/session/{session_id}/fork",
                                                   method="POST", payload=payload)
            if status in (200, 201):
                res_data = json.loads(body.decode("utf-8"))
                return res_data.get("id") or res_data.get("sessionID")
            return None
        return await asyncio.to_thread(_do_req)
    except Exception:
        logger.warning("kilo serve fork_session failed (session=%s, url=%s)", session_id, server_url, exc_info=True)
        return None


async def _kilo_server_respond_question(server_url: str, password: Optional[str],
                                         request_id: str, answers: list[str]) -> bool:
    """Respond to an interactive question/prompt request via `POST /question/:requestID`."""
    payload = {"answers": answers}
    try:
        def _do_req():
            status, _ = _kilo_server_request(server_url, password, f"/question/{request_id}",
                                               method="POST", payload=payload)
            return status in (200, 201, 202, 204)
        return await asyncio.to_thread(_do_req)
    except Exception:
        logger.warning("kilo serve respond_question failed (request=%s, url=%s)", request_id, server_url, exc_info=True)
        return False


async def _kilo_server_update_todo(server_url: str, password: Optional[str],
                                    session_id: str, todos: list[dict]) -> bool:
    """Push a todo list update directly via `POST /session/:sessionID/todo`."""
    try:
        def _do_req():
            status, _ = _kilo_server_request(server_url, password, f"/session/{session_id}/todo",
                                               method="POST", payload={"todos": todos}, timeout=5)
            return status in (200, 201, 202, 204)
        return await asyncio.to_thread(_do_req)
    except Exception:
        logger.warning("kilo serve update_todo failed (session=%s, url=%s)", session_id, server_url, exc_info=True)
        return False


async def _kilo_server_fetch_session_events(server_url: str, password: Optional[str],
                                            session_id: str, timeout_s: float = 3.0) -> list[dict]:
    """Fetch recent live events (or SSE stream snapshot) for a session from `kilo serve` (`GET /event`).
    Returns parsed event objects.

    Reads incrementally off the socket and buffers across chunk boundaries
    (SSE events are separated by a blank line, `\\n\\n`) until `timeout_s`
    elapses, so an event isn't dropped just because it straddled two reads —
    the previous version read one bounded 8KB chunk and lost anything past
    it or split across it."""
    import socket
    import urllib.request

    headers = {"Accept": "text/event-stream"}
    headers.update(_kilo_server_auth_header(password))
    req = urllib.request.Request(f"{server_url}/event", headers=headers)

    events = []
    try:
        def _read_sse():
            deadline = time.monotonic() + timeout_s
            buf = ""
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                while time.monotonic() < deadline:
                    try:
                        chunk = resp.read(8192)
                    except (socket.timeout, TimeoutError):
                        break
                    if not chunk:
                        break
                    buf += chunk.decode("utf-8", errors="replace")
                    while "\n\n" in buf:
                        raw_event, buf = buf.split("\n\n", 1)
                        for line in raw_event.splitlines():
                            if not line.startswith("data:"):
                                continue
                            try:
                                ev = json.loads(line[5:].strip())
                            except Exception:
                                logger.debug("kilo serve SSE: dropped unparsable event line: %r", line, exc_info=True)
                                continue
                            if isinstance(ev, dict) and (not session_id or ev.get("sessionID") == session_id or ev.get("session_id") == session_id):
                                events.append(ev)
        await asyncio.to_thread(_read_sse)
    except Exception:
        logger.warning("kilo serve fetch_session_events failed (session=%s, url=%s)", session_id, server_url, exc_info=True)
    return events


def _write_db_todos(session_id: str, todos: list[dict]) -> bool:
    """Directly write/update todos for a session in Kilo's SQLite database (kilo.db).
    Fallback path used by kilo_update_session_todo when no live `kilo serve` REST
    endpoint was discovered — bypasses Kilo's own API. Foreign keys are disabled
    below to tolerate writing todos for a session row that hasn't landed yet
    (e.g. a race with kilo_implement's lazy session-linking); log it clearly
    when that happens so a silent orphan write is at least visible."""
    try:
        os.makedirs(os.path.dirname(KILO_SESSION_DB), exist_ok=True)
        now_ms = int(time.time() * 1000)
        con = sqlite3.connect(KILO_SESSION_DB, timeout=2)
        with con:
            if not con.execute("SELECT 1 FROM session WHERE id = ?", (session_id,)).fetchone():
                logger.warning(
                    "_write_db_todos: no `session` row yet for session_id=%s — writing "
                    "todos ahead of session creation (FK disabled to tolerate this)",
                    session_id,
                )
            con.execute("PRAGMA foreign_keys = OFF")
            con.execute("DELETE FROM todo WHERE session_id = ?", (session_id,))
            for pos, item in enumerate(todos):
                con.execute(
                    "INSERT INTO todo (session_id, position, content, status, priority, time_created, time_updated) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        pos,
                        item.get("content", ""),
                        item.get("status", "pending"),
                        item.get("priority", "medium"),
                        now_ms,
                        now_ms,
                    ),
                )
        con.close()
        return True
    except Exception:
        logger.warning("_write_db_todos failed (session=%s)", session_id, exc_info=True)
        return False

# Grace period kilo_task_cancel waits after SIGTERM before escalating to
# SIGKILL. Not user-configurable (an implementation detail); a module-level
# constant so tests can shorten it.
_CANCEL_GRACE_S = 2.0

# Default model used by kilo_implement when the caller does not override it —
# the "simple/routine task" tier. Must be a valid provider/model id as
# reported by `kilo models`.
DEFAULT_MODEL = _cfg("KILO_MCP_DEFAULT_MODEL", "kilo", "default_model",
                     "google/gemini-flash-latest")

# The "complex/high-risk task" tier: a stronger (usually costlier) model, for
# tasks with subtle failure modes — read-modify-write races, ambiguous specs,
# security-sensitive edits, multi-step reasoning — where the connected
# assistant should NOT reach for the same model it uses for trivial edits.
# Falls back to DEFAULT_MODEL when unset, so this is fully opt-in. Which
# providers/models exist at all is environment-specific (see
# `kilo_list_models`) — kilo-mcp itself stays provider-agnostic; run
# `--configure-models` to set both tiers interactively.
COMPLEX_MODEL = _cfg("KILO_MCP_COMPLEX_MODEL", "kilo", "complex_model",
                     DEFAULT_MODEL)

# What Kilo's own model tokens cost the operator (USD per million tokens).
# Defaults to 0 (free) — true for operators whose Kilo API keys are provided
# at no charge (e.g. institution-supplied Google keys) — but Kilo is NOT free
# for everyone: set these to the real rates of the configured Kilo model and
# both the default delegation policy and the cost estimates adapt.
KILO_INPUT_PER_MTOK = _cfg("KILO_MCP_KILO_INPUT_PER_MTOK", "kilo",
                           "input_cost_per_mtok", 0.0, float)
KILO_OUTPUT_PER_MTOK = _cfg("KILO_MCP_KILO_OUTPUT_PER_MTOK", "kilo",
                            "output_cost_per_mtok", 0.0, float)
KILO_IS_FREE = KILO_INPUT_PER_MTOK == 0.0 and KILO_OUTPUT_PER_MTOK == 0.0

# Delegation policy injected into the kilo_implement tool description so Claude
# self-selects when to delegate. Override via the KILO_MCP_DELEGATION_POLICY env
# var to tune how aggressively Claude hands work off to Kilo.
#
# The economics: doing a task inline runs an agentic loop whose resent context
# dominates cost; delegating moves that whole loop into Kilo, so Claude pays
# only for a short spec + a review. When Kilo execution is free to the operator
# (the default cost model), delegation wins for any large, well-specifiable
# work. When the operator pays for Kilo tokens too, Kilo's own loop cost joins
# the equation and delegation only wins where Kilo's per-token rate is cheaper
# than Claude's — so the default policy text is picked accordingly.
_DEFAULT_POLICY_FREE = (
    "Kilo runs at no cost to this operator; the only cost is your own (Claude) "
    "tokens. DELEGATE to Kilo when doing the task yourself would need a large "
    "agentic loop that is specifiable up front — multi-file changes, "
    "bulk/mechanical generation (e.g. tests for every endpoint), wide refactors, "
    "anything where the resent context of an inline loop would dwarf a written "
    "spec. ALSO PREFER DELEGATING when Kilo has a skill or agent matching the "
    "task's domain (check kilo_list_skills / kilo_list_agents; pass them via "
    "skills_to_load / agent): domain-specific skills and agents encode proven "
    "procedures and pitfalls, making Kilo's output better than an unguided "
    "inline attempt regardless of size. "
    "DO IT YOURSELF when the task is small (one or a few files, short "
    "edits), needs tight back-and-forth reasoning that can't be pinned down in a "
    "spec, or when writing the spec plus reviewing Kilo's output would cost more "
    "tokens than just doing it inline. Either way, always review the returned "
    "report."
)
_DEFAULT_POLICY_PAID = (
    f"Kilo execution costs this operator real money "
    f"(~${KILO_INPUT_PER_MTOK}/M input, ~${KILO_OUTPUT_PER_MTOK}/M output "
    "tokens), so delegation is not automatically cheaper: the comparison is "
    "spec + review (Claude tokens) + Kilo's own agentic loop (Kilo tokens) "
    "versus doing it inline (Claude tokens). DELEGATE large, well-specifiable "
    "work — multi-file changes, bulk/mechanical generation, wide refactors — "
    "where Kilo's cheaper per-token rate on the heavy loop outweighs the "
    "spec+review overhead, and when Kilo has a skill or agent matching the "
    "task's domain (check kilo_list_skills / kilo_list_agents; pass them via "
    "skills_to_load / agent) — encoded expertise raises output quality beyond "
    "what the token comparison alone suggests. "
    "DO IT YOURSELF when the task is small, needs tight "
    "back-and-forth reasoning, or when Kilo's rates are not clearly below "
    "Claude's for the bulk of the work. Either way, always review the returned "
    "report."
)
_DEFAULT_DELEGATION_POLICY = _DEFAULT_POLICY_FREE if KILO_IS_FREE else _DEFAULT_POLICY_PAID
DELEGATION_POLICY = _cfg("KILO_MCP_DELEGATION_POLICY", "kilo", "delegation_policy",
                         _DEFAULT_DELEGATION_POLICY)

# ---------------------------------------------------------------------------
# Metrics & feedback
#
# Every kilo_implement / kilo_rag_search run appends one JSON record to
# metrics.jsonl; issues found in generated code (logged via kilo_log_issue)
# go to issues.jsonl, keyed by run_id. kilo_metrics aggregates both.
#
# Cost model (all overridable via env):
#   - Claude pricing: what the operator pays for Claude tokens.
#   - Delegation cost = spec written by Claude (output tokens) + Kilo's report
#     read back by Claude (input tokens).
#   - Inline estimate = what Claude would have spent generating the same code
#     itself in an agentic loop: output ≈ generated_tokens × OUTPUT_FACTOR
#     (retries/corrections), input ≈ output × INPUT_PER_OUTPUT (context resent
#     across turns; default is a deliberately conservative, cache-adjusted
#     value — real Kilo sessions on this machine show 40–170× raw).
# ---------------------------------------------------------------------------
DATA_DIR = os.path.expanduser(_cfg(
    "KILO_MCP_DATA_DIR", "metrics", "data_dir", "~/.local/share/kilo-mcp"
))
METRICS_FILE = os.path.join(DATA_DIR, "metrics.jsonl")
ISSUES_FILE = os.path.join(DATA_DIR, "issues.jsonl")

CLAUDE_INPUT_PER_MTOK = _cfg("KILO_MCP_CLAUDE_INPUT_PER_MTOK", "metrics",
                             "claude_input_per_mtok", 5.0, float)
CLAUDE_OUTPUT_PER_MTOK = _cfg("KILO_MCP_CLAUDE_OUTPUT_PER_MTOK", "metrics",
                              "claude_output_per_mtok", 25.0, float)
CHARS_PER_TOKEN = _cfg("KILO_MCP_CHARS_PER_TOKEN", "metrics", "chars_per_token", 4.0, float)
INLINE_OUTPUT_FACTOR = _cfg("KILO_MCP_INLINE_OUTPUT_FACTOR", "metrics",
                            "inline_output_factor", 1.2, float)
INLINE_INPUT_PER_OUTPUT = _cfg("KILO_MCP_INLINE_INPUT_PER_OUTPUT", "metrics",
                               "inline_input_per_output", 40.0, float)


def _tok(chars: int) -> int:
    """Rough chars→tokens estimate."""
    return int(chars / CHARS_PER_TOKEN)


def _append_jsonl(path: str, record: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # metrics must never break the actual run


def _parse_final_report(stdout: str) -> dict:
    """Extract outcome and changed-file list from Kilo's Final Report."""
    outcome = None
    # Same line only (bounded by [^\n]), but tolerant of whatever sits between
    # the "Outcome" label and its value - annotations, extra words, a second
    # label like "(final status)". \W{0,6} previously required the gap to be
    # *only* non-word characters, so anything wordier ("Outcome, in the end,
    # was a success") missed entirely; confirmed against real telemetry where
    # a clean process exit (exit_code 0, real output) still left outcome=None.
    m = re.search(r"Outcome[^\n]{0,40}?\b(success|partial|failed)\b", stdout, re.IGNORECASE)
    if m:
        outcome = m.group(1).lower()
    files: list[str] = []

    def _clean(fragment: str) -> Optional[str]:
        """Normalize one candidate file entry; None if it isn't a path."""
        frag = re.sub(r"\(.*?\)", "", fragment)      # drop "(created)" annotations
        frag = frag.strip().strip("`*").strip()
        if not frag or frag.lower() in {"none", "n/a", "-"}:
            return None
        # a path-ish token: no spaces, contains a dot or slash
        if " " in frag or not re.search(r"[./]", frag):
            return None
        return frag

    collecting = False
    for line in stdout.splitlines():
        header = re.search(r"Files changed\**:?\s*(.*)$", line, re.IGNORECASE)
        if header and not collecting:
            collecting = True
            inline = _clean(header.group(1))         # single-line form: "Files changed: foo.py"
            if inline:
                files.append(inline)
            continue
        if collecting:
            if re.search(r"\*\*(Verification|Issues|Outcome)|^#{1,3}\s|^(Verification|Issues|Outcome)\b", line, re.IGNORECASE):
                break
            # bulleted entry, or bare filename on its own line
            m = re.match(r"\s*[-*]\s*(.+)$", line)
            candidate = _clean(m.group(1) if m else line)
            if candidate:
                files.append(candidate)
            elif not line.strip():
                continue
    return {"outcome": outcome, "files_changed": files}


def _measure_files(cwd: str, files: list[str]) -> dict:
    """Measure the size of changed files that still exist (deleted ones skip)."""
    total_chars = 0
    total_lines = 0
    measured = 0
    for rel in files:
        p = rel if os.path.isabs(rel) else os.path.join(cwd, rel)
        try:
            with open(p, "r", errors="replace") as f:
                content = f.read()
            total_chars += len(content)
            total_lines += content.count("\n") + 1
            measured += 1
        except OSError:
            continue
    return {"files_measured": measured, "code_chars": total_chars, "code_lines": total_lines}


def _estimate_costs(spec_chars: int, result_chars: int, code_chars: int) -> dict:
    """Delegation cost (Claude spend + Kilo execution) vs inline-generation estimate."""
    spec_tokens = _tok(spec_chars)          # Claude wrote this → output tokens
    result_tokens = _tok(result_chars)      # Claude reads this back → input tokens
    claude_cost = (
        spec_tokens * CLAUDE_OUTPUT_PER_MTOK + result_tokens * CLAUDE_INPUT_PER_MTOK
    ) / 1_000_000
    gen_tokens = _tok(code_chars)
    # Both Kilo's loop and a hypothetical inline-Claude loop have the same
    # shape (generated output × retry factor, input = resent context); only
    # the per-token rates differ.
    loop_out = gen_tokens * INLINE_OUTPUT_FACTOR
    loop_in = loop_out * INLINE_INPUT_PER_OUTPUT
    kilo_cost = (
        loop_out * KILO_OUTPUT_PER_MTOK + loop_in * KILO_INPUT_PER_MTOK
    ) / 1_000_000
    delegation = claude_cost + kilo_cost
    inline = (
        loop_out * CLAUDE_OUTPUT_PER_MTOK + loop_in * CLAUDE_INPUT_PER_MTOK
    ) / 1_000_000
    return {
        "spec_tokens": spec_tokens,
        "result_tokens": result_tokens,
        "generated_code_tokens": gen_tokens,
        "claude_cost_usd": round(claude_cost, 4),
        "kilo_execution_cost_usd": round(kilo_cost, 4),
        "delegation_cost_usd": round(delegation, 4),
        "inline_estimate_usd": round(inline, 4),
        "estimated_savings_usd": round(inline - delegation, 4),
    }


def _vscode_kilo_storage() -> str:
    """Platform path of the Kilo VS Code extension's globalStorage, where
    marketplace-installed skills live (mirrors kilocode paths.ts)."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "Code",
                            "User", "globalStorage", "kilocode.kilo-code")
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming")),
                            "Code", "User", "globalStorage", "kilocode.kilo-code")
    return os.path.join(home, ".config", "Code", "User", "globalStorage", "kilocode.kilo-code")


def _find_kilo_files(pattern: str, cwd: Optional[str] = None) -> list[str]:
    """Helper to find Kilo customization files where Kilo actually discovers
    them (see kilocode paths.ts): project .kilo/.kilocode, the global ~/.kilo
    and ~/.kilocode, and the VS Code extension's globalStorage (marketplace
    skills). Note that ~/.config/kilo holds Kilo's *config* (kilo.jsonc) but
    is NOT scanned for skills/agents."""
    files = []
    cwd = cwd or os.getcwd()
    files.extend(glob.glob(os.path.join(cwd, ".kilo", pattern), recursive=True))
    files.extend(glob.glob(os.path.join(cwd, ".kilocode", pattern), recursive=True))
    home = os.path.expanduser("~")
    files.extend(glob.glob(os.path.join(home, ".kilo", pattern), recursive=True))
    files.extend(glob.glob(os.path.join(home, ".kilocode", pattern), recursive=True))
    files.extend(glob.glob(os.path.join(_vscode_kilo_storage(), pattern), recursive=True))
    return files


async def _run_kilo(cmd: list[str], cwd: str, env: dict, on_start=None) -> tuple[Optional[int], str, str]:
    """Run a Kilo subprocess without blocking the asyncio event loop.

    Using asyncio's subprocess API (instead of the blocking subprocess.run)
    is what allows Claude's parallel tool calls to actually run concurrently:
    multiple invocations await here and are driven by the same event loop.

    `on_start`, if given, is called with the subprocess's PID right after it
    is spawned — this is how background tasks record a PID for kilo_task_cancel
    before waiting on the (possibly very long) subprocess to finish.

    stdin is explicitly /dev/null: without this, the child inherits THIS
    server's own stdin — which, over the stdio MCP transport, is the live
    JSON-RPC pipe to the client, not a terminal. If `kilo run` ever probes or
    reads stdin, it blocks forever on that pipe (confirmed live: zero CPU
    growth, no session ever created in kilo.db — the exact "stuck" symptom —
    while the identical command launched from an interactive shell, with a
    normal stdin, completed in seconds).

    Returns (returncode, stdout, stderr). returncode is None on timeout.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if on_start:
        on_start(proc.pid)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=KILO_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        # Reap the killed process so we don't leak a zombie.
        await proc.wait()
        return (
            None,
            "",
            f"Kilo process exceeded the timeout of {KILO_TIMEOUT}s and was terminated.",
        )
    return (
        proc.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


@mcp.tool()
async def kilo_list_agents(
    working_directory: Annotated[
        Optional[str],
        Field(
            description="Absolute path of the project whose agents you want to "
            "list. Pass the same path you will use in kilo_implement / "
            "kilo_rag_search, so project-level agents of THAT repo are included. "
            "Defaults to the server's current working directory."
        ),
    ] = None,
) -> str:
    """Discover which Kilo agents are available for the `agent` argument of
    kilo_rag_search / kilo_implement.

    Scans the project (`.kilo/agent/`, `.kilocode/agent/`), the global
    (`~/.kilo/agent/`, `~/.kilocode/agent/`) directories and the VS Code
    extension storage for custom agent definitions. Built-in agents such as
    `code` (implementation) and `explore` (read-only investigation) always
    exist even if nothing is listed here.

    Call this before delegating if you are unsure which agent to target, so you
    pick a real one instead of guessing.
    """
    agent_files = _find_kilo_files("agent*/**/*.md", cwd=working_directory)
    if not agent_files:
        return "No custom agents found. Default agents (like 'code', 'explore') are available."
    agents = set(os.path.basename(f).replace(".md", "") for f in agent_files)
    return "Available Kilo Agents:\n" + "\n".join(f"- {agent}" for agent in sorted(agents))


@mcp.tool()
async def kilo_list_skills(
    working_directory: Annotated[
        Optional[str],
        Field(
            description="Absolute path of the project whose skills you want to "
            "list. Pass the same path you will use in kilo_implement, so "
            "project-level skills of THAT repo are included. Defaults to the "
            "server's current working directory."
        ),
    ] = None,
) -> str:
    """Discover which Kilo skills are available for the `skills_to_load`
    argument of kilo_implement.

    Scans project and global directories (plus the VS Code extension storage
    for marketplace-installed skills) for `SKILL.md` files. Skills are named
    workflows/knowledge packs that Kilo can adopt for a task. Use the names
    returned here verbatim in `skills_to_load`; passing a name that is not
    listed has no effect.
    """
    skill_files = _find_kilo_files("skill*/**/SKILL.md", cwd=working_directory)
    if not skill_files:
        return "No custom skills found."
    skills = set(os.path.basename(os.path.dirname(f)) for f in skill_files)
    return "Available Kilo Skills:\n" + "\n".join(f"- {skill}" for skill in sorted(skills))


@mcp.tool()
async def kilo_list_models(
    filter: Annotated[
        Optional[str],
        Field(
            description="Case-insensitive substring to narrow the list (e.g. "
            "'gemini', 'claude', 'pro'). Leave empty to return every model. "
            "The full list can be hundreds of entries, so filtering is recommended."
        ),
    ] = None,
) -> str:
    """List the model ids that Kilo can actually use, in `provider/model` form.

    Use this to validate the `model` argument of kilo_implement BEFORE
    delegating: passing a model id that Kilo does not know makes the run fail.
    (For example `google/gemini-3.5-pro` does not exist, but
    `google/gemini-3.5-flash` does.)

    Reflects the providers you are authenticated with; run `kilo auth login` in
    a terminal to add more. This is a read-only discovery call.
    """
    try:
        returncode, stdout, stderr = await _run_kilo(
            ["kilo", "models"], cwd=os.getcwd(), env=os.environ.copy()
        )
        lines = [ln for ln in stdout.splitlines() if "/" in ln]
        if filter:
            needle = filter.lower()
            lines = [ln for ln in lines if needle in ln.lower()]
        if not lines:
            hint = f" matching '{filter}'" if filter else ""
            return f"No models{hint} found.\n\nRaw exit code: {returncode}\nSTDERR:\n{stderr}"
        header = f"Available Kilo models ({len(lines)}"
        header += f" matching '{filter}'):" if filter else " total):"
        return header + "\n" + "\n".join(f"- {ln.strip()}" for ln in sorted(set(lines)))
    except FileNotFoundError:
        return "Error: The 'kilo' executable was not found in the PATH."
    except Exception as e:
        return f"Error listing Kilo models: {str(e)}"


@mcp.tool()
async def kilo_rag_search(
    query: Annotated[
        str,
        Field(
            description="Natural-language description of what to find (concepts, "
            "behaviours, symbols). This is a semantic/vector search backed by "
            "Kilo's codebase index, so describe intent rather than exact strings."
        ),
    ],
    working_directory: Annotated[
        Optional[str],
        Field(
            description="Absolute path to the workspace root to search. MUST be "
            "the exact same absolute path Kilo indexed (the vector collection is "
            "keyed by workspace path), otherwise the search misses the index. "
            "Defaults to the server's current working directory."
        ),
    ] = None,
    path: Annotated[
        Optional[str],
        Field(
            description="Optional sub-directory (relative to working_directory) to "
            "restrict the search. Leave empty to search the whole workspace."
        ),
    ] = None,
) -> str:
    """Delegate READ-ONLY codebase search/exploration to Kilo, so you (the
    architect) can locate relevant code without pulling the whole repository
    into your own context.

    This tool is a standing resource for YOUR OWN work: use it whenever you
    need to understand or navigate an indexed codebase — also (and especially)
    when you are implementing or answering questions yourself and NOT
    delegating anything to kilo_implement.

    Runs Kilo's `explore` agent, which uses semantic (vector) search over the
    workspace's existing index plus targeted file reads, and returns a summary
    with file paths and code snippets. It does NOT modify files.

    Note: this reuses the index maintained by the Kilo IDE extension — it does
    not create or refresh the index. If a workspace was never indexed by the
    extension, semantic search falls back to plain text/glob search.

    Typical flow: call this to find the relevant files, reason about the design
    yourself, then hand the concrete file list to kilo_implement via focus_files.

    Returns a text report: `Exit Code`, `STDOUT` (the findings), `STDERR` (the
    run trace). Review STDOUT as the reviewer.
    """
    cwd = working_directory if working_directory else os.getcwd()
    if not os.path.exists(cwd):
        return f"Error: Working directory {cwd} does not exist."
    try:
        prompt = f"Please perform a semantic search in the codebase for the following query: '{query}'."
        if path:
            prompt += f" Limit the search to the '{path}' directory."
        prompt += (
            "\n\nIf the 'kilo-mcp-rag-explorer' skill is available, load it and follow "
            "it. Use your semantic_search tool (falling back to explore/grep only if "
            "needed) to find the best matches. Return a detailed summary of the findings, "
            "including file paths and relevant code snippets. Do NOT modify any files."
        )

        # `kilo run` is non-interactive by default (it sends a single prompt,
        # streams events to stdout and exits when the session goes idle), so no
        # special env var is needed to keep it from hanging on prompts.
        cmd = ["kilo", "run", "--agent", "explore", prompt]
        env = os.environ.copy()

        started = time.monotonic()
        returncode, stdout, stderr = await _run_kilo(cmd, cwd=cwd, env=env)
        _append_jsonl(METRICS_FILE, {
            "run_id": uuid.uuid4().hex[:12],
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": "kilo_rag_search",
            "working_directory": cwd,
            "duration_s": round(time.monotonic() - started, 1),
            "exit_code": returncode,
            "query_chars": len(query),
            "result_chars": len(stdout) + len(stderr),
        })
        return (
            f"Kilo RAG Search Finished.\n\nExit Code: {returncode}\n\n"
            f"STDOUT:\n{stdout}\n\nSTDERR:\n{stderr}"
        )
    except FileNotFoundError:
        return "Error: The 'kilo' executable was not found in the PATH."
    except Exception as e:
        return f"Error executing Kilo RAG search: {str(e)}"


_IMPLEMENT_DESCRIPTION = (
    """Delegate a concrete code IMPLEMENTATION task to Kilo (the executor) while
you stay the architect/orchestrator/reviewer.

⚠️ SIDE EFFECTS: Kilo runs non-interactively and AUTO-APPROVES its actions —
it will create/edit/delete files and run commands in `working_directory`
without asking. Only delegate work you have specified precisely, in a
version-controlled directory, and review the diff afterwards.

NON-BLOCKING BY DEFAULT: this tool starts the task and returns IMMEDIATELY
with a `task_id` — it does NOT wait for Kilo to finish, so the calling
conversation is never blocked on a long-running delegation. Use:
- kilo_task_progress — live plan/todo list, recent commentary, cost so far.
- kilo_task_status — quick OS-level heuristic (any `kilo run` process).
- kilo_task_result — the final report, once it is done.
- kilo_task_cancel — stop it if it is going wrong.
Pass `background=false` only for short tasks where you deliberately want to
block and get the full report back in this same call.

How to use it well:
1. (Optional) kilo_rag_search to locate the relevant files.
2. Write a precise spec in `task_instructions`; pass the key files as
   `focus_files`; add `execution_hints` and `skills_to_load` as needed.
3. Pick a valid `model` (see kilo_list_models) and `agent`.
4. Scale how closely you watch it to the task's complexity/risk: a small,
   well-scoped change needs only a final kilo_task_result check; a large,
   multi-file, or high-risk delegation deserves periodic kilo_task_progress
   polling so you can catch it drifting off-spec early.
5. Act as reviewer: check `Exit Code` and read `STDOUT` to verify the
   strategy was followed; inspect the resulting changes.

STEERING: if a run (background or foreground) drifts off-spec, don't just
restart from scratch — read its `session_id` (in the result / progress
report), optionally kilo_task_cancel the current run, then call
kilo_implement again with `continue_session_id` set to that id and corrective
`task_instructions`. Kilo resumes the SAME session with full memory of what
it already built, rather than starting blind. (The Kilo CLI queues a
follow-up message even into a still-running session, so this also works as a
live nudge without cancelling first.)

Parallelism: emitting several kilo_implement calls in one turn runs them
concurrently (independent Kilo subprocesses) — useful for isolated features
in different directories.

⚠️ NO ISOLATION, NO LOCKING BY DEFAULT: Kilo runs directly in
`working_directory` with nothing preventing a concurrent writer — another
kilo_implement call, or your own git commands — from racing it in the same
tree (uncommitted changes lost, branches reset out from under a run,
whichever writer runs last silently wins). Pass `isolation='worktree'` to
auto-create a dedicated git worktree + branch for this run (equivalent to
calling kilo_create_worktree yourself first) — use it for anything beyond a
single trivial edit, and always when dispatching parallel tasks against the
same repo. If you don't isolate, the response is prefixed with a collision
warning whenever another task is already running against the exact same
`working_directory` — treat that warning as a stop sign, not a formality.

Returns a text report: `Exit Code` (0 = success, None = timed out), `STDOUT`
(Kilo's final report), `STDERR` (the run trace)."""
    + "\n\n## When to delegate\n"
    + DELEGATION_POLICY
)


@mcp.tool(description=_IMPLEMENT_DESCRIPTION)
async def kilo_implement(
    task_instructions: Annotated[
        str,
        Field(
            description="Detailed, self-contained Markdown specification of the "
            "work to perform. YOU are the architect: state goals, constraints, "
            "acceptance criteria and edge cases explicitly — Kilo executes what "
            "is written and does not share your conversation context. Long specs "
            "are fine; they are passed via a temp-file bridge, not the shell."
        ),
    ],
    working_directory: Annotated[
        Optional[str],
        Field(
            description="Absolute path to the repository/workspace Kilo should "
            "run in and modify. Defaults to the server's current working "
            "directory. Ensure it is under version control before delegating."
        ),
    ] = None,
    agent: Annotated[
        str,
        Field(
            description="Kilo agent to use. `code` for implementation (default); "
            "`explore` for read-only tasks. Use kilo_list_agents to see custom "
            "agents."
        ),
    ] = "code",
    model: Annotated[
        str,
        Field(
            description="Model id in `provider/model` form. MUST be a real id "
            "from kilo_list_models; an unknown id makes the run fail. This "
            "environment has two configured tiers — pick by task risk, don't "
            "just reuse whatever model you (the connected assistant) happen "
            "to be: simple/routine tasks (mechanical edits, low risk) -> "
            + DEFAULT_MODEL + "; complex/high-risk tasks (subtle bugs, "
            "read-modify-write races, security-sensitive edits, multi-step "
            "reasoning) -> " + COMPLEX_MODEL + ". Defaults to " + DEFAULT_MODEL +
            ". Run `kilo-mcp --configure-models` to change these tiers, or "
            "kilo_list_models for the full catalog."
        ),
    ] = DEFAULT_MODEL,
    focus_files: Annotated[
        Optional[list[str]],
        Field(
            description="Paths (absolute, or relative to working_directory) that "
            "Kilo must read before making changes. Populate this with the files "
            "you identified via kilo_rag_search to keep Kilo focused."
        ),
    ] = None,
    execution_hints: Annotated[
        Optional[str],
        Field(
            description="Strategic guidance on HOW to execute: ordering, "
            "constraints, patterns to follow, things to avoid, how to verify."
        ),
    ] = None,
    skills_to_load: Annotated[
        Optional[list[str]],
        Field(
            description="Names of Kilo skills (from kilo_list_skills) that Kilo "
            "should apply to this task. Only listed names have any effect."
        ),
    ] = None,
    env_vars: Annotated[
        Optional[dict],
        Field(
            description="Extra environment variables for the Kilo subprocess "
            "(e.g. {'DEBUG': '1'}). Powerful and rarely needed; can override "
            "Kilo's runtime config. Leave empty unless debugging."
        ),
    ] = None,
    background: Annotated[
        bool,
        Field(
            description="Default true: start the task and return IMMEDIATELY "
            "with a task_id instead of blocking the conversation until Kilo "
            "finishes. Monitor with kilo_task_progress (live plan/commentary) "
            "or kilo_task_status (quick heuristic); fetch the outcome with "
            "kilo_task_result; stop it with kilo_task_cancel. Set to false only "
            "for short tasks where blocking for the full report in this same "
            "call is actually preferable."
        ),
    ] = True,
    continue_session_id: Annotated[
        Optional[str],
        Field(
            description="Kilo session id to resume instead of starting fresh "
            "(see the session_id surfaced by kilo_task_progress / a prior "
            "kilo_implement result). Use this to STEER a delegation: after "
            "reviewing or cancelling a run that went off track, send corrective "
            "task_instructions into the SAME session so Kilo keeps the context "
            "of what it already built. Leave empty for a fresh session."
        ),
    ] = None,
    isolation: Annotated[
        Optional[Literal["worktree"]],
        Field(
            description="Set to 'worktree' to isolate this run: auto-creates a "
            "new git worktree + branch under working_directory (equivalent to "
            "calling kilo_create_worktree yourself first, then passing its path "
            "as working_directory — but one step, harder to forget) and runs "
            "Kilo there instead of directly in working_directory. kilo_implement "
            "has NO isolation and NO locking by default: nothing stops a second "
            "concurrent call (or your own git commands) from racing this one in "
            "the same directory. Use isolation='worktree' for anything beyond a "
            "single trivial edit, and always when dispatching multiple "
            "kilo_implement calls in parallel against the same repo. Leave unset "
            "to run directly in working_directory (you'll get a warning if "
            "another task is already running there — see the tool description). "
            "The worktree (and later the session, once known) are also "
            "registered in .kilo/agent-manager.json best-effort, so they can "
            "show up in the Kilo Agent Manager UI — but there is no live sync "
            "with a running VS Code extension window: if it's open on this "
            "repo, its own next save can silently overwrite this registration "
            "with its own in-memory state. Reliable when the extension isn't "
            "running against this repo, or visible after its next reload "
            "otherwise."
        ),
    ] = None,
    worktree_branch: Annotated[
        Optional[str],
        Field(
            description="Branch name for the new worktree when isolation="
            "'worktree'. Defaults to 'kilo/<task_id>' if omitted. Ignored "
            "unless isolation is set."
        ),
    ] = None,
    worktree_base_branch: Annotated[
        Optional[str],
        Field(
            description="Base branch/commit for the new worktree when "
            "isolation='worktree'. Defaults to the current branch (git "
            "worktree add's own default). Ignored unless isolation is set."
        ),
    ] = None,
) -> str:
    """Delegate a code implementation task to Kilo. See the tool description
    (built from _IMPLEMENT_DESCRIPTION + DELEGATION_POLICY) for full guidance."""
    cwd = working_directory if working_directory else os.getcwd()
    if not os.path.exists(cwd):
        return f"Error: Working directory {cwd} does not exist."

    run_id = uuid.uuid4().hex[:12]
    collision_warning = ""
    isolated_branch = None
    agent_manager_worktree_id = None
    if isolation == "worktree":
        isolated_branch = worktree_branch or f"kilo/{run_id}"
        worktree_path, wt_rc, wt_stdout, wt_stderr, agent_manager_worktree_id = await _create_worktree(
            cwd, isolated_branch, worktree_base_branch, os.environ.copy()
        )
        if not worktree_path:
            return (
                f"Error: could not create isolated worktree (branch '{isolated_branch}') "
                f"under {cwd}.\nExit code: {wt_rc}\nSTDOUT: {wt_stdout}\nSTDERR: {wt_stderr}"
            )
        cwd = worktree_path
    else:
        collision_warning = _collision_warning(cwd, exclude_run_id=run_id)

    # Section order is deliberate: context files first (read before acting),
    # then the task, then strategy, then skills, and finally the report
    # contract that Claude-as-reviewer parses from STDOUT.
    sections = ["# Task Specification from the Orchestrating Architect"]
    if focus_files:
        file_list = "\n".join(f"- {f}" for f in focus_files)
        sections.append(
            "## Context Files — read these FIRST\n"
            "Before doing anything else, read the following files to build context:\n"
            f"{file_list}"
        )
    sections.append("## Task Instructions\n" + task_instructions)
    if execution_hints:
        sections.append("## Execution Hints & Strategy\n" + execution_hints)
    if skills_to_load:
        skill_list = "\n".join(f"- {s}" for s in skills_to_load)
        sections.append(
            "## Required Skills\n"
            "Load and follow each of these skills before starting the task:\n"
            f"{skill_list}"
        )
    sections.append(
        "## Final Report (mandatory)\n"
        "Your final message MUST be a report with exactly these sections:\n"
        "- **Outcome**: success | partial | failed, with one sentence why.\n"
        "- **Files changed**: every file created/modified/deleted, one per line.\n"
        "- **Verification**: commands you ran to verify (tests, builds) and their results.\n"
        "- **Issues**: anything incomplete, skipped, or needing the architect's review; 'none' otherwise."
    )
    full_content = "\n\n".join(sections)

    env = os.environ.copy()
    if env_vars:
        for key, value in env_vars.items():
            env[key] = str(value)

    if not background:
        result = await _execute_implement(
            run_id, cwd, full_content, agent, model, env,
            focus_files, skills_to_load, continue_session_id,
            agent_manager_worktree_id,
        )
        return collision_warning + result

    # Background mode (the default): launch Kilo as a FULLY DETACHED OS
    # process — its own session (start_new_session=True / setsid), output
    # redirected to a log file — instead of an asyncio task awaited inside
    # this server process. This is what lets the task survive an MCP server
    # restart/crash instead of getting silently killed with it: PID liveness
    # and the log file are OS-level state, not in-process state, so
    # kilo_task_result/_progress can reconstruct the outcome later even from
    # a brand-new server instance (see _reconcile_task_if_orphaned).
    os.makedirs(TASKS_DIR, exist_ok=True)
    prompt_file = None
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(full_content)
        prompt_file = f.name
    # If connected to an active kilo serve instance, attempt immediate session creation via REST API
    server_session_id = continue_session_id
    srv_url, srv_pass = _discover_active_kilo_server()
    if srv_url and not server_session_id:
        created_sid = await _kilo_server_create_session(srv_url, srv_pass, title=f"MCP Task {run_id}")
        if created_sid:
            server_session_id = created_sid

    cmd = ["kilo", "run"]
    if server_session_id:
        cmd.extend(["--session", server_session_id])
    if agent:
        cmd.extend(["--agent", agent])
    if model:
        cmd.extend(["--model", model])
    cmd.append(
        f"The file {prompt_file} contains your full task specification from an "
        "orchestrating architect. Read it now, then execute it completely. Your "
        "final message must be the 'Final Report' described in that file."
    )
    log_path = os.path.join(TASKS_DIR, f"{run_id}.log")
    try:
        pid = _spawn_kilo_background(cmd, cwd, env, log_path)
    except FileNotFoundError:
        return collision_warning + "Error: The 'kilo' executable was not found in the PATH."
    except Exception as e:
        return collision_warning + f"Error launching Kilo in background: {e}"

    _write_task_record(run_id, {
        "status": "running",
        "started": datetime.now(timezone.utc).isoformat(),
        "working_directory": cwd,
        "agent": agent,
        "model": model,
        "isolated_branch": isolated_branch,
        "agent_manager_worktree_id": agent_manager_worktree_id,
        "pid": pid,
        "log_path": log_path,
        "prompt_file": prompt_file,
        # Continuing or created session_id: record it up front so kilo_task_progress
        # finds it immediately from the first millisecond.
        "session_id": server_session_id,
    })
    if server_session_id:
        _agent_manager_note_session(cwd, server_session_id, agent_manager_worktree_id)
    else:
        # Schedule lazy session linking fallback if session creation via REST wasn't available
        try:
            loop = asyncio.get_running_loop()
            async def _lazy_link():
                await asyncio.sleep(1.5)
                sid = _find_session_for_task(cwd, datetime.now(timezone.utc).isoformat())
                if sid:
                    _agent_manager_note_session(cwd, sid, agent_manager_worktree_id)
            loop.create_task(_lazy_link())
        except Exception:
            pass

    session_note = (
        f"- Continuing session: {continue_session_id}\n"
        if continue_session_id else
        "- Session id: not yet created; kilo_task_progress will surface it "
        "once Kilo starts.\n"
    )
    isolation_note = (
        f"- Isolated in worktree: {cwd} (branch '{isolated_branch}') — merge or "
        "remove it (git worktree remove) once you've reviewed the result.\n"
        if isolated_branch else ""
    )
    return (
        collision_warning +
        f"Task started in background. (task_id: {run_id})\n\n"
        f"{session_note}"
        f"{isolation_note}"
        f"- Live progress (plan/todo list, commentary, cost): kilo_task_progress "
        f"with task_id '{run_id}'\n"
        f"- Quick OS-level heuristic: kilo_task_status (workspace {cwd})\n"
        f"- Fetch the result when done: kilo_task_result with task_id '{run_id}'\n"
        f"- Stop it if it's going wrong: kilo_task_cancel with task_id '{run_id}'\n"
        "Note: the task runs as a detached OS process (pid "
        f"{pid}), independent of this MCP server — it survives a server "
        "restart or crash; kilo_task_result/_progress will recover the "
        "outcome from its log even if a different server instance answers "
        "the call."
    )


TASKS_DIR = os.path.join(DATA_DIR, "tasks")


def _spawn_kilo_background(cmd: list[str], cwd: str, env: dict, log_path: str) -> int:
    """Launch `kilo run` as a fully detached OS process: stdout/stderr
    redirected to `log_path`, in its own session via `start_new_session=True`
    (POSIX setsid). This is the actual fix for tasks getting stuck at
    status='running' forever — the previous design (`asyncio.create_task` +
    `asyncio.create_subprocess_exec`, awaited inside this server process)
    tied the child's lifetime to this server's own process/process-group, so
    it died silently whenever the server did. A detached child keeps running
    and keeps writing to `log_path` regardless of what happens to this
    server.

    stdin=DEVNULL is equally load-bearing and easy to overlook: without it
    the child inherits this server's own stdin, which over the stdio MCP
    transport is the live JSON-RPC pipe to the client — not a terminal, not
    closed, never sending Kilo anything. Any stdin probe/read by `kilo run`
    then blocks forever on that pipe (confirmed live: zero CPU growth, no
    session ever created in kilo.db, indefinitely — the exact symptom this
    whole background-execution mechanism exists to detect and recover from,
    except here the cause was in this server's own subprocess call, not in
    Kilo or the model). Returns the child's pid."""
    log_fd = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fd, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_fd.close()  # the child holds its own fd table entry to the file
    return proc.pid


def _is_pid_alive(pid: Optional[int]) -> bool:
    """True if a PID still refers to a live, non-zombie process. Good enough
    for our reconcile check since PID reuse inside the few-minutes window a
    task runs for is astronomically unlikely.

    Two steps, found necessary by a real live test: a detached child
    (start_new_session=True) is still OUR child for wait() purposes even
    though it's in its own session/process group — so once it exits, it
    becomes a zombie (<defunct>) until reaped, and os.kill(pid, 0) alone
    reports a zombie as "alive" forever (it still exists in the process
    table, purely pending reap), which made kilo_task_result say a task was
    "still running" for a task that had already finished its work.
    1. Try a non-blocking reap first (os.waitpid WNOHANG). If it's our own
       child and already exited, this both collects it (no more zombie) and
       tells us it's done.
    2. ChildProcessError means it's not our child (e.g. a fresh server
       instance after a restart — the process was reparented to init, which
       reaps it on its own) — fall back to os.kill(pid, 0)."""
    if not pid:
        return False
    try:
        reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
        if reaped_pid == pid:
            return False  # just reaped it — it had already exited
        # reaped_pid == 0: it's our child and it's still running
    except ChildProcessError:
        pass  # not our child (different server instance) — fall through
    except OSError:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal — still alive
    except OSError:
        return False
    return True


def _task_record_path(task_id: str) -> str:
    return os.path.join(TASKS_DIR, f"{task_id}.json")


def _write_task_record(task_id: str, updates: dict) -> None:
    """Merge updates into the task's JSON record on disk."""
    try:
        os.makedirs(TASKS_DIR, exist_ok=True)
        path = _task_record_path(task_id)
        record = {}
        if os.path.exists(path):
            with open(path) as f:
                record = json.load(f)
        record.update(updates)
        with open(path, "w") as f:
            json.dump(record, f, ensure_ascii=False)
    except OSError:
        pass  # task bookkeeping must never break the actual run


def _read_task_record(task_id: str) -> Optional[dict]:
    """Read a task's JSON record, or None if unknown/unreadable."""
    path = _task_record_path(task_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _find_running_tasks_for_cwd(cwd: str, exclude_run_id: Optional[str] = None) -> list[tuple[str, dict]]:
    """Return (task_id, record) pairs for every task still marked 'running'
    whose working_directory resolves to the same real path as `cwd`.

    This is the collision guard for the non-isolated path: kilo_implement has
    no locking, so two calls targeting the exact same directory (a second
    kilo_implement, or the orchestrator's own git commands) can race each
    other silently. Comparing real paths (not raw strings) catches the common
    case of the same directory reached via a symlink or a trailing slash."""
    if not os.path.isdir(TASKS_DIR):
        return []
    target = os.path.realpath(cwd)
    matches = []
    try:
        filenames = os.listdir(TASKS_DIR)
    except OSError:
        return []
    for filename in filenames:
        if not filename.endswith(".json"):
            continue
        task_id = filename[:-len(".json")]
        if task_id == exclude_run_id:
            continue
        record = _read_task_record(task_id)
        if not record or record.get("status") != "running":
            continue
        if not _is_pid_alive(record.get("pid")):
            continue  # orphaned/dead process — not a real collision, just a
            # record nobody has reconciled yet (see _reconcile_task_if_orphaned)
        other_dir = record.get("working_directory")
        if other_dir and os.path.realpath(other_dir) == target:
            matches.append((task_id, record))
    return matches


def _collision_warning(cwd: str, exclude_run_id: str) -> str:
    """Build a loud warning string if another task is already running against
    `cwd`, or "" if there's no collision. Prepended to kilo_implement's return
    value rather than blocking the call — a false positive (e.g. an operator
    intentionally running two sequential tasks moments apart) should not be
    silently prevented, but a real collision must never be silent either."""
    others = _find_running_tasks_for_cwd(cwd, exclude_run_id=exclude_run_id)
    if not others:
        return ""
    details = "; ".join(
        f"task_id {tid} (agent={rec.get('agent')}, started={rec.get('started')})"
        for tid, rec in others
    )
    return (
        "⚠️ COLLISION WARNING: another kilo_implement task is already running "
        f"against this exact working_directory ({cwd}): {details}. Concurrent "
        "writes to the same working tree are not locked or serialized in any "
        "way — they can race (uncommitted changes lost, branches reset "
        "unexpectedly, live conflicts on the exact files being edited). "
        "Consider isolation='worktree', a different working_directory, or "
        "waiting for that task via kilo_task_status/kilo_task_result before "
        "proceeding.\n\n"
    )


async def _execute_implement(run_id: str, cwd: str, full_content: str,
                             agent: str, model: str, env: dict,
                             focus_files: Optional[list],
                             skills_to_load: Optional[list],
                             continue_session_id: Optional[str] = None,
                             agent_manager_worktree_id: Optional[str] = None) -> str:
    """Run one Kilo implementation task end-to-end: temp-file spec bridge,
    subprocess, Final-Report parsing, metrics. Used by the synchronous
    (background=False) path of kilo_implement only — the background path
    spawns a detached process directly (_spawn_kilo_background) instead, so
    it isn't tied to this server call's own lifetime."""
    prompt_file = None
    started_wall = datetime.now(timezone.utc).isoformat()
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(full_content)
            prompt_file = f.name

        # `kilo run` is non-interactive by default, so it executes the prompt
        # and exits without waiting for approvals. The temp file is cleaned up
        # by this server (finally block) — Kilo must not waste a step on it.
        cmd = ["kilo", "run"]
        if continue_session_id:
            cmd.extend(["--session", continue_session_id])
        if agent:
            cmd.extend(["--agent", agent])
        if model:
            cmd.extend(["--model", model])
        cmd.append(
            f"The file {prompt_file} contains your full task specification from an "
            "orchestrating architect. Read it now, then execute it completely. Your "
            "final message must be the 'Final Report' described in that file."
        )

        started = time.monotonic()
        returncode, stdout, stderr = await _run_kilo(cmd, cwd=cwd, env=env)
        duration = round(time.monotonic() - started, 1)

        # Resolve the Kilo session id so the caller can steer/inspect it later
        # (kilo_task_progress, or continue_session_id on a follow-up call).
        session_id = continue_session_id or _find_session_for_task(cwd, started_wall)
        if session_id:
            _agent_manager_note_session(cwd, session_id, agent_manager_worktree_id)

        result_text = (
            f"Kilo Execution Finished. (run_id: {run_id} — use it with kilo_log_issue "
            f"if your review finds defects; session_id: {session_id or 'unknown'} — pass "
            f"it as continue_session_id on a follow-up kilo_implement call to steer this "
            f"same session)\n\nExit Code: {returncode}\n\n"
            f"STDOUT:\n{stdout}\n\nSTDERR:\n{stderr}"
        )

        report = _parse_final_report(stdout)
        measure = _measure_files(cwd, report["files_changed"])
        costs = _estimate_costs(
            spec_chars=len(full_content),
            result_chars=len(result_text),
            code_chars=measure["code_chars"],
        )
        _append_jsonl(METRICS_FILE, {
            "run_id": run_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": "kilo_implement",
            "working_directory": cwd,
            "agent": agent,
            "model": model,
            "session_id": session_id,
            "duration_s": duration,
            "exit_code": returncode,
            "outcome": report["outcome"],
            "files_changed": report["files_changed"],
            **measure,
            **costs,
            "focus_files": focus_files or [],
            "skills_to_load": skills_to_load or [],
            "spec_chars": len(full_content),
        })
        return result_text
    except FileNotFoundError:
        return "Error: The 'kilo' executable was not found in the PATH."
    except Exception as e:
        return f"Error executing Kilo: {str(e)}"
    finally:
        # Kilo is asked to delete the file itself, but clean up defensively in
        # case it exits early (timeout, crash) and never gets there.
        if prompt_file and os.path.exists(prompt_file):
            try:
                os.remove(prompt_file)
            except OSError:
                pass


def _reconcile_task_if_orphaned(task_id: str, record: dict) -> dict:
    """If `record` says status=='running' but the PID it recorded is
    actually dead, the task finished (or crashed) with nobody left to update
    its record — most commonly because the MCP server that launched it
    restarted or was killed mid-run. Since the task now runs as a detached
    OS process (see _spawn_kilo_background), PID liveness is real OS state
    that any server instance can check, and the log file holds everything
    that happened — so this finalizes the record here instead of leaving it
    stuck at 'running' forever (the bug this whole mechanism replaces).

    Returns the (possibly updated) record; also persists the update via
    _write_task_record so future calls short-circuit on status != 'running'."""
    if record.get("status") != "running":
        return record
    if _is_pid_alive(record.get("pid")):
        return record

    log_path = record.get("log_path")
    log_content = ""
    if log_path and os.path.exists(log_path):
        try:
            with open(log_path, "r", errors="replace") as f:
                log_content = f.read()
        except OSError:
            pass

    cwd = record.get("working_directory")
    session_id = record.get("session_id")
    if not session_id and cwd:
        session_id = _find_session_for_task(cwd, record.get("started", ""))

    report = _parse_final_report(log_content) if log_content else {"outcome": "unknown", "files_changed": []}
    final_status = "completed" if report["outcome"] in ("success", "partial") else "failed"
    result_text = (
        f"Kilo Execution Finished (recovered by kilo-mcp after its process "
        f"exited — task_id: {task_id}, session_id: {session_id or 'unknown'}). "
        "The MCP server that launched this task may have restarted before "
        "it could record the outcome itself; this was reconstructed from "
        f"the task's log file.\n\nLOG:\n{log_content or '(no log content captured)'}"
    )

    if cwd:
        measure = _measure_files(cwd, report["files_changed"])
        costs = _estimate_costs(spec_chars=0, result_chars=len(result_text), code_chars=measure["code_chars"])
        _append_jsonl(METRICS_FILE, {
            "run_id": task_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": "kilo_implement",
            "working_directory": cwd,
            "agent": record.get("agent"),
            "model": record.get("model"),
            "session_id": session_id,
            "outcome": report["outcome"],
            "files_changed": report["files_changed"],
            **measure,
            **costs,
            "recovered_after_restart": True,
        })

    prompt_file = record.get("prompt_file")
    if prompt_file and os.path.exists(prompt_file):
        try:
            os.remove(prompt_file)
        except OSError:
            pass

    updates = {"status": final_status, "result": result_text, "session_id": session_id}
    _write_task_record(task_id, updates)
    if session_id:
        _agent_manager_note_session(cwd, session_id, record.get("agent_manager_worktree_id"))
    record = dict(record)
    record.update(updates)
    return record


@mcp.tool()
async def kilo_task_result(
    task_id: Annotated[
        str,
        Field(description="The task_id returned by kilo_implement when "
              "launched with background=true."),
    ],
) -> str:
    """Fetch the outcome of a background kilo_implement task.

    Returns the full result (Final Report, exit code, run trace, session_id)
    once the task completes; while still running it says so — use
    kilo_task_progress for live plan/commentary or kilo_task_status for a
    quick OS-level heuristic. Tasks run as detached OS processes, independent
    of this server: results persist on disk and self-heal across server
    restarts — even a task that was still running when a previous server
    instance died is recovered here (from its log file) the first time
    anything asks about it, not lost.
    """
    record = _read_task_record(task_id)
    if record is None:
        return f"Unknown task_id '{task_id}'. (Records live in {TASKS_DIR})"
    record = _reconcile_task_if_orphaned(task_id, record)
    status = record.get("status")
    if status == "running":
        return (
            f"Task {task_id} is still running (started {record.get('started')}, "
            f"workspace {record.get('working_directory')}, pid {record.get('pid')}). "
            "Use kilo_task_progress for live plan/commentary/cost, or "
            "kilo_task_status for a quick heuristic."
        )
    if status == "cancelled":
        return (
            f"Task {task_id} was cancelled. "
            + record.get("result", "(no further detail recorded)")
        )
    return record.get("result", f"Task {task_id} ended with status '{status}' and no stored result.")


@mcp.tool()
async def kilo_task_progress(
    task_id: Annotated[
        str,
        Field(description="The task_id returned by kilo_implement when "
              "launched with background=true (the default)."),
    ],
) -> str:
    """Poll FINE-GRAINED LIVE progress of a running background kilo_implement
    task: Kilo's own plan/todo list (each step's status), a tail of its most
    recent commentary, and running cost/tokens — read directly from Kilo's
    session database (kilo.db), not inferred from OS-level process heuristics.

    Use this instead of (or alongside) kilo_task_status when you need to
    actually judge WHAT Kilo is doing, not just whether the process is alive —
    e.g. to decide whether to keep waiting, kilo_task_cancel a run that has
    drifted off-spec, or prepare a corrective kilo_implement call with
    continue_session_id.

    Scale how often you poll to the task's complexity/risk: a small,
    well-scoped delegation rarely needs this at all (just wait for
    kilo_task_result); a large, multi-file, or high-stakes one is worth
    checking periodically so problems surface long before the final report.

    If no session is found yet, Kilo has probably just started — try again in
    a few seconds.
    """
    record = _read_task_record(task_id)
    if record is None:
        return f"Unknown task_id '{task_id}'. (Records live in {TASKS_DIR})"
    record = _reconcile_task_if_orphaned(task_id, record)
    status = record.get("status")
    if status != "running":
        return (
            f"Task {task_id} already ended (status: {status}). "
            "Use kilo_task_result for the final report."
        )
    cwd = record.get("working_directory")
    session_id = record.get("session_id")
    if not session_id and cwd:
        session_id = _find_session_for_task(cwd, record.get("started", ""))
        if session_id:
            _write_task_record(task_id, {"session_id": session_id})
            _agent_manager_note_session(cwd, session_id, record.get("agent_manager_worktree_id"))
    if not session_id:
        return (
            f"Task {task_id} is running but no matching Kilo session was found "
            "yet in the session database — it may have just started. Try again "
            "shortly, or use kilo_task_status for an OS-level heuristic in the "
            "meantime."
        )

    lines = [f"# Live progress — task {task_id} (session {session_id})"]
    summary = _read_session_summary(session_id)
    if summary:
        age_s = time.time() - summary["time_updated"] / 1000
        lines.append(f"- title: {summary['title']}")
        lines.append(
            f"- cost so far: ${summary['cost']:.4f} | tokens in/out: "
            f"{summary['tokens_input']}/{summary['tokens_output']}"
        )
        lines.append(f"- last activity: {int(age_s)}s ago")

    todos = _read_todos(session_id)
    lines.append("\n## Plan / todo list")
    if todos:
        mark = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}
        for t in todos:
            lines.append(f"- {mark.get(t['status'], '[?]')} ({t['priority']}) {t['content']}")
    else:
        lines.append("(none recorded yet — Kilo may not have planned steps for this task)")

    texts = _read_recent_texts(session_id)
    if texts:
        lines.append("\n## Recent commentary (oldest of this batch first)")
        for t in texts:
            snippet = t if len(t) <= 500 else t[:500] + "…"
            lines.append(f"---\n{snippet}")

    return "\n".join(lines)


@mcp.tool()
async def kilo_task_cancel(
    task_id: Annotated[
        str,
        Field(description="The task_id returned by kilo_implement when "
              "launched with background=true (the default)."),
    ],
    reason: Annotated[
        Optional[str],
        Field(description="Why you're cancelling — recorded on the task so "
              "kilo_task_result explains what happened."),
    ] = None,
) -> str:
    """STOP a running background kilo_implement task: this is the direct
    intervention lever, for when kilo_task_progress (or kilo_workspace_status)
    shows a delegation going wrong — off-spec changes, a stuck/looping process,
    burning cost with no progress — and you don't want to wait for the timeout.

    Sends SIGTERM to the tracked process (and attempts an HTTP abort if
    connected to a running `kilo serve` instance), escalating to SIGKILL after a
    couple seconds if it hasn't exited. This is a hard stop — Kilo does not get
    a chance to write its Final Report. Any partial file changes it already
    made are left as-is; review with kilo_workspace_status and decide whether to
    keep, revert, or continue them with a fresh kilo_implement call using
    continue_session_id.
    """
    record = _read_task_record(task_id)
    if record is None:
        return f"Unknown task_id '{task_id}'. (Records live in {TASKS_DIR})"
    status = record.get("status")
    if status != "running":
        return f"Task {task_id} already ended (status: {status}); nothing to cancel."

    # Try HTTP abort first if a running kilo serve instance and session_id are known
    session_id = record.get("session_id")
    if session_id:
        await _try_all_kilo_servers(_kilo_server_stop_session, session_id)

    pid = record.get("pid")
    if not pid:
        _write_task_record(task_id, {"status": "cancelled", "result": f"Cancelled: {reason}" if reason else "Cancelled."})
        return (
            f"Task {task_id} has no recorded process id yet — it may have just "
            "started. Marked as cancelled."
        )

    note = f"Cancelled by architect. Reason: {reason}" if reason else "Cancelled by architect (no reason given)."
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _write_task_record(task_id, {"status": "cancelled", "result": f"{note} (process had already exited.)"})
        return f"Task {task_id}: process {pid} was already gone; marked cancelled."
    await asyncio.sleep(_CANCEL_GRACE_S)
    forced = False
    try:
        os.kill(pid, 0)  # still alive?
        os.kill(pid, signal.SIGKILL)
        forced = True
    except ProcessLookupError:
        pass
    _write_task_record(task_id, {"status": "cancelled", "result": note})
    return (
        f"Task {task_id} (pid {pid}) terminated{' (had to force-kill)' if forced else ''}. "
        f"{note} Review kilo_workspace_status before re-delegating."
    )


@mcp.tool()
async def kilo_log_issue(
    run_id: Annotated[
        str,
        Field(
            description="The run_id printed by kilo_implement's result header. "
            "Links this issue to the exact spec/prompt that produced the code."
        ),
    ],
    category: Annotated[
        str,
        Field(
            description="Short kebab-case defect class, e.g. 'wrong-behavior', "
            "'missing-edge-case', 'ignored-instruction', 'style-mismatch', "
            "'broke-existing-code', 'incomplete', 'hallucinated-api', "
            "'report-inaccurate'. Reuse existing categories when possible so "
            "aggregation stays meaningful."
        ),
    ],
    description: Annotated[
        str,
        Field(description="What was wrong, concretely — enough detail to later "
              "understand how the spec/prompt should have prevented it."),
    ],
    severity: Annotated[
        str,
        Field(description="'minor' | 'major' | 'critical'"),
    ] = "minor",
    file: Annotated[
        Optional[str],
        Field(description="File where the defect was found, if applicable."),
    ] = None,
) -> str:
    """Record a defect found while reviewing code that Kilo generated.

    ALWAYS call this when your post-delegation review finds a problem — this is
    the feedback loop used to tune the instruction prompts over time. Issues are
    aggregated by kilo_metrics; recurring categories indicate what the generated
    spec template must start guarding against.
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "category": category.strip().lower(),
        "severity": severity.strip().lower(),
        "description": description,
        "file": file,
    }
    _append_jsonl(ISSUES_FILE, record)
    return f"Issue logged for run {run_id} ({record['category']}, {record['severity']})."


@mcp.tool()
async def kilo_metrics(
    days: Annotated[
        int,
        Field(description="Look-back window in days (default 30)."),
    ] = 30,
) -> str:
    """Summarize kilo-mcp usage: runs, outcomes, generated code volume, real
    delegation cost vs estimated inline-Claude cost, and defect categories from
    kilo_log_issue. Use this to judge whether delegation is paying off and which
    defect classes the spec template should address next.

    Raw data lives in JSONL files (metrics.jsonl / issues.jsonl) under
    KILO_MCP_DATA_DIR (default ~/.local/share/kilo-mcp) for ad-hoc analysis.
    """
    cutoff = time.time() - days * 86400

    def _load(path):
        rows = []
        try:
            with open(path) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        ts = datetime.fromisoformat(r["ts"]).timestamp()
                        if ts >= cutoff:
                            rows.append(r)
                    except (ValueError, KeyError):
                        continue
        except OSError:
            pass
        return rows

    runs = _load(METRICS_FILE)
    issues = _load(ISSUES_FILE)
    impl = [r for r in runs if r.get("tool") == "kilo_implement"]
    rag = [r for r in runs if r.get("tool") == "kilo_rag_search"]
    if not runs:
        return f"No kilo-mcp activity recorded in the last {days} days. (Data dir: {DATA_DIR})"

    lines = [f"# kilo-mcp metrics — last {days} days", ""]
    if impl:
        outcomes = {}
        for r in impl:
            outcomes[r.get("outcome") or "unknown"] = outcomes.get(r.get("outcome") or "unknown", 0) + 1
        code_lines = sum(r.get("code_lines", 0) for r in impl)
        deleg = sum(r.get("delegation_cost_usd", 0) for r in impl)
        kilo_exec = sum(r.get("kilo_execution_cost_usd", 0) for r in impl)
        inline = sum(r.get("inline_estimate_usd", 0) for r in impl)
        lines += [
            f"## kilo_implement: {len(impl)} runs",
            f"- outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items())),
            f"- files changed: {sum(len(r.get('files_changed', [])) for r in impl)}"
            f" | code generated: {code_lines} lines"
            f" (~{sum(r.get('generated_code_tokens', 0) for r in impl)} tokens)",
            f"- avg duration: {sum(r.get('duration_s', 0) for r in impl) / len(impl):.0f}s",
            f"- cost of delegating: ${deleg:.2f}"
            + (f" (Claude spec+review ${deleg - kilo_exec:.2f}"
               f" + Kilo execution ${kilo_exec:.2f})" if kilo_exec > 0
               else " (Claude spec+review; Kilo execution at no cost)"),
            f"- estimated cost if Claude had generated inline: ${inline:.2f}",
            f"- estimated savings: ${inline - deleg:.2f}"
            f" ({(inline / deleg):.0f}x cheaper)" if deleg > 0 else "",
            "",
        ]
    if rag:
        lines += [f"## kilo_rag_search: {len(rag)} runs "
                  f"(avg {sum(r.get('duration_s', 0) for r in rag) / len(rag):.0f}s)", ""]
    if issues:
        by_cat = {}
        for i in issues:
            key = (i.get("category", "?"), i.get("severity", "?"))
            by_cat[key] = by_cat.get(key, 0) + 1
        lines += [f"## defects logged: {len(issues)} "
                  f"(issue rate: {len(issues) / max(1, len(impl)):.2f} per run)"]
        for (cat, sev), n in sorted(by_cat.items(), key=lambda x: -x[1]):
            lines.append(f"- {cat} [{sev}]: {n}")
        lines.append("")
        lines.append("→ Recurring categories above are candidates for new guard "
                     "clauses in the spec template / delegation prompts.")
    else:
        lines.append("## defects logged: none")
    lines.append(f"\n(Raw JSONL: {METRICS_FILE} and {ISSUES_FILE})")
    return "\n".join(filter(None, lines))



# One asyncio.Lock per repo (keyed by realpath), so concurrent worktree
# creation calls against the SAME repo from this server process serialize
# instead of racing on git's own .git/index.lock — mirrors the mutex the
# real Kilo VS Code extension's own WorktreeManager uses for this. Does not
# protect against an external `git` command running at the same moment
# (would need an OS-level file lock); that's a much rarer race and out of
# scope for the "tasks stuck at running forever" bug this file otherwise fixes.
# ---------------------------------------------------------------------------
# Agent Manager (.kilo/agent-manager.json) integration
#
# The Kilo VS Code extension's own "Agent Manager" UI reads its worktree/
# session registry from <repo_root>/.kilo/agent-manager.json (see
# WorktreeStateManager.ts in kilocode). We write into the same file so
# worktrees/sessions created via this MCP server can show up there too.
#
# IMPORTANT, confirmed by reading WorktreeStateManager.ts directly: the
# extension loads this file ONCE at startup into an in-memory Map, never
# watches it for external changes, and every save serializes its *entire*
# in-memory state (full overwrite, not a merge). So an entry we add here is
# only guaranteed to survive until the extension's own next save (any UI
# action — toggling a section, renaming a tab, etc.) while it's running in
# the same repo: that save will overwrite our addition with its own
# (unaware) in-memory snapshot. This is a structural limitation of the
# extension's design, not something fixable from this side. Writing is still
# worthwhile: it's correct whenever the extension isn't currently running
# against this repo, and becomes visible on the extension's next startup
# load in that case. The write itself uses the extension's own atomic
# tmp-file-then-rename scheme so a concurrent reader never sees a partial
# file, and an flock-guarded read-modify-write serializes our own
# (kilo-mcp-server-originated) concurrent writers against each other.
# ---------------------------------------------------------------------------

def _agent_manager_root(cwd: str) -> Optional[str]:
    """Resolve the main repo root that owns .kilo/agent-manager.json for `cwd`.
    Uses git's own common-dir concept so this works uniformly whether `cwd`
    is the main repo or one of its linked worktrees — the Agent Manager
    state file always lives in the main repo root, never inside a worktree."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    git_common_dir = result.stdout.strip()
    if not git_common_dir:
        return None
    return os.path.dirname(git_common_dir)


def _current_branch(cwd: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _iso_now_z() -> str:
    """ISO-8601 with millisecond precision and a literal 'Z' suffix, matching
    JavaScript's `new Date().toISOString()` — the format already stored by
    the extension for every existing entry in agent-manager.json."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _agent_manager_file(root: str) -> str:
    return os.path.join(root, ".kilo", "agent-manager.json")


def _agent_manager_read(root: str) -> dict:
    try:
        with open(_agent_manager_file(root), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    data.setdefault("worktrees", {})
    data.setdefault("sessions", {})
    return data


def _agent_manager_write(root: str, data: dict) -> None:
    """Atomic write: same tmp-file-then-rename scheme the extension itself
    uses, so a concurrent reader (the extension, or us) never observes a
    partially-written file."""
    kilo_dir = os.path.join(root, ".kilo")
    os.makedirs(kilo_dir, exist_ok=True)
    file_path = _agent_manager_file(root)
    tmp_path = f"{file_path}.{os.getpid()}.{int(time.time() * 1000)}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, file_path)


def _agent_manager_locked_update(root: str, mutate) -> None:
    """Read-modify-write agent-manager.json under an flock. Only serializes
    against OTHER kilo-mcp-server writers on this machine — see the module
    note above on why the VS Code extension's own overwrite-on-save can
    still clobber this regardless of the lock."""
    kilo_dir = os.path.join(root, ".kilo")
    os.makedirs(kilo_dir, exist_ok=True)
    lock_path = os.path.join(kilo_dir, ".agent-manager.lock")
    with open(lock_path, "w") as lock_f:
        try:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            data = _agent_manager_read(root)
            mutate(data)
            _agent_manager_write(root, data)
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def _resolve_parent_remote(cwd: str, parent_branch: str) -> Optional[str]:
    """'origin' if `origin/<parent_branch>` exists as a remote-tracking ref, else
    None. Mirrors the extension's own resolveRemote()+refExistsLocally() check
    (WorktreeManager.ts) — Worktree.remote is only meant to be set when the
    parent branch actually has a remote counterpart, since the UI uses it to
    diff against `${remote}/${parentBranch}`; setting it unconditionally would
    point that diff at a ref that may not exist."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", f"refs/remotes/origin/{parent_branch}"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
    except OSError:
        return None
    return "origin" if result.returncode == 0 else None


def _agent_manager_register_worktree(root: str, *, branch: str, path: str,
                                      parent_branch: str, label: Optional[str] = None,
                                      remote: Optional[str] = None) -> str:
    """Add a worktree entry to agent-manager.json and return its synthetic id
    (used to link sessions to it via ManagedSession.worktreeId)."""
    wt_id = f"wt-mcp-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"

    def mutate(data: dict) -> None:
        entry = {
            "branch": branch,
            "path": path,
            "parentBranch": parent_branch,
            "createdAt": _iso_now_z(),
            "branchOwned": True,
        }
        if label:
            entry["label"] = label
        if remote:
            entry["remote"] = remote
        data["worktrees"][wt_id] = entry
        order = data.setdefault("worktreeOrder", [])
        if wt_id not in order:
            order.append(wt_id)

    _agent_manager_locked_update(root, mutate)
    return wt_id


def _agent_manager_register_session(root: str, session_id: str, worktree_id: Optional[str],
                                     cwd: Optional[str] = None) -> None:
    """Add a session entry (worktreeId=None means a "local", non-worktree
    session, matching the extension's own convention).

    `worktree_id` (from the launching call's own record) is only a hint —
    it's set only when THAT SAME kilo_implement call also created the
    worktree via isolation='worktree'. If a task instead reuses an existing
    worktree's path as `working_directory` (no isolation on that particular
    call), `worktree_id` is empty even though the session plainly belongs to
    a registered worktree. So this always falls back to matching `cwd`
    against every registered worktree's `path` (mirrors the extension's own
    `findWorktreeByPath`) — the robust, launch-mechanism-independent source
    of truth. An existing session with `worktreeId: null` is upgradeable by
    a later, better-informed call (e.g. once `cwd` is known); a session
    already linked to a real worktree is never touched."""
    def mutate(data: dict) -> None:
        existing = data["sessions"].get(session_id)
        if existing and existing.get("worktreeId"):
            return  # already correctly linked — never overwrite
        linked = worktree_id if worktree_id and worktree_id in data["worktrees"] else None
        if not linked and cwd:
            target = os.path.realpath(cwd)
            for wt_id, wt in data["worktrees"].items():
                if os.path.realpath(wt.get("path", "")) == target:
                    linked = wt_id
                    break
        data["sessions"][session_id] = {
            "worktreeId": linked,
            "createdAt": existing["createdAt"] if existing else _iso_now_z(),
        }

    _agent_manager_locked_update(root, mutate)


def _agent_manager_note_session(cwd: Optional[str], session_id: Optional[str],
                                 worktree_id: Optional[str]) -> None:
    """Best-effort hook: register a newly-discovered Kilo session (and the
    worktree it belongs to, if any) into .kilo/agent-manager.json. Never
    raises — this is bookkeeping for the Agent Manager UI, not correctness;
    a failure here must never break task tracking."""
    if not cwd or not session_id:
        return
    try:
        root = _agent_manager_root(cwd)
        if root:
            _agent_manager_register_session(root, session_id, worktree_id, cwd=cwd)
    except OSError:
        logger.warning("Agent Manager session registration failed (cwd=%s, session=%s) — "
                        "session won't show up in the Agent Manager UI", cwd, session_id, exc_info=True)


_WORKTREE_LOCKS: dict[str, asyncio.Lock] = {}


def _worktree_lock_for(cwd: str) -> asyncio.Lock:
    key = os.path.realpath(cwd)
    lock = _WORKTREE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _WORKTREE_LOCKS[key] = lock
    return lock


async def _create_worktree(cwd: str, branch_name: str, base_branch: Optional[str],
                            env: dict) -> tuple[Optional[str], int, str, str, Optional[str]]:
    """Shared core of kilo_create_worktree and kilo_implement's isolation='worktree'
    path: `git worktree add -b <branch_name> .kilo/worktrees/<branch_name>` under
    `cwd`. Matches the Kilo VS Code extension's own WorktreeManager convention
    (`KILO_DIR/worktrees`, not a top-level `.kilo-worktrees` — that older path
    isn't covered by the extension's `ensureGitExclude()` patterns, so a
    worktree created there shows up as untracked content to git and isn't
    found by the extension's own `discoverWorktrees()` recovery scan).
    Returns (worktree_abspath_or_None_on_failure, returncode, stdout, stderr,
    agent_manager_worktree_id_or_None)."""
    worktree_rel = os.path.join(".kilo", "worktrees", branch_name)
    cmd = ["git", "worktree", "add", "-b", branch_name, worktree_rel]
    if base_branch:
        cmd.append(base_branch)
    async with _worktree_lock_for(cwd):
        returncode, stdout, stderr = await _run_kilo(cmd, cwd=cwd, env=env)
    worktree_path = os.path.join(cwd, worktree_rel) if returncode == 0 else None

    agent_manager_worktree_id = None
    if worktree_path:
        root = _agent_manager_root(cwd)
        if root:
            parent_branch = base_branch or _current_branch(cwd) or "main"
            try:
                agent_manager_worktree_id = _agent_manager_register_worktree(
                    root, branch=branch_name, path=worktree_path,
                    parent_branch=parent_branch,
                    remote=_resolve_parent_remote(cwd, parent_branch),
                )
            except OSError:
                logger.warning("Agent Manager worktree registration failed (root=%s, branch=%s) — "
                                "worktree created fine, but won't show up in the Agent Manager UI",
                                root, branch_name, exc_info=True)

    return worktree_path, returncode, stdout, stderr, agent_manager_worktree_id


@mcp.tool()
async def kilo_create_worktree(
    branch_name: Annotated[str, Field(description="Name of the new branch and worktree directory")],
    base_branch: Annotated[Optional[str], Field(description="Optional base branch (default: current branch)")] = None,
    working_directory: Annotated[Optional[str], Field(description="Base repository directory")] = None,
) -> str:
    """Create an isolated git worktree under .kilo/worktrees/<branch_name> (runs
    git directly, not Kilo). Each worktree gets its own new branch, so parallel
    kilo_implement runs on independent components never collide on files.

    Prefer passing isolation='worktree' directly to kilo_implement instead of
    calling this separately — one step instead of two, and harder to forget.
    Use this standalone tool when you need the worktree before deciding what
    to delegate into it, or want a worktree kilo_implement doesn't manage.

    Fails if the branch already exists — pick a fresh branch name per task.

    Also registers the new worktree in .kilo/agent-manager.json (best-effort)
    so it can appear in the Kilo Agent Manager UI. There is no live sync with
    a running VS Code extension window — see kilo_implement's isolation
    parameter description for the caveat."""
    cwd = working_directory if working_directory else os.getcwd()
    _, returncode, stdout, stderr, agent_manager_worktree_id = await _create_worktree(
        cwd, branch_name, base_branch, os.environ.copy()
    )
    registration_note = (
        f"\nRegistered in Agent Manager as {agent_manager_worktree_id}."
        if agent_manager_worktree_id else ""
    )
    return (
        f"Worktree creation finished.\nExit code: {returncode}\nSTDOUT: {stdout}\n"
        f"STDERR: {stderr}{registration_note}"
    )


@mcp.tool()
async def kilo_auth_status() -> str:
    """Check the authentication status of Kilo providers by running 'kilo auth list'."""
    returncode, stdout, stderr = await _run_kilo(["kilo", "auth", "list"], cwd=os.getcwd(), env=os.environ.copy())
    return f"Auth Status (exit code {returncode}):\n{stdout}\n{stderr}"


@mcp.tool()
async def kilo_session_revert(
    session_id: Annotated[str, Field(description="The session_id of the Kilo session to revert")],
    message_id: Annotated[Optional[str], Field(description="Optional target message_id to revert back to")] = None,
) -> str:
    """Revert a Kilo session back to a previous message/checkpoint.
    Allows the orchestrator to undo changes or steps if Kilo took a wrong path."""
    ok, _ = await _try_all_kilo_servers(_kilo_server_revert_session, session_id, message_id)
    if ok:
        return f"Successfully reverted session '{session_id}'" + (f" to message '{message_id}'" if message_id else "") + "."
    return (f"Failed to revert session '{session_id}' via Kilo Server API "
            f"(tried {len(_list_active_kilo_servers())} active instance(s)).")


@mcp.tool()
async def kilo_session_fork(
    session_id: Annotated[str, Field(description="The session_id of the Kilo session to fork")],
    message_id: Annotated[Optional[str], Field(description="Optional message_id to branch/fork from")] = None,
) -> str:
    """Fork an existing Kilo session at a specific checkpoint to explore an alternative implementation path in parallel."""
    new_sid, _ = await _try_all_kilo_servers(_kilo_server_fork_session, session_id, message_id)
    if new_sid:
        return f"Successfully forked session '{session_id}'. New forked session_id: {new_sid}"
    return (f"Failed to fork session '{session_id}' via Kilo Server API "
            f"(tried {len(_list_active_kilo_servers())} active instance(s)).")


@mcp.tool()
async def kilo_respond_question(
    request_id: Annotated[str, Field(description="The question requestID prompted by Kilo")],
    answers: Annotated[list[str], Field(description="List of selected answer labels or text responses")],
) -> str:
    """Respond to an interactive question asked by Kilo during task execution, unblocking the session."""
    ok, _ = await _try_all_kilo_servers(_kilo_server_respond_question, request_id, answers)
    if ok:
        return f"Successfully submitted answer(s) for question request '{request_id}'."
    return (f"Failed to respond to question '{request_id}' via Kilo Server API "
            f"(tried {len(_list_active_kilo_servers())} active instance(s)).")


@mcp.tool()
async def kilo_get_session_todo(
    session_id: Annotated[str, Field(description="The session_id to read todos/plan for")],
) -> str:
    """Read the live plan and checklist (todo list) maintained by Kilo for a specific session."""
    todos = _read_todos(session_id)
    if not todos:
        return f"No todo items recorded for session '{session_id}'."
    mark = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}
    lines = [f"# Todo list for session {session_id}"]
    for t in todos:
        lines.append(f"- {mark.get(t['status'], '[?]')} ({t['priority']}) {t['content']}")
    return "\n".join(lines)


@mcp.tool()
async def kilo_update_session_todo(
    session_id: Annotated[str, Field(description="The session_id to update todos for")],
    todos: Annotated[
        list[dict],
        Field(
            description="List of todo items to set or update. Each item should have 'content' (str), "
            "optional 'status' ('pending'|'in_progress'|'completed'), and optional 'priority' ('high'|'medium'|'low')."
        ),
    ],
) -> str:
    """Create or update the checklist/todo list for a Kilo session.
    Allows an orchestrator architect to inject a structured plan or adjust steps mid-task."""
    # Attempt REST API todo update against whichever live instance owns this session
    ok_rest, _ = await _try_all_kilo_servers(_kilo_server_update_todo, session_id, todos)
    if ok_rest:
        return f"Successfully updated {len(todos)} todo item(s) for session '{session_id}' via Server API."

    # Fallback: direct SQLite write
    ok = _write_db_todos(session_id, todos)
    if ok:
        return f"Successfully updated {len(todos)} todo item(s) for session '{session_id}'."
    return f"Failed to update todos for session '{session_id}' in DB."


@mcp.tool()
async def kilo_run_command(
    command_name: Annotated[str, Field(description="Name of the custom Kilo command to run (from .kilo/command/ or ~/.kilo/command/)")],
    args: Annotated[Optional[str], Field(description="Optional arguments passed to the command as its message")] = None,
    working_directory: Annotated[Optional[str], Field(description="Absolute path to run the command in")] = None,
) -> str:
    """Execute a deterministic, pre-programmed Kilo custom command (e.g., db-migrate)
    via `kilo run --command <name>`. Discover available names with kilo_list_skills'
    sibling directories or the .kilo/command/ folder."""
    cwd = working_directory if working_directory else os.getcwd()
    cmd = ["kilo", "run", "--command", command_name]
    if args:
        cmd.append(args)
    returncode, stdout, stderr = await _run_kilo(cmd, cwd=cwd, env=os.environ.copy())
    return f"Command execution:\nExit code: {returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"


@mcp.tool()
async def kilo_workspace_status(
    working_directory: Annotated[Optional[str], Field(description="Absolute path to the workspace")] = None,
) -> str:
    """Report the git status (porcelain short format) of a working directory or
    worktree — use it to review what a delegated run actually touched."""
    cwd = working_directory if working_directory else os.getcwd()
    returncode, stdout, stderr = await _run_kilo(["git", "status", "-s"], cwd=cwd, env=os.environ.copy())
    if returncode != 0:
        return f"git status failed (exit code {returncode}):\n{stderr}"
    return f"Workspace status:\n{stdout or '(clean — no uncommitted changes)'}"


# --- kilo_task_status helpers (pure logic kept separate for testability) ----

KILO_SESSION_DB = os.path.expanduser("~/.local/share/kilo/kilo.db")


def _parse_ps_time(t: str) -> float:
    """Parse ps etime/time format ([[dd-]hh:]mm:ss[.ff]) into seconds."""
    t = t.strip()
    if not t:
        return 0.0
    days = 0
    if "-" in t:
        d, t = t.split("-", 1)
        days = int(d)
    parts = [float(p) for p in t.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def _assess_kilo_task(elapsed_s: float, cpu_s: float, has_network: bool,
                      session_age_s: Optional[float]) -> str:
    """Heuristic verdict for one running `kilo run` process.

    A healthy run creates its session in kilo.db within seconds and keeps
    network connections open while the model works; a hung run shows neither.
    """
    if session_age_s is not None and session_age_s < 120:
        return f"WORKING — session updated {int(session_age_s)}s ago"
    if has_network:
        return "WORKING — connected to the model API (long model call in progress)"
    if elapsed_s < 120:
        return "STARTING — process younger than 2 minutes, judge later"
    return (
        f"LIKELY STUCK — running for {int(elapsed_s // 60)}m with only "
        f"{int(cpu_s)}s CPU, no network connections and no session activity. "
        "Consider killing the PID; the calling session will receive the exit."
    )


def _recent_kilo_sessions(limit: int = 15) -> list[tuple]:
    """(worktree, title, time_updated_ms) of the most recent Kilo sessions,
    read-only so we never lock the db Kilo is writing to."""
    try:
        con = sqlite3.connect(f"file:{KILO_SESSION_DB}?mode=ro", uri=True, timeout=1)
        rows = con.execute(
            "SELECT p.worktree, s.title, s.time_updated FROM session s "
            "JOIN project p ON s.project_id = p.id "
            "ORDER BY s.time_updated DESC LIMIT ?", (limit,)
        ).fetchall()
        con.close()
        return rows
    except Exception:
        return []


def _find_session_for_task(cwd: str, started_iso: str) -> Optional[str]:
    """Best-effort match: the earliest Kilo session created in `cwd` (or its
    main repo root — see below) at or after a task's start time. Kilo creates
    the session row within ~1s of `kilo run` starting, well before any
    assistant output exists, so this is reliable even for a task that has
    barely begun. A 15s buffer absorbs clock skew between this server's wall
    clock and Kilo's own timestamps.

    Kilo records a session's `project.worktree` as the MAIN repository root,
    NOT the specific git worktree subdirectory `kilo run` was actually
    launched in — confirmed by querying kilo.db directly: every session
    launched via `kilo_implement(isolation='worktree')` recorded its parent
    repo's path, never the `.kilo/worktrees/<branch>` path passed as `cwd`.
    Matching only against the literal `cwd` therefore NEVER resolved for any
    isolated-worktree task (session_id stayed 'unknown' in every such task
    tested live) — this falls back to the main repo root (via
    `_agent_manager_root`'s git-common-dir resolution) to fix the common
    case. It's fuzzier under heavy parallelism: multiple concurrent
    isolation='worktree' tasks against the SAME repo all normalize to the
    same DB row's `worktree` value, so disambiguating between them degrades
    to time-window ordering alone — still a large improvement over never
    resolving at all."""
    try:
        started_ms = datetime.fromisoformat(started_iso).timestamp() * 1000
    except (ValueError, TypeError):
        return None
    targets = {os.path.realpath(cwd)}
    root = _agent_manager_root(cwd)
    if root:
        targets.add(os.path.realpath(root))
    try:
        con = sqlite3.connect(f"file:{KILO_SESSION_DB}?mode=ro", uri=True, timeout=1)
        rows = con.execute(
            "SELECT s.id, p.worktree FROM session s "
            "JOIN project p ON s.project_id = p.id "
            "WHERE s.time_created >= ? ORDER BY s.time_created ASC LIMIT 50",
            (started_ms - 15000,),
        ).fetchall()
        con.close()
    except Exception:
        return None
    for sid, worktree in rows:
        if worktree and os.path.realpath(worktree) in targets:
            return sid
    return None


def _read_todos(session_id: str) -> list[dict]:
    """Kilo's own live plan for a session: each step's content/status/priority,
    in execution order. This is Kilo's actual todo list, not a heuristic."""
    try:
        con = sqlite3.connect(f"file:{KILO_SESSION_DB}?mode=ro", uri=True, timeout=1)
        rows = con.execute(
            "SELECT content, status, priority FROM todo WHERE session_id = ? "
            "ORDER BY position ASC",
            (session_id,),
        ).fetchall()
        con.close()
    except Exception:
        return []
    return [{"content": c, "status": s, "priority": p} for c, s, p in rows]


def _read_recent_texts(session_id: str, limit: int = 3) -> list[str]:
    """The last few assistant text parts for a session, oldest first — a tail
    of Kilo's own running commentary, read straight from its session DB."""
    try:
        con = sqlite3.connect(f"file:{KILO_SESSION_DB}?mode=ro", uri=True, timeout=1)
        rows = con.execute(
            "SELECT data FROM part WHERE session_id = ? "
            "ORDER BY time_created DESC LIMIT 30",
            (session_id,),
        ).fetchall()
        con.close()
    except Exception:
        return []
    texts = []
    for (data,) in rows:
        try:
            obj = json.loads(data)
        except ValueError:
            continue
        if obj.get("type") == "text" and obj.get("text"):
            texts.append(obj["text"])
        if len(texts) >= limit:
            break
    texts.reverse()
    return texts


def _read_session_summary(session_id: str) -> Optional[dict]:
    """Title, running cost/tokens, and last-update time for a session."""
    try:
        con = sqlite3.connect(f"file:{KILO_SESSION_DB}?mode=ro", uri=True, timeout=1)
        row = con.execute(
            "SELECT title, cost, tokens_input, tokens_output, time_updated "
            "FROM session WHERE id = ?",
            (session_id,),
        ).fetchone()
        con.close()
    except Exception:
        return None
    if not row:
        return None
    title, cost, tokens_in, tokens_out, time_updated = row
    return {
        "title": title,
        "cost": cost,
        "tokens_input": tokens_in,
        "tokens_output": tokens_out,
        "time_updated": time_updated,
    }


@mcp.tool()
async def kilo_task_status(
    working_directory: Annotated[
        Optional[str],
        Field(description="Only report tasks running in this workspace. "
              "Leave empty to report every running `kilo run` process."),
    ] = None,
) -> str:
    """Diagnose the state of running `kilo run` tasks (e.g. delegations
    launched via kilo_implement, from this or any other session, including
    ones with no known task_id — e.g. legacy foreground runs, or runs started
    outside this MCP server).

    This is the coarse, OS-level check: elapsed vs CPU time, network activity
    (is it talking to the model?), the matching Kilo session's last update,
    and a verdict: WORKING / STARTING / LIKELY STUCK. For a task_id you
    launched yourself via kilo_implement, prefer kilo_task_progress instead —
    it reads Kilo's actual plan/todo list and commentary, not just a heuristic,
    and kilo_task_cancel can then stop it precisely by its recorded PID.
    """
    env = os.environ.copy()
    here = os.getcwd()
    rc, out, _ = await _run_kilo(["pgrep", "-fl", "kilo run"], cwd=here, env=env)
    procs = []
    for line in (out or "").splitlines():
        pid, _, cmdline = line.strip().partition(" ")
        if not pid.isdigit() or "kilo run" not in cmdline:
            continue
        procs.append((pid, cmdline))
    if not procs:
        recent = _recent_kilo_sessions(3)
        lines = ["No `kilo run` process is currently running (all tasks finished or none started)."]
        if recent:
            lines.append("\nMost recent Kilo sessions (finished work):")
            for wt, title, ts in recent:
                age = int(time.time() - ts / 1000)
                lines.append(f"- [{age}s ago] {title} ({wt})")
        return "\n".join(lines)

    sessions = _recent_kilo_sessions()
    report = []
    for pid, cmdline in procs:
        _, ps_out, _ = await _run_kilo(["ps", "-p", pid, "-o", "etime=,time="], cwd=here, env=env)
        fields = ps_out.split()
        elapsed = _parse_ps_time(fields[0]) if len(fields) > 0 else 0.0
        cpu = _parse_ps_time(fields[1]) if len(fields) > 1 else 0.0
        _, cwd_out, _ = await _run_kilo(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], cwd=here, env=env)
        proc_cwd = next((l[1:] for l in cwd_out.splitlines() if l.startswith("n")), "?")
        if working_directory and os.path.realpath(proc_cwd) != os.path.realpath(working_directory):
            continue
        net_rc, net_out, _ = await _run_kilo(["lsof", "-a", "-p", pid, "-i"], cwd=here, env=env)
        has_network = bool(net_out.strip())
        session_age = None
        for wt, _title, ts in sessions:
            if wt and os.path.realpath(wt) == os.path.realpath(proc_cwd):
                age = time.time() - ts / 1000
                if age < elapsed:  # session belongs to this run, not an old one
                    session_age = age
                break
        verdict = _assess_kilo_task(elapsed, cpu, has_network, session_age)
        report.append(
            f"## PID {pid} — {proc_cwd}\n"
            f"- elapsed: {int(elapsed)}s | cpu: {int(cpu)}s | network: {'yes' if has_network else 'no'}"
            f" | session activity: {f'{int(session_age)}s ago' if session_age is not None else 'none for this run'}\n"
            f"- verdict: **{verdict}**\n"
            f"- command: {cmdline[:160]}"
        )
    if not report:
        return f"No `kilo run` process found for workspace {working_directory}."
    return "# Running Kilo tasks\n\n" + "\n\n".join(report)


# Helper: unexport write/execution tools when running in --rag-only mode
if _IS_RAG_ONLY:
    _WRITE_TOOLS = {
        "kilo_implement", "kilo_task_result", "kilo_task_progress", "kilo_task_cancel",
        "kilo_log_issue", "kilo_metrics", "kilo_create_worktree", "kilo_session_revert",
        "kilo_session_fork", "kilo_respond_question", "kilo_get_session_todo",
        "kilo_update_session_todo", "kilo_run_command", "kilo_workspace_status", "kilo_task_status"
    }
    mcp._tool_manager._tools = {
        k: v for k, v in mcp._tool_manager._tools.items()
        if k not in _WRITE_TOOLS
    }


def install_skills():
    """Copy the bundled skills/ directories into the global Kilo skills dir.

    Kilo discovers global skills in ~/.kilo/skills and ~/.kilocode/skills
    (NOT in ~/.config/kilo, which only holds kilo.jsonc and credentials)."""
    print("Installing Kilo skills...")
    dest = os.path.expanduser("~/.kilo/skills")
    os.makedirs(dest, exist_ok=True)

    repo_root = os.path.dirname(os.path.abspath(__file__))
    for plugin_dir in ("architect-side", "executor-side"):
        src_skills = os.path.join(repo_root, "plugins", plugin_dir, "skills")
        if not os.path.exists(src_skills):
            continue
        for skill_dir in os.listdir(src_skills):
            full_src = os.path.join(src_skills, skill_dir)
            if os.path.isdir(full_src) and os.path.exists(os.path.join(full_src, "SKILL.md")):
                # copy the whole skill dir: SKILL.md plus any scripts/assets
                shutil.copytree(full_src, os.path.join(dest, skill_dir), dirs_exist_ok=True)
                print(f"Installed skill: {skill_dir}")
    print("Installation complete.")


def configure_models_interactive():
    """Interactively set the `default_model` (simple/routine tasks) and
    `complex_model` (complex/high-risk tasks) config keys.

    Lists whatever `kilo models` reports for THIS environment (kilo-mcp
    itself never hardcodes a provider) and persists the two answers to the
    resolved config file, so the connected assistant reads them straight out
    of kilo_implement's tool description instead of guessing a model id and
    discovering it doesn't exist."""
    try:
        res = subprocess.run(["kilo", "models"], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        print("Error: the 'kilo' executable was not found in the PATH.")
        return
    except Exception as e:
        print(f"Error listing Kilo models: {e}")
        return

    models = sorted(set(ln.strip() for ln in res.stdout.splitlines() if "/" in ln))
    if not models:
        print("No models reported by `kilo models` — is a provider authenticated? "
              "(`kilo auth login`)")
        return

    print(f"Available Kilo models ({len(models)}):")
    for m in models:
        print(f"  - {m}")
    print()

    def ask(label: str, current: str) -> str:
        while True:
            raw = input(f"{label} [current: {current}]: ").strip()
            if not raw:
                return current
            if raw in models:
                return raw
            confirm = input(f"'{raw}' is not in the list above — use it anyway? [y/N]: ").strip().lower()
            if confirm == "y":
                return raw
            print("Let's try again.")

    simple = ask("Model for SIMPLE/routine tasks (mechanical edits, low risk)", DEFAULT_MODEL)
    complex_ = ask("Model for COMPLEX/high-risk tasks (subtle bugs, security-sensitive, "
                   "multi-step reasoning)", COMPLEX_MODEL)

    target = os.environ.get("KILO_MCP_CONFIG") or _CONFIG.get("_source") or _config_file_candidates()[-1]
    _upsert_toml_string_key(target, "kilo", "default_model", simple)
    _upsert_toml_string_key(target, "kilo", "complex_model", complex_)
    print(f"\nSaved to {target}:")
    print(f"  default_model = \"{simple}\"")
    print(f"  complex_model = \"{complex_}\"")
    print("\nRestart the MCP server (or the client that spawns it) for the new tiers to take effect.")


def main() -> None:
    """Console-script entry point."""
    if "--install-skills" in sys.argv:
        install_skills()
        return
    if "--configure-models" in sys.argv:
        configure_models_interactive()
        return
    mcp.run()


if __name__ == "__main__":
    main()
