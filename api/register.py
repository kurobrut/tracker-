import os
import json
import requests
from http.server import BaseHTTPRequestHandler
from api.common import DISCORD_API, discord_headers

COMMANDS = [
    {
        "name": "track",
        "description": "Track a Roblox user's presence",
        "options": [
            {
                "type": 3,
                "name": "user_id",
                "description": "Roblox user ID",
                "required": True
            },
            {
                "type": 3,
                "name": "label",
                "description": "Optional name/label to display",
                "required": False
            }
        ]
    },
    {
        "name": "untrack",
        "description": "Stop tracking a Roblox user",
        "options": [
            {
                "type": 3,
                "name": "user_id",
                "description": "Roblox user ID",
                "required": True
            }
        ]
    },
    {
        "name": "tracked",
        "description": "Show all tracked Roblox users"
    },
    {
        "name": "check",
        "description": "Run a presence check now"
    }
]


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        admin_key = os.environ.get("ADMIN_KEY")
        auth = self.headers.get("Authorization", "")
        if not admin_key or auth != f"Bearer {admin_key}":
            return self.respond(401, {"error": "Unauthorized"})

        app_id = os.environ.get("DISCORD_APPLICATION_ID")
        guild_id = os.environ.get("DISCORD_GUILD_ID")
        if not app_id:
            return self.respond(500, {"error": "Missing DISCORD_APPLICATION_ID"})

        if guild_id:
            url = f"{DISCORD_API}/applications/{app_id}/guilds/{guild_id}/commands"
        else:
            url = f"{DISCORD_API}/applications/{app_id}/commands"

        results = []
        for command in COMMANDS:
            r = requests.post(url, headers=discord_headers(), json=command, timeout=15)
            results.append({
                "name": command["name"],
                "status": r.status_code,
                "ok": r.ok,
                "body": r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text,
            })

        self.respond(200, {"registered": results})

    def respond(self, status, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
