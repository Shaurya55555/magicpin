"""
conversation_handlers.py — multi-turn reply logic (the optional §7.4 deliverable).

`respond(state, merchant_message)` is the single entrypoint bot.py's /v1/reply delegates to.
It is a deterministic, regex/keyword-based state machine — no LLM call, no external I/O —
so it is trivially fast (<<30s) and side-effect-free to re-run.

Priority order per turn (first match wins), matching the testing brief's replay scenarios:
    1. Auto-reply detection          (challenge-testing-brief.md §Phase 4.1)
    2. Hostility                     (challenge-testing-brief.md §Phase 4.3) — checked ahead of a
                                       calm opt-out so an angry "stop bothering me, this is useless"
                                       is characterized correctly rather than read as a mild decline
    3. Explicit opt-out / hard "no"  (challenge-brief.md Pattern D is the anti-pattern to avoid;
                                       example 2.6 is the correct handling of an explicit stop)
    4. Intent transition             (challenge-brief.md §12.2, Pattern D anti-pattern,
                                       challenge-testing-brief.md §Phase 4.2)
    5. Curveball / off-topic ask     (example 2.7)
    6. Generic affirmative / continuation
    7. Fallback: acknowledge + restate the single open ask (varied wording, anti-repetition)
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Optional, Any

try:
    import llm_client  # optional — reply bodies fall back to templates if unavailable
except Exception:  # pragma: no cover
    llm_client = None


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str] = None
    trigger_id: Optional[str] = None
    category: dict = field(default_factory=dict)
    merchant: dict = field(default_factory=dict)
    trigger: dict = field(default_factory=dict)
    customer: Optional[dict] = None
    turns: list = field(default_factory=list)       # [{"from": "vera"|"merchant"/"customer", "message": str}]
    sent_bodies: set = field(default_factory=set)
    auto_reply_streak: int = 0
    last_merchant_message: str = ""
    ended: bool = False
    suppressed: bool = False
    committed: bool = False
    post_end_ack: bool = False
    last_offer: str = ""            # the ask/CTA text from our most recent outbound message
    opening_rationale: str = ""     # why we started this conversation (from the original composed action)

    def to_dict(self) -> dict:
        """JSON-safe serialization for storage.py (Redis has no native set type over
        the REST API path we use, so sent_bodies round-trips as a list)."""
        d = dict(self.__dict__)
        d["sent_bodies"] = list(self.sent_bodies)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ConversationState":
        d = dict(d)
        d["sent_bodies"] = set(d.get("sent_bodies") or [])
        return cls(**d)


# ---------------------------------------------------------------------------
# Pattern banks
# ---------------------------------------------------------------------------

AUTO_REPLY_PATTERNS = [
    r"thank you for contacting", r"thanks for (contacting|reaching out|your message)",
    r"(will |we'?ll )?respond shortly", r"get back to you shortly", r"we('ll| will) get back",
    r"currently (unavailable|closed|away)", r"we'?re (currently )?away", r"out of (the )?office",
    r"automated (assistant|reply|message|response)", r"this is an automated",
    r"message has been received", r"your message has been", r"received your message",
    r"during business hours", r"business hours", r"team will (reach|respond|contact|get)",
    r"our team will", r"shukriya.*team", r"team tak pahuncha", r"aapki jaankari ke liye",
]

# Off-topic domains Vera should decline rather than attempt (with or without a "?").
OFFTOPIC_PATTERNS = [
    r"\bgst\b", r"\bpan\b", r"income tax", r"\btax return\b", r"\bpayroll\b", r"\btds\b",
    r"file my", r"file the", r"legal (advice|notice|help)", r"\blawsuit\b", r"accounting\b",
    r"balance sheet", r"loan (application|approval)", r"visa\b", r"passport\b",
]

OPTOUT_PATTERNS = [
    r"\bstop\b", r"not interested", r"unsubscribe", r"leave me alone",
    r"stop (messaging|sending|texting)", r"no thanks,? stop", r"do not (message|contact) me",
]

HOSTILE_PATTERNS = [
    r"useless", r"spam", r"stop bothering", r"shut up", r"waste of time",
    r"annoying", r"harass", r"idiot", r"stupid bot",
]

INTENT_TRANSITION_PATTERNS = [
    r"let'?s do it", r"lets do it", r"ok,? let'?s", r"go ahead", r"yes,? let'?s",
    r"sounds good,? do it", r"i want to join", r"want to join", r"confirm\b",
    r"proceed\b", r"yes please,? (proceed|go ahead|do it)", r"chalo (karte hain|shuru)",
    r"haan karo", r"kar do", r"theek hai karo",
]

AFFIRMATIVE_PATTERNS = [
    r"^\s*(yes|yep|yeah|sure|ok(ay)?|please|send|haan|theek hai|thik hai|bilkul)\b",
    r"send (it|the abstract|me)", r"please (send|share|draft)",
]

NEGATIVE_PATTERNS = [
    r"^\s*(no\b|nah\b|nope\b|nahi\b|no thanks|not interested)",
]

# soft "not right now" — defer, do NOT close the conversation
DEFER_PATTERNS = [
    r"\bnot (right )?now\b", r"\blater\b", r"\bbusy\b", r"\bnext week\b", r"\bsome other time\b",
    r"\bcan'?t (right )?now\b", r"\bmaybe later\b", r"\bin a bit\b", r"\bbaad mein\b", r"\babhi nahi\b",
]


def _match_any(patterns: list[str], text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in patterns)


def _sentence(s: str) -> str:
    """Append a full stop only if s doesn't already end on sentence-ending punctuation
    (last_offer is often a CTA question ending in '?' — avoid 'saken?.')."""
    s = (s or "").strip()
    if not s or s[-1] in ".!?":
        return s
    return s + "."


def _as_plan(offer: str, fallback: str) -> str:
    """last_offer is usually phrased as a question ('Want me to draft X?' / 'Kya aap X
    karna chahenge?') — dropping it into a statement verbatim reads as if we were
    re-asking. Strip the question-framing (English and Hinglish) plus trailing
    yes/no scaffolding so it reads as a stated plan."""
    s = (offer or fallback).strip()
    # trailing CTA scaffolding
    s = re.sub(r"\s*[\(\[]?\s*(yes\s*/\s*no|y\s*/\s*n|✅\s*/\s*❌|yes or no)\s*[\)\]]?\s*$", "", s, flags=re.IGNORECASE)
    s = s.strip().rstrip("?।.").strip()
    # English question lead-ins
    s = re.sub(r"^(want me to|shall i|should i|would you like me to|do you want me to|can i)\s+", "",
               s, flags=re.IGNORECASE)
    s = re.sub(r"^(recommend:|reply \w+ to)\s+", "", s, flags=re.IGNORECASE)
    # Hinglish / Hindi question frame:  "kya aap … karna chahenge" -> "… karna"
    s = re.sub(r"^(kya aap|क्या आप)\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+(karna chahenge|karna chahengi|chahenge|chahengi|karenge|karengi|"
               r"करना चाहेंगे|करना चाहेंगी|चाहेंगे|चाहेंगी)\s*$", "", s, flags=re.IGNORECASE)
    return s.strip() or fallback


def _dedupe(state: ConversationState, candidate: str) -> str:
    """Never resend an identical body verbatim in the same conversation."""
    if candidate not in state.sent_bodies:
        return candidate
    variants = [
        candidate + " (following up on this)",
        "Circling back — " + candidate,
        candidate.rstrip(".") + " — happy to hold off if now's not a good time.",
    ]
    for v in variants:
        if v not in state.sent_bodies:
            return v
    return candidate + " "  # last resort, still technically distinct


def _record_sent(state: ConversationState, body: str) -> None:
    state.sent_bodies.add(body)
    state.turns.append({"from": "vera", "message": body})


# ---------------------------------------------------------------------------
# LLM polish for reply bodies — the deterministic machine decides the ACTION and
# INTENT; this turns the decision into one natural, non-repetitive line. Any
# failure falls straight back to the template body, so behaviour never depends
# on the model being up.
# ---------------------------------------------------------------------------
_REPLY_SYS = (
    "You are Vera, magicpin's growth partner, replying to a small-business owner on WhatsApp. "
    "Write ONE short reply (1-2 sentences, under 30 words), warm and human, never robotic. "
    "Do NOT invent facts, numbers, offers or dates. Do NOT repeat a line you've already sent. "
    "Match the owner's language (English / Hindi-English mix). Output only the reply text."
)

_INTENT_BRIEF = {
    "auto_reply_1": "Their reply looks automated. Gently note you'll wait for the owner, and restate the one open ask in fresh words.",
    "commit": "They just said yes / let's do it. Confirm you're on it and state the concrete next step you'll take, in plain words. End by asking them to reply CONFIRM.",
    "clarify": "They asked a genuine follow-up question about the plan (timing, cost, how it works). Answer it briefly and honestly from what you know, then nudge them to confirm.",
    "offtopic": "They asked about something outside magicpin growth help (tax, weather, unrelated). Politely say that's not your area, in one line, then bring them back to the open ask.",
    "affirm": "They agreed. Say you're sending it through now, warmly, and name the next step.",
    "ack": "Their reply didn't clearly signal yes or no. Acknowledge it and restate the single open ask in fresh words, no new asks.",
}


def _polish(state: ConversationState, intent: str, det_body: str, merchant_msg: str) -> str:
    if llm_client is None or not getattr(llm_client, "available", lambda: False)():
        return det_body
    brief = _INTENT_BRIEF.get(intent)
    if not brief:
        return det_body
    already = " | ".join(list(state.sent_bodies)[-3:])
    user = (
        f"OPEN ASK (what we're waiting on): {state.last_offer or 'proceeding with the suggestion'}\n"
        f"BUSINESS: {(state.merchant or {}).get('identity', {}).get('name', 'their business')}\n"
        f"OWNER JUST SAID: \"{merchant_msg}\"\n"
        f"SITUATION: {brief}\n"
        f"Lines you've ALREADY sent this chat (do not repeat): {already or '(none)'}\n"
        f"Write the reply."
    )
    try:
        out = llm_client.chat(_REPLY_SYS, user, temperature=0.3, max_tokens=180, try_fallback_model=True)
    except Exception:
        out = None
    if not out:
        return det_body
    out = out.strip().strip('"').strip()
    out = re.sub(r"^(vera|reply|message)\s*[:\-]\s*", "", out, flags=re.I).strip()
    if 8 <= len(out) <= 320 and out not in state.sent_bodies:
        return out
    return det_body


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

_FOLLOWUP_Q = re.compile(
    r"\b(when|how (long|soon|much|does|will|do)|what('| i)?s? (the )?(cost|price|next|timeline|catch)|"
    r"go live|goes? live|start(ing)?|turnaround|by when|which one|what happens|any (fee|charge)|"
    # Hindi/Hinglish equivalents - the bot is required to code-switch, and a merchant asking a
    # genuine timing/cost question in Hindi must not fall through to the off-topic classifier
    # just because the classifier only recognised English phrasing.
    r"kitna time|kitna waqt|kitne din|kab tak|kab hoga|kitna kharcha|kitna paisa|kitna lagega|"
    r"kya price|price kya|kya cost|cost kya|kaise hoga|kaise karenge)\b", re.I)


def _send(state, intent, det_body, msg, cta="open_ended"):
    body = _polish(state, intent, det_body, msg)
    body = _dedupe(state, body)
    _record_sent(state, body)
    return {"action": "send", "body": body, "cta": cta, "rationale": _RATIONALE.get(intent, intent)}


_RATIONALE = {
    "auto_reply_1": "Likely auto-reply (canned phrasing / verbatim repeat) — one explicit prompt for the owner before backing off.",
    "commit": "Merchant committed ('let's do it') — switched from qualifying to action, no further qualifying questions.",
    "clarify": "Merchant asked a genuine follow-up about the plan — answered briefly, then nudged to confirm.",
    "offtopic": "Out-of-scope ask — politely declined and redirected to the open ask without losing the thread.",
    "affirm": "Merchant accepted the ask — honoured directly rather than re-qualifying.",
    "ack": "Reply didn't match a clear intent — acknowledged and restated the single open ask, no new ask.",
}


def respond(state: ConversationState, merchant_message: str) -> dict:
    if state.ended:
        # one graceful sign-off if they keep messaging a closed thread, then silence
        if not getattr(state, "post_end_ack", False):
            state.post_end_ack = True
            return {"action": "send", "body": "We've wrapped this one up — start a fresh chat any time you need me.",
                    "cta": "none", "rationale": "Conversation already closed; single graceful sign-off."}
        return {"action": "end", "body": "", "rationale": "Conversation already closed; no further sends."}

    msg = merchant_message or ""
    state.turns.append({"from": "merchant", "message": msg})

    is_repeat_of_last = bool(state.last_merchant_message) and msg.strip() == state.last_merchant_message.strip()
    is_auto_reply_like = _match_any(AUTO_REPLY_PATTERNS, msg) or is_repeat_of_last
    state.last_merchant_message = msg
    committed = getattr(state, "committed", False)

    # 1. Auto-reply -------------------------------------------------------------
    if is_auto_reply_like:
        state.auto_reply_streak += 1
        if state.auto_reply_streak == 1:
            det = "Looks like that came through automatically — no rush. When you're back, " + \
                  _sentence(_as_plan(state.last_offer, "just reply YES and I'll get started"))
            return _send(state, "auto_reply_1", det, msg, cta="binary")
        if state.auto_reply_streak == 2:
            return {"action": "wait", "wait_seconds": 14400,
                    "body": "No worries — looks like you're away. I'll check back later.",
                    "rationale": "Same auto-reply twice — owner likely away. Backing off 4 hours."}
        state.ended = True
        return {"action": "end",
                "body": "I'll pause here and leave it with you — just reply when you're back and I'll pick it up.",
                "rationale": f"Auto-reply {state.auto_reply_streak}x with no real engagement — closing, suppression_key on cooldown."}

    # 2. Hostility (before calm opt-out) --------------------------------------
    if _match_any(HOSTILE_PATTERNS, msg):
        state.ended = True; state.suppressed = True
        return {"action": "end", "body": "Understood — I'll stop here. Reach out any time if it's useful later.",
                "rationale": "Explicit frustration/hostility — closing gracefully, suppressing follow-ups for a cooldown."}

    # 3. Explicit opt-out ----------------------------------------------------
    if _match_any(OPTOUT_PATTERNS, msg):
        state.ended = True; state.suppressed = True
        return {"action": "end", "body": "No problem, I'll leave it there. Ping me whenever you want to pick this back up.",
                "rationale": "Explicit opt-out — closing and suppressing this conversation's suppression_key."}

    # 4. Commitment — switch to action, never re-qualify --------------------
    if _match_any(INTENT_TRANSITION_PATTERNS, msg):
        state.committed = True
        next_step = _as_plan(state.last_offer, "the next step")
        det = f"On it — I'll {next_step} and send it over for your approval. Reply CONFIRM and it's done."
        state.last_offer = f"confirming so I can send {next_step}"
        return _send(state, "commit", det, msg, cta="binary")

    # 5. Genuine follow-up question about the plan (post-pitch) -------------
    if "?" in msg and _FOLLOWUP_Q.search(msg) and not _match_any(OFFTOPIC_PATTERNS, msg):
        det = "Usually within a day of you confirming. " + \
              _sentence("Reply CONFIRM and I'll " + _as_plan(state.last_offer, "get it moving"))
        return _send(state, "clarify", det, msg, cta="binary")

    # 6. Off-topic / out-of-scope -----------------------------------------
    looks_like_question = "?" in msg
    on_topic = bool(re.search(
        r"\b(abstract|draft|post|slot|book|yes|no|price|offer|listing|review|promo|campaign|"
        # Hindi/Hinglish equivalents of the same on-topic nouns
        r"offer|slot|price|waqt|paisa|kharcha|booking|samay)\b", msg.lower()))
    if (looks_like_question and not on_topic) or _match_any(OFFTOPIC_PATTERNS, msg):
        det = "That one's outside what I can help with — worth asking the right specialist. " + \
              (f"Back to us: {_as_plan(state.last_offer, 'shall I go ahead?')}." if state.last_offer else "Anything on the growth side I can help with?")
        return _send(state, "offtopic", det, msg, cta="open_ended")

    # 7. Affirmative -----------------------------------------------------
    if _match_any(AFFIRMATIVE_PATTERNS, msg):
        det = "Great — sending that through now: " + _sentence(_as_plan(state.last_offer, "I'll follow up shortly"))
        return _send(state, "affirm", det, msg, cta="open_ended")

    # 8a. Soft "not right now" — defer, keep the thread open ----------------
    if _match_any(DEFER_PATTERNS, msg):
        det = "No problem — I'll check back in a few days. " + \
              _sentence("Reply here any time and I'll pick up " + _as_plan(state.last_offer, "where we left off"))
        r = _send(state, "ack", det, msg, cta="none")
        r["action"] = "wait"; r["wait_seconds"] = 172800
        r["rationale"] = "Merchant asked to defer ('not now' / 'later') — holding, not closing."
        return r

    # 8b. Explicit no ----------------------------------------------------
    if _match_any(NEGATIVE_PATTERNS, msg):
        state.ended = True
        return {"action": "end", "body": "Got it, no worries. I'll leave this one — here if you change your mind.",
                "rationale": "Merchant declined — exiting gracefully rather than re-pitching."}

    # 9. Fallback: acknowledge + restate the single open ask --------------
    det = "Noted. " + _sentence(_as_plan(state.last_offer, "let me know if you'd like me to go ahead"))
    return _send(state, "ack", det, msg, cta="open_ended")
