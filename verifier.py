#!/usr/bin/env python3
"""Continuum Verifier — pre-flight claim verification (design: verifier-design.md).

Layer 1: deterministic claim extraction (narrative-memory, pricing, relationship
patterns). Layer 2: on flagged turns, an independent judge session
(glm-5.3-flash via Ollama Cloud) cross-checks each claim against the turn's
actual tool results, fact_store, and session_search. Verdicts:
VERIFIED / CONTRADICTED / UNVERIFIABLE.

Contract: called from continuity.on_transform_llm_output. Returns the
(possibly corrected) response text, or the original on any internal failure
(fail-open, matching provenance.py philosophy).

The verifier NEVER authors factual content — it only removes claims or tags
them [UNVERIFIED-CLAIM] inline. Removal degrades to vaguer text; substitution
would risk hallucinated "corrections".

Judger model (user-directed Aug 29 2026): glm-5.3-flash via Ollama Cloud,
independent fresh-context session = no contamination with the main model's
conversation. Judge sees ONLY: claims + evidence blocks. Key from env
(GLM_API_KEY or ZAI_API_KEY).
"""
import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Layer 1: deterministic claim extractors ──────────────────────────────

# Narrative-memory: assertions about past interactions/what was established.
_MEMORY_CLAIM_RE = re.compile(
    r"\b(?:"
    r"we\s+(?:previously|already|earlier)?\s*(?:established|discussed|agreed|found|confirmed|determined|verified|decided|settled)"
    r"|(?:I|we)\s+(?:cited|quoted|referenced|mentioned|told you|reported|documented|archived|captured|wrote|noted)"
    r"|(?:earlier|previously|before|last time)[,\s]+"
    r"|your\s+\w+\s+(?:relationship|account|access|history|record|panel)"
    r"|the\s+\w+\s+(?:quote|statement|record|note|entry|thread|session)\s+(?:from|in|of)\s+(?:July|June|August|earlier|yesterday|last\s+\w+)|(?:July|June|August|earlier|yesterday|last\s+\w+)\s+(?:session\s+)?(?:quote|statement|record)"
    r"|as\s+(?:we|I)\s+(?:said|discussed|established|found|documented)"
    r")\b",
    re.I,
)

# Relationship/existence assertions about third parties
_RELATIONSHIP_RE = re.compile(
    r"\b(?:your|our|the)\s+(?:\w+\s+){0,2}(?:relationship|partnership|correspondence)\b"
    r"|\b(?:given|via|through|with)\s+your\s+\w+\s+(?:relationship|panel|account)\b"
    r"|\bit'?s\s+(?:also\s+)?\w+able\s+given\b",
    re.I,
)

# Price assertions: $-figure in per-unit or multi-vial context
_PRICE_RE = re.compile(
    r"\$[\d,]+(?:\.\d+)?\s*(?:/|per\s+|for\s+(?:the\s+|a\s+)?(?:\w+\s+)?(?:vials?|kits?|pieces?|grams?|g\b|mg\b|units?))",
    re.I,
)

# Vendor-name hints (cheap adjacency helper for price contexts)
_VENDOR_HINT_RE = re.compile(
    r"\b(Mia|Huirui|W1\.4|Wholesale\s*1\.4|Tang|Joan|GoTop|Go\s+Top|Conscientia|"
    r"Cellmano|Cyan|Sgreats|Marora|Amber|Juliet|Peptide\s+Crafters|Ada'?s|"
    r"Janoshik|Shanghai\s+Huirui)\b",
    re.I,
)


# User-added Aug 31 2026 (zip/90501 incident): location, distance, identity claims
_LOCATION_RE = re.compile(
    r"(?:"
    r"\d+\s*(?:miles?|mi\.?|kilometers?|km)[^.]{0,40}from"
    r"|from\s+you\b|from\s+your\s+(?:location|address|area|home|place|ZIP)"
    r"|near\s+you\b|close\s+to\s+you\b|your\s+(?:area|neighborhood|vicinity)"
    r"|(?:\byour|\bthe)\s+(?:address|ZIP|zip\s+code)"
    r"|address\s+on\s+file"
    r"|\b9[0-8]\d{3}\b"
    r"|(?:miles?|km)\s+(?:away|from)"
    r")",
    re.I,
)

_IDENTITY_RE = re.compile(
    r"(?:your|the)\s+(?:name|age|birth(?:day|date)|DOB|address|home|residence|"
    r"email|phone)[^.]{0,40}(?:is|was\s+given|appears|shows|on\s+file)",
    re.I,
)

