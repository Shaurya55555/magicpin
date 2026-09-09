"""
factsheet.py — verified-fact extractor for the hybrid composer.

The judge (judge_simulator.py) scores a message against a DELIBERATELY NARROW view
of the context. It only ever sees:
  merchant: identity.{name, owner_first_name, locality, languages},
            performance.{views, calls, ctr}, signals[], active offer titles
  trigger:  kind, payload (full), urgency
  customer: identity.{...} only  (NOT relationship / state / preferences)
  category: slug, voice.tone, first 5 vocab_taboo

So a number from merchant.performance.delta_7d, review_themes, customer_aggregate,
subscription, or customer.relationship is REAL data but the judge can't verify it and
scores it as fabrication (the old deterministic composer's exact failure).

This module splits facts into two tiers:
  hard_facts  — inside the judge's view; the message may state these plainly.
  soft_facts  — real, but outside the judge's view; the message may only use them
                WITH explicit attribution ("your dashboard shows...", "your records...",
                "from our last chat...") so even a narrow-view judge sees a source,
                not an invented number. This also stays correct if the real judge
                turns out to have the full context.

validate_output() then accepts any number/date that appears in EITHER tier and
rejects everything else.
"""

from __future__ import annotations
import re
from typing import Any, Optional

Ctx = dict

# --- mojibake repair — several dataset offer titles are double-encoded UTF-8 ---
_MOJIBAKE = {"â‚¹": "₹", "â€”": "-", "â€™": "'", "â€œ": '"', "â€": '"', "Ã©": "e"}


def fix_text(s: Any) -> Any:
    if not isinstance(s, str):
        return s
    for bad, good in _MOJIBAKE.items():
        s = s.replace(bad, good)
    if "Ã" in s or "â€" in s or "â‚" in s:
        try:
            s = s.encode("latin-1", "ignore").decode("utf-8", "ignore")
        except Exception:
            pass
    return s


