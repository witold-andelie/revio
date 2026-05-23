# revio · Internals (debug map for the maintainer)

> Not user docs. This is YOUR file — when something breaks, here's the
> exact file-by-file execution path for each feature. Use Ctrl-F by symptom.

---

## 0. Mental model

```
Entry point:    src/revio/cli/main.py  →  Typer app
                                       ↓
                                  decides:
                                  · review/audit/dedup → _run() or _run_dedup_with_fix()
                                  · bare `revio`        → cli/repl.py run_repl()
                                  · `revio config X`    → config subcommand
                                  · `revio guidelines X`→ guidelines subcommand
                                  · `revio skills X`    → skills subcommand
                                       ↓
                                  if no config:
                                      cli/wizard.py run_wizard()
                                       ↓
                                  agent/runner.py run_agent_sync()
                                       ↓
                                  asyncio.run(run_agent(...))
                                       ↓
                                  builds LangGraph, streams events,
                                  captures final_state["findings" / "patches"]
                                       ↓
                                  optional: cli/fix.py run_fix_flow()
```

If revio is hanging, breaking, or producing wrong output, **trace down
this column** to localize. Each section below details the next-level
zoom-in.

---

## 1. Cold start: `revio` (no args, no config)

**Symptom**: wizard doesn't show / fails / config not saved

**File trace**:
```
cli/main.py           : @app.callback(invoke_without_command=True)
   _root(ctx)         : ctx.invoked_subcommand is None
   ↓
config.py             : config_exists() reads ~/.config/revio/config.toml
   returns False if not present
   ↓
cli/wizard.py         : run_wizard()
   questionary.select / .text / .password / .confirm prompts
   ↓
cli/wizard.py         : _test_connection(provider, url, key, model)
   Picks ChatAnthropic OR ChatOpenAI based on provider
   ↓
config.py             : save_user_config(cfg)
   Writes TOML to ~/.config/revio/config.toml with mode 0600
   ↓
cli/main.py           : run_repl() (bare invocation)
   OR returns control to subcommand
```

**Common failures**:
- `wizard saves but next run still asks` → check `config.py:user_config_path()`
- `connection test fails on Mimo / DeepSeek` → `disable_thinking=true` needed
- `wizard not interactive` → not a TTY (CI?). `questionary.ask()` returns None

---

## 2. Subcommand: `revio review` / `revio audit`

**Symptom**: hangs, wrong output format, missing findings

**File trace**:
```
cli/main.py:review()                : Typer-decorated function
   ↓ delegates to
cli/main.py:_run()                  : shared body
   ↓
   _ensure_config()                 : load config (trigger wizard if missing)
   ↓
   _handle_non_git() [review only]  : 3-way prompt for non-git paths
   ↓
   output/stream.py:StreamRenderer  : renderer for terminal output
   ↓
agent/__init__.py:run_agent_sync()  : sync wrapper
   ↓
   asyncio.run(run_agent(...))
   ↓
agent/runner.py:run_agent()         : main async entry
   ↓
   detect/fingerprint.py:detect_project(repo_path)
      - walks files, counts extensions
      - reads package.json / Cargo.toml / etc.
      - returns ProjectFingerprint
   ↓
   profiles/__init__.py:get_profile(profile_name)
      - loads all profiles (lazy)
      - returns ProfileBase subclass
   ↓
   profile.make_reasoning_hints()   : injected into system prompt
   ↓
   Builds initial AgentState
   ↓
   agent/mcp_client.py:connect_servers()
      - parallel async connects (asyncio.timeout, NOT wait_for!)
      - failures captured in connect_results
   ↓
   AsyncSqliteSaver.from_conn_string(per-repo path)
      - checkpointer.serde = JsonPlusSerializer(allowed_msgpack_modules=[...])
        ← MUST include any custom pydantic types here or they get blocked
   ↓
   agent/graph.py:build_graph(checkpointer)
      - StateGraph(AgentState) compiled with reducers
   ↓
   graph.astream_events(initial_state, run_config, version="v2")
      → fires events: on_chain_start, on_tool_start, on_chat_model_stream, ...
   ↓
   _dispatch_event(ev, on_event)   : translates LangGraph events to our types
   ↓
   StreamRenderer.handle(event, payload) : terminal output
   ↓
   snapshot = await graph.aget_state()
   final_state = snapshot.values
   ↓
   _build_report(final_state, config)  : ReviewReport from state
   ↓
   FindingsStore.record_run + compare    [agent/findings_store.py]
      - SQLite at ~/.cache/revio/<hash>_findings.sqlite
      - emits findings_compared event
   ↓
   on_event("session_end", {"report": report.model_dump()})
   ↓
   Stash patches in cli/main.py module via sys.modules
      ← USES sys.modules['revio.cli.main'] NOT direct attribute access
        (revio.cli.main is shadowed by the re-exported main function)
```

