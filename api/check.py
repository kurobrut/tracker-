import os
import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from api.common import check_all, init_db


def is_authorized(handler):
    secret = os.environ.get("CHECK_SECRET", "").strip()

    # If CHECK_SECRET is not configured, reject requests instead of exposing
    # the checker publicly by accident.
    if not secret:
        return False, "Missing CHECK_SECRET environment variable"

    # Option 1: Authorization: Bearer <CHECK_SECRET>
    auth = handler.headers.get("Authorization", "")
    if auth == f"Bearer {secret}":
        return True, None

    # Option 2: /api/check?key=<CHECK_SECRET>
    # Useful for uptime monitors that cannot send custom headers.
    parsed = urlparse(handler.path)
    query = parse_qs(parsed.query)
    supplied_key = (query.get("key") or [""])[0]
    if supplied_key == secret:
        return True, None

    return False, "Unauthorized"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        allowed, error = is_authorized(self)
        if not allowed:
            status = 500 if error and error.startswith("Missing") else 401
            return self.respond(status, {"ok": False, "error": error})

        try:
            init_db()
            result = check_all(send_notifications=True)
            return self.respond(200, {
                "ok": True,
                "source": "uptime-monitor",
                **result,
            })
        except Exception as e:
            return self.respond(500, {"ok": False, "error": str(e)})

    def do_HEAD(self):
        # A HEAD request is only a health/auth check. It intentionally does not
        # trigger Roblox presence checks or Discord notifications.
        allowed, error = is_authorized(self)
        if not allowed:
            status = 500 if error and error.startswith("Missing") else 401
            self.send_response(status)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()

    def respond(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