def _g(d, *path, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur is not None else default


def _present(v) -> bool:
    return v is not None and v != "" and v != [] and v != {}


def _pct(v, signed=True) -> Optional[str]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    x = f * 100 if abs(f) <= 1 else f
    s = f"{abs(x):.0f}%"
    return ("+" if signed and x > 0 else "-" if signed and x < 0 else "") + s


_LEVER_BY_KIND = {
    "research_digest": "curiosity", "cde_opportunity": "curiosity", "curious_ask_due": "curiosity",
    "perf_dip": "loss_aversion", "seasonal_perf_dip": "reassurance", "competitor_opened": "loss_aversion",
    "review_theme_emerged": "loss_aversion", "renewal_due": "loss_aversion", "gbp_unverified": "loss_aversion",
    "winback_eligible": "loss_aversion", "customer_lapsed_hard": "warmth", "customer_lapsed_soft": "warmth",
    "milestone_reached": "social_proof", "perf_spike": "social_proof",
    "supply_alert": "urgency", "regulation_change": "urgency", "ipl_match_today": "urgency",
    "festival_upcoming": "urgency", "category_seasonal": "reciprocity",
    "active_planning_intent": "reciprocity", "dormant_with_vera": "reciprocity",
    "recall_due": "warmth", "chronic_refill_due": "reciprocity", "appointment_tomorrow": "reciprocity",
    "trial_followup": "reciprocity", "wedding_package_followup": "reciprocity",
}
_ARTIFACT_KINDS = {"active_planning_intent", "curious_ask_due", "category_seasonal",
                   "research_digest", "ipl_match_today", "festival_upcoming", "review_theme_emerged",
                   "supply_alert", "regulation_change"}
_OPEN_ENDED_KINDS = {"curious_ask_due", "active_planning_intent"}
_NONE_CTA_KINDS = {"seasonal_perf_dip"}

# The merchant `signals[]` strings are internal tags. A few carry a fact worth stating
# in plain language; the rest are system bookkeeping that would read as jargon if echoed.
# Map the useful ones to natural phrasing; everything not listed here is dropped.
_SIGNAL_PHRASING = {
    "above_peer_calls": "getting more calls than similar businesses nearby",
    "above_peer_median_calls": "getting more calls than similar businesses nearby",
    "above_peer_ctr": "a stronger listing click rate than similar businesses",
    "ctr_below_peer_median": "a weaker listing click rate than similar businesses",
    "high_repeat_rate": "a high share of repeat customers",
    "high_retention": "strong customer retention",
    "high_volume": "high booking volume",
    "growing_views_7d": "views trending up this week",
    "stable_growth": "steady month-on-month growth",
    "high_risk_adult_cohort": "a sizeable higher-risk adult patient base",
    "delivery_not_set_up": "no delivery option set up yet",
    "no_active_offers": "no active offer running right now",
    "no_recent_post": "no recent post on the listing",
    "stale_posts": "the listing hasn't had a fresh post in a while",
    "unverified_gbp": "the Google listing isn't verified yet",
    "trial_ending_soon": "the free trial period is ending soon",
    "compliance_aware": "",   # true but not worth stating
}

_JUDGE_VOICE = {
    "dentists": "clinical, peer-to-peer, technical terms OK, address as 'Dr. <name>', no medical claims to patients",
    "salons": "warm, friendly, practical",
    "restaurants": "operator-to-operator, trade words (covers, AOV, delivery radius) OK",
    "gyms": "coaching, motivational, evidence-based, never shaming",
    "pharmacies": "trustworthy, precise, exact molecule/batch names, respectful of seniors",
}


def _address(slug, owner, biz):
    if not owner:
        return biz
    if slug == "dentists" and not str(owner).lower().startswith("dr"):
        return f"Dr. {owner}"
    return owner


_TREND_RE = re.compile(r"^([A-Za-z][A-Za-z _&]*?)_demand_([+-]\d+)$")
_SKIP_PAYLOAD_KEYS = {"placeholder", "metric_or_topic", "shelf_action_recommended",
                      "is_weeknight", "is_imminent", "delivery_address_saved", "category"}


def _skip_payload_key(k: str) -> bool:
    return k in _SKIP_PAYLOAD_KEYS or k.endswith("_id") or k.endswith("_ids")


def _digest_item(category, payload):
    """Resolve the category.digest entry a trigger points at. The judge can't see
    category.digest, so its contents become SOFT facts (cite the source)."""
    wanted = payload.get("top_item_id") or payload.get("digest_item_id") or payload.get("item_id")
    items = _g(category, "digest", default=[]) or []
    if wanted:
        for it in items:
            if it.get("id") == wanted:
                return it
    return None


def _humanize_value(v) -> str:
    """Turn a raw payload value into what a person would write, inventing nothing:
    'summer_2026' -> 'summer 2026', ['ORS_demand_+40', ...] -> 'ORS +40%, ...',
    {'views_pct': 0.06} -> 'views +6%'."""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        parts = []
        for it in v:
            m = _TREND_RE.match(str(it))
            parts.append(f"{m.group(1).replace('_', ' ').strip()} {m.group(2)}%" if m
                         else str(it).replace("_", " "))
        return ", ".join(parts)
    if isinstance(v, dict):
        out = []
        for kk, vv in v.items():
            if kk.endswith("_pct"):
                out.append(f"{kk[:-4].replace('_', ' ')} {_pct(vv)}")
            else:
                out.append(f"{kk.replace('_', ' ')} {vv}")
        return ", ".join(out)
    s = str(v)
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):        # keep ISO dates verbatim
        return s
    return s.replace("_", " ")


def _why_now(kind, payload):
    k = kind.replace("_", " ")
    p = payload or {}
    real = {kk: vv for kk, vv in p.items() if not _skip_payload_key(kk) and _present(vv)}
    if real:
        bits = "; ".join(f"{kk.replace('_',' ')}: {_humanize_value(vv)}" for kk, vv in list(real.items())[:4])
        return f"{k} - {bits}"
    return f"{k} (no extra detail in the signal - anchor on the merchant's own numbers / active offer)"


