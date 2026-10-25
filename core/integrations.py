"""Agent integrations module for agent-brain.

Handles auto-detection and registration of the MCP server across supported AI agent
clients (Antigravity, Claude Desktop, Cursor) on Windows, macOS, and Linux.
Preserves any existing MCP configurations (e.g. stitch, filesystem, etc.) without data loss.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("agent_brain.integrations")

SERVER_NAME = "agent-brain"
ACCEPTED_SERVER_NAMES = {"agent-brain"}

AGENTS_RULE_CONTENT = """# AGENTS.md - Agent Memory & Token Efficiency Rule

## Code Exploration & Dependency Impact Protocol
- **Token Efficiency**: Always prefer using the `agent-brain` MCP tools (`search_code_graph`, `get_dependency_impact`) to locate symbols, callers, and dependencies before falling back to reading raw files with `read_file`.
- When modifying or refactoring any function or class, first run `get_dependency_impact` to identify all downstream callers.
"""


def ensure_workspace_rule(workspace_dir: Optional[str | Path] = None) -> Optional[Path]:
    """Ensure AGENTS.md rule exists in target workspace directory for proactive agent graph usage."""
    target_dir = Path(workspace_dir or Path.cwd()).resolve()
    rule_file = target_dir / "AGENTS.md"
    if not rule_file.exists():
        try:
            rule_file.write_text(AGENTS_RULE_CONTENT, encoding="utf-8")
            logger.info(f"Created proactive agent rule at {rule_file}")
            return rule_file
        except Exception as e:
            logger.warning(f"Could not create AGENTS.md at {rule_file}: {e}")
    return None


def get_agent_config_path(client: str, workspace_dir: Optional[str] = None) -> Optional[Path]:
    """Resolve standard configuration file path for a given agent client.

    Args:
        client: Client identifier ('antigravity', 'claude', 'cursor').
        workspace_dir: Optional workspace directory for workspace-scoped configs.

    Returns:
        Path object pointing to the client configuration file, or None if unrecognized.
    """
    client_norm = client.lower().strip()

    if client_norm == "antigravity":
        # Global Antigravity config
        return Path.home() / ".gemini" / "config" / "mcp_config.json"

    if client_norm == "claude":
        # Claude Desktop config across platforms
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA")
            base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
            return base / "Claude" / "claude_desktop_config.json"
        elif sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        else:
            return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"

    if client_norm == "cursor":
        # Cursor workspace-scoped or home-scoped mcp configuration
        if workspace_dir:
            return Path(workspace_dir) / ".cursor" / "mcp.json"
        return Path.cwd() / ".cursor" / "mcp.json"

    return None


def build_mcp_server_config(
    db_path: Optional[str] = None, enable_watch: bool = False
) -> Dict[str, Any]:
    """Generate the MCP server configuration block for agent-brain.

    Args:
        db_path: Optional path to the .agent-memory/brain.db database.
            If None, enables dynamic workspace discovery so the server resolves
            ./.agent-memory/brain.db relative to whichever project workspace is currently active.
        enable_watch: If True, configures the MCP server to automatically start
            the background outbox file watcher on launch.

    Returns:
        Dictionary conforming to the standard Stdio MCP server schema.
    """
    # Check if agent-brain or agent-memory executable is available on PATH
    brain_exec = "agent-brain" if shutil.which("agent-brain") else ("agent-memory" if shutil.which("agent-memory") else None)
    if brain_exec:
        command = brain_exec
        args = ["mcp"]
        if enable_watch:
            args.append("--watch")
    else:
        # Fall back to current python executable
        python_exec = sys.executable.replace("\\", "/")
        command = python_exec
        args = ["-m", "tools.mcp_server"]
        if enable_watch:
            args.append("--watch")

    cfg: Dict[str, Any] = {
        "command": command,
        "args": args,
    }

    env_vars: Dict[str, str] = {}
    if enable_watch:
        env_vars["AGENT_MEMORY_WATCH"] = "1"

    # Only set explicit AGENT_MEMORY_DB_PATH environment variable if explicitly specified.
    # When omitted, dynamic workspace discovery resolves ./.agent-memory/brain.db in the active workspace.
    if db_path is not None:
        resolved_db = Path(db_path).resolve()
        db_path_str = str(resolved_db).replace("\\", "/")
        env_vars["AGENT_MEMORY_DB_PATH"] = db_path_str

    if env_vars:
        cfg["env"] = env_vars

    return cfg


def register_client_mcp(
    client: str,
    db_path: Optional[str] = None,
    config_path_override: Optional[Path] = None,
    workspace_dir: Optional[str] = None,
    enable_watch: bool = False,
) -> Tuple[bool, str]:
    """Register or update agent-brain in the given AI agent's MCP config.

    Args:
        client: Client identifier ('antigravity', 'claude', 'cursor').
        db_path: Path to the .agent-memory/brain.db database.
        config_path_override: Optional explicit path for testing.
        workspace_dir: Optional workspace directory for workspace-scoped configs.
        enable_watch: Automatically run background outbox watcher when agent connects.

    Returns:
        Tuple of (success: bool, message: str).
    """
    cfg_path = config_path_override or get_agent_config_path(client, workspace_dir)
    if not cfg_path:
        return False, f"Unknown agent client '{client}'. Supported: antigravity, claude, cursor"

    try:
        # Ensure parent directory exists
        cfg_path.parent.mkdir(parents=True, exist_ok=True)

        data: Dict[str, Any] = {}
        if cfg_path.exists():
            try:
                content = cfg_path.read_text(encoding="utf-8").strip()
                if content:
                    data = json.loads(content)
            except Exception as e:
                logger.warning(f"Could not parse existing JSON from {cfg_path}: {e}. Creating fresh config.")
                data = {}

        if not isinstance(data, dict):
            data = {}

        # Ensure 'mcpServers' object exists
        if "mcpServers" not in data or not isinstance(data["mcpServers"], dict):
            data["mcpServers"] = {}

        # Build server config and insert
        server_cfg = build_mcp_server_config(db_path, enable_watch=enable_watch)
        data["mcpServers"][SERVER_NAME] = server_cfg

        # Write back indented JSON
        cfg_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        logger.info(f"Successfully registered {SERVER_NAME} in {cfg_path}")

        # Ensure proactive workspace rule exists in target workspace
        rule_created = ensure_workspace_rule(workspace_dir)
        rule_msg = f" (and initialized {rule_created.name})" if rule_created else ""

        return True, f"Successfully registered '{SERVER_NAME}' in {cfg_path}{rule_msg}"

    except Exception as exc:
        err_msg = f"Failed to register with {client} at {cfg_path}: {exc}"
        logger.error(err_msg, exc_info=True)
        return False, err_msg


def register_all_clients(
    db_path: Optional[str] = None,
    workspace_dir: Optional[str] = None,
    enable_watch: bool = False,
) -> List[Tuple[str, bool, str]]:
    """Register MCP server with all supported agent clients.

    Returns:
        List of (client_name, success, message) tuples.
    """
    clients = ["antigravity", "claude", "cursor"]
    results = []
    for c in clients:
        success, msg = register_client_mcp(
            c, db_path=db_path, workspace_dir=workspace_dir, enable_watch=enable_watch
        )
        results.append((c, success, msg))
    return results


def get_registered_clients(
    workspace_dir: Optional[str] = None,
    client_paths_override: Optional[Dict[str, Path]] = None,
) -> List[str]:
    """Return list of clients where agent-brain is actively configured.

    Args:
        workspace_dir: Optional workspace directory for workspace-scoped configs.
        client_paths_override: Optional mapping of client name to Path for testing.

    Returns:
        List of client names (e.g. ['antigravity', 'claude']).
    """
    clients = ["antigravity", "claude", "cursor"]
    registered: List[str] = []

    for client in clients:
        if client_paths_override and client in client_paths_override:
            cfg_path = client_paths_override[client]
        else:
            cfg_path = get_agent_config_path(client, workspace_dir=workspace_dir)

        if cfg_path and cfg_path.exists():
            try:
                content = cfg_path.read_text(encoding="utf-8").strip()
                if content:
                    data = json.loads(content)
                    if isinstance(data, dict):
                        servers = data.get("mcpServers", {})
                        if isinstance(servers, dict) and any(s in servers for s in ACCEPTED_SERVER_NAMES):
                            registered.append(client)
            except Exception as e:
                logger.debug(f"Could not inspect config at {cfg_path}: {e}")

    return registered

