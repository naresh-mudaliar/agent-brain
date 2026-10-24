"""Unit tests for performance and token efficiency benchmark."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from benchmark import MemoryBenchmarker, run_benchmark
from cli import cli
from core.integrations import get_registered_clients


class TestBenchmark(unittest.TestCase):
    """Test suite for benchmark execution across client detection states."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.codebase_dir = Path(self.temp_dir) / "src"
        self.codebase_dir.mkdir(parents=True, exist_ok=True)

        # Create sample files in multiple languages
        (self.codebase_dir / "service.py").write_text(
            "class UserService:\n"
            "    def __init__(self):\n"
            "        self.active = True\n"
            "    def get_user(self, uid):\n"
            "        return {'id': uid}\n",
            encoding="utf-8",
        )
        (self.codebase_dir / "calculator.rs").write_text(
            "pub struct Calculator;\n"
            "impl Calculator {\n"
            "    pub fn add(a: i32, b: i32) -> i32 { a + b }\n"
            "}\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_run_baseline_rag_multi_language(self) -> None:
        """Verify run_baseline_rag reads all supported language files and counts tokens."""
        bench = MemoryBenchmarker(codebase_dir=str(self.codebase_dir), db=None)
        res = bench.run_baseline_rag()

        self.assertEqual(res["file_count"], 2)
        self.assertGreater(res["tokens_consumed"], 0)
        self.assertIn("Traditional Raw-File RAG", res["method"])

    def test_run_file_indexing_benchmark(self) -> None:
        """Verify run_file_indexing_benchmark returns parsed AST statistics and latency."""
        bench = MemoryBenchmarker(codebase_dir=str(self.codebase_dir), db=None)
        res = bench.run_file_indexing_benchmark()

        self.assertEqual(res["files_indexed"], 2)
        self.assertGreaterEqual(res["classes_extracted"], 2)
        self.assertGreaterEqual(res["functions_extracted"], 3)
        self.assertGreater(res["raw_tokens"], 0)
        self.assertIn("Multi-Language AST File Indexing", res["method"])

    def test_run_benchmark_without_mcp_client(self) -> None:
        """When no MCP client is registered, benchmark runs in 'indexing' mode."""
        res = run_benchmark(
            path=str(self.codebase_dir),
            registered_clients_override=[],
            force_mode="auto",
        )

        self.assertEqual(res["mode"], "indexing")
        self.assertIn("indexing", res)
        self.assertEqual(res["indexing"]["files_indexed"], 2)

    def test_run_benchmark_with_mcp_client(self) -> None:
        """When an MCP client is detected, benchmark runs comparative graph traversal."""
        res = run_benchmark(
            path=str(self.codebase_dir),
            symbol="__init__",
            registered_clients_override=["antigravity"],
            force_mode="auto",
        )

        self.assertEqual(res["mode"], "compare")
        self.assertIn("baseline", res)
        self.assertIn("graph", res)
        self.assertIn("token_savings_pct", res)
        self.assertIsInstance(res["token_savings_pct"], float)

    def test_cli_benchmark_command(self) -> None:
        """Verify 'agent-memory benchmark' works from CLI."""
        runner = CliRunner()
        res = runner.invoke(
            cli,
            ["benchmark", "--path", str(self.codebase_dir), "--force-mode", "indexing"],
        )
        self.assertEqual(res.exit_code, 0)
        self.assertIn("File Indexing Benchmark", res.output)


if __name__ == "__main__":
    unittest.main()
