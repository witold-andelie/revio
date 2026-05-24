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

## 8b. MCP server: exposing revio to other agents

**Symptom**: external host (Claude Code / Cursor) can't see revio's
tools, or `revio_audit` errors when called via MCP

**Entry point**: `revio mcp-server` CLI subcommand
  → `cli/main.py:mcp_server()`
  → `mcp_server/server.py:run_stdio()`

**File trace**:
```
cli/main.py:mcp_server() typer command
   ↓
   from ..mcp_server import run_stdio
   ↓
   run_stdio():
      logging.basicConfig(stream=sys.stderr, level=WARNING)
         ★ CRITICAL: stdout is JSON-RPC, must NOT receive log output
      ↓
      server = build_server()  → FastMCP("revio", instructions=...)
         For each tool: @mcp.tool(name=..., description=...) decorator
         registers a callable in mcp._tool_manager
      ↓
      server.run("stdio")
         Blocks on the MCP SDK's stdio_server() loop, reading JSON-RPC
         from stdin and writing responses to stdout.

External host calls revio_audit(repo_path, profile, budget):
   ↓
   mcp_server/server.py:revio_audit() handler (async)
   ↓
   _run_pipeline("audit", repo_path, profile=profile, budget=budget)
      cfg = load_config()
      cfg.agent.max_tool_calls = min(budget, 30)  ← per-request budget clamp
      ↓
      from ..agent.runner import run_agent
      report = await run_agent(mode="audit", ..., on_event=noop)
         ↑ Same code path as `revio audit` CLI. The MCP server is a thin
         wrapper around runner.py.
      ↓
   return json.dumps(report.model_dump(mode="json"), indent=2, default=str)
      ★ For revio_dedup: include_patches=True keeps "patches" key in JSON.
         For revio_audit/review: patches stripped (no edit ops expected).

External host calls revio_run_bandit(path):
   ↓
   mcp_server/server.py:revio_run_bandit() handler (sync — fast path)
   ↓
   _run_static_analyzer("bandit", path)
      runner = BanditRunner()
      findings = runner.scan_to_findings(abs_path, repo_root=repo_root)
      ↑ DIRECTLY invokes Layer 2 — no LLM, no agent loop.
   ↓
   return json.dumps({"analyzer": "bandit", "count": N, "findings": [...]})
```

**Common failures**:
- **"Server crashed during startup"**: usually a print() somewhere
  contaminating stdout. Audit your new code — stdout is JSON-RPC only.
  Logs/diagnostics go to stderr via logging.
- **"Tool returned malformed JSON"**: `report.model_dump(mode="json")`
  must use the json mode (handles datetime/Path/enums). Forgetting
  `mode="json"` returns raw Python objects that json.dumps then chokes on.
- **"Budget seems ignored"**: per-request budget is clamped to
  `max(1, min(budget, 30))` — a hostile client cannot bypass this.
- **"Bandit not installed" error returned via MCP**: graceful — the
  analyzer wrapper catches `NotInstalledError` and serializes it into
  the JSON response. The MCP host sees an error key, not a crash.

