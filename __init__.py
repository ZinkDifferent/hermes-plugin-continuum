"""Continuum — session continuity engine + enforcement harness.

Consolidates the enforcement gates (Charon/Circe patterns) into one
turn-state machine, and adds the continuity layer Hermes lacks:
gap detection with task resumption, mandatory skill matching per task,
fact_store integration with auto fact_feedback, stale-knowledge
contradiction capture, and a persistent connection registry.

Subsystems:
  A. Enforcement gates   (pre_tool_call chokepoint)
  B. Continuity engine   (pre/post LLM hooks)

State:
  TurnState    — resets every turn, in-memory
  SessionState — persists via ctx.state (PluginState JSON store)

Hooks:
  on_session_start  — load SessionState, detect resumption from prior session
  pre_llm_call      — gap detection, resumption injection, skill resolution,
                      fact/entity injection, ENFORCEABLE digest re-injection
  pre_tool_call     — gates: verify-before-act, one-failure lockout,
                      timeout arithmetic, SSH/connection guard, no-skill block
  post_tool_call    — turn-state updates (verification seen, failures counted,
                      connections learned)
  post_llm_call     — claim provenance flags, amnesia-pattern detection,
                      auto fact_feedback
"""

import json
import logging

from . import state
from . import continuity
from . import gates
from . import connections

logger = logging.getLogger(__name__)


def register(ctx):
    ctx.register_hook("on_session_start", _safe(state.on_session_start))
    ctx.register_hook("pre_llm_call", _safe(continuity.on_pre_llm_call))
    ctx.register_hook("pre_tool_call", _safe_none(gates.on_pre_tool_call))
    ctx.register_hook("post_tool_call", _safe_none(gates.on_post_tool_call))
    ctx.register_hook("post_llm_call", _safe(continuity.on_post_llm_call))

    ctx.register_command(
        name="continuum",
        description="Continuity engine: session/task state, connections, gates.",
        handler=_cmd_continuum,
        args_hint="[status|task <name>|task clear|conn|gates on|gates off]",
    )

    # Initialize persistent stores lazily via ctx.state facade.
    state.init(ctx)
    logger.info("continuum registered (5 hooks, 1 command)")


def _safe(fn):
    def wrapper(**kwargs):
        try:
            return fn(**kwargs)
        except Exception as e:
            logger.warning("continuum hook error in %s: %s", fn.__name__, e)
            return ""
    return wrapper


def _safe_none(fn):
    def wrapper(**kwargs):
        try:
            return fn(**kwargs)
        except Exception as e:
            logger.warning("continuum hook error in %s: %s", fn.__name__, e)
            return None
    return wrapper


def _cmd_continuum(args: str, ctx=None, **kwargs) -> str:
    args = (args or "").strip()
    parts = args.split(None, 1) if args else ["status"]
    sub = parts[0].lower()

    if sub == "status":
        return state.status_report()
    if sub == "task" and len(parts) > 1:
        name = parts[1].strip()
        if name.lower() in ("clear", "none", "off"):
            state.clear_task()
            return "Task cleared."
        state.set_task(name)
        matched = state.resolve_skills_for_task(name)
        lines = [f"Task set: {name}"]
        if matched["found"]:
            lines.append(f"Skills loaded: {', '.join(matched['found'])}")
        if matched["missing"]:
            lines.append(f"NO SKILL FOUND for: {', '.join(matched['missing'])}")
        return "\n".join(lines)
    if sub == "conn":
        return connections.report()
    if sub == "gates":
        enabled = len(parts) > 1 and parts[1].lower() == "on"
        gates.set_enabled(enabled)
        return f"Gates {'enabled' if enabled else 'disabled'}."
    return (
        "Usage: /continuum [status|task <name>|task clear|conn|gates on|gates off]"
    )
