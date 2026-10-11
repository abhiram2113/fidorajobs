"""Vercel Python serverless entrypoint.

Use a normal import so the runtime can resolve the project module reliably.
Database initialization is lazy and happens on the first request, returning a
JSON error rather than failing module import if Supabase is not configured.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as portal_app


class handler(portal_app.Handler):
    def __init__(self, *args, **kwargs):
        portal_app.init_db()
        super().__init__(*args, **kwargs)
