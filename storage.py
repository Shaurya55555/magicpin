"""
storage.py — state store for bot.py.

Vercel Functions have no instance affinity across invocations (a request can land on
any warm/cold instance, and nothing guarantees the process that handled /v1/context
is the one that later handles /v1/tick or /v1/reply). Plain module-level Python dicts
— which is what a normal "no restarts expected" deployment target would use — would
silently lose state between requests. So this module is a thin key/value abstraction:
Upstash Redis (REST API, so it works fine from a short-lived serverless function) when
UPSTASH_REDIS_REST_URL/TOKEN are set, falling back to an in-memory dict for local dev
(`uvicorn bot:app` without any Redis env vars configured).

Only bot.py talks to this module — composer.py/conversation_handlers.py stay pure.
"""

from __future__ import annotations
import json
import os
from typing import Any, Optional

_URL = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
_TOKEN = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")

_redis = None
if _URL and _TOKEN:
    from upstash_redis import Redis
    _redis = Redis(url=_URL, token=_TOKEN)

# In-memory fallback — only correct for local single-process dev, never used when
# Redis env vars are present (i.e. never in the deployed/Vercel path).
_mem: dict[str, str] = {}


def set_json(key: str, value: Any) -> None:
    raw = json.dumps(value)
    if _redis:
        _redis.set(key, raw)
    else:
        _mem[key] = raw


def get_json(key: str) -> Optional[Any]:
    raw = _redis.get(key) if _redis else _mem.get(key)
    if raw is None:
        return None
    return json.loads(raw)


def exists(key: str) -> bool:
    if _redis:
        return bool(_redis.exists(key))
    return key in _mem


def incr(key: str) -> int:
    if _redis:
        return int(_redis.incr(key))
    _mem[key] = str(int(_mem.get(key, "0")) + 1)
    return int(_mem[key])


def scan_prefix(prefix: str) -> list[str]:
    """Used only by /v1/healthz (count contexts) and /v1/teardown (wipe). Fine at the
    dataset sizes this challenge uses (dozens–hundreds of keys), not meant for scale."""
    if _redis:
        keys: list[str] = []
        cursor = 0
        while True:
            cursor, batch = _redis.scan(cursor, match=f"{prefix}*", count=200)
            keys.extend(batch)
            if cursor == 0:
                break
        return keys
    return [k for k in _mem if k.startswith(prefix)]


def delete_all(prefix: str = "") -> None:
    keys = scan_prefix(prefix)
    if _redis:
        if keys:
            _redis.delete(*keys)
    else:
        for k in keys:
            _mem.pop(k, None)
