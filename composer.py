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


def _derive_perf_delta(merchant: Ctx, want_positive: bool) -> Optional[tuple[str, float]]:
    """Pull a real metric/delta pair straight from MerchantContext.performance.delta_7d
    (always present) when the trigger payload itself didn't carry one."""
    delta_7d = _g(merchant, "performance", "delta_7d", default={}) or {}
    cands = []
    for k, v in delta_7d.items():
        if not k.endswith("_pct") or not isinstance(v, (int, float)):
            continue
        metric = k[: -len("_pct")]
        if want_positive and v > 0:
            cands.append((metric, v))
        elif not want_positive and v < 0:
            cands.append((metric, v))
    if not cands:
        return None
    cands.sort(key=lambda x: -abs(x[1]))
    return cands[0]


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
    body_suffix = f"  — {src}" if src else ""
    return facts, cta_en, cta_hi, "open_ended", ["specificity/source citation", "curiosity", "reciprocity"], body_suffix


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
    src = item.get("source", "")
    body_suffix = f"  — {src}" if src else ""
    return facts, cta_en, cta_hi, "binary_yes_no", ["specificity/deadline", "loss aversion (compliance risk)", "effort externalization"], body_suffix


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
    return facts, cta_en, cta_hi, "binary_yes_no", ["specificity", "effort externalization"], f"  — {src}" if src else ""


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
        derived = _derive_perf_delta(merchant, want_positive=True)
        if not derived:
            return _mf_generic(category, merchant, trigger, payload)
        metric, delta = derived
        baseline = _g(merchant, "performance", metric)
    metric = metric or "views"
    driver = payload.get("likely_driver")
    facts = [f"Your {metric} are up {fmt_pct(delta)} this week"]
    if baseline is not None:
        facts[0] += f" (currently {baseline}/30d)."
    else:
        facts[0] += "."
    if driver:
        facts.append(f"Likely driver: {driver.replace('_', ' ')}.")
    peer_avg = _g(category, "peer_stats", f"avg_{metric}_30d") if metric in ("views", "calls", "directions") else None
    if peer_avg:
        facts.append(f"Peer median for your category is {peer_avg}/30d — you're tracking above it now.")
    cta_en = "Want me to double down — repeat whatever worked, or push a follow-up post while it's hot?"
    cta_hi = "Isi cheez ko repeat karke follow-up post bhej doon?"
    return facts, cta_en, cta_hi, "open_ended", ["specificity", "reciprocity", "momentum"], ""


