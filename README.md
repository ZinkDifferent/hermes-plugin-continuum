# Continuum — Session Continuity Engine + Enforcement Harness for Hermes Agent

A Hermes Agent plugin that consolidates enforcement gates (Charon/Circe/Veritas patterns) into a single turn-state machine and adds the continuity layer Hermes lacks: gap detection with task resumption, temporal grounding, skill routing per task, and a persistent connection registry.

Production-verified over five days (~330 turns) alongside [MemoryOS](https://github.com/ClaudioDrews/memory-os) and GLM-5.3-Flash — see [Recommended Stack](#recommended-stack) below.

## What It Does

### A. Enforcement Gates (`pre_tool_call` / `post_tool_call`)

| Gate | Function |
|------|----------|
| **Gate 1** — verify-before-act | Action tools (terminal, write_file, browser, etc.) require a verification tool call (read_file, search_files, session_search, skill_view, web_search...) in the same turn |
| **Gate 2** — one-failure lockout | Only genuine infrastructure failures (timeouts, connection refused, DNS, SSH auth) lock the turn. Ordinary command failures and benign not-found outcomes never lock |
| **Gate 3** — timeout arithmetic | Blocks commands whose internal `--max-time` exceeds the terminal timeout — prevents the classic self-timeout trap |
| **Gate 6** — SSH/connection guard | One-strike SSH auth rule, known-host registry, credential-guessing block |
| **Task-skill gate** | Action tools require task-matched skills to be loaded first (when a task is set via `/continuum task`) |

Error classification (refined): `EXPECTED` (test runners, guarded commands, benign not-found) and `COMMAND` (ordinary nonzero exits) never lock. `INFRA` (timeouts, connection failures, auth failures, OOM) locks the turn and requires user guidance — preventing cascading retry spirals.

### B. Continuity Engine (`pre_llm_call` / `post_llm_call`)

| Feature | Function |
|---------|----------|
| **Temporal grounding** | Injects an authoritative local timestamp on **every turn** — relative terms ("yesterday", "earlier today") are resolved against a verified clock, killing temporal drift |
| **Gap resumption** | After any idle gap >30 minutes, injects a resumption block: active task, established connections, credential sources — before the model speaks. Prevents amnesia and "what were we doing?" resets |
| **Task-skill routing** | `/continuum task <name>` maps task keywords to required skills; missing skills are reported, not silently skipped |
| **Skill digest re-injection** | While a task is active, the ENFORCEABLE section of its required skills is re-injected every turn |
| **Connection registry** | Persistent, user-inspectable record of SSH hosts, users, auth methods, and where credentials live (never raw passwords) |
| **Amnesia detection** | Flags responses that ask for context already injected in the resumption block |
| **Relative-time advisory** | Flags relative-time terms in responses (advisory) |

## Installation

```bash
git clone https://github.com/ZinkDifferent/hermes-plugin-continuum.git ~/.hermes/plugins/continuum
hermes gateway restart
```

Then verify:

```bash
# Run the test suite (43 tests)
cd ~/.hermes/plugins/continuum && python3 test_continuum.py

# Check status
/continuum status

# Set a task to activate skill routing
/continuum task "your task name"

# Manage gates
/continuum gates on|off

# Inspect the connection registry
/continuum conn
```

## The `/continuum` Command

```
/continuum status              Full state report
/continuum task <name>         Set active task (+ auto-resolve skills)
/continuum task clear          Drop active task
/continuum conn                Show connection registry
/continuum gates on|off        Toggle enforcement gates
```

## Recommended Stack

Continuum is one of three complementary layers. The layers cover each other's failure modes: the model handles judgment, MemoryOS handles state, Continuum handles time.

### 1. Model: GLM-5.3-Flash

**Why recommended:** GLM-5.3-Flash is explicitly trained for sustained agentic work and long-horizon software engineering — instruction following and tool-call discipline are internalized, not gate-enforced. In five days of production telemetry (~330 turns), the enforcement gates fired **zero** blocking events (vs 379 blocks in the previous Charon/Circe era) — the model self-verifies before acting without ever being blocked. It is natively multimodal (image/video/audio with no assist model) and handles long sessions without compliance drift.

It is hosted on [Ollama Cloud](https://ollama.com/library/glm-5.3-flash:cloud) (`glm-5.3-flash:cloud`, hosted in the US and Europe, zero data retention per Ollama's privacy policy) and is available at the Pro tier — $20/month with 3-instance concurrency — making it the best cost-effective solution for this stack.

### 2. Memory: [MemoryOS](https://github.com/ClaudioDrews/memory-os)

**Why recommended:** Compliance within a turn is the model's job; coherence across turns requires persistent state — which no model can self-provide. MemoryOS implements the lossless-memory thesis: retain everything (Qdrant semantic store, trust-scored structured facts, fabric recall, auto-curated wiki), then surface exactly the right context per turn via surgical injection. In the same five-day window, temporal grounding was injected on 100% of turns and the amnesia detector caught **zero** violations — the model never asked for context it had already been given. MemoryOS is what keeps long sessions grounded regardless of how long they sit idle.

### 3. Continuity: this plugin

Gap resumption after every >30-minute idle gap, temporal grounding every turn, deterministic infra-failure classification (19 turn-lockouts in 5 days — all correct, all prevented cascading retries during flaky remote-server work), and the connection registry so SSH credentials are never re-asked or re-guessed.

### The division of labor

| Layer | Handles | Failure mode it covers |
|-------|---------|------------------------|
| GLM-5.3-Flash | Judgment (within-turn) | Would drift without a clock or memory |
| MemoryOS | State (across sessions) | Lossy summarization, context rot |
| Continuum | Time + containment | Temporal drift, amnesia after gaps, infra failure cascades |

## Configuration

Continuum reads/writes state at `~/.hermes/plugin-data/agent-plugin-continuum-*/state.json`. The task-skill routing map is user-editable at `continuum-data/skill_map.json` (falls back to `DEFAULT_SKILL_MAP` in `state.py`).

`plugin.yaml` declares `requires_skills` — the skills referenced by the routing map. Missing skills are reported via `/continuum status`; the plugin works without them, but task-skill gating is relaxed.

## License

MIT