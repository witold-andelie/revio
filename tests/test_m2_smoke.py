"""End-to-end M2 smoke test with mocked LLM.

Verifies the full Layer-1/Layer-2 toolchain plus grounding validator on
the js_sample fixture:

- list_files (universal)
- run_oxlint (Layer 2)
- find_duplicate_groups (Layer 1 via FunctionIndex)
- read_file (universal)
- report_finding (universal) — must cite a file actually read

Findings that reference unread files are dropped by the grounding validator.
This test asserts: real findings survive, hallucinated ones are dropped.

Run:
    .venv/bin/python tests/test_m2_smoke.py
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage

from revio.profiles import load_all_profiles


FIXTURE = Path(__file__).parent / "fixtures" / "js_sample"


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


# Phase state for cycling fake LLMs across plan / react / reflect
_PHASE: dict[str, str] = {"current": "plan"}


def _fake_make_llm_factory():
    def fake_make_llm(config, max_tokens=4096):
        phase = _PHASE["current"]
        fake = _FakeLLM()

        if phase == "plan":
            fake.script([
                AIMessage(
                    content=(
                        "1. list_files to map the repo\n"
                        "2. run_oxlint to surface deterministic issues\n"
                        "3. find_duplicate_groups for dedup candidates\n"
                        "4. read suspicious files (server.js for SQL injection)\n"
                        "5. report findings via report_finding"
                    )
                )
            ])
            _PHASE["current"] = "react"
        elif phase == "react":
            fake.script([
                # Turn 1: list_files
                AIMessage(
                    content="Mapping the repo first.",
                    tool_calls=[{"name": "list_files", "args": {"subdir": "."}, "id": "tc_lf"}],
                ),
                # Turn 2: run_oxlint
                AIMessage(
                    content="Now running oxlint for static issues.",
                    tool_calls=[{"name": "run_oxlint", "args": {"relative_path": "."}, "id": "tc_ox"}],
                ),
                # Turn 3: find_duplicate_groups (Layer 1 via FunctionIndex)
                AIMessage(
                    content="Checking for duplicate functions.",
                    tool_calls=[{"name": "find_duplicate_groups", "args": {}, "id": "tc_dup"}],
                ),
                # Turn 4: read the file containing duplicates
                AIMessage(
                    content="Reading utils.js to inspect the duplicates.",
                    tool_calls=[{"name": "read_file", "args": {"relative_path": "src/utils.js"}, "id": "tc_rd1"}],
                ),
                # Turn 5: read server.js
                AIMessage(
                    content="Reading server.js for the SQL injection trace.",
                    tool_calls=[{"name": "read_file", "args": {"relative_path": "src/server.js"}, "id": "tc_rd2"}],
                ),
                # Turn 6: report grounded finding (real file, real line)
                AIMessage(
                    content="Recording SQLi finding.",
                    tool_calls=[{
                        "name": "report_finding",
                        "args": {
                            "file_path": "src/server.js",
                            "line_start": 14,
                            "severity": "critical",
                            "category": "security",
                            "title": "SQL injection in /user/:id handler",
                            "hypothesis": "req.params.id is interpolated into a SQL query via template literal without sanitization",
                            "evidence_summaries": [
                                "read_file showed: const query = `SELECT * FROM users WHERE id = ${id}`",
                                "db.query(query, ...) executes the raw concatenated string",
                                "No sanitize/escape import seen in server.js",
                            ],
                            "counter_considered": "Could mysql2 auto-escape? No, this uses raw query path, not parameterized form",
                            "confidence": 0.95,
                            "suggestion": "Use parameterized: db.query('SELECT * FROM users WHERE id = ?', [id], cb)",
                        },
                        "id": "tc_f1",
                    }],
                ),
                # Turn 7: report dedup finding (real file, real line — utils.js:1)
                AIMessage(
                    content="Recording dedup finding.",
                    tool_calls=[{
                        "name": "report_finding",
                        "args": {
                            "file_path": "src/utils.js",
                            "line_start": 2,
                            "severity": "warning",
                            "category": "redundancy",
                            "title": "Duplicate function: formatUserName == buildDisplayName",
                            "hypothesis": "formatUserName and buildDisplayName have identical structure (same normalized hash) and the same behavior",
                            "evidence_summaries": [
                                "find_duplicate_groups returned them in the same group",
                                "read_file confirmed both functions interpolate the two params and call .trim()",
                            ],
                            "counter_considered": "None — bodies are syntactically identical save for identifier names",
                            "confidence": 0.92,
                            "suggestion": "Keep one (formatUserName), remove the other, update imports",
                        },
                        "id": "tc_f2",
                    }],
                ),
                # Turn 8: HALLUCINATED finding (file never read) — must be dropped
                AIMessage(
                    content="Recording one more finding.",
                    tool_calls=[{
                        "name": "report_finding",
                        "args": {
                            "file_path": "src/auth.js",  # DOES NOT EXIST in fixture
                            "line_start": 42,
                            "severity": "critical",
                            "category": "security",
                            "title": "JWT verification bypass",
                            "hypothesis": "auth.js uses algorithm:none in jwt.verify",
                            "evidence_summaries": ["common pattern in Express apps"],
                            "confidence": 0.7,
                        },
                        "id": "tc_f3_hallucinated",
                    }],
                ),
                # Turn 9: stop
                AIMessage(content="Investigation complete."),
            ])
            _PHASE["current"] = "reflect"
        elif phase == "reflect":
            fake.script([
                AIMessage(content=(
                    '```json\n'
                    '{\n'
                    '  "summary": "Found 1 critical SQLi in server.js and 1 duplicate function pair in utils.js.",\n'
                    '  "systemic_observations": [\n'
                    '    "Direct DB query interpolation without parameterization",\n'
                    '    "AI-generated duplicate functions in utility module"\n'
                    '  ]\n'
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
        AgentConfig, Config, FixConfig, LLMConfig, OutputConfig, ProfileConfig
    )

    cfg = Config(
        llm=LLMConfig(provider="anthropic", api_key="fake", model="claude-sonnet-4-5"),
        agent=AgentConfig(max_tool_calls=20, checkpoint_dir=str(Path(tempfile.gettempdir()) / "revio_m2_smoke")),
        profile=ProfileConfig(default="js"),
        output=OutputConfig(),
        fix=FixConfig(),
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
    print("revio M2 smoke test (JS profile + grounding)")
    print("=" * 70)

    t0 = time.time()
    report, events = asyncio.run(run_smoke())
    dt = time.time() - t0

    event_types = [e[0] for e in events]
    tool_starts = [e[1].get("tool", "") for e in events if e[0] == "tool_start"]
    print(f"\nTools invoked ({len(tool_starts)}):  {tool_starts}")

    # JS-specific tool coverage
    required_tools = {"list_files", "run_oxlint", "find_duplicate_groups", "read_file", "report_finding"}
    used_tools = set(tool_starts)
    missing = required_tools - used_tools
    if missing:
        print(f"\n❌ MISSING tool invocations: {missing}")
        return 1

    # Grounded findings should survive (server.js + utils.js were read).
    # Note: as of the M3 auto-emit refactor, run_oxlint also auto-records its
    # own findings (eval, no-unused-vars), so the count is ≥ 2 (LLM-emitted)
    # plus however many oxlint surfaces. We assert the SET of files instead.
    if len(report.findings) < 2:
        print(f"\n❌ Expected ≥ 2 grounded findings, got {len(report.findings)}")
        for f in report.findings:
            print(f"   · {f.title} → {f.file_path}:{f.line_start}")
        return 1

    real_paths = {f.file_path for f in report.findings}
    if "src/auth.js" in real_paths:
        print("\n❌ HALLUCINATED finding (src/auth.js) was not dropped")
        return 1

    # Every finding must reference a file that was actually read (server.js
    # and utils.js are the only files the mock reads).
    expected_files = {"src/server.js", "src/utils.js"}
    if not real_paths.issubset(expected_files):
        print(f"\n❌ Findings reference unread files: {real_paths - expected_files}")
        return 1

    # Verify the two LLM-emitted findings are present
    llm_titles = {f.title for f in report.findings if f.detected_by == "agent"}
    if not any("SQL injection" in t for t in llm_titles):
        print(f"\n❌ Expected SQL injection finding from LLM, got: {llm_titles}")
        return 1
    if not any("Duplicate function" in t for t in llm_titles):
        print(f"\n❌ Expected duplicate function finding from LLM, got: {llm_titles}")
        return 1

    # Grounding validator should have dropped the hallucinated one
    dropped_events = [e[1] for e in events if e[0] == "findings_dropped"]
    if not dropped_events:
        print("\n❌ Expected a findings_dropped event for src/auth.js")
        return 1
    dropped_titles = [d["title"] for ev in dropped_events for d in ev.get("dropped", [])]
    if not any("JWT" in t for t in dropped_titles):
        print(f"\n❌ Hallucinated JWT finding not in dropped list: {dropped_titles}")
        return 1

    # Reflect output present
    if not report.summary:
        print("\n❌ reflect summary empty")
        return 1
    if len(report.systemic_observations) < 1:
        print("\n❌ no systemic observations")
        return 1

    print(f"\n✓ Findings kept ({len(report.findings)}):")
    for f in report.findings:
        print(f"   [{f.severity.value:8}] {f.file_path}:{f.line_start}  {f.title}")
    print(f"\n✓ Findings dropped (hallucinated): {len(dropped_titles)}")
    for t in dropped_titles:
        print(f"   ✗ {t}")
    print(f"\n✓ Tool coverage: all of {sorted(required_tools)} invoked")
    print(f"\n✓ Reflect:")
    print(f"   summary: {report.summary}")
    for obs in report.systemic_observations:
        print(f"   · {obs}")
    print(f"\n✓ Session: {report.tool_calls_used}/{report.tool_calls_budget} calls · {dt:.2f}s")
    print("\n✓ ALL M2 CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
