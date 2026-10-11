"""Vercel serverless entrypoint for the existing FidoraPath HTTP handler.

NOTE: This compatibility entrypoint does not migrate SQLite data to PostgreSQL.
Persistent production storage still requires a complete database-layer migration.
"""
import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fidorapath_app", ROOT / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

# The current application initializes SQLite only in main(). Initialize its
# schema for each fresh serverless instance so the API doesn't fail with
# "no such table: users" on a cold start.
app.init_db()

# Vercel's Python runtime supports BaseHTTPRequestHandler subclasses.
class handler(app.Handler):
    pass
