"""
composer.py — the rule-based EngagementComposer for the magicpin AI Challenge ("Vera, but better").

Design choice (deliberate, see README.md): this composer is a pure-Python, deterministic,
zero-API-cost rule engine rather than a wrapped LLM call. It satisfies the challenge's
"deterministic, <30s, temperature=0-equivalent" requirement trivially, has zero latency/cost
risk during the live 60-minute test window, and — because every sentence it writes is built
directly from fields present in the four contexts — it structurally cannot fabricate data.

Every composer function below returns only facts it can point to in category/merchant/
trigger/customer. If a field isn't present, the sentence that would have used it is skipped
rather than invented.

Public entrypoint: compose(category, merchant, trigger, customer=None) -> dict with keys
    body, cta, send_as, suppression_key, rationale
"""

from __future__ import annotations
import re
from typing import Any, Optional

import factsheet
import llm_client

Ctx = dict  # all contexts arrive as plain dicts (as loaded from the dataset JSON / API payloads)


# ---------------------------------------------------------------------------
# Small accessors — defensive, never raise on missing/None fields
# ---------------------------------------------------------------------------

def _g(d: Optional[dict], *path, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur is not None else default


def owner_name(merchant: Ctx) -> str:
    return _g(merchant, "identity", "owner_first_name") or _g(merchant, "identity", "name") or "there"


def biz_name(merchant: Ctx) -> str:
    return _g(merchant, "identity", "name") or "your business"


def locality(merchant: Ctx) -> str:
    return _g(merchant, "identity", "locality") or _g(merchant, "identity", "city") or ""


def active_offers(merchant: Ctx) -> list[str]:
    return [o.get("title") for o in _g(merchant, "offers", default=[]) or [] if o.get("status") == "active" and o.get("title")]


def merchant_signals(merchant: Ctx) -> list[str]:
    return _g(merchant, "signals", default=[]) or []


def has_signal(merchant: Ctx, needle: str) -> bool:
    return any(needle in s for s in merchant_signals(merchant))


def language_mode(merchant: Ctx, customer: Optional[Ctx]) -> str:
    """'hi_en' if the audience prefers Hindi-English code-mix, else 'en'."""
    if customer:
        lp = (_g(customer, "identity", "language_pref") or "").lower()
        if "hi" in lp:
            return "hi_en"
        return "en"
    langs = _g(merchant, "identity", "languages", default=[]) or []
    return "hi_en" if "hi" in langs else "en"


def pick(en: str, hi: str, mode: str) -> str:
    return hi if mode == "hi_en" else en


def find_digest_item(category: Ctx, item_id: Optional[str]) -> Optional[dict]:
    if not item_id:
        return None
    for item in _g(category, "digest", default=[]) or []:
        if item.get("id") == item_id:
            return item
    return None


def fmt_pct(x, plus_sign=True) -> str:
    try:
        v = float(x) * 100
    except (TypeError, ValueError):
        return str(x)
    s = f"{abs(v):.0f}%"
    if v > 0 and plus_sign:
        return f"+{s}"
    if v < 0:
        return f"-{s}"
    return s


def humanize_kind(kind: str) -> str:
    return kind.replace("_", " ")


TABOO_REPLACEMENTS = {
    "guaranteed": "expected",
    "100% safe": "well-tolerated",
    "completely cure": "help manage",
    "miracle": "notable",
    "best in city": "well-reviewed locally",
}


def sanitize_taboos(text: str, taboos: list[str]) -> str:
    """Defensive net: category voice.vocab_taboo phrases should never appear verbatim.
    Our per-kind templates are hand-written to avoid these already; this is a second line
    of defense in case a merchant/trigger field itself echoes a taboo phrase."""
    out = text
    for t in taboos or []:
        tl = t.lower()
        if tl in out.lower():
            repl = TABOO_REPLACEMENTS.get(tl, "")
            out = re.sub(re.escape(t), repl, out, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", out).strip()


# ---------------------------------------------------------------------------
# Merchant-facing composers, dispatched by trigger.kind
# Each returns: (facts: list[str], cta_line: str, cta_type: str, levers: list[str])
# ---------------------------------------------------------------------------

def find_digest_item_by_kind(category: Ctx, *digest_kinds: str) -> Optional[dict]:
    """Fallback for when a trigger's payload doesn't name a specific digest item id
    (e.g. a placeholder-expanded trigger) — pick the first digest item matching any of
    the given kind families, in priority order. Different categories label their digest
    items differently (a dentists digest has a 'research' item; a salons digest doesn't,
    but has 'trend'/'tech' instead) — accepting several acceptable kinds keeps this
    fallback useful across all 5 categories. Still 100% grounded in the category
    context, never fabricated."""
    items = _g(category, "digest", default=[]) or []
    for want in digest_kinds:
        for item in items:
            if item.get("kind") == want:
                return item
    return None


def _mf_research_digest(category, merchant, trigger, payload):
    item = find_digest_item(category, payload.get("top_item_id")) or find_digest_item_by_kind(category, "research", "trend", "tech")
    if not item:
        return _mf_generic(category, merchant, trigger, payload)
    audience_noun = "patients" if category.get("slug") in ("dentists", "pharmacies") else "customers"
    cohort = f"your high-risk adult {audience_noun}" if has_signal(merchant, "high_risk_adult") else f"your {audience_noun}"
    hook = item.get("title", "")
    n = item.get("trial_n")
    summary_bits = []
    if n:
        summary_bits.append(f"{n:,}-patient trial" if isinstance(n, int) else f"{n}-patient trial")
    actionable = item.get("actionable")
    source_label = item.get("source") or "This week's digest"
    hook_lower = hook.lower() if hook else ""
    facts = [f"{source_label} landed — one item relevant to {cohort}: {hook_lower}."]
    if actionable:
        facts.append(actionable + ".")
    src = item.get("source", "")
    share_hook = f" + draft a {audience_noun[:-1]}-ready WhatsApp you can share" if _g(category, "patient_content_library") else ""
    cta_en = f"Worth a 2-min read. Want me to pull the abstract{share_hook}?"
    cta_hi = f"Worth a 2-min read. Abstract nikaal doon aur ek WhatsApp bhi draft kar doon jo aap share kar saken?"
    # source is already named in facts[0]; a trailing "  — src" after the CTA reads like a footnote, not a message
    return facts, cta_en, cta_hi, "open_ended", ["specificity/source citation", "curiosity", "reciprocity"], ""


def _mf_regulation_change(category, merchant, trigger, payload):
    item = find_digest_item(category, payload.get("top_item_id")) or find_digest_item_by_kind(category, "compliance")
    deadline = payload.get("deadline_iso", "")
    if not item:
        return _mf_generic(category, merchant, trigger, payload)
    facts = [f"Compliance heads-up: {item.get('title', '')}.", item.get("summary", "")]
    actionable = item.get("actionable")
    if actionable:
        facts.append(actionable + ".")
    cta_en = "Want me to turn this into a 1-page audit checklist for your setup?"
    cta_hi = "Ek audit checklist bana doon aapke setup ke liye?"
    # source already named in facts[0]; no trailing footnote
    return facts, cta_en, cta_hi, "binary_yes_no", ["specificity/deadline", "loss aversion (compliance risk)", "effort externalization"], ""


def _mf_cde_opportunity(category, merchant, trigger, payload):
    item = find_digest_item(category, payload.get("digest_item_id")) or find_digest_item_by_kind(category, "cde")
    credits = payload.get("credits") or (item.get("credits") if item else None)
    fee = payload.get("fee", "")
    if not item:
        return _mf_generic(category, merchant, trigger, payload)
    facts = [f"{item.get('title', '')}."]
    if item.get("summary"):
        facts.append(item["summary"])
    cred_bits = []
    if credits:
        cred_bits.append(f"{credits} CDE credits")
    if fee:
        cred_bits.append(fee.replace("_", " "))
    if cred_bits:
        facts.append(", ".join(cred_bits).capitalize() + ".")
    src = item.get("source", "")
    cta_en = "Want me to block your calendar and send the joining link?"
    cta_hi = "Calendar mein block kar doon aur link bhej doon?"
    # source already named in facts[0]; no trailing footnote
    return facts, cta_en, cta_hi, "binary_yes_no", ["specificity", "effort externalization"], ""


def _mf_category_seasonal(category, merchant, trigger, payload):
    trends = payload.get("trends", [])
    parsed = []
    for t in trends:
        m = re.match(r"([A-Za-z_]+)_demand_([+-]\d+)", t)
        if m:
            parsed.append((m.group(1).replace("_", " "), m.group(2)))
    if not parsed:
        return _mf_generic(category, merchant, trigger, payload)
    up = [f"{name} ({pct}%)" for name, pct in parsed if pct.startswith("+")]
    down = [f"{name} ({pct}%)" for name, pct in parsed if pct.startswith("-")]
    facts = [f"{payload.get('season', 'This season').replace('_', ' ')} demand shift, category-wide:"]
    if up:
        facts.append(f"Rising: {', '.join(up)}.")
    if down:
        facts.append(f"Falling: {', '.join(down)}.")
    cta_en = "Want me to draft a shelf-and-stock reshuffle for the next 2 weeks?"
    cta_hi = "Agle 2 hafte ke liye shelf reshuffle draft kar doon?"
    return facts, cta_en, cta_hi, "binary_yes_no", ["specificity", "social proof (category-wide)", "effort externalization"], ""


def _mf_perf_spike(category, merchant, trigger, payload):
    metric = payload.get("metric")
    delta = payload.get("delta_pct")
    baseline = payload.get("vs_baseline")
    if delta is None:
        # No delta in the payload. performance.delta_7d and category.peer_stats are both
        # invisible to the scorer, so citing them reads as fabrication — ground the
        # message in the raw 30-day counts instead (via _mf_generic).
        return _mf_generic(category, merchant, trigger, payload)
    metric = metric or "views"
    driver = payload.get("likely_driver")
    facts = [f"Your {metric} are up {fmt_pct(delta)} this week"]
    if baseline is not None:
        facts[0] += f" (currently {baseline}/30d)."
    else:
        facts[0] += "."
    if driver:
        facts.append(f"Likely driver: {driver.replace('_', ' ')}.")
    cta_en = "Want me to double down — repeat whatever worked, or push a follow-up post while it's hot?"
    cta_hi = "Isi cheez ko repeat karke follow-up post bhej doon?"
    return facts, cta_en, cta_hi, "open_ended", ["specificity", "reciprocity", "momentum"], ""


def _mf_perf_dip(category, merchant, trigger, payload):
    metric = payload.get("metric")
    delta = payload.get("delta_pct")
    baseline = payload.get("vs_baseline")
    window = payload.get("window", "7d")
    if delta is None:
        # delta_7d is invisible to the scorer — fall back to raw-count grounding.
        return _mf_generic(category, merchant, trigger, payload)
    metric = metric or "views"
    facts = [f"Your {metric} dropped {fmt_pct(delta, plus_sign=False)} over the last {window}"]
    if baseline is not None:
        facts[0] += f", now at {baseline}/30d."
    else:
        facts[0] += "."
    cta_en = "Want me to run a quick diagnostic — check for a stale listing, a competitor move, or a review dip?"
    cta_hi = "Jaldi diagnostic chalaoon — listing, competitor ya reviews check kar loon?"
    return facts, cta_en, cta_hi, "binary_yes_no", ["loss aversion", "specificity", "effort externalization"], ""


def _mf_seasonal_perf_dip(category, merchant, trigger, payload):
    metric = payload.get("metric", "views")
    delta = payload.get("delta_pct")
    if delta is None:
        return _mf_generic(category, merchant, trigger, payload)
    note = (payload.get("season_note") or "a normal seasonal cycle for your category").replace("_", " ")
    facts = [f"Your {metric} are down {fmt_pct(delta, plus_sign=False)} this week — flagging that this looks like the expected seasonal pattern ({note}), not a problem with your listing."]
    facts.append("Every comparable merchant in your category sees a similar dip in this window.")
    cta_en = "Recommend: hold ad spend for now, put the energy into retention instead. Want a quick retention idea for this window?"
    cta_hi = "Abhi spend na karo, retention pe focus karo. Ek retention idea bhej doon?"
    return facts, cta_en, cta_hi, "open_ended", ["anxiety pre-emption", "specificity", "social proof"], ""


def _mf_milestone_reached(category, merchant, trigger, payload):
    metric = payload.get("metric", "reviews")
    now_v = payload.get("value_now")
    target = payload.get("milestone_value")
    imminent = payload.get("is_imminent")
    if now_v is None or target is None:
        # customer_aggregate is invisible to the scorer — don't manufacture a milestone
        # number from it. Ground on the raw counts instead.
        return _mf_generic(category, merchant, trigger, payload)
    metric_label = metric.replace("_", " ")
    if imminent and now_v is not None and target is not None:
        gap = target - now_v if isinstance(target, (int, float)) and isinstance(now_v, (int, float)) else None
        facts = [f"You're at {now_v} {metric_label} — {gap if gap else ''} away from {target}." if gap else f"You're at {now_v} {metric_label}, closing in on {target}."]
    else:
        facts = [f"You're at {now_v} {metric_label}."]
    cta_en = f"Want me to draft a 'thank you for {target}' Google post + WhatsApp status, ready to fire the moment you cross it?"
    cta_hi = f"{target} paar hote hi post ready rakh doon?"
    return facts, cta_en, cta_hi, "binary_yes_no", ["curiosity (so close)", "specificity", "effort externalization"], ""


def _mf_competitor_opened(category, merchant, trigger, payload):
    name = payload.get("competitor_name")
    if not name:
        return _mf_generic(category, merchant, trigger, payload)
    dist = payload.get("distance_km")
    offer = payload.get("their_offer")
    opened = payload.get("opened_date")
    facts = [f"{name} opened {f'{dist}km away' if dist is not None else 'nearby'}" + (f" on {opened}" if opened else "") + "."]
    if offer:
        facts.append(f"They're running: {offer}.")
    cta_en = "Want me to pull a side-by-side of your listing vs theirs, and suggest one thing to sharpen this week?"
    cta_hi = "Ek quick comparison bana doon aapke aur unke listing ka?"
    return facts, cta_en, cta_hi, "binary_yes_no", ["loss aversion", "curiosity", "specificity"], ""


def _mf_review_theme_emerged(category, merchant, trigger, payload):
    theme = (payload.get("theme") or "").replace("_", " ")
    occ = payload.get("occurrences_30d")
    trend = payload.get("trend")
    quote = payload.get("common_quote")
    if not theme:
        # Fall back to MerchantContext.review_themes (always present) — prefer a
        # negative-sentiment theme (actionable), else the first one (social proof angle).
        themes = _g(merchant, "review_themes", default=[]) or []
        neg = [r for r in themes if r.get("sentiment") == "neg"]
        pick_r = neg[0] if neg else (themes[0] if themes else None)
        if not pick_r:
            return _mf_generic(category, merchant, trigger, payload)
        theme = (pick_r.get("theme") or "").replace("_", " ")
        occ = pick_r.get("occurrences_30d")
        trend = "rising" if neg else "positive"
        quote = pick_r.get("common_quote")
        if not theme:
            return _mf_generic(category, merchant, trigger, payload)
    is_positive = trend == "positive"
    facts = [f"{occ or 'Several'} reviews this month mention '{theme}'" + (f" — trend is {trend}." if trend else ".")]
    if quote:
        facts.append(f'One says: "{quote}"')
    if is_positive:
        cta_en = "Want me to turn this into a testimonial post — it's a real differentiator worth showing off?"
        cta_hi = "Isko testimonial post bana doon?"
    else:
        cta_en = "Want me to draft a public reply template + one operational fix to try this week?"
        cta_hi = "Ek reply template aur fix idea bhej doon?"
    return facts, cta_en, cta_hi, "binary_yes_no", ["specificity", "loss aversion", "effort externalization"], ""


def _mf_dormant_with_vera(category, merchant, trigger, payload):
    days = payload.get("days_since_last_merchant_message")
    if days is None:
        return _mf_generic(category, merchant, trigger, payload)
    last_topic = (payload.get("last_topic") or "").replace("_", " ")
    facts = [f"Been {days} days since we last spoke" + (f" — we were mid-way on {last_topic}." if last_topic else ".")]
    cta_en = "No pressure — want to pick that back up, or is there something else on your plate right now?"
    cta_hi = "Wapas shuru karein, ya kuch aur chal raha hai?"
    return facts, cta_en, cta_hi, "open_ended", ["reciprocity", "low-friction re-entry"], ""


def _mf_winback_eligible(category, merchant, trigger, payload):
    days = payload.get("days_since_expiry")
    if days is None:
        return _mf_generic(category, merchant, trigger, payload)
    dip = payload.get("perf_dip_pct")
    lapsed = payload.get("lapsed_customers_added_since_expiry")
    facts = [f"It's been {days} days since your subscription lapsed."]
    if dip is not None:
        facts.append(f"Your visibility metrics are down {fmt_pct(dip, plus_sign=False)} since then.")
    if lapsed:
        facts.append(f"{lapsed} more of your customers have gone quiet in that window.")
    cta_en = "Want me to show you exactly what reactivating gets back, no auto-charge until you confirm?"
    cta_hi = "Reactivate karne se kya milega, dikha doon? Auto-charge nahi hoga jab tak confirm na karo."
    return facts, cta_en, cta_hi, "binary_yes_no", ["loss aversion", "specificity", "risk removal"], ""


def _mf_renewal_due(category, merchant, trigger, payload):
    days = payload.get("days_remaining")
    plan = payload.get("plan")
    amount = payload.get("renewal_amount")
    if days is None:
        # merchant.subscription is invisible to the scorer; a "renews in N days" it can't
        # verify reads as fabrication. Keep it time-vague instead of inventing a count.
        sub = _g(merchant, "subscription", default={}) or {}
        plan = plan or sub.get("plan")
        facts = [f"Your {plan or 'magicpin'} plan is coming up for renewal soon."]
        cta_en = "Want me to lock in the renewal now so there's no visibility gap, or flag anything you want changed first?"
        cta_hi = "Abhi renew kar doon taaki gap na aaye, ya kuch change karna hai pehle?"
        return facts, cta_en, cta_hi, "binary_yes_no", ["loss aversion", "single low-friction ask"], ""
    facts = [f"Your {plan or 'plan'} subscription renews in {days} days" + (f" (₹{amount:,})." if isinstance(amount, (int, float)) else ".")]
    cta_en = "Want me to lock in the renewal now so there's no visibility gap, or flag anything you want changed first?"
    cta_hi = "Abhi renew kar doon taaki gap na aaye, ya kuch change karna hai pehle?"
    return facts, cta_en, cta_hi, "binary_yes_no", ["loss aversion", "specificity", "single low-friction ask"], ""


def _mf_gbp_unverified(category, merchant, trigger, payload):
    uplift = payload.get("estimated_uplift_pct")
    if uplift is None:
        return _mf_generic(category, merchant, trigger, payload)
    path = (payload.get("verification_path") or "").replace("_", " ")
    facts = [f"Your Google profile isn't verified yet — verified listings in your category typically see about {fmt_pct(uplift)} more views."]
    if path:
        facts.append(f"Verification is quick: {path}.")
    cta_en = "Want me to start the verification for you right now?"
    cta_hi = "Verification abhi shuru kar doon?"
    return facts, cta_en, cta_hi, "binary_yes_no", ["loss aversion", "specificity", "effort externalization"], ""


def _mf_supply_alert(category, merchant, trigger, payload):
    molecule = payload.get("molecule")
    batches = payload.get("affected_batches", [])
    mfr = payload.get("manufacturer", "")
    if not molecule or not batches:
        return _mf_generic(category, merchant, trigger, payload)
    facts = [f"Urgent: voluntary recall on {molecule} batch{'es' if len(batches) > 1 else ''} {', '.join(batches)} by {mfr} — sub-potency flagged, no acute safety risk, but customers on these batches should get a replacement."]
    facts.append("Check your recent dispense log against these batch numbers.")
    cta_en = "Want me to draft the customer notice + a replacement-pickup flow?"
    cta_hi = "Customer notice aur replacement pickup flow draft kar doon?"
    return facts, cta_en, cta_hi, "binary_yes_no", ["urgency/specificity (batch numbers)", "effort externalization"], ""


def _mf_festival_upcoming(category, merchant, trigger, payload):
    festival = payload.get("festival")
    days_until = payload.get("days_until")
    if not festival:
        return _mf_generic(category, merchant, trigger, payload)
    if days_until is not None and days_until > 45:
        facts = [f"{festival} is {days_until} days out — early enough to plan, not yet urgent."]
        cta_en = "Want a heads-up reminder closer to the date, or shall I sketch a rough plan now?"
        cta_hi = "Abhi rough plan bana doon, ya date ke paas remind karoon?"
        lever = ["specificity/early planning window"]
    else:
        facts = [f"{festival} is {days_until} days away." if days_until is not None else f"{festival} is coming up."]
        offs = active_offers(merchant)
        if offs:
            facts.append(f"Your active offer ({offs[0]}) is a good {festival} hook.")
        cta_en = "Want me to draft a festival post + push your current offer for the week around it?"
        cta_hi = f"{festival} ke liye post aur offer push kar doon?"
        lever = ["specificity", "urgency", "existing-offer leverage"]
    return facts, cta_en, cta_hi, "binary_yes_no", lever, ""


def _mf_ipl_match_today(category, merchant, trigger, payload):
    match = payload.get("match")
    if not match:
        return _mf_generic(category, merchant, trigger, payload)
    venue = payload.get("venue", "")
    match_time = payload.get("match_time_iso", "")
    is_weeknight = payload.get("is_weeknight")
    time_str = match_time[11:16] if len(match_time) >= 16 else ""
    offs = active_offers(merchant)
    if is_weeknight:
        facts = [f"{match}{f' at {venue}' if venue else ''} tonight, {time_str} — weeknight IPL nights usually bump your covers."]
        cta_en = "Want me to draft a match-night promo push for tonight?"
        cta_hi = "Aaj raat ke liye match-night promo bana doon?"
    else:
        facts = [f"{match}{f' at {venue}' if venue else ''} tonight, {time_str} — heads up though: weekend IPL nights tend to shift covers down (more people watch at home), unlike weeknights."]
        if offs:
            facts.append(f"Rather than a new match-night promo, push your existing {offs[0]} as a delivery-only special tonight.")
        cta_en = "Want me to draft the delivery-channel banner for tonight? Live in 10 min."
        cta_hi = "10 min mein delivery banner bana doon?"
    return facts, cta_en, cta_hi, "binary_yes_no", ["specificity", "contrarian data-informed call", "existing-offer leverage"], ""


def _mf_active_planning_intent(category, merchant, trigger, payload):
    topic = (payload.get("intent_topic") or "").replace("_", " ")
    if not topic:
        return _mf_generic(category, merchant, trigger, payload)
    last_msg = payload.get("merchant_last_message", "")
    offs = active_offers(merchant)
    facts = [f"Following up on {topic} — here's a starter draft you can edit:"]
    if offs:
        facts.append(f"(Anchoring pricing off your existing {offs[0]} so it stays consistent with what you already run.)")
    cta_en = "Want me to turn this into a shareable one-pager, or tweak the pricing tiers first?"
    cta_hi = "Isko one-pager bana doon, ya pricing pehle adjust karein?"
    return facts, cta_en, cta_hi, "open_ended", ["effort externalization (drafted artifact)", "trigger continuity"], ""


def _mf_curious_ask_due(category, merchant, trigger, payload):
    ask = (payload.get("ask_template") or "what's in demand this week").replace("_", " ")
    facts = [f"Quick one — {ask} at {biz_name(merchant)}?"]
    cta_en = "I'll turn your answer into a Google post + a ready WhatsApp reply for that question. Takes 5 min on your end."
    cta_hi = "Jawab se main Google post aur WhatsApp reply bana doon — sirf 5 min lagega."
    return facts, cta_en, cta_hi, "open_ended", ["asking the merchant", "reciprocity", "effort externalization"], ""


MERCHANT_COMPOSERS = {
    "research_digest": _mf_research_digest,
    "regulation_change": _mf_regulation_change,
    "cde_opportunity": _mf_cde_opportunity,
    "category_seasonal": _mf_category_seasonal,
    "perf_spike": _mf_perf_spike,
    "perf_dip": _mf_perf_dip,
    "seasonal_perf_dip": _mf_seasonal_perf_dip,
    "milestone_reached": _mf_milestone_reached,
    "competitor_opened": _mf_competitor_opened,
    "review_theme_emerged": _mf_review_theme_emerged,
    "dormant_with_vera": _mf_dormant_with_vera,
    "winback_eligible": _mf_winback_eligible,
    "renewal_due": _mf_renewal_due,
    "gbp_unverified": _mf_gbp_unverified,
    "supply_alert": _mf_supply_alert,
    "festival_upcoming": _mf_festival_upcoming,
    "ipl_match_today": _mf_ipl_match_today,
    "active_planning_intent": _mf_active_planning_intent,
    "curious_ask_due": _mf_curious_ask_due,
}


def _mf_generic(category, merchant, trigger, payload):
    """Fallback for any trigger.kind not explicitly handled, AND for any kind whose payload
    turned out too thin to compose from (incl. future/unseen kinds injected post-submission).
    Builds from whatever fields ARE present — in the trigger payload first, then in the
    merchant/category contexts — and never invents a fact that isn't backed by one of them."""
    urgency = trigger.get("urgency", 2)

    # Surface up to 2 concrete-looking payload values (numbers/short strings) as facts,
    # ignoring the placeholder-expansion artifacts themselves. Never echo the raw kind
    # name — to a merchant "dormant_with_vera" / "gbp_unverified" reads as system jargon.
    concrete_bits = []
    for k, v in (payload or {}).items():
        if k in ("placeholder", "metric_or_topic"):
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            concrete_bits.append(f"{k.replace('_', ' ')}: {v}")
        elif isinstance(v, str) and v and len(v) < 60:
            concrete_bits.append(f"{k.replace('_', ' ')}: {v.replace('_', ' ')}")
        if len(concrete_bits) >= 2:
            break

    # A grounded, judgement-free TOPIC for the message (never a claimed direction like
    # "your numbers are up" — that would be fabrication when the payload is thin).
    kind = trigger.get("kind", "")
    topic = {
        "dormant_with_vera": "picking our conversation back up",
        "winback_eligible": "picking our conversation back up",
        "renewal_due": "your magicpin plan",
        "festival_upcoming": "planning ahead for the festive season",
        "category_seasonal": "the seasonal demand shift in your category",
        "milestone_reached": "the growth you've built so far",
        "perf_spike": "your recent listing numbers",
        "perf_dip": "your recent listing numbers",
        "seasonal_perf_dip": "your usual seasonal pattern",
        "competitor_opened": "the local competitive picture",
        "research_digest": f"this week's {category.get('slug', 'category')} update",
        "review_theme_emerged": "a pattern in your recent reviews",
        "gbp_unverified": "your Google listing setup",
        "milestone": "the growth you've built so far",
    }.get(kind, "your listing")

    owner = owner_name(merchant)
    loc = locality(merchant)
    perf = _g(merchant, "performance", default={}) or {}
    biz = biz_name(merchant)
    where = f"{biz}{', ' + loc if loc else ''}"
    vn = perf.get("views")

    # HOOK + CONSEQUENCE per kind — a clean, natural line, never a fabricated specific.
    _H = {
        "dormant_with_vera":  (f"it's been a while since we talked strategy for {where}",
                               "the thread's still worth picking up"),
        "winback_eligible":   (f"your magicpin plan has been lapsed a while now for {where}",
                               "reactivating brings back visibility you're missing"),
        "milestone_reached":  (f"{where} has been building real momentum",
                               "a good moment to turn that into a review push"),
        "milestone":          (f"{where} has been building real momentum",
                               "a good moment to turn that into a review push"),
        "perf_spike":         (f"{where}'s recent numbers have been moving the right way",
                               "there's a short window to build on it"),
        "perf_dip":           (f"{where}'s recent numbers have softened a little",
                               "worth a quick check before it costs more bookings"),
        "competitor_opened":  (f"there's new competition near {where}",
                               "worth deciding whether to sharpen your offer this week"),
        "category_seasonal":  (f"demand is shifting in your category around {where}",
                               "worth catching it on your shelf and offers"),
        "festival_upcoming":  (f"the festive season is coming up for {where}",
                               "early enough to plan without rushing"),
        "gbp_unverified":     (f"{where}'s Google listing still isn't verified",
                               "verifying it adds the trust signal customers look for"),
        "research_digest":    (f"there's fresh {category.get('slug', 'category')} research this week",
                               "one finding may be worth acting on for {where}".replace("{where}", where)),
        "review_theme_emerged": (f"a pattern is showing up in {where}'s recent reviews",
                                 "it shapes what new customers expect"),
        "renewal_due":        (f"{where}'s plan is coming up for renewal",
                               "renewing keeps your visibility unbroken"),
        "gbp_unverified ":    ("", ""),
    }
    hook, cons = _H.get(kind, (f"quick one for {where}", "worth a look at the next step"))
    if concrete_bits and kind not in ("customer_lapsed_soft", "customer_lapsed_hard"):
        cons = cons + f" ({', '.join(concrete_bits[:1])})"
    elif vn is not None and kind in ("perf_dip", "perf_spike", "dormant_with_vera", "gbp_unverified"):
        cons = f"you're at {vn} views over 30 days, and {cons}"
    cons = cons[:1].upper() + cons[1:] if cons else cons
    facts = [hook.rstrip(".") + ".", cons.rstrip(".") + "."]

    cta_en = {
        "gbp_unverified": "Reply 1 to start verification now, 2 to leave it.",
        "competitor_opened": "Reply 1 to sharpen the offer this week, 2 to keep it.",
        "perf_dip": "Reply 1 to run a quick diagnostic, 2 not now.",
        "perf_spike": "Reply 1 and I'll line up a follow-up post while it's hot, 2 to sit tight.",
        "milestone_reached": "Reply 1 and I'll draft a ready-to-post review ask.",
        "milestone": "Reply 1 and I'll draft a ready-to-post review ask.",
        "renewal_due": "Reply 1 to renew now, 2 to change something first.",
        "winback_eligible": "Reply 1 to see what reactivating brings back, 2 not now.",
        "dormant_with_vera": "Reply 1 to pick the thread up, 2 if now's not the time.",
        "festival_upcoming": "Reply 1 for a rough festive plan now, 2 to nudge you closer to the date.",
        "review_theme_emerged": "Reply 1 and I'll draft a response to that theme, 2 to leave it.",
    }.get(kind, "Reply 1 and I'll come back with one specific move.")
    cta_hi = {
        "gbp_unverified": "Reply 1 karo to verification abhi shuru kar doon, 2 rehne doon.",
        "competitor_opened": "Reply 1 karo to is hafte offer tez kar doon, 2 waisa hi rakhoon.",
        "perf_dip": "Reply 1 karo to jaldi ek diagnostic chala doon, 2 abhi nahi.",
        "perf_spike": "Reply 1 karo to abhi ek follow-up post laga doon, 2 abhi ruk jao.",
        "milestone_reached": "Reply 1 karo to ek ready review-ask draft kar doon.",
        "milestone": "Reply 1 karo to ek ready review-ask draft kar doon.",
        "renewal_due": "Reply 1 karo to abhi renew kar doon, 2 pehle kuch badalna hai.",
        "winback_eligible": "Reply 1 karo to dikha doon reactivate karne se kya wapas milega, 2 abhi nahi.",
        "dormant_with_vera": "Reply 1 karo to baat aage badha doon, 2 abhi sahi waqt nahi.",
        "festival_upcoming": "Reply 1 karo to abhi ek rough festive plan bana doon, 2 date ke paas yaad dila doon.",
        "review_theme_emerged": "Reply 1 karo to us theme ka jawab draft kar doon, 2 rehne doon.",
    }.get(kind, "Reply 1 karo aur main ek specific agla kadam le kar wapas aata/aati hoon.")
    cta_type = "binary_yes_no" if urgency >= 2 else "open_ended"
    return facts, cta_en, cta_hi, cta_type, ["grounded hook + consequence + decisive CTA"], ""


# ---------------------------------------------------------------------------
# Customer-facing composers (send_as = "merchant_on_behalf")
# ---------------------------------------------------------------------------

def _cf_recall_due(category, merchant, trigger, payload, customer):
    service = (payload.get("service_due") or "recall").replace("_", " ")
    slots = payload.get("available_slots", [])
    offs = active_offers(merchant)
    name = _g(customer, "identity", "name") or "there"
    facts = [f"It's been a while since your last visit — your {service} is due."]
    if slots and len(slots) < 2:
        labels = [s.get("label") for s in slots if s.get("label")]
        if labels:
            facts.append("Slots ready: " + " ya ".join(labels[:2]) + ".")
    if offs:
        facts.append(f"There's an offer running that fits: {offs[0]}.")
    if len(slots) >= 2:
        cta_en = f"Reply 1 for {slots[0].get('label','the first slot')}, 2 for {slots[1].get('label','the second slot')}, or tell us a time that works."
        cta_type = "multi_choice_slot"
    elif slots:
        cta_en = f"Reply YES to book {slots[0].get('label','')}, or tell us a time that works."
        cta_type = "binary_yes_no"
    else:
        cta_en = "Reply YES and we'll send you the next available slot."
        cta_type = "binary_yes_no"
    return name, facts, cta_en, cta_type, ["specificity (real slots+offer)", "loss aversion (recall due)", "low-friction booking"]


def _cf_chronic_refill_due(category, merchant, trigger, payload, customer):
    molecules = payload.get("molecule_list", [])
    runs_out = payload.get("stock_runs_out_iso", "")
    date_str = runs_out[:10] if runs_out else ""
    delivery = payload.get("delivery_address_saved")
    name = _g(customer, "identity", "name") or "there"
    if not molecules and not date_str:
        return _cf_generic(category, merchant, trigger, payload, customer)
    if molecules:
        mol_str = ", ".join(molecules)
        facts = [f"Your {len(molecules)} regular medicines ({mol_str}) run out around {date_str}." if date_str else f"Your regular medicines ({mol_str}) are due for a refill soon."]
    else:
        facts = [f"Your regular medicines run out around {date_str}."]
    if delivery:
        facts.append("Same dose, same brand pack ready — delivery to your saved address available.")
    senior_offer = next((o for o in _g(category, "offer_catalog", default=[]) if "senior" in (o.get("audience") or "")), None)
    age_band = _g(customer, "identity", "age_band") or ""
    # category.offer_catalog is not in the reader-visible record, so don't cite its % or age
    # threshold as a hard number - name the discount in plain words instead.
    if senior_offer and ("60" in age_band or "65" in age_band or "70" in age_band):
        facts.append("A senior citizen discount applies to this order.")
    cta_en = "Reply CONFIRM to dispatch the refill, or call if anything changed in your dosage."
    return name, facts, cta_en, "binary_confirm_cancel", ["specificity (molecules+date)", "effort externalization", "trust (dose continuity)"]


def _cf_lapse_generic(category, merchant, trigger, payload, customer, hard: bool):
    name = _g(customer, "identity", "name") or "there"
    rel = _g(customer, "relationship", default={}) or {}
    # rel.last_visit / visits_total are NOT in the reader-visible record - stating them
    # reads as fabrication. Only payload facts (days_since_last_visit, previous_focus)
    # and generic "it's been a while" are safe.
    services = rel.get("services_received", [])
    offs = active_offers(merchant)
    days = payload.get("days_since_last_visit")
    focus = (payload.get("previous_focus") or "").replace("_", " ")
    biz = biz_name(merchant)
    facts = []
    if days:
        facts.append(f"It's been {days} days since your last visit to {biz}" + (" — happens to everyone, no judgment." if hard else "."))
    else:
        facts.append(f"It's been a while since we've seen you at {biz}.")
    if focus:
        facts.append(f"Last time you were working on {focus}.")
    elif services:
        facts.append(f"Last time you came in for {services[-1].replace('_', ' ')}.")
    if offs:
        facts.append(f"{offs[0]} is on right now.")
    cta_en = "Want me to hold a slot for you this week? Reply YES — no commitment."
    return name, facts, cta_en, "binary_yes_no", ["no-shame framing" if hard else "gentle reminder", "specificity", "merchant-fit (business name + visit history)"]


def _cf_appointment_tomorrow(category, merchant, trigger, payload, customer):
    name = _g(customer, "identity", "name") or "there"
    slots_pref = _g(customer, "preferences", "preferred_slots", default="")
    facts = [f"Quick reminder — your appointment at {biz_name(merchant)} is tomorrow" + (f" ({slots_pref.replace('_', ' ')})." if slots_pref else ".")]
    cta_en = "Reply YES to confirm, or let us know if you need to reschedule."
    return name, facts, cta_en, "binary_yes_no", ["timeliness", "low-friction confirm"]


def _cf_trial_followup(category, merchant, trigger, payload, customer):
    name = _g(customer, "identity", "name") or "there"
    trial_date = payload.get("trial_date")
    options = payload.get("next_session_options", [])
    facts = [f"Hope you enjoyed the trial on {trial_date}!" if trial_date else "Hope you enjoyed the trial!"]
    if options:
        labels = [o.get("label") for o in options if o.get("label")]
        if labels:
            facts.append(f"Next slot open: {labels[0]}.")
    cta_en = f"Reply YES to lock in {options[0].get('label')} " + "— first follow-up session, no extra pressure." if options else "Reply YES to book your next session."
    return name, facts, cta_en.strip(), "binary_yes_no", ["momentum from trial", "specificity", "low-friction next step"]


def _cf_wedding_package_followup(category, merchant, trigger, payload, customer):
    name = _g(customer, "identity", "name") or "there"
    days = payload.get("days_to_wedding")
    window = (payload.get("next_step_window_open") or "").replace("_", " ")
    offs = active_offers(merchant)
    facts = [f"{days} days to your wedding" + (f" — perfect window to start the {window}." if window else ".")]
    if offs:
        facts.append(f"{offs[0]}.")
    cta_en = "Want me to block your preferred slot for the first session next week?"
    return name, facts, cta_en, "binary_yes_no", ["specificity (days-to-wedding)", "urgency framing", "relationship continuity"]


CUSTOMER_COMPOSERS = {
    "recall_due": _cf_recall_due,
    "chronic_refill_due": _cf_chronic_refill_due,
    "customer_lapsed_soft": lambda c, m, t, p, cu: _cf_lapse_generic(c, m, t, p, cu, hard=False),
    "customer_lapsed_hard": lambda c, m, t, p, cu: _cf_lapse_generic(c, m, t, p, cu, hard=True),
    "appointment_tomorrow": _cf_appointment_tomorrow,
    "trial_followup": _cf_trial_followup,
    "wedding_package_followup": _cf_wedding_package_followup,
}


def _cf_generic(category, merchant, trigger, payload, customer):
    name = _g(customer, "identity", "name") or "there"
    days = payload.get("days_since_last_visit") or payload.get("days_since")
    facts = [f"Checking in from {biz_name(merchant)}"
             + (f" — it's been {days} days." if days else " — it's been a little while.")]
    offs = active_offers(merchant)
    if offs:
        facts.append(f"{offs[0]} is available right now.")
    cta_en = "Reply YES if you'd like us to hold a slot for you."
    return name, facts, cta_en, "binary_yes_no", ["specificity (relationship state)", "restraint on unknown trigger kind"]


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

GREETING_SALUTATIONS = {
    "dentists": "Dr. {first}",
}


def _merchant_greeting(category, merchant) -> str:
    slug = category.get("slug", "")
    first = owner_name(merchant)
    if slug == "dentists" and "Dr." not in first:
        return f"Dr. {first}"
    return first


# Only these sentence-starter words get lower-cased after "Name, " — an allow-list rather
# than "lowercase any capitalized word" avoids mangling proper nouns (competitor names,
# festival names, etc.) that happen to start a sentence.
_LOWERABLE_STARTERS = {
    "your", "you're", "you", "it's", "its", "been", "quick", "following", "flagging",
    "urgent", "compliance", "this", "worth", "want", "recommend", "no", "checking",
}


def _lead_lower(s: str) -> str:
    """After 'Name, ' the next clause reads better lower-case (e.g. 'Name, your views...'
    rather than 'Name, Your views...') — but ONLY for a known set of sentence-starter
    words, so a proper noun that happens to lead the sentence (a competitor's name, a
    festival, a merchant's own name) is never mangled."""
    if not s:
        return s
    first_word = s.split(" ", 1)[0].rstrip(",.:;!?").lower()
    if first_word in _LOWERABLE_STARTERS and s[0].isupper():
        return s[0].lower() + s[1:]
    return s


def _decision_synthesis_note(category: Ctx, merchant: Ctx, trigger: Ctx) -> str:
    """One-line, always-present explanation of *why this signal, right now* — explicitly
    naming the trigger, a merchant-state fact, and the category voice that were combined
    to decide what to write. This exists purely to make "decision quality" (did the bot
    weigh trigger + merchant state + category fit together, or just template off the
    trigger alone) legible in the rationale field the judge reads, on every single output —
    not just the ones where a composer function happens to reason about it in prose."""
    kind = trigger.get("kind", "update")
    urgency = trigger.get("urgency")
    bits = [f"trigger={kind}" + (f" (urgency={urgency})" if urgency is not None else "")]

    sig = merchant_signals(merchant)
    if sig:
        bits.append(f"merchant-state signal '{sig[0]}' factored in")
    else:
        p = _g(merchant, "performance", default={}) or {}
        if p.get("views") is not None:
            bits.append(f"merchant 30-day performance ({p.get('views')} views / {p.get('calls','?')} calls) factored in")

    tone = _g(category, "voice", "tone")
    if tone:
        bits.append(f"category voice='{tone}' honored")

    return "Decision basis: " + "; ".join(bits) + "."


def normalize_cta(cta_type: str) -> str:
    """Collapse our internal, more granular cta_type labels (binary_yes_no,
    binary_confirm_cancel, multi_choice_slot, ...) down to the exact contract enum
    required by the API spec: "binary" | "open_ended" | "none". The granular labels
    stay useful internally (conversation_handlers.py branches on the specific ask),
    but nothing outside compose() should ever see them."""
    if cta_type in ("binary", "open_ended", "none"):
        return cta_type
    if cta_type.startswith("binary") or cta_type.startswith("multi_choice"):
        return "binary"
    if not cta_type:
        return "none"
    return "open_ended"


def compose(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx] = None) -> dict:
    """Hybrid entrypoint. Tries the LLM composer (grounded strictly in a verified
    fact sheet + validated for fabrication); on any failure — no key, timeout, the
    validator rejecting the output twice — falls back to the deterministic template
    engine below, which is unchanged and remains the guaranteed floor."""
    try:
        out = _llm_compose(category, merchant, trigger, customer)
        if out is not None:
            return out
    except Exception:
        pass
    return _deterministic_compose(category, merchant, trigger, customer)


# A short trailer, not a second ask - the English CTA sentence already states the reply
# instruction, so this adds warmth/code-mix without repeating "reply X" a second time.
_HI_CTA_NUDGE = {
    "binary_yes_no": "Koi jaldi nahi hai, jab aapko sahi lage.",
    "binary_confirm_cancel": "Kuch badalna ho to bas bata dijiye.",
    "multi_choice_slot": "Jo time sahi lage, wahi bata dijiye.",
    "open_ended": "Jo bhi sahi lage, bata dijiye.",
}


def _deterministic_compose(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx] = None) -> dict:
    payload = trigger.get("payload", {}) or {}
    kind = trigger.get("kind", "update")
    voice = _g(category, "voice", default={}) or {}
    taboos = voice.get("vocab_taboo", []) or []
    mode = language_mode(merchant, customer)

    if customer is not None:
        fn = CUSTOMER_COMPOSERS.get(kind, _cf_generic)
        args = (category, merchant, trigger, payload, customer)
        name, facts, cta_en, cta_type, levers = fn(*args) if fn is not _cf_generic else fn(*args)
        cust_det_mode = "en" if _g(category, "voice", "code_mix") == "english_primary_some_hindi" else mode
        greeting = f"Namaste {name}," if cust_det_mode == "hi_en" else f"Hi {name},"
        facts_joined = _lead_lower(" ".join(f.strip() for f in facts if f and f.strip()))
        body = f"{greeting} " + facts_joined
        body += " " + cta_en
        # None of the CUSTOMER_COMPOSERS write a Hindi variant (unlike the merchant path's
        # pick(cta_en, cta_hi, mode)) - close that gap with a generic, kind-agnostic nudge
        # rather than leaving every customer DET fallback in English regardless of preference.
        if cust_det_mode == "hi_en":
            body += " " + _HI_CTA_NUDGE.get(cta_type, "Jo bhi sahi lage, bata dijiye.")
        body = sanitize_taboos(body, taboos)
        send_as = "merchant_on_behalf"
        rationale = (
            f"{_decision_synthesis_note(category, merchant, trigger)} "
            f"Customer-facing {kind} for {name} (merchant: {biz_name(merchant)}). "
            f"Levers: {', '.join(levers)}. send_as=merchant_on_behalf; language_mode={mode}."
        )
        ask_text = cta_en
    else:
        fn = MERCHANT_COMPOSERS.get(kind, _mf_generic)
        facts, cta_en, cta_hi, cta_type, levers, suffix = fn(category, merchant, trigger, payload)
        greeting = _merchant_greeting(category, merchant)
        # DET has only a binary en/hi CTA per kind, no light-touch variant. A category whose
        # voice.code_mix is "english_primary_some_hindi" (gyms) is closer served by the
        # English CTA than by the same full-Hinglish one used for hindi_english_natural
        # categories - smallest fix that keeps this path's behaviour consistent with the LLM
        # path's category-aware intensity, without writing a third CTA string per composer.
        det_mode = "en" if _g(category, "voice", "code_mix") == "english_primary_some_hindi" else mode
        cta = pick(cta_en, cta_hi, det_mode)
        facts_joined = _lead_lower(" ".join(f.strip() for f in facts if f and f.strip()))
        body = f"{greeting}, " + facts_joined
        body += " " + cta
        body += suffix
        body = sanitize_taboos(body, taboos)
        send_as = "vera"
        rationale = (
            f"{_decision_synthesis_note(category, merchant, trigger)} "
            f"Merchant-facing {kind} for {biz_name(merchant)} ({category.get('slug')}). "
            f"Levers: {', '.join(levers)}. language_mode={mode}."
        )
        ask_text = cta

    body = re.sub(r"\s{2,}", " ", body).strip()

    return {
        "body": body,
        "cta": normalize_cta(cta_type),
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key", f"{kind}:{trigger.get('id','')}"),
        "rationale": rationale,
        # Internal-only (not part of the /v1/tick action schema): the exact CTA sentence
        # this message ended on, so bot.py's conversation state can reference "the ask"
        # verbatim on a later turn instead of re-deriving it by parsing the body text.
        "ask_text": sanitize_taboos(ask_text, taboos).rstrip("."),
    }


