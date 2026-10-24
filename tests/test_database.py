"""Unit tests for core.database using embedded Kùzu engine."""

import concurrent.futures
import unittest

from core.database import (
    DEFAULT_DB_PATH,
    DEFAULT_GRAPH_NAME,
    Database,
    DatabaseConnectionError,
    DatabaseQueryError,
    QueryResult,
    execute_query,
    get_db,
)


class TestDatabase(unittest.TestCase):
    """Test suite for Kùzu Database class."""

    def test_in_memory_initialization_and_schema(self):
        """Test successful in-memory Kùzu connection and schema creation."""
        with Database(db_path=":memory:") as db:
            self.assertEqual(db.graph_name, DEFAULT_GRAPH_NAME)
            # Verify nodes can be merged and queried
            res = db.execute_query(
                "MERGE (cf:CodeFile {path: $path}) SET cf.filename = $filename, cf.language = 'python', cf.has_syntax_error = false",
                {"path": "test.py", "filename": "test.py"},
            )
            self.assertIsInstance(res, QueryResult)

            query_res = db.execute_query("MATCH (cf:CodeFile) RETURN cf.path, cf.filename")
            self.assertEqual(query_res.header, ["cf.path", "cf.filename"])
            self.assertEqual(query_res.result_set, [["test.py", "test.py"]])

    def test_empty_query_raises_value_error(self):
        """Test that an empty query raises ValueError."""
        with Database(db_path=":memory:") as db:
            with self.assertRaises(ValueError):
                db.execute_query("")
            with self.assertRaises(ValueError):
                db.execute_query("   ")

    def test_query_syntax_error_raises_database_query_error(self):
        """Test that invalid Cypher syntax raises DatabaseQueryError."""
        with Database(db_path=":memory:") as db:
            with self.assertRaises(DatabaseQueryError):
                db.execute_query("INVALID QUERY NOT CYPHER")

    def test_context_manager_and_close(self):
        """Test connection opening and closing through context manager."""
        db = Database(db_path=":memory:")
        db.connect()
        self.assertIsNotNone(db._conn)
        db.close()
        self.assertIsNone(db._conn)

    def test_query_result_representation(self):
        """Test QueryResult representation string."""
        qr = QueryResult(header=["a", "b"], result_set=[[1, 2], [3, 4]])
        self.assertIn("rows=2", repr(qr))

    def test_concurrent_queries_thread_safety(self):
        """Test thread-safe query execution across multiple concurrent worker threads."""
        with Database(db_path=":memory:") as db:
            db.execute_query(
                "MERGE (cf:CodeFile {path: 'shared.py'}) SET cf.filename = 'shared.py', cf.language = 'python', cf.has_syntax_error = false"
            )

            def worker_query(i: int):
                res = db.execute_query("MATCH (cf:CodeFile) RETURN cf.filename")
                return len(res.result_set)

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(worker_query, i) for i in range(20)]
                results = [f.result() for f in concurrent.futures.as_completed(futures)]

            self.assertEqual(len(results), 20)
            self.assertTrue(all(r == 1 for r in results))


if __name__ == "__main__":
    unittest.main()
