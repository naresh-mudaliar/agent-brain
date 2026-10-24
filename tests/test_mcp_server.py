"""Unit tests for tools.mcp_server."""

import asyncio
import json
import unittest
from unittest.mock import MagicMock

from tools.mcp_server import (
    get_dependency_impact_impl,
    search_code_graph_impl,
    server,
)


class MockQueryResult:
    """Mock KùzuDB QueryResult."""

    def __init__(self, header, result_set):
        self.header = header
        self.result_set = result_set


class TestMCPServer(unittest.TestCase):
    """Test suite for tools.mcp_server."""

    def test_search_code_graph_empty_input(self):
        """Test search_code_graph_impl with empty input."""
        res = search_code_graph_impl("")
        self.assertFalse(res["success"])
        self.assertIn("cannot be empty", res["error"])

    def test_search_code_graph_success(self):
        """Test search_code_graph_impl returning symbol, file, callers, callees."""
        mock_db = MagicMock()

        # Step 1: Match node
        node_res = MockQueryResult(
            header=[
                "id",
                "name",
                "qualified_name",
                "labels",
                "path",
                "line_number",
                "end_line_number",
                "docstring",
            ],
            result_set=[
                [
                    "core/database.py:Database",
                    "Database",
                    "Database",
                    ["Class"],
                    "core/database.py",
                    33,
                    150,
                    "Database manager docstring",
                ]
            ],
        )

        # Step 2: Callers
        callers_res = MockQueryResult(
            header=["name", "qualified_name", "path", "line_number"],
            result_set=[
                ["run_app", "run_app", "app.py", 12],
            ],
        )

        # Step 3: Callees
        callees_res = MockQueryResult(
            header=["name", "labels", "qualified_name", "path", "line_number"],
            result_set=[
                ["connect", ["Function"], "Database.connect", "core/database.py", 50],
            ],
        )

        # Step 4: Parent
        parent_res = MockQueryResult(
            header=["name", "path", "labels"],
            result_set=[
                ["database.py", "core/database.py", ["CodeFile"]],
            ],
        )

        mock_db.execute_query.side_effect = [
            node_res,
            callers_res,
            callees_res,
            parent_res,
        ]

        res = search_code_graph_impl("Database", db=mock_db)

        self.assertTrue(res["success"])
        self.assertEqual(res["matches_count"], 1)
        match = res["matches"][0]
        self.assertEqual(match["name"], "Database")
        self.assertEqual(match["file_path"], "core/database.py")
        self.assertEqual(len(match["immediate_callers"]), 1)
        self.assertEqual(match["immediate_callers"][0]["name"], "run_app")
        self.assertEqual(len(match["immediate_callees"]), 1)
        self.assertEqual(match["immediate_callees"][0]["name"], "connect")

    def test_get_dependency_impact_success(self):
        """Test get_dependency_impact_impl multi-hop traversal."""
        mock_db = MagicMock()

        # Target check
        target_res = MockQueryResult(
            header=["id", "name", "qualified_name", "path", "labels"],
            result_set=[
                [
                    "core/database.py:execute_query",
                    "execute_query",
                    "execute_query",
                    "core/database.py",
                    ["Function"],
                ]
            ],
        )

        # Multi-hop impact: caller1 (hop 1), caller2 (hop 2)
        impact_res = MockQueryResult(
            header=[
                "id",
                "name",
                "qualified_name",
                "file_path",
                "line_number",
                "hop_distance",
            ],
            result_set=[
                [
                    "extractors/ast_parser.py:ASTParser.save_to_graph",
                    "save_to_graph",
                    "ASTParser.save_to_graph",
                    "extractors/ast_parser.py",
                    234,
                    1,
                ],
                [
                    "extractors/ast_parser.py:ASTParser.parse_and_store",
                    "parse_and_store",
                    "ASTParser.parse_and_store",
                    "extractors/ast_parser.py",
                    350,
                    2,
                ],
            ],
        )

        mock_db.execute_query.side_effect = [target_res, impact_res]

        res = get_dependency_impact_impl("execute_query", db=mock_db)

        self.assertTrue(res["success"])
        self.assertEqual(res["total_affected_functions"], 2)
        self.assertEqual(res["total_affected_files"], 1)
        self.assertIn("extractors/ast_parser.py", res["affected_files"])
        self.assertIn(1, res["impact_by_depth"])
        self.assertIn(2, res["impact_by_depth"])

    def test_mcp_server_registered_tools(self):
        """Test that MCP server registers the two required tools and can call them."""
        tool_names = [t.name for t in server._tool_manager.list_tools()]
        self.assertIn("search_code_graph", tool_names)
        self.assertIn("get_dependency_impact", tool_names)

        # Test calling search_code_graph via MCP server registered tool function
        tool = server._tool_manager.get_tool("search_code_graph")
        self.assertIsNotNone(tool)
        raw_text = tool.fn("")
        parsed = json.loads(raw_text)
        self.assertFalse(parsed["success"])
        self.assertIn("cannot be empty", parsed["error"])


if __name__ == "__main__":
    unittest.main()