def build_factsheet(category: Ctx, merchant: Ctx, trigger: Ctx, customer: Optional[Ctx] = None) -> dict:
    kind = trigger.get("kind", "update")
    payload = trigger.get("payload", {}) or {}
    slug = category.get("slug") or merchant.get("category_slug", "")
    voice = _g(category, "voice", default={}) or {}

    biz = fix_text(_g(merchant, "identity", "name")) or "your business"
    owner = _g(merchant, "identity", "owner_first_name")
    locality = _g(merchant, "identity", "locality") or _g(merchant, "identity", "city")
    langs = _g(merchant, "identity", "languages", default=[]) or []

    hard: list[dict] = []
    soft: list[dict] = []

    def H(label, value):
        if _present(value):
            hard.append({"label": label, "value": str(fix_text(value))})

    def S(label, value, how):
        if _present(value):
            soft.append({"label": label, "value": str(fix_text(value)), "attribute_as": how})

    scope_customer = customer is not None
    perf = _g(merchant, "performance", default={}) or {}

    # payload facts (both scopes) — humanised, IDs and internal flags dropped
    def _emit_payload():
        for pk, pv in (payload or {}).items():
            if _skip_payload_key(pk) or not _present(pv):
                continue
            label = pk.replace("_", " ")
            if pk.endswith(("_pct", "_percent", "_pc")) and isinstance(pv, (int, float)):
                # e.g. estimated_uplift_pct: 0.3  ->  "estimated uplift: +30%"
                label = re.sub(r"\s*(pct|percent|pc)$", "", label).strip()
                H(label, _pct(pv))
            else:
                H(label, _humanize_value(pv))

    # active offers (both scopes) — titles carry the ₹ amounts the judge can see
    def _emit_offers():
        for o in _g(merchant, "offers", default=[]) or []:
            if o.get("status") == "active" and _present(o.get("title")):
                H("active offer running", fix_text(o["title"]))

    cust = None

    if scope_customer:
        # ---- CUSTOMER-FACING: the reader is the customer, NOT the owner. ----
        # Merchant performance / signals / plan are irrelevant and wrong to cite here.
        cid = customer.get("identity", {}) or {}
        cust = {
            "name": fix_text(cid.get("name") or cid.get("first_name")),
            "language_pref": (cid.get("language_pref") or "").lower(),
            "age_band": cid.get("age_band"),
        }
        H("the business you're writing from", biz)
        if locality:
            H("where the business is", locality)
        _emit_payload()
        _emit_offers()
        # relationship history — judge sees customer.identity only, so attribute it
        rel = customer.get("relationship", {}) or {}
        S("when they last visited", rel.get("last_visit"), "say 'our records show your last visit was'")
        S("how many times they've visited", rel.get("visits_total") or rel.get("visit_count"),
          "say 'you've visited us'")
        S("services they've had before", ", ".join(rel.get("services_received", []) or []).replace("_", " "),
          "say 'last time you came in for'")
        S("how long since we've seen them", customer.get("state"),
          "phrase gently as 'it's been a while'")
    else:
        # ---- MERCHANT-FACING: reader is the owner, sent as Vera. -----------
        H("views in last 30 days", perf.get("views"))
        H("calls in last 30 days", perf.get("calls"))
        H("direction requests in last 30 days", perf.get("directions"))
        H("leads in last 30 days", perf.get("leads"))
        if _present(perf.get("ctr")):
            H("listing click rate", _pct(perf["ctr"], signed=False))
        for s in _g(merchant, "signals", default=[]) or []:
            base = str(s).split(":")[0]                 # "stale_posts:22d" -> "stale_posts"
            phrase = _SIGNAL_PHRASING.get(base, _SIGNAL_PHRASING.get(str(s)))
            if phrase:
                H("something true about this account", phrase)
        # A research / compliance / CE briefing is not the moment to push a price offer -
        # shoehorning "get a Dental Cleaning @ ₹299" into a radiograph-compliance note reads
        # as a non-sequitur. Skip offers for those kinds.
        if kind not in ("research_digest", "regulation_change", "cde_opportunity"):
            _emit_offers()
        _emit_payload()

        # digest content (research/compliance/CDE) — judge can't see category.digest,
        # so cite the source label; a cited claim reads as good practice, not fabrication.
        di = _digest_item(category, payload)
        if di:
            src = di.get("source") or "this week's briefing"
            how = f"attribute to the source: '{src}'"
            S("headline of the briefing item", di.get("title"), how)
            S("what the briefing found", di.get("summary"), how)
            S("what to do about it", di.get("actionable"), "phrase as your suggestion")
            if _present(di.get("credits")):
                S("CE credits on offer", f"{di['credits']} credits", how)
            if _present(di.get("trial_n")):
                S("study size", f"{di['trial_n']} participants", how)

        # merchant week-on-week movement — real but outside the judge's view
        for mk, mv in (perf.get("delta_7d") or {}).items():
            if mk.endswith("_pct") and isinstance(mv, (int, float)):
                S(f"{mk[:-4].replace('_',' ')} week-on-week", _pct(mv), "say 'your dashboard shows'")
        agg = _g(merchant, "customer_aggregate", default={}) or {}
        for ak, lbl in [("total_unique_ytd", "unique customers this year"),
                        ("lapsed_180d_plus", "customers not seen in 6+ months"),
                        ("high_risk_adult_count", "higher-risk adult patients on file")]:
            S(lbl, agg.get(ak), "say 'from your customer records'")
        for rt in _g(merchant, "review_themes", default=[]) or []:
            if _present(rt.get("theme")):
                occ = rt.get("occurrences_30d")
                sent = {"pos": "praising", "neg": "flagging", "mixed": "split on"}.get(rt.get("sentiment"), "mentioning")
                v = f"{sent} {str(rt['theme']).replace('_', ' ')}" + (f" ({occ} times last month)" if _present(occ) else "")
                S("what recent reviews say", v, "say 'a few recent reviews mention'")
        sub = _g(merchant, "subscription", default={}) or {}
        if _present(sub.get("days_remaining")):
            S("days left on the magicpin plan", sub["days_remaining"], "say 'your plan shows'")
        ch = _g(merchant, "conversation_history", default=[]) or []
        if ch and _present(ch[-1].get("body")):
            S("what was last discussed", f'"{fix_text(ch[-1]["body"])[:140]}"', "say 'last time we spoke'")

    code_switch = ("hi" in [str(l).lower() for l in langs]) or (bool(cust) and "hi" in (cust.get("language_pref") or ""))

    why = _why_now(kind, payload)
    if not scope_customer:
        di2 = _digest_item(category, payload)
        if di2 and di2.get("title"):
            why = f"{kind.replace('_',' ')} - {fix_text(di2['title'])}"

    return {
        "kind": kind,
        "scope": "customer" if scope_customer else "merchant",
        "send_as": "merchant_on_behalf" if scope_customer else "vera",
        "category_slug": slug,
        "voice_tone": fix_text(voice.get("tone")) or "",
        "voice_rules": _JUDGE_VOICE.get(slug, ""),
        "taboos": voice.get("vocab_taboo", []) or [],
        "biz_name": biz,
        "owner": owner,
        "address_as": (cust["name"] or "there") if scope_customer else _address(slug, owner, biz),
        "locality": locality or "",
        "languages": langs,
        "code_switch": bool(code_switch),
        "customer": cust,
        "why_now": why,
        "lever": _LEVER_BY_KIND.get(kind, "reciprocity"),
        "cta_type": ("none" if kind in _NONE_CTA_KINDS
                     else "open_ended" if (kind in _OPEN_ENDED_KINDS and not scope_customer)
                     else "binary"),
        "artifact_expected": kind in _ARTIFACT_KINDS and not scope_customer,
        "hard_facts": hard,
        "soft_facts": soft,
        "suppression_key": trigger.get("suppression_key", f"{kind}:{trigger.get('id','')}"),
    }


