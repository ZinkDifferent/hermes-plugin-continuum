# Continuum — Session Continuity Engine + Enforcement Harness for Hermes Agent

A Hermes Agent plugin that consolidates enforcement gates (Charon/Circe/Veritas patterns) into a single turn-state machine and adds the continuity layer Hermes lacks: gap detection with task resumption, temporal grounding, skill routing per task, and a persistent connection registry.

Production-verified over five days (~330 turns) alongside [MemoryOS](https://github.com/ClaudioDrews/memory-os) and GLM-5.3-Flash — see [Recommended Stack](#recommended-stack) below.

Continuum registers **six hooks** spanning the full turn lifecycle — `on_session_start`, `pre_llm_call`, `pre_tool_call`, `post_tool_call`, `post_llm_call`, `transform_llm_output` — plus the `/continuum` command.

## What It Does

### A. Enforcement Gates (`pre_tool_call` / `post_tool_call` / `transform_llm_output`)

| Gate | Function |
|------|----------|
| **Gate 1** — verify-before-act | Action tools (terminal, write_file, browser, etc.) require a verification tool call (read_file, search_files, session_search, skill_view, web_search...) in the same turn |
| **Gate 2** — one-failure lockout | Only genuine infrastructure failures (timeouts, connection refused, DNS, SSH auth) lock the turn. Ordinary command failures and benign not-found outcomes never lock |
| **Gate 3** — timeout arithmetic | Blocks commands whose internal `--max-time` exceeds the terminal timeout — prevents the classic self-timeout trap |
| **Gate 6** — SSH/connection guard | One-strike SSH auth rule, known-host registry, credential-guessing block |
| **Gate V** — venture separation | Blocks (pre-execution) any command writing one venture's credential onto another venture's infrastructure. Release requires explicit user acknowledgment: `/continuum ack-venture <name>` (30-minute TTL) |
| **Gate P1/P4** — claim provenance | Appends a visible `[PROVENANCE AUDIT]` footer when the final response asserts availability/state ("X is callable", "X is up") without a same-turn tool-verified anchor. Includes the list≠callable rule: a models/directory listing cannot anchor a callability claim |
| **Gate 3-new** — verification-before-conclusion | After two same-method failed probes, an "offline/down" conclusion gets a `[VERIFICATION GAP]` footer unless a third, different-method probe ran first |
| **Task-skill gate** | Action tools require task-matched skills to be loaded first (when a task is set via `/continuum task`) |

Error classification (refined): `EXPECTED` (test runners, guarded commands, benign not-found) and `COMMAND` (ordinary nonzero exits) never lock. `INFRA` (timeouts, connection failures, auth failures, OOM) locks the turn and requires user guidance — preventing cascading retry spirals.

Pre-tool gates block **before execution** — the tool call never runs. Prose gates fire on `transform_llm_output`, after the response is generated — they cannot delete the answer, so they **append a visible audit footer** naming the unanchored claim instead of silently passing.

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

### C. Claim Verification (`transform_llm_output` — verifier.py)

| Feature | Function |
|---------|----------|
| **Pre-flight claim verification** | Every factual claim (digits, $ amounts, dates, proper nouns) in the agent's response is cross-checked against the turn's actual tool outputs + fact store + injected memory via an independent glm-5.3-flash judge session |
| **FAIL-CLOSED mode** | Unverified claims are REMOVED before the response ships — never tagged, never passed silently |
| **Verify-before-release bounce** | Unverified claims bounce back to the agent with a remediation block; the agent must run REAL tool calls and rewrite (max 2 bounces, then hard removal) |
| **N-turn lookback buffer** | Evidence window includes tool outputs from the previous **N=5 turns** (adjustable via `state.LOOKBACK_TURNS`), not just the current turn — eliminates false-positive bounces on claims referencing earlier-turn data in long conversations |
| **Trace-marker bypass** | Sentences with explicit evidence markers ([tool output], [user message], [facts]) ship untagged |

## Changelog

### v0.3.0 (Sep 04 2026)

**feat: N-turn lookback buffer for verifier evidence window**

- `state.py`: Added `LOOKBACK_TURNS = 5` constant + `_tool_output_history` rolling deque. `new_turn()` shifts the previous turn's `tool_outputs_this_turn` into the history buffer before resetting.
- `verifier.py` (NEW FILE): Full pre-flight claim verification pipeline — Layer 1 deterministic claim extraction, Layer 2 independent glm-5.3-flash judge, FAIL-CLOSED mode, verify-before-release bounce loop.
- `_turn_evidence()` expanded: evidence blocks now include tool outputs from the previous 5 turns (labeled `[HIST-{turn_id}-TOOL-{n}]`), not just the current turn.
- `continuity.py`: `verify_response()` wired into `on_transform_llm_output` pipeline, running before provenance footer audit.

**Motivation:** verify-gate false positives in long conversations — claims referencing data from earlier turns bounced because the evidence window was current-turn-only. The lookback buffer (N=5, adjustable to 8/10 via `state.LOOKBACK_TURNS`) gives the judge access to previous turns' tool outputs.

### v0.2.0 (Aug 29 2026)

- Gates V (venture separation), P1/P4 (claim provenance), 3-new (verification-before-conclusion)
- Verify-gate bounce mechanism with 2-round cap
- `/continuum ack-venture` command
- Gate 2 lockout message renamed to CONTINUUM'S CIRCE/GATE 2 LOCKOUT

## Installation

```bash
git clone https://github.com/ZinkDifferent/hermes-plugin-continuum.git ~/.hermes/plugins/continuum
hermes gateway restart
```

Then verify:

```bash
# Run the test suites (44 unit checks; 23 integration checks through the real host hook dispatch)
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
/continuum ack-venture <name> Acknowledge cross-venture key placement (30-min TTL)
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