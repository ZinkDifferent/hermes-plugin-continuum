"""Continuum gates: pre_tool_call enforcement + post_tool_call state updates.

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
"""

import logging
import re

from . import state
from . import connections

logger = logging.getLogger(__name__)

_enabled = True


def is_enabled():
    return _enabled


def set_enabled(v: bool):
    global _enabled
    _enabled = bool(v)


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

# Genuine infrastructure faults — the ONLY class that locks the turn
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


def _find_signatures(text, signatures):
    low = (text or "").lower()
    return [sig for sig in signatures if sig in low]


def classify_failure(command="", result=None, error=None):
    """Return 'expected' | 'command' | 'infra' | None.

    Precedence matters: benign-not-found beats tool-error (a query tool
    returning not_found IS its answer even if wrapped in an error field);
    infra beats expected (an ssh auth failure during a test run still locks).
    """
    err_text = ""
    if error and not isinstance(error, bool):
        err_text = str(error)
        out_text = ""
        if isinstance(result, dict):
            out_text = str(result.get("output") or "")
        elif isinstance(result, str):
            out_text = result

        # Tool error wrapping a benign answer → expected
        if _find_signatures(err_text + " " + out_text, _BENIGN_NOTFOUND_SIGNATURES):
            return "expected"
        return "infra"

    exit_code = None
    output = ""
    if isinstance(result, dict):
        exit_code = result.get("exit_code")
        output = str(result.get("output") or "")
    elif isinstance(result, str):
        m = re.search(r"exit_code[\"']?\s*[:=]\s*(\d+)", result)
        if m:
            exit_code = int(m.group(1))
        output = result

    combined = output

    # Infra first: a real fault hides behind any command shape
    if _find_signatures(combined, _INFRA_SIGNATURES):
        return "infra"

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


# ── pre_tool_call ───────────────────────────────────────────────────────

def on_pre_tool_call(**kwargs):
    """Return error string to block, or None to pass."""
    if not _enabled:
        return None

    tool = kwargs.get("tool") or kwargs.get("tool_name") or ""
    t = state.turn()

    # Turn-state updates from this call itself
    if tool in state.VERIFICATION_TOOLS:
        t["verification_called"] = True
    if tool == "skill_view":
        name = kwargs.get("name") or kwargs.get("skill_name")
        if name:
            t["skills_loaded"].add(str(name))

    # Gate 2 — lockout check first (only infra failures lock)
    if tool in state.ACTION_TOOLS and t["locked"]:
        return (
            "CIRCE LOCKOUT: an infrastructure failure already occurred this "
            "turn. All further action tools are blocked. Inform the user of "
            "the failure and ask for guidance before proceeding."
        )

    # Gate 1 — verify-before-act
    if tool in state.ACTION_TOOLS and not t["verification_called"]:
        return (
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
            return (
                f"SKILL REQUIRED: active task requires {', '.join(sorted(required))}. "
                f"Not yet loaded this turn: {', '.join(sorted(missing))}. "
                f"Call skill_view for them first, or have the user run "
                f"/continuum task clear to drop the task."
            )

    # Command-level checks
    command = kwargs.get("command") or ""
    if command and tool in ("terminal", "process"):
        # Gate 3 — timeout arithmetic
        tt = kwargs.get("timeout")
        violation = _timeout_arithmetic_violation(command, tt)
        if violation:
            return f"TIMEOUT ARITHMETIC: {violation}"
        # Gate 6 — SSH guard
        if _SSH_RE.search(command):
            problem = _ssh_guard(command)
            if problem:
                return problem
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
    tool = kwargs.get("tool") or kwargs.get("tool_name") or ""
    result = kwargs.get("result")
    error = kwargs.get("error")
    command = kwargs.get("command") or ""

    t = state.turn()
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
        out_text = ""
        if isinstance(result, str):
            out_text = result
        elif isinstance(result, dict):
            out_text = str(result.get("output") or "")
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

    return None
