"""
Vercel entrypoint. Vercel's Python runtime builds each file under api/ and looks for
an ASGI/WSGI `app` object — this just re-exports the real FastAPI app defined in
bot.py at the repo root so bot.py/composer.py/conversation_handlers.py/storage.py
stay the single source of truth (no duplicated logic between local dev and prod).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import app  # noqa: E402
