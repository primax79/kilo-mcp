# Specifiche Architetturali: Orchestrazione Agenti e Worktree via MCP Server

## 0. Stato di implementazione (aggiornato 2026-07-29)

Questo documento proponeva un toolset `kilo_orchestrator_*` non ancora
implementato quando è stato scritto. Da allora `~/devel/kilo-mcp-server`
(`server.py`) ha coperto buona parte di quel toolset sotto nomi diversi, più
tre bug di affidabilità dell'esecuzione in background sono stati corretti e
verificati dal vivo — mappa aggiornata per non ripartire da zero:

| Proposto qui                        | Stato                                                                                                                                                                                                                                                                                                                              |
| ----------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `kilo_orchestrator_create_worktree` | ✅ Implementato — `kilo_create_worktree` / `kilo_implement(isolation='worktree')`, con lock interno anti-race su `.git/index.lock`. In più (non proposto qui): registra il worktree in `.kilo/agent-manager.json` automaticamente.                                                                                                 |
| `kilo_orchestrator_remove_worktree` | ❌ Non implementato — nessun tool rimuove un worktree; resta `git worktree remove` manuale (documentato in `KILO_CLI_WORKTREE_GUIDE.md` §2.5).                                                                                                                                                                                     |
| `kilo_orchestrator_spawn_agent`     | ✅ Implementato — `kilo_implement(background=true)`, il default. Dal 2026-07-29 gira come processo OS realmente detached (`setsid`), non più legato al ciclo di vita del server MCP (era un bug reale: task bloccati su "running" per sempre — 3 cause distinte, tutte corrette e verificate dal vivo, vedi `ARCHITECTURE.md` §8). |
| `kilo_orchestrator_list_sessions`   | ⚠️ Parziale — `kilo_task_status` (euristica OS-level, tutti i processi `kilo run`) e `kilo_task_progress` (piano/commentario live per un `task_id` noto) coprono la maggior parte del caso d'uso; manca un singolo tool che elenchi *tutte* le sessioni con `git diff --stat` allegato in una chiamata.                            |
| `kilo_orchestrator_prompt_agent`    | ✅ Implementato — `kilo_implement(continue_session_id=...)`, verificato contro la CLI reale (Kilo accoda il follow-up anche a sessione ancora in esecuzione).                                                                                                                                                                      |
| `kilo_orchestrator_get_logs`        | ⚠️ Parziale — `kilo_task_progress` restituisce una coda del commentario recente da `kilo.db`; il log grezzo completo esiste su disco (`TASKS_DIR/<task_id>.log`, introdotto con il fix del 2026-07-29) ma non è ancora esposto da un tool dedicato.                                                                                |
| `kilo_orchestrator_merge_task`      | ❌ Non implementato, deliberatamente — il merge resta un passo manuale con verifica esplicita (build/test reali, non solo il report di Kilo) prima di mergiare, per la disciplina descritta in `KILO_CLI_WORKTREE_GUIDE.md` §2.4/§2.5. Automatizzarlo rischierebbe di saltare proprio quella verifica.                             |

Non proposta qui, aggiunta il 2026-07-29: registrazione automatica di
worktree/sessioni in `.kilo/agent-manager.json` (visibilità nell'Agent
Manager UI di VS Code, best-effort — vedi `README.md` §Agent Manager
Integration e `ARCHITECTURE.md` §9). Copre parzialmente lo scopo di
"orchestrazione visibile" che questo documento poneva come motivazione
originaria (§1), senza bisogno del toolset `kilo_orchestrator_*` proposto
sotto — quel toolset resta un'opzione se in futuro servisse un'API di
orchestrazione esplicita invece della sola visibilità cosmetica.

---

## 1. Context & Purpose

