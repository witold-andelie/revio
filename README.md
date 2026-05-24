# revio

**Agentic code review CLI** that combines LangGraph-orchestrated LLM
reasoning with deterministic static analyzers (oxlint / bandit / clippy /
spotbugs / golangci-lint / cppcheck + 30 PLC rules) across
**17 language profiles**.

```
$ revio review --commit HEAD
🔍 auth.js ──────────────────────────────────────────────
  → read_file(auth.js:42)
  💭 Line 42 builds SQL via template literal. Where does user_id come from?
  → get_call_sites(getUserById)
  💭 Called from POST /user/:id with req.params.id
  ⚠️ CRITICAL  SQL injection at auth.js:42  (confidence 0.95)
     Evidence: req.params.id → getUserById → `WHERE id = ${id}`
     Counter-considered: ORM auto-escape — ruled out, raw query path
```

## Features

| | What |
|---|---|
| **3 modes** | `review` (diff) · `audit` (full repo) · `dedup` (find AI redundancy) |
| **17 languages** | JS/TS · Python · Rust · Java · Go · C/C++ · Shell · Lua · SQL · Ruby · PHP · Kotlin · **Verilog/SystemVerilog** · 4 generic · PLC · 8 LLM-only |
| **13 static analyzers** | oxlint · bandit · clippy · spotbugs · golangci-lint · cppcheck · shellcheck · luacheck · sqlfluff · rubocop · phpstan · detekt · **verilator** |
| **Local / self-hosted LLM** | Point at any Ollama / vLLM / private endpoint — code never leaves the machine. **Free + air-gap + FERPA/GDPR-safe.** |
| **PLC support** | 7 vendor parsers · 30+ PLCopen rules · HW audit · LD/FBD/SFC → ST |
| **RAG** | Index your company's coding guidelines, cited inline in findings |
| **Skills** | Anthropic Agent Skills spec, dual-layer (project + user) |
| **MCP** | Client + server — connects to your tools, exposes its own |
| **`dedup --fix` + undo** | Applies patches AND records snapshot history — `revio fix undo` reverts any past session |
| **Cross-session memory** | "🆕 New since last run: 3" — SQLite-backed |
| **Multi-LLM** | Anthropic + OpenAI-compat (DeepSeek / Mimo / OpenRouter / Ollama / ...) |
| **Multilingual REPL** | Any human language (en / 中 / de / es / 日 ...) → English findings |

---

## Install

### One-click

**macOS / Linux** — copy-paste into a terminal:
```bash
curl -sSL https://raw.githubusercontent.com/witold-andelie/revio/main/scripts/install.sh | bash
```

**Windows** — copy-paste into PowerShell:
```powershell
iwr https://raw.githubusercontent.com/witold-andelie/revio/main/scripts/install.ps1 | iex
```

What the installer does:
1. Verifies Python ≥ 3.11 (offers to install via `winget` on Windows if missing)
2. Clones the repo to `~/.local/share/revio` (macOS / Linux) or `%LOCALAPPDATA%\revio` (Windows)
3. Creates a venv inside that directory and pip-installs `revio` with all extras
4. Installs **oxlint / cppcheck / golangci-lint** via your OS package manager if available (graceful skip otherwise — revio falls back to AST + LLM)
5. Adds a `revio` launcher to `~/.local/bin` (macOS / Linux) or `%LOCALAPPDATA%\revio\bin` on PATH (Windows)

Re-run the same command anytime to update to the latest `main`.

### Manual install (any OS)

```bash
git clone https://github.com/witold-andelie/revio.git
cd revio
python3 -m venv .venv
# macOS / Linux:
.venv/bin/pip install -e ".[js,plc,python,languages]"
# Windows PowerShell:
.venv\Scripts\pip install -e ".[js,plc,python,languages]"
```

Or straight from GitHub without cloning:
```bash
pip install "git+https://github.com/witold-andelie/revio.git#egg=revio[js,plc,python,languages]"
```

