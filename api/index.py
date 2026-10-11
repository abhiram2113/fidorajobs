"""Vercel serverless entrypoint for FidoraJobs."""
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as portal_app


class handler(portal_app.Handler):
    """Initialize PostgreSQL inside the request lifecycle and report safe errors."""
    def handle(self):
        try:
            portal_app.init_db()
        except Exception as exc:
            # Keep credentials private; report the database error to Vercel logs
            # and return a useful response instead of a generic invocation crash.
            print("FidoraJobs database initialization failed:")
            traceback.print_exc()
            try:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(
                    ('{"error":"Database initialization failed","type":"'
                     + type(exc).__name__ + '"}').encode("utf-8")
                )
            except Exception:
                pass
            return
        super().handle()
