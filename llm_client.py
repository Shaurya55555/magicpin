"""
llm_client.py — minimal Groq (OpenAI-compatible) chat client for the hybrid composer.

urllib only (no new dependency). Env-driven so the same code path works locally and
on Vercel. Short timeout + retry with backoff; the caller (composer.compose) treats
any failure here as "fall back to the deterministic template engine", so the bot
never blocks or errors on an LLM hiccup during the test window.

If the primary model returns a rate-limit / quota error, one retry is made against
LLM_FALLBACK_MODEL (a smaller model on a separate quota bucket) before giving up.
"""

from __future__ import annotations
import os
import json
import time
from urllib import request as _rq

PROVIDER = os.getenv("LLM_PROVIDER", "groq").lower()
# LLM_API_KEY may be a single key or a comma-separated pool; on a 429/quota error the
# client advances to the next key. On Vercel a single key is the normal case.
_KEYS = [k.strip() for k in os.getenv("LLM_API_KEY", "").split(",") if k.strip()]
API_KEY = _KEYS[0] if _KEYS else ""
_key_idx = 0
MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL", "openai/gpt-oss-20b")
# The judge allows 30s per call. Keep the per-request timeout well under that so a
# slow provider degrades to the deterministic renderer instead of blowing the budget.
TIMEOUT = float(os.getenv("LLM_TIMEOUT", "7"))
# Brief requires deterministic behaviour (temperature 0 equivalent). Overridable, but 0 by default.
TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0"))
# gpt-oss / qwen3 on Groq are reasoning models: without this the reasoning trace can
# eat the whole max_tokens budget and leave message.content empty.
REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "low")

_ENDPOINTS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "deepseek": "https://api.deepseek.com/v1/chat/completions",
}


def available() -> bool:
    return bool(_KEYS) and PROVIDER in _ENDPOINTS


def _rotate_key() -> bool:
    """Advance to the next key in the pool. Returns False if there's only one."""
    global _key_idx, API_KEY
    if len(_KEYS) < 2:
        return False
    _key_idx = (_key_idx + 1) % len(_KEYS)
    API_KEY = _KEYS[_key_idx]
    return True


def model_label() -> str:
    return f"{PROVIDER}:{MODEL}" if available() else "deterministic-only"


def _one_call(model: str, system: str, user: str, temperature: float, max_tokens: int):
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if REASONING_EFFORT and ("gpt-oss" in model or "qwen3" in model):
        payload["reasoning_effort"] = REASONING_EFFORT
    req = _rq.Request(
        _ENDPOINTS[PROVIDER], data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",  # Groq/Cloudflare 403s the default urllib UA
        },
        method="POST",
    )
    resp = _rq.urlopen(req, timeout=TIMEOUT)
    data = json.loads(resp.read().decode("utf-8"))
    return (data["choices"][0]["message"].get("content") or "").strip() or None


def chat(system: str, user: str, *, temperature: float | None = None, max_tokens: int = 1200,
         retries: int = 1, try_fallback_model: bool = True) -> str | None:
    """One composition's worth of generation. Bounded cost: at most
    (retries+1) tries on the primary model, plus one try on the fallback model.
    With TIMEOUT=7 and retries=1 that is ~21s worst case, inside the 30s judge budget;
    the common case is a single ~2s call."""
    if not available():
        return None
    if temperature is None:
        temperature = TEMPERATURE
    models = [MODEL]
    if try_fallback_model and FALLBACK_MODEL and FALLBACK_MODEL != MODEL:
        models.append(FALLBACK_MODEL)
    for mi, model in enumerate(models):
        tries = (retries + 1) if mi == 0 else 1
        laps = 0
        for attempt in range(tries + len(_KEYS) * 2):
            try:
                out = _one_call(model, system, user, temperature, max_tokens)
                if out:
                    return out
            except Exception as e:  # noqa: BLE001 — any failure -> retry / fallback / deterministic
                code = getattr(e, "code", None)
                if code == 429:
                    if _rotate_key():
                        if _key_idx == 0:
                            laps += 1
                            if laps >= 2:
                                break       # whole pool minute-limited -> deterministic
                            time.sleep(2)   # let the per-minute window breathe
                        continue
                    if mi < len(models) - 1:
                        break
                if attempt < tries - 1:
                    time.sleep(1.0 * (attempt + 1))
                elif code != 429:
                    break
    return None
