"""Background outbox file watcher for real-time incremental code graph indexing.

Provides low-memory debounced file watching backed by watchdog, an in-memory outbox
queue, and atomic batch reconciliation into KùzuDB without full codebase scans.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from core.database import Database, get_db
from extractors.ast_parser import (
    DEFAULT_EXCLUDED_DIRS,
    SUPPORTED_EXTENSIONS,
    ASTParser,
    FileParseResult,
)

logger = logging.getLogger("agent_brain.watcher")


class OutboxQueue:
    """Thread-safe deduplicated queue for filesystem change events."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Mapping: normalized_path -> (operation: "UPDATE"|"DELETE", last_seen_timestamp)
        self._items: Dict[str, Tuple[str, float]] = {}

    def push(self, file_path: str | Path, operation: str = "UPDATE") -> None:
        """Push a file path with an operation and update its debounce timestamp.

        Args:
            file_path: Relative or absolute file path.
            operation: Operation type ("UPDATE" or "DELETE").
        """
        path_str = str(file_path).replace("\\", "/")
        with self._lock:
            # If previously queued for UPDATE and now DELETED, change to DELETE
            # If previously queued for DELETE and now UPDATED (re-created), change to UPDATE
            self._items[path_str] = (operation, time.time())

    def pop_ready_batch(
        self, debounce_seconds: float = 1.0
    ) -> Tuple[List[str], List[str]]:
        """Drain entries that have settled past the debounce window.

        Args:
            debounce_seconds: Minimum idle time since the last event before processing.

        Returns:
            Tuple of (update_paths, delete_paths).
        """
        now = time.time()
        updates: List[str] = []
        deletes: List[str] = []

        with self._lock:
            ready_keys = [
                path
                for path, (op, ts) in self._items.items()
                if (now - ts) >= debounce_seconds
            ]
            for key in ready_keys:
                op, _ = self._items.pop(key)
                if op == "DELETE":
                    deletes.append(key)
                else:
                    updates.append(key)

        return updates, deletes

    def size(self) -> int:
        """Return total pending items in the outbox."""
        with self._lock:
            return len(self._items)

    def clear(self) -> None:
        """Clear all pending items in the outbox."""
        with self._lock:
            self._items.clear()


class CodebaseEventHandler(FileSystemEventHandler):
    """Watchdog event handler that filters code files and queues them into an OutboxQueue."""

    def __init__(
        self,
        outbox: OutboxQueue,
        root_dir: str | Path,
        excluded_dirs: Optional[Set[str]] = None,
        supported_extensions: Optional[Set[str]] = None,
    ) -> None:
        super().__init__()
        self.outbox = outbox
        self.root_dir = Path(root_dir).resolve()
        self.excluded_dirs = (
            excluded_dirs if excluded_dirs is not None else DEFAULT_EXCLUDED_DIRS
        )
        self.supported_extensions = (
            supported_extensions
            if supported_extensions is not None
            else set(SUPPORTED_EXTENSIONS.keys())
        )

    def _should_process(self, file_path_str: str) -> bool:
        """Determine if a file event should be processed based on exclusions and extensions."""
        p = Path(file_path_str)
        # Check extensions
        if p.suffix.lower() not in self.supported_extensions:
            return False

        # Check path parts against excluded directories
        for part in p.parts:
            if part in self.excluded_dirs:
                return False

        # Skip temporary swap or hidden editor files
        if p.name.startswith((".", "~")) or p.name.endswith(".tmp"):
            return False

        return True

    def _get_tracked_path(self, raw_path: str) -> str:
        """Resolve path relative to root_dir if inside it, normalized to forward slashes."""
        abs_p = Path(raw_path).resolve()
        try:
            rel = abs_p.relative_to(self.root_dir)
            return str(rel).replace("\\", "/")
        except ValueError:
            return str(abs_p).replace("\\", "/")

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._should_process(event.src_path):
            tracked = self._get_tracked_path(event.src_path)
            self.outbox.push(tracked, operation="UPDATE")

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._should_process(event.src_path):
            tracked = self._get_tracked_path(event.src_path)
            self.outbox.push(tracked, operation="UPDATE")

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._should_process(event.src_path):
            tracked = self._get_tracked_path(event.src_path)
            self.outbox.push(tracked, operation="DELETE")

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            if self._should_process(event.src_path):
                old_tracked = self._get_tracked_path(event.src_path)
                self.outbox.push(old_tracked, operation="DELETE")
            if hasattr(event, "dest_path") and self._should_process(event.dest_path):
                new_tracked = self._get_tracked_path(event.dest_path)
                self.outbox.push(new_tracked, operation="UPDATE")


