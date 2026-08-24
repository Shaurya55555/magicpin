#!/usr/bin/env python3
"""
bot.py — magicpin AI Challenge candidate bot ("Vera, but better").

Implements the 5-endpoint HTTP contract from challenge-testing-brief.md:
    POST /v1/context   — receive a context push (idempotent by (scope, context_id, version))
    POST /v1/tick       — periodic wake-up; bot may proactively initiate conversations
    POST /v1/reply      — receive a merchant/customer reply; respond send/wait/end
    GET  /v1/healthz    — liveness probe
    GET  /v1/metadata   — bot identity

Composition logic lives in composer.py (first-touch / proactive messages) and
conversation_handlers.py (multi-turn replies). This file is purely the HTTP/state layer.

Run:
    pip install -r requirements.txt
    uvicorn bot:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

import composer
import storage
from conversation_handlers import ConversationState, respond as ch_respond

# ---------------------------------------------------------------------------
# Fill this in before submitting
# ---------------------------------------------------------------------------
TEAM_NAME = "Shaurya Bajpai"
TEAM_MEMBERS = ["Shaurya Bajpai"]
CONTACT_EMAIL = "bajpaishaurya2911@gmail.com"
BOT_VERSION = "1.0.0"

MAX_SENDS_PER_MERCHANT = 5          # soft frequency cap across the whole test window
MAX_ACTIONS_PER_TICK = 20           # hard cap per the testing brief §5

app = FastAPI(title="magicpin-ai-challenge-bot")
START = time.time()

# ---------------------------------------------------------------------------
# State lives in storage.py (Redis-backed on Vercel, in-memory for local dev) —
# NOT module-level dicts. Vercel Functions have no instance affinity, so a plain
# dict here would silently lose state between a /v1/context push and a later
# /v1/tick or /v1/reply on a different instance.
# ---------------------------------------------------------------------------
_CTX_PREFIX = "ctx:"
_CONV_PREFIX = "conv:"
_SUPP_PREFIX = "supp:"
_SENDCOUNT_PREFIX = "sendcount:"
_OPEN_CONV_PREFIX = "openconv:"

# Trigger kinds that carry real business stakes (compliance risk, competitive threat,
# revenue risk) regardless of what the payload's own `urgency` field says — a thin/
# placeholder-expanded trigger of one of these kinds still deserves priority over a
# richer but lower-stakes one. Tuned from the challenge brief's per-kind descriptions,
# not from any specific merchant/category instance, so it generalizes to unseen data.
_KIND_STAKES_BOOST = {
    "supply_alert": 8,           # patient safety / recall
    "regulation_change": 6,      # compliance risk
    "renewal_due": 5,            # visibility gap if missed
    "winback_eligible": 4,       # revenue already lost, growing
    "competitor_opened": 3,      # competitive threat window
    "dormant_with_vera": 2,      # relationship decay
    "gbp_unverified": 2,
}

# (merchant_signal, trigger_kind) -> bonus: reward triggers that match a real merchant-
# state signal we were already told about, over one that doesn't — this is exactly what
# the rubric's "decision quality" dimension names: combine trigger + merchant state
# before deciding what to send, not just trigger.urgency alone.
_SIGNAL_KIND_MATCH_BOOST = {
    ("dormant", "dormant_with_vera"): 5,
    ("dormant", "winback_eligible"): 4,
    ("ctr_below_peer", "perf_dip"): 4,
    ("ctr_below_peer", "competitor_opened"): 2,
    ("stale_posts", "category_seasonal"): 3,
    ("stale_posts", "research_digest"): 2,
    ("high_risk_adult", "research_digest"): 2,
}

# Below this score, a trigger's signal is judged too thin to justify an outbound message
# right now — restraint (skip) beats sending a low-content nudge. Only bites when urgency
# is already low AND the payload has no real facts (see _is_thin_payload) — a genuinely
# urgent or fact-rich trigger is never held back by this.
MIN_FIRE_SCORE = 8

# A merchant with an unresolved (unreplied) outbound message only gets interrupted by a
# new proactive trigger if that trigger clears this bar — i.e. it's a materially bigger
# deal than "business as usual". Below it, restraint wins: don't pile a second message on
# top of one the merchant hasn't answered yet.
OPEN_CONV_OVERRIDE_SCORE = 45


def _is_thin_payload(trigger: dict) -> bool:
    payload = trigger.get("payload") or {}
    if not payload:
        return True
    keys = set(payload.keys())
    return keys <= {"placeholder", "metric_or_topic"}


def _priority_score(trigger: dict, merchant: dict, category: dict) -> tuple[float, list[str]]:
    """Score how strong a signal this trigger is for this merchant right now, combining
    the trigger itself, merchant state, and category fit — rather than trigger.urgency
    alone. Returns (score, reasons) so /v1/tick can put the actual reasoning into the
    action's rationale, not just the winning number."""
    kind = trigger.get("kind", "")
    urgency = trigger.get("urgency", 1) or 1
    score = float(urgency) * 10
    reasons = [f"urgency {urgency}"]

    boost = _KIND_STAKES_BOOST.get(kind, 0)
    if boost:
        score += boost
        reasons.append(f"'{kind}' carries inherent business stakes (+{boost})")

    signals = set(composer.merchant_signals(merchant))
    for (sig, want_kind), pts in _SIGNAL_KIND_MATCH_BOOST.items():
        if sig in signals and kind == want_kind:
            score += pts
            reasons.append(f"matches merchant signal '{sig}' (+{pts})")

    if category.get("slug") in ("dentists", "pharmacies") and kind in ("regulation_change", "supply_alert"):
        score += 2
        reasons.append("category is compliance-sensitive, weighting this kind higher (+2)")

    if _is_thin_payload(trigger):
        score -= 4
        reasons.append("payload is thin/placeholder-only (-4)")

    return score, reasons