### Optional static analyzers (per language)

| Language | Tool | macOS / Linux | Windows |
|---|---|---|---|
| JS / TS | oxlint | `brew install oxlint` / `npm i -g oxlint` | `npm i -g oxlint` |
| Python | bandit | (already in `[python]` extra) | (already in `[python]` extra) |
| C / C++ | cppcheck | `brew install cppcheck` / `apt install cppcheck` | `winget install Cppcheck.Cppcheck` |
| Go | golangci-lint | `brew install golangci-lint` | `winget install golangci-lint.golangci-lint` |
| Rust | clippy | `rustup component add clippy` | `rustup component add clippy` |
| Java | spotbugs | `brew install spotbugs` (needs JDK) | download from spotbugs.github.io |
| Shell | shellcheck | `brew install shellcheck` / `apt install shellcheck` | `winget install koalaman.shellcheck` |
| Lua | luacheck | `brew install luacheck` / `luarocks install luacheck` | `scoop install luacheck` |
| SQL | sqlfluff | (auto-installed into revio's venv) | (auto-installed into revio's venv) |
| Ruby | rubocop | `gem install rubocop` | `gem install rubocop` (needs Ruby) |
| PHP | phpstan | `composer global require phpstan/phpstan` | (same; needs PHP + Composer) |
| Kotlin | detekt | `brew install detekt` (needs JDK) | download `detekt-cli` from GitHub |
| Verilog / SystemVerilog | verilator | `brew install verilator` / `apt install verilator` | `scoop install verilator` |

**Missing analyzers don't break anything** — revio detects what's installed and falls back to AST + LLM reasoning for the rest.

---

## First run

```bash
.venv/bin/revio
```

Triggers a 6-step wizard: pick **provider** → **API URL** → **key** →
**model** → **thinking mode** → **default profile**. Tests the
connection, then saves to `~/.config/revio/config.toml`.

---

## The 3 modes

### `review` — diff / commit

```bash
revio review                           # latest commit
revio review --commit abc1234
revio review --format markdown -o review.md
```

Tight tool budget. Focus: security + correctness in the changed lines.

### `audit` — full-repo scan

```bash
revio audit
revio audit --profile python --budget 30
```

Heavy on Layer 2 (static analyzers run first). LLM adds semantic context
on top.

### `dedup` — find AI-generated redundancy

```bash
revio dedup                            # just report
revio dedup --fix                      # interactive: review & apply each patch
revio dedup --fix --dry-run            # preview patches, don't write
revio dedup --fix --yes                # auto-apply high-confidence (CI)
```

Detects duplicate functions, single-use wrappers, dead code, repeated
templates — the LLM-generated patterns. `--fix` refuses to start on a
dirty repo unless `--allow-dirty` (which stashes first).

**Undo any past fix** — every `--fix` session snapshots the affected
files before writing, so you can roll back regardless of git state:

```bash
revio fix history                       # list past sessions
revio fix undo                          # restore from most recent session
revio fix undo 2026-05-24T10-15-32.123_a3f9  # restore a specific one
revio fix show <session_id>             # preview what would be reverted
revio fix clean --older-than-days 14    # purge old snapshots
```

History caps default to 50 sessions / 30 days / 1 MiB per file — tweak
in `~/.config/revio/config.toml` under `[fix_history]`. Works without
git; the git stash path remains as additional safety when present.

---

## Configuration

```bash
revio config show / edit / path / init
```

Or directly in `~/.config/revio/config.toml`:
```toml
[llm]
provider = "openai_compat"             # or "anthropic" / "mimo" / "custom"
api_url = "https://api.deepseek.com"
api_key = "sk-..."
model = "deepseek-v4-pro"
```

For per-project overrides, drop a `.revio.toml` in the repo root —
shadows the user-global config and is meant to be committed.

---

## Local / self-hosted LLM (zero data leaves the box)