**Common failures**:
- `auto profile picks wrong language` → `detect/fingerprint.py:_suggest_profile()`
- `MCP servers timeout but session continues` → expected; check `mcp_connected` event
- `Custom Pydantic type silently dropped` → add to `allowed_msgpack_modules` in `runner.py`
- `Hangs on first run with new repo` → checkpoint DB creation; check `~/.cache/revio/` perms
- `Findings appear and disappear` → grounding validator dropping them; see §5

---

## 3. Subcommand: `revio dedup --fix`

**Symptom**: "No patches proposed" despite propose_patch tool calls visible

**File trace**:
```
cli/main.py:dedup()                 : Typer command with extra --fix/--dry-run/--yes flags
   ↓
cli/main.py:_run_dedup_with_fix()   : NOT the shared _run()
   ↓
   run_agent_sync(mode="dedup", ...) : standard agent run
   ↓
   --- inside agent/runner.py at session_end:
      try:
          import sys
          import revio.cli.main   ← Crucial. revio.cli.main is the function
                                    re-exported via cli/__init__.py:
                                    "from .main import app, main"
                                    So direct attribute access fails!
          cli_main_module = sys.modules["revio.cli.main"]
          cli_main_module._last_session_patches = list(patches)
   ↓
   Back in cli/main.py:_run_dedup_with_fix():
   ↓
   if fix or dry_run:
       patches = _pull_patches_from_recent_run(cfg, repo_path)
       ↑ reads sys.modules[__name__]._last_session_patches
   ↓
cli/fix.py:run_fix_flow(patches, repo_root, dry_run, yes, ...)
   ↓
   agent/patch.py:PatchApplier(repo_root)
   ↓
   applier.begin_session(allow_dirty=False)
       - refuses dirty git repo (unless --allow-dirty)
       - creates safety stash with timestamp
   ↓
   for each patch:
       ok, reason = applier.can_apply(patch)   ← pre-flight check
       preview = applier.preview(patch)         ← unified diff in Rich Panel
       questionary.select("Apply?") → user choice
       applier.apply(patch)                    ← writes files
   ↓
   FixSessionResult.render_summary()
      - Applied / Skipped / Failed counts
      - "Undo all changes:" instructions
```

**Common failures**:
- **"No patches proposed"** despite seeing `propose_patch` calls:
  1. Check the runner's `import revio.cli.main` and `sys.modules["revio.cli.main"]` 
  2. Bug source: `from .main import main` in `cli/__init__.py` shadows the module
  3. Workaround: ONLY ever access `_last_session_patches` via `sys.modules`
- **"old_content doesn't match"** during apply:
  - `agent/patch.py:_normalize_ws()` does lenient compare (rstrip lines)
  - Failure message now includes the actual vs expected
  - Usually means LLM emitted slightly different whitespace
- **"Patch refused: dirty repo"** → use `--allow-dirty` or `git commit` first
- **No `propose_patch` calls at all** → dedup mode prompt issue; check
  `agent/prompts.py:MODE_INSTRUCTIONS["dedup"]`. The "CRITICAL — TWO actions
  per confirmed finding" section is what makes the LLM call propose_patch.

---

## 4. Tool dispatch inside react_node

**Symptom**: tool calls happen but state isn't updated

