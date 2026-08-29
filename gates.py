"""Continuum gates: pre_tool_call enforcement + post_tool_call state updates.

Host contract (verified in hermes_cli/plugins.py 6421-6495, model_tools.py 1136):
  pre_tool_call receives: tool_name=..., args={...}, task_id, session_id, ...
  A BLOCK must be returned as {"action": "block", "message": str}.
  Plain-string returns are silently ignored by the host (dead code).
  post_tool_call receives: tool_name=..., args={...}, result=<raw>, status,
  error_type, error_message. The command lives at args["command"].

Gate 1 — verify-before-act (action tool requires a verification tool this turn)
Gate 2 — one-failure lockout (only INFRA failures lock; see below)
Gate 3 — timeout arithmetic guard (curl/wget --max-time vs terminal timeout)
Gate 6 — SSH/connection guard (known-host injection, password-guess block,
         one-auth-attempt rule, credential-self-retrieval requirement)

Error interpretation (refined per user direction):
  A nonzero exit code or tool error is NOT automatically an action failure.
  Failure classes:
    EXPECTED — benign/expected nonzero outcomes that carry information but
      are not faults. Includes: test runners (pytest, unittest, ...),
      guarded commands (`|| true`), diff/grep with no matches, AND
      benign not-found/state-gone responses from query tools
      ("not_found", "no longer exists", "session id not found",
      "process registry cleared", empty result sets). Logged, never locks.
    COMMAND — ordinary nonzero exits. Signal to the model; never locks.
    INFRA — genuine execution-environment faults: timeouts, connection
      refused/reset, DNS resolution, SSH auth failures, killed/OOM.
      These and ONLY these lock the turn.

INFRA classification is COMMAND-SHAPED (2026-08-27 audit fix): a fault
signature only locks when the command itself is a network/long-running
operation that could genuinely produce it. Reading a log file that
CONTAINS the word "timed out" is not a timeout — greps, cats, and other
read-only inspection commands can never be infra failures by content
alone. This killed the 4/4 false-positive lockouts found in production
logs (including one where a grep over an error log containing "killed:"
locked the turn).
"""

import logging
import re
import time

from . import state
from . import connections

logger = logging.getLogger(__name__)

_enabled = True

# ── Gate V (venture separation, 2026-08-28 corrective build) ────────────
# Hosts known to belong to a specific venture. Commands that WRITE
# credentials/secrets (env-file appends, key injection, heredocs writing
# keys) targeting a host of venture A while carrying a key labeled venture
# B are blocked pending explicit user acknowledgment.
# Key provenance is detected from the SOURCE PATH of the staged key
# (/tmp/orkey.txt-style temp files are unlabeled → treated as the last
# venture the key was staged for, tracked in session state).

_VENTURE_HOSTS = {
    "184.105.199.114": "astera",
    "asteraintelligence.com": "astera",
    "184.105.199.113": "thinkingmachine",
    "thinkingmachine.me": "thinkingmachine",
    "hermes-macmini-1": "personal",
    "100.95.3.16": "personal",
}

# Commands that move credentials onto a machine
_CRED_WRITE_RE = re.compile(
    r"(>>?\s*[^\s]*\.env\b|tee\s+-a\s*[^\s]*\.env|"
    r"echo\s+[A-Z_]*(KEY|TOKEN|SECRET|PASSWORD)[A-Z_]*=)"
    r"|"
    r"(scp|rsync|ssh)\s+[^\s]*orkey[^\s]*"
    r"|"
    r"(shred|rm)\s+(-f\s+)?[^\s]*orkey",
    re.I,
)

# Explicit acknowledgment flag lives in session state:
#   st["venture_key_ack"] = {"target_venture": <name>, "at": <ts>}
# Set ONLY by the user via /continuum ack-venture <venture> (30-min TTL).


def _venture_of_host(host):
    if not host:
        return None
    return _VENTURE_HOSTS.get(host) or _VENTURE_HOSTS.get(str(host).strip())