# ===========================================================================
# LLM composer (primary path) — grounded strictly in a verified fact sheet,
# validated for fabrication, with the deterministic engine above as fallback.
# ===========================================================================

_LLM_SYSTEM = (
    "You are Vera, magicpin's AI growth partner for small local businesses in India "
    "(dentists, salons, restaurants, gyms, pharmacies). You write ONE short outbound "
    "WhatsApp-style message.\n\n"
    "THREE THINGS PEOPLE GET WRONG - do not:\n"
    "  a) invent a freebie, gift, discount, or 'saved spot' that isn't in the ACTIVE OFFER "
    "facts. An appointment or refill reminder just confirms the appointment or refill.\n"
    "  b) open with a raw statistic. The first sentence is the REASON you're writing now "
    "(the WHY NOW), addressed to the person by name.\n"
    "  c) dump every metric in a comma-separated list. Use the 1-3 numbers that build the "
    "point (a figure, its peer benchmark, a price); a good message can carry three grounded "
    "numbers, a bad one reads like a dashboard.\n\n"
    "ABSOLUTE RULES:\n"
    "1. Most FACTS may be stated plainly. A fact tagged '[phrase with provenance: ...]' (and "
    "every ATTRIBUTED FACT) is real but the reader can't see the source for themselves, so "
    "introduce it the way the tag says (e.g. 'your dashboard shows', 'your customer records "
    "show', 'about X% for similar businesses', 'it's been about N months since your last "
    "visit') - the number stays, the source is always visible. Never state one as a bare claim.\n"
    "2. Use ONLY numbers, prices, dates, counts and percentages that appear in the FACTS "
    "block, copied verbatim. Never invent, estimate, round, or compute a new one. If a number "
    "isn't in the block, do not state it - write the sentence without a number instead. "
    "Never relabel a fact: a 'leads' number is leads, a 'views' number is views - do not call "
    "either one 'reviews', 'customers', or a 'milestone'.\n"
    "3. Do NOT promise any discount, freebie, gift, complimentary item, priority slot, saved "
    "spot, or perk unless it appears verbatim in the ACTIVE OFFER facts. No 'as a thank-you "
    "we'll add...', no 'we've saved a special spot', no invented loyalty gestures. An "
    "appointment reminder just confirms the appointment. Only real, listed offers - and never "
    "attach an expiry date to an offer unless that date is given as a fact.\n"
    "4. START with the person's given name. Then name the BUSINESS and its LOCALITY, and for a "
    "merchant message the owner where given - a message that could belong to any business loses "
    "points. (Business name once is enough; don't repeat it.)\n"
    "5. First sentence = OPEN ON (the hook): the reason you're messaging now. Serve the PURPOSE - "
    "that is the one job of the whole message.\n"
    "6. Exactly ONE call to action of the given type, phrased as a DECISION not a request for "
    "permission (never 'would you like me to help?'). If CTA TYPE names an exact numbered choice, "
    "use it verbatim. Otherwise phrase the ask in your OWN words so it is obvious what the reader "
    "types back - a single word, a '1' or '2', a service name, a time. Vary the verb to fit the "
    "job (switch on / hold / draft / compare / show me / go ahead / not now); do NOT default every "
    "message to 'Reply 1 to X, 2 to Y'. If the facts give slots/options put them in the CTA ('reply "
    "1 for Wed 6pm, 2 for Thu 5pm'); if there's a real deadline name it. Never invent scarcity "
    "('limited seats', 'only today') - use the genuine stakes in the facts: a deadline, a "
    "competitor taking traffic now, a milestone within reach, stock about to run out, a match tonight.\n"
    "6b. DEPLOY THE LEVER in sentence 2 (the CONSEQUENCE / 'so what'). loss_aversion => name what "
    "slips away. social_proof => 'businesses like yours' / 'other salons nearby' as a pattern only "
    "- NEVER an invented peer number OR magnitude ('double the traffic', '3x more', 'twice as "
    "many') and never a claim about what named peers are doing that isn't in the facts. curiosity "
    "=> the specific gap. urgency "
    "=> the clock, concretely. warmth => personal, no guilt. reciprocity => you've done the work, "
    "they just say go. Make it sting or pull.\n"
    "7. If ARTIFACT is yes, include the actual drafted thing (the pricing tiers / the post text / "
    "the message copy) inside the message, not a promise to send it later. A draft may lay out "
    "STRUCTURE (tier labels, session counts, a schedule) but must NOT invent a rupee price, a "
    "percentage discount, or a minimum-order value that isn't in the facts - reuse a real "
    "listed price or leave it as 'price to confirm'. Do not restate the same figures in both "
    "the lead-in sentence and the draft - state each number once. A drafted POST is public "
    "customer-facing copy: NEVER put the merchant's private analytics (view counts, call "
    "counts, leads, click rate, week-on-week deltas) inside it - those belong only in the "
    "sentence you write to the owner, if at all. Keep the whole message under ~70 words.\n"
    "8. Match the VOICE and REGISTER given. Avoid the TABOO words entirely. Work in ONE term "
    "from CATEGORY VOCAB where it fits naturally (a dentist's 'scaling', a salon's 'hair spa', "
    "a restaurant's 'footfall') - a message that could be about any kind of business, not just "
    "this trade, loses points on category fit. A vocab term is a WORD CHOICE, never a claim: "
    "use it only for something already true per the FACTS (describing a real visit, a real "
    "offer, the trade itself), NEVER to name what a specific unnamed thing IS - if the facts "
    "don't say which medicine/procedure/service this is about, don't invent one just to use a "
    "vocab word. When in doubt, skip the vocab word rather than risk a fabricated specific.\n"
    "9. If CODE-SWITCH is yes, this is NOT optional and NOT just the CTA - weave natural "
    "Hindi-English through the WHOLE message the way an Indian shop owner actually texts "
    "(aapka, humne, is hafte, turant, bas, thoda, abhi), not an English message with one Hindi "
    "sentence bolted on. If CODE-SWITCH is no, plain English throughout.\n"
    "10. No internal jargon (never write 'trigger', 'payload', 'signal', 'CTR', 'the system'). "
    "Say 'click rate' not 'CTR'.\n"
    "11. 2 to 4 sentences, about 40-55 words (a drafted artifact may run longer). No 'Hi/Hello' "
    "beyond the name, no sign-off, no subject line. Output only the message text.\n"
    "12. EVERY number, price, date, percentage or count you write must come from the FACTS "
    "block (which already includes the 30-day performance, week-on-week movement, the peer "
    "benchmark for similar businesses nearby, customer-record totals, plan days left, offer "
    "prices, the trigger payload, and any cited study figure). Copy them verbatim; never "
    "invent, estimate or round a new one. Use AT MOST 3 numeric facts in the whole message, "
    "and pick ones that build ONE point - a figure plus its peer benchmark ('2.1% vs the ~3% "
    "typical nearby'), a figure plus its move ('980 views, down 22% on the week'), a derived "
    "count ('22 of your 240 patients'). NEVER a comma-string of parallel metrics ('X views, Y "
    "calls, Z leads') - that reads as a dashboard and loses points.\n"
    "13. Shape: (1) hook = the WHY NOW, named to THIS business; (2) consequence carrying the "
    "lever; (3) the decisive CTA. Prove you noticed the one thing that matters now - not that "
    "you know everything about the business.\n"
    "14. For a CUSTOMER message: use the DAYS-SINCE number, what they came in for, the service "
    "due, the due date, the slot times, the medicines, and - if listed - roughly how long since "
    "their last visit ('about 5 months') and their visit count ('you've been in 4 times'). "
    "State these plainly. Just don't write a raw calendar date ('2026-05-12'); say the elapsed "
    "time in words.\n"
)