**File trace**:
```
agent/graph.py:react_node()
   ↓
   ctx = ToolContext(repo_root, profile_name)  ← lazy indexes attached here
   ↓
   Universal tools assembled:
       list_files, read_file, search_guidelines, load_skill,
       report_finding, propose_patch
   ↓
   Profile-specific tools:
       profile_cls.make_tools(ctx) → loaded via profiles/<name>_runtime.py
       Returns 4-8 additional tools (run_oxlint, get_function_at, etc.)
   ↓
   MCP tools from run_config["configurable"]["mcp_tools"]
   ↓
   _filter_tools_for_mode(tools, mode) ← strips noise per mode
   ↓
   llm = make_llm(cfg) ← in agent/llm.py, branches on provider
   llm_with_tools = llm.bind_tools(tools)
   ↓
   while True:
       response = await llm_with_tools.ainvoke(messages)
       
       tool_calls = response.tool_calls
       
       FOR each tc IN tool_calls:
           used += 1   ← budget counted even on error
           
           result = await tool.ainvoke(args)
           
           ★ CRITICAL: Command extraction
           if isinstance(result, Command):
               if "findings" in result.update:
                   new_findings.extend(result.update["findings"])
                   ack = f"Recorded finding: {findings[0].title}"
               if "patches" in result.update:
                   new_patches.extend(result.update["patches"])
                   ack = f"Queued patch: {patches[0].title}"
               
               messages.append(ToolMessage(content=ack, tool_call_id=tcid))
           else:
               messages.append(ToolMessage(content=str(result), ...))
           
           ★ Drain auto-emit buffer (Layer 2 tools push directly to ctx)
           if ctx.pending_findings:
               new_findings.extend(ctx.pending_findings)
               ctx.pending_findings.clear()
       
       # NEVER break mid-batch — OpenAI strict tool-call pairing
       if used >= budget_max: break
   ↓
   facts = collect_tool_facts(messages)
   grounded, dropped = validate_findings(new_findings, facts)
   ↓
   return {
       "messages": messages,
       "findings": grounded,
       "patches": new_patches,
       "dropped_findings": dropped,
       ...
   }
```

**Common failures**:
- **State not updated despite tool call**: result was a Command but no
  matching key in `if "X" in result.update`. Add the handler.
- **OpenAI 400 "tool_calls must be followed by tool messages"**: a tool
  call in the batch didn't get a paired ToolMessage. The for-loop now
  processes WHOLE batch — never break mid-batch.
- **Budget overrun**: by design. Per-batch atomicity > per-call cap.
- **Layer 2 tools fired but findings invisible**: check
  `ctx.pending_findings` drain after the tool — should be cleared per
  iteration.

---

## 5. Hallucination defense (grounding validator)

**Symptom**: agent finds something but it's not in the report

**File trace**:
```
agent/grounding.py:collect_tool_facts(messages)
   ↓ walks messages list:
   - For each AIMessage with tool_calls: index call_id → (name, args)
   - For each ToolMessage: pair with prior call_id
   - For successful read_file calls: facts.files_read[path] = max_line
   - For error read_file calls: facts.files_failed.add(path)
   ↓
agent/grounding.py:validate_findings(findings, facts)
   ↓ for each finding:
   - If file_path not in files_read (even via _path_variants):
       → drop, reason: "File X was never successfully read"
   - If line_start > max_line:
       → downgrade, "[ungrounded] line_start=N exceeds last line read"
   - If evidence list empty:
       → drop, reason: "no evidence"
   ↓
   returns (kept, dropped) tuple
   ↓
react_node returns kept findings + "dropped_findings" in state
   ↓
runner.py emits findings_dropped event
   ↓
output/stream.py:_on_findings_dropped()
   ↓
   Console: "⚠ Ungrounded findings dropped" section
```

**Common failures**:
- **Real findings being dropped** (false positives in validator):
  - Check `agent/grounding.py:_path_variants()` — does it handle the
    path format the LLM used?
  - Common: LLM used `./src/foo.js` but read it as `src/foo.js`
