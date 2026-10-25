"""Antigravity Token Tracker service for agent-brain.

Tracks and benchmarks real-world token usage across Google Antigravity environments:
- Multi-environment tracking: Antigravity IDE (VS Code editor) & Antigravity 2.0 (Desktop companion).
- Server-side ground truth model token metrics (input, cache-read, output).
- Detailed tool invocation metrics (agent-brain MCP vs traditional raw file reads).
- Context payload token weights using BPE tokenizer, resolving offloaded disk payloads.
- Token savings and cache hit efficiency ratios.
"""

from __future__ import annotations

import glob
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

# Ensure UTF-8 output encoding across Windows consoles
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import tiktoken

AGENT_BRAIN_TOOLS = {"search_code_graph", "get_dependency_impact"}
RAW_READ_TOOLS = {"read_file", "view_file"}


def get_default_brain_dirs() -> List[Path]:
    """Resolve all default Antigravity brain directories (both Antigravity IDE and Desktop App)."""
    candidates: List[Path] = []
    if sys.platform == "win32":
        user_profile = os.environ.get("USERPROFILE", "")
        if user_profile:
            base = Path(user_profile) / ".gemini"
            candidates = [
                base / "antigravity-ide" / "brain",
                base / "antigravity" / "brain",
            ]
    if not candidates:
        home_base = Path.home() / ".gemini"
        candidates = [
            home_base / "antigravity-ide" / "brain",
            home_base / "antigravity" / "brain",
        ]
    existing = [p for p in candidates if p.exists()]
    return existing if existing else candidates


def get_default_brain_dir() -> Path:
    """Resolve the primary default Antigravity brain logs directory."""
    dirs = get_default_brain_dirs()
    return dirs[0] if dirs else Path.home() / ".gemini" / "antigravity" / "brain"


@dataclass
class ToolUsageStats:
    """Stats for a specific tool category or tool."""

    calls: int = 0
    payload_tokens: int = 0