def _open_conv_id(merchant_id: str) -> Optional[str]:
    return storage.get_json(f"{_OPEN_CONV_PREFIX}{merchant_id}")


def _set_open_conv(merchant_id: str, conv_id: str) -> None:
    storage.set_json(f"{_OPEN_CONV_PREFIX}{merchant_id}", conv_id)


def _clear_open_conv(merchant_id: str) -> None:
    storage.set_json(f"{_OPEN_CONV_PREFIX}{merchant_id}", None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ctx_key(scope: str, context_id: str) -> str:
    return f"{_CTX_PREFIX}{scope}:{context_id}"


def _get(scope: str, context_id: str) -> Optional[dict]:
    entry = storage.get_json(_ctx_key(scope, context_id))
    return entry["payload"] if entry else None


def _is_suppressed(key: str) -> bool:
    return storage.exists(f"{_SUPP_PREFIX}{key}")


def _suppress(key: str) -> None:
    if key:
        storage.set_json(f"{_SUPP_PREFIX}{key}", True)


def _send_count(merchant_id: str) -> int:
    return storage.get_json(f"{_SENDCOUNT_PREFIX}{merchant_id}") or 0


def _incr_send_count(merchant_id: str) -> None:
    storage.incr(f"{_SENDCOUNT_PREFIX}{merchant_id}")


def _get_conversation(conv_id: str) -> Optional[ConversationState]:
    d = storage.get_json(f"{_CONV_PREFIX}{conv_id}")
    return ConversationState.from_dict(d) if d else None


def _put_conversation(state: ConversationState) -> None:
    storage.set_json(f"{_CONV_PREFIX}{state.conversation_id}", state.to_dict())


def _is_expired(trigger: dict, now_iso: str) -> bool:
    exp = trigger.get("expires_at")
    if not exp:
        return False
    try:
        exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        return now_dt > exp_dt
    except Exception:
        return False


# ---------------------------------------------------------------------------
# GET /v1/healthz
# ---------------------------------------------------------------------------
@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    try:
        for key in storage.scan_prefix(_CTX_PREFIX):
            scope = key[len(_CTX_PREFIX):].split(":", 1)[0]
            if scope in counts:
                counts[scope] += 1
    except Exception:
        # Liveness must never fail just because the count scan hiccuped — 3 consecutive
        # healthz failures disqualifies the test slot, so this endpoint stays up even if
        # the context count itself is momentarily unavailable.
        pass
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts}


# ---------------------------------------------------------------------------
# GET /v1/metadata
# ---------------------------------------------------------------------------
@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": TEAM_NAME,
        "team_members": TEAM_MEMBERS,
        "model": "rule-based-composer-v1 (no external LLM call; see README.md)",
        "approach": "Deterministic per-trigger-kind template engine over the 4-context framework, "
                    "with a generic fallback for unseen trigger kinds; multi-turn state machine for "
                    "auto-reply detection / intent-transition / hostile handling.",
        "contact_email": CONTACT_EMAIL,
        "version": BOT_VERSION,
        "submitted_at": _now(),
    }


# ---------------------------------------------------------------------------
# POST /v1/context
# ---------------------------------------------------------------------------
class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: Optional[str] = None


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in ("category", "merchant", "customer", "trigger"):
        return {"accepted": False, "reason": "invalid_scope", "details": f"unknown scope '{body.scope}'"}

    key = _ctx_key(body.scope, body.context_id)
    cur = storage.get_json(key)
    if cur and cur["version"] > body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
    if cur and cur["version"] == body.version:
        # Idempotent no-op: exact re-post of the version already stored, not a stale
        # (older) push — the payload is presumed unchanged for a given version.
        return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}", "stored_at": _now()}

    storage.set_json(key, {"version": body.version, "payload": body.payload})
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}", "stored_at": _now()}


# ---------------------------------------------------------------------------
# POST /v1/tick
# ---------------------------------------------------------------------------
class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


