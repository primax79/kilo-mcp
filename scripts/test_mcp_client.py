#!/usr/bin/env python3
"""
test_mcp_client.py — Script client autonomo per testare kilo-mcp.

Esegue chiamate dirette al server kilo-mcp via stdio (protocollo JSON-RPC MCP),
simulando l'interazione da parte di un qualsiasi client/orchestratore MCP.
"""

import asyncio
import os
import re
import sys
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# Percorso di default al server.py nella radice di kilo-mcp
DEFAULT_SERVER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "server.py",
)


async def run_mcp_tests(server_script: str):
    print(f"🚀 Avvio del client di test MCP su: {server_script}\n")

    server_params = StdioServerParameters(
        command="uv",
        args=["run", "--no-project", "--with", "mcp>=2", "python", server_script],
        env=os.environ.copy(),
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            # 1. Inizializzazione della sessione MCP
            await session.initialize()
            print("✅ Connessione al server kilo-mcp stabilita con successo!")

            # 2. Elenco dei Tool disponibili
            tools_response = await session.list_tools()
            tool_names = [tool.name for tool in tools_response.tools]
            print(f"\n🛠️  Tool esposti dal server ({len(tool_names)}):")
            for name in tool_names:
                print(f"   - {name}")

            # 3. Test 1: Lista Agenti Kilo
            print("\n--------------------------------------------------")
            print("🧪 Test 1: Esecuzione di `kilo_list_agents`")
            res_agents = await session.call_tool("kilo_list_agents", {})
            print(f"Risultato:\n{res_agents.content[0].text[:300]}...")

            # 4. Test 2: Lista Modelli
            print("\n--------------------------------------------------")
            print("🧪 Test 2: Esecuzione di `kilo_list_models` (filter='gemini')")
            res_models = await session.call_tool("kilo_list_models", {"filter": "gemini"})
            print(f"Risultato:\n{res_models.content[0].text[:300]}...")

            # 5. Test 3: Creazione Worktree
            test_branch = "mcp-test-script-branch"
            print("\n--------------------------------------------------")
            print(f"🧪 Test 3: Esecuzione di `kilo_create_worktree` (branch={test_branch})")
            res_wt = await session.call_tool(
                "kilo_create_worktree",
                {"branch_name": test_branch},
            )
            print(f"Risultato:\n{res_wt.content[0].text}")

            # 6. Test 4: Avvio Task Background (kilo_implement)
            print("\n--------------------------------------------------")
            print("🧪 Test 4: Esecuzione di `kilo_implement` in sottofondo")
            res_imp = await session.call_tool(
                "kilo_implement",
                {
                    "task_instructions": "Crea un file test_mcp_output.txt con scritto 'Hello from MCP Client'",
                    "working_directory": os.getcwd(),
                    "background": True,
                },
            )
            text_resp = res_imp.content[0].text
            print(f"Risultato:\n{text_resp}")

            # Estrazione task_id
            match = re.search(r"task_id:\s*([a-zA-Z0-9-]+)", text_resp)
            if match:
                task_id = match.group(1)
                print(f"\n📌 Task ID estratto: {task_id}")

                # 7. Test 5: Controllo Avanzamento Task
                print("\n--------------------------------------------------")
                print(f"🧪 Test 5: Polling di `kilo_task_progress` per task_id: {task_id}")
                await asyncio.sleep(2)
                res_prog = await session.call_tool(
                    "kilo_task_progress",
                    {"task_id": task_id},
                )
                print(f"Avanzamento:\n{res_prog.content[0].text}")

            # 8. Test 6: Iniezione e aggiornamento TODO via MCP (`kilo_update_session_todo`)
            test_session_id = "ses_mcp_test_demo_123"
            print("\n--------------------------------------------------")
            print(f"🧪 Test 6: Aggiornamento dei TODO per sessione mock '{test_session_id}'")
            res_todo_update = await session.call_tool(
                "kilo_update_session_todo",
                {
                    "session_id": test_session_id,
                    "todos": [
                        {"content": "Passaggio 1: Analizzare la spec", "status": "completed", "priority": "high"},
                        {"content": "Passaggio 2: Scrivere il codice", "status": "in_progress", "priority": "high"},
                        {"content": "Passaggio 3: Eseguire i test", "status": "pending", "priority": "medium"},
                    ],
                },
            )
            print(f"Risultato aggiornamento:\n{res_todo_update.content[0].text}")

            # 9. Test 7: Lettura TODO via MCP (`kilo_get_session_todo`)
            print("\n--------------------------------------------------")
            print(f"🧪 Test 7: Lettura TODO per sessione '{test_session_id}'")
            res_todo_get = await session.call_tool(
                "kilo_get_session_todo",
                {"session_id": test_session_id},
            )
            print(f"Checklist letta:\n{res_todo_get.content[0].text}")

            print("\n--------------------------------------------------")
            print("🎉 Tutti i test end-to-end completati con successo!")


if __name__ == "__main__":
    script_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SERVER_PATH
    if not os.path.exists(script_path):
        print(f"❌ File non trovato: {script_path}")
        sys.exit(1)
    asyncio.run(run_mcp_tests(script_path))