- **Hallucinated findings slipping through**: validator only checks
  exact path matches. Improvements: fuzzy filename matching, content
  attestation (require evidence to contain verbatim quote from file).

---

## 6. RAG: indexing and retrieval

**Symptom**: search_guidelines returns "no guidelines indexed" / wrong results

**File trace** — indexing (CLI):
```
cli/main.py:guidelines_add(paths)
   ↓
layers/rag/document_loader.py:DocumentLoader.load_file/load_directory(path)
   ↓ dispatches by extension:
   - .md/.markdown      → _load_markdown (split by # headers)
   - .txt/.rst/.adoc    → _load_text (chunked by paragraph)
   - .pdf               → _load_pdf (pypdf, page-by-page)
   - .docx              → _load_docx (python-docx, by heading)
   ↓
   Returns list[Document] with metadata
   ↓
layers/rag/indexer.py:GuidelineIndexer(repo_root)
   - persist_dir = ~/.cache/revio/<repo-hash>/vectorstore/
   - lazy load HuggingFaceEmbeddings (all-MiniLM-L6-v2, ~80MB)
   ↓
   indexer.vectorstore.add_documents(docs)
      ← langchain_chroma.Chroma adds + persists
```

**File trace** — retrieval (during agent run):
```
agent/tool_context.py:ToolContext.rag property (lazy)
   ↓
layers/rag/retriever.py:GuidelineRetriever(repo_root)
   - reuses GuidelineIndexer instance via class-level singleton cache
   - if has_index() returns False: ctx.rag returns None
   ↓
   retriever.search_with_scores(query, k=5)
      ← Chroma similarity_search_with_relevance_scores
   ↓
agent/tools.py:make_search_guidelines_tool(ctx) - the tool itself
   ↓
   When LLM calls search_guidelines(query, k):
   - If ctx.rag is None: return "no guidelines indexed" message
   - Else: render top-k chunks with source / section / relevance
```

**Common failures**:
- **"no guidelines indexed"** but you ran `guidelines add`: the index
  is per-repo (hashed from cwd). Run `add` from the same directory you
  run `audit` from.
- **Wrong / irrelevant results**: cosine similarity isn't perfect.
  Consider better embedding model or re-chunk the source documents.
- **First run is slow (~10s)**: sentence-transformers downloads
  `all-MiniLM-L6-v2` to ~/.cache/huggingface (one-time, ~80MB).

---

## 7. Skills: load + activation

**Symptom**: skill defined but agent doesn't see it / doesn't load it

**File trace**:
```
skills/loader.py:project_skills_dir() → .revio/skills/
skills/loader.py:user_skills_dir()    → ~/.config/revio/skills/
   ↓
discover_skills():
   - walks both dirs (user first, project shadows on name collision)
   - for each subdir/SKILL.md:
       _parse_frontmatter(content) → YAML dict
       SkillMatchRules from matches.* fields
       Skill model with name + description + when_to_use + matches
   ↓
SkillsRegistry.discover(project_root) → all loaded skills
   ↓
agent/tool_context.py:
   ctx.skills_registry → lazy SkillsRegistry
   ctx.activated_skills → lazy: runs detect_project then registry.activate_for(
       extensions=..., languages=..., frameworks=..., filenames=...
   )
   ↓
agent/graph.py:_build_skills_section(state):
   - Lists ALL skills as catalog (name + description)
   - For activated ones: includes full body inline in system prompt
       (capped at 2000 chars; agent can load_skill for full content)
   ↓
agent/tools.py:make_load_skill_tool(ctx)
   - Called by LLM when it wants full body of a non-auto-activated skill
```

**Common failures**:
- **Skill not discovered**: missing `description` in frontmatter
  (validator drops these — see `loader.py:_load_one`)
- **Skill discovered but not auto-activated**: skill has NO `matches`
  rules → only loadable on demand via `load_skill` tool
- **Same-name skill not user-overridden**: project shadows user (by design)

---

## 8. MCP: client connection lifecycle

**Symptom**: MCP server times out / crashes the agent