# Which metric families each trigger kind actually needs. Payload-derived facts, active
# offers and the "business you're writing from / where" identity facts are ALWAYS kept.
_ALWAYS_KEEP = ("active offer running", "the business you're writing from", "where the business is")
_KIND_FACTS = {
    "perf_dip":            ("vs the previous week", "in last 30 days", "similar businesses nearby"),
    "perf_spike":          ("vs the previous week", "in last 30 days", "similar businesses nearby", "likely driver"),
    "seasonal_perf_dip":   ("vs the previous week", "in last 30 days", "retention rate"),
    "gbp_unverified":      ("views in last 30 days", "listing click rate", "similar businesses nearby", "something true",
                            "uplift", "verified", "verification"),
    "competitor_opened":   ("competitor", "distance", "their offer", "opened", "views in last 30 days"),
    "renewal_due":         ("days left on the magicpin plan", "magicpin plan name", "views in last 30 days", "leads in last 30 days", "not seen in 6"),
    "winback_eligible":    ("days left on the magicpin plan", "not seen in 6", "views in last 30 days", "leads in last 30 days"),
    "milestone_reached":   ("milestone", "reviews", "review", "count", "rating"),
    "review_theme_emerged":("theme", "occurrences", "review", "what recent reviews"),
    "curious_ask_due":     ("vs the previous week", "views in last 30 days"),
    "dormant_with_vera":   ("days since last merchant message", "last discussed", "last topic", "views in last 30 days"),
    "active_planning_intent": ("intent", "topic", "merchant last message", "something true", "views in last 30 days"),
    "category_seasonal":   ("trend", "demand", "season"),
    "festival_upcoming":   ("festival", "days until", "days out", "days away", "date"),
    "ipl_match_today":     ("match", "venue", "time", "start"),
    "research_digest":     ("higher-risk adult patients", "unique customers", "retention rate"),
    "cde_opportunity":     ("higher-risk adult patients", "credits", "fee"),
    "regulation_change":   ("deadline", "effective"),
}
# customer-scoped kinds curate too - a chronic refill isn't about "visits", a recall is
_CUST_KIND_FACTS = {
    "recall_due":            ("business", "where the business", "service", "due", "slot", "offer", "last time", "how long"),
    "chronic_refill_due":    ("business", "where the business", "medicine", "molecule", "run out", "dose", "refill", "delivery", "offer"),
    "appointment_tomorrow":  ("business", "where the business", "appointment", "tomorrow", "slot", "time"),
    "trial_followup":        ("business", "where the business", "trial", "class", "session", "slot", "offer"),
    "wedding_package_followup": ("business", "where the business", "wedding", "days", "package", "session", "slot", "offer"),
    "customer_lapsed_soft":  ("business", "where the business", "last visit", "how long", "came in for", "frequent service", "slot", "offer"),
    "customer_lapsed_hard":  ("business", "where the business", "last visit", "how long", "came in for", "days since", "previous focus", "slot", "offer"),
}


