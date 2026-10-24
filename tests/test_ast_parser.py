"""Unit tests for extractors.ast_parser."""

import sys
import unittest
from unittest.mock import MagicMock, call

from extractors.ast_parser import ASTParser, CodeVisitor, FileParseResult


class TestASTParser(unittest.TestCase):
    """Test suite for ASTParser and CodeVisitor."""

    def setUp(self):
        self.parser = ASTParser()

    def test_extract_classes_and_functions(self):
        """Test extraction of class, methods, functions, and docstrings."""
        code = '''
"""Module docstring."""

class Engine:
    """Represents a car engine."""

    def start(self):
        """Start the engine."""
        self.ignite()

    def ignite(self):
        pass


def run_car():
    eng = Engine()
    eng.start()
'''
        result = self.parser.parse_source(code, file_path="car.py", filename="car.py")

        self.assertFalse(result.has_syntax_error)
        self.assertEqual(len(result.classes), 1)
        self.assertEqual(result.classes[0].name, "Engine")
        self.assertEqual(result.classes[0].qualified_name, "Engine")
        self.assertEqual(result.classes[0].docstring, "Represents a car engine.")

        # Check functions
        # start, ignite, run_car
        func_names = [f.name for f in result.functions]
        self.assertIn("start", func_names)
        self.assertIn("ignite", func_names)
        self.assertIn("run_car", func_names)

        # Check methods
        start_fn = next(f for f in result.functions if f.name == "start")
        self.assertTrue(start_fn.is_method)
        self.assertEqual(start_fn.parent_type, "class")
        self.assertEqual(start_fn.parent_id, "car.py:Engine")

        run_car_fn = next(f for f in result.functions if f.name == "run_car")
        self.assertFalse(run_car_fn.is_method)
        self.assertEqual(run_car_fn.parent_type, "file")

    def test_extract_calls(self):
        """Test extraction of call relationships."""
        code = """
def worker():
    setup()
    do_work()

def main():
    worker()
"""
        result = self.parser.parse_source(code, file_path="app.py", filename="app.py")
        self.assertFalse(result.has_syntax_error)

        callees = [c.callee_name for c in result.calls]
        self.assertIn("setup", callees)
        self.assertIn("do_work", callees)
        self.assertIn("worker", callees)

        # Check caller mapping
        worker_calls = [c for c in result.calls if c.caller_id == "app.py:worker"]
        self.assertEqual(len(worker_calls), 2)
        worker_callees = {c.callee_name for c in worker_calls}
        self.assertEqual(worker_callees, {"setup", "do_work"})

    def test_graceful_syntax_error_handling(self):
        """Test that syntax errors do not raise exceptions and are flagged gracefully."""
        bad_code = """
def broken_syntax(
    print("missing closing paren"
"""
        result = self.parser.parse_source(
            bad_code, file_path="broken.py", filename="broken.py"
        )

        self.assertTrue(result.has_syntax_error)
        self.assertIsNotNone(result.error_message)
        self.assertIn("Syntax error", result.error_message)
        self.assertEqual(len(result.classes), 0)
        self.assertEqual(len(result.functions), 0)

    def test_save_to_graph_creates_nodes_and_edges(self):
        """Test that save_to_graph executes MERGE for files, classes, functions, CONTAINS, and CALLS."""
        code = """
class Service:
    def execute(self):
        helper()

def helper():
    pass
"""
        parse_result = self.parser.parse_source(
            code, file_path="srv.py", filename="srv.py"
        )

        mock_db = MagicMock()
        stats = self.parser.save_to_graph([parse_result], db=mock_db)

        self.assertEqual(stats["files"], 1)
        self.assertEqual(stats["classes"], 1)
        self.assertEqual(stats["functions"], 2)
        self.assertGreaterEqual(stats["contains_edges"], 2)
        self.assertGreaterEqual(stats["calls_edges"], 1)

        # Verify that execute_query was called multiple times for nodes and relationships
        self.assertTrue(mock_db.execute_query.called)
        query_texts = [call_args[0][0] for call_args in mock_db.execute_query.call_args_list]

        # Check for node queries
        self.assertTrue(any("MERGE (f:CodeFile" in q for q in query_texts))
        self.assertTrue(any("MERGE (c:Class" in q for q in query_texts))
        self.assertTrue(any("MERGE (fn:Function" in q for q in query_texts))

        # Check for relationship queries
        self.assertTrue(any("[:CONTAINS]" in q for q in query_texts))
        self.assertTrue(any("[:CALLS]" in q for q in query_texts))

    def test_parse_rust(self):
        """Test Rust struct, trait, impl, and fn extraction."""
        code = """
pub struct CandidatePipeline {
    name: String,
}

impl CandidatePipeline {
    pub async fn execute(&self) {
        self.fetch_items();
    }
}

fn fetch_items() {}
"""
        res = self.parser.parse_source(code, file_path="pipeline.rs", filename="pipeline.rs")
        self.assertEqual(res.language, "rust")
        self.assertEqual(len(res.classes), 1)
        self.assertEqual(res.classes[0].name, "CandidatePipeline")
        fn_names = [f.name for f in res.functions]
        self.assertIn("execute", fn_names)
        self.assertIn("fetch_items", fn_names)

    def test_parse_typescript(self):
        """Test TypeScript class, interface, arrow function, and call extraction."""
        code = """
export interface User {
    id: string;
}

export class UserService {
    async getUser(id: string) {
        logUser(id);
    }
}

const logUser = (id: string) => console.log(id);
"""
        res = self.parser.parse_source(code, file_path="user.ts", filename="user.ts")
        self.assertEqual(res.language, "typescript")
        self.assertGreaterEqual(len(res.classes), 2)
        fn_names = [f.name for f in res.functions]
        self.assertIn("getUser", fn_names)
        self.assertIn("logUser", fn_names)

    def test_parse_go(self):
        """Test Go struct and method with receiver extraction."""
        code = """
package main

type Server struct {
    port int
}

func (s *Server) Start() {
    initRoutes()
}

func initRoutes() {}
"""
        res = self.parser.parse_source(code, file_path="main.go", filename="main.go")
        self.assertEqual(res.language, "go")
        self.assertEqual(len(res.classes), 1)
        self.assertEqual(res.classes[0].name, "Server")
        fn_names = [f.name for f in res.functions]
        self.assertIn("Start", fn_names)
        self.assertIn("initRoutes", fn_names)

    def test_parse_scala(self):
        """Test Scala class, object, and def extraction."""
        code = """
case class Post(id: Long)

object PostService {
    def rankPosts(): Unit = {
        computeScore()
    }
}
"""
        res = self.parser.parse_source(code, file_path="post.scala", filename="post.scala")
        self.assertEqual(res.language, "scala")
        self.assertGreaterEqual(len(res.classes), 2)
        fn_names = [f.name for f in res.functions]
        self.assertIn("rankPosts", fn_names)

    def test_parse_sql(self):
        """Test SQL CREATE TABLE, VIEW, and dependency extraction."""
        code = """
CREATE TABLE users (
    id INT PRIMARY KEY,
    name VARCHAR(100)
);

CREATE VIEW active_users AS
SELECT * FROM users;
"""
        res = self.parser.parse_source(code, file_path="schema.sql", filename="schema.sql")
        self.assertEqual(res.language, "sql")
        class_names = [c.name for c in res.classes]
        self.assertIn("users", class_names)
        self.assertIn("active_users", class_names)

    def test_parse_pyspark(self):
        """Test PySpark file language tagging."""
        code = """
from pyspark.sql import SparkSession

def process_data(spark: SparkSession):
    return spark.read.parquet("data.parquet")
"""
        res = self.parser.parse_source(code, file_path="job.py", filename="job.py")
        self.assertEqual(res.language, "pyspark")


if __name__ == "__main__":
    unittest.main()