**File trace**:
```
agent/runner.py:run_agent():
   ↓
   mcp_server_configs = [MCPServerConfig(...) for each enabled server]
   ↓
   async with ClientSessionGroup(component_name_hook=...) as mcp_group:
       ↑ NOT MCPSessionManager wrapper. Direct SDK class.
       ↑ Wrapper caused anyio cancel-scope-cross-task issues.
   ↓
   agent/mcp_client.py:connect_servers(group, configs, name_holder)
      for cfg in configs:
          name_holder["current"] = cfg.name  ← used by component_name_hook
          async with asyncio.timeout(cfg.timeout):
              ★ MUST be asyncio.timeout (3.11+) NOT asyncio.wait_for!
              ★ wait_for spawns a new task → cancel-scope cross-task error
              await group.connect_to_server(cfg.to_server_params())
   ↓
   tools = langchain_tools_from(group)
      For each aggregated tool in group.tools:
          Wrap in StructuredTool with async coroutine that calls
          group.call_tool(name, arguments=kwargs)
   ↓
   on_event("mcp_connected", {...})
   ↓
   ... agent runs with mcp_tools in run_config["configurable"]["mcp_tools"]
   ↓ (in react_node, MCP tools merged into tool list)
   ↓
   ... session ends, ClientSessionGroup async cleanup
```

**Common failures**:
- **"Attempted to exit cancel scope in a different task"**:
  - Cause: someone added `asyncio.wait_for` somewhere
  - Fix: use `async with asyncio.timeout(...)` (Python 3.11+)
- **MCP server hangs**: per-server timeout enforced; check
  `cfg.timeout` (default 5s)
- **Tools not visible to agent**: check `name_holder["current"]` is set
  before each connect_to_server call — the name hook closure depends on it

---

## 9. PatchApplier: --fix internals

**Symptom**: patch refused with "old_content doesn't match"

**File trace**:
```
agent/patch.py:PatchApplier(repo_root)
   ↓
   For each PatchOp in PatchSet:
      _can_apply_op(op):
         - Path safety: _resolve(relative_path) returns None if escapes root
         - For edit/delete_lines:
              * Read file
              * Get slice lines[line_start-1:line_end]
              * Compare _normalize_ws(slice) vs _normalize_ws(old_content)
              * If mismatch: detailed error showing actual vs expected
         - For delete_file/create_file/rename: other checks
   ↓
   begin_session(allow_dirty=False):
      - _is_git_repo()
      - _has_uncommitted_changes()
      - If dirty + allow_dirty: git stash with timestamped message
   ↓
   apply(patchset):
      - can_apply() pre-flight (atomic on the SET — if any op fails, none apply)
      - for op in patchset.ops:
            _apply_op(op):
               - delete_file: full.unlink()
               - create_file: write new_content
               - rename: full.rename(new_full)
               - edit/delete_lines:
                    lines = file.read().splitlines(keepends=True)
                    before = lines[:line_start-1]
                    after = lines[line_end:]
                    replacement = (op.new_content if edit) or []
                    file.write_text(...)
   ↓
   end_session(): no-op (stash stays for user-driven revert)
```

**Common failures**:
- **"old_content doesn't match"**: usually whitespace or LLM-emitted
  near-match. Error message shows both — diff them. Most fixes are in
  `_normalize_ws` (lenient compare).
- **"file may have been edited"**: user modified the file between
  agent run and apply. Rerun the agent.
- **"path escapes repo root"**: LLM tried `/etc/passwd` or `../foo`.
  By design.

---

## 10. Cross-session memory: findings history

**Symptom**: "🆕 New since last run" wrong / missing