revio's `openai_compat` provider works with **any OpenAI-compatible
endpoint** — that's the dominant API standard for self-hosted runtimes.
revio doesn't care whether the model is a 4 GB quantized Qwen on a
laptop, a 70 B Llama on a workstation, **or a full-power 671 B
DeepSeek-V3 / 405 B Llama-3.1 / 489 B Qwen-Max running on a bank's
own GPU cluster**. The same provider config drives all of them.

Compatible runtimes (non-exhaustive): **Ollama · vLLM · SGLang ·
llama.cpp server · LM Studio · LocalAI · TGI (HuggingFace Text
Generation Inference) · OpenLLM · Triton Inference Server**. If it
exposes `/v1/chat/completions`, revio works against it.

### Why this matters

| Constraint | Why local LLM is the only answer |
|---|---|
| Student / patient / financial code review | FERPA / HIPAA / GDPR / SOX often forbid sending code to a US API |
| **Banks / insurance / law firms** | Internal IP + regulator audit trails — code must stay inside the firewall, often with full-size models running on private GPU clusters |
| **Government / defense / aerospace** | Air-gapped by policy; both `plc` and `verilog` profiles are valuable here |
| **National AI sovereignty** | Russia / China / EU mandates that prohibit US-origin LLM access for state-related work |
| Cost at scale | A CS department doing 5 000 audits / semester pays $0 instead of $50-500 |
| Vendor independence | No provider rug-pull / pricing-tier change breaks your CI |

### One-time setup (any local server)

```toml
# ~/.config/revio/config.toml
[llm]
provider = "openai_compat"
api_url = "http://<host>:<port>/v1"   # whatever your local server exposes
api_key = "unused"                     # or your internal token, if any
model = "<whatever-model-id-the-endpoint-serves>"
```

Then `revio audit .` — fully offline from this point on. The setup is
**model-agnostic**: replace the `api_url` + `model` line and you're
talking to a different deployment. Examples:

| Deployment | `api_url` | `model` |
|---|---|---|
| Ollama on your laptop | `http://localhost:11434/v1` | e.g. `qwen2.5:7b`, `llama3.1:8b` |
| vLLM behind nginx in your DC | `https://llm.internal/v1` | e.g. `deepseek-v3-671b`, `llama-3.1-405b` |
| Bank's on-prem cluster (full-size frontier model) | `https://gpu-cluster.bank.local/v1` | whatever the cluster team registers |
| Air-gapped lab box | `http://192.168.x.x:8000/v1` | whatever's loaded |

`/model` REPL command **auto-discovers** whatever models the endpoint
serves by hitting `GET /v1/models`. No separate config for each model.

`/cost` shows token counts but **silently omits the `$` figure** for
local models (no cost to misrepresent).

### Hybrid setup

Embeddings used by RAG (`all-MiniLM-L6-v2`, ~80 MB) always run locally
in revio's own process — your indexed guidelines never go through any
API. You can combine local embeddings with a cloud LLM, or go fully
local. Mix and match per `.revio.toml`.

### What works · what's identical · what scales with hardware

| Feature | Local LLM (any size) | Cloud LLM |
|---|---|---|
| Agent loop · tools · streaming · MCP · `--fix` | ✅ identical | ✅ |
| All 13 Layer-2 static analyzers | ✅ identical (run as subprocesses) | ✅ |
| RAG (embeddings) | ✅ always local | ✅ embeddings local; LLM cloud |
| Per-call latency | hardware-bound (laptop: slow · 8×H100: fast) | network-bound |
| Finding quality on tricky semantic cases | scales with model size — a frontier 671 B on local hardware ≈ frontier cloud | typically high |
| Cost per audit | **$0** (you already paid for the GPUs) | $0.01-$0.30 typical |

