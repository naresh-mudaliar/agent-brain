"""Database management module for agent-brain using embedded Kùzu graph engine.

Provides connection management, schema initialization, and openCypher query execution
on an embedded Kùzu graph database stored at ./.agent-memory/brain.db (or in-memory).

Kùzu runs natively on Windows, Linux, and macOS without external services, Docker,
or background daemons, delivering sub-millisecond graph query latency.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import kuzu

logger = logging.getLogger("agent_brain.database")


def resolve_default_db_path() -> str:
    """Resolve the default brain.db path with hierarchical workspace discovery."""
    env_path = os.environ.get("AGENT_MEMORY_DB_PATH") or os.environ.get("AGENT_MEMORY_PATH")
    if env_path:
        return env_path

    # 1. Direct cwd check
    cwd_db = Path.cwd() / ".agent-memory" / "brain.db"
    if cwd_db.exists():
        return str(cwd_db)

    # 2. Search upward from cwd (for nested subdirectories in workspace)
    try:
        cur = Path.cwd().resolve()
        for _ in range(6):
            cand = cur / ".agent-memory" / "brain.db"
            if cand.exists():
                return str(cand)
            if cur.parent == cur:
                break
            cur = cur.parent
    except Exception:
        pass

    # 3. Check repo root relative to this module
    try:
        repo_root = Path(__file__).resolve().parent.parent
        repo_db = repo_root / ".agent-memory" / "brain.db"
        if repo_db.exists():
            return str(repo_db)
    except Exception:
        pass

    return "./.agent-memory/brain.db"


DEFAULT_DB_PATH = resolve_default_db_path()
DEFAULT_GRAPH_NAME = "CompanyBrain"


class AgentMemoryDBError(Exception):
    """Base exception for agent-brain database operations."""




class DatabaseConnectionError(AgentMemoryDBError):
    """Raised when connecting to Kùzu fails."""


class DatabaseQueryError(AgentMemoryDBError):
    """Raised when query execution on Kùzu fails."""


class QueryResult:
    """Represents a Cypher query execution result with header and result_set."""

    def __init__(self, header: List[str], result_set: List[List[Any]]) -> None:
        self.header = header
        self.result_set = result_set

    def __repr__(self) -> str:
        return f"QueryResult(header={self.header}, rows={len(self.result_set)})"


class Database:
    """Embedded Kùzu graph database manager for agent-brain."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        graph_name: str = DEFAULT_GRAPH_NAME,
        **kwargs: Any,
    ) -> None:
        self.db_path = db_path or resolve_default_db_path()
        self.graph_name = graph_name
        self._db: Optional[kuzu.Database] = None
        self._conn: Optional[kuzu.Connection] = None
        self._lock = threading.Lock()

    def connect(self) -> None:
        """Initialize Kùzu database and connection, establishing the graph schema."""
        if self._conn is not None:
            return

        try:
            if self.db_path != ":memory:":
                # If a legacy SQLite file exists from an earlier version, remove it so Kuzu can initialize
                if os.path.isfile(self.db_path):
                    try:
                        with open(self.db_path, "rb") as f:
                            header = f.read(16)
                        if header.startswith(b"SQLite format 3"):
                            logger.info(f"Detected legacy SQLite file at '{self.db_path}'. Replacing with Kùzu database.")
                            os.remove(self.db_path)
                    except Exception as clean_err:
                        logger.warning(f"Could not inspect legacy database file: {clean_err}")

                db_dir = Path(self.db_path).parent
                db_dir.mkdir(parents=True, exist_ok=True)

            logger.info(f"Connecting to embedded Kùzu graph engine at '{self.db_path}'...")
            self._db = kuzu.Database(self.db_path)
            self._conn = kuzu.Connection(self._db)
            self._init_schema()
            logger.info(f"Connected to Kùzu graph engine at '{self.db_path}' successfully.")

        except Exception as err:
            err_msg = f"Failed to connect to Kùzu database at '{self.db_path}': {err}"
            logger.error(err_msg, exc_info=True)
            raise DatabaseConnectionError(err_msg) from err

    def _init_schema(self) -> None:
        """Create standard Node and Rel tables for codebase symbols if they do not exist."""
        if self._conn is None:
            return

        ddl_statements = [
            """
            CREATE NODE TABLE IF NOT EXISTS CodeFile (
                path STRING,
                filename STRING,
                language STRING,
                has_syntax_error BOOLEAN,
                PRIMARY KEY (path)
            );
            """,
            """
            CREATE NODE TABLE IF NOT EXISTS Class (
                id STRING,
                name STRING,
                qualified_name STRING,
                path STRING,
                docstring STRING,
                line_number INT64,
                end_line_number INT64,
                PRIMARY KEY (id)
            );
            """,
            """
            CREATE NODE TABLE IF NOT EXISTS Function (
                id STRING,
                name STRING,
                qualified_name STRING,
                path STRING,
                is_method BOOLEAN,
                is_async BOOLEAN,
                docstring STRING,
                line_number INT64,
                end_line_number INT64,
                PRIMARY KEY (id)
            );
            """,
            """
            CREATE REL TABLE IF NOT EXISTS CONTAINS (
                FROM CodeFile TO Class,
                FROM CodeFile TO Function,
                FROM Class TO Class,
                FROM Class TO Function,
                FROM Function TO Function
            );
            """,
            """
            CREATE REL TABLE IF NOT EXISTS CALLS (
                FROM Function TO Function,
                FROM Function TO Class
            );
            """,
        ]

        for ddl in ddl_statements:
            try:
                self._conn.execute(ddl.strip())
            except Exception as err:
                logger.debug(f"DDL statement notice: {err}")

    @property
    def connection(self) -> kuzu.Connection:
        """Return the active Kùzu connection."""
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

    @property
    def graph(self) -> Database:
        """Compatibility property for graph query chaining."""
        return self

    def query(
        self,
        query: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> QueryResult:
        """Compatibility alias for execute_query."""
        return self.execute_query(query, params)

    def execute_query(
        self,
        query: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> QueryResult:
        """Execute an openCypher query against the Kùzu graph database.

        Args:
            query: Cypher query string to execute.
            params: Optional dictionary of query parameters.

        Returns:
            QueryResult containing .header (column names) and .result_set (rows).

        Raises:
            DatabaseQueryError: If query execution fails.
            ValueError: If query is empty.
        """
        if not query or not query.strip():
            raise ValueError("Query string cannot be empty.")

        try:
            with self._lock:
                conn = self.connection
                logger.debug(f"Executing Cypher query: {query.strip()} (params: {params})")

                if params:
                    raw_result = conn.execute(query.strip(), params)
                else:
                    raw_result = conn.execute(query.strip())

                header = [str(col) for col in raw_result.get_column_names()]
                result_set: List[List[Any]] = []
                while raw_result.has_next():
                    result_set.append(raw_result.get_next())

                return QueryResult(header=header, result_set=result_set)

        except Exception as err:
            err_msg = f"Error executing query '{query}': {err}"
            logger.error(err_msg, exc_info=True)
            raise DatabaseQueryError(err_msg) from err

    def close(self) -> None:
        """Close connection and clean up resources."""
        try:
            self._conn = None
            self._db = None
            logger.info(f"Closed database connection for '{self.db_path}'.")
        except Exception as err:
            logger.warning(f"Error while closing database: {err}")

    def __enter__(self) -> Database:
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


# Module-level singleton instance for convenient plug-and-pack usage
_default_instance: Optional[Database] = None


def get_db(
    db_path: str = DEFAULT_DB_PATH,
    graph_name: str = DEFAULT_GRAPH_NAME,
) -> Database:
    """Get or initialize the default Database instance."""
    global _default_instance
    if _default_instance is None or _default_instance.db_path != db_path:
        _default_instance = Database(db_path=db_path, graph_name=graph_name)
    return _default_instance


def execute_query(
    query: str,
    params: Optional[Dict[str, Any]] = None,
    db_path: str = DEFAULT_DB_PATH,
    graph_name: str = DEFAULT_GRAPH_NAME,
) -> QueryResult:
    """Convenience function to execute a Cypher query using the default database instance."""
    db = get_db(db_path=db_path, graph_name=graph_name)
    return db.execute_query(query, params=params)


# Alias for backwards compatibility
AgentMemoryDB = Database

__all__ = [
    "Database",
    "AgentMemoryDB",
    "QueryResult",
    "get_db",
    "execute_query",
    "AgentMemoryDBError",
    "DatabaseConnectionError",
    "DatabaseQueryError",
    "resolve_default_db_path",
    "DEFAULT_DB_PATH",
    "DEFAULT_GRAPH_NAME",
]
