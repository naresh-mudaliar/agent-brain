"""Unit tests for agent integrations and MCP auto-registration."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from cli import cli
from core.integrations import (
    SERVER_NAME,
    build_mcp_server_config,
    get_agent_config_path,
    register_all_clients,
    register_client_mcp,
)


class TestAgentIntegrations(unittest.TestCase):
    """Tests for agent MCP registration and configuration updates."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)
        self.mock_config = self.temp_path / "mock_mcp_config.json"
        self.mock_db = self.temp_path / "brain.db"
        self.mock_db.touch()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_get_agent_config_path(self) -> None:
        """Verify standard path resolution for recognized and unrecognized clients."""
        self.assertIsNotNone(get_agent_config_path("antigravity"))
        self.assertIsNotNone(get_agent_config_path("claude"))
        self.assertIsNotNone(get_agent_config_path("cursor"))
        self.assertIsNone(get_agent_config_path("unknown_agent_xyz"))

    def test_build_mcp_server_config(self) -> None:
        """Verify generated server config dictionary format and environment variables."""
        cfg = build_mcp_server_config(db_path=str(self.mock_db))
        self.assertIn("command", cfg)
        self.assertIn("args", cfg)
        self.assertIn("env", cfg)
        self.assertIn("AGENT_MEMORY_DB_PATH", cfg["env"])
        self.assertTrue(cfg["env"]["AGENT_MEMORY_DB_PATH"].endswith("brain.db"))

    def test_build_mcp_server_config_dynamic_discovery(self) -> None:
        """Verify dynamic workspace discovery omits hardcoded env paths."""
        cfg = build_mcp_server_config(db_path=None)
        self.assertIn("command", cfg)
        self.assertIn("args", cfg)
        self.assertNotIn("env", cfg)

    def test_register_creates_new_config_file(self) -> None:
        """Verify register_client_mcp creates directories and files when missing."""
        target_file = self.temp_path / "nested" / "config.json"
        self.assertFalse(target_file.exists())

        success, msg = register_client_mcp(
            client="antigravity",
            db_path=str(self.mock_db),
            config_path_override=target_file,
        )
        self.assertTrue(success)
        self.assertTrue(target_file.exists())

        with open(target_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn("mcpServers", data)
        self.assertIn(SERVER_NAME, data["mcpServers"])
        self.assertEqual(data["mcpServers"][SERVER_NAME]["args"], ["mcp"])

    def test_register_preserves_existing_servers(self) -> None:
        """Verify register_client_mcp preserves existing servers (e.g. stitch)."""
        initial_data = {
            "mcpServers": {
                "stitch": {
                    "serverUrl": "https://stitch.googleapis.com/mcp",
                    "headers": {"X-Goog-Api-Key": "test-key-123"},
                }
            }
        }
        self.mock_config.write_text(json.dumps(initial_data, indent=2), encoding="utf-8")

        success, msg = register_client_mcp(
            client="antigravity",
            db_path=str(self.mock_db),
            config_path_override=self.mock_config,
        )
        self.assertTrue(success)

        with open(self.mock_config, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Ensure stitch is still intact
        self.assertIn("stitch", data["mcpServers"])
        self.assertEqual(data["mcpServers"]["stitch"]["headers"]["X-Goog-Api-Key"], "test-key-123")
        # Ensure agent-brain was added
        self.assertIn(SERVER_NAME, data["mcpServers"])

    def test_register_is_idempotent(self) -> None:
        """Verify running register multiple times updates safely without error."""
        for _ in range(3):
            success, msg = register_client_mcp(
                client="antigravity",
                db_path=str(self.mock_db),
                config_path_override=self.mock_config,
            )
            self.assertTrue(success)

        with open(self.mock_config, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn(SERVER_NAME, data["mcpServers"])

    def test_register_invalid_client(self) -> None:
        """Verify appropriate failure message when registering unknown client."""
        success, msg = register_client_mcp(client="nonexistent_agent_client")
        self.assertFalse(success)
        self.assertIn("Unknown agent client", msg)

    def test_cli_register_command(self) -> None:
        """Verify CLI 'register' command works via CliRunner."""
        runner = CliRunner()
        with patch("core.integrations.get_agent_config_path") as mock_path:
            mock_path.return_value = self.mock_config
            res = runner.invoke(cli, ["register", "--client", "antigravity", "--db-path", str(self.mock_db)])
            self.assertEqual(res.exit_code, 0)
            self.assertIn("ANTIGRAVITY", res.output)
            self.assertIn("Successfully registered", res.output)


if __name__ == "__main__":
    unittest.main()