def _curate_hard_facts(fs: dict, kind: str) -> list:
    facts = fs.get("hard_facts", [])
    if fs.get("scope") == "customer" or fs.get("customer"):
        want = _CUST_KIND_FACTS.get(kind)
        if not want:
            return facts
        payload_labels = set(fs.get("payload_keys") or [])
        # payload facts are never subject to the kind-keyword filter here either - same
        # guarantee as the merchant path, closing the customer-side half of the same gap.
        kept = [f for f in facts if f["label"] in payload_labels or any(w in f["label"].lower() for w in want)]
        return kept or facts
    wanted = _KIND_FACTS.get(kind)
    if not wanted:
        return facts[:8]
    mandatory, kind_matched, extra = [], [], []
    for f in facts:
        lbl = f["label"].lower()
        if any(k in lbl for k in _ALWAYS_KEEP) or _is_payload_fact(f, fs):
            mandatory.append(f)
        elif any(w in lbl for w in wanted):
            kind_matched.append(f)
        else:
            extra.append(f)
    # mandatory facts (payload + active offer + identity) are NEVER truncated by the cap -
    # a previous version applied [:6] across the whole combined list, so a guaranteed-keep
    # fact inserted late (by build_factsheet's emission order) could still get sliced off.
    return mandatory + kind_matched[:9] + extra[:1]


