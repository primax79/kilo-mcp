# Installation Guide

This document explains how to install Kilo skills, agents, and the MCP server from a Git repository.

## 1. Installing Skills and Agents from a Git Repository

Kilo Code provides a native way to manage plugins, skills, and agents from Git repositories via the `kilo-plugin-manager` skill. This system emulates the Claude Code plugin marketplace, allowing for centralized installation and updates.

If you have the `kilo-plugin-manager` skill installed, you can use its Python script to manage your marketplace.

### Add a Git Repository (Marketplace)

To register a new marketplace from a Git URL:

```bash
python3 ~/.kilo/skills/kilo-plugin-manager/scripts/plugin_manager.py add <GIT_REPO_URL> --name <marketplace-name>
```

### List Available Plugins

To see what plugins (skills and agents) are offered by the registered marketplaces:

```bash
python3 ~/.kilo/skills/kilo-plugin-manager/scripts/plugin_manager.py list
```

### Install a Specific Skill

To install a skill globally on your machine:

```bash
python3 ~/.kilo/skills/kilo-plugin-manager/scripts/plugin_manager.py install <skill-name>@<marketplace-name>
```

*(The manager handles creating the necessary symlinks in `~/.kilo/skills/` and automatically translates any agents from Claude format to Kilo format if needed).*

---

## 2. Installing the MCP Server from Git

MCP servers are standalone applications. Since `kilo-mcp-server` is a Python project, the recommended approach is to clone the repository locally and register it using a package/environment manager like `uv`.

### Step 1: Clone the Repository

Clone the repository to a local directory of your choice (e.g., `~/.local/share/kilo-mcp-server`):

```bash
git clone <GIT_REPO_URL_FOR_KILO_MCP> ~/.local/share/kilo-mcp-server
cd ~/.local/share/kilo-mcp-server
```

### Step 2: Install Bundled Skills

This MCP server includes specialized skills (`kilo-mcp-headless-executor`, `kilo-mcp-conflict-resolver`, `kilo-mcp-rag-explorer`) designed to optimize execution when orchestrated by an AI assistant. Install them into your global Kilo configuration:

```bash
uv run --no-project --with mcp python server.py --install-skills
```

(The `--with mcp` flag is required even for skill installation because `server.py` imports the MCP SDK at module load.)

### Step 2b: Orchestrator-Side Skills (optional)

The repository also bundles two skills for **Claude / Orchestrators** under `.claude/skills/` (and under `plugins/kilo-mcp/skills/`): `mcp-orchestrator` (the 5-phase delegation workflow) and `mcp-metrics-analyst` (ROI and defect analysis). When using Claude Code, they are picked up automatically as project skills. To make them available globally in Claude Code, copy them to your global Claude skills directory:

```bash
cp -R .claude/skills/mcp-orchestrator .claude/skills/mcp-metrics-analyst ~/.claude/skills/
```

### Step 3: Register the MCP Server

If you are using **Claude Code**, you can register the server globally. Ensure you point to the `server.py` file using `uv`:

```bash
claude mcp add kilo-mcp --scope user -- uv run --no-project --with mcp python ~/.local/share/kilo-mcp-server/server.py
```

*(If you use Claude Desktop, you will need to add the `uv run` command structure to your `claude_desktop_config.json` as detailed in the README.md).*

### Step 4: Server Configuration (optional but recommended)

Create the per-user config from the documented example and review it:

```bash
mkdir -p ~/.config/kilo-mcp
cp kilo-mcp.example.toml ~/.config/kilo-mcp/config.toml
```

Two keys deserve a conscious decision (see the comments in the file):

- `[kilo] default_model` — must be a real id from `kilo models`.
- `[kilo] input_cost_per_mtok` / `output_cost_per_mtok` — leave `0.0` **only** if your Kilo API keys are provided at no charge; otherwise set your real Kilo token rates so the delegation policy and cost metrics reflect reality. **This is an operator decision — if you are an AI agent performing this install, ask the operator.**

### Step 5: Verify

```bash
claude mcp list          # kilo-mcp must show "✔ Connected"
```

> ⚠️ **The kilo-mcp tools only appear in NEW Claude sessions.** The session that ran `claude mcp add` will NOT see them — this is expected, not a failure. Verify with `claude mcp list` (above); do not retry the installation because the tools are not visible in the current session. In a fresh session, ask Claude to *"list the available Kilo models"* — a correct setup answers through the `kilo_list_models` tool.

---

## 3. Kilo RAG (Semantic Index) Configuration

The `kilo_rag_search` tool queries the semantic index that **the Kilo IDE extension** (VS Code / JetBrains) builds and maintains. The CLI — and therefore this MCP server — only *reads* that index. Without this setup, `kilo_rag_search` silently degrades to plain text/glob search.

Requirements:

