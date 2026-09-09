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
API_KEY = os.getenv("LLM_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL", "openai/gpt-oss-20b")
TIMEOUT = float(os.getenv("LLM_TIMEOUT", "30"))
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
    return bool(API_KEY) and PROVIDER in _ENDPOINTS


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


def chat(system: str, user: str, *, temperature: float = 0.35, max_tokens: int = 1200,
         retries: int = 2) -> str | None:
    if not available():
        return None
    models = [MODEL] + ([FALLBACK_MODEL] if FALLBACK_MODEL and FALLBACK_MODEL != MODEL else [])
    for mi, model in enumerate(models):
        for attempt in range(retries + 1):
            try:
                out = _one_call(model, system, user, temperature, max_tokens)
                if out:
                    return out
            except Exception as e:  # noqa: BLE001 — any failure -> retry / fallback / deterministic
                code = getattr(e, "code", None)
                if code == 429 and mi < len(models) - 1:
                    break  # quota hit on this model — jump straight to the fallback model
                if attempt < retries:
                    time.sleep((4 if code == 429 else 1.5) * (attempt + 1))
    return None
