# AGENTS.md - Agent Memory & Token Efficiency Rule

## Code Exploration & Dependency Impact Protocol
- **Token Efficiency**: Always prefer using the `agent-brain` MCP tools (`search_code_graph`, `get_dependency_impact`) to locate symbols, callers, and dependencies before falling back to reading raw files with `read_file`.
- When modifying or refactoring any function or class, first run `get_dependency_impact` to identify all downstream callers.
- **Avoid Redundant File Reads**: `search_code_graph` and `get_dependency_impact` return the exact `code_snippet` along with line numbers. Do NOT call `read_file` on symbols already returned by the graph unless you require lines outside the returned snippet.
