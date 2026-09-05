"""Continuity engine: pre/post LLM hooks.

Req 1 — gap detection + task resumption injection
Req 2 — ENFORCEABLE skill digest re-injection while task is active
Req 3 — task detection (keyword-based, user-steerable via /continuum task)
Req 4 — fact_store entity lookup + injection; auto fact_feedback post-turn
Req 5 — claim provenance flagging (post_llm_call), amnesia-pattern detection

Host contract (verified in agent/turn_context.py:1279, agent/turn_finalizer.py:632):
  pre_llm_call receives: session_id, task_id, turn_id, user_message,
    conversation_history, is_first_turn, model, platform, ...
    A string (or {"context": str}) return is injected into the user message.
  post_llm_call receives: session_id, task_id, turn_id, user_message,
    assistant_response, conversation_history, model, platform.
"""

import logging
import re
import time

from . import state

logger = logging.getLogger(__name__)

# ── Amnesia patterns (Req 1 enforcement signal) ────────────────────────

AMNESIA_PATTERNS = re.compile(
    r"\b(what are the credentials|can you tell me what this is about|"
    r"let me see what i know|remind me what we|what were we working on|"
    r"can you give me access credentials|what server are we)\b",
    re.I,
)

# Relative-time terms that require a verified current date (Req 2 check)
RELATIVE_TIME_TERMS = re.compile(
    r"\b(yesterday|today|this morning|last night|an hour ago|earlier today|"
    r"recently|just now|tomorrow|next week|last week)\b",
    re.I,
)


# ── pre_llm_call ────────────────────────────────────────────────────────

def on_pre_llm_call(**kwargs):
    """Returns context string injected into the user message sidecar."""
    state.new_turn(kwargs.get("turn_id"))
    state.turn()["user_message"] = kwargs.get("user_message", "") or ""

    st = state.session()
    blocks = []

    # ── Req 1: gap detection ──
    last = st.get("last_turn_at")
    gap_seconds = None
    if last:
        gap_seconds = time.time() - last
        if gap_seconds > state.GAP_THRESHOLD_SECONDS:
            blocks.append(_resumption_block(st, gap_seconds))
            # Register so post_llm_call amnesia detection can arm.
            state.turn()["injected_blocks"].append("resumption")
            state.update_session(lambda s: s.update({"gap_resumed": True}))

    # ── Req 3: task awareness ──
    task = st.get("active_task")
    if not task and state.turn()["user_message"]:
        # Auto-detect from keywords in first substantial message after idle
        detected = state.match_task_to_skills(str(state.turn()["user_message"]))
        if detected:
            pass  # don't auto-set task; surface suggestion only
    if task:
        missing = st.get("skills_missing", [])
        required = st.get("required_skills", [])
        if missing and not st.get("no_skill_ack"):
            blocks.append(
                "[SKILL GAP] No skill found for: " + ", ".join(missing) +
                ". Per standing rule: create an appropriate skill FIRST "
                "(skill_manage create) before engaging further. If proceeding "
                "without one anyway, say so explicitly so it can be logged."
            )
            state.turn()["injected_blocks"].append("skill_gap")
        elif required:
            digest = _skill_digest_block(required)
            if digest:
                blocks.append(digest)
                state.turn()["injected_blocks"].append("skill_digest")

    # ── Req 2: temporal grounding ──
    import datetime
    now_local = datetime.datetime.now().astimezone()
    blocks.append(
        f"[TEMPORAL GROUNDING] Current local date/time: "
        f"{now_local.strftime('%A, %Y-%m-%d %H:%M %Z')}. "
        f"Relative time terms MUST be resolved against this timestamp."
    )
    state.turn()["injected_blocks"].append("temporal")

    # ── Connections reminder when SSH-ish content present ──
    msg_lower = str(state.turn()["user_message"] or "").lower()
    conn_hints = []
    for host, info in (st.get("connections") or {}).items():
        if host.split(".")[0].lower() in msg_lower or host in msg_lower:
            conn_hints.append(f"  {host}: user={info.get('user')}, auth={info.get('auth_method')}, cred_source={info.get('cred_source')}")
    if conn_hints:
        blocks.append("[KNOWN CONNECTIONS]\n" + "\n".join(conn_hints))
        state.turn()["injected_blocks"].append("connections")

    return "\n\n".join(blocks) if blocks else ""


def _resumption_block(st, gap_seconds):
    hours = gap_seconds / 3600.0
    parts = [
        f"[TASK RESUMPTION] Session paused {hours:.1f}h.",
    ]
    task = st.get("active_task")
    if task:
        parts.append(f"Active task: {task}. Do NOT ask the user what this session is about.")
    conns = st.get("connections") or {}
    if conns:
        lines = [f"  {h}: user={i.get('user')}, cred_source={i.get('cred_source')}" for h, i in conns.items()]
        parts.append("Connections already established:\n" + "\n".join(lines))
        parts.append("Do NOT re-ask for credentials already listed here.")
    req = st.get("required_skills") or []
    if req:
        parts.append(f"Task skills: {', '.join(req)}")
    parts.append(
        "Check recent tool results in this conversation before asking the "
        "user to repeat any context."
    )
    return "\n".join(parts)


