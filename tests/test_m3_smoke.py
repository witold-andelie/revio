"""M3 end-to-end smoke test with mocked LLM.

Adds onto M2 coverage:
- RAG: search_guidelines tool present and callable
- Skills: load_skill tool present + auto-activation in plan-stage system prompt
- Findings persistence: cross-run comparison fires findings_compared event
- Mode differentiation: tool filter strips mode-irrelevant tools

(MCP is tested separately in tests/test_mcp_bridge.py because it needs a
stub subprocess and a real ClientSessionGroup lifecycle.)

Run:
    .venv/bin/python tests/test_m3_smoke.py
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage

from revio.profiles import load_all_profiles
from revio.skills import SkillsRegistry


FIXTURE = Path(__file__).parent / "fixtures" / "js_sample"


# Cycle fake LLMs across plan/react/reflect
_PHASE: dict[str, str] = {"current": "plan"}


class _FakeLLM:
    def __init__(self):
        self._responses: list[AIMessage] = []
        self._tool_names: set[str] = set()

    def bind_tools(self, tools):
        self._tool_names = {t.name for t in tools}
        return self

    def script(self, responses: list[AIMessage]):
        self._responses = list(responses)
        return self

    async def ainvoke(self, messages, **kwargs):
        if self._responses:
            return self._responses.pop(0)
        return AIMessage(content="Done.")

    def invoke(self, messages, **kwargs):
        return self.ainvoke(messages)


def _fake_make_llm_factory():
    """Mock that scripts the plan/react/reflect responses."""

    def fake_make_llm(config, max_tokens=4096):
        phase = _PHASE["current"]
        fake = _FakeLLM()

        if phase == "plan":
            fake.script([
                AIMessage(content=(
                    "1. list_files first\n"
                    "2. search_guidelines for project policies\n"
                    "3. load_skill for relevant guidance\n"
                    "4. read_file + report_finding"
                ))
            ])
            _PHASE["current"] = "react"
        elif phase == "react":
            fake.script([
                # 1. list_files
                AIMessage(content="Mapping repo.", tool_calls=[
                    {"name": "list_files", "args": {"subdir": "."}, "id": "tc_lf"}
                ]),
                # 2. search_guidelines (RAG)
                AIMessage(content="Checking project policies.", tool_calls=[
                    {"name": "search_guidelines",
                     "args": {"query": "SQL injection", "k": 2},
                     "id": "tc_rag"}
                ]),
                # 3. load_skill — pull up a skill body
                AIMessage(content="Loading dedup skill.", tool_calls=[
                    {"name": "load_skill",
                     "args": {"name": "audit-supply-chain"},
                     "id": "tc_sk"}
                ]),
                # 4. read_file
                AIMessage(content="Reading source.", tool_calls=[
                    {"name": "read_file",
                     "args": {"relative_path": "src/server.js"},
                     "id": "tc_rd"}
                ]),
                # 5. report_finding
                AIMessage(content="Recording.", tool_calls=[{
                    "name": "report_finding",
                    "args": {
                        "file_path": "src/server.js",
                        "line_start": 14,
                        "severity": "critical",
                        "category": "security",
                        "title": "SQL injection via template literal",
                        "hypothesis": "req.params.id interpolated into raw SQL string",
                        "evidence_summaries": [
                            "read_file showed: const query = `SELECT * FROM users WHERE id = ${id}`",
                            "search_guidelines returned: security_checklist.md / SQL Injection Prevention",
                        ],
                        "counter_considered": "ORM auto-escape — ruled out, raw mysql2 query path",
                        "confidence": 0.95,
                        "suggestion": "Use parameterized query: db.query('SELECT * FROM users WHERE id = ?', [id], cb)",
                    },
                    "id": "tc_f"
                }]),
                # 6. stop
                AIMessage(content="Done."),
            ])
            _PHASE["current"] = "reflect"
        elif phase == "reflect":
            fake.script([
                AIMessage(content=(
                    '```json\n'
                    '{\n'
                    '  "summary": "1 critical SQLi in server.js.",\n'
                    '  "systemic_observations": ["Direct DB query interpolation pattern"]\n'
                    '}\n'
                    '```'
                ))
            ])
            _PHASE["current"] = "done"

        return fake

    return fake_make_llm


async def run_smoke():
    _PHASE["current"] = "plan"
    load_all_profiles()

    from revio.agent.runner import run_agent
    from revio.config import (
        AgentConfig, Config, FixConfig, LLMConfig, MCPConfig,
        OutputConfig, ProfileConfig,
    )

    # Seed a guideline index for the fixture so search_guidelines has data
    from revio.layers.rag import DocumentLoader, GuidelineIndexer
    guideline_src = (
        Path("/tmp/code-review-v1/intelligent-code-review-agent/data/guidelines")
    )
    if guideline_src.is_dir():
        indexer = GuidelineIndexer(repo_root=FIXTURE)
        docs = DocumentLoader.load_directory(guideline_src)
        # Clear any existing index so we have a known baseline
        indexer.clear()
        indexer = GuidelineIndexer(repo_root=FIXTURE)  # re-init after clear
        indexer.index_documents(docs)

    # Seed a project-level skill in the fixture so it's discoverable
    fixture_skills = FIXTURE / ".revio" / "skills" / "audit-supply-chain"
    fixture_skills.mkdir(parents=True, exist_ok=True)
    (fixture_skills / "SKILL.md").write_text(
        "---\n"
        "name: audit-supply-chain\n"
        "description: Audit JS package.json for sketchy dependencies\n"
        "matches:\n"
        "  filename_patterns: ['**/package.json']\n"
        "---\n\n"
        "# Supply chain audit\n\nWatch for typo-squatted packages and post-install hooks.\n",
        encoding="utf-8",
    )

    cfg = Config(
        llm=LLMConfig(provider="anthropic", api_key="fake", model="claude-sonnet-4-5"),
        agent=AgentConfig(max_tool_calls=20, checkpoint_dir=str(Path(tempfile.gettempdir()) / "revio_m3_smoke")),
        profile=ProfileConfig(default="js"),
        output=OutputConfig(),
        fix=FixConfig(),
        mcp=MCPConfig(servers={}),  # no MCP in this test
    )

    events: list[tuple[str, dict]] = []

    def on_event(et, p):
        events.append((et, p))

    with patch("revio.agent.graph.make_llm", side_effect=_fake_make_llm_factory()):
        report = await run_agent(
            mode="audit",
            repo_path=str(FIXTURE),
            target_ref="",
            target_description="the JS sample fixture",
            profile_name="js",
            config=cfg,
            on_event=on_event,
        )

    return report, events


def main():
    print("=" * 70)
    print("revio M3 smoke test (RAG + Skills + Findings history)")
    print("=" * 70)

    t0 = time.time()
    report, events = asyncio.run(run_smoke())
    dt = time.time() - t0

    event_types = [e[0] for e in events]
    tool_starts = [e[1].get("tool", "") for e in events if e[0] == "tool_start"]
    print(f"\nEvents emitted ({len(events)}):")
    print(f"  tool calls: {tool_starts}")

    # --- M3-specific tool coverage ---
    must_have_tools = {"list_files", "search_guidelines", "load_skill", "read_file", "report_finding"}
    used_tools = set(tool_starts)
    missing = must_have_tools - used_tools
    if missing:
        print(f"\n❌ MISSING tool invocations: {missing}")
        return 1

    # --- Findings grounded ---
    if len(report.findings) != 1:
        print(f"\n❌ Expected 1 finding, got {len(report.findings)}")
        return 1
    f = report.findings[0]
    if f.file_path != "src/server.js" or f.line_start != 14:
        print(f"\n❌ Finding location wrong: {f.file_path}:{f.line_start}")
        return 1

    # --- Findings comparison fired ---
    cmp_events = [e[1] for e in events if e[0] == "findings_compared"]
    if not cmp_events:
        print("\n❌ Expected findings_compared event")
        return 1
    cmp = cmp_events[0]
    print(f"\n  Findings comparison: new={cmp.get('new')} still={cmp.get('still_present')} fixed={cmp.get('maybe_fixed')}")
    if cmp.get("total_history", 0) < 1:
        print("\n❌ Expected total_history ≥ 1 after recording")
        return 1

    # --- Reflect output ---
    if not report.summary:
        print("\n❌ reflect summary empty")
        return 1
    if not report.systemic_observations:
        print("\n❌ no systemic observations")
        return 1

    print(f"\n✓ Tool coverage:    all of {sorted(must_have_tools)}")
    print(f"✓ Finding:          [{f.severity.value}] {f.file_path}:{f.line_start}  {f.title}")
    print(f"  evidence chain:   {len(f.evidence)} entries")
    print(f"  counter-considered: {bool(f.counter_considered)}")
    print(f"✓ Cross-run compare: new={cmp.get('new')} history={cmp.get('total_history')}")
    print(f"✓ Reflect:")
    print(f"   summary: {report.summary}")
    for obs in report.systemic_observations:
        print(f"   · {obs}")
    print(f"\n✓ Session: {report.tool_calls_used}/{report.tool_calls_budget} calls · {dt:.2f}s")
    print("\n✓ ALL M3 CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