def _venture_guard(command):
    """Block cross-venture credential writes unless user acknowledged."""
    if not command or not _CRED_WRITE_RE.search(command):
        return None
    st = state.session()
    # Which venture is this key FOR? Track last-staged key venture.
    key_venture = st.get("staged_key_venture")
    if not key_venture:
        return None  # unlabeled staging — cannot judge; don't block
    # Which venture is the target host?
    target_venture = None
    for host, venture in _VENTURE_HOSTS.items():
        if host in command:
            target_venture = venture
            break
    if not target_venture or target_venture == key_venture:
        return None
    ack = st.get("venture_key_ack") or {}
    if ack.get("venture") == key_venture and (time.time() - ack.get("at", 0)) < 1800:
        return None  # acknowledged within 30 min
    return (
        f"VENTURE SEPARATION: this command writes a credential belonging to "
        f"the '{key_venture}' venture onto infrastructure of the "
        f"'{target_venture}' venture. Ventures must stay ENTIRELY separate. "
        f"If the user has explicitly approved this specific cross-venture "
        f"placement, they can acknowledge with: /continuum ack-venture "
        f"{key_venture} (valid 30 minutes). Otherwise, do not proceed."
    )


def _stage_key_venture(command):
    """Record which venture a staged key file belongs to, from the command
    that stages it (e.g. writing the key to /tmp from a user message that
    names the venture, or scp'ing a venture's key file around)."""
    if not command:
        return None
    low = command.lower()
    # The staging command's context: venture keywords near the key write
    for venture in ("astera", "thinkingmachine"):
        marker = f"{venture}-key" if "key" in low else venture
        if venture in low and (".txt" in low or ".env" in low or "orkey" in low or "/tmp" in low):
            if re.search(r"(write|echo|printf|tee|>|scp)", low):
                def _rec(st):
                    st["staged_key_venture"] = venture
                state.update_session(_rec)
                return venture
    return None


def is_enabled():
    return _enabled


def set_enabled(v: bool):
    global _enabled
    _enabled = bool(v)


# ── Host-contract helpers ────────────────────────────────────────────────

def _tool_of(kwargs):
    """Host passes tool_name; tests may pass tool. Accept both."""
    tool = kwargs.get("tool_name") or kwargs.get("tool") or ""
    return str(tool)


def _args_of(kwargs):
    """Host nests tool arguments under args (dict). Accept top-level too."""
    a = kwargs.get("args")
    if isinstance(a, dict):
        return a
    # Fallback for legacy/test callers passing fields at top level
    return kwargs


def _command_of(kwargs):
    a = _args_of(kwargs)
    c = a.get("command") or ""
    return c if isinstance(c, str) else str(c)


def _result_text(result):
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return str(result.get("output") or result.get("content") or "")
    return str(result or "")


# ── Error classification (refined interpretation) ──────────────────────

# Command shapes whose nonzero exit is expected signal, not fault
_TEST_RUNNER_RE = re.compile(
    r"\b(pytest|unittest|python3?\s+-m\s+pytest|python3?\s+.*test_\w+\.py|"
    r"\btox\b|\bnose2?\b|cargo\s+test|go\s+test|npm\s+(?:run\s+)?test)\b", re.I)
_GUARDED_RE = re.compile(r"\|\|\s*(true|echo)")
_DIFF_GREP_RE = re.compile(r"^\s*(diff|grep|rg)\b")

# Benign not-found / state-gone outcomes from query-style tools.
# These mean "the thing you asked about isn't there" — an ANSWER, not a fault.
_BENIGN_NOTFOUND_SIGNATURES = (
    "not_found",
    "not found",
    "no longer exists",
    "does not exist",
    "session id no longer exists",
    "session_id not found",
    "process registry",
    "already exited",
    "already completed",
    "no such process",
    "no results",
    "0 matches",
    "no matches",
)

# Genuine infrastructure fault signatures — the ONLY class that locks.
_INFRA_SIGNATURES = (
    "command timed out",
    "timed out after",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "name resolution",
    "could not resolve host",
    "permission denied (publickey",
    "permission denied, please try again",
    "authentication failed",
    "killed:",
    "out of memory",
)

