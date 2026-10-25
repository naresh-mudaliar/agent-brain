"""CLI entrypoint for agent-brain using Click.

Commands:
  agent-brain init --path <dir> : Scans directory using AST parser and populates Kùzu graph.
  agent-brain register          : Auto-detects and registers MCP server with AI agents.
  agent-brain export            : Compresses and encrypts local database into 'memory-seed.enc'.
  agent-brain pull              : Decrypts and restores the database from 'memory-seed.enc'.
  agent-brain benchmark         : Runs token efficiency & indexing performance benchmark.
  agent-brain track             : Tracks Antigravity IDE agent token usage and benchmark metrics.
  agent-brain mcp               : Starts the MCP stdio server for local AI agents.
  agent-brain watch             : Runs background outbox file watcher for real-time incremental graph sync.
  agent-brain generate-key      : Generates a new Fernet AES encryption key.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

# Ensure UTF-8 output encoding across all Windows terminals (CMD, PowerShell)
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import click

from core.database import (
    DEFAULT_DB_PATH,
    DEFAULT_GRAPH_NAME,
    Database,
    DatabaseConnectionError,
    DatabaseQueryError,
)
from core.integrations import register_all_clients, register_client_mcp
from extractors.ast_parser import ASTParser
from sync.s3_sync import (
    DEFAULT_ARCHIVE_FILE,
    DEFAULT_SOURCE_DIR,
    ENV_SECRET_KEY,
    DecryptionError,
    EncryptionError,
    export_memory_archive,
    generate_encryption_key,
    import_memory_archive,
)


@click.group(name="agent-brain")
@click.version_option(version="0.2.0", prog_name="agent-brain")
def cli() -> None:
    """agent-brain: Plug-and-pack graph memory for AI agents backed by KùzuDB."""
    pass


@cli.command("init")
@click.option(
    "--path",
    "-p",
    required=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, readable=True),
    help="Target codebase directory to scan and index into KùzuDB.",
)
@click.option(
    "--db-path",
    default=None,
    help="Path to embedded KùzuDB database file (default: <path>/.agent-memory/brain.db).",
)
@click.option(
    "--graph-name",
    default=DEFAULT_GRAPH_NAME,
    show_default=True,
    help="Name of the target KùzuDB graph.",
)
@click.option(
    "--workers",
    "-w",
    default=None,
    type=int,
    help="Number of parallel worker threads for file parsing (default: auto CPU-based).",
)
@click.option(
    "--connect",
    "-c",
    type=click.Choice(["all", "antigravity", "claude", "cursor"], case_sensitive=False),
    default=None,
    help="Auto-connect indexed memory to an AI agent client upon completion.",
)
@click.option(
    "--watch/--no-watch",
    default=True,
    show_default=True,
    help="Enable automatic background file watching whenever the connected AI agent runs.",
)
def init_cmd(
    path: str,
    db_path: Optional[str],
    graph_name: str,
    workers: Optional[int] = None,
    connect: Optional[str] = None,
    watch: bool = True,
) -> None:
    """Scan the codebase directory using multi-language parallel AST parsers and populate Kùzu."""
    target_dir = Path(path).resolve()
    actual_db = db_path or str(target_dir / ".agent-memory" / "brain.db")
    click.secho(
        f"\n[INFO] Scanning codebase in parallel at: {target_dir}",
        fg="cyan",
        bold=True,
    )

    parser = ASTParser()
    results = parser.scan_directory(target_dir, max_workers=workers)

    total_files = len(results)
    syntax_errors = sum(1 for r in results if r.has_syntax_error)
    total_classes = sum(len(r.classes) for r in results)
    total_functions = sum(len(r.functions) for r in results)
    total_calls = sum(len(r.calls) for r in results)

    # Group by language
    lang_counts: dict[str, int] = {}
    for r in results:
        lang_counts[r.language] = lang_counts.get(r.language, 0) + 1
    lang_summary = ", ".join(f"{k}: {v}" for k, v in sorted(lang_counts.items()))

    click.echo(f"  * Files indexed:   {total_files}")
    click.echo(f"  * Languages:       {lang_summary or 'none'}")
    click.echo(f"  * Classes/Structs: {total_classes}")
    click.echo(f"  * Functions/Defs:  {total_functions}")
    click.echo(f"  * Function calls:  {total_calls}")
    if syntax_errors > 0:
        click.secho(
            f"  * Syntax errors:   {syntax_errors} (skipped gracefully)",
            fg="yellow",
        )

    click.secho(
        f"\n[INGEST] Ingesting AST nodes & relationships into graph '{graph_name}'...",
        fg="cyan",
        bold=True,
    )

    try:
        db = Database(db_path=actual_db, graph_name=graph_name)
        stats = parser.save_to_graph(results, db=db)
        click.secho("[OK] Graph successfully populated!", fg="green", bold=True)
        click.echo(f"  * :CodeFile nodes:       {stats['files']}")
        click.echo(f"  * :Class nodes:          {stats['classes']}")
        click.echo(f"  * :Function nodes:       {stats['functions']}")
        click.echo(f"  * :CONTAINS edges:       {stats['contains_edges']}")
        click.echo(f"  * :CALLS edges:          {stats['calls_edges']}")
        click.echo(f"  * Database file:         {Path(actual_db).resolve()}")

        if connect:
            click.secho(
                f"\n[REGISTER] Auto-connecting memory to '{connect}'...",
                fg="cyan",
                bold=True,
            )
            # Use dynamic discovery if db_path was not explicitly customized
            reg_db = db_path
            if connect.lower() == "all":
                results = register_all_clients(
                    db_path=reg_db, workspace_dir=str(target_dir), enable_watch=watch
                )
                for client_name, ok, msg in results:
                    prefix = "✓" if ok else "✗"
                    color = "green" if ok else "red"
                    click.secho(f"  {prefix} [{client_name.upper()}] {msg}", fg=color)
            else:
                ok, msg = register_client_mcp(
                    connect, db_path=reg_db, workspace_dir=str(target_dir), enable_watch=watch
                )
                prefix = "✓" if ok else "✗"
                color = "green" if ok else "red"
                click.secho(f"  {prefix} [{connect.upper()}] {msg}", fg=color)
    except DatabaseConnectionError as err:
        click.secho(f"\n[ERROR] Database connection failed: {err}", fg="red", err=True)
        sys.exit(1)
    except Exception as err:
        click.secho(f"\n[ERROR] Ingestion error: {err}", fg="red", err=True)
        sys.exit(1)


@cli.command("export")
@click.option(
    "--source",
    default=DEFAULT_SOURCE_DIR,
    show_default=True,
    help="Source directory containing agent memory.",
)
@click.option(
    "--output",
    "-o",
    default=DEFAULT_ARCHIVE_FILE,
    show_default=True,
    help="Output path for the encrypted archive.",
)
@click.option(
    "--key",
    "-k",
    default=None,
    help=f"Fernet AES encryption key (or set {ENV_SECRET_KEY} env var).",
)
def export_cmd(source: str, output: str, key: Optional[str]) -> None:
    """Compress and encrypt the local database into 'memory-seed.enc'."""
    src_path = Path(source).resolve()
    out_path = Path(output).resolve()

    if not src_path.exists():
        click.secho(
            f"[ERROR] Source directory '{src_path}' does not exist. Run 'init' first.",
            fg="red",
            err=True,
        )
        sys.exit(1)

    click.secho(
        f"\n[EXPORT] Compressing and encrypting '{src_path}' -> '{out_path}'...",
        fg="cyan",
        bold=True,
    )

    try:
        saved_file = export_memory_archive(
            source_dir=src_path, output_file=out_path, secret_key=key
        )
        file_size_kb = saved_file.stat().st_size / 1024
        click.secho(
            "[OK] Memory exported and encrypted successfully!", fg="green", bold=True
        )
        click.echo(f"  * Encrypted archive: {saved_file}")
        click.echo(f"  * Archive size:      {file_size_kb:.2f} KB")
        click.echo(f"  * Encryption method: AES-128-CBC / Fernet HMAC-SHA256")
    except ValueError as err:
        click.secho(f"[ERROR] Key configuration error: {err}", fg="red", err=True)
        sys.exit(1)
    except EncryptionError as err:
        click.secho(f"[ERROR] Encryption error: {err}", fg="red", err=True)
        sys.exit(1)


@cli.command("pull")
@click.option(
    "--archive",
    "-a",
    default=DEFAULT_ARCHIVE_FILE,
    show_default=True,
    help="Path to the encrypted archive file.",
)
@click.option(
    "--target",
    "-t",
    default=DEFAULT_SOURCE_DIR,
    show_default=True,
    help="Target directory to restore memory files into.",
)
@click.option(
    "--key",
    "-k",
    default=None,
    help=f"Fernet AES encryption key (or set {ENV_SECRET_KEY} env var).",
)
def pull_cmd(archive: str, target: str, key: Optional[str]) -> None:
    """Decrypt and restore the database from 'memory-seed.enc'."""
    arch_path = Path(archive).resolve()
    dest_path = Path(target).resolve()

    if not arch_path.exists():
        click.secho(
            f"[ERROR] Encrypted archive '{arch_path}' was not found.", fg="red", err=True
        )
        sys.exit(1)

    click.secho(
        f"\n[RESTORE] Decrypting and restoring '{arch_path}' -> '{dest_path}'...",
        fg="cyan",
        bold=True,
    )

    try:
        restored = import_memory_archive(
            archive_file=arch_path, target_dir=dest_path, secret_key=key
        )
        click.secho("[OK] Database restored successfully!", fg="green", bold=True)
        click.echo(f"  * Restored location: {restored}")
        db_file = dest_path / "brain.db"
        if db_file.exists():
            click.echo(f"  * Database file:    {db_file} (Ready)")
    except ValueError as err:
        click.secho(f"[ERROR] Key configuration error: {err}", fg="red", err=True)
        sys.exit(1)
    except DecryptionError as err:
        click.secho(f"[ERROR] Decryption error: {err}", fg="red", err=True)
        sys.exit(1)


@cli.command("register")
@click.option(
    "--client",
    "-c",
    type=click.Choice(["all", "antigravity", "claude", "cursor"], case_sensitive=False),
    default="all",
    show_default=True,
    help="AI agent client to configure with this MCP server.",
)
@click.option(
    "--db-path",
    default=None,
    help="Explicit path to embedded database file (default: dynamic workspace discovery across projects).",
)
@click.option(
    "--watch/--no-watch",
    default=True,
    show_default=True,
    help="Enable automatic background file watching whenever the AI agent connects.",
)
def register_cmd(
    client: str, db_path: Optional[str] = None, watch: bool = True
) -> None:
    """Auto-detect and register MCP server with installed AI agents (Antigravity, Claude, Cursor)."""
    watch_msg = " (auto-watch enabled)" if watch else ""
    click.secho(
        f"\n[REGISTER] Registering agent-brain MCP server with '{client}'{watch_msg}...",
        fg="cyan",
        bold=True,
    )
    if client.lower() == "all":
        results = register_all_clients(db_path=db_path, enable_watch=watch)
        for c_name, ok, msg in results:
            prefix = "✓" if ok else "✗"
            color = "green" if ok else "red"
            click.secho(f"  {prefix} [{c_name.upper()}] {msg}", fg=color)
    else:
        ok, msg = register_client_mcp(client, db_path=db_path, enable_watch=watch)
        prefix = "✓" if ok else "✗"
        color = "green" if ok else "red"
        click.secho(f"  {prefix} [{client.upper()}] {msg}", fg=color)
        if not ok:
            sys.exit(1)
    click.echo()


@cli.command("mcp")
@click.option(
    "--client",
    "-c",
    type=click.Choice(["all", "antigravity", "claude", "cursor"], case_sensitive=False),
    default=None,
    help="Auto-register with target agent client before launching the server.",
)
@click.option(
    "--db-path",
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to embedded database file.",
)
@click.option(
    "--watch",
    is_flag=True,
    default=False,
    help="Enable background outbox file watcher for real-time incremental code indexing.",
)
def mcp_cmd(
    client: Optional[str] = None,
    db_path: str = DEFAULT_DB_PATH,
    watch: bool = False,
) -> None:
    """Launch the Model Context Protocol (MCP) server over stdio for local AI agents."""
    if client:
        click.secho(
            f"[REGISTER] Ensuring registration for '{client}'...",
            fg="cyan",
            bold=True,
            err=True,
        )
        if client.lower() == "all":
            results = register_all_clients(db_path=db_path)
            for c_name, ok, msg in results:
                color = "green" if ok else "red"
                click.secho(f"  * [{c_name.upper()}] {msg}", fg=color, err=True)
        else:
            ok, msg = register_client_mcp(client, db_path=db_path)
            color = "green" if ok else "red"
            click.secho(f"  * [{client.upper()}] {msg}", fg=color, err=True)

    if watch:
        os.environ["AGENT_MEMORY_WATCH"] = "1"

    try:
        from tools.mcp_server import main as run_mcp

        click.secho(
            "[MCP] Starting agent-brain MCP server on stdio transport...",
            fg="cyan",
            bold=True,
            err=True,
        )
        run_mcp()
    except Exception as err:
        click.secho(f"[ERROR] Failed to start MCP server: {err}", fg="red", err=True)
        sys.exit(1)


@cli.command("watch")
@click.option(
    "--path",
    "-p",
    default=".",
    show_default=True,
    help="Codebase directory to watch.",
)
@click.option(
    "--db-path",
    default=None,
    help="Path to embedded KùzuDB database file (default: <path>/.agent-memory/brain.db).",
)
@click.option(
    "--debounce",
    "-d",
    default=1.0,
    show_default=True,
    type=float,
    help="Debounce delay in seconds before reconciling file change batches.",
)
@click.option(
    "--workers",
    "-w",
    default=None,
    type=int,
    help="Worker threads for AST parsing (default: auto CPU-based).",
)
def watch_cmd(
    path: str,
    db_path: Optional[str],
    debounce: float,
    workers: Optional[int],
) -> None:
    """Start background outbox file watcher for real-time incremental graph sync."""
    target_dir = Path(path).resolve()
    if not target_dir.exists():
        click.secho(f"[ERROR] Target directory '{target_dir}' does not exist.", fg="red", err=True)
        sys.exit(1)

    actual_db = db_path or str(target_dir / ".agent-memory" / "brain.db")

    click.secho("\n[WATCH] Initializing agent-brain outbox file watcher...", fg="cyan", bold=True)
    click.echo(f"  * Watching directory:  {target_dir}")
    click.echo(f"  * Database path:       {actual_db}")
    click.echo(f"  * Debounce delay:      {debounce}s")
    click.echo(f"  * AST worker threads:  {workers or 'auto'}")

    from sync.watcher import CodebaseWatcher
    import time

    def on_batch_reconciled(stats: dict) -> None:
        duration = stats.get("duration_ms", 0)
        purged = stats.get("purged_files", 0)
        files = stats.get("files", 0)
        calls = stats.get("calls_edges", 0)
        click.secho(
            f"  [SYNC] Reconciled batch in {duration}ms: {files} updated, {purged} purged, {calls} calls re-wired",
            fg="green",
        )

    watcher = CodebaseWatcher(
        target_dir=target_dir,
        db_path=actual_db,
        debounce_seconds=debounce,
        max_workers=workers,
        on_reconcile=on_batch_reconciled,
    )

    try:
        watcher.start()
        click.secho("\n[READY] Watching for code changes. Press Ctrl+C to stop.\n", fg="green", bold=True)
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        click.secho("\n[STOPPING] Shutting down watcher...", fg="yellow")
    finally:
        watcher.stop()
        click.secho("[OK] Watcher stopped cleanly.", fg="green")


@cli.command("benchmark")
@click.option(
    "--path",
    "-p",
    default="./core",
    show_default=True,
    help="Target codebase folder to benchmark.",
)
@click.option(
    "--symbol",
    "-s",
    default="__init__",
    show_default=True,
    help="Target function symbol for dependency graph traversal.",
)
@click.option(
    "--force-mode",
    type=click.Choice(["auto", "indexing", "compare"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="Force benchmark mode: 'auto', 'indexing', or 'compare'.",
)
def benchmark_cmd(path: str, symbol: str, force_mode: str) -> None:
    """Run performance and token efficiency benchmark."""
    from benchmark import run_benchmark

    run_benchmark(path=path, symbol=symbol, force_mode=force_mode)


@cli.command("track")
@click.option(
    "--session",
    "-s",
    default=None,
    help="Target Antigravity conversation session ID (default: latest active session).",
)
@click.option(
    "--all",
    "-a",
    "track_all",
    is_flag=True,
    default=False,
    help="Aggregate token metrics across all recorded Antigravity sessions.",
)
@click.option(
    "--list",
    "-l",
    "list_sessions",
    is_flag=True,
    default=False,
    help="List all recorded Antigravity sessions.",
)
@click.option(
    "--json-output",
    "--json",
    is_flag=True,
    default=False,
    help="Output metrics as raw structured JSON.",
)
@click.option(
    "--export",
    "-e",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Export benchmark metrics to a file (JSON format).",
)
def track_cmd(
    session: Optional[str],
    track_all: bool,
    list_sessions: bool,
    json_output: bool,
    export: Optional[str],
) -> None:
    """Track and benchmark token usage across Google Antigravity IDE agent sessions."""
    from tools.antigravity_token_tracker import AntigravityTokenTracker

    tracker = AntigravityTokenTracker()

    if list_sessions:
        sessions = tracker.list_sessions()
        if not sessions:
            click.secho("[!] No Antigravity sessions found in logs directory.", fg="yellow")
            return
        click.secho("\n[SESSIONS] Recorded Antigravity Sessions:\n", fg="cyan", bold=True)
        click.echo(f"  {'#':<4} {'SESSION ID':<38} {'MODIFIED':<18} {'ENV':<6} {'SIZE':<10}")
        click.echo("  " + "-" * 78)
        import datetime
        for idx, s in enumerate(sessions[:25], 1):
            mtime_str = datetime.datetime.fromtimestamp(s["mtime"]).strftime("%Y-%m-%d %H:%M")
            size_kb = f"{s['size_bytes'] / 1024:.1f} KB"
            marker = " (Latest)" if idx == 1 else ""
            click.echo(f"  {idx:<4} {s['session_id']:<38} {mtime_str:<18} {s.get('environment', 'App'):<6} {size_kb:<10}{marker}")
        click.echo("\n💡 Run 'agent-brain track -s <session_id>' to inspect a specific session.\n")
        return

    if track_all:
        report = tracker.analyze_all()
    elif session:
        report = tracker.analyze_session(session).to_dict()
    else:
        latest = tracker.get_latest_session_id()
        if not latest:
            click.secho("[!] No Antigravity sessions found in logs directory.", fg="yellow")
            return
        report = tracker.analyze_session(latest).to_dict()

    if export:
        out_path = Path(export)
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        click.secho(f"[✓] Token report exported to: {out_path.resolve()}", fg="green")

    if json_output:
        click.echo(json.dumps(report, indent=2))
    else:
        click.echo(tracker.format_report(report))


@cli.command("generate-key")
def generate_key_cmd() -> None:
    """Generate a new Fernet AES encryption key for memory synchronization."""
    new_key = generate_encryption_key()
    click.secho("[KEY] New Encryption Key Generated:\n", fg="green", bold=True)
    click.secho(new_key, fg="yellow", bold=True)
    click.echo("\nTo use this key, set it as your environment variable:")
    click.secho(
        f"  export {ENV_SECRET_KEY}='{new_key}'   # Linux / macOS / WSL",
        fg="cyan",
    )
    click.secho(
        f"  $env:{ENV_SECRET_KEY}='{new_key}'      # Windows PowerShell",
        fg="cyan",
    )


if __name__ == "__main__":
    cli()