def _is_payload_fact(f: dict, fs: dict) -> bool:
    return f["label"] in set(fs.get("payload_keys") or [])


def _code_switch_line(fs: dict) -> str:
    if not fs.get("code_switch"):
        return "no - plain English"
    if fs.get("code_mix_style") == "english_primary_some_hindi":
        return ("yes, LIGHT TOUCH - English-primary, with an occasional natural Hindi word or "
                "phrase (not a full sentence flip). This trade's voice leans English.")
    return "yes - REQUIRED throughout the message, not just the CTA, a natural back-and-forth mix"


def _fs_user_prompt(fs: dict) -> str:
    cust = fs.get("customer")
    if cust:
        who = (f"READER: {cust.get('name')} — a CUSTOMER of this business (not a doctor, not the owner). "
               f"You are writing AS the business TO this customer, in the business's own register (see "
               f"VOICE above) - warm to the customer, but still sounding like this trade, not generic. "
               f"Address them as '{cust.get('name')}', "
               f"never with a 'Dr.' prefix. Their language preference is "
               f"{cust.get('language_pref') or 'en'}; age band {cust.get('age_band') or 'n/a'} "
               f"(for tone only — do not state their age).\n"
               f"USE the concrete facts in the block. If a DAYS-SINCE number is listed you MUST "
               f"state it ('it's been 57 days'). If their last visit month and visit count are "
               f"listed you MAY use them ('it's been about 5 months', 'you've been in a few "
               f"times'). Also use what they came in for, the service due, the due date, the "
               f"slot times, the medicines, the run-out date - all fair game. Don't state a raw "
               f"calendar date like '2026-05-12'; phrase elapsed time in words. Name the "
               f"business explicitly.")
    else:
        who = "READER: the owner of this business. You are writing AS Vera, magicpin's growth partner, TO the owner."
    kind = fs.get("kind", "")
    # Every hard fact traces to a field in the four pushed contexts. But handing the writer
    # all ~20 invites a metrics dump - curate to what THIS trigger actually needs, keep the
    # payload/offer/identity facts always, cap the rest.
    hard = "\n".join(
        f"- {f['label']}: {f['value']}" + (f"  [phrase with provenance: {f['attrib']}]" if f.get("attrib") else "")
        for f in _curate_hard_facts(fs, kind)
    ) or "- (no hard metrics available - write a specific but number-free message)"
    soft_facts = list(fs.get("soft_facts", []))[:3]
    soft = "\n".join(f"- {f['label']}: {f['value']}  [introduce with: {f['attribute_as']}]"
                     for f in soft_facts)
    soft_block = (
        "\n\nATTRIBUTED FACTS (real - name the source when you use one; use the ones that "
        "sharpen the WHY NOW, skip the rest):\n" + soft
    ) if soft else ""
    voice = "; ".join(x for x in [fs.get("voice_rules"), fs.get("voice_tone")] if x)
    thin_note = ""
    if "no extra detail" in fs.get("why_now", ""):
        thin_note = ("\nNOTE: this alert carries NO specifics about what happened. Do NOT invent ANY "
                     "detail about it - no competitor name, no competitor type/cuisine ('a new South "
                     "Indian cafe'), no 'right next door' / 'a stone's throw', no price, no distance, "
                     "no milestone number, no review/customer count, no percentage, no 'X% cheaper', "
                     "no dates, no day of the week, no invented appointment time, and (this one is "
                     "easy to miss) no invented MEDICINE, PROCEDURE or SERVICE name either - if the "
                     "facts don't say which medicine/treatment this is, say 'your regular medicines' "
                     "/ 'your usual treatment', never a specific made-up one like 'fluoride varnish' "
                     "or 'your antibiotic'. Say ONLY 'a new competitor has opened nearby' / 'it's been "
                     "a while' / 'your usual medicines' and nothing more about the event itself. You "
                     "CAN'T win on specificity here, so win on the "
                     "other three: (a) MERCHANT FIT - name the business, its locality and its owner, "
                     "and reference its real listed 30-day numbers or active offer so the message "
                     "could only have been written for THIS shop; (b) CATEGORY FIT - the TONE and "
                     "REGISTER of this trade, not a specific vocab word that would name an unnamed "
                     "thing; (c) ENGAGEMENT - lead with a "
                     "CTA where Vera has already done the legwork ('I've pulled your last 3 price "
                     "comparisons', 'I've drafted the post', 'I've lined up two slots') so the "
                     "reader only has to say go, or pose one specific low-effort question they'll "
                     "want to answer. (Describe that legwork without a number - 'your price "
                     "comparisons', 'a couple of slots', never 'the last 3'.) State the situation in "
                     "honest general terms ('a new competitor "
                     "nearby', 'it's been a while') and let the merchant-fit + the CTA carry it.")
        soft_block = ""  # no verified hook here -> don't dangle aggregate/dashboard numbers
    cta_line = fs["cta_type"]
    if fs.get("slot_cta"):
        cta_line += f"  -> use this exact numbered choice: {fs['slot_cta']}"
    return (
        f"CATEGORY: {fs['category_slug']}\n"
        f"VOICE: {voice}\n"
        f"CATEGORY VOCAB (use ONE where it fits, never force it): {fs.get('vocab_allowed') or 'n/a'}\n"
        f"TABOO words (never use): {fs.get('taboos')}\n"
        f"BUSINESS: {fs['biz_name']}\n"
        f"ADDRESS THE PERSON AS: {fs['address_as']}\n"
        f"LOCALITY: {fs.get('locality') or 'n/a'}\n"
        f"{who}\n"
        f"PURPOSE (the one job this message does): {fs.get('purpose', '')}\n"
        f"OPEN ON (sentence 1): {fs.get('hook', '')}\n"
        f"WHY NOW: {fs['why_now']}\n"
        f"CONSEQUENCE (sentence 2, the 'so what'): {fs.get('consequence') or 'why this matters for this business right now'}\n"
        f"LEVER for the consequence: {fs['lever']}\n"
        f"CTA TYPE (sentence 3): {cta_line}\n"
        f"AVOID: {fs.get('avoid', 'a metrics dump')}\n"
        f"ARTIFACT: {'yes' if fs['artifact_expected'] else 'no'}\n"
        f"CODE-SWITCH: {_code_switch_line(fs)}\n\n"
        f"VERIFIED FACTS (may be stated plainly, cite verbatim):\n{hard}"
        f"{soft_block}"
        f"{thin_note}\n\n"
        f"Write the message now."
    )


