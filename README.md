# Vera, but better — magicpin AI Challenge submission

**Team:** Shaurya Bajpai — contact: bajpaishaurya2911@gmail.com

**Live bot:** https://magicpin-vera-bot.vercel.app · **Repo:** https://github.com/Shaurya55555/magicpin

## Files

| File | What it is |
|---|---|
| `composer.py` | Core message-composition logic — the `compose(category, merchant, trigger, customer=None)` function required by §7.3. |
| `bot.py` | FastAPI service implementing the 5-endpoint HTTP contract from `challenge-testing-brief.md` (`/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata`, plus an optional `/v1/teardown`). |
| `conversation_handlers.py` | Optional §7.4 deliverable — a deterministic multi-turn `respond(state, message)` state machine used by `/v1/reply`. |
| `storage.py` | State store used by `bot.py` — Redis (Upstash REST API) in production, in-memory fallback for local dev. See "Deployment & state" below. |
| `api/index.py`, `vercel.json` | Vercel Python deployment entrypoint/config. |
| `submission.jsonl` | One composed message per canonical test pair in `dataset/test_pairs.json` (30 lines), each with `test_id, body, cta, send_as, suppression_key, rationale`. |
| `requirements.txt`, `Dockerfile` | Deploy scaffolding (container path, if not using Vercel). |

## Approach

**We chose a deterministic, rule-based composer instead of an LLM call.** Every message is built by a per-trigger-kind template function that reads only the four context payloads (category, merchant, trigger, customer) and assembles a body from fields that are actually present. There is no model in the loop for `/v1/context` or `/v1/tick`.

The reasoning:

- **Anti-fabrication is the single most heavily weighted failure mode in the rubric.** An LLM, even a well-prompted one, can paraphrase its way into inventing a number, a date, or a claim that isn't in the payload. A rule engine that only ever interpolates fields it read from the context objects structurally cannot do that — if a fact isn't in the payload, the composer either finds an equivalent fact elsewhere in the same merchant/category/customer context, or falls back to a grounded-but-generic message. It never guesses.
- **Determinism and latency.** The brief requires responses within 30s and rewards consistent behavior across the warmup/test/replay phases. A template engine responds in single-digit milliseconds and gives byte-identical output for byte-identical input, which also makes the whole thing trivial to unit-test and to debug when the judge's replay phase surfaces an edge case.
- **Cost/complexity.** No API key, no rate limits, no retry/timeout handling, nothing that can silently degrade mid-test-window.

The tradeoff we accepted: raw fluency and one-off cleverness are lower than what a strong LLM prompt could produce on a rich payload. We tried to close that gap with per-category voice (a dentist's "compliance heads-up" reads differently from a salon's "trend alert"), Hindi-English code-switching driven by `customer.identity.language_pref` / `merchant.identity.languages`, and layered compulsion levers (specificity, loss aversion, social proof, effort externalization, single binary CTA) baked into each template rather than left to a model's discretion.

### Composer design

`composer.py` has two dispatch tables:

- `MERCHANT_COMPOSERS` — one function per merchant-facing trigger kind (`research_digest`, `perf_spike`, `perf_dip`, `competitor_opened`, `milestone_reached`, `festival_upcoming`, `renewal_due`, `gbp_unverified`, `supply_alert`, `active_planning_intent`, and 9 others), covering all 26 trigger kinds present in the generated dataset.
- `CUSTOMER_COMPOSERS` — one function per customer-facing kind (`recall_due`, `chronic_refill_due`, `customer_lapsed_soft/hard`, `appointment_tomorrow`, `trial_followup`, `wedding_package_followup`).