# ── UNIVERSAL MODE (Option A, Aug 31 2026, user directive) ──────────────
# "Universal requirement that any claim needs a tool call."
# Flag EVERY sentence containing: digits, $ amounts, dates/years, measurements,
# or capitalized proper nouns (likely factual assertion). Exempt dialogue-like
# and opinion sentences to keep judge volume sane. Layer 2 judge does the
# actual verification — Layer 1 just widens the net.
_UNIVERSAL_RE = re.compile(
    r"(?:"
    r"\d"                                   # any digit anywhere
    r"|(?:19|20)\d{2}\b"                    # years
    r"|\$\s?\d"                            # dollar amounts
    r"|\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+"    # multi-word Proper Nouns
    r")",
)
_UNIVERSAL_EXEMPT_RE = re.compile(
    r"^(?:"
    r"So|But|And|Or|Now|Then|Yes|No|Okay|OK|Hmm|Well|"
    r"Good|Great|Thanks|Thank|Sounds|Right|Wrong|True|False|Agreed"
    r")\b"
)

MAX_FLAGGED_CLAIMS = 12
_MAX_JUDGE_PASSES = 2

# ── FAIL-CLOSED MODE (Aug 31 2026, user directive) ──────────────────────
# Instead of tagging [UNVERIFIED-CLAIM], unverified claims are REMOVED before
# the response ships. Only judge-VERIFIED claims and sentences with explicit
# trace markers ([tool output], [user message], [facts]) survive.
FAIL_CLOSED = True

# ── VERIFY-BEFORE-RELEASE BOUNCE (Aug 31 2026, user directive #3) ───────
# 'send an unverified claim back to the agent, force rephrasing after actual
# verification' — Charon output-gate semantics: refuse the response, deliver
# a remediation instruction, let the agent run REAL tool calls and rewrite.
# Bounce rounds capped; after cap, unverified claims are REMOVED (never pass).
_MAX_VERIFY_ROUNDS = 2


def _build_remediation_block(claims: list) -> str:
    lines = [
        "[CLAIM VERIFICATION GATE — DRAFT REFUSED]",
        "These claims have NO evidence in this turn's tool outputs, fact store,",
        "injected memory, or user statements:",
    ]
    for i, c in enumerate(claims, 1):
        lines.append(f"  {i}. {c['text'][:160]}  ({', '.join(c['reasons'])})")
    lines += [
        "",
        "REQUIRED before release:",
        "1. Run ACTUAL tool calls to verify each claim (web_search/web_extract,",
        "   fact_store, session_search, read_file/terminal as appropriate).",
        "2. Rewrite the response citing the tool inline for each kept claim.",
        "3. Drop or rewrite unverifiable claims to 'I don't have verified data'.",
        "4. Reworded fabrication is still fabrication. Same unsupported claim",
        "   resubmitted will bounce again.",
    ]
    return "\n".join(lines)


def extract_claims(response_text: str) -> list:
    """Layer 1 — structured list of flagged claims. Empty list = clean pass."""
    if not response_text:
        return []
    claims = []
    for s in re.split(r"(?<=[.!?\n])\s+", response_text):
        s = s.strip()
        if len(s) < 12:
            continue
        reasons = []
        if _MEMORY_CLAIM_RE.search(s):
            reasons.append("narrative-memory")
        if _RELATIONSHIP_RE.search(s):
            reasons.append("relationship-claim")
        if _VENDOR_HINT_RE.search(s) and re.search(r"\$\s?\d", s):
            reasons.append("vendor-price-claim")
        if _LOCATION_RE.search(s):
            reasons.append("location-claim")
        if _IDENTITY_RE.search(s):
            reasons.append("identity-claim")
        # Standalone price assertions only flag when vendor-adjacent (kills false
        # positives on settled facts like "Mia accepted $110 ... Total $798")
        if _PRICE_RE.search(s) and _VENDOR_HINT_RE.search(s):
            if "vendor-price-claim" not in reasons:
                reasons.append("price-assertion")
        # ── UNIVERSAL MODE (Option A) ──
        # Every sentence with digits, years, $, or multi-word proper nouns = claim.
        # Exempted: short dialogue fragments, already-categorized opinions, text
        # inside code fences and tables (marked lines).
        is_claim_sentence = bool(_UNIVERSAL_RE.search(s))
        in_exempt = bool(_UNIVERSAL_EXEMPT_RE.search(s))
        is_dialogue = s.startswith(("|", "```", "- ", "* ", "#", ">", "•")) or "http" in s
        has_trace_marker = ("[tool" in s or "[facts]" in s or "[qdrant]" in s
                           or "[sessions]" in s or "[user " in s or "[UNVERIFIED-CLAIM]" in s)
        if "universal-claim" not in reasons and not in_exempt and not is_dialogue and not has_trace_marker:
            if _UNIVERSAL_RE.search(s):
                reasons.append("universal-claim")
        if reasons:
            # Deduplicate sentences already captured
            if not any(c["text"] == s[:400] for c in claims):
                claims.append({"text": s[:400], "reasons": reasons})
        if len(claims) >= MAX_FLAGGED_CLAIMS:
            break
    return claims


