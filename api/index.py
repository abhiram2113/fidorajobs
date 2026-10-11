"""Vercel serverless entrypoint for FidoraJobs.

Keep the Vercel runtime's handler lifecycle intact. Database initialization is
performed per request inside the normal request dispatcher, with errors logged
without exposing connection credentials.
"""
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as portal_app


class handler(portal_app.Handler):
    def handle_one_request(self):
        try:
            portal_app.init_db()
        except Exception as exc:
            print("FidoraJobs database initialization failed:")
            traceback.print_exc()
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            body = ('{"error":"Database initialization failed","type":"'
                    + type(exc).__name__ + '"}').encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().handle_one_request()