Each function returns the facts it found, a CTA, the compulsion levers it used, and a rationale. Internally the CTA is tracked at a finer grain than the API needs (`binary_yes_no`, `binary_confirm_cancel`, `multi_choice_slot`, `open_ended`) so `conversation_handlers.py` can branch on the specific ask type on a later turn — `compose()` collapses this through `normalize_cta()` to the exact contract enum (`"binary" | "open_ended" | "none"`) before it ever leaves the function, and everything also runs through a taboo-word sanitizer (`sanitize_taboos`) before returning.

**A specific finding that shaped the fallback design:** of the 100 generated trigger contexts, 75 carry only a placeholder payload (`{"placeholder": true, "metric_or_topic": "..."}`) rather than the rich payload shown in the brief's worked examples — only the original 25 seed-trigger instances (plus a few single-occurrence kinds) have full detail. Rather than let those 75 either crash or fabricate a plausible-sounding number, every composer function that depends on a specific payload field checks for its presence first and falls through to `_mf_generic` / `_cf_generic` when it's missing. Those fallbacks don't give up on specificity entirely — they pull whatever concrete fact *is* available and verifiable from the merchant, category, or customer context (a real performance delta, an active offer, a review theme, a visit count) rather than defaulting to a content-free "check out our offers!" message. We verified this by composing all 100 trigger contexts directly and scanning every output body for literal `None`, empty-string artifacts, underscore leaks, and double punctuation — zero occurrences across all 100.

### Decision quality (trigger selection in `/v1/tick`, `bot.py`)

The rubric's "decision quality" dimension asks whether the bot picks the *best* signal for the moment by combining trigger + merchant state + category fit — not whether any one composed message reads well in isolation. We treat this as primarily a selection problem, not a writing problem, and handle it in `bot.py`'s `/v1/tick`, upstream of `compose()`:

- **`_priority_score()`** scores every candidate trigger for a merchant by `urgency * 10`, plus a business-stakes boost for kinds with real downside regardless of what `urgency` says (`supply_alert`, `regulation_change`, `renewal_due`, `winback_eligible`, `competitor_opened`, `dormant_with_vera`, `gbp_unverified`), plus a bonus when the trigger kind matches a merchant-state signal we were already told about (e.g. a `dormant` signal boosts `dormant_with_vera`/`winback_eligible`; `ctr_below_peer` boosts `perf_dip`), plus a small category-fit weighting (compliance-type triggers score higher for dentists/pharmacies). A thin/placeholder-only payload is penalized. This is the "combine trigger + merchant state + category fit" the rubric names, made explicit and inspectable rather than left implicit in a template choice.
- **Restraint, twice over.** If the *best* available signal for a merchant still scores under a floor (`MIN_FIRE_SCORE` — a low-urgency, placeholder-only trigger with nothing real to say), we send nothing that tick rather than force out a content-free nudge. Separately, if a merchant already has an outbound message they haven't replied to yet (tracked via `openconv:{merchant_id}` in `storage.py`, cleared when the conversation ends), a new trigger only interrupts that thread if it clears a materially higher bar (`OPEN_CONV_OVERRIDE_SCORE`) — otherwise we hold off rather than pile a second message on an unanswered one.
- **The reasoning is visible, not just the outcome.** Every `/v1/tick` action's `rationale` states which candidates were considered, their scores, and why the winner won (e.g. *"Picked over 1 other candidate(s) this tick (curious_ask_due (score 6)); selection basis: urgency 3; 'dormant_with_vera' carries inherent business stakes (+2); matches merchant signal 'dormant' (+5)"*). `compose()` itself also prepends a one-line `Decision basis: trigger=…; merchant-state signal '…' factored in; category voice='…' honored.` to every rationale it returns (both `/v1/tick` actions and the 30 canonical `submission.jsonl` rows), so the trigger+merchant+category synthesis is explicit on every single output, not only the ones a composer function happens to narrate in prose.

### Multi-turn handling (`conversation_handlers.py`)

`respond()` is a priority-ordered regex/keyword state machine, checked in this order per turn:

1. **Auto-reply detection** (canned phrasing or verbatim repeat) — escalates send → wait (4h) → end across three consecutive occurrences, so we don't hammer an unattended inbox.
2. **Hostility** — checked *before* a calm opt-out, so "stop bothering me, this is useless spam" is correctly characterized as frustration (ending gracefully with a cooldown) rather than a mild decline.
3. **Explicit opt-out** — ends and suppresses the conversation's `suppression_key`, distinct from the hostility path.
4. **Intent transition** — the brief explicitly calls out "asking a qualifying question after the merchant has already said 'let's do it'" as an anti-pattern. We detect the commitment phrase and switch immediately to a binary CONFIRM/CANCEL ask on the already-stated next step, with no further qualifying questions. Since the prior ask (`last_offer`) is usually phrased as a question ("Want me to draft X?"), dropping it into a statement verbatim would read like a re-ask — `_as_plan()` strips the question framing so the confirmation reads as a stated plan instead ("Great — here's the plan: draft X. Reply CONFIRM...").
5. **Curveball / off-topic** — a question containing no on-topic keyword gets a polite decline and is redirected back to the single open ask, rather than answered or ignored.
6. **Generic affirmative / negative** — advances or ends against the previously stated ask.
7. **Fallback** — acknowledges and restates the single open ask, with an anti-repetition rephraser (`_dedupe`) so we never send the byte-identical body twice in one conversation.

All six of these paths, plus the escalation sequence, were exercised live against the running server (context push → tick → reply) as a final regression check before packaging this submission.

## What additional context would have helped most

- **Richer trigger payloads across the board.** As noted above, 75% of generated triggers are placeholder-only. The composer degrades gracefully, but a bot with access to the *real* metric behind e.g. `competitor_opened` or `festival_upcoming` for every instance (not just the 25 seed examples) could be meaningfully more specific and more compelling across the full trigger population, not just the seed subset.
- **A merchant's actual reply-latency/read-receipt signal.** Auto-reply detection currently relies on canned-phrase matching and verbatim-repeat detection; a real "message read" or "typing" signal from WhatsApp would let us distinguish "owner is slow" from "owner is genuinely away" more precisely than a fixed 4-hour backoff.
- **Explicit category taboo lists per category** (we inferred a taboo/sanitizer list from the brief's compliance framing for clinical categories, but an authoritative per-category list — what a dentist or pharmacy is legally not allowed to claim — would remove guesswork).
- **A shared "already said this" ledger across conversations for the same merchant**, not just within one conversation — right now `_dedupe` only prevents repeats inside a single thread; a merchant getting near-identical messages across two different triggers in the same tick window is a real risk given how sparse many trigger payloads are.

## Deployment & state

The bot runs as a Vercel Python Function (`api/index.py` re-exports the FastAPI `app` from `bot.py`). Vercel Functions have **no instance affinity across requests** — a request can land on any warm/cold instance, so the original assumption of "the process isn't restarted mid-test-window" doesn't hold there. `storage.py` accounts for this: all state (pushed contexts, conversation state, suppression keys, per-merchant send counts) is read/written through a small key-value abstraction backed by **Upstash Redis** (REST API, provisioned via the Vercel Marketplace) rather than module-level Python dicts. This was verified live — push a context, force a real cold start (15+ min idle), then confirm `/v1/healthz`'s `contexts_loaded` count is unchanged even though the instance's own `uptime_seconds` reset to near-zero.

If `KV_REST_API_URL`/`KV_REST_API_TOKEN` aren't set (e.g. plain local dev), `storage.py` falls back to an in-memory dict automatically — useful for `uvicorn bot:app` but never the path used in production.

### Running it locally

```bash
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

or via Docker:

```bash
docker build -t vera-bot .
docker run -p 8080:8080 vera-bot
```

### Running it on Vercel

```bash
vercel link
vercel integration add upstash/upstash-kv   # provisions Redis + env vars
vercel deploy --prod
```
