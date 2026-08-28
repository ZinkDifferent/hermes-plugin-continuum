"""Standalone unit tests for continuum modules (no Hermes runtime needed).

Run: python3 test_continuum.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import continuum.state as state
import continuum.gates as gates
import continuum.connections as connections
import continuum.continuity as continuity

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


def verified_turn(turn_id="t", user_message=""):
    """Fresh turn that has passed verification (so gates focus on what we test)."""
    state.new_turn(turn_id)
    state.turn()["user_message"] = user_message
    gates.on_pre_tool_call(tool_name="read_file", args={})
    return state.turn()


class FakeState:
    def __init__(self):
        self._data = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


class FakeCtx:
    def __init__(self):
        self.state = FakeState()


state.init(FakeCtx())
state.clear_task()

print("== state ==")

check("session defaults present", state.session().get("active_task") is None)
check("turn state fresh", state.turn()["locked"] is False)

# ── Task resolution ────────────────────────────────────────────────────

matched = state.match_task_to_skills("we need to restart the proxmox VM for grommunio")
check("proxmox+grommunio matched", "proxmox-ve" in matched and "grommunio" in matched, str(matched))

matched2 = state.match_task_to_skills("track my fedex package")
check("fedex tracking matched", "shipment-tracker" in matched2, str(matched2))

matched3 = state.match_task_to_skills("hello there")
check("no false match", len(matched3) == 0, str(matched3))

res = state.resolve_skills_for_task("proxmox work")
st = state.session()
check("required skills recorded", st["required_skills"] == ["proxmox-ve"], str(st["required_skills"]))

# ── Gate: skill-required (Req 3) — before other gate tests ────────────

print("== gate: skill required ==")
# Hermetic fixture: the skill-gate tests must not depend on which skills a
# given machine has installed (VMs carry a minimal library). Guarantee the
# fixture skills resolve as available, then restore.
_avail_backup = state._available_skills
state._available_skills = lambda: _avail_backup() | {"proxmox-ve", "grommunio", "shipment-tracker", "withings-health", "protocol-state-management"}
# Re-resolve under patched availability: on machines without the fixture
# skills installed, the earlier resolve recorded them as skills_missing,
# and the gate then (correctly) declines to block. Re-running makes the
# session state match the hermetic fixture on every machine.
state.resolve_skills_for_task("proxmox work")
t = verified_turn("tskill")
r = gates.on_pre_tool_call(tool_name="terminal", args={"command": "ls"})
check("blocks action without task skill", r is not None and "SKILL REQUIRED" in (r.get("message") or ""), str(r))
t["skills_loaded"].add("proxmox-ve")  # simulate skill_view
r = gates.on_pre_tool_call(tool_name="terminal", args={"command": "ls"})
check("passes after skill loaded", r is None, str(r))

# Missing-skill tasks don't hard-block (skill doesn't exist to load)
state.resolve_skills_for_task("proxmox work")  # found=proxmox-ve presumably
# Simulate a missing skill scenario directly:
state.update_session(lambda s: s.update({"required_skills": ["nonexistent-skill"], "skills_missing": ["nonexistent-skill"]}))
t2 = verified_turn("tskill2")
r = gates.on_pre_tool_call(tool="terminal", command="ls")
check("missing skills do NOT block", r is None, str(r))
state._available_skills = _avail_backup  # restore real skill discovery

# Clear task for subsequent gate tests
state.clear_task()

# ── Gate 1: verify-before-act ──────────────────────────────────────────
# NOTE: callbacks receive HOST-shaped kwargs (tool_name, args={...}),
# matching hermes_cli/plugins.py:6477 dispatch.

print("== gate 1: verify-before-act ==")
state.new_turn("t1")
r = gates.on_pre_tool_call(tool_name="terminal", args={"command": "ls"})
check("blocks terminal without verification", r is not None and "VERIFY" in (r.get("message") if isinstance(r, dict) else r), str(r))

r = gates.on_pre_tool_call(tool_name="read_file", args={})
check("verification tool passes", r is None, str(r))

r = gates.on_pre_tool_call(tool_name="terminal", args={"command": "ls"})
check("terminal passes after verification", r is None, str(r))

# ── Error classification (refined interpretation) ──────────────────────

print("== error classification ==")
k = gates.classify_failure(command="python3 test_continuum.py",
                           result={"exit_code": 1, "output": "FAIL  something"})
check("test failure = expected", k == "expected", k)

k = gates.classify_failure(command="ls foo",
                           result={"exit_code": 0, "output": ""})
check("clean exit = None", k is None, k)

k = gates.classify_failure(command="", result={"output": "process poll: session_id no longer exists (not_found)"})
check("benign not_found = expected", k == "expected", k)

k = gates.classify_failure(error="session id no longer exists (not_found)")
check("error-wrapped not_found = expected", k == "expected", k)

k = gates.classify_failure(command="curl https://x", result={"exit_code": 28, "output": "command timed out"})
check("timeout = infra", k == "infra", k)

k = gates.classify_failure(command="ssh x", result={"exit_code": 255, "output": "Permission denied (publickey,password)."})
check("ssh auth fail = infra", k == "infra", k)

k = gates.classify_failure(command="grep pattern file.txt", result={"exit_code": 1, "output": ""})
check("grep no-match = expected", k == "expected", k)

k = gates.classify_failure(command="make build", result={"exit_code": 2, "output": "error: syntax"})
check("ordinary nonzero = command (no lock)", k == "command", k)

# ── Lockout only on infra failures ────────────────────────────────────

print("== lockout semantics ==")
t = verified_turn("tl1")
gates.on_post_tool_call(tool_name="terminal", args={"command": "python3 test_x.py"},
                        result={"exit_code": 1, "output": "FAIL"})
check("expected failure does NOT lock", t["locked"] is False)

gates.on_post_tool_call(tool_name="terminal", args={"command": "make build"},
                        result={"exit_code": 2, "output": "error: syntax"})
check("command failure does NOT lock", t["locked"] is False)

# False-positive regression (Aug 27 audit): grep output CONTAINING a fault
# string is quoted data, not a live fault — must never lock.
t = verified_turn("tl1b")
gates.on_post_tool_call(
    tool_name="terminal",
    args={"command": "grep 'killed:' errors.log"},
    result={"exit_code": 0, "output": "...WARNING killed: 1..."})
check("grep output containing 'killed:' does NOT lock", t["locked"] is False)

gates.on_post_tool_call(tool_name="terminal", args={"command": "curl -s https://x --max-time 5"},
                        result={"exit_code": 28, "output": "command timed out after 5s"})
check("infra failure DOES lock", t["locked"] is True)
check("failure recorded", len(t["action_failures"]) >= 1, str(t["action_failures"]))

r = gates.on_pre_tool_call(tool_name="terminal", args={"command": "echo hi"})
check("locked turn blocks action tools", r is not None and "LOCKOUT" in (r.get("message") or ""), str(r))

r = gates.on_pre_tool_call(tool_name="read_file", args={})
check("verification still allowed when locked", r is None, str(r))

# ── Gate 3: timeout arithmetic ─────────────────────────────────────────

print("== gate 3: timeout arithmetic ==")
t = verified_turn("t2")

r = gates.on_pre_tool_call(
    tool_name="terminal",
    args={"command": "curl -s --max-time 15 https://example.com", "timeout": 15},
)
check("equal timeouts blocked", r is not None and "TIMEOUT" in (r.get("message") or ""), str(r))

r = gates.on_pre_tool_call(
    tool_name="terminal",
    args={"command": "curl -s --max-time 15 https://example.com", "timeout": 30},
)
check("staggered timeouts pass", r is None, str(r))

r = gates.on_pre_tool_call(
    tool_name="terminal",
    args={"command": "curl -s https://example.com", "timeout": 15},
)
check("no internal timeout passes", r is None, str(r))

# ── Gate 6: SSH guard ──────────────────────────────────────────────────

print("== gate 6: ssh guard ==")
t = verified_turn("t3")

r = gates.on_pre_tool_call(
    tool_name="terminal",
    args={"command": "sshpass -p 'x' ssh root@203.0.113.5 'ls'"},
)
check("unknown host warned", r is not None and "SSH GUARD" in (r.get("message") or ""), str(r))

connections.record("203.0.113.5", user="root", cred_source="user-provided")
r = gates.on_pre_tool_call(
    tool_name="terminal",
    args={"command": "sshpass -p 'x' ssh root@203.0.113.5 'ls'"},
)
check("known host passes", r is None, str(r))

gates.on_post_tool_call(
    tool_name="terminal", args={"command": "sshpass -p 'x' ssh root@203.0.113.5 'ls'"},
    result={"exit_code": 255, "output": "Permission denied (publickey,password)."})
check("auth failure recorded", state.session()["ssh_auth_failed"].get("203.0.113.5", 0) >= 1,
      str(state.session()["ssh_auth_failed"]))

t = verified_turn("t4")
r = gates.on_pre_tool_call(
    tool_name="terminal",
    args={"command": "sshpass -p 'y' ssh root@203.0.113.5 'ls'"},
)
check("retry blocked after auth failure", r is not None and "already failed" in (r.get("message") or ""), str(r))

# ── Continuity: gap detection ──────────────────────────────────────────

print("== continuity: gap detection ==")
state.update_session(lambda s: s.update({
    "last_turn_at": time.time() - 6 * 3600,
    "active_task": "Proxmox VM migration",
    "connections": {"192.168.1.10": {"user": "root", "cred_source": "/etc/mysql/conf.d/my.cnf"}},
}))

state.new_turn("t5")
ctx_block = continuity.on_pre_llm_call(turn_id="t5", user_message="ok continuing")
check("resumption block injected", "TASK RESUMPTION" in ctx_block, ctx_block[:200])
check("resumption mentions task", "Proxmox VM migration" in ctx_block)
check("resumption mentions cred source", "my.cnf" in ctx_block)
check("gap_resumed flagged", state.session().get("gap_resumed") is True)

# Simulate the turn completing → last_turn_at updated
continuity.on_post_llm_call(response="Continuing the migration.")

state.new_turn("t6")
ctx_block2 = continuity.on_pre_llm_call(turn_id="t6", user_message="next step")
check("no resumption without gap", "TASK RESUMPTION" not in ctx_block2, ctx_block2[:200])
check("temporal grounding present", "TEMPORAL GROUNDING" in ctx_block2)

# ── Continuity: amnesia detection runs ─────────────────────────────────

print("== continuity: amnesia detection ==")
state.turn()["injected_blocks"] = ["resumption", "temporal"]
continuity.on_post_llm_call(response="What are the credentials for that server again?")
check("amnesia check runs without crash", True)

# ── Skill digest ───────────────────────────────────────────────────────

print("== skill digest ==")
block = continuity._skill_digest_block(["proxmox-ve"])
check("digest runs", isinstance(block, str))

# ── Status report ──────────────────────────────────────────────────────

print("== status ==")
rep = state.status_report()
check("status renders", "Continuum status" in rep)
rep2 = connections.report()
check("connections report renders", "203.0.113.5" in rep2)

print()
print(f"RESULTS: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
