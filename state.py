"""Continuum state: TurnState (per-turn, in-memory) + SessionState (persistent).

SessionState persists via the host PluginState facade (ctx.state), giving
atomic locked JSON at ~/.hermes/plugin-data/continuum/state.json.
"""

import json
import logging
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_ctx = None  # PluginContext, set by init()

# ── TurnState ──────────────────────────────────────────────────────────

_turn = {
    "turn_id": None,
    "started_at": None,
    "skills_loaded": set(),       # skill_view targets this turn
    "verification_called": False, # any verification tool this turn
    "action_failures": [],        # infra/command failures this turn
    "locked": False,              # Circe-style lockout flag (infra only)
    "injected_blocks": [],        # what we injected into this turn's prompt
    "facts_injected": [],         # fact ids injected (for auto fact_feedback)
    "user_message": "",
}


def new_turn(turn_id=None):
    _turn["turn_id"] = turn_id
    _turn["started_at"] = time.time()
    _turn["skills_loaded"] = set()
    _turn["verification_called"] = False
    _turn["action_failures"] = []
    _turn["locked"] = False
    _turn["injected_blocks"] = []
    _turn["facts_injected"] = []
    _turn["user_message"] = ""


def turn():
    return _turn


# ── Tool classification ───────────────────────────────────────────────

VERIFICATION_TOOLS = frozenset({
    "read_file", "search_files", "skill_view", "skills_list",
    "web_search", "web_extract", "session_search", "clarify",
    "vision_analyze", "video_analyze",
})

ACTION_TOOLS = frozenset({
    "terminal", "process", "write_file",
    "browser_click", "browser_type", "browser_navigate", "browser_press",
    "browser_scroll", "browser_back", "browser_exec",
    "image_generate", "text_to_speech",
    "cronjob", "delegate_task",
})


# ── SessionState (persistent) ─────────────────────────────────────────

DEFAULT_SESSION = {
    "active_task": None,
    "task_started_at": None,
    "task_keywords": [],
    "required_skills": [],
    "skills_missing": [],
    "last_turn_at": None,
    "session_started_at": None,
    "connections": {},        # host -> {user, auth_method, cred_source, last_used}
    "ssh_auth_failed": {},    # host -> count (per-session)
    "no_skill_ack": False,    # explicit proceed-without-skill acknowledgment
    "gap_resumed": False,
}

GAP_THRESHOLD_SECONDS = 30 * 60  # 30 minutes


def on_session_start(**kwargs):
    """Hook: session start. Record timestamp and detect resumption gap."""
    if _ctx is None:
        init(kwargs.get("ctx"))
    st = _read_session()
    now = time.time()
    st["last_session_at"] = st.get("session_started_at")
    st["session_started_at"] = now
    _write_session(st)


def init(ctx):
    global _ctx
    _ctx = ctx
    st = _read_session()
    if st.get("session_started_at") is None:
        st["session_started_at"] = time.time()
        _write_session(st)


def _read_session():
    if _ctx is None:
        return dict(DEFAULT_SESSION)
    try:
        data = _ctx.state.get("session", {})
    except Exception as e:
        logger.warning("continuum session read failed: %s", e)
        data = {}
    merged = dict(DEFAULT_SESSION)
    if isinstance(data, dict):
        merged.update(data)
    return merged


def _write_session(st):
    if _ctx is None:
        return
    try:
        _ctx.state.set("session", st)
    except Exception as e:
        logger.warning("continuum session write failed: %s", e)


def session():
    return _read_session()


def update_session(fn):
    """Atomically read-modify-write SessionState. fn(st) mutates in place."""
    st = _read_session()
    fn(st)
    _write_session(st)
    return st


# ── Task management (Req 3) ───────────────────────────────────────────

def set_task(name):
    def _set(st):
        st["active_task"] = name
        st["task_started_at"] = time.time()
        st["no_skill_ack"] = False
    update_session(_set)


def clear_task():
    def _clear(st):
        st["active_task"] = None
        st["task_started_at"] = None
        st["required_skills"] = []
        st["skills_missing"] = []
        st["no_skill_ack"] = False
    update_session(_clear)