@dataclass
class SessionTokenStats:
    """Aggregated token and tool usage metrics for a session."""

    session_id: str
    environment: str = "Antigravity"
    steps_count: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    total_model_tokens: int = 0
    caching_ratio_pct: float = 0.0

    # Tool specific payload & call metrics
    agent_brain_calls: int = 0
    agent_brain_payload_tokens: int = 0
    raw_read_calls: int = 0
    raw_read_payload_tokens: int = 0

    tool_breakdown: Dict[str, int] = field(default_factory=dict)
    token_savings_pct: Optional[float] = None
    agent_brain_avg_tokens: float = 0.0
    raw_read_avg_tokens: float = 0.0
    per_call_savings_pct: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert dataclass to clean dictionary."""
        return asdict(self)


class AntigravityTokenTracker:
    """Service to monitor, track, and benchmark token usage in Antigravity IDE and App."""

    def __init__(
        self,
        brain_dir: Optional[Union[str, Path, List[Union[str, Path]]]] = None,
        encoding_name: str = "cl100k_base",
    ) -> None:
        """Initialize tracker with target Antigravity brain directories and tokenizer."""
        if brain_dir is None:
            self.brain_dirs = get_default_brain_dirs()
        elif isinstance(brain_dir, list):
            self.brain_dirs = [Path(d) for d in brain_dir]
        else:
            self.brain_dirs = [Path(brain_dir)]

        # Backward compatibility property
        self.brain_dir = self.brain_dirs[0] if self.brain_dirs else get_default_brain_dir()

        try:
            self.tokenizer = tiktoken.get_encoding(encoding_name)
        except Exception:
            self.tokenizer = tiktoken.encoding_for_model("gpt-4o")

    def count_tokens(self, text: str) -> int:
        """Calculate token count for text using BPE tokenizer."""
        if not text:
            return 0
        try:
            return len(self.tokenizer.encode(str(text)))
        except Exception:
            return max(1, len(str(text)) // 4)

    def list_sessions(self) -> List[Dict[str, Any]]:
        """List all available Antigravity conversation sessions across all brain directories."""
        sessions = []
        seen_ids = set()

        for b_dir in self.brain_dirs:
            if not b_dir.exists():
                continue

            env_name = "IDE" if "antigravity-ide" in str(b_dir).lower() else "App"

            for entry in b_dir.iterdir():
                if not entry.is_dir() or entry.name.startswith("."):
                    continue

                if entry.name in seen_ids:
                    continue

                log_dir = entry / ".system_generated" / "logs"
                transcript = log_dir / "transcript_full.jsonl"
                if not transcript.exists():
                    transcript = log_dir / "transcript.jsonl"

                if transcript.exists():
                    mtime = transcript.stat().st_mtime
                    seen_ids.add(entry.name)
                    sessions.append(
                        {
                            "session_id": entry.name,
                            "environment": env_name,
                            "path": str(entry),
                            "brain_dir": str(b_dir),
                            "transcript_path": str(transcript),
                            "mtime": mtime,
                            "size_bytes": transcript.stat().st_size,
                        }
                    )

        sessions.sort(key=lambda s: s["mtime"], reverse=True)
        return sessions

    def get_latest_session_id(self) -> Optional[str]:
        """Return the most recently active session ID, if any."""
        sessions = self.list_sessions()
        return sessions[0]["session_id"] if sessions else None

    def find_session_path(self, session_id: str) -> Tuple[Optional[Path], str]:
        """Locate the session folder and environment name across configured directories."""
        for b_dir in self.brain_dirs:
            candidate = b_dir / session_id
            if candidate.exists():
                env_name = "IDE" if "antigravity-ide" in str(b_dir).lower() else "App"
                return candidate, env_name
        return None, "Unknown"

    def analyze_session(self, session_id: str) -> SessionTokenStats:
        """Analyze a specific Antigravity conversation session."""
        session_dir, env_name = self.find_session_path(session_id)
        if not session_dir:
            return SessionTokenStats(session_id=session_id)

        log_dir = session_dir / ".system_generated" / "logs"

        # Prioritize full transcript so payload tokens are never truncated
        transcript = log_dir / "transcript_full.jsonl"
        if not transcript.exists():
            transcript = log_dir / "transcript.jsonl"

        stats = SessionTokenStats(session_id=session_id, environment=env_name)

        if not transcript.exists():
            return stats

        pending_tool_category: Optional[str] = None

        with open(transcript, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line_str = line.strip()
                if not line_str:
                    continue

                try:
                    entry = json.loads(line_str)
                except Exception:
                    continue

                entry_type = entry.get("type")

                # Step N: Ground truth model tokens + tool calls
                if entry_type == "PLANNER_RESPONSE":
                    stats.steps_count += 1
                    inp = entry.get("input_tokens", 0) or 0
                    cache = entry.get("cache_read_tokens", 0) or 0
                    out = entry.get("output_tokens", 0) or 0

                    stats.input_tokens += inp
                    stats.cache_read_tokens += cache
                    stats.output_tokens += out

                    tool_calls = entry.get("tool_calls", [])
                    if tool_calls:
                        for call in tool_calls:
                            name = str(call.get("name", "")).strip()
                            args = call.get("args", {}) or {}

                            # Case 1: Lazy MCP call through 'call_mcp_tool'
                            if name == "call_mcp_tool":
                                # Strip internal quotes (e.g. '"agent-brain"' in Antigravity IDE)
                                s_name = str(args.get("ServerName", "")).strip('"\' \t\r\n')
                                t_name = str(args.get("ToolName", "")).strip('"\' \t\r\n')

                                if s_name == "agent-brain" or t_name in AGENT_BRAIN_TOOLS or "agent-brain" in s_name:
                                    stats.agent_brain_calls += 1
                                    key = f"agent-brain:{t_name or 'mcp'}"
                                    stats.tool_breakdown[key] = stats.tool_breakdown.get(key, 0) + 1
                                    pending_tool_category = "agent-brain"
                                else:
                                    key = f"mcp:{s_name}:{t_name}"
                                    stats.tool_breakdown[key] = stats.tool_breakdown.get(key, 0) + 1
                                    pending_tool_category = "other_mcp"

                            # Case 2: Eager MCP tool or direct function name
                            elif "agent-brain" in name or name in AGENT_BRAIN_TOOLS:
                                stats.agent_brain_calls += 1
                                stats.tool_breakdown[name] = stats.tool_breakdown.get(name, 0) + 1
                                pending_tool_category = "agent-brain"

                            # Case 3: Traditional raw file inspection
                            elif name in RAW_READ_TOOLS:
                                stats.raw_read_calls += 1
                                stats.tool_breakdown[name] = stats.tool_breakdown.get(name, 0) + 1
                                pending_tool_category = "raw_read"

                            # Case 4: Other tools
                            else:
                                stats.tool_breakdown[name] = stats.tool_breakdown.get(name, 0) + 1
                                pending_tool_category = None
                    else:
                        pending_tool_category = None

                # Step N+1: Tool result payload returning into context
                elif pending_tool_category and entry.get("content"):
                    payload = str(entry.get("content", ""))

                    # Resolve offloaded output files if Antigravity saved large payload to disk
                    if (
                        "The output was large and was saved to:" in payload
                        or "output was truncated due to being too long" in payload
                    ):
                        step_idx = entry.get("step_index")
                        if step_idx is not None:
                            step_file = (
                                session_dir
                                / ".system_generated"
                                / "steps"
                                / str(step_idx)
                                / "output.txt"
                            )
                            if step_file.exists():
                                try:
                                    payload = step_file.read_text(
                                        encoding="utf-8", errors="ignore"
                                    )
                                except Exception:
                                    pass

                    tokens = self.count_tokens(payload)

                    if pending_tool_category == "agent-brain":
                        stats.agent_brain_payload_tokens += tokens
                    elif pending_tool_category == "raw_read":
                        stats.raw_read_payload_tokens += tokens

                    pending_tool_category = None

        stats.total_model_tokens = stats.input_tokens + stats.output_tokens
        total_prompt_context = stats.input_tokens + stats.cache_read_tokens
        if total_prompt_context > 0:
            stats.caching_ratio_pct = round(
                (stats.cache_read_tokens / total_prompt_context) * 100, 2
            )

        if stats.raw_read_payload_tokens > 0 and stats.agent_brain_payload_tokens > 0:
            savings = (
                (stats.raw_read_payload_tokens - stats.agent_brain_payload_tokens)
                / stats.raw_read_payload_tokens
            ) * 100
            stats.token_savings_pct = round(savings, 2)

        stats.agent_brain_avg_tokens = (
            round(stats.agent_brain_payload_tokens / stats.agent_brain_calls, 1)
            if stats.agent_brain_calls > 0
            else 0.0
        )
        stats.raw_read_avg_tokens = (
            round(stats.raw_read_payload_tokens / stats.raw_read_calls, 1)
            if stats.raw_read_calls > 0
            else 0.0
        )
        if stats.raw_read_avg_tokens > 0 and stats.agent_brain_avg_tokens > 0:
            stats.per_call_savings_pct = round(
                (
                    (stats.raw_read_avg_tokens - stats.agent_brain_avg_tokens)
                    / stats.raw_read_avg_tokens
                )
                * 100,
                1,
            )

        return stats

    def analyze_all(self, max_sessions: Optional[int] = None) -> Dict[str, Any]:
        """Aggregate token statistics across all recorded Antigravity IDE and App sessions."""
        sessions = self.list_sessions()
        if max_sessions:
            sessions = sessions[:max_sessions]

        aggregated = {
            "sessions_count": len(sessions),
            "environments": sorted(list(set(s.get("environment", "Unknown") for s in sessions))),
            "total_steps": 0,
            "input_tokens": 0,
            "cache_read_tokens": 0,
            "output_tokens": 0,
            "total_model_tokens": 0,
            "caching_ratio_pct": 0.0,
            "agent_brain_calls": 0,
            "agent_brain_payload_tokens": 0,
            "raw_read_calls": 0,
            "raw_read_payload_tokens": 0,
            "tool_breakdown": {},
            "token_savings_pct": None,
            "sessions": [],
        }

        for s in sessions:
            st = self.analyze_session(s["session_id"])
            st_dict = st.to_dict()
            aggregated["sessions"].append(st_dict)

            aggregated["total_steps"] += st.steps_count
            aggregated["input_tokens"] += st.input_tokens
            aggregated["cache_read_tokens"] += st.cache_read_tokens
            aggregated["output_tokens"] += st.output_tokens
            aggregated["agent_brain_calls"] += st.agent_brain_calls
            aggregated["agent_brain_payload_tokens"] += st.agent_brain_payload_tokens
            aggregated["raw_read_calls"] += st.raw_read_calls
            aggregated["raw_read_payload_tokens"] += st.raw_read_payload_tokens

            for tool, count in st.tool_breakdown.items():
                aggregated["tool_breakdown"][tool] = (
                    aggregated["tool_breakdown"].get(tool, 0) + count
                )

        aggregated["total_model_tokens"] = (
            aggregated["input_tokens"] + aggregated["output_tokens"]
        )
        total_ctx = aggregated["input_tokens"] + aggregated["cache_read_tokens"]
        if total_ctx > 0:
            aggregated["caching_ratio_pct"] = round(
                (aggregated["cache_read_tokens"] / total_ctx) * 100, 2
            )

        if (
            aggregated["raw_read_payload_tokens"] > 0
            and aggregated["agent_brain_payload_tokens"] > 0
        ):
            savings = (
                (
                    aggregated["raw_read_payload_tokens"]
                    - aggregated["agent_brain_payload_tokens"]
                )
                / aggregated["raw_read_payload_tokens"]
            ) * 100
            aggregated["token_savings_pct"] = round(savings, 2)

        ab_calls = aggregated["agent_brain_calls"]
        ab_tokens = aggregated["agent_brain_payload_tokens"]
        raw_calls = aggregated["raw_read_calls"]
        raw_tokens = aggregated["raw_read_payload_tokens"]

        aggregated["agent_brain_avg_tokens"] = (
            round(ab_tokens / ab_calls, 1) if ab_calls > 0 else 0.0
        )
        aggregated["raw_read_avg_tokens"] = (
            round(raw_tokens / raw_calls, 1) if raw_calls > 0 else 0.0
        )
        if aggregated["raw_read_avg_tokens"] > 0 and aggregated["agent_brain_avg_tokens"] > 0:
            aggregated["per_call_savings_pct"] = round(
                (
                    (aggregated["raw_read_avg_tokens"] - aggregated["agent_brain_avg_tokens"])
                    / aggregated["raw_read_avg_tokens"]
                )
                * 100,
                1,
            )
        else:
            aggregated["per_call_savings_pct"] = None

        return aggregated

    def format_report(self, data: Dict[str, Any] | SessionTokenStats) -> str:
        """Format tracking results into a clean, human-readable terminal report."""
        if isinstance(data, SessionTokenStats):
            data = data.to_dict()

        is_multi = "sessions_count" in data
        env = data.get("environment", "Unified")
        session_id = data.get("session_id", "UNKNOWN")

        if is_multi:
            header_title = f"ALL SESSIONS SUMMARY ({data.get('sessions_count', 0)} sessions across {', '.join(data.get('environments', ['Antigravity']))})"
        else:
            header_title = f"SESSION {session_id} [{env}]"

        lines = [
            "=" * 68,
            f"  ANTIGRAVITY TOKEN TRACKER - {header_title}",
            "=" * 68,
        ]

        if is_multi:
            lines.append(f"Sessions Analyzed:             {data.get('sessions_count', 0):,}")
            lines.append(f"Total Agent Planner Steps:     {data.get('total_steps', 0):,}")
        else:
            lines.append(f"Session Environment:           {env}")
            lines.append(f"Session Steps:                 {data.get('steps_count', 0):,}")

        inp_tokens = data.get("input_tokens", 0)
        cache_tokens = data.get("cache_read_tokens", 0)
        out_tokens = data.get("output_tokens", 0)
        tot_model = data.get("total_model_tokens", 0)

        if tot_model > 0:
            lines.extend([
                f"Active Model Input Tokens:     {inp_tokens:,}",
                f"Cached Context Tokens:         {cache_tokens:,}",
                f"Model Output/Thought Tokens:   {out_tokens:,}",
                f"Total Billable Model Tokens:   {tot_model:,}",
                f"Context Cache Hit Rate:        {data.get('caching_ratio_pct', 0.0)}%",
            ])
        else:
            lines.append("Model Tokens:                  (Logged via IDE client background telemetry)")

        ab_calls = data.get("agent_brain_calls", 0)
        ab_tokens = data.get("agent_brain_payload_tokens", 0)
        ab_avg = data.get("agent_brain_avg_tokens", round(ab_tokens / ab_calls, 1) if ab_calls > 0 else 0.0)

        raw_calls = data.get("raw_read_calls", 0)
        raw_tokens = data.get("raw_read_payload_tokens", 0)
        raw_avg = data.get("raw_read_avg_tokens", round(raw_tokens / raw_calls, 1) if raw_calls > 0 else 0.0)

        lines.extend([
            "-" * 68,
            "TOOL CONTEXT EFFICIENCY: AGENT-BRAIN VS RAW FILE READS",
            f"• agent-brain Graph Calls:     {ab_calls:,} calls | {ab_tokens:,} tokens  (avg: {ab_avg:.1f} tokens/call)",
            f"• Raw File Read Calls:         {raw_calls:,} calls | {raw_tokens:,} tokens  (avg: {raw_avg:.1f} tokens/call)",
        ])

        if ab_calls > 0 and raw_calls > 0:
            per_call_pct = data.get("per_call_savings_pct")
            if per_call_pct is None and raw_avg > 0:
                per_call_pct = round(((raw_avg - ab_avg) / raw_avg) * 100, 1)

            if per_call_pct is not None and per_call_pct > 0:
                lines.append(f"⚡ Per-Call Payload Reduction:   {per_call_pct:.1f}% smaller payload per graph query")
                est_saved = max(0, int((raw_avg - ab_avg) * ab_calls))
                lines.append(f"💡 Estimated Context Saved:     ~{est_saved:,} tokens saved across {ab_calls} graph lookups")
            elif per_call_pct is not None:
                lines.append(f"• Per-Call Payload Difference:  {abs(per_call_pct):.1f}%")

            lines.append("   (Note: File reads also handle code edits, configs, and test logs)")
        elif ab_calls > 0:
            lines.append(f"• Graph Lookups Active:        {ab_calls} calls (avg: {ab_avg:.1f} tokens/call)")
        elif raw_calls > 0:
            lines.append("• Tip: Run 'agent-brain register' to use graph-accelerated symbol lookups.")
        else:
            lines.append("• No code exploration calls recorded in this session.")

        lines.extend(["-" * 68, "Top Tools Invocation Breakdown:"])
        breakdown = data.get("tool_breakdown", {})
        if breakdown:
            for tool, cnt in sorted(breakdown.items(), key=lambda x: x[1], reverse=True)[:10]:
                lines.append(f"  - {tool:30}: {cnt:,} calls")
        else:
            lines.append("  (No tool calls recorded)")

        lines.append("=" * 68)
        return "\n".join(lines)


if __name__ == "__main__":
    tracker = AntigravityTokenTracker()
    latest = tracker.get_latest_session_id()
    if latest:
        report = tracker.analyze_session(latest)
        print(tracker.format_report(report))
    else:
        print("No Antigravity sessions found.")