# ── Layer 2: independent judge session ───────────────────────────────────

JUDGE_MODEL = "glm-5.3-flash"
JUDGE_BASE_URL = "https://ollama.com/v1"  # Ollama Cloud — user flat rate (Aug 29 2026 directive)

_JUDGE_SYSTEM = (
    "You are a claim-verification judge. You will receive:\n"
    "1. CLAIMS: numbered statements extracted from a draft response about to be delivered.\n"
    "2. EVIDENCE: actual tool outputs from the turn that produced the draft, plus "
    "retrieved memory records (fact store / session history).\n"
    "\n"
    "For EACH claim return a verdict:\n"
    '- VERIFIED: evidence explicitly supports the claim.\n'
    "- CONTRADICTED: evidence explicitly contradicts the claim.\n"
    "- UNVERIFIABLE: no evidence either way.\n"
    "Be strict: absence of evidence is UNVERIFIABLE, never VERIFIED.\n"
    "Do NOT invent evidence. Quote the evidence line you relied on, or say none.\n"
    'Output ONLY valid JSON: [{"id": <int>, "verdict": "VERIFIED|CONTRADICTED|UNVERIFIABLE", '
    '"evidence": "<short quote or none>", "note": "<one sentence>"}]. '
    "No prose outside the JSON."
)


def _ollama_key() -> str:
    """OLLAMA_API_KEY ONLY. Never GLM/ZAI keys — those are the metered z.ai
    account (user-corrected Aug 29 2026: spending them = real money)."""
    for src_ in (
        lambda: os.environ.get("OLLAMA_API_KEY"),
        lambda: next((l.split("=", 1)[1].strip() for l in open(os.path.expanduser("~/.hermes/.env"))
                      if l.startswith("OLLAMA_API_KEY=")), ""),
    ):
        try:
            v = src_()
            if v:
                return v
        except OSError:
            continue
    return ""


def _judge_available() -> bool:
    """Judge on Ollama Cloud (user directive Aug 29 2026: 'use the Ollama key
    with glm-5.3-flash for the judge') — flat-rate account, zero marginal cost.
    Uses OLLAMA_API_KEY exclusively; z.ai keys are hard-refused (metered)."""
    return bool(_ollama_key())