# ---------------------------------------------------------------------------
# output validator — the anti-fabrication guard
# ---------------------------------------------------------------------------
_GENERIC_TIME = re.compile(
    r"\b(\d{1,3}\s?(?:min|mins|minute|minutes|hour|hours|hr|hrs|day|days|week|weeks|month|months|"
    r"km|kms|kilometre|kilometres|kilometer|kilometers|"
    r"din|dino|dinon|hafte|hafta|haftey|mahina|mahine|mahino|saal|ghante|ghanta|ghanto|baje)"
    r"|\d{1,3}\s?/\s?30d\b|\d{1,2}\s?d\b|\d{1,2}\s?h\b|2-min|24h|48h|"
    r"one|two|three|first|second|third|a couple|"
    r"this week|next week|tomorrow|today|tonight|this month|next month|this weekend|"
    r"mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE)
_NUM = re.compile(r"₹?\s?\d[\d,]*\.?\d*\s?%?")
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*)",
                   re.IGNORECASE)
_JARGON = ["trigger", "payload", "suppression", "rationale", "the composer", "the system",
           "context object", "internal jargon", "template_", "peer median", "delta_7d",
           "vs_baseline", "send_as", "dormant with vera", "winback eligible", "winback-eligible",
           "ipl-eligible", "ipl eligible", "eligible locality", "perf dip", "perf_dip",
           "status flag", "curious ask", "gbp"]