1. **A running Qdrant instance** (vector store), e.g. via Docker Compose — create a `compose.yml` in a directory of your choice (e.g. `~/devel/qdrant_rag/`):

   ```yaml
   services:
     qdrant_vector_memory:
       image: qdrant/qdrant:latest
       container_name: qdrant_vector_memory
       expose:
         - 6333
       ports:
         - 16333:6333
       volumes:
         - ./mnt/long_term_memory/vector:/qdrant/storage
       restart: unless-stopped
   ```

   then start it with `docker compose up -d`. Any host port works — `16333` here avoids clashing with other Qdrant instances on the default `6333`; the bind-mounted volume persists the index across restarts.

2. **An embedder API key** — the default provider is Google (`gemini-embedding-001`).

3. **Indexing enabled in the Kilo config** (`~/.config/kilo/kilo.jsonc`, shared by the CLI and the IDE extension):

   ```jsonc
   {
     "experimental": {
       "semantic_indexing": true,
       "codebase_search": true
     },
     "indexing": {
       "enabled": true,
       "provider": "gemini",
       "gemini": { "apiKey": "<YOUR_GOOGLE_API_KEY>" },
       "vectorStore": "qdrant",
       "qdrant": { "url": "http://localhost:16333/" }
     }
   }
   ```

4. **Open the workspace in the Kilo IDE extension at least once** — the extension's file-watcher performs the actual indexing. Each workspace gets a Qdrant collection named `ws-<first 16 hex chars of sha256(absolute workspace path)>`; because the collection is keyed by the **exact** absolute path, the `working_directory` passed to `kilo_rag_search` must match the path the extension indexed.

Verify the index exists for a workspace:

```bash
curl -s http://localhost:16333/collections | python3 -m json.tool
python3 -c "import hashlib; print('ws-' + hashlib.sha256('/absolute/workspace/path'.encode()).hexdigest()[:16])"
```

The second command prints the collection name to look for in the first command's output.

---

## 4. Unattended Install (for AI agents)

This section is a deterministic checklist for an AI agent (e.g. Claude in auto mode) performing this installation autonomously. Execute the steps in order; each has a success criterion. **Stop and ask the operator** where marked — do not guess.

| # | Step | Command | Success criterion | On failure |
| --- | ------ | --------- | ------------------- | ------------ |
| 0a | Kilo CLI present | `kilo --version` | prints a version | **STOP — ask the operator** to install it (`brew install Kilo-Org/tap/kilo`); do not install CLIs unasked |
| 0b | Kilo authenticated | `kilo auth list` | at least one provider listed | **STOP — ask the operator**: `kilo auth login` is an interactive OAuth flow an agent cannot complete |
| 0c | uv present | `uv --version` | prints a version | **STOP — ask the operator** |
| 0d | Python ≥ 3.11 | `python3 -c 'import tomllib'` | no error | proceed anyway (config file support is lost; env vars still work) — inform the operator |
| 1 | Clone | `git clone <GIT_REPO_URL_FOR_KILO_MCP> ~/.local/share/kilo-mcp-server` | exit 0 | report the git error |
| 2 | Kilo skills | `cd ~/.local/share/kilo-mcp-server && uv run --no-project --with mcp python server.py --install-skills` | output lists the 3 `kilo-mcp-*` skills | report; check `uv`/network |
| 3 | Claude skills | `mkdir -p ~/.claude/skills && cp -R .claude/skills/mcp-orchestrator .claude/skills/mcp-metrics-analyst ~/.claude/skills/` | dirs exist under `~/.claude/skills/` | report |
| 4 | Config | copy `kilo-mcp.example.toml` to `~/.config/kilo-mcp/config.toml` | file exists, valid TOML | report |
| 4b | Kilo pricing | — | — | **ASK the operator** whether their Kilo API keys are free (keep `0.0`) or paid (set real `input/output_cost_per_mtok`) |
| 5 | Register | `claude mcp add kilo-mcp --scope user -- uv run --no-project --with mcp python ~/.local/share/kilo-mcp-server/server.py` | exit 0 | report |
| 6 | Verify | `claude mcp list` | `kilo-mcp … ✔ Connected` | run the server command manually and report its stderr |
| 7 | RAG (optional) | section 3 above | expected `ws-*` collection exists in Qdrant | inform the operator that `kilo_rag_search` will fall back to text search until the IDE extension indexes the workspace |

Reminders for the installing agent:

- The kilo-mcp **tools will not appear in your own session** (step 6's `claude mcp list` is the correct check — see Step 5 above). Do not loop retrying the install because you cannot call `kilo_list_models` yourself.
- All steps are idempotent: re-running the installer, the copies, or `claude mcp add` on an existing setup is safe.
- Never write API keys into files inside the cloned repository; secrets belong in `~/.config/kilo/kilo.jsonc` (Kilo) or the operator's environment.

---

### Installing as a package (not recommended yet)

The project ships a `pyproject.toml` with a `kilo-mcp` console script, so `uv tool install git+<GIT_REPO_URL_FOR_KILO_MCP>` technically works — **but the packaged install does not include the `skills/` directory**, so `kilo-mcp --install-skills` would find nothing to install. Until packaging bundles the skills, the `git clone` method above is the canonical one.