class OutboxWorker(threading.Thread):
    """Background daemon worker that periodically drains the OutboxQueue and updates KùzuDB."""

    def __init__(
        self,
        outbox: OutboxQueue,
        root_dir: str | Path,
        db: Optional[Database] = None,
        parser: Optional[ASTParser] = None,
        debounce_seconds: float = 1.0,
        poll_interval: float = 0.25,
        max_workers: Optional[int] = None,
        on_reconcile: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        super().__init__(name="agent-brain-outbox-worker", daemon=True)
        self.outbox = outbox
        self.root_dir = Path(root_dir).resolve()
        self.db = db
        self.parser = parser or ASTParser()
        self.debounce_seconds = debounce_seconds
        self.poll_interval = poll_interval
        self.max_workers = max_workers
        self.on_reconcile = on_reconcile
        self._running = False
        self._stop_event = threading.Event()

    def run(self) -> None:
        self._running = True
        logger.info(
            f"Outbox worker started for '{self.root_dir}' (debounce: {self.debounce_seconds}s)."
        )

        while not self._stop_event.is_set():
            try:
                updates, deletes = self.outbox.pop_ready_batch(
                    debounce_seconds=self.debounce_seconds
                )
                if updates or deletes:
                    self._process_batch(updates, deletes)
            except Exception as err:
                logger.error(f"Error in outbox reconcile loop: {err}", exc_info=True)

            self._stop_event.wait(timeout=self.poll_interval)

        logger.info("Outbox worker stopped.")

    def _process_batch(self, updates: List[str], deletes: List[str]) -> None:
        """Parse updated files and commit reconciliation to KùzuDB."""
        start_time = time.time()
        logger.info(
            f"Processing outbox batch: {len(updates)} update(s), {len(deletes)} delete(s)."
        )

        # 1. Parse AST outside the database lock using thread pool
        parse_results: List[FileParseResult] = []
        existing_updates: List[str] = []

        for rel_path in updates:
            full_path = self.root_dir / rel_path
            if full_path.exists() and full_path.is_file():
                existing_updates.append(rel_path)
            else:
                # File was removed before parse
                deletes.append(rel_path)

        if existing_updates:
            num_workers = min(len(existing_updates), self.max_workers or 4)
            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                futures = {
                    executor.submit(
                        self.parser.parse_file,
                        self.root_dir / p,
                        root_dir=self.root_dir,
                    ): p
                    for p in existing_updates
                }
                for f in futures:
                    try:
                        res = f.result()
                        parse_results.append(res)
                    except Exception as parse_err:
                        logger.error(
                            f"Error parsing '{futures[f]}': {parse_err}", exc_info=True
                        )

        # 2. Acquire database connection and execute atomic batch reconcile
        database = self.db or get_db()
        stats = self.parser.reconcile_batch_in_graph(
            deleted_paths=deletes,
            parse_results=parse_results,
            db=database,
        )

        duration_ms = (time.time() - start_time) * 1000
        stats["duration_ms"] = round(duration_ms, 2)
        stats["updates_count"] = len(existing_updates)
        stats["deletes_count"] = len(deletes)

        logger.info(
            f"Batch reconciled in {duration_ms:.1f}ms: "
            f"files={stats.get('files', 0)}, purged={stats.get('purged_files', 0)}, "
            f"calls={stats.get('calls_edges', 0)}"
        )

        if self.on_reconcile:
            try:
                self.on_reconcile(stats)
            except Exception as cb_err:
                logger.debug(f"Reconcile callback notice: {cb_err}")

    def stop(self) -> None:
        """Signal the worker to stop."""
        self._running = False
        self._stop_event.set()


class CodebaseWatcher:
    """Manager for watching codebase changes and triggering low-memory incremental indexing."""

    def __init__(
        self,
        target_dir: str | Path,
        db_path: Optional[str] = None,
        debounce_seconds: float = 1.0,
        max_workers: Optional[int] = None,
        on_reconcile: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.target_dir = Path(target_dir).resolve()
        self.db_path = db_path
        self.debounce_seconds = debounce_seconds
        self.max_workers = max_workers
        self.on_reconcile = on_reconcile

        self.outbox = OutboxQueue()
        self.db: Optional[Database] = None
        self.worker: Optional[OutboxWorker] = None
        self.observer: Optional[Observer] = None
        self._is_active = False

    def start(self) -> None:
        """Start the watchdog observer and the outbox background worker."""
        if self._is_active:
            return

        self.db = get_db(db_path=self.db_path) if self.db_path else get_db()
        self.db.connect()

        self.worker = OutboxWorker(
            outbox=self.outbox,
            root_dir=self.target_dir,
            db=self.db,
            debounce_seconds=self.debounce_seconds,
            max_workers=self.max_workers,
            on_reconcile=self.on_reconcile,
        )
        self.worker.start()

        event_handler = CodebaseEventHandler(
            outbox=self.outbox,
            root_dir=self.target_dir,
        )

        self.observer = Observer()
        self.observer.schedule(event_handler, str(self.target_dir), recursive=True)
        self.observer.start()
        self._is_active = True

        logger.info(f"CodebaseWatcher actively monitoring '{self.target_dir}'.")

    def stop(self) -> None:
        """Stop the watcher observer and background worker."""
        if not self._is_active:
            return

        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=2.0)
            self.observer = None

        if self.worker:
            self.worker.stop()
            self.worker.join(timeout=2.0)
            self.worker = None

        if self.db:
            try:
                self.db.close()
            except Exception:
                pass
            self.db = None

        self._is_active = False
        logger.info(f"CodebaseWatcher stopped for '{self.target_dir}'.")

    def __enter__(self) -> CodebaseWatcher:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()
