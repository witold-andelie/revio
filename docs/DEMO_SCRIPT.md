# revio · Demo recording script

A **3-minute terminal demo** you can record with `asciinema` and convert
to gif. Hits the highest-impact features: wizard → audit (auto-emit
+ grounded findings) → dedup --fix (file actually changes) → cross-
session memory.

---

## Setup (one-time, before recording)

```bash
# 1. Install asciinema + agg (gif converter)
brew install asciinema
cargo install --git https://github.com/asciinema/agg     # OR brew install agg

# 2. Prepare a clean state
cd "/Users/mac/Documents/New project/revio"

# 3. Confirm DeepSeek API key works
.venv/bin/revio config show         # verify provider/model

# 4. Create a clean demo fixture (we'll mutate it during recording)
mkdir -p /tmp/revio_demo/src && cd /tmp/revio_demo
git init -b main -q
git config user.email demo@local && git config user.name demo
cat > src/utils.js << 'EOF'
function formatUserName(first, last) {
  const result = `${first} ${last}`;
  return result.trim();
}

function buildDisplayName(firstName, lastName) {
  const display = `${firstName} ${lastName}`;
  return display.trim();
}

function deadHelper() {
  return Math.random();
}

module.exports = { formatUserName, buildDisplayName };
EOF
cat > src/auth.py << 'EOF'
"""Auth module with deliberate vulnerabilities for demo."""

import hashlib
import os
import pickle


def get_user(user_id):
    query = f"SELECT * FROM users WHERE id = {user_id}"
    return db.execute(query)


def login(username, password):
    hashed = hashlib.md5(password.encode()).hexdigest()
    query = f"SELECT * FROM users WHERE name='{username}' AND pass='{hashed}'"
    return db.execute(query).fetchone()


def load_session(filename):
    with open(os.path.join("/sessions", filename), "rb") as f:
        return pickle.load(f)
EOF
git add -A && git commit -q -m "demo seed"
cd "/Users/mac/Documents/New project/revio"
```

---

## Recording (run this)

### Start asciinema

```bash
cd "/Users/mac/Documents/New project/revio"
asciinema rec /tmp/revio_demo.cast \
  --title "revio: agentic code review CLI" \
  --idle-time-limit 2 \
  --rows 32 --cols 110
```

Now you're recording. Follow the script below — **don't think, just type**.
Idle time is auto-trimmed to 2 seconds so you can pause to read what's on screen.

---

### Beat 1: setup — "what is revio" (10 seconds)

```
clear
echo "=== revio: agentic code review CLI ==="
echo "  · 6 static analyzers · 17 language profiles"
echo "  · RAG · Skills · MCP client · --fix"
echo
.venv/bin/revio --help | head -15
```

### Beat 2: audit a vulnerable Python file (~50 seconds)

```
clear
echo "=== Audit a deliberately-vulnerable Python file ==="
cat /tmp/revio_demo/src/auth.py
echo
echo "Watch: bandit auto-emits findings, agent adds semantic context..."
echo
.venv/bin/revio audit /tmp/revio_demo --profile python --budget 12
```

Pause 3-4 seconds after it finishes — let viewers read the finding cards.

### Beat 3: cross-session memory (5 seconds, no API call)

```
clear
echo "=== Re-run on the same repo — see 'still present' tracking ==="
.venv/bin/revio audit /tmp/revio_demo --profile python --budget 8 2>&1 | grep -A 4 "Cross-run"
```

This call is fast (mostly cache). The "📌 Still present: N" line is the
money shot — shows persistent memory across sessions.

### Beat 4: dedup --fix actually edits files (~60 seconds)

```
clear
echo "=== dedup mode finds AI-generated duplicates ==="
cat /tmp/revio_demo/src/utils.js
echo
echo "--- agent proposes structural fixes ---"
.venv/bin/revio dedup /tmp/revio_demo --profile js --budget 15 --fix --yes
```

Important: `--yes` skips interactive prompts since you're recording.
For a more dramatic version (showing the questionary UI), drop `--yes`
and type "Yes, apply" manually — but that's harder to demo without
TTY hiccups.

### Beat 5: prove it actually changed the file (10 seconds)

```
echo "--- did the agent really change the file? ---"
cd /tmp/revio_demo && git diff src/utils.js && cd -
```

The diff shows `-function buildDisplayName(...)` etc. — the real magic moment.