# Commands that CAN genuinely produce infra faults. Only these may lock
# on a content signature. Read-only inspection commands (grep/cat/ls/
# find/head/tail/awk/sed/wc/which/file/stat/echo/printf/true/false/test,
# python reading files, git log/show/diff/status...) are excluded even
# when their output mentions a fault string.
_INFRA_CAPABLE_RE = re.compile(
    r"\b(curl|wget|ssh|scp|sftp|sshpass|rsync|nc|netcat|telnet|ftp|"
    r"ping|dig|nslookup|host|git\s+(push|pull|fetch|clone|ls-remote)|"
    r"pip3?\s+install|pipx|uv\s+(pip\s+)?install|npm\s+(install|ci|run)|"
    r"brew\s+install|apt(-get)?\s+install|brew|make|cmake|"
    r"docker\s+(build|pull|push|compose)|hermes\s+update|tar|gunzip|"
    r"systemctl|launchctl|brew\s+(upgrade|update)|softwareupdate)\b",
    re.I,
)

# Read-only inspection commands: their output text is DATA, never a
# live fault report about the command itself. Never lock on these.
_READ_ONLY_INSPECTION_RE = re.compile(
    r"^\s*(grep|egrep|fgrep|rg|ag|cat|head|tail|less|more|ls|find|"
    r"awk|sed\s+-n|wc|which|file|stat|echo|printf|true|false|test|"
    r"python3?\s+-c\s|python3?\s+-m\s+json\.tool|jq|sqlite3|git\s+(log|show|diff|status|grep)|"
    r"ps\b|lsof|du\b|df\b)\b",
    re.I,
)


def _find_signatures(text, signatures):
    low = (text or "").lower()
    return [sig for sig in signatures if sig in low]


def classify_failure(command="", result=None, error=None):
    """Return 'expected' | 'command' | 'infra' | None.

    Precedence: benign-not-found beats tool-error; infra beats expected
    (an ssh auth failure during a test run still locks) — BUT only when
    the command shape could genuinely have produced the fault.
    """
    # Tool-level errors (exceptions raised by the tool itself)
    if error and not isinstance(error, bool):
        err_text = str(error)
        out_text = _result_text(result)
        if _find_signatures(err_text + " " + out_text, _BENIGN_NOTFOUND_SIGNATURES):
            return "expected"
        return "infra"

    exit_code = None
    output = _result_text(result)
    if isinstance(result, dict):
        exit_code = result.get("exit_code")
    elif isinstance(result, str):
        m = re.search(r"exit_code[\"']?\s*[:=]\s*(\d+)", result)
        if m:
            exit_code = int(m.group(1))

    combined = output

    # Infra first: a real fault hides behind any command shape —
    # but only if the command could genuinely have caused one.
    if _find_signatures(combined, _INFRA_SIGNATURES):
        if _READ_ONLY_INSPECTION_RE.match(command or ""):
            # Output is quoted data (log lines, file contents). A grep
            # that prints "timed out" did not time out.
            pass
        elif _INFRA_CAPABLE_RE.search(command or ""):
            return "infra"
        # Command shapes outside both lists: treat as non-infra signal.
        # Conservative: avoids locking on unknown read-only shapes.

    # Benign answers
    if _find_signatures(combined, _BENIGN_NOTFOUND_SIGNATURES):
        return "expected"

    if exit_code in (None, 0):
        return None

    # Expected-failure command shapes
    if (_TEST_RUNNER_RE.search(command or "")
            or _GUARDED_RE.search(command or "")
            or _DIFF_GREP_RE.search((command or "").strip())):
        return "expected"

    # Everything else nonzero: ordinary command failure — signal only
    return "command"


# ── Gate 3 helpers ──────────────────────────────────────────────────────

_MAXTIME_RE = re.compile(r"--(?:max-time|maxtime)\s+(\d+)")
_TIMEOUT_FLAG_RE = re.compile(r"(?:^|\s)timeout\s+(\d+)")


def _timeout_arithmetic_violation(command, terminal_timeout):
    """Return violation description or None."""
    worst = 0
    for rx in (_MAXTIME_RE, _TIMEOUT_FLAG_RE):
        for m in rx.finditer(command or ""):
            try:
                worst = max(worst, int(m.group(1)))
            except ValueError:
                continue
    if worst and terminal_timeout is not None:
        if terminal_timeout < worst + 5:
            return (
                f"Command uses an internal timeout of {worst}s but the "
                f"terminal timeout is {terminal_timeout}s. Set terminal "
                f"timeout to at least {worst + 10}s so the command can exit "
                f"on its own."
            )
    return None


# ── Gate 6 helpers ──────────────────────────────────────────────────────

_SSH_RE = re.compile(r"\b(ssh|scp|sshpass|sftp)\b")


