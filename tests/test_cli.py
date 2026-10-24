"""Unit tests for cli.py."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from cli import cli
from sync.s3_sync import generate_encryption_key


class TestCLI(unittest.TestCase):
    """Test suite for CLI commands."""

    def setUp(self):
        self.runner = CliRunner()
        self.test_dir = tempfile.mkdtemp()
        self.codebase_dir = Path(self.test_dir) / "codebase"
        self.codebase_dir.mkdir(parents=True, exist_ok=True)
        (self.codebase_dir / "example.py").write_text(
            "class Sample:\n    def run(self):\n        pass\n"
        )
        self.key = generate_encryption_key()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_cli_help(self):
        """Test cli top-level help."""
        result = self.runner.invoke(cli, ["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("init", result.output)
        self.assertIn("export", result.output)
        self.assertIn("pull", result.output)

    def test_init_command_with_mock_db(self):
        """Test 'init --path <dir>' command."""
        with patch("cli.Database") as mock_db_cls:
            mock_db_instance = MagicMock()
            mock_db_cls.return_value = mock_db_instance

            result = self.runner.invoke(
                cli, ["init", "--path", str(self.codebase_dir)]
            )
            self.assertEqual(result.exit_code, 0)
            self.assertIn("Scanning codebase", result.output)
            self.assertIn("Classes/Structs", result.output)
            self.assertIn("Functions/Defs", result.output)
            self.assertIn("Graph successfully populated", result.output)

    def test_init_missing_path_fails(self):
        """Test 'init' without --path option fails."""
        result = self.runner.invoke(cli, ["init"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Missing option '--path'", result.output)

    def test_export_and_pull_commands(self):
        """Test roundtrip 'export' and 'pull' CLI commands."""
        memory_dir = Path(self.test_dir) / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "brain.db").write_text("TEST_DB_DATA")

        archive_file = Path(self.test_dir) / "memory-seed.enc"
        restore_dir = Path(self.test_dir) / "restored"

        # Export
        export_res = self.runner.invoke(
            cli,
            [
                "export",
                "--source",
                str(memory_dir),
                "--output",
                str(archive_file),
                "--key",
                self.key,
            ],
        )
        self.assertEqual(export_res.exit_code, 0)
        self.assertIn("Memory exported and encrypted successfully", export_res.output)
        self.assertTrue(archive_file.exists())

        # Pull
        pull_res = self.runner.invoke(
            cli,
            [
                "pull",
                "--archive",
                str(archive_file),
                "--target",
                str(restore_dir),
                "--key",
                self.key,
            ],
        )
        self.assertEqual(pull_res.exit_code, 0)
        self.assertIn("Database restored successfully", pull_res.output)
        self.assertTrue((restore_dir / "brain.db").exists())
        self.assertEqual((restore_dir / "brain.db").read_text(), "TEST_DB_DATA")

    def test_generate_key_command(self):
        """Test 'generate-key' command."""
        result = self.runner.invoke(cli, ["generate-key"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("New Encryption Key Generated", result.output)


if __name__ == "__main__":
    unittest.main()