### Beat 6: configuration & end (10 seconds)

```
clear
echo "=== revio is configurable & multi-provider ==="
.venv/bin/revio config show 2>&1 | head -10
echo
echo "Switch model anytime:  revio config edit"
echo "Or via slash command:  > /model claude-opus-4-5"
echo
echo "Full docs:  docs/ARCHITECTURE.md  docs/INTERNALS.md  docs/BENCHMARKS.md"
echo
echo "Thanks for watching!"
```

### Stop recording

Press **Ctrl-D** or type `exit`.

---

## Convert to gif

```bash
# Method 1: agg (recommended — pure Rust, fast)
agg /tmp/revio_demo.cast /tmp/revio_demo.gif \
  --theme monokai --font-size 14 --cols 110 --rows 32 \
  --speed 1.2

# Method 2: asciinema's upstream tool (slower but works)
# pip install asciinema-edit && ...

# Final gif file:
open /tmp/revio_demo.gif
ls -lh /tmp/revio_demo.gif    # expect ~2-5 MB
```

**Target file size**: keep gif under 5 MB so it embeds in GitHub / Twitter.
If it's larger, drop the font size to 12 or reduce frame rate.

---

## Tips for a polished recording

1. **Resize your terminal to exactly 110×32 BEFORE starting asciinema** — the
   recording is letterbox-fixed at the size you start with
2. **Type slowly** but **steadily** — viewers can't read very fast
3. **Don't fix typos mid-recording** — your typos get edited out by idle-time-limit;
   typing `^U` to clear and retry adds noise
4. **Press Enter, then wait ~1 second before the next command** — gives the
   gif a natural pause beat
5. **The first `revio audit` call takes 30-45 seconds** — that's the longest
   pause. Talk over it in your demo presentation, OR fast-forward in the gif
   conversion (use `--speed 2.5` for that segment)
6. **Pre-warm the cache** — run `.venv/bin/revio audit /tmp/revio_demo --profile python` once
   BEFORE recording, so the HuggingFace download happens off-camera

---

## Alternative shorter version (90 seconds)

If 3 minutes is too long for a slide/tweet:

```bash
asciinema rec /tmp/revio_short.cast --idle-time-limit 1 --rows 32 --cols 110

clear
.venv/bin/revio audit /tmp/revio_demo --profile python --budget 8
# (let it finish — ~40s)
clear
.venv/bin/revio dedup /tmp/revio_demo --profile js --budget 15 --fix --yes
# (let it finish — ~30s)
cd /tmp/revio_demo && git diff && cd -
# Ctrl-D to stop
```

90 seconds, two punchy moments: audit produces critical findings, then dedup
edits real files.

---

## What to say over the demo (for live presentation)

Optional voiceover script if you're presenting live:

> "This is revio — an agentic code review CLI. Behind the curtain it's
> LangGraph orchestrating six static analyzers across 17 language profiles.
> Watch:
>
> [Beat 2] — bandit finds 7 deterministic security issues. The agent then
> reads the code, adds semantic context, and emits hypothesis-evidence
> findings with line-precise locations. None of this is just LLM
> guessing — every finding cites a tool call.
>
> [Beat 3] — second run shows the cross-session memory: 'still present'
> tracking. Same code, same findings, but revio knows it's the same set.
>
> [Beat 4-5] — `dedup --fix` doesn't just say 'this is a duplicate' —
> it generates a structured patch and applies it to disk. `git diff` shows
> the agent really deleted the duplicate function and updated the module
> exports.
>
> [Beat 6] — multi-provider, configurable, ready for any organization."

Total speaking time: ~2 min 30 sec. Pauses naturally during command execution.

---

## Troubleshooting the recording

| Problem | Fix |
|---|---|
| `asciinema: command not found` | `brew install asciinema` |
| `agg: command not found` | `cargo install --git https://github.com/asciinema/agg` (needs Rust toolchain) |
| Terminal resize during recording | Restart; asciinema captures terminal size at the START |
| Audio in recording | asciinema only records text — no audio. Add voiceover post-hoc in Quicktime / ffmpeg |
| Gif too large | Reduce `--font-size 12`, `--cols 100`, or increase `--speed 1.5` |
| Long pauses (waiting for LLM) | Set `--idle-time-limit 1` to auto-trim pauses to 1 second |
| Want a slow-motion version | `--speed 0.5` in agg → 2× longer gif, easier to follow |
