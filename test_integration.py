"""Integration tests: continuum hooks against the REAL Hermes host dispatch.

Unlike test_continuum.py (unit tests with imagined signatures), this file
calls hermes_cli.plugins._dispatch_pre_tool_call_hooks and the real
invoke_hook("post_tool_call") payload shape, proving that blocks actually
reach the model.

Run with the Hermes venv:
  /Users/hzink/.hermes/hermes-agent/venv/bin/python test_integration.py
"""

import os
import sys

# Source resolution: HERMES_HOME env or ~/.hermes (same as the host).
_SOURCE_HOME = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")

# State isolation: by default the suite runs against a throwaway HERMES_HOME
# (real hermes-agent/plugins source symlinked in, state store isolated), so
# test fixtures can never pollute live plugin state on any installation.
# Opt back into the live state store with CONTINUUM_TEST_LIVE_STATE=1.
if os.environ.get("CONTINUUM_TEST_LIVE_STATE") == "1":
    _HERMES_HOME = _SOURCE_HOME
else:
    import tempfile
    _HERMES_HOME = tempfile.mkdtemp(prefix="continuum-test-home-")
    for _d in ("hermes-agent", "plugins"):
        _src = os.path.join(_SOURCE_HOME, _d)
        if os.path.isdir(_src):
            os.symlink(_src, os.path.join(_HERMES_HOME, _d))
    os.environ["HERMES_HOME"] = _HERMES_HOME  # host resolves state from this