def _ssh_target(command):
    """Extract host from ssh-ish commands."""
    m = re.search(r"(?:ssh|scp|sftp)\s+(?:[^\s]+\s+)?(?:[\w.-]+@)([\w.\-]+)", command)
    if m:
        return m.group(1)
    m = re.search(r"ssh\s+([\w.\-]+)\s*$", command.strip())
    if m:
        return m.group(1)
    return None


def _ssh_guard(command):
    st = state.session()
    host = _ssh_target(command)
    if not host:
        return None
    known = st.get("connections", {}).get(host)
    fails = st.get("ssh_auth_failed", {}).get(host, 0)
    if fails >= 1:
        return (
            f"SSH GUARD: auth to {host} already failed this session ({fails}x). "
            f"One attempt = stop and ask the user for correct credentials. "
            f"Do NOT retry."
        )
    if not known:
        return (
            f"SSH GUARD: Host {host} not in connection registry. Before "
            f"connecting: confirm credentials with the user or search known "
            f"cred-file locations on reachable servers. Never guess passwords."
        )
    return None


# ── Gate 3-new helpers: failed-probe tracking ───────────────────────────
# Verification-before-conclusion: after 2 failed probes of the SAME target,
# an "offline/unreachable/down" conclusion requires a third, DIFFERENT-method
# probe before it may be stated as fact. (Session case: mini declared
# offline after 2 same-method probes; a different-method probe would have
# shown it reachable.)

_CONCLUSION_RE = re.compile(
    r"\b(is|are)\s+(offline|down|unreachable|not responding|not reachable)\b",
    re.I,
)

def _conclusion_guard(response_text, turn_state):
    """Return a footer warning if an offline-conclusion lacks a
    different-method probe after 2+ failures. Observer-level (transform)."""
    t = turn_state or {}
    fails = t.get("probe_failures") or []
    if len(fails) < 2:
        return ""
    if not response_text or not _CONCLUSION_RE.search(response_text):
        return ""
    methods = {f.get("method") for f in fails if isinstance(f, dict)}
    # different-method probe already run this turn?
    probed = t.get("probe_methods_used") or set()
    if len(probed) > len(methods):
        return ""
    return (
        "\n\n---\n[VERIFICATION GAP] This response concludes a target is "
        "offline/down after only failed same-method probes. Before stating "
        "this as fact: run a third, DIFFERENT-method probe (e.g. if SSH "
        "failed twice, try ping, the hypervisor console, or another "
        "protocol) — or qualify the claim as unverified."
    )


# ── pre_tool_call ───────────────────────────────────────────────────────

def on_pre_tool_call(**kwargs):
    """Return {"action": "block", "message": str} to block, or None to pass.

    The host IGNORES plain-string returns from pre_tool_call
    (hermes_cli/plugins.py _get_pre_tool_call_directive_details:
    'if not isinstance(result, dict): continue').
    """
    if not _enabled:
        return None

    tool = _tool_of(kwargs)
    t = state.turn()

    # Turn-state updates from this call itself
    if tool in state.VERIFICATION_TOOLS:
        t["verification_called"] = True
    if tool == "skill_view":
        a = _args_of(kwargs)
        name = a.get("name") or a.get("skill_name")
        if name:
            t["skills_loaded"].add(str(name))

    def _block(msg):
        return {"action": "block", "message": msg}

    # Gate 2 — lockout check first (only infra failures lock)
    if tool in state.ACTION_TOOLS and t["locked"]:
        return _block(
            "CIRCE LOCKOUT: an infrastructure failure already occurred this "
            "turn. All further action tools are blocked. Inform the user of "
            "the failure and ask for guidance before proceeding."
        )

    # Gate 1 — verify-before-act
    if tool in state.ACTION_TOOLS and not t["verification_called"]:
        return _block(
            "VERIFY BEFORE ACT: no verification tool (read_file, search_files, "
            "session_search, skill_view, web_search...) has been called this "
            "turn. Check available evidence first, then retry."
        )

    # Task-skill gate (Req 3): action tools require task skills loaded when
    # the active task has required skills AND those skills exist.
    st = state.session()
    required = [s for s in (st.get("required_skills") or [])
                if s not in (st.get("skills_missing") or [])]
    if tool in state.ACTION_TOOLS and required:
        missing = set(required) - t["skills_loaded"]
        if missing and not st.get("no_skill_ack"):
            return _block(
                f"SKILL REQUIRED: active task requires {', '.join(sorted(required))}. "
                f"Not yet loaded this turn: {', '.join(sorted(missing))}. "
                f"Call skill_view for them first, or have the user run "
                f"/continuum task clear to drop the task."
            )

    # Command-level checks
    command = _command_of(kwargs)
    if command and tool in ("terminal", "process"):
        # Gate 3-new — probe tracking for verification-before-conclusion
        t.setdefault("commands_this_turn", []).append(command)
        # Gate V — venture separation (credential writes)
        _stage_key_venture(command)
        vproblem = _venture_guard(command)
        if vproblem:
            return _block(vproblem)
        # Gate 3 — timeout arithmetic
        a = _args_of(kwargs)
        tt = a.get("timeout")
        violation = _timeout_arithmetic_violation(command, tt)
        if violation:
            return _block(f"TIMEOUT ARITHMETIC: {violation}")
        # Gate 6 — SSH guard
        if _SSH_RE.search(command):
            problem = _ssh_guard(command)
            if problem:
                return _block(problem)
        # Track connections from ssh commands carrying user@host
        target = _ssh_target(command)
        if target and "@" in command:
            user_m = re.search(r"([\w.\-]+)@" + re.escape(target), command)
            connections.record(target, user=user_m.group(1) if user_m else "?",
                               source="command-observed")

    return None