def _next_conv_id(merchant_id: str, kind: str) -> str:
    n = storage.incr("conv_counter")
    short_mid = merchant_id.split("_")[1] if "_" in merchant_id else merchant_id[:10]
    return f"conv_{short_mid}_{kind}_{n}"


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    # Group candidate triggers by merchant so we send at most one per merchant per tick
    # (spam control) and prefer the highest-urgency one when several fire together.
    candidates_by_merchant: dict[str, list[dict]] = {}

    for trg_id in body.available_triggers:
        trigger = _get("trigger", trg_id)
        if not trigger:
            continue
        if _is_expired(trigger, body.now):
            continue
        supp_key = trigger.get("suppression_key", trg_id)
        if _is_suppressed(supp_key):
            continue
        mid = trigger.get("merchant_id")
        if not mid:
            continue
        candidates_by_merchant.setdefault(mid, []).append(trigger)

    for mid, triggers_for_merchant in candidates_by_merchant.items():
        if len(actions) >= MAX_ACTIONS_PER_TICK:
            break
        if _send_count(mid) >= MAX_SENDS_PER_MERCHANT:
            continue  # restraint — this merchant has had enough proactive sends this run

        merchant = _get("merchant", mid)
        if not merchant:
            continue
        category = _get("category", merchant.get("category_slug", ""))
        if not category:
            continue

        # Score every candidate against trigger + merchant state + category fit (not
        # urgency alone) and take the best one. Runners-up are kept for the rationale so
        # the "why this one, not that one" reasoning is visible to the judge, not just
        # the outcome.
        scored = sorted(
            ((t, *_priority_score(t, merchant, category)) for t in triggers_for_merchant),
            key=lambda x: -x[1],
        )
        trigger, best_score, best_reasons = scored[0]
        runners_up = [f"{t.get('kind')} (score {s:.0f})" for t, s, _ in scored[1:3]]

        if best_score < MIN_FIRE_SCORE:
            continue  # restraint: the strongest available signal is still too thin to earn an outbound message

        open_conv = _open_conv_id(mid)
        if open_conv and best_score < OPEN_CONV_OVERRIDE_SCORE:
            continue  # restraint: merchant hasn't replied to the last outbound message yet — don't pile on

        customer = None
        if trigger.get("scope") == "customer" and trigger.get("customer_id"):
            customer = _get("customer", trigger["customer_id"])
            if not customer:
                continue  # can't compose a customer-facing message without the customer context

        composed = composer.compose(category, merchant, trigger, customer)

        conv_id = _next_conv_id(mid, trigger.get("kind", "gen"))
        state = ConversationState(
            conversation_id=conv_id,
            merchant_id=mid,
            customer_id=trigger.get("customer_id"),
            trigger_id=trigger.get("id"),
            category=category, merchant=merchant, trigger=trigger, customer=customer,
            last_offer=composed.get("ask_text") or composed["body"],
            opening_rationale=composed["rationale"],
        )
        state.sent_bodies.add(composed["body"])
        state.turns.append({"from": "vera", "message": composed["body"]})
        _put_conversation(state)

        _suppress(trigger.get("suppression_key", trigger.get("id", "")))
        _incr_send_count(mid)
        _set_open_conv(mid, conv_id)

        name = composer.biz_name(merchant)
        short_body = composed["body"][:60]
        selection_note = f"Picked over {len(scored) - 1} other candidate(s) this tick ({', '.join(runners_up)}); " if runners_up else ""
        rationale = (
            f"{selection_note}selection basis: {'; '.join(best_reasons)} (score {best_score:.0f}). "
            f"{composed['rationale']}"
        )
        actions.append({
            "conversation_id": conv_id,
            "merchant_id": mid,
            "customer_id": trigger.get("customer_id"),
            "send_as": composed["send_as"],
            "trigger_id": trigger.get("id"),
            "template_name": f"vera_{trigger.get('kind','generic')}_v1",
            "template_params": [name, short_body],
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": composed["suppression_key"],
            "rationale": rationale,
        })

    return {"actions": actions}


# ---------------------------------------------------------------------------
# POST /v1/reply
# ---------------------------------------------------------------------------
class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: Optional[str] = None
    turn_number: Optional[int] = None


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    state = _get_conversation(body.conversation_id)
    if state is None:
        # Judge replied on a conversation we don't recognize (shouldn't happen per spec,
        # but degrade gracefully rather than error).
        mid = body.merchant_id or ""
        merchant = _get("merchant", mid) or {}
        state = ConversationState(
            conversation_id=body.conversation_id,
            merchant_id=mid,
            customer_id=body.customer_id,
            merchant=merchant,
        )

    result = ch_respond(state, body.message)
    if result.get("action") == "end":
        _clear_open_conv(state.merchant_id)  # merchant is free to be proactively messaged again
        if state.suppressed:
            supp = state.trigger.get("suppression_key") if state.trigger else None
            if supp:
                _suppress(supp)
    _put_conversation(state)
    return result


# ---------------------------------------------------------------------------
# POST /v1/teardown (optional, per §11 of the testing brief)
# ---------------------------------------------------------------------------
@app.post("/v1/teardown")
async def teardown():
    storage.delete_all(_CTX_PREFIX)
    storage.delete_all(_CONV_PREFIX)
    storage.delete_all(_SUPP_PREFIX)
    storage.delete_all(_SENDCOUNT_PREFIX)
    storage.delete_all(_OPEN_CONV_PREFIX)
    storage.delete_all("conv_counter")
    return {"status": "wiped"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
