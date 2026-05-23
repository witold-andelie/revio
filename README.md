# revio

Agentic code review CLI. Three modes:

- **review** — diff/PR review (default for git changes)
- **audit** — full-repo security audit
- **dedup** — find and (optionally) fix AI-generated redundancy

Built on LangGraph with a 4-layer architecture (Parser / Static / LLM Reasoning / Advanced). Primary target: JavaScript/TypeScript via oxc. PLC and Python supported as profiles.

## Status

**M1** — Skeleton + agent UX foundation. Not yet production-ready.

## Quick start

```bash
pip install -e .[js]
revio                       # First run launches setup wizard
revio review --commit HEAD  # One-shot diff review
revio                       # Interactive REPL with slash commands
```

## Layout

```
src/revio/
├── cli/        # Typer entry + REPL + wizard
├── agent/      # LangGraph graph + state + nodes + tools
├── layers/     # 4-layer capability stack
│   ├── parser/    # L1: AST / CFG / symbol graph
│   ├── static/    # L2: lint / taint / call graph
│   ├── reasoning/ # L3: LLM-driven (lives inside agent nodes)
│   └── advanced/  # L4: opt-in symbolic / SMT / PoC
├── profiles/   # Language packs (js, plc, python)
├── detect/     # Auto-detect project type
└── output/     # Models + streaming/json/markdown formatters
```