**File trace**:
```
agent/findings_store.py:FindingsStore(db_path)
   ↓ DB: ~/.cache/revio/<hash>_findings.sqlite
   ↓ Schema: findings_history table (fingerprint PK, file/line/title/
              severity/category/confidence/line_content_hash/first_seen/
              last_seen/run_count/finding_json)
   ↓
   store.compare(current_findings, repo_root):
      for f in current_findings:
         fp = _fingerprint(f)  ← sha1(file_path + line_start + normalized_title)
         prior = get_by_fingerprint(fp)
         current_hash = _line_content_hash(repo_root, file, line)
         if prior is None: status = "new"
         elif prior.line_content_hash != current_hash: status = "maybe_fixed"
         else: status = "still_present"
   ↓
   store.record_run(current_findings, repo_root):
      for each finding: INSERT or UPDATE run_count + last_seen
   ↓
   runner.py emits findings_compared event
   ↓
   output/stream.py:_on_findings_compared() renders the section
```

**Common failures**:
- **"All still present" but finding text changed**: by design.
  fingerprint uses title only, not description. Title stable, description
  varies LLM-to-LLM.
- **"All new" every run**: the SQLite DB got wiped (cache cleared?).
- **Wrong "maybe_fixed" status**: line_content_hash computes from
  current file state. If you edited the source between runs, that's
  intentional.

---

## 11. Multi-LLM provider routing

**Symptom**: wrong model called, "thinking blocks not allowed" error

**File trace**:
```
config.py:LLMConfig
   - provider: "anthropic" | "mimo" | "openai_compat" | "custom"
   - api_url / api_key / model / disable_thinking
   ↓
agent/llm.py:make_llm(config, max_tokens):
   - api_key = config.llm.resolve_api_key()
      ↑ checks api_key (direct), api_key_env (env var), ANTHROPIC_API_KEY fallback
   ↓
   if provider in {"anthropic", "mimo"}:
      _make_anthropic(config, api_key, max_tokens)
         ChatAnthropic(model, api_key, max_tokens, base_url, thinking={...})
   elif provider in {"openai_compat", "custom"}:
      _make_openai(config, api_key, max_tokens)
         ChatOpenAI(model, api_key, base_url)
   ↓
   Returned LLM has identical .ainvoke() interface → rest of code agnostic
```

**Common failures**:
- **"thinking blocks not allowed"**: provider doesn't support extended
  thinking (Mimo, OpenAI-compat). Set `disable_thinking = true`.
- **"model not supported"**: wrong model ID for that provider. Check
  provider's docs.
- **Connection times out**: wrong `api_url`. OpenAI-compat usually needs
  `/v1` suffix; check provider docs.

---

## 12. Streaming / output

**Symptom**: output looks broken / missing

**File trace**:
```
agent/runner.py:run_agent emits events via on_event(type, payload)
   ↓
   Streamed events:
   - session_start, auto_detect, mcp_connected
   - node_start, node_end (per plan/react/reflect)
   - plan
   - tool_start, tool_end
   - llm_start, llm_token, llm_end (when streaming model output)
   - finding_recorded, finding_dropped
   - reflect (with summary + observations)
   - findings_compared
   - session_end (with full ReviewReport.model_dump())
   ↓
output/stream.py:StreamRenderer.handle(event, payload):
   - method = getattr(self, f"_on_{event}", None)
   - if method: method(payload)
   ↓
   _on_<event> methods render to Rich console:
   - _on_plan: blue Panel with plan text
   - _on_tool_start: → arrow + tool name + args summary
   - _on_tool_end: ✓ check + result preview
   - _on_finding_recorded: severity-badge card
   - _on_reflect: cyan rule + summary
   - _on_session_end: full finding cards + footer stats
```

**Common failures**:
- **Missing event in output**: add a `_on_<event>` method to StreamRenderer
- **JSON format wrong / missing data**: `output/stream.py:format_as_json()`
  uses `report.model_dump()` — make sure new fields are in `ReviewReport`
  Pydantic model

---

## 13. CLI entry points (`pyproject.toml` → `revio` executable)

```
pyproject.toml: [project.scripts]
                revio = "revio.cli.main:app"
   ↓
   `pip install -e .` creates a `revio` shim in .venv/bin/
   ↓
   `revio` → calls cli.main:app (the Typer app object)
   ↓
   Typer dispatches based on args:
      - no args: @app.callback fires → wizard / REPL
      - subcommand: matching @app.command function
      - --help: Typer auto-generates
```