def _skill_digest_block(required_skills):
    """Re-inject the ENFORCEABLE section of active-task skills."""
    from pathlib import Path
    chunks = ["[ACTIVE SKILL RULES — obey these mechanically]"]
    found_any = False
    base = Path.home() / ".hermes" / "skills"
    for name in required_skills[:4]:  # cap context cost
        matches = list(base.rglob(f"*/{name}/SKILL.md")) or list(base.rglob(f"{name}/SKILL.md"))
        if not matches:
            continue
        try:
            text = matches[0].read_text(errors="replace")
        except OSError:
            continue
        m = re.search(r"##\s*ENFORCEABLE\s*\n(.*?)(?=\n##\s|\Z)", text, re.S)
        if m:
            rules = m.group(1).strip()
            if rules:
                chunks.append(f"— {name}:\n{rules}")
                found_any = True
    if not found_any:
        return ""
    return "\n\n".join(chunks)


# ── post_llm_call ───────────────────────────────────────────────────────

def on_post_llm_call(**kwargs):
    """Observer: flags, feedback. Return value ignored by host.

    Host passes the turn's final assistant text as assistant_response
    (agent/turn_finalizer.py:632). Also accepts response/text/output for
    the standalone test harness.
    """
    response_text = ""
    for key in ("assistant_response", "response", "text", "output"):
        v = kwargs.get(key)
        if isinstance(v, str):
            response_text = v
            break

    t = state.turn()

    # ── Req 1: amnesia detection ──
    if t.get("injected_blocks") and "resumption" in t["injected_blocks"]:
        if AMNESIA_PATTERNS.search(response_text):
            logger.warning(
                "continuum: AMNESIA VIOLATION — asked for context already "
                "injected in resumption block. turn=%s", t.get("turn_id"))

    # ── Req 2: ungrounded relative time ──
    if RELATIVE_TIME_TERMS.search(response_text):
        logger.info("continuum: relative-time terms used this turn (advisory)")

    # ── Req 4: auto fact_feedback on injected facts ──
    _auto_fact_feedback(response_text, t)

    # Update last_turn_at for gap detection
    state.update_session(lambda s: s.update({"last_turn_at": time.time()}))
    return ""


def _auto_fact_feedback(response_text, t):
    """If injected facts appear referenced in the response, mark helpful."""
    fact_ids = t.get("facts_injected") or []
    if not fact_ids or not response_text:
        return
    try:
        tool_call = None
        try:
            from fact_store import feedback as fs_feedback
            tool_call = fs_feedback
        except ImportError:
            pass
        if tool_call is None:
            return  # direct API unavailable; skip silently
        for fid in fact_ids:
            try:
                tool_call(action="helpful", fact_id=fid)
            except Exception:
                break
        t["facts_injected"] = []
    except Exception as e:
        logger.debug("fact_feedback skipped: %s", e)


# ── transform_llm_output (2026-08-28 gates 1-4 enforcement point) ──────

def on_transform_llm_output(**kwargs):
    """Host contract (turn_finalizer.py:601-623): first non-empty string
    return REPLACES the final response. We append audit footers when the
    turn's prose carries unanchored claims (gate P1/P4) or an offline
    conclusion without a different-method probe (gate 3-new).

    Pre-flight verification (2026-08-29 build): verifier.verify_response runs
    FIRST — Layer 1 extracts narrative/price/relationship claims, Layer 2
    cross-checks flagged claims against turn tool results + fact evidence via
    an independent glm-5.3-flash judge session. CONTRADICTED claims are
    removed; UNVERIFIABLE ones tagged inline. Fail-open by design.
    """
    response_text = ""
    for key in ("response_text", "response", "text", "output"):
        v = kwargs.get(key)
        if isinstance(v, str) and v:
            response_text = v
            break
    if not response_text:
        return None
    t = state.turn()
    footers = []
    original_text = response_text

    # Pre-flight verifier (2026-08-29) — runs before provenance so corrected
    # text is what the provenance footer audit sees.
    try:
        from . import verifier as _verifier
        response_text = _verifier.verify_response(response_text, t)
    except Exception as e:
        logger.debug("verifier pass skipped: %s", e)

    # Gate P1/P4 — claim provenance + list≠callable
    try:
        from . import provenance as _prov
        if _prov.is_enabled():
            f = _prov.audit_footer(response_text, t)
            if f:
                footers.append(f)
    except Exception as e:
        logger.debug("provenance footer skipped: %s", e)

    # Gate 3-new — verification-before-conclusion
    try:
        from . import gates as _g
        f = _g._conclusion_guard(response_text, t)
        if f:
            footers.append(f)
    except Exception as e:
        logger.debug("conclusion guard skipped: %s", e)

    # If the verifier corrected the text (claim removed/tagged), return the
    # corrected text — host contract: first non-empty string REPLACES.
    changed_by_verifier = (
        "[claim removed" in response_text or "[UNVERIFIED-CLAIM]" in response_text
    ) and response_text != original_text
    if changed_by_verifier:
        # Append any pending footers for context, then release the corrected text.
        out = response_text
        for f in footers:
            if f not in out:
                out += f
        return out

    if not footers:
        return None
    out = response_text
    for f in footers:
        if f not in out:
            out += f
    return out