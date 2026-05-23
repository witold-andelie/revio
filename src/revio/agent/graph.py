"""LangGraph definition: plan → react_loop → reflect."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

logger = logging.getLogger(__name__)

from ..config import Config
from ..output.models import Finding
from .grounding import collect_tool_facts, sanitize_plan_text, validate_findings
from .llm import make_llm
from .prompts import PLAN_PROMPT, REACT_INTRO_PROMPT, REFLECT_PROMPT, SYSTEM_PROMPT
from .state import AgentState
from .tool_context import ToolContext
from .tools import (
    make_list_files_tool,
    make_load_skill_tool,
    make_read_file_tool,
    make_search_guidelines_tool,
    report_finding,
)


# --- Node: plan ---------------------------------------------------------------


async def plan_node(state: AgentState, config) -> dict:
    """Generate a short plan via single LLM call (visible to user)."""
    cfg: Config = config["configurable"]["app_config"]
    llm = make_llm(cfg, max_tokens=600)

    prompt = PLAN_PROMPT.format(
        mode=state.get("mode", "review"),
        target_description=state.get("target_description", "the current diff"),
        repo_path=state.get("repo_path", "."),
        profile_name=state.get("profile_name", "auto"),
        budget_max=state.get("tool_calls_budget", 15),
    )

    response = await llm.ainvoke([HumanMessage(content=prompt)])
    raw_plan = _content_to_text(response.content).strip()

    # Strip any fabricated <tool_call>/<tool_response> markup before passing
    # the plan to react. Some models write fake ReAct traces in the plan body
    # and then treat them as real evidence in later turns.
    plan_text = sanitize_plan_text(raw_plan)

    # Build skills section for the system prompt (progressive disclosure)
    skills_section = _build_skills_section(state)

    # Seed the messages list with system + initial human turn for react loop
    system = SYSTEM_PROMPT.format(
        mode=state.get("mode", "review"),
        repo_path=state.get("repo_path", "."),
        profile_name=state.get("profile_name", "auto"),
        profile_hints=(state.get("profile_hints", "") + skills_section),
        budget_max=state.get("tool_calls_budget", 15),
    )

    initial_user = (
        f"# Plan (strategy only, no tools called yet)\n\n"
        f"{plan_text}\n\n"
        f"---\n\n"
        f"{REACT_INTRO_PROMPT}"
    )

    return {
        "plan": plan_text,
        "messages": [SystemMessage(content=system), HumanMessage(content=initial_user)],
    }


def _build_skills_section(state: AgentState) -> str:
    """Assemble the Skills catalog + auto-activated bodies for the system prompt."""
    try:
        ctx = ToolContext(
            repo_root=Path(state["repo_path"]),
            profile_name=state.get("profile_name", "auto"),
        )
        all_skills = ctx.skills_registry.all()
        if not all_skills:
            return ""

        activations = ctx.activated_skills
        activated_names = {a.skill.name for a in activations}

        parts: list[str] = ["\n\n## Skills available\n"]
        parts.append(
            "You can call `load_skill(name)` to expand any of these into the conversation. "
            "Skills marked **[auto-loaded]** are already in your context below.\n"
        )
        for s in all_skills:
            tag = " **[auto-loaded]**" if s.name in activated_names else ""
            parts.append(f"- `{s.name}`{tag}: {s.description}")

        if activations:
            parts.append("\n\n## Auto-loaded skills\n")
            for act in activations:
                reasons = ", ".join(act.matched_rules)
                body = act.skill.load_body().strip()
                # Cap body size so prompt doesn't blow up
                if len(body) > 2000:
                    body = body[:2000] + "\n... (truncated; call load_skill to read the rest)"
                parts.append(
                    f"\n### {act.skill.name}\n*(activated because: {reasons})*\n\n{body}\n"
                )

        return "\n".join(parts)
    except Exception as e:
        logger.warning("skills section build failed: %s", e)
        return ""


# --- Node: react_loop ---------------------------------------------------------


async def react_node(state: AgentState, config) -> dict:
    """Run the tool-calling loop until the LLM stops or budget hits zero."""
    cfg: Config = config["configurable"]["app_config"]
    repo_root = Path(state["repo_path"])
    profile_name = state.get("profile_name", "auto")

    # Shared per-session context (RAG / parser indexes / static analyzers)
    ctx = ToolContext(repo_root=repo_root, profile_name=profile_name or "auto")

    # Universal tools (every profile gets these)
    list_files_tool = make_list_files_tool(repo_root)
    read_file_tool = make_read_file_tool(repo_root)
    search_guidelines_tool = make_search_guidelines_tool(ctx)
    load_skill_tool = make_load_skill_tool(ctx)
    tools = [
        list_files_tool,
        read_file_tool,
        search_guidelines_tool,
        load_skill_tool,
        report_finding,
    ]

    # Profile-specific tools (Layer 1 + Layer 2)
    if profile_name and profile_name != "auto":
        try:
            from ..profiles import get_profile, load_all_profiles

            load_all_profiles()
            profile_cls = get_profile(profile_name)
            if profile_cls is not None:
                profile_tools = profile_cls.make_tools(ctx)
                tools.extend(profile_tools)
        except Exception as e:
            logger.warning("failed to load profile tools (%s): %s", profile_name, e)

    # MCP tools (from configured MCP servers, bridged in runner.run_agent)
    mcp_tools = (config.get("configurable", {}) or {}).get("mcp_tools", []) if isinstance(config, dict) else []
    if mcp_tools:
        tools.extend(mcp_tools)

    tools_by_name = {t.name: t for t in tools}

    llm = make_llm(cfg, max_tokens=4096)
    llm_with_tools = llm.bind_tools(tools)

    messages = list(state.get("messages", []))
    budget_max = state.get("tool_calls_budget", 15)
    used = state.get("tool_calls_used", 0)
    iteration = state.get("iteration", 0)

    # New findings collected in this node call (state reducer will concat)
    new_findings: list[Finding] = []

    while True:
        iteration += 1
        if iteration > 30:
            messages.append(AIMessage(content="[safety] Iteration limit hit."))
            break

        try:
            response = await llm_with_tools.ainvoke(messages)
        except Exception as e:
            # API error mid-session (e.g. strict tool-call pairing on OpenAI-
            # compat endpoints). Preserve findings collected so far rather
            # than crashing the whole run.
            logger.warning("react LLM call failed at iteration %d: %s", iteration, e)
            messages.append(
                AIMessage(content=f"[api-error] {type(e).__name__}: {str(e)[:200]}")
            )
            break

        messages.append(response)

        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break  # LLM stopped calling tools

        if used >= budget_max:
            messages.append(
                AIMessage(content="[budget] Tool-call budget exhausted, stopping investigation.")
            )
            break

        # CRITICAL: every tool_call in the AIMessage we just appended MUST
        # receive a paired ToolMessage before the next invoke. OpenAI-compat
        # providers (DeepSeek, OpenRouter) reject the next call otherwise.
        # So we iterate without `break` on budget exhaustion mid-batch — we
        # process every tool_call in this batch, then break afterwards.
        for tc in tool_calls:
            tcid = tc.get("id", "")
            name = tc.get("name", "")
            args = tc.get("args", {}) or {}

            # Always increment usage (even on errors, so budget reflects API cost)
            used += 1

            tool = tools_by_name.get(name)
            if tool is None:
                messages.append(
                    ToolMessage(content=f"Unknown tool: {name}", tool_call_id=tcid)
                )
                continue

            try:
                result = await tool.ainvoke(args)
            except Exception as e:
                messages.append(
                    ToolMessage(content=f"Tool error: {e}", tool_call_id=tcid)
                )
                continue

            # Special case: report_finding returns a Command with state update.
            # The Finding objects flow into state via the reducer; the chat
            # history gets a human-readable acknowledgement.
            if hasattr(result, "update") and "findings" in (result.update or {}):
                findings = result.update["findings"]
                new_findings.extend(findings)
                title = findings[0].title if findings else "(unnamed)"
                messages.append(
                    ToolMessage(content=f"Recorded finding: {title}", tool_call_id=tcid)
                )
            else:
                messages.append(
                    ToolMessage(content=str(result), tool_call_id=tcid)
                )

        # After processing the WHOLE batch, decide whether to continue.
        # No mid-batch break — that would leave tool_calls without responses.
        if used >= budget_max:
            messages.append(
                AIMessage(content="[budget] Tool-call budget exhausted after this batch.")
            )
            break

    # Grounding validation — drop or downgrade findings that don't trace to
    # actual tool calls. This is the M1-bug fix.
    facts = collect_tool_facts(messages)
    grounded, dropped = validate_findings(new_findings, facts, drop_ungrounded=True)

    return {
        "messages": messages,
        "tool_calls_used": used,
        "iteration": iteration,
        "findings": grounded,
        # Dropped findings surface to user but don't count as real findings.
        "dropped_findings": dropped,
    }


# --- Node: reflect ------------------------------------------------------------


async def reflect_node(state: AgentState, config) -> dict:
    """Look across all findings, produce summary + systemic observations."""
    cfg: Config = config["configurable"]["app_config"]
    llm = make_llm(cfg, max_tokens=800)

    findings = state.get("findings", []) or []

    # Build a concise findings dump for the reflect prompt
    findings_dump = "\n".join(
        f"- [{f.severity.value}] {f.title} ({f.file_path}:{f.line_start}) "
        f"— {f.hypothesis[:120]}"
        for f in findings
    ) or "(no findings)"

    prompt = REFLECT_PROMPT.format(
        n_findings=len(findings),
        used=state.get("tool_calls_used", 0),
        budget=state.get("tool_calls_budget", 0),
    )
    prompt += f"\n\nFindings list:\n{findings_dump}"

    response = await llm.ainvoke([HumanMessage(content=prompt)])
    raw = _content_to_text(response.content)

    summary = ""
    obs: list[str] = []
    try:
        # Extract JSON from possible markdown code block
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            summary = data.get("summary", "")
            obs = data.get("systemic_observations", []) or []
    except (json.JSONDecodeError, AttributeError):
        summary = raw.strip()[:300]

    if not summary:
        summary = f"Found {len(findings)} issue(s)."

    return {
        "summary": summary,
        "systemic_observations": obs,
    }


# --- Helpers ------------------------------------------------------------------


def _content_to_text(content) -> str:
    """Normalize ChatAnthropic response content to plain text.

    Anthropic returns str; some compat providers return list-of-blocks
    (text + thinking interleaved).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


# --- Graph builder ------------------------------------------------------------


def build_graph(checkpointer=None):
    """Build the agent graph. Pass an optional checkpointer for persistence."""
    workflow = StateGraph(AgentState)
    workflow.add_node("plan", plan_node)
    workflow.add_node("react", react_node)
    workflow.add_node("reflect", reflect_node)

    workflow.add_edge(START, "plan")
    workflow.add_edge("plan", "react")
    workflow.add_edge("react", "reflect")
    workflow.add_edge("reflect", END)

    return workflow.compile(checkpointer=checkpointer)