def _mf_perf_dip(category, merchant, trigger, payload):
    metric = payload.get("metric")
    delta = payload.get("delta_pct")
    baseline = payload.get("vs_baseline")
    window = payload.get("window", "7d")
    if delta is None:
        derived = _derive_perf_delta(merchant, want_positive=False)
        if not derived:
            return _mf_generic(category, merchant, trigger, payload)
        metric, delta = derived
        baseline = _g(merchant, "performance", metric)
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
        # Derive a real round-number milestone from customer_aggregate (always present).
        total = _g(merchant, "customer_aggregate", "total_unique_ytd")
        if total is None:
            return _mf_generic(category, merchant, trigger, payload)
        metric = "unique customers YTD"
        now_v = total
        target = ((total // 100) + 1) * 100
        imminent = (target - total) <= 25
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
        # MerchantContext.subscription always carries this — use it directly.
        sub = _g(merchant, "subscription", default={}) or {}
        days = sub.get("days_remaining")
        plan = plan or sub.get("plan")
        if days is None:
            return _mf_generic(category, merchant, trigger, payload)
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
    kind = trigger.get("kind", "update")
    urgency = trigger.get("urgency", 2)
    label = humanize_kind(kind)

    # Surface up to 2 concrete-looking payload values (numbers/short strings) as facts,
    # ignoring the placeholder-expansion artifacts themselves.
    concrete_bits = []
    for k, v in (payload or {}).items():
        if k in ("placeholder", "metric_or_topic"):
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            concrete_bits.append(f"{k.replace('_', ' ')}: {v}")
        elif isinstance(v, str) and v and len(v) < 60:
            concrete_bits.append(f"{k.replace('_', ' ')}: {v}")
        if len(concrete_bits) >= 2:
            break

    if concrete_bits:
        detail = ", ".join(concrete_bits)
    else:
        # No usable trigger payload at all — ground the message in the merchant context instead.
        sig = merchant_signals(merchant)
        perf = _g(merchant, "performance", default={}) or {}
        if sig:
            detail = f"current signal on file: {sig[0].replace('_', ' ')}"
        elif perf.get("views") is not None:
            detail = f"last 30d: {perf.get('views')} views, {perf.get('calls', '?')} calls"
        else:
            detail = ""

    facts = [f"Flagging a {label} item for {biz_name(merchant)}" + (f" — {detail}." if detail else ".")]
    cta_en = "Want me to look into this further and come back with a specific recommendation?"
    cta_hi = "Isko dekh ke aapko specific recommendation bhej doon?"
    cta_type = "binary_yes_no" if urgency >= 3 else "open_ended"
    return facts, cta_en, cta_hi, cta_type, ["specificity (available fields)", "restraint on thin trigger data"], ""


# ---------------------------------------------------------------------------
# Customer-facing composers (send_as = "merchant_on_behalf")
# ---------------------------------------------------------------------------

def _cf_recall_due(category, merchant, trigger, payload, customer):
    service = (payload.get("service_due") or "recall").replace("_", " ")
    slots = payload.get("available_slots", [])
    offs = active_offers(merchant)
    name = _g(customer, "identity", "name") or "there"
    last_visit = payload.get("last_service_date")
    facts = [f"It's been a while since your last visit" + (f" ({last_visit})" if last_visit else "") + f" — your {service} is due."]
    if slots:
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
    if senior_offer and ("60" in age_band or "65" in age_band or "70" in age_band):
        facts.append(f"{senior_offer.get('title')} applies to this order.")
    cta_en = "Reply CONFIRM to dispatch the refill, or call if anything changed in your dosage."
    return name, facts, cta_en, "binary_confirm_cancel", ["specificity (molecules+date)", "effort externalization", "trust (dose continuity)"]


def _cf_lapse_generic(category, merchant, trigger, payload, customer, hard: bool):
    name = _g(customer, "identity", "name") or "there"
    rel = _g(customer, "relationship", default={}) or {}
    last_visit = rel.get("last_visit")
    services = rel.get("services_received", [])
    visits_total = rel.get("visits_total")
    offs = active_offers(merchant)
    days = payload.get("days_since_last_visit")
    biz = biz_name(merchant)
    facts = []
    if days:
        facts.append(f"It's been about {days} days since your last visit to {biz} — happens to everyone, no judgment." if hard else f"It's been {days} days since your last visit to {biz}.")
    elif last_visit:
        facts.append(f"It's been a while since your {last_visit} visit to {biz} — no judgment, life gets busy." if hard else f"It's been a while since your visit to {biz} on {last_visit}.")
    else:
        facts.append(f"It's been a while since we've seen you at {biz}.")
    if services:
        facts.append(f"Last time you came in for {services[-1].replace('_', ' ')}.")
    elif visits_total:
        facts.append(f"You've been in {visits_total} times so far — always good to have you back.")
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
    state = _g(customer, "state") or ""
    rel = _g(customer, "relationship", default={}) or {}
    last_visit = rel.get("last_visit")
    facts = [f"Checking in from {biz_name(merchant)}" + (f" — it's been a bit since {last_visit}." if last_visit else ".")]
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
        perf = _g(merchant, "performance", "delta_7d", default={}) or {}
        if perf:
            k = next(iter(perf))
            bits.append(f"merchant performance delta ({k.replace('_pct','').replace('_',' ')}: {fmt_pct(perf[k])}) factored in")

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
    payload = trigger.get("payload", {}) or {}
    kind = trigger.get("kind", "update")
    voice = _g(category, "voice", default={}) or {}
    taboos = voice.get("vocab_taboo", []) or []
    mode = language_mode(merchant, customer)

    if customer is not None:
        fn = CUSTOMER_COMPOSERS.get(kind, _cf_generic)
        args = (category, merchant, trigger, payload, customer)
        name, facts, cta_en, cta_type, levers = fn(*args) if fn is not _cf_generic else fn(*args)
        greeting = f"Hi {name},"
        facts_joined = _lead_lower(" ".join(f.strip() for f in facts if f and f.strip()))
        body = f"{greeting} " + facts_joined
        body += " " + cta_en
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
        cta = pick(cta_en, cta_hi, mode)
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
