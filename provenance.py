"""Provenance gates: prose-level enforcement via transform_llm_output.

Gate P1 — claim-provenance scan (2026-08-28 corrective build, approved 1-4).
Gate P4 — model-list≠callable rule (specific provenance rule).

Host contract (verified agent/turn_finalizer.py:601-623):
  transform_llm_output fires ONCE per turn AFTER the tool-calling loop
  completes, with response_text=<final assistant text>. First hook to
  return a NON-EMPTY STRING WINS and REPLACES the response. None/empty
  return leaves text unchanged.

These gates CANNOT block a response (the response is already generated);
they APPEND a provenance audit footer to the delivered text when the
turn's assertions lack tool-verified anchors. Blocking would delete the
user's answer entirely — appending is the fail-safe design.

What counts as an ANCHOR (set by gates.on_post_tool_call / continuity):
  turn["claims"] — list of dicts {claim, source, needs_anchor}
  turn["verified_tools"] — set of tool names that returned results this turn

Claim shapes that REQUIRE anchors (heuristic, conservative):
  - availability/callability assertions ("X is available/callable/works")
    when the turn's tool calls only LISTED X (list ≠ invoke)
  - state assertions ("X is running/offline/down/up") when zero probes of
    X succeeded this turn
"""

import re
import logging

logger = logging.getLogger(__name__)

_enabled = True

# Assertions about state/availability that demand a same-turn anchor.
_AVAILABILITY_RE = re.compile(
    r"\b(is|are|was|were)\s+(?:now\s+)?"
    r"(callable|available|accessible|working|live|up|running|offline|down|"
    r"reachable|unreachable|verified|confirmed)\b",
    re.I,
)

# List-only tool signatures: a models list or directory listing is not a
# call. Commands that LIST without INVOKING cannot anchor callability.
_LIST_ONLY_RE = re.compile(
    r"(/v1/models|ls\s+\"?/?[a-z/]*models|list\s+models|hermes\s+model|"
    r"show\s+models|--list|list\s+--?keys?)\b",
    re.I,
)


def is_enabled():
    return _enabled


def set_enabled(v: bool):
    global _enabled
    _enabled = bool(v)


def availability_claims(text):
    """Return list of availability-assertion snippets found in prose."""
    if not text:
        return []
    return [m.group(0) for m in _AVAILABILITY_RE.finditer(text)]


def _turn_had_list_only_evidence(turn_state):
    """True when the turn's terminal commands were list-only (no real call)."""
    cmds = (turn_state or {}).get("commands_this_turn") or []
    if not cmds:
        return False
    terminal_cmds = [c for c in cmds if isinstance(c, str)]
    if not terminal_cmds:
        return False
    # list-only = every command matched list shape AND none invoked anything
    listy = [c for c in terminal_cmds if _LIST_ONLY_RE.search(c)]
    invoked = [c for c in terminal_cmds if not _LIST_ONLY_RE.search(c)]
    return len(listy) > 0 and len(invoked) == 0


def audit_footer(response_text, turn_state):
    """Return the provenance audit footer string, or '' if clean.

    Called by continuity.on_transform_llm_output with the turn state dict.
    """
    if not _enabled or not response_text:
        return ""
    from . import state as _state

    t = turn_state if turn_state is not None else _state.turn()
    claims = availability_claims(response_text)

    if not claims:
        return ""

    verified = t.get("verification_called") or False
    tools_with_results = (t.get("verified_tools") or set()) if isinstance(
        t.get("verified_tools"), (set, list)) else set()
    n_verified = len(tools_with_results) if tools_with_results else (
        1 if verified else 0)

    if n_verified >= 1 and not _turn_had_list_only_evidence(t):
        # Turn had real (non-list) verification; claims are anchored.
        return ""

    # Unanchored availability claims detected
    examples = "; ".join(sorted(set(c.strip() for c in claims))[:3])
    footer = (
        "\n\n---\n[PROVENANCE AUDIT] This response asserts availability/state "
        "(e.g. \"{examples}\") without a same-turn tool-verified anchor. The "
        "user treats unverified claims as fabrication risk. Verify with a "
        "REAL invocation (not a list) or qualify as unverified."
    ).format(examples=examples)
    logger.info("continuum: provenance footer appended (%d claims)", len(claims))
    return footer


def on_transform_llm_output(**kwargs):
    """Hook: append provenance audit footer when claims lack anchors."""
    if not _enabled:
        return None
    response_text = ""
    for key in ("response_text", "response", "text", "output"):
        v = kwargs.get(key)
        if isinstance(v, str) and v:
            response_text = v
            break
    if not response_text:
        return None
    from . import state as _state
    footer = audit_footer(response_text, _state.turn())
    if footer and footer not in response_text:
        return response_text + footer
    return None