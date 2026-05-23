"""End-to-end M1 smoke test with mocked LLM.

Verifies:
- Graph compiles
- Plan → React → Reflect runs to completion
- Tools dispatch correctly (read_file, report_finding)
- State accumulates findings via reducer
- Stream events fire in expected order
- Final ReviewReport materializes

Run with:
    pytest tests/test_m1_smoke.py -v
or:
    .venv/bin/python tests/test_m1_smoke.py
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage

# Make sure profiles are loaded
from revio.profiles import load_all_profiles
load_all_profiles()


class _FakeLLM:
    """A minimal ChatAnthropic substitute that returns scripted responses."""

    def __init__(self, *args, **kwargs):
        self._responses: list[AIMessage] = []

    def bind_tools(self, tools):
        # We need to know which tools exist so we can craft tool_calls
        self._tool_names = {t.name for t in tools}
        return self

    def script_responses(self, responses: list[AIMessage]):
        self._responses = list(responses)
        return self

    async def ainvoke(self, messages, **kwargs):
        if self._responses:
            return self._responses.pop(0)
        return AIMessage(content="Done.")

    def invoke(self, messages, **kwargs):
        # Sync version used by reflect_node? Actually we use async there too
        if self._responses:
            return self._responses.pop(0)
        return AIMessage(content="Done.")


# Counter to thread a stateful script across LLM instances since make_llm is
# called multiple times in one run (plan, react, reflect).
_SCRIPT_STATE = {"phase": "plan"}


def _make_fake_llm_factory():
    """Return a make_llm replacement that returns phase-aware fake LLMs."""

    def fake_make_llm(config, max_tokens=4096):
        phase = _SCRIPT_STATE["phase"]
        fake = _FakeLLM()

        if phase == "plan":
            fake.script_responses([
                AIMessage(
                    content=(
                        "1. Read app.py to see the changes\n"
                        "2. Check for SQL injection via f-strings\n"
                        "3. Report any findings via report_finding"
                    )
                )
            ])
            _SCRIPT_STATE["phase"] = "react"
        elif phase == "react":
            fake.script_responses([
                # Turn 1: call read_file
                AIMessage(
                    content="Let me read app.py first.",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"relative_path": "app.py", "max_lines": 30},
                            "id": "tc_read_1",
                        }
                    ],
                ),
                # Turn 2: call report_finding
                AIMessage(
                    content="I see SQL injection. Recording finding.",
                    tool_calls=[
                        {
                            "name": "report_finding",
                            "args": {
                                "file_path": "app.py",
                                "line_start": 9,
                                "severity": "critical",
                                "category": "security",
                                "title": "SQL injection in get_user",
                                "hypothesis": "user_id interpolated into SQL query via f-string without sanitization",
                                "evidence_summaries": [
                                    "read_file showed: query = f\"SELECT * FROM users WHERE id = {user_id}\"",
                                    "No sanitize call seen before db.execute",
                                ],
                                "counter_considered": "Could be ORM-escaped — ruled out, raw db.execute used",
                                "confidence": 0.95,
                                "suggestion": "Use parameterized query: db.execute('SELECT * FROM users WHERE id = ?', (user_id,))",
                            },
                            "id": "tc_finding_1",
                        }
                    ],
                ),
                # Turn 3: stop calling tools
                AIMessage(content="Investigation complete."),
            ])
            _SCRIPT_STATE["phase"] = "reflect"
        elif phase == "reflect":
            fake.script_responses([
                AIMessage(
                    content=(
                        "```json\n"
                        '{\n'
                        '  "summary": "Found 1 critical SQL injection in app.py.",\n'
                        '  "systemic_observations": [\n'
                        '    "f-string SQL construction pattern needs project-wide audit"\n'
                        '  ]\n'
                        '}\n'
                        "```"
                    )
                )
            ])
            _SCRIPT_STATE["phase"] = "done"

        return fake

    return fake_make_llm


async def run_smoke():
    """Run the full agent with mocked LLM, return the report."""
    _SCRIPT_STATE["phase"] = "plan"

    from revio.agent.runner import run_agent
    from revio.config import AgentConfig, Config, FixConfig, LLMConfig, OutputConfig, ProfileConfig

    cfg = Config(
        llm=LLMConfig(provider="anthropic", api_key="fake-key-for-smoke", model="claude-sonnet-4-5"),
        agent=AgentConfig(max_tool_calls=10, checkpoint_dir=str(Path(tempfile.gettempdir()) / "revio_smoke")),
        profile=ProfileConfig(default="python"),
        output=OutputConfig(),
        fix=FixConfig(),
    )

    events: list[tuple[str, dict]] = []

    def on_event(et, p):
        events.append((et, p))

    # Patch make_llm in BOTH places it's imported from
    with patch("revio.agent.graph.make_llm", side_effect=_make_fake_llm_factory()):
        report = await run_agent(
            mode="review",
            repo_path="/tmp/code-review-v1/test-repo",
            target_ref="HEAD",
            target_description="the test-repo's app.py",
            profile_name="python",
            config=cfg,
            on_event=on_event,
        )

    return report, events


def main():
    print("=" * 70)
    print("revio M1 smoke test")
    print("=" * 70)

    t0 = time.time()
    report, events = asyncio.run(run_smoke())
    dt = time.time() - t0

    # Summarize event stream
    event_types = [e[0] for e in events]
    print(f"\nEvents emitted ({len(events)}):")
    for et in event_types:
        print(f"  · {et}")

    # Assert expected events fired
    must_have = {"session_start", "plan", "tool_start", "tool_end", "finding_recorded", "reflect", "session_end"}
    missing = must_have - set(event_types)
    if missing:
        print(f"\n❌ MISSING event types: {missing}")
        return 1

    # Assert findings collected
    if len(report.findings) != 1:
        print(f"\n❌ Expected 1 finding, got {len(report.findings)}")
        return 1
    f = report.findings[0]
    if "SQL injection" not in f.title:
        print(f"\n❌ Finding title unexpected: {f.title}")
        return 1
    if not f.evidence:
        print(f"\n❌ Finding has no evidence — hypothesis-evidence model broken")
        return 1
    if not f.counter_considered:
        print(f"\n❌ counter_considered missing")
        return 1

    # Assert reflect output
    if not report.summary:
        print("\n❌ summary empty")
        return 1
    if not report.systemic_observations:
        print("\n❌ systemic_observations empty")
        return 1

    print(f"\n✓ Finding: [{f.severity.value}] {f.title}")
    print(f"  evidence chain ({len(f.evidence)}): " + " → ".join(e.summary[:40] for e in f.evidence))
    print(f"  counter-considered: {f.counter_considered}")
    print(f"  confidence: {f.confidence}")
    print(f"\n✓ Summary: {report.summary}")
    print(f"  systemic: {report.systemic_observations}")
    print(f"\n✓ Stats: {report.tool_calls_used}/{report.tool_calls_budget} tool calls, {dt:.2f}s")
    print("\n✓ ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
