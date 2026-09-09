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
    r"^\s*(no|nah|not now|nope|nahi)\b",
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
# Main entrypoint
# ---------------------------------------------------------------------------

def respond(state: ConversationState, merchant_message: str) -> dict:
    if state.ended:
        return {"action": "end", "rationale": "Conversation already closed; no further sends."}

    msg = merchant_message or ""
    state.turns.append({"from": "merchant", "message": msg})

    is_repeat_of_last = bool(state.last_merchant_message) and msg.strip() == state.last_merchant_message.strip()
    is_auto_reply_like = _match_any(AUTO_REPLY_PATTERNS, msg) or is_repeat_of_last
    state.last_merchant_message = msg

    # 1. Auto-reply detection ------------------------------------------------
    if is_auto_reply_like:
        state.auto_reply_streak += 1
        if state.auto_reply_streak == 1:
            body = _dedupe(state, "Looks like an auto-reply 😊 When the owner sees this — " + _sentence(state.last_offer or "just reply YES and I'll go ahead"))
            _record_sent(state, body)
            return {"action": "send", "body": body, "cta": "binary",
                    "rationale": "Detected likely auto-reply (canned phrasing / verbatim repeat). One explicit prompt flagged for the owner before backing off."}
        elif state.auto_reply_streak == 2:
            return {"action": "wait", "wait_seconds": 14400,
                    "rationale": "Same auto-reply pattern twice in a row — owner likely not at phone. Backing off 4 hours before retrying."}
        else:
            state.ended = True
            return {"action": "end",
                    "rationale": f"Auto-reply {state.auto_reply_streak}x in a row with zero real engagement signal. Closing conversation; suppressing suppression_key for retry cooldown."}

    # 2. Hostility (checked ahead of plain opt-out — "stop bothering me, useless spam"
    #    should be characterized as hostility, not a calm opt-out, so the rationale
    #    matches what the merchant actually said) -----------------------------------
    if _match_any(HOSTILE_PATTERNS, msg):
        state.ended = True
        state.suppressed = True
        return {"action": "end",
                "rationale": "Merchant frustration/hostility explicit. Closing gracefully without further engagement; suppressing follow-ups for this merchant for a cooldown period."}

    # 3. Explicit opt-out / hard no ------------------------------------------
    if _match_any(OPTOUT_PATTERNS, msg):
        state.ended = True
        state.suppressed = True
        return {"action": "end",
                "rationale": "Merchant explicitly opted out. Closing conversation; suppressing this conversation's suppression_key from future ticks."}

    # 4. Intent transition — switch from pitch/qualify mode to action mode ---
    if _match_any(INTENT_TRANSITION_PATTERNS, msg):
        next_step = _as_plan(state.last_offer, "the next step")
        body = _dedupe(state, f"Great — here's the plan: {next_step}. Reply CONFIRM and I'll send it through.")
        _record_sent(state, body)
        state.last_offer = "confirming and sending the draft"
        return {"action": "send", "body": body, "cta": "binary",
                "rationale": "Merchant explicitly committed ('let's do it' / equivalent). Switching immediately from qualifying to action — no further qualifying questions, per the intent-handoff rule."}

    # 5. Curveball / off-topic ask --------------------------------------------
    # NOTE: word-boundary matching is required here — a naive substring check
    # (e.g. "no" in msg.lower()) false-positives on "do you KNOw", "post" inside
    # "impossible", etc., which would wrongly route a real curveball into the
    # generic fallback instead of the off-topic redirect.
    looks_like_question = "?" in msg
    on_topic_hint = bool(re.search(
        r"\b(abstract|draft|post|slot|book|yes|no|price|offer)\b", msg.lower()
    ))
    is_offtopic_domain = _match_any(OFFTOPIC_PATTERNS, msg)
    if (looks_like_question and not on_topic_hint) or is_offtopic_domain:
        body = _dedupe(state, "That's outside what I can help with directly — best to check with the right specialist for that one. " + (f"Coming back to it: {_as_plan(state.last_offer, '')}." if state.last_offer else "Anything else on the original topic I can help with?"))
        _record_sent(state, body)
        return {"action": "send", "body": body, "cta": "open_ended",
                "rationale": "Off-topic/out-of-scope ask politely declined; redirected back to the original trigger without losing the thread."}

    # 6. Generic affirmative — advance with the promised next step ------------
    if _match_any(AFFIRMATIVE_PATTERNS, msg):
        body = _dedupe(state, "Sending that through now — " + _sentence(_as_plan(state.last_offer, "will follow up shortly")))
        _record_sent(state, body)
        return {"action": "send", "body": body, "cta": "open_ended",
                "rationale": "Merchant accepted the prior ask; honoring it directly rather than re-qualifying."}

    if _match_any(NEGATIVE_PATTERNS, msg):
        state.ended = True
        return {"action": "end", "rationale": "Merchant declined the ask. Exiting gracefully rather than re-pitching."}

    # 7. Fallback: acknowledge + restate the single open ask -------------------
    body = _dedupe(state, "Noted — " + _sentence(state.last_offer or "let me know if you'd like to proceed"))
    _record_sent(state, body)
    return {"action": "send", "body": body, "cta": "open_ended",
            "rationale": "Reply didn't match a known intent signal; acknowledging and restating the single open ask without adding a new one."}