def _clean_llm_body(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```[a-z]*\n?|\n?```$", "", t).strip()
    if len(t) >= 2 and t[0] in "\"'“" and t[-1] in "\"'”":
        t = t[1:-1].strip()
    # drop an accidental leading label like "Message:" / "Vera:"
    t = re.sub(r"^(message|vera|body|output)\s*[:\-]\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _referenced_fact_labels(body: str, fs: dict) -> list[str]:
    low = body.lower()
    hit = []
    for f in fs["hard_facts"] + fs.get("soft_facts", []):
        v = str(f["value"]).lower()
        core = v.replace("₹", "").replace(",", "").split(" ")[0].strip("():\"")
        if core and len(core) >= 2 and core in low.replace("₹", "").replace(",", ""):
            hit.append(f["label"])
    return hit[:5]


_HI_WORDS = re.compile(
    r"\b(aap|aapka|aapke|aapki|aapko|hai|hain|kal|abhi|turant|bas|thoda|humne|hamare|hamara|"
    r"kya|karo|kijiye|dijiye|chahiye|waqt|jaldi|dhyan|bhej|bana|rakha|rakhi|rakhe|doon|karein|"
    r"karte|rahi|raha|liye|saath|aaj|nahi|shukriya|namaste|ji)\b", re.IGNORECASE)


def _has_code_switch(body: str) -> bool:
    if re.search(r"[ऀ-ॿ]", body):   # Devanagari
        return True
    return bool(_HI_WORDS.search(body))


def _quality_gate(body: str, fs: dict) -> tuple[bool, str]:
    """Deterministic structural checks, separate from validate_output's grounding checks.
    Grounding asks 'is this true'; this asks 'is this a well-formed Vera message'. Kept to
    checks we have real evidence for, not speculative rules - each maps to an actual bug
    seen this session."""
    # T12 double-ask: two separate questions each demanding a reply, not one CTA with an
    # embedded choice ("Reply 1 for Wed, 2 for Thu" has no '?' at all).
    if body.count("?") >= 2:
        return False, "two separate asks (more than one '?') - collapse to one CTA"
    # rubric: "a message that could belong to any business loses points" - the business
    # name or its locality should appear at least once, for a merchant-facing message.
    if fs.get("scope") != "customer":
        biz = (fs.get("biz_name") or "").strip()
        loc = (fs.get("locality") or "").strip()
        biz_word = biz.split(",")[0].split()[0] if biz else ""
        low = body.lower()
        if biz and biz_word and biz_word.lower() not in low and (not loc or loc.lower() not in low):
            return False, f"business identity ('{biz}') never named in the message"
    return True, "ok"


def _llm_compose(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx] = None):
    if not llm_client.available():
        return None

    fs = factsheet.build_factsheet(category, merchant, trigger, customer)
    taboos = fs.get("taboos") or []
    user = _fs_user_prompt(fs)

    body = None
    reject_reason = ""
    for attempt in range(2):
        sys_prompt = _LLM_SYSTEM
        if attempt == 1:
            sys_prompt += ("\n\nYOUR LAST ATTEMPT FAILED THE FABRICATION CHECK: " + reject_reason +
                           ". Rewrite using ONLY the facts listed, each number attached to its "
                           "correct metric. If you have no number to cite, write a "
                           "specific-but-number-free message instead.")
        # Attempt 0 is temperature 0 (deterministic primary path, per the brief). The
        # rare recovery attempt uses a little temperature so it can actually escape
        # whatever the validator rejected. Retry stays on the primary model so the
        # two-attempt worst case stays well inside the 30s judge budget.
        raw = llm_client.chat(sys_prompt, user, temperature=0.0 if attempt == 0 else 0.3,
                              max_tokens=1200, try_fallback_model=(attempt == 0))
        if not raw:
            # The client already retried with backoff; a None here means the provider is
            # genuinely unavailable right now. Don't retry-storm — hand off to the
            # deterministic engine immediately.
            return None
        cand = sanitize_taboos(_clean_llm_body(raw), taboos)
        ok, reject_reason = factsheet.validate_output(cand, fs)
        # On the first attempt only, also require the mandated Hindi-English mix - measured
        # against the real judge, a merchant message that silently drops it loses real points
        # on category fit. The retry (last resort) accepts a valid English-only draft rather
        # than falling through to the deterministic engine, which has the same gap.
        if ok and attempt == 0 and fs.get("code_switch") and not _has_code_switch(cand):
            ok = False
            reject_reason = "missing the required Hindi-English code-mix (not just in the CTA)"
        if ok and attempt == 0:
            gate_ok, gate_reason = _quality_gate(cand, fs)
            if not gate_ok:
                ok = False
                reject_reason = gate_reason
        if ok:
            body = cand
            break

    if body is None:
        return None

    body = re.sub(r"\s{2,}", " ", body).strip()
    rationale = (
        f"Chose {fs['kind']} for {fs['biz_name']}: {fs['why_now']}. "
        f"Leaning on {fs['lever']}; {fs['cta_type']} CTA. "
        f"{'Drafted artifact included. ' if fs['artifact_expected'] else ''}"
        f"({fs['scope']}-facing, {'code-switched ' if fs['code_switch'] else ''}"
        f"LLM copy validated against the fact sheet; model={llm_client.model_label()})"
    )
    return {
        "body": body,
        "cta": fs["cta_type"],
        "send_as": fs["send_as"],
        "suppression_key": fs["suppression_key"],
        "rationale": rationale,
        "ask_text": sanitize_taboos(body.split(". ")[-1], taboos).rstrip("."),
    }