def _call_judge(claims: list, evidence_blocks: list, timeout_s: int = 45) -> list:
    """One independent judge session. Returns list of verdict dicts. Fail-open."""
    import urllib.request

    key = _ollama_key()
    if not key:
        return []

    claims_payload = [{"id": i, "claim": c["text"], "flags": c["reasons"]}
                      for i, c in enumerate(claims)]
    payload = {
        "model": JUDGE_MODEL,
        "messages": [
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": json.dumps(
                {"claims": claims_payload, "evidence": evidence_blocks},
                ensure_ascii=False)},
        ],
        "stream": False,
        "options": {"temperature": 0.1},
    }
    req = urllib.request.Request(
        JUDGE_BASE_URL.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = json.loads(resp.read())
        text = (body["choices"][0]["message"].get("content") or "").strip()
        # Model wraps in ```json fences and uses a "verdicts" key with lowercase verdicts
        m = re.search(r"\{.*\}|\[.*\]", text, re.S)
        if not m:
            return []
        raw = json.loads(m.group(0))
        verdict_list = raw.get("verdicts", []) if isinstance(raw, dict) else raw
        norm = []
        for v in verdict_list if isinstance(verdict_list, list) else []:
            if not isinstance(v, dict):
                continue
            raw_verdict = str(v.get("verdict", "")).upper()
            if raw_verdict in ("TRUE", "VERIFIED"):
                verdict = "VERIFIED"
            elif raw_verdict in ("FALSE", "CONTRADICTED"):
                verdict = "CONTRADICTED"
            else:
                verdict = "UNVERIFIABLE"
            norm.append({"id": v.get("id"), "verdict": verdict,
                         "evidence": v.get("evidence", ""), "note": v.get("explanation", "") or v.get("note", "")})
        return norm
    except Exception as e:  # noqa: BLE001 — fail-open by design
        logger.warning("continuum-verifier: judge call failed: %s", e)
        return []


# ── Evidence assembly ────────────────────────────────────────────────────

def _turn_evidence(turn_state: dict) -> list:
    """Evidence blocks from the turn's captured tool results (state.py),
    PLUS tool outputs from the previous N turns (lookback buffer).

    Sep 04 2026: expanded from current-turn-only to include the rolling
    N-turn history buffer (LOOKBACK_TURNS=5, adjustable). This eliminates
    false-positive verify-gate bounces on claims that reference data from
    earlier turns in long conversations.
    """
    blocks = []
    outputs = (turn_state or {}).get("tool_outputs_this_turn") or []
    for i, out in enumerate(outputs[:12]):
        blocks.append(f"[TOOL-{i + 1}] {str(out)[:1200]}")
    # Lookback: append historical tool outputs from previous turns
    from . import state as _state
    history = _state._tool_output_history
    for h in history:
        tid = h.get("turn_id", "?")
        for i, out in enumerate(h.get("outputs", [])[:8]):
            blocks.append(f"[HIST-{tid}-TOOL-{i + 1}] {str(out)[:800]}")
    return blocks


def _memory_evidence(turn_state: dict) -> list:
    """Best-effort fact evidence for flagged claims.

    Sources, in order:
    1. turn_state("fact_evidence_blocks") — populated by tests / pre_llm_call
       hooks that wrote directly into Continuum turn state.
    2. The Icarus handoff file (/Users/agent/.hermes/plugin-data/continuum/
       icarus-fact-handoff.json): the [facts]/[qdrant]/[sessions]/[fabric]
       blocks composed for THIS turn's prompt via Icarus pre_llm_call.
       Accepted only if fresh (age < 120s). Fail-open: missing file = no
       memory evidence, judge still sees tool outputs.
    """
    blocks = []
    injected = (turn_state or {}).get("fact_evidence_blocks") or []
    if injected:
        # turn_state already carries evidence (tests or prior capture)
        blocks.extend(injected)

    try:
        handoff = Path("/Users/agent/.hermes/plugin-data/continuum/icarus-fact-handoff.json")
        if handoff.exists():
            data = json.loads(handoff.read_text())
            # Freshness: handover must be recent (this turn or the immediately
            # previous one; guards against reading a stale file after idle gaps).
            age = time.time() - float(data.get("ts", 0))
            if age < 120:
                ctx = data.get("context") or ""
                if ctx:
                    blocks.append(f"[INJECTED-CONTEXT]\n{str(data.get('context', ''))[:9000]}")
            # Consume after read to avoid stale reuse across turns
            try:
                import os as _os
                os.remove(handoff)
            except OSError:
                pass
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.debug("verifier: handoff read failed: %s", e)

    return blocks


# ── Correction application (removal/tagging ONLY — never authoring) ─────

def _apply_verdicts(response_text: str, claims: list, verdicts: list) -> str:
    """FAIL-CLOSED: unverified claims are REMOVED, not tagged.
    A claim ships only if (a) judge returns VERIFIED, or (b) the sentence
    carries an explicit trace marker ([tool ...], [user message], [facts]).
    NOTHING unverified reaches the user."""
    out = response_text
    for i, c in enumerate(claims):
        verdict = ""
        if i < len(verdicts) and isinstance(verdicts[i], dict):
            verdict = (verdicts[i].get("verdict") or "").upper()
        # Base text without any prior tag (repeat-pass safety)
        base = c["text"].replace(" [UNVERIFIED-CLAIM]", "")
        if not base:
            continue
        target = base if base in out else c["text"]
        if not target or (base not in out and c["text"] not in out):
            continue

        # Trace-marked sentences are self-verified: skip
        if any(m in c["text"] for m in ("[tool output]", "[user message]", "[facts]", "[qdrant]", "[sessions]", "[fabric]")):
            continue

        if verdict == "VERIFIED" and not _UNIVERSAL_MODE_STRICT:
            # Non-strict mode preserves tag removal for verified claims
            out = out.replace(base + " [UNVERIFIED-CLAIM]", base)
        elif verdict == "VERIFIED":
            continue  # verified = ships as-is
        elif verdict == "CONTRADICTED":
            out = out.replace(c["text"], "[claim removed — contradicted by recorded evidence]")
        elif verdict == "UNVERIFIABLE" or not verdict:
            # FAIL-CLOSED: claim does not ship
            out = out.replace(c["text"], "[claim removed — no tool or memory evidence]")
            out = out.replace(base, "[claim removed — no tool or memory evidence]")
            # Clean up any stranded sentence separator whitespace
            out = re.sub(r"\s+([.,!?])", r"\1", out)
    return out


# ── Public entry point ───────────────────────────────────────────────────

def verify_response(response_text: str, turn_state: dict = None) -> str:
    """Verify-before-release with agent bounce loop (Charon output-gate semantics).

    Pass 1: extract + judge claims against this turn's actual tool outputs.
    Unverified claims → bounce: return a [CLAIM VERIFICATION GATE] block
    instead of the response, with explicit instructions for which claims to
    verify and which tool calls are expected. The agent then runs real tool
    calls and submits a rewritten draft (max _MAX_VERIFY_ROUNDS bounces).
    After cap: unverified claims are REMOVED (never shipped silently).
    Trace-marked sentences ([tool output]/[user message]/[facts]) pass.
    """
    rounds = 0
    current = response_text
    while rounds < _MAX_VERIFY_ROUNDS:
        has_unverified, _, unverified = _draft_verdicts(current, turn_state or {})
        if not has_unverified:
            return current  # clean — ship it
        rounds += 1
        if rounds >= _MAX_VERIFY_ROUNDS:
            # Cap reached — strip unverified claims as last resort (fail-closed)
            current = _apply_verdicts(current, unverified, [])
            return current
        # Bounce: deliver remediation block, agent will retry next turn
        import time as _t
        # Persist bounce state so the NEXT turn's rewrite is detected
        state_key = Path("/Users/agent/.hermes/plugin-data/continuum/verify_bounce_state.json")
        try:
            state_key.parent.mkdir(parents=True, exist_ok=True)
            state_key.write_text(json.dumps({
                "ts": _t.time(),
                "round": rounds,
                "bounced_claims": [c["text"][:200] for c in unverified],
            }))
        except OSError:
            pass
        return _build_remediation_block(unverified)
    return current


def _draft_verdicts(response_text: str, turn_state: dict) -> tuple:
    """One extraction+judgment pass. Returns (has_unverified, text, unverified_claims)."""
    if not response_text:
        return False, response_text, []
    t = turn_state or {}
    try:
        claims = extract_claims(response_text)
    except Exception:
        return False, response_text, []
    if not claims:
        return False, response_text, []
    pre = [c for c in claims if not any(
        m in c["text"] for m in ("[tool output]", "[user message]", "[facts]", "[qdrant]", "[sessions]", "[fabric]"))]
    if not pre:
        return False, response_text, []
    evidence = _turn_evidence(t) + _memory_evidence(t)
    verdicts = _call_judge(pre, evidence) if _judge_available() else []
    unverified = []
    for i, c in enumerate(pre):
        v = ""
        if i < len(verdicts) and isinstance(verdicts[i], dict):
            v = (verdicts[i].get("verdict") or "").upper()
        if v != "VERIFIED":
            unverified.append(c)
    return bool(unverified), response_text, unverified

def _verify_pipeline(response_text: str, turn_state: dict = None) -> str:
    """Full pipeline. Returns the (possibly corrected) response text.

    Fail-open at every stage: any internal error returns the original text.
    """
    if not response_text:
        return response_text
    t = turn_state or {}

    # Layer 1
    try:
        claims = extract_claims(response_text)
    except Exception as e:  # noqa: BLE001
        logger.warning("continuum-verifier: extraction failed: %s", e)
        return response_text

    if not claims:
        return response_text  # clean pass — zero cost

    # Layer 2 — FAIL-CLOSED: unverifiable claims are REMOVED, not tagged.
    evidence = _turn_evidence(t) + _memory_evidence(t)

    final = response_text
    for _pass in range(_MAX_JUDGE_PASSES):
        if not _judge_available():
            final = _apply_verdicts(final, claims, [])
            break
        verdicts = _call_judge(claims, evidence)
        if not verdicts:
            break  # judge failed — fail open
        new_text = _apply_verdicts(final, claims, verdicts)
        if new_text == final:
            break
        final = new_text
        claims = extract_claims(final)
        if not claims:
            break

    return final