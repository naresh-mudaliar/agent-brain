"""Unit and integration tests for sync.watcher (Outbox Queue, File Watcher, Batch Reconciler)."""

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from core.database import Database
from extractors.ast_parser import ASTParser
from sync.watcher import (
    CodebaseEventHandler,
    CodebaseWatcher,
    OutboxQueue,
    OutboxWorker,
)


class TestOutboxQueue(unittest.TestCase):
    """Test suite for OutboxQueue debouncing and deduplication."""

    def test_push_and_deduplication(self):
        queue = OutboxQueue()
        queue.push("src/main.py", operation="UPDATE")
        queue.push("src/main.py", operation="UPDATE")
        self.assertEqual(queue.size(), 1)

        # Before debounce duration, batch is empty
        updates, deletes = queue.pop_ready_batch(debounce_seconds=10.0)
        self.assertEqual(updates, [])
        self.assertEqual(deletes, [])
        self.assertEqual(queue.size(), 1)

        # Ready after zero-delay debounce
        updates, deletes = queue.pop_ready_batch(debounce_seconds=0.0)
        self.assertEqual(updates, ["src/main.py"])
        self.assertEqual(deletes, [])
        self.assertEqual(queue.size(), 0)

    def test_delete_and_update_state_transitions(self):
        queue = OutboxQueue()
        queue.push("src/old.py", operation="UPDATE")
        queue.push("src/old.py", operation="DELETE")
        self.assertEqual(queue.size(), 1)

        updates, deletes = queue.pop_ready_batch(debounce_seconds=0.0)
        self.assertEqual(updates, [])
        self.assertEqual(deletes, ["src/old.py"])


class TestCodebaseEventHandler(unittest.TestCase):
    """Test suite for CodebaseEventHandler path filtering and exclusions."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.outbox = OutboxQueue()
        self.handler = CodebaseEventHandler(self.outbox, root_dir=self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_filters_excluded_directories(self):
        # .git or node_modules paths should be rejected
        self.assertFalse(self.handler._should_process(os.path.join(self.temp_dir, ".git", "HEAD")))
        self.assertFalse(self.handler._should_process(os.path.join(self.temp_dir, "node_modules", "pkg", "index.js")))
        self.assertFalse(self.handler._should_process(os.path.join(self.temp_dir, ".agent-memory", "brain.db")))

    def test_filters_unsupported_extensions(self):
        self.assertFalse(self.handler._should_process(os.path.join(self.temp_dir, "README.md")))
        self.assertFalse(self.handler._should_process(os.path.join(self.temp_dir, "data.json")))
        self.assertFalse(self.handler._should_process(os.path.join(self.temp_dir, "image.png")))

    def test_accepts_supported_extensions(self):
        self.assertTrue(self.handler._should_process(os.path.join(self.temp_dir, "service.py")))
        self.assertTrue(self.handler._should_process(os.path.join(self.temp_dir, "utils.ts")))
        self.assertTrue(self.handler._should_process(os.path.join(self.temp_dir, "main.rs")))
        self.assertTrue(self.handler._should_process(os.path.join(self.temp_dir, "handler.go")))


class TestBatchReconciliation(unittest.TestCase):
    """Test suite for incremental batch purge and re-insertion into KùzuDB."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db = Database(db_path=":memory:")
        self.db.connect()
        self.parser = ASTParser()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.db.close()

    def test_purge_and_reconcile_cycle(self):
        test_file = Path(self.temp_dir) / "app.py"
        test_file.write_text("def old_function(): pass\n", encoding="utf-8")

        # 1. Initial parse and save
        initial_res = self.parser.parse_file(test_file, root_dir=self.temp_dir)
        stats = self.parser.save_to_graph([initial_res], db=self.db)
        self.assertEqual(stats["functions"], 1)

        # Verify old_function exists in graph
        res = self.db.execute_query("MATCH (fn:Function {name: 'old_function'}) RETURN fn.name")
        self.assertEqual(len(res.result_set), 1)

        # 2. Modify file: replace old_function with new_function
        test_file.write_text("def new_function(): pass\n", encoding="utf-8")
        updated_res = self.parser.parse_file(test_file, root_dir=self.temp_dir)

        # 3. Reconcile batch: should atomically delete old_function and insert new_function
        reconcile_stats = self.parser.reconcile_batch_in_graph(
            deleted_paths=[],
            parse_results=[updated_res],
            db=self.db,
        )
        self.assertEqual(reconcile_stats["purged_files"], 1)
        self.assertEqual(reconcile_stats["functions"], 1)

        # Verify old_function was purged (0 ghost functions)
        old_res = self.db.execute_query("MATCH (fn:Function {name: 'old_function'}) RETURN fn.name")
        self.assertEqual(len(old_res.result_set), 0)

        # Verify new_function exists
        new_res = self.db.execute_query("MATCH (fn:Function {name: 'new_function'}) RETURN fn.name")
        self.assertEqual(len(new_res.result_set), 1)

    def test_delete_reconciliation(self):
        test_file = Path(self.temp_dir) / "delete_me.py"
        test_file.write_text("def delete_candidate(): pass\n", encoding="utf-8")

        initial_res = self.parser.parse_file(test_file, root_dir=self.temp_dir)
        self.parser.save_to_graph([initial_res], db=self.db)

        # Verify function exists
        res = self.db.execute_query("MATCH (fn:Function {name: 'delete_candidate'}) RETURN fn.name")
        self.assertEqual(len(res.result_set), 1)

        # Reconcile deletion
        self.parser.reconcile_batch_in_graph(
            deleted_paths=["delete_me.py"],
            parse_results=[],
            db=self.db,
        )

        # Function and CodeFile should be gone
        res_after = self.db.execute_query("MATCH (fn:Function {name: 'delete_candidate'}) RETURN fn.name")
        self.assertEqual(len(res_after.result_set), 0)

        file_after = self.db.execute_query("MATCH (f:CodeFile {path: 'delete_me.py'}) RETURN f.path")
        self.assertEqual(len(file_after.result_set), 0)


class TestCodebaseWatcherIntegration(unittest.TestCase):
    """Integration test verifying end-to-end watcher operations with debounce."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_dir = Path(self.temp_dir) / ".agent-memory"
        self.db_path = str(self.db_dir / "brain.db")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_watcher_lifecycle_and_callback(self):
        reconciled_events = []

        def on_reconcile(stats):
            reconciled_events.append(stats)

        watcher = CodebaseWatcher(
            target_dir=self.temp_dir,
            db_path=self.db_path,
            debounce_seconds=0.2,
            on_reconcile=on_reconcile,
        )

        with watcher:
            # Create a code file inside watched dir
            code_file = Path(self.temp_dir) / "worker.py"
            code_file.write_text("def execute_task(): pass\n", encoding="utf-8")

            # Wait for debounce and processing
            time.sleep(1.2)

            self.assertGreaterEqual(len(reconciled_events), 1)
            last_stats = reconciled_events[-1]
            self.assertEqual(last_stats.get("files", 0), 1)

            # Check database directly via the active connection
            res = watcher.db.execute_query("MATCH (fn:Function {name: 'execute_task'}) RETURN fn.name")
            self.assertEqual(len(res.result_set), 1)


if __name__ == "__main__":
    unittest.main()
