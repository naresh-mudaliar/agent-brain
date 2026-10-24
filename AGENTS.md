# AGENTS.md - Agent Memory & Token Efficiency Rule

## Code Exploration & Dependency Impact Protocol
- **Token Efficiency**: Always prefer using the `agent-brain` MCP tools (`search_code_graph`, `get_dependency_impact`) to locate symbols, callers, and dependencies before falling back to reading raw files with `read_file`.
- When modifying or refactoring any function or class, first run `get_dependency_impact` to identify all downstream callers.
