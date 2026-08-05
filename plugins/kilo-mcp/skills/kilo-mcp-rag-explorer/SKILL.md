---
name: kilo-mcp-rag-explorer
description: Specialized skill for Kilo's explore agent when running codebase semantic searches. Optimizes index lookups, file summaries, and ensures read-only safety.
---

# kilo-mcp-rag-explorer

Use this skill when Kilo is invoked for RAG search and codebase exploration tasks through the MCP server.

## Logic and Behavior

- **Read-Only Safety:** You are running in exploration mode. DO NOT create, modify, or delete any files under any circumstances.
- **Index-First Retrieval:** Prioritize your `semantic_search` or codebase indexing tools to locate relevant code blocks. Avoid slow, sequential grep or file walking across the entire repository unless the index lookup fails.
- **Precise Location Mapping:** Always return the exact absolute or relative file paths and line number ranges for the matching snippets so the calling LLM can accurately use them for `focus_files`.
- **Structured Summaries:** Provide a concise explanation of how the matched components interact or where specific business logic is located. Keep the response dense, technical, and token-efficient.