# Skill map: task keywords -> required skills.
# Lives in continuum-data/ so it's user-editable without touching code.
_SKILL_MAP_FILE = Path(__file__).resolve().parent.parent / "continuum-data" / "skill_map.json"

DEFAULT_SKILL_MAP = {
    "proxmox": ["proxmox-ve"],
    "esxi": ["proxmox-ve", "esxi-proxmox-vm-migration"],
    "cloudpanel": ["cloudpanel"],
    "wordpress": ["wordpress-router", "wp-wpcli-and-ops"],
    "grommunio": ["grommunio"],
    "mail migration": ["imap-migration"],
    "ssl certificate": ["ssl-certificate-management", "nginx-ssl"],
    "dns": ["self-hosted-dns"],
    "package tracking": ["shipment-tracker", "package-tracking"],
    "fedex": ["shipment-tracker", "package-tracking"],
    "ups tracking": ["shipment-tracker"],
    "usps": ["shipment-tracker"],
    "17track": ["shipment-tracker"],
    "peptide": ["peptide-research", "protocol-state-management"],
    "dosing": ["protocol-state-management"],
    "food logging": ["food-logging", "fatsecret-api-response-patterns"],
    "weight": ["withings-health"],
    "github": ["github-pr-workflow", "github-auth"],
    "email": ["himalaya"],
    "apple notes": ["apple-notes"],
    "calendar": ["apple-calendar"],
    "floorplan": ["floorplan-creation", "sweet-home-3d"],
    "baja ventures": ["baja-ventures-site"],
    "kumberland": ["kumberland-site-consolidation"],
}


def load_skill_map():
    try:
        if _SKILL_MAP_FILE.exists():
            return json.loads(_SKILL_MAP_FILE.read_text())
    except Exception:
        pass
    return DEFAULT_SKILL_MAP


def save_skill_map(m):
    _SKILL_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SKILL_MAP_FILE.write_text(json.dumps(m, indent=2))


def match_task_to_skills(task_text):
    """Match task text against the keyword->skills map. Deterministic."""
    m = load_skill_map()
    text_lower = str(task_text or "").lower()
    matched = set()
    for keywords_fragment, skills in m.items():
        for kw in ([keywords_fragment] if isinstance(keywords_fragment, str) else keywords_fragment):
            if kw.lower() in text_lower:
                matched.update(skills)
                break
    return sorted(matched)


def resolve_skills_for_task(name):
    """Resolve and record required skills for a task; report found/missing."""
    required = match_task_to_skills(name)
    available = _available_skills()
    found, missing = [], []
    for s in required:
        (found if s in available else missing).append(s)

    def _rec(st):
        st["required_skills"] = required
        st["skills_missing"] = missing
    update_session(_rec)
    return {"found": found, "missing": missing}


def _available_skills():
    """Read installed skill names from ~/.hermes/skills (dirs with SKILL.md)."""
    import os
    base = Path.home() / ".hermes" / "skills"
    names = set()
    if base.exists():
        for root, dirs, files in os.walk(base):
            if "SKILL.md" in files:
                rel = Path(root).relative_to(base)
                names.add(rel.parts[-1])
            dirs[:] = [d for d in dirs if not d.startswith(".")]
    return names


# ── Status report ──────────────────────────────────────────────────────

def status_report():
    st = session()
    t = _turn
    from . import gates as _g
    lines = [
        "Continuum status:",
        f"  Active task: {st.get('active_task') or 'none'}"
        + (f" (since {time.strftime('%Y-%m-%d %H:%M', time.localtime(st['task_started_at']))})" if st.get("task_started_at") else ""),
        f"  Required skills: {', '.join(st.get('required_skills', [])) or 'none'}",
        f"  Missing skills: {', '.join(st.get('skills_missing', [])) or 'none'}",
        f"  Connections tracked: {len(st.get('connections', {}))}",
        f"  Gates: {'ON' if _g.is_enabled() else 'OFF'}",
        "  This turn:",
        f"    Skills loaded: {', '.join(sorted(t['skills_loaded'])) or 'none'}",
        f"    Verification called: {t['verification_called']}",
        f"    Failures: {len(t['action_failures'])}",
        f"    Locked: {t['locked']}",
        f"    Injected blocks: {', '.join(t['injected_blocks']) or 'none'}",
    ]
    return "\n".join(lines)
