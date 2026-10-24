"""MCP server for agent-brain using official Model Context Protocol Python SDK.

Exposes two tools to local AI agents:
1. search_code_graph(symbol_name: str):
   Runs openCypher query on KùzuDB to return a function/class node,
   its file path, and its immediate callers/callees.
2. get_dependency_impact(function_name: str):
   Performs a multi-hop traversal to find all downstream functions affected
   by changing that function.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict, List, Optional

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    try:
        from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "Could not import MCPServer from 'mcp.server.mcpserver' or 'mcp.server.fastmcp'. "
            "Please ensure 'mcp' is installed via 'pip install mcp'."
        ) from exc

from core.database import Database, DatabaseConnectionError, DatabaseQueryError, get_db

# Configure logger to output to stderr so stdout is reserved for JSON-RPC MCP messages
logger = logging.getLogger("agent-brain.mcp_server")
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Initialize MCP server
server = MCPServer("agent-brain")


def _serialize_value(val: Any) -> Any:
    """Helper to convert KùzuDB objects into JSON-serializable primitives."""
    if val is None or isinstance(val, (str, int, float, bool)):
        return val
    if hasattr(val, "properties"):
        props = dict(val.properties)
        if hasattr(val, "labels"):
            props["_labels"] = list(val.labels)
        return props
    if isinstance(val, (list, tuple, set)):
        return [_serialize_value(item) for item in val]
    if isinstance(val, dict):
        return {k: _serialize_value(v) for k, v in val.items()}
    return str(val)


def _result_to_dicts(result: Any) -> List[Dict[str, Any]]:
    """Convert KùzuDB query result into list of dicts based on result header."""
    if result is None:
        return []
    if hasattr(result, "result_set") and hasattr(result, "header"):
        headers = [str(h) for h in result.header]
        records: List[Dict[str, Any]] = []
        for row in result.result_set:
            record = {}
            for idx, col_name in enumerate(headers):
                if idx < len(row):
                    record[col_name] = _serialize_value(row[idx])
            records.append(record)
        return records
    if isinstance(result, list):
        return [_serialize_value(item) for item in result]
    return []


def search_code_graph_impl(
    symbol_name: str, db: Optional[Database] = None
) -> Dict[str, Any]:
    """Core logic to search for a function/class, its file path, callers, and callees.

    Args:
        symbol_name: Name or qualified name of the class or function.
        db: Optional Database instance.

    Returns:
        Structured dictionary containing matched symbols, file paths, callers, and callees.
    """
    clean_name = symbol_name.strip()
    if not clean_name:
        return {
            "success": False,
            "error": "Symbol name cannot be empty.",
            "symbol": symbol_name,
            "matches": [],
        }

    database = db or get_db()

    # 1. Search for matching function or class nodes
    search_query = """
    MATCH (n:Function)
    WHERE n.name = $name OR n.qualified_name = $name OR n.id = $name
    RETURN n.id AS id,
           n.name AS name,
           n.qualified_name AS qualified_name,
           'Function' AS label,
           n.path AS path,
           n.line_number AS line_number,
           n.end_line_number AS end_line_number,
           n.docstring AS docstring
    UNION
    MATCH (n:Class)
    WHERE n.name = $name OR n.qualified_name = $name OR n.id = $name
    RETURN n.id AS id,
           n.name AS name,
           n.qualified_name AS qualified_name,
           'Class' AS label,
           n.path AS path,
           n.line_number AS line_number,
           n.end_line_number AS end_line_number,
           n.docstring AS docstring
    """
    try:
        raw_nodes = database.execute_query(search_query, {"name": clean_name})
        node_records = _result_to_dicts(raw_nodes)

        # Fallback to case-insensitive partial match if no exact match is found
        if not node_records:
            fallback_query = """
            MATCH (n:Function)
            WHERE toLower(n.name) = toLower($name) OR n.name CONTAINS $name
            RETURN n.id AS id,
                   n.name AS name,
                   n.qualified_name AS qualified_name,
                   'Function' AS label,
                   n.path AS path,
                   n.line_number AS line_number,
                   n.end_line_number AS end_line_number,
                   n.docstring AS docstring
            LIMIT 10
            UNION
            MATCH (n:Class)
            WHERE toLower(n.name) = toLower($name) OR n.name CONTAINS $name
            RETURN n.id AS id,
                   n.name AS name,
                   n.qualified_name AS qualified_name,
                   'Class' AS label,
                   n.path AS path,
                   n.line_number AS line_number,
                   n.end_line_number AS end_line_number,
                   n.docstring AS docstring
            LIMIT 10
            """
            raw_nodes = database.execute_query(fallback_query, {"name": clean_name})
            node_records = _result_to_dicts(raw_nodes)

        if not node_records:
            return {
                "success": True,
                "symbol": clean_name,
                "message": f"No function or class matching '{clean_name}' found in graph.",
                "matches": [],
            }

        matches = []
        for rec in node_records:
            node_id = rec.get("id")
            node_type = rec.get("label") or (
                rec.get("labels", ["Unknown"])[0]
                if isinstance(rec.get("labels"), list) and rec.get("labels")
                else "Unknown"
            )

            # Query immediate callers: who calls this node?
            callers_query = """
            MATCH (caller:Function)-[:CALLS]->(target:Function)
            WHERE target.id = $id
            RETURN caller.name AS name,
                   caller.qualified_name AS qualified_name,
                   caller.path AS path,
                   caller.line_number AS line_number
            UNION
            MATCH (caller:Function)-[:CALLS]->(target:Class)
            WHERE target.id = $id
            RETURN caller.name AS name,
                   caller.qualified_name AS qualified_name,
                   caller.path AS path,
                   caller.line_number AS line_number
            """
            raw_callers = database.execute_query(callers_query, {"id": node_id})
            callers = _result_to_dicts(raw_callers)

            # Query immediate callees: who does this node call?
            callees_query = """
            MATCH (caller:Function)-[:CALLS]->(callee:Function)
            WHERE caller.id = $id
            RETURN callee.name AS name,
                   'Function' AS label,
                   callee.qualified_name AS qualified_name,
                   callee.path AS path,
                   callee.line_number AS line_number
            UNION
            MATCH (caller:Function)-[:CALLS]->(callee:Class)
            WHERE caller.id = $id
            RETURN callee.name AS name,
                   'Class' AS label,
                   callee.qualified_name AS qualified_name,
                   callee.path AS path,
                   callee.line_number AS line_number
            """
            raw_callees = database.execute_query(callees_query, {"id": node_id})
            callees = _result_to_dicts(raw_callees)

            # Query containing parent file or class
            parent_query = """
            MATCH (parent:CodeFile)-[:CONTAINS]->(target:Function)
            WHERE target.id = $id
            RETURN parent.filename AS name,
                   parent.path AS path,
                   'CodeFile' AS label
            UNION
            MATCH (parent:Class)-[:CONTAINS]->(target:Function)
            WHERE target.id = $id
            RETURN parent.name AS name,
                   parent.path AS path,
                   'Class' AS label
            UNION
            MATCH (parent:CodeFile)-[:CONTAINS]->(target:Class)
            WHERE target.id = $id
            RETURN parent.filename AS name,
                   parent.path AS path,
                   'CodeFile' AS label
            """
            raw_parent = database.execute_query(parent_query, {"id": node_id})
            parent_info = _result_to_dicts(raw_parent)

            matches.append(
                {
                    "id": node_id,
                    "name": rec.get("name"),
                    "type": node_type,
                    "qualified_name": rec.get("qualified_name"),
                    "file_path": rec.get("path"),
                    "line_number": rec.get("line_number"),
                    "end_line_number": rec.get("end_line_number"),
                    "docstring": rec.get("docstring"),
                    "parent": parent_info[0] if parent_info else None,
                    "immediate_callers": callers,
                    "immediate_callees": callees,
                }
            )

        return {
            "success": True,
            "symbol": clean_name,
            "matches_count": len(matches),
            "matches": matches,
        }

    except (DatabaseConnectionError, DatabaseQueryError) as err:
        logger.error(f"Database error during search_code_graph: {err}")
        return {
            "success": False,
            "error": str(err),
            "symbol": clean_name,
            "matches": [],
        }
    except Exception as err:
        logger.error(f"Unexpected error in search_code_graph: {err}", exc_info=True)
        return {
            "success": False,
            "error": f"Internal error searching graph: {err}",
            "symbol": clean_name,
            "matches": [],
        }


def get_dependency_impact_impl(
    function_name: str, max_depth: int = 5, db: Optional[Database] = None
) -> Dict[str, Any]:
    """Core logic to perform multi-hop traversal to find downstream functions affected.

    Args:
        function_name: Name or qualified name of the target function.
        max_depth: Maximum hops to traverse in call chain (default: 5).
        db: Optional Database instance.

    Returns:
        Structured impact assessment report detailing all affected functions by hop distance.
    """
    clean_name = function_name.strip()
    if not clean_name:
        return {
            "success": False,
            "error": "Function name cannot be empty.",
            "function_name": function_name,
            "impacted_functions": [],
        }

    database = db or get_db()

    # 1. Verify existence of the target function/symbol
    check_query = """
    MATCH (target:Function)
    WHERE target.name = $name OR target.qualified_name = $name
    RETURN target.id AS id,
           target.name AS name,
           target.qualified_name AS qualified_name,
           target.path AS path,
           'Function' AS label
    UNION
    MATCH (target:Class)
    WHERE target.name = $name OR target.qualified_name = $name
    RETURN target.id AS id,
           target.name AS name,
           target.qualified_name AS qualified_name,
           target.path AS path,
           'Class' AS label
    """
    try:
        raw_target = database.execute_query(check_query, {"name": clean_name})
        target_records = _result_to_dicts(raw_target)

        if not target_records:
            return {
                "success": True,
                "function_name": clean_name,
                "message": f"Target function '{clean_name}' not found in the codebase graph.",
                "total_affected": 0,
                "impacted_functions": [],
            }

        target_info = target_records[0]

        # 2. Multi-hop traversal: find all functions that reach the target via CALLS chain
        try:
            safe_depth = max(1, min(int(max_depth), 20))
        except (ValueError, TypeError):
            safe_depth = 5

        traversal_query = f"""
        MATCH path = (affected:Function)-[e:CALLS*1..{safe_depth}]->(target:Function)
        WHERE (target.name = $name OR target.qualified_name = $name)
          AND affected.id <> target.id
        RETURN affected.id AS id,
               affected.name AS name,
               affected.qualified_name AS qualified_name,
               affected.path AS file_path,
               affected.line_number AS line_number,
               length(path) AS hop_distance
        ORDER BY hop_distance ASC, affected.qualified_name ASC
        """
        raw_impact = database.execute_query(traversal_query, {"name": clean_name})
        impact_records = _result_to_dicts(raw_impact)

        # Deduplicate per affected function keeping shortest hop distance
        impacted_map: Dict[str, Dict[str, Any]] = {}
        for rec in impact_records:
            fid = rec.get("id")
            if not fid:
                continue
            hop = rec.get("hop_distance", 1)
            if fid not in impacted_map or hop < impacted_map[fid]["hop_distance"]:
                impacted_map[fid] = rec

        impacted_list = sorted(
            impacted_map.values(),
            key=lambda x: (x.get("hop_distance", 0), x.get("qualified_name", "")),
        )

        affected_files = sorted(
            list({rec["file_path"] for rec in impacted_list if rec.get("file_path")})
        )

        # Categorize by hop distance
        by_hop: Dict[int, List[Dict[str, Any]]] = {}
        for rec in impacted_list:
            hop = rec.get("hop_distance", 1)
            by_hop.setdefault(hop, []).append(rec)

        return {
            "success": True,
            "target": target_info,
            "max_depth_scanned": max_depth,
            "total_affected_functions": len(impacted_list),
            "total_affected_files": len(affected_files),
            "affected_files": affected_files,
            "impact_by_depth": by_hop,
            "impacted_functions": impacted_list,
        }

    except (DatabaseConnectionError, DatabaseQueryError) as err:
        logger.error(f"Database error during get_dependency_impact: {err}")
        return {
            "success": False,
            "error": str(err),
            "function_name": clean_name,
            "impacted_functions": [],
        }
    except Exception as err:
        logger.error(f"Unexpected error in get_dependency_impact: {err}", exc_info=True)
        return {
            "success": False,
            "error": f"Internal error evaluating dependency impact: {err}",
            "function_name": clean_name,
            "impacted_functions": [],
        }


# Register MCP tools with the server
@server.tool()
def search_code_graph(symbol_name: str) -> str:
    """Runs an openCypher query on KùzuDB to return a function/class node,
    its file path, and its immediate callers/callees.

    Args:
        symbol_name: The name or qualified name of the class or function to find.

    Returns:
        JSON string containing matching nodes, file locations, callers, and callees.
    """
    result = search_code_graph_impl(symbol_name)
    return json.dumps(result, indent=2)


@server.tool()
def get_dependency_impact(function_name: str) -> str:
    """Performs a multi-hop traversal to find all downstream functions affected
    by changing that function.

    Args:
        function_name: The name or qualified name of the function being changed.

    Returns:
        JSON string detailing affected functions, depth/hop distance, and affected files.
    """
    result = get_dependency_impact_impl(function_name)
    return json.dumps(result, indent=2)


def main() -> None:
    """Run the MCP server over stdio transport."""
    logger.info("Starting agent-brain MCP server on stdio transport...")
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
