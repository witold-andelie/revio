"""Streaming output handler — turns agent events into terminal output.

Implements the "agent feel" UX: visible plan, narrated tool calls,
streaming LLM tokens, findings as cards, reflect summary at end.

Used by the CLI's stream format. JSON and Markdown formats use the final
ReviewReport directly.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text


# --- Severity badge styles ----------------------------------------------------

_SEV_STYLE = {
    "critical": "bold white on red",
    "error": "bold white on dark_orange",
    "warning": "bold black on yellow",
    "info": "bold white on blue",
}

_SEV_GLYPH = {
    "critical": "⛔",
    "error": "🔴",
    "warning": "🟡",
    "info": "🔵",
}


# --- StreamRenderer -----------------------------------------------------------


class StreamRenderer:
    """Receives agent events and writes pretty output to a Rich console.

    Pass `.handle` as the on_event callback to run_agent().
    """

    def __init__(self, console: Console | None = None, *, verbose: bool = True):
        self.console = console or Console()
        self.verbose = verbose
        self._in_llm_stream = False
        self._token_buffer: list[str] = []
        self._last_node: str | None = None
        # Token meter: cumulative + last-call snapshot for /cost and footer
        self._tokens_in = 0
        self._tokens_out = 0
        self._llm_calls = 0
        self._last_throughput = 0.0
        self._est_cost = 0.0

    # --- Public entry point ---

    def handle(self, event: str, payload: dict[str, Any]) -> None:
        method = getattr(self, f"_on_{event}", None)
        if method:
            method(payload)
        # Unknown events are silently dropped — keeps forward-compatibility.

    # --- Event handlers ---

    def _on_session_start(self, p: dict) -> None:
        c = self.console
        c.print()
        c.rule(f"[bold cyan]revio · {p['mode']}[/]", style="cyan")
        c.print(
            f"  [dim]repo[/]    {p['repo_path']}\n"
            f"  [dim]profile[/] {p.get('profile_name') or '(auto)'}\n"
            f"  [dim]model[/]   {p.get('model', '')}\n"
            f"  [dim]budget[/]  {p.get('budget')} tool calls"
        )
        c.print()

    def _on_findings_compared(self, p: dict) -> None:
        new = p.get("new", 0)
        still = p.get("still_present", 0)
        fixed = p.get("maybe_fixed", 0)
        total = p.get("total_history", 0)
        if new == 0 and still == 0 and fixed == 0:
            return
        self.console.print()
        self.console.rule("[bold]🕒 Cross-run comparison[/]", style="cyan")
        if new:
            self.console.print(f"  [bold]🆕 New since last run:[/]   {new}")
        if still:
            self.console.print(f"  [yellow]📌 Still present:[/]       {still}")
        if fixed:
            self.console.print(f"  [green]✓ Maybe fixed:[/]         {fixed}")
        self.console.print(f"  [dim](history has {total} unique findings tracked)[/]")

    def _on_mcp_connected(self, p: dict) -> None:
        servers = p.get("servers", []) or []
        failures = p.get("failures", []) or []
        if not servers:
            return
        self.console.print()
        self.console.print("[bold]🔌 MCP servers[/]")
        for s in servers:
            if s.get("connected"):
                self.console.print(
                    f"  [green]✓[/] {s['name']} → {s.get('tool_count', 0)} tools"
                )
            else:
                self.console.print(f"  [red]✗[/] {s['name']} (not connected)")
        for f in failures:
            self.console.print(
                f"  [red]·[/] {f['name']}: [dim]{f.get('error', '?')}[/]"
            )
        self.console.print()

    def _on_auto_detect(self, p: dict) -> None:
        fp = p.get("fingerprint", {})
        suggested = fp.get("suggested_profile", "?")
        primary = fp.get("primary_language", "?")
        if self.verbose:
            self.console.print(
                f"  [dim]auto-detect[/] primary={primary} → profile={suggested}",
                highlight=False,
            )

    def _on_plan(self, p: dict) -> None:
        plan_text = p.get("plan_text", "").strip()
        if not plan_text:
            return
        self.console.print()
        self.console.print(
            Panel(
                Text(plan_text),
                title="📋 Plan",
                title_align="left",
                border_style="blue",
                padding=(0, 1),
            )
        )
        self.console.print()

    def _on_node_start(self, p: dict) -> None:
        node = p.get("node", "")
        self._last_node = node
        if node == "react":
            self.console.print("[bold]🔍 Investigation[/]")

    def _on_node_end(self, p: dict) -> None:
        # Flush any incomplete token stream
        self._flush_llm_tokens()

    def _on_llm_start(self, p: dict) -> None:
        # Inside react node, LLM "thinks" before each tool call — show inline marker
        self._in_llm_stream = True
        self._token_buffer = []

    def _on_llm_token(self, p: dict) -> None:
        token = p.get("chunk", "")
        if not token:
            return
        self._token_buffer.append(token)
        # Print directly (no buffering for max responsiveness)
        # Dim the thinking text so it visually recedes vs tool calls + findings
        self.console.print(token, end="", style="dim", highlight=False, soft_wrap=True)

    def _on_llm_end(self, p: dict) -> None:
        self._flush_llm_tokens()
        self._in_llm_stream = False

    def _on_llm_usage(self, p: dict) -> None:
        """Per-LLM-call token accounting — real numbers from usage_metadata."""
        from .cost import format_count, format_cost, is_priced

        delta_in = int(p.get("delta_input", 0) or 0)
        delta_out = int(p.get("delta_output", 0) or 0)
        self._tokens_in = int(p.get("total_input", self._tokens_in))
        self._tokens_out = int(p.get("total_output", self._tokens_out))
        self._llm_calls = int(p.get("call_count", self._llm_calls + 1))
        self._last_throughput = float(p.get("throughput_tps", 0.0) or 0.0)
        self._est_cost = float(p.get("est_cost_usd", self._est_cost))
        model = str(p.get("model", "") or "")

        # If we got zero from the provider, stay silent — no point flashing 0s
        if delta_in == 0 and delta_out == 0:
            return

        tps = f"{self._last_throughput:.0f} tok/s" if self._last_throughput > 0 else "—"
        # Suppress $ figure entirely for models we don't have pricing for —
        # better silent than misleading.
        cost_part = f" · {format_cost(self._est_cost)}" if is_priced(model) else ""
        self.console.print(
            f"    [dim]·[/] [dim cyan]tokens[/] "
            f"+{format_count(delta_in)} in, +{format_count(delta_out)} out "
            f"[dim](Σ {format_count(self._tokens_in)} / {format_count(self._tokens_out)} · "
            f"{tps}{cost_part})[/]",
            highlight=False,
        )

    def _flush_llm_tokens(self) -> None:
        if self._token_buffer:
            self.console.print()  # newline after token stream
            self._token_buffer = []

    def _on_tool_start(self, p: dict) -> None:
        self._flush_llm_tokens()
        tool = p.get("tool", "")
        args = p.get("args", {}) or {}
        # Show: → tool_name(key=val, key=val)
        arg_str = ", ".join(f"{k}={_short(v)}" for k, v in args.items())
        self.console.print(f"  [cyan]→[/] [bold]{tool}[/]([dim]{arg_str}[/])")

    def _on_tool_end(self, p: dict) -> None:
        tool = p.get("tool", "")
        preview = p.get("result_preview", "") or ""
        # Show: ✓ first-line-of-result
        first_line = preview.split("\n", 1)[0]
        if len(first_line) > 100:
            first_line = first_line[:100] + "…"
        if tool == "report_finding":
            # Finding is special — we'll get a separate finding_recorded event
            return
        self.console.print(f"    [green]✓[/] [dim]{first_line}[/]")

    def _on_finding_recorded(self, p: dict) -> None:
        title = p.get("title", "(finding)")
        severity = p.get("severity", "")
        file_path = p.get("file_path", "")
        line = p.get("line_start", 0)

        sev_style = _SEV_STYLE.get(severity, "white on grey50")
        glyph = _SEV_GLYPH.get(severity, "⚑")

        if file_path:
            self.console.print(
                f"    {glyph} [{sev_style}] {severity.upper():8} [/] "
                f"[bold]{title}[/]  [dim]({file_path}:{line})[/]"
            )
        else:
            self.console.print(f"    [yellow]⚑[/] [bold]Finding recorded:[/] {title}")

    def _on_findings_dropped(self, p: dict) -> None:
        dropped = p.get("dropped", []) or []
        if not dropped:
            return
        self.console.print()
        self.console.rule("[bold red]⚠ Ungrounded findings dropped[/]", style="red")
        for d in dropped:
            self.console.print(
                f"  [red]✗[/] [bold]{d.get('title', '?')}[/]"
            )
            self.console.print(f"    [dim]reason:[/] {d.get('reason', '')}")
            if d.get("file_path"):
                self.console.print(f"    [dim]claimed file:[/] {d['file_path']}")

    def _on_reflect(self, p: dict) -> None:
        summary = p.get("summary", "").strip()
        observations = p.get("observations", []) or []

        self.console.print()
        self.console.rule("[bold]📊 Reflection[/]", style="cyan")
        if summary:
            self.console.print(f"  {summary}")
        if observations:
            self.console.print("\n  [bold]Systemic observations:[/]")
            for ob in observations:
                self.console.print(f"    • {ob}")

    def _on_session_end(self, p: dict) -> None:
        report = p.get("report", {}) or {}
        findings = report.get("findings", []) or []

        # Render findings as cards (skip if shown elsewhere)
        if findings:
            self.console.print()
            self.console.rule("[bold]Findings[/]", style="cyan")
            for f in findings:
                self._render_finding(f)

        # Footer stats — tool calls, findings, wall time, model, AND token cost
        used = report.get("tool_calls_used", 0)
        budget = report.get("tool_calls_budget", 0)
        duration = report.get("duration_seconds", 0)
        tok_in = int(report.get("total_input_tokens", 0) or 0)
        tok_out = int(report.get("total_output_tokens", 0) or 0)
        cost = float(report.get("est_cost_usd", 0.0) or 0.0)
        calls = int(report.get("llm_call_count", 0) or 0)

        from .cost import format_count, format_cost

        self.console.print()
        self.console.print(
            f"  [dim]session:[/] {used}/{budget} tool calls · "
            f"{len(findings)} findings · "
            f"{duration:.1f}s · "
            f"{report.get('model_used', '')}"
        )
        if tok_in > 0 or tok_out > 0:
            from .cost import is_priced

            avg_tps = (tok_out / duration) if duration > 0 else 0.0
            cost_part = (
                f" · [bold]{format_cost(cost)}[/]"
                if is_priced(report.get("model_used", ""))
                else ""
            )
            self.console.print(
                f"  [dim]tokens:[/]  {format_count(tok_in)} in · "
                f"{format_count(tok_out)} out · "
                f"{calls} LLM call(s) · "
                f"avg {avg_tps:.0f} tok/s"
                f"{cost_part}"
            )
        self.console.print()

    def _render_finding(self, f: dict) -> None:
        sev = f.get("severity", "info")
        title = f.get("title", "")
        path = f.get("file_path", "")
        line = f.get("line_start", "?")
        conf = f.get("confidence", 0)

        badge = Text(f" {sev.upper()} ", style=_SEV_STYLE.get(sev, "white on grey50"))
        glyph = _SEV_GLYPH.get(sev, "•")

        header = Text()
        header.append(f"{glyph} ", style="bold")
        header.append_text(badge)
        header.append(f"  {title}", style="bold")

        body_lines: list[str] = []
        body_lines.append(f"[dim]location:[/] {path}:{line}")
        body_lines.append(f"[dim]hypothesis:[/] {f.get('hypothesis', '')}")
        ev = f.get("evidence", []) or []
        if ev:
            body_lines.append("[dim]evidence:[/]")
            for e in ev[:5]:
                body_lines.append(f"  · {e.get('summary', '')}")
        cc = f.get("counter_considered")
        if cc:
            body_lines.append(f"[dim]counter-considered:[/] {cc}")
        sg = f.get("suggestion")
        if sg:
            body_lines.append(f"[dim]suggestion:[/] {sg}")
        body_lines.append(f"[dim]confidence:[/] {conf:.2f}")

        self.console.print()
        self.console.print(header)
        for line_str in body_lines:
            self.console.print(f"    {line_str}", highlight=False)


# --- JSON / Markdown formatters ----------------------------------------------


def format_as_json(report) -> str:
    return json.dumps(report.model_dump(), indent=2, default=str)


def format_as_markdown(report) -> str:
    lines: list[str] = []
    lines.append("# revio review")
    lines.append("")
    lines.append(f"**Summary:** {report.summary}")
    lines.append("")
    lines.append(f"- Model: `{report.model_used}`")
    lines.append(f"- Tool calls: {report.tool_calls_used}/{report.tool_calls_budget}")
    lines.append(f"- Duration: {report.duration_seconds:.1f}s")
    lines.append(f"- Findings: {len(report.findings)}")
    if report.total_input_tokens > 0 or report.total_output_tokens > 0:
        from .cost import format_count, format_cost, is_priced

        lines.append(
            f"- Tokens: {format_count(report.total_input_tokens)} in / "
            f"{format_count(report.total_output_tokens)} out "
            f"({report.llm_call_count} LLM call(s))"
        )
        if is_priced(report.model_used):
            lines.append(f"- Estimated cost: {format_cost(report.est_cost_usd)}")
    lines.append("")

    if report.systemic_observations:
        lines.append("## Systemic observations")
        for ob in report.systemic_observations:
            lines.append(f"- {ob}")
        lines.append("")

    if report.findings:
        lines.append("## Findings")
        lines.append("")
        for f in report.findings:
            lines.append(f"### [{f.severity.value.upper()}] {f.title}")
            lines.append("")
            lines.append(f"**Location:** `{f.file_path}:{f.line_start}`")
            lines.append("")
            lines.append(f"**Hypothesis:** {f.hypothesis}")
            lines.append("")
            if f.evidence:
                lines.append("**Evidence:**")
                for e in f.evidence:
                    lines.append(f"- {e.summary}")
                lines.append("")
            if f.counter_considered:
                lines.append(f"**Counter-considered:** {f.counter_considered}")
                lines.append("")
            if f.suggestion:
                lines.append(f"**Suggestion:** {f.suggestion}")
                lines.append("")
            lines.append(f"_Confidence: {f.confidence:.2f}_")
            lines.append("")
            lines.append("---")
            lines.append("")
    return "\n".join(lines)


# --- Helpers ------------------------------------------------------------------


def _short(v, limit: int = 60) -> str:
    s = str(v)
    return s if len(s) <= limit else s[:limit] + "…"


@contextmanager
def quiet_stderr():
    """Temporarily suppress stderr (langgraph emits some warnings on shutdown)."""
    saved = sys.stderr
    try:
        sys.stderr = open("/dev/null", "w")
        yield
    finally:
        try:
            sys.stderr.close()
        except Exception:
            pass
        sys.stderr = saved