# ── post_tool_call ──────────────────────────────────────────────────────

def on_post_tool_call(**kwargs):
    if not _enabled:
        return None
    tool = _tool_of(kwargs)
    result = kwargs.get("result")
    error = kwargs.get("error")
    command = _command_of(kwargs)

    t = state.turn()
    # Gate 3-new — verification-before-conclusion bookkeeping: a
    # verification tool that RETURNED a result counts as an anchor.
    # (Runs OUTSIDE the ACTION_TOOLS branch — verification tools are a
    # different set.)
    if tool in state.VERIFICATION_TOOLS and result is not None:
        t.setdefault("verified_tools", set()).add(tool)
    if tool in state.ACTION_TOOLS:
        klass = classify_failure(command=command, result=result, error=error)

        if klass == "infra":
            t["action_failures"].append(tool)
            t["locked"] = True
            logger.warning(
                "continuum: INFRA FAILURE on %s — turn locked (%s)",
                tool, (str(result) or str(error))[:200])
        elif klass == "command":
            t["action_failures"].append(tool)
            logger.info(
                "continuum: command failure on %s (non-locking): %s",
                tool, str(result)[:150])
        elif klass == "expected":
            logger.info(
                "continuum: expected outcome on %s — no lock (%s)",
                tool, (str(result) or "")[:120])
        # klass None → success

    # SSH auth-failure detection (one-strike rule) — only for real ssh usage
    if tool in ("terminal", "process"):
        out_text = _result_text(result)
        low = out_text.lower()
        if _SSH_RE.search(command or "") and any(sig in low for sig in (
            "permission denied (publickey,password)",
            "permission denied, please try again",
            "authentication failed",
        )):
            target = _ssh_target(command) or "unknown"

            def _mark(s):
                s.setdefault("ssh_auth_failed", {})
                s["ssh_auth_failed"][target] = s["ssh_auth_failed"].get(target, 0) + 1
            state.update_session(_mark)
            logger.warning("continuum: SSH auth failure recorded for %s", target)

        # Gate 3-new — failed-probe tracking (connectivity-class failures,
        # by method: ssh / ping / dns / http)
        low_cmd = (command or "").lower()
        if _INFRA_SIGNATURES and _find_signatures(low, ("timed out", "connection refused", "connection reset", "no route to host", "name resolution", "could not resolve host")):
            method = "ssh" if _SSH_RE.search(low_cmd) else (
                "ping" if "ping" in low_cmd else (
                "dns" if any(x in low_cmd for x in ("dig ", "nslookup", "host ")) else (
                "http" if any(x in low_cmd for x in ("curl", "wget")) else "other")))
            target = _ssh_target(command) or "unknown"
            t.setdefault("probe_failures", []).append(
                {"target": target, "method": method})
            t.setdefault("probe_methods_used", set()).add(method)

    return None