**The boundary rule (don't break it)**: revio's MCP server NEVER applies
patches. `revio_dedup` returns patch operations as data. The host agent
decides what to do with them. If you're tempted to add a `revio_apply_patch`
tool — don't. The host already has filesystem tools; adding mutation here
creates a confusing two-layer permission model.

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

## 9b. Fix history: snapshot-based multi-step undo

**Symptom**: `revio fix undo` doesn't restore correctly, history grows
unbounded, oversized files cause apparent corruption

**Storage layout**:
```
~/.cache/revio/<repo_hash>_fix_history/
  2026-05-24T10-15-32.847291_a3f9/    ← session ID, sortable as string = chronological
    manifest.json                       ← {session_id, started_at, repo_root,
                                           patchset_titles, files: [{relpath, existed, oversized}]}
    applied.json                        ← {patches: [PatchSet.model_dump_json(...)]}
    snapshots/
      src/utils.js                      ← pre-apply file content (verbatim copy)
      src/page.js
  2026-05-24T11-02-08.123456_7c1d/
    ...
```

**File trace — recording**:
```
cli/fix.py:run_fix_flow(patches, repo_root, config=...)
   ↓
   if not dry_run and config is not None:
      history_store = FixHistoryStore(
         repo_root, config.agent.checkpoint_dir,
         max_sessions=50, max_age_days=30, max_file_bytes=1_048_576,
      )
   ↓
   applier = PatchApplier(repo_root, history_store=history_store)
   ↓
   applier.begin_session(allow_dirty=...)
      → history_store.begin_session()
         session_id = ISO timestamp with µs + 4-char random suffix
         self._snapshots = {}
         self._applied = []
         mkdir <history_root>/<session_id>/snapshots/
         cleanup()                       ← age + count purge (see below)
      → git stash (if in git AND dirty AND allow_dirty)
   ↓
   for patch in patches:
      applier.apply(patch)
         → history_store.snapshot_files(patch)
            For each path in patch.affected_files:
               If not yet snapshotted in this session:
                  Stat the file
                  If size > max_file_bytes: record FileSnapshot(oversized=True)
                  Elif file doesn't exist: record FileSnapshot(existed=False)
                  Else: shutil.copy2 into snapshots/<relpath>
         → write the new content to disk
         → history_store.add_applied(patch)
   ↓
   applier.end_session()
      → history_store.finalize()
         If no patches applied: rmtree session dir (no empty entries)
         Else: write manifest.json + applied.json
```

**File trace — undoing**:
```
cli/main.py:fix_undo(session_id, ...)
   ↓
   store = FixHistoryStore(...)
   ↓
   If session_id is None:
      session_id = store.list_sessions(limit=1)[0].session_id     ← most recent
   ↓
   store.undo_session(session_id):
      sess = self.get_session(session_id)                          ← read manifest
      ↓
      # Step 1: snapshot CURRENT state as a new session so undo can be undone
      undo_session_id = self.begin_session()
      For each path in sess.files:
         self._snapshot_one(path)
      ↓
      # Step 2: restore each file from the OLD session's snapshots
      For snap in sess.files:
         If snap.oversized: warnings.append("skipped <path>"); continue
         If not snap.existed: target.unlink() (file was created by fix → delete it)
         Else: shutil.copy2(<old_session>/snapshots/<relpath>, target)
      ↓
      # Step 3: finalize the undo session with a synthetic PatchSet so it shows
      # up in `revio fix history`
      self._applied.append(PatchSet(title=f"undo of {session_id}", ops=[]))
      self.finalize()
```

**Auto-rotation (cleanup)**:
```
FixHistoryStore.cleanup() runs on every begin_session():
   1) Age purge: shutil.rmtree any dir older than max_age_days
   2) Count purge: keep newest max_sessions COMMITTED (manifest.json present);
      treat in-flight uncommitted sessions as "about to add one" so the final
      committed count lands at ≤ max_sessions exactly
```

Key invariant: **session ID format must remain string-sortable
chronologically**. The microsecond precision in the timestamp is what
makes `list_sessions(sort by name reverse)` correct. If you ever change
`_make_session_id`, keep this property.

**Common failures**:
- **`undo_session` restores stale content**: snapshot was taken AFTER a
  prior fix in the same `--fix` session. The store dedupes per file —
  the FIRST snapshot wins (= true pre-session content). If you see
  multi-patch sessions undoing to mid-state, check `snapshot_files`
  for the `if relpath in self._snapshots: continue` line.
- **"no such session"**: session ID typo, or session was rotated out.
  `revio fix history --all` shows everything still on disk.
- **Undo skipped a file with "oversized" warning**: file was >
  `max_file_bytes` at fix time. Bump the cap in
  `config.toml [fix_history]` or accept the loss (these are typically
  generated bundles).
- **`revio fix history` shows undos as separate entries**: by design —
  every undo is itself a session, so `undo` then `undo` again redoes.
- **Disk usage grew unexpectedly**: check `max_sessions` and
  `max_age_days` in `cfg.fix_history`. Defaults are 50 / 30 days.
- **Mid-session crash**: applied patches that completed got snapshotted
  (and recorded), but `finalize` may not have written the manifest.
  Stranded session dirs without manifest.json are ignored by
  `list_sessions` and cleaned up by `cleanup()` when count is exceeded.

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

### 11b. Local-LLM routing (Ollama / vLLM / on-prem cluster)

**Symptom**: revio configured for a local server but calls fail / hang /
return junk; `/model` picker shows nothing; `/cost` shows `$0.00` for a
paid cloud model

**Mental model**: a local LLM endpoint that speaks
`POST /v1/chat/completions` is, from revio's point of view, **identical
to DeepSeek or OpenRouter** — same code path (`_make_openai` → ChatOpenAI),
same streaming, same usage_metadata extraction. The only thing different
is the URL.

**Config shape** (any of these is valid):
```toml
[llm]
provider = "openai_compat"
api_url = "http://localhost:11434/v1"            # Ollama
# api_url = "https://api.mistral.ai/v1"          # Mistral cloud (EU-sovereign)
# api_url = "http://gpu-node.internal:8000/v1"   # vLLM in a DC
# api_url = "https://bank-cluster.local/v1"      # enterprise cluster
api_key = "unused"                                # or internal token
model = "qwen2.5:7b"                              # or 'codestral-latest', 'mistral-large-latest', etc.
```

**File trace — what differs from cloud**:
```
agent/llm.py:_make_openai(config, ...)
   ↓
   ChatOpenAI(model=config.llm.model,
              api_key=config.llm.resolve_api_key(),
              base_url=config.llm.api_url)
   ↑ Same code path as DeepSeek / OpenRouter / any other openai_compat.
   ↑ The local server doesn't care that the "api_key" is the string
     "unused" — most local runtimes ignore the header.
   ↓
Stream events flow identically:
   - on_chat_model_stream → chunks parsed by _chunk_to_text
   - on_chat_model_end → usage_metadata extracted by _TokenAccountant
      ★ Local servers (Ollama, vLLM) DO populate usage_metadata
        following the OpenAI standard. We tested this. Token counts
        are real.
      ★ For older / non-standard local servers, fallback to
        response_metadata.token_usage (prompt_tokens / completion_tokens)
        — see §14 (Token meter).
```

**Model discovery — `/model` REPL command**:
```
cli/models_catalog.py:discover_from_endpoint(api_url, api_key)
   url = api_url.rstrip('/') + '/v1/models' (or just /models if /v1 in URL)
   GET <url> with Authorization: Bearer <key>
      ★ Ollama responds with {data: [{id: "qwen2.5:7b", ...}, ...]}
        — exactly the OpenAI format. Live discovery just works.
      ★ vLLM same.
      ★ llama.cpp server: also OpenAI-format /v1/models endpoint.
      ★ TGI (HuggingFace Text Generation Inference): /v1/models supported
        in recent versions.
   Returns ModelEntry(name=id, source='discovered').
```

**Cost gating**: `output/cost.py:_resolve_pricing` matches
`llama`/`qwen`/`ollama` substrings to `(0, 0)` — explicitly free, the
`$` figure displays as `$0.00`. For an arbitrary local model name that
doesn't match those substrings (e.g. `internal-tuned-model-v3`),
`is_priced()` returns False and the `$` segment is suppressed entirely
(see §14). Token counts always display — those come from the API's
real response, not pricing math.

**Common failures**:
- **Connection refused**: local server isn't running. `curl http://...:.../v1/models`
  manually to verify.
- **`/model` picker empty for local server**: server doesn't expose
  `/v1/models`. Falls back to curated; if the api_url doesn't match a
  curated substring (likely for private deployments), the picker shows
  empty. Workaround: use `/model <name>` directly. Long-term: add a
  curated entry keyed by the org's api_url substring.
- **All token counts zero**: server doesn't populate `usage_metadata`
  AND doesn't populate `response_metadata.token_usage`. Some older
  llama.cpp server versions do this. Update the server or accept that
  token tracking won't work for that deployment.
- **Streaming hangs / very slow first token**: local server is
  CPU-bound and slow to load weights on first call. Pre-warm with a
  dummy request before running `revio audit`.
- **Wrong findings from a 7-8B model**: small models reason worse on
  cross-finding patterns. revio's grounding validator still catches
  hallucinated file paths, but the LLM's hypothesis lines may be
  shallow. Reach for a 14-32B or larger model for production.
- **"$0.00" appearing on a paid cloud model**: model name didn't fuzzy-
  match any PRICING entry, so `_resolve_pricing` returned (0, 0). Add
  the entry to PRICING or accept the suppressed `$` (see §14).

**Key invariant**: revio's local-LLM support has NO special-case code
paths. Everything goes through the same `_make_openai` → ChatOpenAI →
stream / usage / cost. If you find yourself writing
`if provider == "ollama":` somewhere, you've made a mistake — that's
exactly what we're avoiding.

---

## 11c. Verilator wrapper (Verilog Layer 2)

**Symptom**: `run_verilator` returns no findings on a clearly buggy
file; bit-width / latch warnings missing

**File trace**:
```
agent/lint_tools.py:make_verilator_tool(ctx) → @tool('run_verilator')
   ↓ user / agent calls it with relative_path
   ↓ ctx.verilator → lazy-builds VerilatorRunner (None if binary missing)
   ↓
layers/static/verilator.py:VerilatorRunner.scan(target):
   - If target is a dir: expand to .v / .vh / .sv / .svh files (recursive)
   - subprocess: ['verilator', '--lint-only', '-Wall', <files>]
   - ★ Verilator emits diagnostics on STDERR, not stdout (this is the
     #1 gotcha when writing wrappers for it).
   - Parse each stderr line via _LINE_RE:
        %(Error|Warning|Info)(-RULE_TAG)?: file:line:col: message
   - Returns list[VerilatorDiagnostic]
```

**Severity / category mapping** (`_map_category`):
```
Synthesis-correctness (survive into silicon) → POTENTIAL_BUG:
  WIDTH / WIDTHEXPAND / WIDTHTRUNC / WIDTHCONCAT (bit-width)
  CASEINCOMPLETE / CASEX / CASEOVERLAP          (latch-inferring case)
  BLKSEQ / BLKANDNBLK                            (blocking in sequential)
  LATCH                                          (inferred combinational latch)
  MULTIDRIVEN / MULTIDRIVE                       (multi-driven nets)
  ASYNC / ASYNCBR                                (async logic / CDC)
  UNDRIVEN / PINMISSING / PINNOTFOUND            (connectivity)
  STMTDLY / INFINITELOOP                         (simulation hazards)
  REALCVT                                        (silent type conversion)

Cosmetic → CODE_STYLE:
  UNUSED / UNUSEDPARAM / UNUSEDSIGNAL
  DECLFILENAME                                   (filename ≠ module)
  EOFNEWLINE
  VARHIDDEN
```

**Common failures**:
- **Empty result on a known-buggy file**: verilator parse error came
  first and aborted later passes. Run `verilator --lint-only <file>`
  manually to see the parse error; usually fixable by adding the right
  `-I<include-dir>` (TODO: revio doesn't pass include dirs yet — feed
  them via REVIO_VERILATOR_BIN wrapper script if needed).
- **VHDL files (.vhd / .vhdl) get no findings**: by design — verilator
  is Verilog-only. VHDL routes to the LLM-only fallback (no profile).
- **Same finding emitted N times for an N-include file**: verilator
  re-emits warnings for each instantiation. revio doesn't dedupe yet
  — manifests as noise on large repos. Workaround: lower `-Wall` to a
  narrower set via `REVIO_VERILATOR_BIN` env pointing at a wrapper
  script.
- **"Filename does not match module name" noise**: that's the
  DECLFILENAME warning; treated as CODE_STYLE (low priority). If your
  project deliberately mixes module-per-file boundaries, this is
  cosmetic and can be ignored.

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
   - llm_usage (real numbers from usage_metadata — see §14)
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
   - _on_llm_usage: inline 'tokens +X in, +Y out (...)' line
   - _on_finding_recorded: severity-badge card
   - _on_reflect: cyan rule + summary
   - _on_session_end: full finding cards + footer stats incl. token totals
```

---

## 14. Token meter + cost (per-call + cumulative)

**Symptom**: token numbers wrong, throughput shows zero, `$` figure
appears for a model where we don't have pricing, or vice versa

**File trace**:
```
agent/runner.py:_TokenAccountant(model)
   ↓
   on `on_chat_model_start` event:
      accountant.on_llm_start()
         self._last_call_start = time.time()
   ↓
   on `on_chat_model_stream` event:
      pass-through — chunk text streamed via 'llm_token' event
   ↓
   on `on_chat_model_end` event:
      out_msg = data.get("output")            ← AIMessage
      usage = getattr(out_msg, "usage_metadata", None)
         ★ LangChain standardized field: {input_tokens, output_tokens, total_tokens}
         ★ FALLBACK: if missing, try response_metadata.token_usage
           with {prompt_tokens, completion_tokens} (some OpenAI-compat
           servers use the old naming).
      ↓
      payload = accountant.on_llm_end(usage)
         elapsed = now - self._last_call_start
         self.input_tokens  += int(usage["input_tokens"])
         self.output_tokens += int(usage["output_tokens"])
         self.call_count    += 1
         throughput = (delta_out / elapsed) if elapsed > 0.01 else 0
         return {
             "model": self.model,                   ← needed by is_priced gate
             "delta_input": ..., "delta_output": ...,
             "total_input": ..., "total_output": ...,
             "throughput_tps": ...,
             "est_cost_usd": estimate_cost_usd(model, total_in, total_out),
             "call_count": ...,
         }
      ↓
      on_event("llm_end", payload)            (existing)
      on_event("llm_usage", payload)          (new, drives the inline line)
   ↓
   ... session continues ...
   ↓
   _build_report(state, config, accountant=accountant):
      Returns ReviewReport with:
         total_input_tokens  = accountant.input_tokens
         total_output_tokens = accountant.output_tokens
         llm_call_count      = accountant.call_count
         est_cost_usd        = accountant.est_cost_usd
```

**Rendering path** (`output/stream.py:StreamRenderer`):
```
_on_llm_usage(payload):
   if delta_in == 0 and delta_out == 0: return    ← stay silent for unknown providers
   from .cost import is_priced
   cost_part = f" · {format_cost(est_cost)}" if is_priced(model) else ""
   print("  · tokens +Xk in, +Y out (Σ Σin / Σout · TPS{cost_part})")

_on_session_end(payload):
   ... existing footer ...
   if tok_in > 0 or tok_out > 0:
      cost_part = f" · {format_cost(cost)}" if is_priced(model) else ""
      print(f"  tokens: {format_count(tok_in)} in · {format_count(tok_out)} out · ...{cost_part}")
```

**Pricing matcher** (`output/cost.py`):
```
_resolve_pricing(model) → (input_$/1M, output_$/1M)
   - Lowercase the model name
   - Pick the LONGEST substring match in PRICING dict
     (so 'deepseek-v4-pro-0524' matches 'deepseek-v4-pro', not 'deepseek')
   - Return (0.0, 0.0) for any unmatched model

is_priced(model) → bool
   - Returns True iff _resolve_pricing(model) is not (0, 0)
   - Used as a gate: when False, the renderer drops the '$' segment.
     Token counts and throughput still print.
```

**Common failures**:
- **All tokens show 0**: provider didn't populate `usage_metadata`
  AND didn't populate `response_metadata.token_usage`. Add a provider-
  specific extraction branch in `_dispatch_event`'s `on_chat_model_end`
  handler.
- **Throughput shows '—' constantly**: `delta_output` is zero (the call
  ended without producing text) OR `elapsed < 0.01s` (very fast cached
  response). Both are correct; the dash is intentional.
- **`$0.00` appears**: model name matched a `(0, 0)` entry (intentional
  for `llama` / `qwen` / `ollama` local models). For an unknown model
  that you DO want to see priced, add an entry to `PRICING`.
- **`$` appears for unknown model**: `is_priced` returned True
  spuriously — check the substring match isn't being shadowed by a
  longer key.

---

## 15. /model picker + live discovery

**Symptom**: `/model` doesn't show the actual models available on a new
provider's endpoint, or the picker is empty for a known provider

**File trace**:
```
cli/repl.py:_cmd_model(arg, cfg, state)
   ↓
   if arg == "list" or arg == "ls":
      → fall to discovery path, print only
   if arg:
      → cfg.llm.model = arg; save_user_config(cfg); done
   else:
      → discovery path + interactive questionary picker
   ↓
   cli/models_catalog.py:list_models_for(api_url, api_key)
      → discover_from_endpoint(api_url, api_key)
         url = api_url.rstrip('/') + ('/models' if '/v1' in url else '/v1/models')
         urllib.request with Bearer api_key, SSL context = certifi
            ★ MUST use certifi context — macOS Python's default trust store
              fails on legitimate certs ('CERTIFICATE_VERIFY_FAILED').
         Parse {"data": [{"id": "..."}]} → list of ModelEntry(source="discovered")
         ★ Returns [] on ANY exception — network, auth, parse — never raises.
      → If discovered is empty: fall back to curated_for(api_url)
         curated_for iterates _CURATED dict; returns entries whose
         substring key matches api_url.
      → If both empty: return []
      → If discovered won: merge curated 'note' fields onto discovered
        entries when names match (so live deepseek-v4-pro gets the
        'strongest, recommended' note from curated).
```

**Common failures**:
- **`/model` returns empty for a known provider**: API key invalid or
  endpoint changed. Check `discover_from_endpoint` against the URL
  manually with curl. Curated fallback should still produce results
  if the api_url substring matches.
- **`/model` discovers nothing for a new vendor**: their endpoint
  doesn't expose `/v1/models`. Two options: (1) add a curated entry
  in `_CURATED` keyed on a substring of their api_url, or (2) tell
  the user to use `/model <name>` directly.
- **`CERTIFICATE_VERIFY_FAILED`**: certifi import failed. Ensure
  `certifi` is in install_requires. The code falls back to system
  trust store if certifi is missing, which fails on macOS — install
  certifi.
- **Live discovered model has no pricing**: expected for new vendors.
  See §14 — the `$` figure is intentionally suppressed when
  `is_priced` is False.

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

## 13c. Adding analyzers after install (`revio analyzers`)

**Symptom**: user wants C++ / Kotlin / etc. coverage after the initial
install. Re-running the installer is overkill — there's a dedicated
subcommand.

**File trace**:
```
cli/main.py:analyzers_app   ← Typer subgroup
   ├── revio analyzers              → _analyzers_print_status (status table)
   ├── revio analyzers list         → same
   ├── revio analyzers install <C>  → analyzers_install
   └── revio analyzers menu         → interactive picker (uses questionary)
   ↓
cli/analyzers.py
   REGISTRY = [AnalyzerSpec(code='j', label='JS / TS', check_cmd='oxlint',
                            npm_id='oxlint', ...), ...]
   ↓
   parse_letters('jcs')  ← case-insensitive, dedup, ignore spaces
                         ← '*' = full registry
                         ← unknown letters bubble up so the CLI can warn
   ↓
   install_one(spec)
     1. is_installed(spec)? → return InstallOutcome(status='already')
     2. is_pip_into_venv (sqlfluff)? → `sys.executable -m pip install <pkg>`
     3. npm available + spec has npm_id? → `npm install -g <pkg>`
     4. platform == macOS + brew + spec.brew_pkg? → `brew install <pkg>`
        platform == linux + apt + spec.apt_pkg?  → `sudo apt-get install <pkg>`
        platform == windows + winget + spec.winget_id? → `winget install ...`
                          OR + scoop + spec.scoop_id?  → `scoop install ...`
     5. Nothing matched → InstallOutcome(status='skipped',
                                          note=spec.manual_hint or 'no PM')
   ↓
   Output (rich-formatted):
     ✓ [letter]  Label  (already installed)
     ✓ [letter]  Label  (via brew)
     ✗ [letter]  Label  failed via winget: rc=...
     · [letter]  Label
         needs JDK; download detekt-cli from GitHub
```

**Invariant**: REGISTRY in `cli/analyzers.py` MUST match the menus in
`scripts/install.sh` + `scripts/install.ps1`. When adding a new analyzer:
edit all three. The smoke test could enforce this but doesn't (TODO).

**Common failures**:
- **Letter not recognized**: typo. `revio analyzers list` shows the
  legitimate letters.
- **Install hangs**: package manager waiting on a sudo prompt or
  network. Run the underlying command manually (`sudo apt-get install
  cppcheck` etc.) to see the real error.
- **Status table says 'installed' but agent says 'not found'**: the
  binary is on PATH for one shell but not the one revio started in.
  Restart revio. (Or PATH inheritance bug — check `$env:PATH` /
  `echo $PATH`.)

---

## 13d. Output-language routing (i18n)

**Symptom**: agent returns English findings even when user asked in
Chinese / German / etc.

**No special routing layer** — this is enforced 100% by the prompt:

`src/revio/agent/prompts.py:SYSTEM_PROMPT`'s 'Output language' section
declares TWO buckets:

```
USER LANGUAGE bucket (mirrors whatever the user typed):
  Finding.title / hypothesis / suggestion / counter_considered
  reflect summary
  systemic_observations entries
  Plan text shown in the stream Panel

ENGLISH bucket (always — for tool / log compatibility):
  All tool call args (read_file('src/auth.py'), regex queries, ...)
  Evidence.summary strings that quote verbatim tool output
  Severity / Category enum values
```

`PLAN_PROMPT` and `REFLECT_PROMPT` mirror the same rule. The LLM
infers user language from conversation history (the user's request is
in the message stream). No regex detection, no language-tag plumbing.

**Why it works without a detection layer**:
- Modern LLMs (Claude / DeepSeek / Mistral / Qwen / mimo) reliably
  mirror the input language when explicitly instructed.
- Tool args staying English avoids the trap of path translation
  (revio always calls `read_file("src/auth.py")`, never `read_file("源/认证.py")`).
- Evidence quotes staying English ensures bandit / cppcheck / etc.
  output is preserved verbatim (their rules are English-named).

**Common failures**:
- **Findings come back English when user asked in CJK**: usually a
  smaller / older model not following the instruction. Try a larger
  model. Or set `REVIO_DEBUG=1` and inspect what the system prompt
  actually delivered — sometimes a wizard misconfigured `disable_thinking`
  on a thinking-only model and the system message got truncated.
- **Tool args got translated** (e.g. agent tries to read a Chinese path
  that doesn't exist): same root cause — model didn't honor the
  two-bucket rule. Prompt is explicit enough that this shouldn't
  happen on Claude 4 / DeepSeek-V3 / Mistral-Large-2 / Qwen-2.5+.

---

## 13e. Owl mascot timing (startup + between tasks)

**Symptom**: animation flickers / doesn't appear / appears too often

**File trace**:
```
cli/repl.py:_print_banner(cfg, state)             # REPL startup
   from .mascot import play_startup_animation
   play_startup_animation(_console)               # ~1.2s on startup

cli/repl.py:_handle_nl_input(line, cfg, state)    # after every NL task
   try:
       run_agent_sync(...)                        # the actual task
   finally:
       from .mascot import play_startup_animation
       _console.print()
       play_startup_animation(_console)           # ~1.2s after task
       _console.print()
```

**Why finally:**, not else: — the mascot plays whether the agent
succeeded, raised, or got KeyboardInterrupted. Visual reset feels
honest regardless of outcome.

**Slash commands do NOT trigger the animation** — those are config /
discovery actions (cheap, instantaneous), not "tasks". Hard-coded by
which dispatcher routes them (_handle_slash vs _handle_nl_input).

**Suppression**:
- `REVIO_NO_MASCOT=1` env var → animation skipped entirely
- `not sys.stdout.isatty()` → animation skipped (CI, piped output)
- `console.is_terminal == False` → animation skipped

**Animation script** (`cli/mascot.py:_SCRIPT`):
```
7 frames, 1.20s total:
  idle (0.18s) → left-scan (0.22s) → idle (0.10s) →
  right-scan (0.22s) → idle (0.10s) → both-lit (0.18s) → rest (0.20s)
```

Eye glyphs: `EYE_REST_RAW = ' ◉ '` (3-col), `EYE_SCAN_RAW = '(◉)'` (3-col).
Slot width is fixed so the silhouette never jitters between rest and
scan. If you ever change them, keep the column count equal or the
body box-drawing will misalign.

**Common failures**:
- **Mascot appears twice on startup**: someone called
  play_startup_animation outside _print_banner. There's exactly one
  startup call site; if you see two, search for stray calls.
- **No animation despite TTY**: REVIO_NO_MASCOT is set, or
  `console.is_terminal` returns False (Rich's auto-detect failed —
  forcing `Console(force_terminal=True)` is a workaround).

---

## 13f. UI language is English-only (design choice)

**Symptom**: someone tries to localize the wizard / banner / slash
commands and is wondering why we don't.

**Decision**: ALL of the following are English-only forever:
- Wizard prompts (`cli/wizard.py`)
- REPL banner (`cli/repl.py:_print_banner`)
- Slash command help text + `/help` output
- Install scripts (`scripts/install.sh`, `install.ps1`)
- `revio config show`, `revio analyzers`, `revio fix history`, etc.
- Error messages from revio itself (not from the underlying tool)
- All `docs/` (ARCHITECTURE, INTERNALS, BENCHMARKS, DEMO_SCRIPT)
- README

**Why**: screenshots, support tickets, Stack Overflow Q&A, and CI logs
should look identical regardless of the user's locale. A French
developer pasting an error message into a Czech colleague's IDE
shouldn't get a translation mismatch.

**What DOES localize**: the agent's user-facing reasoning fields
(see §13d 'Output-language routing'). That's the only locale-sensitive
surface — the LLM's response to the user's actual question.

**If you find Chinese / German / etc. in user-displayed strings outside
the agent's response**: that's a bug. Search and replace. The
`_KEYWORD_RULES` in cli/repl.py:563 is the ONE exception — keyword
patterns are non-user-facing matching logic, not display text.

---

## 13g. Letter-coded analyzer menu

**Symptom**: someone wants to add a 14th analyzer / change the codes /
the menu doesn't show their language.

**File trace**:
```
Three places that MUST stay in sync (no tooling enforces this yet):

  src/revio/cli/analyzers.py:REGISTRY     ← `revio analyzers ...`
  scripts/install.ps1   :$AnalyzerMap     ← Windows installer Stage 6b
  scripts/install.sh    :ANALYZERS array  ← macOS/Linux installer Stage 6b
```

All three list the same 12 analyzers with the same letters. The codes
are mnemonics, mostly the language's first letter; collisions resolved
by taking the next salient letter:

```
j = JS/TS      c = C/C++       g = Go        r = Rust
a = jAva       s = Shell       l = Lua       q = sQl (S taken)
v = Verilog    u = rUby        h = pHp       k = Kotlin
```

`parse_letters("jcs")` walks character-by-character, lowercases,
strips spaces, deduplicates while preserving first-appearance order,
and returns (specs, unknown_letters). `*` resolves to all. Empty
resolves to nothing.

**Common failures**:
- **'unknown letter' on a code that should work**: probably a typo, or
  someone removed the spec from REGISTRY but the docs / install scripts
  still mention it. Check all three files.
- **New analyzer doesn't appear in `revio analyzers`**: only added to
  REGISTRY in analyzers.py. Make sure scripts/install.{ps1,sh} also
  list it under the menu so installer flow is consistent.

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
| Tests fail after a change | Run all 9 smoke tests in tests/ |
| Findings come back English when user asked in CJK | Model not following bucket-rule; see §13d. Try a larger model |
| `revio analyzers install` says skipped | No package manager available for that OS+analyzer; follow the `manual_hint` in the output |

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
| `tests/test_fix_history_smoke.py` | Fix-history: apply→undo (no git), undo-of-undo=redo, target old session, oversized files skipped, auto-rotation at cap | Deterministic |
| `tests/test_phase2_linters_smoke.py` | 7 phase-2 linters: shellcheck/sqlfluff/verilator live subprocess; luacheck/rubocop/phpstan/detekt NotInstalled fallbacks; ToolContext lazy properties | Deterministic (gated on binary presence) |

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