HERMES_SRC = os.path.join(_HERMES_HOME, "hermes-agent")
PLUGINS_DIR = os.path.join(_HERMES_HOME, "plugins")
for p in (HERMES_SRC, PLUGINS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import continuum.state as state
import continuum.gates as gates
import continuum.continuity as continuity
from hermes_cli.plugins import _dispatch_pre_tool_call_hooks, PluginManager

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class FakeState:
    def __init__(self):
        self._d = {}

    def get(self, k, d=None):
        return self._d.get(k, d)

    def set(self, k, v):
        self._d[k] = v


# Wire the plugin the way the real host would: PluginContext + manager,
# then register the raw callback the way register_hook does.
_pm = PluginManager()
state.init(type("C", (), {"state": FakeState()})())
state.clear_task()
# Host-shaped registration (callbacks receive exactly what the host sends)
_pm._hooks.setdefault("pre_tool_call", []).append(gates.on_pre_tool_call)
_pm._hooks.setdefault("post_tool_call", []).append(gates.on_post_tool_call)

# CRITICAL: _dispatch_pre_tool_call_hooks routes through the module-global
# manager (get_plugin_manager). Adopting our manager via the documented
# test seam (plugins.py:6054 — monkeypatch _plugin_manager directly) makes
# the real dispatch path use OUR callbacks.
import hermes_cli.plugins as _P
_P._plugin_manager = _pm


def host_pre(tool_name, args, **kw):
    """Exactly what the host passes (agent/tool_executor.py:647)."""
    return _dispatch_pre_tool_call_hooks(
        tool_name, args,
        task_id=kw.get("task_id", "t"), session_id=kw.get("sid", "s"),
        tool_call_id="tc", turn_id=kw.get("turn", "tr"),
        api_request_id="", middleware_trace=[],
    )


def fresh_verified(turn="v"):
    state.new_turn(turn)
    host_pre("read_file", {"path": "/tmp/x"})
    return state.turn()


print("== 1. verify-before-act: REAL host dispatch ==")
state.new_turn("i1")
block, mod = host_pre("terminal", {"command": "ls /"})
check("unverified action BLOCKED via real dispatch",
      block is not None and "VERIFY" in block, repr(block))

fresh_verified("i1b")
block, mod = host_pre("terminal", {"command": "ls /"})
check("verified action passes via real dispatch", block is None, repr(block))

print("== 2. command read from nested args (host shape) ==")
fresh_verified("i2")
block, mod = host_pre("terminal", {"command": "curl -s --max-time 10 https://x", "timeout": 10})
check("timeout arithmetic block via nested args",
      block is not None and "TIMEOUT ARITHMETIC" in block, repr(block))

fresh_verified("i2b")
block, mod = host_pre("terminal", {"command": "curl -s --max-time 10 https://x", "timeout": 30})
check("staggered timeout passes", block is None, repr(block))

print("== 3. SSH guard via real dispatch ==")
fresh_verified("i3")
block, mod = host_pre("terminal", {"command": "sshpass -p x ssh root@203.0.113.9 'ls'"})
check("unknown ssh host blocked", block is not None and "SSH GUARD" in block, repr(block))

# Record auth failure through HOST-SHAPED post_tool_call (command in args)
state.new_turn("i3b")
fresh_verified("i3b")
host_pre("terminal", {"command": "sshpass -p x ssh root@203.0.113.9 'ls'"})
# simulate the registry being populated (manual add so guard passes)
import continuum.connections as connections
connections.record("203.0.113.9", user="root", cred_source="user-provided")
for cb in _pm._hooks["post_tool_call"]:
    cb(tool_name="terminal", args={"command": "sshpass -p x ssh root@203.0.113.9 'ls'"},
       result='{"exit_code": 255, "output": "Permission denied (publickey,password)."}',
       status="error", error_type=None, error_message=None)
check("auth failure recorded via host kwargs",
      state.session()["ssh_auth_failed"].get("203.0.113.9", 0) >= 1,
      str(state.session()["ssh_auth_failed"]))
fresh_verified("i3c")
block, mod = host_pre("terminal", {"command": "sshpass -p y ssh root@203.0.113.9 'ls'"})
check("retry blocked after auth failure", block is not None and "already failed" in block, repr(block))

print("== 4. lockout semantics via host kwargs ==")
t = fresh_verified("i4")
# command failure: does NOT lock
for cb in _pm._hooks["post_tool_call"]:
    cb(tool_name="terminal", args={"command": "make build"},
       result='{"exit_code": 2, "output": "error: syntax"}',
       status="error", error_type=None, error_message=None)
check("command failure does NOT lock", t["locked"] is False)

# FALSE-POSITIVE REGRESSION (the Aug 27 audit finding): grep output
# CONTAINING "killed:" must not lock.
t2 = fresh_verified("i4b")
for cb in _pm._hooks["post_tool_call"]:
    cb(tool_name="terminal", args={"command": "grep 'killed:' /Users/hzink/.hermes/logs/errors.log"},
       result='{"exit_code": 0, "output": "2026-08-22 WARNING ... killed: 1\\n...more log lines..."}',
       status="ok", error_type=None, error_message=None)
check("grep output containing 'killed:' does NOT lock", t2["locked"] is False, str(t2["action_failures"]))

# grep with nonzero exit (no matches) — still just signal
t3 = fresh_verified("i4c")
for cb in _pm._hooks["post_tool_call"]:
    cb(tool_name="terminal", args={"command": "grep pattern file.txt"},
       result='{"exit_code": 1, "output": ""}', status="ok",
       error_type=None, error_message=None)
check("grep no-match does NOT lock", t3["locked"] is False)

# genuine infra: real network command that timed out
t4 = fresh_verified("i4d")
for cb in _pm._hooks["post_tool_call"]:
    cb(tool_name="terminal", args={"command": "curl -s https://x --max-time 5"},
       result='{"exit_code": 28, "output": "command timed out after 5s"}',
       status="error", error_type=None, error_message=None)
check("curl timeout DOES lock", t4["locked"] is True, str(t4["action_failures"]))

# locked turn blocks action tools via real dispatch
block, mod = host_pre("terminal", {"command": "echo hi"})
check("locked turn blocks via real dispatch", block is not None and "LOCKOUT" in block, repr(block))
block, mod = host_pre("read_file", {"path": "/tmp/x"})
check("verification still allowed when locked", block is None, repr(block))

print("== 5. skill-required gate via real dispatch ==")
state.update_session(lambda s: s.update({"required_skills": ["proxmox-ve"], "skills_missing": []}))
t5 = fresh_verified("i5")
block, mod = host_pre("terminal", {"command": "ls"})
check("action blocked without task skill", block is not None and "SKILL REQUIRED" in block, repr(block))
t5["skills_loaded"].add("proxmox-ve")
block, mod = host_pre("terminal", {"command": "ls"})
check("passes after skill loaded", block is None, repr(block))
state.clear_task()

print("== 6. post_llm_call host kwargs (amnesia) ==")
state.new_turn("i6")
ctx_block = continuity.on_pre_llm_call(turn_id="i6", user_message="ok continuing")
# force a resumption-armed turn
state.turn()["injected_blocks"].append("resumption")
captured = []
import logging as _lg


class _H(_lg.Handler):
    def emit(self, rec):
        captured.append(rec.getMessage())


_lg.getLogger("continuum.continuity").addHandler(_H())
_lg.getLogger("continuum.continuity").setLevel(_lg.WARNING)
continuity.on_post_llm_call(
    assistant_response="What are the credentials for that server again?",
    user_message="ok", conversation_history=[], model="m", platform="cli")
check("amnesia violation detected via assistant_response kwarg",
      any("AMNESIA VIOLATION" in m for m in captured), str(captured))

print("== 7. venture separation gate (2026-08-28 build) ==")
# Stage an astera key, then attempt a credential write to thinkingmachine host
state.new_turn("i7")
state.update_session(lambda s: s.update({"staged_key_venture": "astera", "venture_key_ack": None}))
host_pre("read_file", {"path": "/tmp/x"})  # verify
# register both hosts as known connections (else the pre-existing SSH
# guard blocks first — correct behavior, wrong for THIS test's focus)
import continuum.connections as _conn
_conn.record("184.105.199.113", user="root", cred_source="test")
_conn.record("184.105.199.114", user="root", cred_source="test")
block, mod = host_pre("terminal", {"command": "echo FAL_KEY=xyz >> /home/memos/.hermes/.env && ssh root@184.105.199.113 true"})
check("cross-venture cred write BLOCKED",
      block is not None and "VENTURE SEPARATION" in block, repr(block))
# same-venture write passes
block, mod = host_pre("terminal", {"command": "echo FAL_KEY=xyz >> /home/memo/.hermes/.env && ssh root@184.105.199.114 true"})
check("same-venture cred write passes",
      block is None, repr(block))
# after user ack, cross-venture passes
state.update_session(lambda s: s.update({"venture_key_ack": {"venture": "astera", "at": __import__('time').time()}}))
block, mod = host_pre("terminal", {"command": "echo FAL_KEY=xyz >> /home/memos/.hermes/.env && ssh root@184.105.199.113 true"})
check("acked cross-venture write passes",
      block is None, repr(block))
state.update_session(lambda s: s.update({"staged_key_venture": None, "venture_key_ack": None}))

print("== 8. provenance transform: list≠callable footer (2026-08-28 build) ==")
state.new_turn("i8")
# simulate list-only evidence: commands that LIST models, verification not real
state.turn()["commands_this_turn"] = ["curl -s https://ollama.com/v1/models -H auth"]
state.turn()["verification_called"] = False
out = continuity.on_transform_llm_output(
    response_text="GLM-5.3-flash is available on this key and callable.",
    session_id="s", model="m", platform="cli")
check("list-only evidence + callability claim -> footer appended",
      out is not None and "PROVENANCE AUDIT" in out, repr(out)[:200])
# real verification suppresses footer
state.new_turn("i8b")
state.turn()["commands_this_turn"] = ["curl -s https://ollama.com/v1/chat/completions -d test"]
state.turn()["verification_called"] = True
out = continuity.on_transform_llm_output(
    response_text="GLM-5.3-flash is callable — live test returned OK.",
    session_id="s", model="m", platform="cli")
check("real-invocation turn -> no footer",
      out is None, repr(out))

print("== 9. verification-before-conclusion footer (2026-08-28 build) ==")
state.new_turn("i9")
state.turn()["probe_failures"] = [
    {"target": "hermes-macmini-1", "method": "ssh"},
    {"target": "hermes-macmini-1", "method": "ssh"},
]
state.turn()["probe_methods_used"] = {"ssh"}
out = continuity.on_transform_llm_output(
    response_text="The mini is offline. It cannot be reached.",
    session_id="s", model="m", platform="cli")
check("2 same-method fails + offline claim -> verification-gap footer",
      out is not None and "VERIFICATION GAP" in out, repr(out)[:200])
# different-method probe run -> no footer. (verified_tools anchors the
# response so the provenance footer also stays silent — an offline claim
# backed by real probes is a tool-verified state assertion.)
state.new_turn("i9b")
state.turn()["probe_failures"] = [
    {"target": "hermes-macmini-1", "method": "ssh"},
    {"target": "hermes-macmini-1", "method": "ssh"},
]
state.turn()["probe_methods_used"] = {"ssh", "ping"}
state.turn()["verified_tools"] = {"terminal"}
state.turn()["verification_called"] = True
out = continuity.on_transform_llm_output(
    response_text="The mini is offline.",
    session_id="s", model="m", platform="cli")
check("different-method probe present -> no footer",
      out is None, repr(out))

print()
print(f"RESULTS: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)