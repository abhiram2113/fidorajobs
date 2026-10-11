"""Vercel serverless entrypoint for the FidoraJobs API."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fidorapath_app", ROOT / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

# PostgreSQL is persistent; initialize idempotent schema and admin account.
app.init_db()

class handler(app.Handler):
    pass