**The point**: customers with budget for a serious local deployment
(banks, defense primes, large universities, telcos) get the **same**
quality as cloud — with full data sovereignty. Customers without that
budget can run a 7-8 B model on a developer laptop and still get
useful static-analyzer coverage + LLM reasoning. **Same product, both
extremes of the spectrum.**

---

## Guidelines (RAG)

Index your team's coding standards so findings cite them directly:

```bash
revio guidelines add docs/styleguide.md team-policies/
revio guidelines search "SQL injection prevention"
revio guidelines list / clear / reindex
```

Supported: `.md` `.txt` `.rst` `.adoc` `.pdf` `.docx`. Index lives at
`~/.cache/revio/<repo-hash>/vectorstore/` (per-repo).

During a review:
```
⚠️ CRITICAL  SQL injection
  Evidence:
    · read_file showed: query = f"SELECT * FROM users WHERE id = {id}"
    · search_guidelines → security_checklist.md / SQL Injection: ...
```

---

## Skills

Markdown files with YAML frontmatter that teach the agent how to handle
specific scenarios. Dual layer:

- `.revio/skills/<name>/SKILL.md` (project, committable)
- `~/.config/revio/skills/<name>/SKILL.md` (user-global)

```bash
revio skills list / show <name> / activated
```

Skills with matching `extensions`, `imports`, or `filename_patterns`
auto-fire; others stay catalog-only until the LLM pulls them via
`load_skill(name)`.

---

## MCP — both directions

### As a client — consume your existing servers

```toml
[mcp.servers.jira]
command = "uvx"
args = ["mcp-server-atlassian-jira"]
env = { ATLASSIAN_TOKEN = "$ATLASSIAN_TOKEN" }
```

revio connects in parallel at session start, wraps their tools as
`mcp_<server>_<tool>`, and merges them into the agent's toolkit. Failed
servers degrade gracefully.

### As a server — expose revio to other agents

```bash
revio mcp-server               # stdio MCP server
```

Tools exposed: `revio_audit`, `revio_review`, `revio_dedup`,
`revio_run_bandit`, `revio_run_oxlint`, `revio_run_cppcheck`,
`revio_search_guidelines`, `revio_list_profiles`, `revio_detect_profile`.

Register with Claude Code:
```json
{ "mcpServers": { "revio": { "command": "revio", "args": ["mcp-server"] } } }
```

The server returns patch *operations* via `revio_dedup` but never
applies them — the host agent decides what to write.

---

## Interactive REPL

```bash
revio              # drop into REPL
```

```
> review the last 3 commits
> 检查这个项目里有没有重复代码         ← any language; output stays English
> /model deepseek-v4-pro
```

| Slash command | |
|---|---|
| `/help` `/?` | List all commands |
| `/model` | Interactive picker (live `/v1/models` + curated catalog) |
| `/model <name>` | Set model directly |
| `/models` | List available models on current endpoint |
| `/url` `/key` `/config` | Endpoint / key / config-file management |
| `/profile <name>` | Switch profile |
| `/mode <name>` | Default mode for next NL input |
| `/budget <n>` | Tool-call budget for this session |
| `/cost` | Real token usage + USD cost for the REPL session |
| `/clear` `/history` `/exit` | Standard |

Non-slash input is classified by an intent LLM into `review` / `audit` /
`dedup` / `chat`. Multilingual by design.

---

## Profiles

