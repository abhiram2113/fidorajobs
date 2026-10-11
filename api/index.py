"""Vercel serverless entrypoint for FidoraJobs.

Vercel dispatches requests to the handler's do_* methods. Ensure the PostgreSQL
schema exists inside those methods before the application handles a request;
overriding handle_one_request alone is not a reliable initialization hook for
the serverless runtime.
"""
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as portal_app


class handler(portal_app.Handler):
    def _initialize_database(self):
        try:
            portal_app.init_db()
            return True
        except Exception as exc:
            print("FidoraJobs database initialization failed:")
            traceback.print_exc()
            body = (
                '{"error":"Database initialization failed","type":"'
                + type(exc).__name__
                + '"}'
            ).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False

    def do_GET(self):
        if not self._initialize_database():
            return
        return super().do_GET()

    def do_POST(self):
        if not self._initialize_database():
            return
        return super().do_POST()

    def do_PUT(self):
        if not self._initialize_database():
            return
        method = getattr(super(), "do_PUT", None)
        if method:
            return method()
        self.send_error(405)

    def do_DELETE(self):
        if not self._initialize_database():
            return
        method = getattr(super(), "do_DELETE", None)
        if method:
            return method()
        self.send_error(405)
