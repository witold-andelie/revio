"""Runner — connects the agent graph to the streaming output layer.

This is the bridge between async LangGraph execution and the user-facing
terminal stream. Outside callers invoke `run_agent(...)` and receive a
ReviewReport at the end; everything in between is streamed to stdout.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Callable

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ..config import Config


logger = logging.getLogger(__name__)
from ..detect import detect_project
from ..output.models import ReviewReport
from ..profiles import get_profile, load_all_profiles
from .graph import build_graph
from .state import AgentState


# --- Stream event callback signature ------------------------------------------

# Callbacks: (event_type, payload) — output layer decides how to render.
StreamCallback = Callable[[str, dict], None]


def _noop_stream(event: str, payload: dict) -> None:
    pass


# --- Token accountant --------------------------------------------------------


class _TokenAccountant:
    """Accumulates per-call and session-wide token usage from LLM events.

    Pulls `usage_metadata` (standardized across LangChain providers as
    {input_tokens, output_tokens, total_tokens}) off the AIMessage emitted
    by on_chat_model_end. Computes throughput against the wall-clock time
    between on_chat_model_start and on_chat_model_end.

    Unknown providers / missing usage → silently records zero, so the UX
    never breaks.
    """

    def __init__(self, model: str):
        self.model = model
        self.input_tokens = 0
        self.output_tokens = 0
        self.call_count = 0
        self._last_call_start: float | None = None

    def on_llm_start(self) -> None:
        self._last_call_start = time.time()

    def on_llm_end(self, usage: dict) -> dict:
        """Update totals; return per-call delta payload for the stream."""
        now = time.time()
        elapsed = (now - self._last_call_start) if self._last_call_start else 0.0
        self._last_call_start = None

        delta_in = int(usage.get("input_tokens", 0) or 0)
        delta_out = int(usage.get("output_tokens", 0) or 0)
        self.input_tokens += delta_in
        self.output_tokens += delta_out
        self.call_count += 1

        throughput = (delta_out / elapsed) if (elapsed > 0.01 and delta_out > 0) else 0.0
        from ..output.cost import estimate_cost_usd

        return {
            "delta_input": delta_in,
            "delta_output": delta_out,
            "total_input": self.input_tokens,
            "total_output": self.output_tokens,
            "throughput_tps": throughput,
            "duration_seconds": elapsed,
            "call_count": self.call_count,
            "est_cost_usd": estimate_cost_usd(
                self.model, self.input_tokens, self.output_tokens
            ),
        }

    @property
    def est_cost_usd(self) -> float:
        from ..output.cost import estimate_cost_usd

        return estimate_cost_usd(self.model, self.input_tokens, self.output_tokens)


# --- Main runner --------------------------------------------------------------


async def run_agent(
    *,
    mode: str,
    repo_path: str,
    target_ref: str = "",
    target_description: str = "",
    profile_name: str | None = None,
    config: Config,
    on_event: StreamCallback = _noop_stream,
) -> ReviewReport:
    """Run the agent end-to-end and return a structured ReviewReport.

    Streams events to `on_event(event_type, payload)` throughout. Event types:
      session_start    {mode, repo_path, profile_name, budget}
      plan             {plan_text}
      llm_start        {node}
      llm_token        {chunk}                 (token-level when available)
      llm_end          {node}
      tool_start       {tool, args}
      tool_end         {tool, result_preview, ok}
      finding          {finding_dict}
      reflect          {summary, observations}
      session_end      {report}
    """
    load_all_profiles()
    repo_path_resolved = str(Path(repo_path).expanduser().resolve())

    # Resolve profile if needed
    if profile_name in (None, "auto"):
        fp = detect_project(repo_path_resolved)
        profile_name = fp.suggested_profile if fp.suggested_profile != "auto" else "js"
        on_event("auto_detect", {"fingerprint": fp.model_dump()})

    profile_cls = get_profile(profile_name) if profile_name else None
    profile_hints = profile_cls.make_reasoning_hints() if profile_cls else ""

    # Initial state
    initial: AgentState = {
        "mode": mode,  # type: ignore[assignment]
        "repo_path": repo_path_resolved,
        "target_ref": target_ref,
        "target_description": target_description or _default_target_description(mode, target_ref),
        "profile_name": profile_name or "auto",
        "profile_hints": profile_hints,
        "messages": [],
        "tool_calls_used": 0,
        "tool_calls_budget": config.agent.max_tool_calls,
        "iteration": 0,
        "findings": [],
        "patches": [],
        "dropped_findings": [],
        "plan": "",
        "summary": "",
        "systemic_observations": [],
        "started_at": time.time(),
        "finished_at": 0.0,
        "model_used": config.llm.model,
    }

    on_event(
        "session_start",
        {
            "mode": mode,
            "repo_path": repo_path_resolved,
            "profile_name": profile_name,
            "budget": config.agent.max_tool_calls,
            "model": config.llm.model,
        },
    )

    # Checkpointer — per-repo SQLite, so re-runs against the same repo persist
    ckpt_dir = Path(config.agent.checkpoint_dir).expanduser()
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    repo_hash = hashlib.sha1(repo_path_resolved.encode()).hexdigest()[:12]
    db_path = ckpt_dir / f"{repo_hash}.sqlite"
    thread_id = f"{mode}:{target_ref or 'default'}:{uuid.uuid4().hex[:8]}"

    final_state: dict = {}

    # Build MCP server configs from the user's config
    from mcp.client.session_group import ClientSessionGroup

    from .mcp_client import (
        MCPServerConfig,
        connect_servers,
        connection_summary,
        langchain_tools_from,
        make_name_hook,
    )

    mcp_server_configs: list[MCPServerConfig] = []
    for name, spec in (config.mcp.servers or {}).items():
        if not spec.enabled:
            continue
        mcp_server_configs.append(MCPServerConfig(
            name=name,
            command=spec.command,
            args=list(spec.args),
            env=dict(spec.env),
            url=spec.url,
            api_key_env=spec.api_key_env,
            timeout=spec.timeout,
        ))

    # Per-call mutable name holder — used by the component name hook so each
    # server's tools get prefixed with the right alias.
    name_holder: dict[str, str] = {"current": ""}

    async with ClientSessionGroup(component_name_hook=make_name_hook(name_holder)) as mcp_group:
        connect_results = await connect_servers(mcp_group, mcp_server_configs, name_holder=name_holder)
        mcp_tools = langchain_tools_from(mcp_group)

        # Surface MCP connection results to the stream
        if mcp_server_configs:
            on_event("mcp_connected", {
                "servers": [
                    {"name": r.name, "connected": r.connected, "tool_count": r.new_tool_count}
                    for r in connect_results
                ],
                "failures": [
                    {"name": r.name, "error": r.error}
                    for r in connect_results if not r.connected
                ],
            })

        async with AsyncSqliteSaver.from_conn_string(str(db_path)) as checkpointer:
            # Register our custom Pydantic types in the checkpointer's msgpack
            # allowlist so they round-trip without deprecation warnings.
            try:
                from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

                checkpointer.serde = JsonPlusSerializer(
                    allowed_msgpack_modules=[
                        ("revio.output.models", "Severity"),
                        ("revio.output.models", "ReviewCategory"),
                        ("revio.output.models", "Finding"),
                        ("revio.output.models", "Evidence"),
                        ("revio.output.models", "ReviewReport"),
                        # Patches for `revio dedup --fix`
                        ("revio.agent.patch", "PatchOp"),
                        ("revio.agent.patch", "PatchSet"),
                    ]
                )
            except Exception:
                pass  # Best-effort; permissive default still works

            graph = build_graph(checkpointer=checkpointer)

            run_config = {
                "configurable": {
                    "thread_id": thread_id,
                    "app_config": config,
                    "mcp_tools": mcp_tools,  # available to react_node
                },
                "recursion_limit": 50,
            }

            # Token accountant — collects per-call usage from on_chat_model_end
            accountant = _TokenAccountant(model=config.llm.model)

            # Stream every event from the graph
            async for ev in graph.astream_events(initial, run_config, version="v2"):
                _dispatch_event(ev, on_event, accountant=accountant)

            # Capture final state
            snapshot = await graph.aget_state(run_config)
            final_state = snapshot.values

    # Build the final report
    report = _build_report(final_state, config, accountant=accountant)

    # Stash patches in the CLI's module-level cache so `revio dedup --fix`
    # can pick them up.
    #
    # NOTE on the import dance: `revio/cli/__init__.py` does
    # `from .main import main`, which makes `revio.cli.main` refer to the
    # FUNCTION (an attribute on the package), not the module. Even
    # `import revio.cli.main as X` resolves to the function because Python
    # picks the attribute when both name kinds exist. We bypass that by
    # importing the module first (to make sure it's loaded into sys.modules)
    # then reaching for the actual module object via sys.modules.
    try:
        import sys
        import revio.cli.main  # ensures the module is in sys.modules
        cli_main_module = sys.modules["revio.cli.main"]
        patches = final_state.get("patches", []) or []
        cli_main_module._last_session_patches = list(patches)
        logger.info("revio: cached %d patches for --fix flow", len(patches))
    except Exception as e:
        logger.warning("revio: failed to cache patches: %s", e)

    # Cross-session findings comparison + persistence
    try:
        from .findings_store import FindingsStore

        findings_db = ckpt_dir / f"{repo_hash}_findings.sqlite"
        store = FindingsStore(findings_db)
        comparisons = store.compare(report.findings, repo_root=repo_path_resolved)
        new_count = sum(1 for c in comparisons if c.status == "new")
        still_count = sum(1 for c in comparisons if c.status == "still_present")
        fixed_count = sum(1 for c in comparisons if c.status == "maybe_fixed")
        # Record this run's findings AFTER comparing
        store.record_run(report.findings, repo_root=repo_path_resolved)

        on_event("findings_compared", {
            "new": new_count,
            "still_present": still_count,
            "maybe_fixed": fixed_count,
            "total_history": store.count(),
        })
    except Exception as e:
        logger.warning("findings history skipped: %s", e)

    on_event("session_end", {"report": report.model_dump()})
    return report


def _dispatch_event(
    ev: dict,
    on_event: StreamCallback,
    accountant: "_TokenAccountant | None" = None,
) -> None:
    """Translate raw LangGraph events to our public stream event types."""
    ev_type = ev["event"]
    name = ev.get("name", "")
    data = ev.get("data", {}) or {}

    # Node-level events
    if ev_type == "on_chain_start" and name in {"plan", "react", "reflect"}:
        on_event("node_start", {"node": name})
        return
    if ev_type == "on_chain_end" and name in {"plan", "react", "reflect"}:
        output = data.get("output") or {}
        if name == "plan":
            on_event("plan", {"plan_text": output.get("plan", "")})
        elif name == "react":
            # Surface grounding-validator output if any findings were dropped
            dropped = output.get("dropped_findings", []) or []
            if dropped:
                on_event("findings_dropped", {"dropped": dropped})
        elif name == "reflect":
            on_event(
                "reflect",
                {
                    "summary": output.get("summary", ""),
                    "observations": output.get("systemic_observations", []),
                },
            )
        on_event("node_end", {"node": name})
        return

    # Tool events
    if ev_type == "on_tool_start":
        on_event("tool_start", {"tool": name, "args": data.get("input", {})})
        return
    if ev_type == "on_tool_end":
        out = data.get("output")
        preview = _preview(out)
        on_event("tool_end", {"tool": name, "result_preview": preview, "ok": True})
        # If the tool was report_finding, extract the title from the Command
        # update so the stream UI can show it cleanly.
        if name == "report_finding":
            title = "(finding)"
            severity = ""
            file_path = ""
            line_start = 0
            if hasattr(out, "update"):
                findings = (out.update or {}).get("findings", [])
                if findings and hasattr(findings[0], "title"):
                    title = findings[0].title
                    severity = findings[0].severity.value
                    file_path = findings[0].file_path
                    line_start = findings[0].line_start
            on_event("finding_recorded", {
                "title": title,
                "severity": severity,
                "file_path": file_path,
                "line_start": line_start,
            })
        return

    # LLM events (per-chunk streaming)
    if ev_type == "on_chat_model_start":
        if accountant is not None:
            accountant.on_llm_start()
        on_event("llm_start", {})
        return
    if ev_type == "on_chat_model_stream":
        chunk = data.get("chunk")
        token = _chunk_to_text(chunk)
        if token:
            on_event("llm_token", {"chunk": token})
        return
    if ev_type == "on_chat_model_end":
        # Pull usage_metadata off the final AIMessage. LangChain standardizes
        # this across providers; missing fields silently become zero.
        usage: dict = {}
        out_msg = data.get("output")
        if out_msg is not None:
            um = getattr(out_msg, "usage_metadata", None)
            if um:
                usage = dict(um)
            else:
                # Some providers stash it under response_metadata.token_usage
                rmeta = getattr(out_msg, "response_metadata", None) or {}
                tu = rmeta.get("token_usage") or rmeta.get("usage") or {}
                if tu:
                    usage = {
                        "input_tokens": tu.get("prompt_tokens") or tu.get("input_tokens", 0),
                        "output_tokens": tu.get("completion_tokens") or tu.get("output_tokens", 0),
                    }
        payload = {}
        if accountant is not None:
            payload = accountant.on_llm_end(usage)
        on_event("llm_end", payload)
        if payload:
            on_event("llm_usage", payload)
        return


def _chunk_to_text(chunk) -> str:
    if chunk is None:
        return ""
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, dict) and b.get("type") == "text_delta":
                parts.append(b.get("text", ""))
        return "".join(parts)
    return ""


def _preview(out, limit: int = 240) -> str:
    s = str(out)
    return s if len(s) <= limit else s[:limit] + "…"


def _build_report(
    state: dict,
    config: Config,
    *,
    accountant: "_TokenAccountant | None" = None,
) -> ReviewReport:
    """Materialize a ReviewReport from final graph state."""
    findings = state.get("findings", []) or []
    started = state.get("started_at", 0.0)
    duration = time.time() - started if started else 0.0

    return ReviewReport(
        summary=state.get("summary", "") or f"Found {len(findings)} issue(s).",
        findings=findings,
        reviewed_files=[],
        skipped_files=[],
        tool_calls_used=state.get("tool_calls_used", 0),
        tool_calls_budget=state.get("tool_calls_budget", 0),
        duration_seconds=duration,
        model_used=config.llm.model,
        systemic_observations=state.get("systemic_observations", []) or [],
        total_input_tokens=accountant.input_tokens if accountant else 0,
        total_output_tokens=accountant.output_tokens if accountant else 0,
        llm_call_count=accountant.call_count if accountant else 0,
        est_cost_usd=accountant.est_cost_usd if accountant else 0.0,
    )


def _default_target_description(mode: str, target_ref: str) -> str:
    if mode == "review":
        return f"the diff at {target_ref}" if target_ref else "the latest commit"
    if mode == "audit":
        return "the entire repository"
    if mode == "dedup":
        return "the codebase, looking for AI-generated redundancy"
    return "the codebase"


# --- Sync wrapper -------------------------------------------------------------


def run_agent_sync(**kwargs) -> ReviewReport:
    """Sync wrapper for non-async callers."""
    return asyncio.run(run_agent(**kwargs))