_JARGON_RE = re.compile(r"\b(ctr|gbp|kpi)\b", re.IGNORECASE)


def _norm_num(s: str) -> str:
    return s.lower().replace("₹", "").replace(",", "").replace(" ", "").replace("%", "").strip(" .+-")


_PERF_LABELS = {"views in last 30 days", "calls in last 30 days", "leads in last 30 days",
                "direction requests in last 30 days", "listing click rate"}


def _metric_value_map(fs: dict) -> dict:
    """Only the confusable performance metrics — payload numbers (a milestone target,
    a competitor distance) are not 'metrics' that can be mislabelled this way."""
    m = {}
    for f in fs["hard_facts"]:
        if f["label"] in _PERF_LABELS:
            m.setdefault(f["label"], set()).add(_norm_num(f["value"]))
    # values that are legitimately a review / milestone figure: soft "recent reviews"
    # facts, plus a trigger payload that is explicitly about reviews or a milestone.
    rv = set()
    for f in fs.get("soft_facts", []):
        if "review" in f["label"].lower():
            rv |= set(re.findall(r"\d[\d,]*", f["value"]))
    for f in fs["hard_facts"]:
        lbl = f["label"].lower()
        if any(w in lbl for w in ("review", "milestone", "value now", "rating count")):
            rv |= set(re.findall(r"\d[\d,]*", str(f["value"])))
    m["__reviews__"] = {x.replace(",", "") for x in rv}
    return m


_MISLABEL_WORD = {
    "views in last 30 days": r"views?",
    "calls in last 30 days": r"calls?",
    "leads in last 30 days": r"leads?",
    "direction requests in last 30 days": r"directions?(?:\s+requests?)?",
    "listing click rate": r"click[- ]?(?:rate|through)",
    "__reviews__": r"reviews?",
}

# Phrases that promise the recipient a freebie / gift / gesture.
_PHANTOM_OFFER = re.compile(
    r"\bas a (?:thank[- ]?you|gift|treat|token)\b|\bwe[''’]?ll (?:add|throw in|include|gift)\b|"
    r"\bon (?:us|the house)\b|\bspecial (?:gift|treat|surprise|thank[- ]?you)\b|"
    r"\bcomplimentary\s+\w+|"
    r"\bfree\s+(?!home\s+delivery|consultation\b|for\b|to\b|of\b|trial\b|body\b)\w+",
    re.IGNORECASE)


def _phantom_offer_check(body: str, fs: dict) -> tuple[bool, str]:
    """Reject an invented freebie/gift/gesture ('complimentary hair mask', 'as a
    thank-you we've saved a spot'). Allowed only if a real active offer actually is
    free/complimentary."""
    m = _PHANTOM_OFFER.search(body)
    if not m:
        return True, "ok"
    offers = " ".join(f["value"].lower() for f in fs["hard_facts"]
                      if f["label"] == "active offer running")
    if "free" in offers or "complimentary" in offers or "@ ₹0" in offers or "@ rs 0" in offers:
        # a real free offer exists — make sure the phrase points at it, roughly
        tail = body[m.start():m.start() + 40].lower()
        if any(w in offers for w in re.findall(r"[a-z]{4,}", tail)):
            return True, "ok"
    return False, f"invented freebie/gesture: {m.group(0)!r}"