L'obiettivo di questa specifica è definire l'architettura per un **MCP Server** in grado di gestire e orchestrare sessioni concorrenti di **Kilo CLI** basate su **Git Worktree**.
Attualmente, l'orchestrazione multi-task/multi-worktree è delegata all'estensione VS Code di Kilo (tramite l'interfaccia Agent Manager). Spostare o replicare questa logica all'interno di un MCP Server personalizzato permette di rendere l'orchestrazione **head-less**, indipendente dall'IDE e integrabile con strumenti esterni (es. CLI, agenti superiori, CI/CD, sistemi di ticketing).

---

## 2. Stato Attuale e Primitive di Riferimento (Agent Manager)

Nel contesto VS Code, l'orchestrazione si basa sulle seguenti primitive:

- **`mode: "worktree"`**: Creazione di un git worktree isolato e avvio di una sessione dedicata.
- **`action: "list"`**: Ispezione asincrona delle sessioni (stato `idle`, `busy`, `waiting`, diff git, branch).
- **`action: "prompt"`**: Invio asincrono di ulteriori istruzioni a una specifica sessione in esecuzione via `sessionID`.
- **`action: "stop"`**: Interruzione e pulizia della sessione/worktree.

---

## 3. Architettura dell'MCP Server per l'Orchestrazione

L'MCP Server esporrà una suite di **Tool MCP** per permettere a un Agente Orchestratore (o a client esterni) di gestire l'intero ciclo di vita dei sub-agenti Kilo.

### 3.1. Toolset Proposto per l'MCP Server

#### A. Workspace & Worktree Management

1. **`kilo_orchestrator_create_worktree`**
   - **Input:** `task_id` (string), `branch_name` (string), `base_branch` (optional, default: current/main).
   - **Azione:**
     - Esegue `git worktree add .kilo/worktrees/<branch_name> -b <branch_name>`.
     - Inizializza l'ambiente invocando lo script di setup se presente (es. `.kilo/setup-script.sh`).
   - **Output:** Path assoluto del worktree creato.

2. **`kilo_orchestrator_remove_worktree`**
   - **Input:** `branch_name` (string), `force` (boolean).
   - **Azione:** Pulizia del worktree via `git worktree remove` ed eventuale eliminazione branch se unito.

#### B. Process & Agent Supervision

1. **`kilo_orchestrator_spawn_agent`**
   - **Input:**
     - `worktree_path` (string)
     - `prompt` (string): Istruzione/task da eseguire.
     - `model` (optional string): Override del modello da usare per la sotto-sessione.
     - `auto_verify_command` (optional string): Comando di test/validazione (es. `npm test`, `pytest`).
   - **Azione:**
     - Spawna un processo figli di Kilo CLI in modalità headless all'interno della cartella `worktree_path`.
     - Registra il processo in una mappa interna di stato (PID, `session_id`, log stream, status).
   - **Output:** `session_id` univoco per il monitoraggio.

2. **`kilo_orchestrator_list_sessions`**
   - **Input:** Nessuno (o filtri per stato).
   - **Azione:** Restituisce la lista aggiornata delle sessioni attive/completate con:
     - `session_id`
     - `branch` / `worktree_path`
     - `status` (`running`, `idle`, `completed`, `failed`)
     - Modifiche Git pendenti (riferimento a `git status` / `git diff --stat`)
     - Ultimo output dai log.

3. **`kilo_orchestrator_prompt_agent`**
   - **Input:** `session_id` (string), `prompt` (string).
   - **Azione:** Invia un nuovo messaggio/istruzione alla sessione in esecuzione (tramite stdin/IPC o rilancio controllato della CLI sul worktree).

4. **`kilo_orchestrator_get_logs`**
   - **Input:** `session_id` (string), `lines` (optional int).
   - **Azione:** Restituisce la coda dell'output (stdout/stderr) dell'agente.

#### C. Integration & Merge

1. **`kilo_orchestrator_merge_task`**
   - **Input:** `branch_name` (string), `target_branch` (optional string).
   - **Azione:**
     - Esegue i check di validazione/test.
     - Effettua il merge del branch del worktree sul branch di destinazione.
     - Rimuove il worktree.

---

## 4. Flusso Operativo dell'Orchestratore (Self-Verification Loop)

```text
[Client / External Trigger / Master Agent]
              │
              ├─► Call: kilo_orchestrator_create_worktree
              │
              ├─► Call: kilo_orchestrator_spawn_agent (con prompt + comando di test)
              │
              ├─► Polling via kilo_orchestrator_list_sessions / get_logs
              │
              ├─► [Se falliscono i test] ──► Call: kilo_orchestrator_prompt_agent (con stacktrace errore)
              │
              └─► [Se completato con successo] ──► Call: kilo_orchestrator_merge_task
```

---

## 5. Considerazioni Tecniche di Implementazione

1. **Stato e Persistenza:**
   - Mantenere uno stato leggero in memoria/file (es. `.kilo/mcp-orchestrator-state.json`) per tracciare la corrispondenza tra `session_id`, PID del processo Kilo CLI, log file path e git worktree.
2. **Concorrenza e Locking:**
   - Isolare ogni esecuzione nel rispettivo worktree evita conflitti sul filesystem.
   - Evitare l'uso di `git stash` condivisi tra worktree per prevenire race condition.
3. **Gestione Ambienti e Variabili:**
   - Passare al processo figlio Kilo le giuste variabili d'ambiente (`WORKTREE_PATH`, `REPO_PATH`) in modo conforme a quanto già strutturato nelle convenzioni di Kilo (`.kilo/setup-script`).
