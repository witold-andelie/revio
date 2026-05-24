# revio

**Agentic code review CLI** with cross-language deep tooling. Combines
LangGraph-orchestrated LLM reasoning with deterministic static analyzers
(oxlint / bandit / clippy / spotbugs / golangci-lint / cppcheck + 30 PLC rules)
across **17 language profiles**.

```
$ revio review --commit HEAD
🔍 auth.js ──────────────────────────────────────────────
  → read_file(auth.js:42)
  💭 Line 42 builds SQL via template literal. Where does user_id come from?
  → get_call_sites(getUserById)
  💭 Called from POST /user/:id handler with req.params.id
  ⚠️ CRITICAL  SQL injection at auth.js:42  (confidence 0.95)
     Evidence chain: req.params.id → getUserById → `WHERE id = ${id}`
     Counter-considered: ORM auto-escape — ruled out, raw query path
```

## Features at a glance

| | What it does | Tools |
|---|---|---|
| **3 modes** | `review` (diff) · `audit` (full repo) · `dedup` (find AI redundancy) | Per-mode prompts + tool whitelist |
| **17 language profiles** | JS / TS / Python / Rust / Java / Go / C/C++ + 10 generic + PLC + 9 LLM-only | Auto-detected from repo content |
| **6 static analyzers** | oxlint · bandit · clippy · spotbugs · golangci-lint · cppcheck | Auto-emit (LLM doesn't have to re-report) |
| **PLC support** | 7 vendor parsers, 30+ PLCopen rules, HW config audit, LD/FBD/SFC → ST | Siemens / Beckhoff / CODESYS / Rockwell / ABB / GE / Omron |
| **RAG** | Index your company's coding guidelines → cited in findings | ChromaDB + sentence-transformers |
| **Skills** | Anthropic Agent Skills spec — dual-layer (project + user) | Progressive disclosure |
| **MCP client** | Connect to external MCP servers (Jira / wiki / etc.) | Official `mcp` SDK |
| **`dedup --fix`** | Actually apply patches (delete duplicates, update imports) | Git stash safety net |
| **Cross-session memory** | "🆕 New since last run: 3" — finding history | SQLite-backed |
| **Multi-LLM** | Anthropic + OpenAI-compat (DeepSeek / Mimo / OpenRouter / Ollama / ...) | Provider-aware factory |
| **Multilingual NL** | REPL accepts any language (en / 中 / de / es / 日 ...) | LLM-based intent classifier |

---

## Install

```bash
git clone <repo>
cd revio
python3 -m venv .venv
.venv/bin/pip install -e ".[js,plc,python,languages]"
```

Optional but strongly recommended (per language):
```bash
brew install oxlint                # JS/TS linting (or: npm i -g oxlint)
brew install cppcheck              # C/C++ analysis
brew install golangci-lint         # Go linting
.venv/bin/pip install bandit       # Python security (already included in [python])
rustup component add clippy        # Rust linting (if you analyze Rust)
brew install spotbugs              # Java analysis (needs JDK + compiled .class)
```

## First run

```bash
.venv/bin/revio
```

Triggers a 6-step setup wizard:

1. **LLM provider** — Anthropic / Mimo / DeepSeek / OpenAI-compatible / Custom
2. **API URL** — auto-filled per provider, override for self-hosted
3. **API key** — masked input, stored with `0600` perms in `~/.config/revio/config.toml`
4. **Model** — preset list per provider, or enter custom
5. **Thinking mode** — required disabled for Mimo / DeepSeek
6. **Default profile** — `auto` (recommended; detects per repo) / `js` / `plc` / `python`

Connection test runs at the end. Config saved to `~/.config/revio/config.toml`.

---

## The 3 modes

### `review` — diff / commit review

```bash
revio review                           # latest commit in current dir
revio review --commit abc1234          # specific commit
revio review path/to/repo --commit HEAD
revio review --format markdown -o review.md
```

Optimized for small diffs. Tool budget tight. Focus: security + correctness in
the changed lines.

### `audit` — full-repo security scan

```bash
revio audit                            # current dir
revio audit path/to/repo
revio audit --profile python           # force a profile
revio audit --budget 30                # spend more tool calls
```

Walks every file. Heavy on Layer 2 (static analyzers run first). LLM adds
semantic context on top.

### `dedup` — find AI-generated redundancy

```bash
revio dedup                            # find redundancy, just report
revio dedup --fix                      # interactive: apply patches
revio dedup --fix --dry-run            # show patches, don't write
revio dedup --fix --yes                # auto-apply high-confidence (CI mode)
revio dedup --fix --yes --min-confidence 0.85   # lower threshold
revio dedup --fix --allow-dirty        # stash uncommitted changes first
```

Looks for: duplicate functions, single-use wrappers, dead code, repeated
templates. Common in AI-generated codebases.

`--fix` flow:
```
🔧 Proposed fix 1 of 4
   [unified diff preview]
   Confidence: 0.92  ·  Affects 3 files
   Apply?  ▸ Yes / No / Explain / Approve-all-remaining / Quit
```

Git safety: refuses to start on a dirty repo unless `--allow-dirty` (which
creates a stash first). After applying, undo via `git reset --hard HEAD`.

---

## Configuration

### View / edit config

```bash
revio config show                      # masked api_key
revio config path                      # print path
revio config edit                      # open in $EDITOR
revio config init                      # re-run wizard
```

### Switching LLM provider

Edit `~/.config/revio/config.toml`:
```toml
[llm]
provider = "openai_compat"             # or "anthropic" / "mimo" / "custom"
api_url = "https://api.deepseek.com"
api_key = "sk-..."
model = "deepseek-chat"
disable_thinking = false
```

Or use the REPL:
```
[sonnet-4-5] (review) › /model deepseek-chat
[sonnet-4-5] (review) › /url https://api.deepseek.com
[sonnet-4-5] (review) › /key
```

### Project-level overrides

Drop `.revio.toml` in your project root:
```toml
[agent]
max_tool_calls = 25
```

This shadows the user-global config. Commit it to share with your team.

---

## Guidelines (RAG)

Index your company's coding standards so the agent cites them as evidence:

```bash
# Add files / directories
revio guidelines add docs/styleguide.md
revio guidelines add team-policies/

# List indexed sources
revio guidelines list

# Test retrieval before running a review
revio guidelines search "SQL injection prevention"

# Reindex everything from .revio/guidelines/
revio guidelines reindex

# Clear the index
revio guidelines clear
```

Supported formats: `.md` `.txt` `.rst` `.adoc` `.pdf` `.docx`.
Index lives at `~/.cache/revio/<repo-hash>/vectorstore/` (per-repo isolation).

During a review, the agent calls `search_guidelines(query)` and cites
relevant chunks in evidence:

```
⚠️ CRITICAL  SQL injection
  Evidence:
    · read_file showed: query = f"SELECT * FROM users WHERE id = {user_id}"
    · search_guidelines returned: security_checklist.md / SQL Injection: ...
```

---

## Skills (Anthropic Agent Skills)

Skills are markdown files with YAML frontmatter that tell the agent *how*
to handle specific scenarios. Dual layer:

- `.revio/skills/<name>/SKILL.md` (project — committable)
- `~/.config/revio/skills/<name>/SKILL.md` (user-global)

Project skills shadow user skills with the same name.

```bash
revio skills list                      # all discovered skills
revio skills show review-react-rsc     # full body
revio skills activated                 # which would auto-fire here
```

Example skill:
```yaml
---
name: review-react-rsc
description: How to review React Server Components for correctness + perf
when_to_use: Reviewing TSX in Next.js app/ directories
matches:
  extensions: [".tsx"]
  imports: ["next"]
  filename_patterns: ["**/app/**/*"]
---

# Reviewing React Server Components

When reviewing RSC code, focus on:
1. Boundary violations: client hooks in server components
2. Sequential await instead of Promise.all
3. ...
```

Auto-activation: skills with matching extensions / imports / frameworks /
filename patterns get their bodies pre-loaded into the agent's context.
Other skills appear in a catalog the LLM can pull via `load_skill(name)`.

---

## MCP integration

revio acts as an **MCP client** — connects to your existing MCP servers
(Jira / Confluence / internal wiki / etc.) and routes their tools to the agent.

Add to `~/.config/revio/config.toml`:
```toml
[mcp.servers.jira]
command = "uvx"
args = ["mcp-server-atlassian-jira"]
env = { ATLASSIAN_TOKEN = "$ATLASSIAN_TOKEN" }
timeout = 5.0

[mcp.servers.company_wiki]
url = "https://wiki.internal/mcp"
api_key_env = "WIKI_TOKEN"
timeout = 10.0
```

At session start, revio connects to all configured servers in parallel
(5-second timeout each), pulls their tool lists, and wraps them as
`mcp_<server>_<tool>` LangChain tools — visible to the agent alongside
revio's built-in toolkit.

Failed servers don't abort the session — they're listed in the stream
output and the agent proceeds without their tools.

### revio as an MCP server

revio is also an **MCP server** — other agents (Claude Code, Cursor, custom
LangGraph workflows) can call revio's pipelines as tools:

```bash
revio mcp-server               # starts stdio MCP server, blocks
```

Tools exposed:

| Tool | Purpose | Cost |
|---|---|---|
| `revio_audit(repo_path, profile, budget)` | Full-repo audit | LLM (~40s) |
| `revio_review(repo_path, base_ref, profile, budget)` | Diff review | LLM (~30s) |
| `revio_dedup(repo_path, profile, budget)` | Find AI redundancy + patches | LLM (~40s) |
| `revio_run_bandit(path)` | Python security scan | Layer 2 only |
| `revio_run_oxlint(path)` | JS/TS lint | Layer 2 only |
| `revio_run_cppcheck(path)` | C/C++ analysis | Layer 2 only |
| `revio_search_guidelines(repo_path, query)` | RAG query | Embedding only |
| `revio_list_profiles()` | Discovery | Instant |
| `revio_detect_profile(repo_path)` | Auto-detect best profile | Instant |

To register with Claude Code, add to `~/.config/claude-code/mcp.json`:

```json
{
  "mcpServers": {
    "revio": {
      "command": "revio",
      "args": ["mcp-server"]
    }
  }
}
```

The MCP server deliberately does NOT expose `--fix` (patch application).
It returns patch *operations* via `revio_dedup` — the host agent
inspects them and decides whether/how to apply, keeping the security
model clean: revio's server only reports, never mutates.

---

## Interactive REPL

```bash
revio                                  # drop into REPL
```

```
revio-v2 (auto profile · sonnet-4-5)
Type / for commands, or describe what you want.

> review the last 3 commits
  ... [agent runs] ...

> 检查这个项目里有没有重复代码
  ... [intent → dedup] ...

> /model deepseek-chat
  ✓ model → deepseek-chat
```

**Slash commands**:

| | |
|---|---|
| `/help` `/?` | List all commands |
| `/model` | Interactive picker (live `/v1/models` + curated catalog) |
| `/model <name>` | Set LLM model directly |
| `/model list`, `/models` | List available models on current endpoint |
| `/url <url>` | Change API endpoint |
| `/key` | Update API key (masked) |
| `/profile <name>` | Switch profile (auto / js / plc / python / ...) |
| `/mode <name>` | Default mode for next NL input |
| `/budget <n>` | Set tool-call budget for this session |
| `/cost` | Real token usage + est. cost (USD) for the REPL session |
| `/config` | Open config file in $EDITOR |
| `/clear` | Clear screen |
| `/history` | Recent REPL commands |
| `/exit` `/quit` | Exit |

**Natural language**: any non-slash input is classified by an intent LLM
call (one of: `review` / `audit` / `dedup` / `chat`). Works in any language
— English / Chinese / German / French / Spanish / Czech / Japanese / ...

---

## Profiles (language packs)

| Profile | Layer 1 (AST) | Layer 2 (static) | Reasoning hints |
|---|---|---|---|
| `js` | Tree-sitter + symbol graph + call graph + dedup index | **oxlint** | React / Next.js / Vue / Prisma patterns |
| `python` | Tree-sitter | **bandit** | SQLi / pickle / shell=True / weak crypto / etc. |
| `rust` | Tree-sitter | **clippy** | unsafe / unwrap / `Rc<RefCell<>>` / clone-in-loop |
| `java` | Tree-sitter | **spotbugs** (needs .class) | XXE / deserialization / hardcoded passwords |
| `go` | Tree-sitter | **golangci-lint** (govet + staticcheck + gosec + 100 more) | shell injection / race conditions / context |
| `cpp` | Tree-sitter | **cppcheck** | buffer overflow / null deref / uninit / use-after-free |
| `plc` | 7 vendor parsers + LD/FBD/SFC converters | **30+ PLCopen rules** + CFG + HW audit | E-Stop / interlocks / fail-safe / timing |
| `generic` | Tree-sitter only (Kotlin, Scala, C#, Ruby, PHP, Swift, Lua, Julia, SQL, Shell) | — | Per-language hints |
| `matlab`, `r`, `verilog`, `sas`, `cobol`, `solidity`, `zig`, `objc`, `dart` | — | — | LLM-only review with 250-450 chars of language-specific hints |

Default: `auto` — `detect_project` walks the repo, counts file extensions,
checks marker files (package.json / pyproject.toml / Cargo.toml / go.mod /
PLC vendor XML headers / etc.), and picks the best-matching profile.

Override:
```bash
revio audit --profile cpp              # force C/C++
revio audit --profile generic          # AST-only fallback
```

---

## Output formats

```bash
revio audit --format stream            # default — colored, streaming (TTY)
revio audit --format json -o report.json
revio audit --format markdown -o report.md
```

Stream events the renderer surfaces:
- `session_start` — banner with mode/profile/model/budget
- `auto_detect` — fingerprint summary
- `mcp_connected` — MCP servers and their tool counts
- `plan` — agent's strategy box
- `tool_start` / `tool_end` — every tool call with truncated result
- `finding_recorded` — severity-badged finding card
- `llm_usage` — per-LLM-call token delta + running totals + throughput
- `reflect` — summary + systemic observations
- `findings_compared` — cross-run delta (new / still_present / maybe_fixed)
- `findings_dropped` — grounding-validator rejections (with reason)
- `session_end` — full report cards

---

## Token usage & cost

revio reads real `usage_metadata` off every LLM response and prints it
inline. After each LLM call:

```
  · tokens +1.2k in, +340 out  (Σ 8.4k / 1.9k · 85 tok/s · $0.011)
```

And in the session footer:

```
  session: 6/6 tool calls · 7 findings · 18.1s · deepseek-v4-pro
  tokens:  12.6k in · 573 out · 5 LLM call(s) · avg 32 tok/s · $0.0040
```

Pricing is fuzzy-matched against an internal table (DeepSeek / Claude /
OpenAI / Mistral / local Ollama). **For models we don't have pricing for
(Xiaomi, new providers, custom endpoints, etc.) the `$` amount is
silently omitted** — token counts and throughput still show, but the
dollar figure disappears rather than misleading you with `$0.00`.

`/cost` in the REPL prints cumulative usage across all NL queries in
the session, also gated on pricing availability.

JSON and markdown outputs include `total_input_tokens`,
`total_output_tokens`, `llm_call_count`, and `est_cost_usd` fields on
`ReviewReport`.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Clean — no findings or only info-severity |
| `1` | Operational error (no config, invalid path, etc.) |
| `2` | At least one CRITICAL finding |
| `130` | Interrupted (Ctrl-C) |

For CI: `revio audit . --format json -o /tmp/report.json` then check the
exit code; `2` should fail the build.

---

## What's special about revio

| | Other LLM review tools | revio |
|---|---|---|
| Static-analysis backbone | One LLM call per file, hopes the model spots it | 6 deterministic analyzers + LLM on top |
| Hallucinated findings | Common (LLM invents file paths) | Grounding validator rejects findings on un-read files |
| Token cost on a large repo | Re-reads whole files in every prompt | Agent pulls only enclosing functions via Tree-sitter |
| Company-specific rules | Generic prompt only | RAG over your own coding guidelines |
| Multi-language depth | Usually one strong language | 6 deeply-supported + 10 AST + 9 LLM-only |
| PLC / industrial control | Not supported | 7 vendor parsers + 30 PLCopen rules + HW audit |
| Configurable LLM | Vendor-locked | Anthropic + OpenAI-compat (DeepSeek / Mimo / Ollama / ...) |
| Cross-session memory | Stateless | SQLite history; "🆕 New since last run" |
| Auto-fix | Text suggestion only | `--fix` actually edits files (with git safety) |

See `docs/ARCHITECTURE.md` for the technical deep dive.

---

## Troubleshooting

**Wizard didn't run**
```bash
rm ~/.config/revio/config.toml         # then `revio`
```

**Connection test fails / model not found**
- DeepSeek / Mimo: set `disable_thinking = true` in `[llm]`
- OpenAI-compat: many gateways have their own model IDs — check provider docs
- Self-hosted: ensure `api_url` ends correctly (some need `/v1`, some don't)

**Findings dropped as "ungrounded"**
The grounding validator dropped findings whose claimed file path was
never actually read by the agent. This is *the* hallucination defense.
If you think it's wrong, run with `REVIO_DEBUG=1` to see the full
tool-call history.

**`revio dedup --fix` says no patches**
The agent didn't call `propose_patch`. Either:
- Strict gateway dropped the tool call (rare — try a different provider)
- The model didn't think a mechanical fix was safe (look at the findings'
  `suggestion` field for the proposed manual fix)

**Tree-sitter import errors**
Missing language grammar — install with `pip install -e ".[languages]"`.

**oxlint / cppcheck / etc. not found**
revio degrades gracefully — if a Layer 2 tool isn't installed, the agent
falls back to AST + LLM reasoning. Install the missing binary to get full
coverage (see the Install section).

---

## License

MIT.

---

## Documentation

- `docs/ARCHITECTURE.md` — system design, key innovations, target market
- `docs/INTERNALS.md` — execution-flow map for maintainers
- `tests/test_*_smoke.py` — runnable end-to-end examples per feature