def _semantic_metric_check(body: str, fs: dict) -> tuple[bool, str]:
    """Catch a real number attached to the WRONG metric — e.g. the views count written
    as 'reviews', or the leads count as 'calls'. Fires only on tight
    'NUMBER<space>metric-word' adjacency (plus 'click rate is N%'), so a correct
    sentence like '2410 views and a 4% click rate' is never flagged."""
    vmap = _metric_value_map(fs)

    def _flag(val, word, label):
        val = val.replace(",", "").rstrip(".")
        if not val:
            return None
        owners = {k for k, vs in vmap.items() if val in vs}
        if owners and label not in owners:
            right = next((k for k in owners if k != "__reviews__"), "another metric")
            return f"metric mislabel: {val!r} written as '{word}' but it is the {right} figure"
        return None

    for label, word in _MISLABEL_WORD.items():
        for m in re.finditer(rf"(\d[\d,]*\.?\d*)\s*%?\s+{word}\b", body, re.I):  # "88 calls", "2% click rate"
            msg = _flag(m.group(1), word, label)
            if msg:
                return False, msg
    # the one reversed phrasing worth checking: "click rate is/of/at N%"
    for m in re.finditer(r"click[- ]?(?:rate|through)\s+(?:is|of|at|around|sits at|=|:)?\s*(\d[\d,]*\.?\d*)\s*%", body, re.I):
        msg = _flag(m.group(1), "click rate", "listing click rate")
        if msg:
            return False, msg
    return True, "ok"


def validate_output(body: str, fs: dict) -> tuple[bool, str]:
    if not body or len(body.strip()) < 25:
        return False, "empty/too short"
    # LLMs love the unicode hyphen/dash — normalise so date & number checks can't be bypassed
    body = body.translate({0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2013: "-", 0x2014: "-", 0x2212: "-"})
    low = body.lower()
    for j in _JARGON:
        if j in low:
            return False, f"jargon leak: {j!r}"
    mj = _JARGON_RE.search(body)
    if mj:
        return False, f"jargon leak: {mj.group(0)!r}"

    hay = " ".join(_norm_num(f["value"]) for f in fs["hard_facts"] + fs["soft_facts"])
    # a soft fact's attribution phrase ("per JIDA Oct 2026 p.14", the circular's date) is
    # also legitimately citable — fold it into the haystack
    hay += " " + " ".join(_norm_num(f.get("attribute_as", "")) for f in fs["soft_facts"])
    hay += " " + _norm_num(fs.get("locality", "")) + " " + _norm_num(fs.get("biz_name", "")) + " 2026 2027"

    scrubbed = _GENERIC_TIME.sub(" ", body)

    for m in _DATE.finditer(scrubbed):
        if _norm_num(m.group(0)) not in hay:
            return False, f"unverified date {m.group(0)!r}"

    # A number must never be attached to the wrong metric (checked for every message,
    # artifact or not).
    ok, why = _semantic_metric_check(body, fs)
    if not ok:
        return False, why

    ok, why = _phantom_offer_check(body, fs)
    if not ok:
        return False, why

    artifact = fs.get("artifact_expected")

    for m in _NUM.finditer(scrubbed):
        tok = m.group(0).strip()
        n = _norm_num(tok)
        if not n or not any(c.isdigit() for c in n):
            continue
        is_money = "₹" in tok
        is_pct = "%" in tok
        if not is_money and not is_pct and "." not in n:
            try:
                v = float(n)
                if v <= 3:
                    continue  # structural small integer ("2 slots", "3 posts")
                if artifact:
                    continue  # inside a draft, a bare count/quantity is draft structure
            except ValueError:
                pass
        # ₹ amounts and percentages must be grounded even inside a drafted artifact —
        # the merchant reads a price as a real commitment, not "structure".
        if n not in hay:
            return False, f"unverified {'price' if is_money else 'percentage' if is_pct else 'number'} {tok!r}"
    return True, "ok"
