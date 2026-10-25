"""Unit tests for AntigravityTokenTracker service and CLI command."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from cli import cli
from tools.antigravity_token_tracker import AntigravityTokenTracker, SessionTokenStats


class TestAntigravityTokenTracker(unittest.TestCase):
    """Test suite for AntigravityTokenTracker."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.brain_dir = Path(self.test_dir) / "brain"
        self.brain_dir.mkdir(parents=True, exist_ok=True)
        self.tracker = AntigravityTokenTracker(brain_dir=self.brain_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_mock_session(self, session_id: str, events: list) -> Path:
        """Helper to create a session directory with transcript.jsonl."""
        session_folder = self.brain_dir / session_id / ".system_generated" / "logs"
        session_folder.mkdir(parents=True, exist_ok=True)
        transcript_file = session_folder / "transcript.jsonl"
        with open(transcript_file, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
        return transcript_file

    def test_empty_directory(self):
        """Test listing when no sessions exist."""
        sessions = self.tracker.list_sessions()
        self.assertEqual(sessions, [])
        self.assertIsNone(self.tracker.get_latest_session_id())

    def test_analyze_session_with_mcp_and_raw_read(self):
        """Test parsing of model tokens, lazy MCP call, raw file read, and savings."""
        events = [
            # User input
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "content": "Please check database impact",
            },
            # Step 1: PLANNER_RESPONSE with lazy call to agent-brain MCP tool
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "input_tokens": 1000,
                "cache_read_tokens": 200,
                "output_tokens": 150,
                "tool_calls": [
                    {
                        "name": "call_mcp_tool",
                        "args": {
                            "ServerName": "agent-brain",
                            "ToolName": "search_code_graph",
                            "Arguments": {"symbol_name": "Database"},
                        },
                    }
                ],
            },
            # Step 2: Tool output for agent-brain
            {
                "step_index": 2,
                "type": "GENERIC",
                "content": "{'target': 'Database', 'callers': ['get_db', 'run_query']}",
            },
            # Step 3: PLANNER_RESPONSE with traditional raw file read
            {
                "step_index": 3,
                "type": "PLANNER_RESPONSE",
                "input_tokens": 1200,
                "cache_read_tokens": 800,
                "output_tokens": 100,
                "tool_calls": [
                    {
                        "name": "read_file",
                        "args": {"Path": "/path/to/large_file.py"},
                    }
                ],
            },
            # Step 4: Tool output for raw file read (large payload)
            {
                "step_index": 4,
                "type": "GENERIC",
                "content": "def test():\n    pass\n" * 200,
            },
        ]

        self._create_mock_session("session-test-01", events)
        stats = self.tracker.analyze_session("session-test-01")

        self.assertEqual(stats.session_id, "session-test-01")
        self.assertEqual(stats.steps_count, 2)
        self.assertEqual(stats.input_tokens, 2200)
        self.assertEqual(stats.cache_read_tokens, 1000)
        self.assertEqual(stats.output_tokens, 250)
        self.assertEqual(stats.total_model_tokens, 2450)
        self.assertAlmostEqual(stats.caching_ratio_pct, (1000 / 3200) * 100, places=1)

        # Tool calls & payload validation
        self.assertEqual(stats.agent_brain_calls, 1)
        self.assertEqual(stats.raw_read_calls, 1)
        self.assertGreater(stats.agent_brain_payload_tokens, 0)
        self.assertGreater(stats.raw_read_payload_tokens, stats.agent_brain_payload_tokens)
        self.assertIsNotNone(stats.token_savings_pct)
        self.assertGreater(stats.token_savings_pct, 50.0)

    def test_analyze_all_aggregation(self):
        """Test aggregation across multiple sessions."""
        events_s1 = [
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "input_tokens": 500,
                "cache_read_tokens": 100,
                "output_tokens": 50,
                "tool_calls": [{"name": "search_code_graph", "args": {}}],
            },
            {"step_index": 2, "type": "GENERIC", "content": "graph result"},
        ]
        events_s2 = [
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "input_tokens": 700,
                "cache_read_tokens": 300,
                "output_tokens": 70,
                "tool_calls": [{"name": "read_file", "args": {}}],
            },
            {"step_index": 2, "type": "GENERIC", "content": "raw content"},
        ]

        self._create_mock_session("session-01", events_s1)
        self._create_mock_session("session-02", events_s2)

        agg = self.tracker.analyze_all()
        self.assertEqual(agg["sessions_count"], 2)
        self.assertEqual(agg["input_tokens"], 1200)
        self.assertEqual(agg["cache_read_tokens"], 400)
        self.assertEqual(agg["output_tokens"], 120)
        self.assertEqual(agg["agent_brain_calls"], 1)
        self.assertEqual(agg["raw_read_calls"], 1)

    def test_dual_directory_support(self):
        """Test tracking sessions spanning across multiple brain directories (IDE and App)."""
        app_dir = Path(self.test_dir) / "app_brain"
        ide_dir = Path(self.test_dir) / "ide_brain"
        app_dir.mkdir(parents=True, exist_ok=True)
        ide_dir.mkdir(parents=True, exist_ok=True)

        tracker = AntigravityTokenTracker(brain_dir=[app_dir, ide_dir])

        # Session in App
        s1_folder = app_dir / "s-app" / ".system_generated" / "logs"
        s1_folder.mkdir(parents=True, exist_ok=True)
        (s1_folder / "transcript.jsonl").write_text(
            json.dumps({"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "read_file"}]}) + "\n"
            + json.dumps({"type": "GENERIC", "content": "file content here"}) + "\n",
            encoding="utf-8",
        )

        # Session in IDE with quoted arguments
        s2_folder = ide_dir / "s-ide" / ".system_generated" / "logs"
        s2_folder.mkdir(parents=True, exist_ok=True)
        (s2_folder / "transcript.jsonl").write_text(
            json.dumps({
                "type": "PLANNER_RESPONSE",
                "tool_calls": [{
                    "name": "call_mcp_tool",
                    "args": {"ServerName": '"agent-brain"', "ToolName": '"search_code_graph"'}
                }]
            }) + "\n"
            + json.dumps({"type": "GENERIC", "content": "graph result here"}) + "\n",
            encoding="utf-8",
        )

        sessions = tracker.list_sessions()
        self.assertEqual(len(sessions), 2)

        st_ide = tracker.analyze_session("s-ide")
        self.assertEqual(st_ide.agent_brain_calls, 1)

        all_stats = tracker.analyze_all()
        self.assertEqual(all_stats["sessions_count"], 2)
        self.assertEqual(all_stats["agent_brain_calls"], 1)
        self.assertEqual(all_stats["raw_read_calls"], 1)

    def test_cli_track_command(self):
        """Test Click CLI 'track' command."""
        runner = CliRunner()

        events = [
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "input_tokens": 300,
                "cache_read_tokens": 50,
                "output_tokens": 25,
                "tool_calls": [{"name": "run_command", "args": {}}],
            }
        ]
        self._create_mock_session("cli-session", events)

        # Override default brain dirs with test directory
        with patch(
            "tools.antigravity_token_tracker.get_default_brain_dirs",
            return_value=[self.brain_dir],
        ):
            # Test default / latest session
            res = runner.invoke(cli, ["track"])
            self.assertEqual(res.exit_code, 0)
            self.assertIn("ANTIGRAVITY TOKEN TRACKER", res.output)
            self.assertIn("cli-session", res.output)

            # Test JSON output
            res_json = runner.invoke(cli, ["track", "--json"])
            self.assertEqual(res_json.exit_code, 0)
            data = json.loads(res_json.output)
            self.assertEqual(data["session_id"], "cli-session")
            self.assertEqual(data["input_tokens"], 300)

            # Test export output
            export_file = Path(self.test_dir) / "export.json"
            res_exp = runner.invoke(cli, ["track", "--export", str(export_file)])
            self.assertEqual(res_exp.exit_code, 0)
            self.assertTrue(export_file.exists())

            # Test list sessions option
            res_list = runner.invoke(cli, ["track", "--list"])
            self.assertEqual(res_list.exit_code, 0)
            self.assertIn("[SESSIONS] Recorded Antigravity Sessions:", res_list.output)
            self.assertIn("cli-session", res_list.output)


if __name__ == "__main__":
    unittest.main()