| Profile | Layer 1 (AST) | Layer 2 (static) |
|---|---|---|
| `js` | Tree-sitter + symbol graph + call graph + dedup index | **oxlint** |
| `python` | Tree-sitter | **bandit** |
| `rust` | Tree-sitter | **clippy** |
| `java` | Tree-sitter | **spotbugs** (needs .class) |
| `go` | Tree-sitter | **golangci-lint** |
| `cpp` | Tree-sitter | **cppcheck** |
| `plc` | 7 vendor parsers + LD/FBD/SFC | **30+ PLCopen rules** + HW audit |
| `shell` | Tree-sitter | **shellcheck** |
| `lua` | Tree-sitter | **luacheck** |
| `sql` | Tree-sitter | **sqlfluff** (multi-dialect) |
| `ruby` | Tree-sitter | **rubocop** |
| `php` | Tree-sitter | **phpstan** |
| `kotlin` | Tree-sitter | **detekt** (needs JDK) |
| `verilog` | Tree-sitter | **verilator** (--lint-only) |
| `generic` | Tree-sitter (Scala / C# / Swift / Julia) | — |
| LLM-only | MATLAB · R · Verilog · SAS · COBOL · Solidity · Zig · ObjC · Dart | — |

Default `auto` walks the repo, counts file extensions + marker files,
picks the best match. Override with `--profile <name>`.

---

## Token usage & cost

revio reads real `usage_metadata` off every LLM response. Per call:
```
  · tokens +1.2k in, +340 out  (Σ 8.4k / 1.9k · 85 tok/s · $0.011)
```

Session footer:
```
  session: 6/6 tool calls · 7 findings · 18.1s · deepseek-v4-pro
  tokens:  12.6k in · 573 out · 5 LLM call(s) · avg 32 tok/s · $0.0040
```

Pricing is fuzzy-matched (DeepSeek / Claude / OpenAI / Mistral / Ollama).
**For models we don't have pricing for (new providers, custom endpoints)
the `$` figure is silently omitted** — token counts and throughput still
show. We never display a misleading `$0.00`.

`/cost` in the REPL shows cumulative usage across the whole REPL session.

---

## Output formats

```bash
revio audit --format stream            # default (TTY)
revio audit --format json -o report.json
revio audit --format markdown -o report.md
```

JSON / markdown include `total_input_tokens`, `total_output_tokens`,
`llm_call_count`, `est_cost_usd` on the `ReviewReport`.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Clean, or only info-severity |
| `1` | Operational error (no config, invalid path, ...) |
| `2` | At least one CRITICAL finding |
| `130` | Ctrl-C |

For CI: `revio audit . --format json -o report.json` and fail the build
on exit `2`.

---

## What's special about revio

| | Other LLM review tools | revio |
|---|---|---|
| Static-analysis backbone | One LLM call per file | 6 deterministic analyzers + LLM on top |
| Hallucinated findings | Common (LLM invents paths) | Grounding validator rejects un-read files |
| Token cost on a large repo | Re-reads whole files | Pulls only enclosing functions via Tree-sitter |
| Company-specific rules | Generic prompt only | RAG over your own guidelines |
| PLC / industrial control | Not supported | 7 vendor parsers + 30 PLCopen + HW audit |
| LLM provider | Vendor-locked | Anthropic + any OpenAI-compat |
| Cross-session memory | Stateless | SQLite history — "🆕 New since last run" |
| Auto-fix | Text suggestion only | `--fix` actually edits files |
| Undo a fix | "Hope you committed before running" | `revio fix undo` — snapshot-based, multi-step, no git required |
| Self-hosted / offline | Cloud-only, your code goes to a US API | Any OpenAI-compatible endpoint — **Ollama, vLLM, on-prem GPU**; FERPA/HIPAA/GDPR-safe |

---

## Troubleshooting

**Wizard didn't run** → `rm ~/.config/revio/config.toml` then `revio`.

**Connection test fails** → DeepSeek / Mimo need `disable_thinking = true`. OpenAI-compat gateways often have their own model IDs.

**Findings dropped as "ungrounded"** → the validator rejected findings on files the agent never read. Run with `REVIO_DEBUG=1` to see the full tool-call trace.

**`dedup --fix` says no patches** → the model didn't emit `propose_patch` calls. Either a strict gateway dropped the tool call, or the model didn't think a mechanical fix was safe (check `suggestion` in the finding).

**Tree-sitter import errors** → `.venv/bin/pip install -e ".[languages]"`.

---

## License

MIT.
