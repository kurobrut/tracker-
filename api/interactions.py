import os
import json
from http.server import BaseHTTPRequestHandler
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError
from api.common import (
    add_tracked_user,
    remove_tracked_user,
    get_tracked_users,
    check_all,
    is_admin,
)


def option_map(interaction):
    data = interaction.get("data", {})
    return {o["name"]: o.get("value") for o in data.get("options", [])}


def text_response(content, ephemeral=True):
    flags = 64 if ephemeral else 0
    return {
        "type": 4,
        "data": {
            "content": content,
            "flags": flags,
        }
    }


def get_actor_id(interaction):
    member = interaction.get("member") or {}
    user = member.get("user") or interaction.get("user") or {}
    return user.get("id")


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        signature = self.headers.get("X-Signature-Ed25519", "")
        timestamp = self.headers.get("X-Signature-Timestamp", "")
        public_key = os.environ.get("DISCORD_PUBLIC_KEY", "")

        try:
            VerifyKey(bytes.fromhex(public_key)).verify(timestamp.encode() + body, bytes.fromhex(signature))
        except (ValueError, BadSignatureError):
            self.send_response(401)
            self.end_headers()
            return

        interaction = json.loads(body.decode("utf-8"))

        if interaction.get("type") == 1:
            return self.reply({"type": 1})

        if interaction.get("type") != 2:
            return self.reply(text_response("Unsupported interaction."))

        actor_id = get_actor_id(interaction)
        if not is_admin(actor_id):
            return self.reply(text_response("You are not allowed to manage this bot."))

        name = interaction.get("data", {}).get("name")
        opts = option_map(interaction)

        try:
            if name == "track":
                user_id = int(opts["user_id"])
                label = opts.get("label")
                add_tracked_user(user_id, label)
                return self.reply(text_response(f"✅ Tracking Roblox user `{user_id}`" + (f" as **{label}**" if label else "") + "."))

            if name == "untrack":
                user_id = int(opts["user_id"])
                removed = remove_tracked_user(user_id)
                if removed:
                    return self.reply(text_response(f"🗑️ Stopped tracking `{user_id}`."))
                return self.reply(text_response(f"`{user_id}` was not being tracked."))

            if name == "tracked":
                users = get_tracked_users()
                if not users:
                    return self.reply(text_response("No Roblox users are being tracked."))
                lines = [f"• `{uid}` — {label}" for uid, label in users[:50]]
                extra = "" if len(users) <= 50 else f"\n…and {len(users)-50} more."
                return self.reply(text_response("**Tracked users**\n" + "\n".join(lines) + extra))

            if name == "check":
                result = check_all(send_notifications=True)
                return self.reply(text_response(f"✅ Checked **{result['checked']}** user(s). **{result['changed']}** change(s) found."))

            return self.reply(text_response("Unknown command."))
        except Exception as e:
            return self.reply(text_response(f"❌ Error: `{str(e)[:1500]}`"))

    def reply(self, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
