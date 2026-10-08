import os
import json
from urllib.parse import parse_qs

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, Response
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

from common import (
    DISCORD_API,
    discord_headers,
    add_tracked_user,
    remove_tracked_user,
    get_tracked_users,
    check_all,
    init_db,
    is_admin,
)

app = FastAPI(title="Roblox Presence Discord Bot")

COMMANDS = [
    {
        "name": "track",
        "description": "Track a Roblox user's presence",
        "options": [
            {"type": 3, "name": "user_id", "description": "Roblox user ID", "required": True},
            {"type": 3, "name": "label", "description": "Optional name/label to display", "required": False},
        ],
    },
    {
        "name": "untrack",
        "description": "Stop tracking a Roblox user",
        "options": [
            {"type": 3, "name": "user_id", "description": "Roblox user ID", "required": True}
        ],
    },
    {"name": "tracked", "description": "Show all tracked Roblox users"},
    {"name": "check", "description": "Run a presence check now"},
]


def option_map(interaction):
    data = interaction.get("data", {})
    return {o["name"]: o.get("value") for o in data.get("options", [])}


def text_response(content, ephemeral=True):
    return {
        "type": 4,
        "data": {
            "content": content,
            "flags": 64 if ephemeral else 0,
        },
    }


def get_actor_id(interaction):
    member = interaction.get("member") or {}
    user = member.get("user") or interaction.get("user") or {}
    return user.get("id")


def check_secret_ok(request: Request):
    secret = os.environ.get("CHECK_SECRET", "").strip()
    if not secret:
        raise HTTPException(status_code=500, detail="Missing CHECK_SECRET environment variable")

    auth = request.headers.get("authorization", "")
    if auth == f"Bearer {secret}":
        return True

    supplied = request.query_params.get("key", "")
    if supplied == secret:
        return True

    raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/")
def root():
    return {
        "ok": True,
        "service": "Roblox Presence Discord Bot",
        "check_endpoint": "/api/check?key=YOUR_CHECK_SECRET",
    }


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/check")
def uptime_check(request: Request):
    check_secret_ok(request)
    try:
        init_db()
        result = check_all(send_notifications=True)
        return {"ok": True, "source": "uptime-monitor", **result}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.head("/api/check")
def uptime_head(request: Request):
    check_secret_ok(request)
    # Health/auth only; do not trigger Roblox checks on HEAD.
    return Response(status_code=200)


@app.post("/api/interactions")
async def interactions(request: Request):
    body = await request.body()
    signature = request.headers.get("x-signature-ed25519", "")
    timestamp = request.headers.get("x-signature-timestamp", "")
    public_key = os.environ.get("DISCORD_PUBLIC_KEY", "")

    try:
        VerifyKey(bytes.fromhex(public_key)).verify(
            timestamp.encode() + body,
            bytes.fromhex(signature),
        )
    except (ValueError, BadSignatureError):
        return Response(status_code=401)

    try:
        interaction = json.loads(body.decode("utf-8"))
    except Exception:
        return Response(status_code=400)

    if interaction.get("type") == 1:
        return JSONResponse({"type": 1})

    if interaction.get("type") != 2:
        return JSONResponse(text_response("Unsupported interaction."))

    actor_id = get_actor_id(interaction)
    if not is_admin(actor_id):
        return JSONResponse(text_response("You are not allowed to manage this bot."))

    name = interaction.get("data", {}).get("name")
    opts = option_map(interaction)

    try:
        if name == "track":
            user_id = int(opts["user_id"])
            label = opts.get("label")
            add_tracked_user(user_id, label)
            msg = f"✅ Tracking Roblox user `{user_id}`"
            if label:
                msg += f" as **{label}**"
            return JSONResponse(text_response(msg + "."))

        if name == "untrack":
            user_id = int(opts["user_id"])
            removed = remove_tracked_user(user_id)
            if removed:
                return JSONResponse(text_response(f"🗑️ Stopped tracking `{user_id}`."))
            return JSONResponse(text_response(f"`{user_id}` was not being tracked."))

        if name == "tracked":
            users = get_tracked_users()
            if not users:
                return JSONResponse(text_response("No Roblox users are being tracked."))
            lines = [f"• `{uid}` — {label}" for uid, label in users[:50]]
            extra = "" if len(users) <= 50 else f"\n…and {len(users)-50} more."
            return JSONResponse(text_response("**Tracked users**\n" + "\n".join(lines) + extra))

        if name == "check":
            result = check_all(send_notifications=True)
            return JSONResponse(text_response(
                f"✅ Checked **{result['checked']}** user(s). **{result['changed']}** change(s) found."
            ))

        return JSONResponse(text_response("Unknown command."))
    except Exception as e:
        return JSONResponse(text_response(f"❌ Error: `{str(e)[:1500]}`"))


@app.post("/api/register")
def register_commands(request: Request):
    admin_key = os.environ.get("ADMIN_KEY")
    auth = request.headers.get("authorization", "")
    if not admin_key or auth != f"Bearer {admin_key}":
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    app_id = os.environ.get("DISCORD_APPLICATION_ID")
    guild_id = os.environ.get("DISCORD_GUILD_ID")
    if not app_id:
        return JSONResponse(status_code=500, content={"error": "Missing DISCORD_APPLICATION_ID"})

    if guild_id:
        url = f"{DISCORD_API}/applications/{app_id}/guilds/{guild_id}/commands"
    else:
        url = f"{DISCORD_API}/applications/{app_id}/commands"

    results = []
    for command in COMMANDS:
        r = requests.post(url, headers=discord_headers(), json=command, timeout=15)
        ctype = r.headers.get("content-type", "")
        try:
            body = r.json() if ctype.startswith("application/json") else r.text
        except Exception:
            body = r.text
        results.append({
            "name": command["name"],
            "status": r.status_code,
            "ok": r.ok,
            "body": body,
        })

    return {"registered": results}