**Common failures**:
- **`command not found: revio`**: not in venv, OR pip install failed.
  Re-run `pip install -e .`
- **subcommand not found**: not registered. Check `cli/main.py` for
  missing `@app.command()` decorator
- **Help missing for subcommand**: missing docstring on the function

---

## 14. Quick diagnostic checklist

| Symptom | First check |
|---|---|
| Nothing happens / hangs | Is API key valid? Try `revio config show`, then test with simple Python script |
| "No findings" but you expect some | (1) run with `REVIO_DEBUG=1` (2) check if findings dropped by grounding (look for `findings_dropped` event) |
| Wrong language profile picked | `revio audit ... --profile X` to override; if X doesn't exist, check `profiles/__init__.py:load_all_profiles` |
| Layer 2 tool not running | `revio config show` to verify provider; check tool binary exists (`which oxlint` / `which bandit` etc.) |
| Wizard infinite loop | Delete `~/.config/revio/config.toml` and retry |
| `--fix` reports 0 patches | Check that propose_patch was called (look at `tool_start` events for `propose_patch`) AND `sys.modules["revio.cli.main"]._last_session_patches` is non-empty |
| RAG returns nothing | `revio guidelines list` to verify; per-repo index lives in `~/.cache/revio/<hash>/` |
| Tests fail after a change | Run all 7: M1/M2/M3/MCP/Languages/PLC/Patch smoke tests |

---

## 15. Test layout (what each test file proves)

| File | Tests | Mock or real |
|---|---|---|
| `tests/test_m1_smoke.py` | Agent skeleton: plan→react→reflect, 15 events, evidence chain | Mock LLM |
| `tests/test_m2_smoke.py` | JS profile: 5 tools, grounding drops hallucinated finding | Mock LLM |
| `tests/test_m3_smoke.py` | RAG + skills + findings history + mode diff | Mock LLM |
| `tests/test_mcp_bridge.py` | Real stub MCP stdio server + tool roundtrip | Real (subprocess) |
| `tests/test_languages_smoke.py` | Tree-sitter for Py/Rust/Java/Go; cppcheck + golangci-lint + LLM-only profile registration | Deterministic |
| `tests/test_plc_smoke.py` | PLC parser core, 23 rule violations on fixture, PLC-006 regex bug fix verification | Deterministic |
| `tests/test_patch_smoke.py` | PatchApplier: serialization, can_apply pre-flight, apply, git safety, preview, propose_patch tool | Deterministic |

Each test should run in **under 10 seconds** (no real API calls; PLC tests
needs Python imports only; MCP test launches a stub subprocess).

---

## 16. Key invariants (don't break these)

1. **`from .main import main` in `cli/__init__.py` shadows `revio.cli.main`** — always use `sys.modules["revio.cli.main"]` for module access from inside `agent/runner.py`
2. **`asyncio.wait_for` is forbidden** in MCP code paths — use `asyncio.timeout()` (3.11+) instead. wait_for spawns a task, breaking anyio cancel scopes
3. **`Command(update={X: [...]})` only updates state if `X` is a key in `AgentState` AND `react_node` extracts it** — adding a new state field requires touching both
4. **Layer 2 tools push to `ctx.pending_findings`, NOT return findings** — react_node drains after each tool call
5. **Patches and Findings can't share a transport** — they go to different state keys (`findings` vs `patches`)
6. **The grounding validator runs only on LLM-emitted findings, not Layer 2 auto-emitted ones** (Layer 2 is deterministic, doesn't need grounding)
7. **Mode-specific tool blacklist in `graph.py:_MODE_TOOL_BLACKLIST`** — never strip universal tools like `read_file` / `report_finding`
8. **`make_llm` is the ONLY place that knows about LLM providers** — every other module is provider-agnostic
9. **`detect/fingerprint.py:_LANG_PROFILE` is the routing table** — adding a language → add an entry here
10. **Checkpointer's msgpack allowlist** must include EVERY custom Pydantic type that flows through agent state. Forgetting causes silent data loss
