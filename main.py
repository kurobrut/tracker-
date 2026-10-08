import os
import time
import asyncio
import threading
from datetime import datetime, timezone
from typing import Optional

import requests
import psycopg2
import psycopg2.extras
import discord
from discord import app_commands
from fastapi import FastAPI
import uvicorn

# =========================
# ENVIRONMENT
# =========================

DATABASE_URL = os.environ["DATABASE_URL"]

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
DISCORD_GUILD_ID = int(os.environ["DISCORD_GUILD_ID"]) if os.getenv("DISCORD_GUILD_ID") else None

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])

CHECK_INTERVAL = max(10, int(os.getenv("CHECK_INTERVAL", "30")))
PORT = int(os.getenv("PORT", "10000"))

DISCORD_ADMIN_USER_IDS = {
    int(x.strip())
    for x in os.getenv("DISCORD_ADMIN_USER_IDS", "").split(",")
    if x.strip().isdigit()
}
TELEGRAM_ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

ROBLOX_PRESENCE_URL = "https://presence.roblox.com/v1/presence/users"
ROBLOX_USERS_URL = "https://users.roblox.com/v1/users/{}"
ROBLOX_PLACE_URL = "https://games.roblox.com/v1/games/multiget-place-details?placeIds={}"

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "RobloxPresenceTracker/1.0"})

# =========================
# DATABASE
# =========================

def db_connect():
    # Render/Neon/Supabase/etc. external PostgreSQL URLs commonly need SSL.
    # If the provider already includes ?sslmode=require, psycopg2 uses it.
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)

def init_db():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tracked_users (
                    user_id BIGINT PRIMARY KEY,
                    label TEXT NOT NULL,
                    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS presence_state (
                    user_id BIGINT PRIMARY KEY REFERENCES tracked_users(user_id) ON DELETE CASCADE,
                    username TEXT,
                    presence_type INTEGER,
                    place_id BIGINT,
                    game_id TEXT,
                    game_name TEXT,
                    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS place_cache (
                    place_id BIGINT PRIMARY KEY,
                    game_name TEXT NOT NULL,
                    game_url TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
        conn.commit()

def get_tracked_users():
    with db_connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT user_id, label FROM tracked_users ORDER BY added_at ASC")
            return list(cur.fetchall())

def add_tracked_user(user_id: int, label: str):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tracked_users (user_id, label)
                VALUES (%s, %s)
                ON CONFLICT (user_id)
                DO UPDATE SET label = EXCLUDED.label
            """, (user_id, label))
        conn.commit()

def remove_tracked_user(user_id: int):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tracked_users WHERE user_id = %s", (user_id,))
            deleted = cur.rowcount > 0
        conn.commit()
    return deleted

def get_saved_state(user_id: int):
    with db_connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT user_id, username, presence_type, place_id, game_id, game_name, checked_at
                FROM presence_state
                WHERE user_id = %s
            """, (user_id,))
            return cur.fetchone()

def save_state(user_id: int, username: str, presence_type: int,
               place_id: Optional[int], game_id: Optional[str], game_name: Optional[str]):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO presence_state
                    (user_id, username, presence_type, place_id, game_id, game_name, checked_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (user_id)
                DO UPDATE SET
                    username = EXCLUDED.username,
                    presence_type = EXCLUDED.presence_type,
                    place_id = EXCLUDED.place_id,
                    game_id = EXCLUDED.game_id,
                    game_name = EXCLUDED.game_name,
                    checked_at = NOW()
            """, (user_id, username, presence_type, place_id, game_id, game_name))
        conn.commit()

def get_active_rows():
    with db_connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT t.user_id, t.label, p.username, p.presence_type,
                       p.game_name, p.place_id, p.checked_at
                FROM tracked_users t
                LEFT JOIN presence_state p ON p.user_id = t.user_id
                WHERE COALESCE(p.presence_type, 0) <> 0
                ORDER BY t.label
            """)
            return list(cur.fetchall())

# =========================
# ROBLOX
# =========================

def get_username(user_id: int) -> str:
    try:
        r = HTTP.get(ROBLOX_USERS_URL.format(user_id), timeout=10)
        if r.ok:
            return r.json().get("name") or f"User_{user_id}"
    except requests.RequestException:
        pass
    return f"User_{user_id}"

def get_game_info(place_id: Optional[int]):
    if not place_id:
        return None, None

    with db_connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT game_name, game_url FROM place_cache WHERE place_id = %s", (place_id,))
            row = cur.fetchone()
            if row:
                return row["game_name"], row["game_url"]

    game_name = "Unknown Game"
    game_url = f"https://www.roblox.com/games/{place_id}"

    try:
        r = HTTP.get(ROBLOX_PLACE_URL.format(place_id), timeout=10)
        if r.ok:
            data = r.json()
            if data:
                game_name = data[0].get("name") or game_name
    except requests.RequestException:
        pass

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO place_cache (place_id, game_name, game_url, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (place_id)
                DO UPDATE SET game_name = EXCLUDED.game_name,
                              game_url = EXCLUDED.game_url,
                              updated_at = NOW()
            """, (place_id, game_name, game_url))
        conn.commit()

    return game_name, game_url

def fetch_presences(user_ids):
    if not user_ids:
        return {}

    try:
        r = HTTP.post(
            ROBLOX_PRESENCE_URL,
            json={"userIds": user_ids},
            timeout=15,
        )
        r.raise_for_status()
        return {int(x["userId"]): x for x in r.json().get("userPresences", [])}
    except requests.RequestException as exc:
        print(f"[Roblox] Presence request failed: {exc}")
        return {}

# =========================
# MESSAGE FORMATTING
# =========================

def state_label(presence_type: Optional[int]) -> str:
    if presence_type in (2, 3):
        return "playing"
    if presence_type == 1:
        return "online"
    return "offline"

def build_change_message(label, username, old, new_presence_type, game_name, game_url, old_game_name, same_game_server_changed):
    old_type = int(old["presence_type"]) if old and old["presence_type"] is not None else None
    new_type = int(new_presence_type)

    display = f"{label} ({username})" if label.lower() != username.lower() else username

    if old is None:
        if new_type in (2, 3):
            return (
                "🎮 Current Status",
                f"**{display}** is currently playing **{game_name or 'Unknown Game'}**"
                + (f"\n{game_url}" if game_url else "")
            )
        if new_type == 1:
            return "🟢 Current Status", f"**{display}** is currently online."
        return "🔴 Current Status", f"**{display}** is currently offline."

    if old_type == new_type:
        if new_type in (2, 3):
            if old_game_name and game_name and old_game_name != game_name:
                return (
                    "🔄 Switched Game",
                    f"**{display}** switched from **{old_game_name}** to **{game_name}**"
                    + (f"\n{game_url}" if game_url else "")
                )
            if same_game_server_changed:
                return (
                    "🔁 Changed Server",
                    f"**{display}** changed servers in **{game_name or 'Unknown Game'}**"
                    + (f"\n{game_url}" if game_url else "")
                )
        return None

    if new_type == 0:
        return "🔴 Went Offline", f"**{display}** went offline."

    if new_type == 1:
        if old_type in (2, 3):
            return "🟢 Left Game", f"**{display}** left the game but is still online."
        return "🟢 Came Online", f"**{display}** came online."

    if new_type in (2, 3):
        if old_type == 0:
            title = "🎮 Came Online & Started Playing"
        elif old_type == 1:
            title = "🎮 Started Playing"
        else:
            title = "🎮 Playing"
        return (
            title,
            f"**{display}** is playing **{game_name or 'Unknown Game'}**"
            + (f"\n{game_url}" if game_url else "")
        )

    return None

# =========================
# TELEGRAM
# =========================

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def telegram_send(text: str, chat_id: Optional[str] = None):
    target = str(chat_id or TELEGRAM_CHAT_ID)
    # Strip Discord markdown bold into Telegram-friendly bold.
    try:
        r = HTTP.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": target,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if not r.ok:
            print(f"[Telegram] sendMessage failed: {r.status_code} {r.text[:300]}")
    except requests.RequestException as exc:
        print(f"[Telegram] send failed: {exc}")

def telegram_authorized(user_id: int) -> bool:
    return not TELEGRAM_ADMIN_IDS or user_id in TELEGRAM_ADMIN_IDS

def telegram_active_text():
    rows = get_active_rows()
    if not rows:
        return "No tracked players are currently active."

    lines = ["🟢 Active tracked players:"]
    for row in rows:
        username = row["username"] or f"User_{row['user_id']}"
        label = row["label"]
        ptype = row["presence_type"]
        if ptype in (2, 3):
            game = row["game_name"] or "Unknown Game"
            lines.append(f"• {label} ({username}) — 🎮 {game}")
        else:
            lines.append(f"• {label} ({username}) — 🟢 Online")
    return "\n".join(lines)

def telegram_tracked_text():
    users = get_tracked_users()
    if not users:
        return "No tracked Roblox users."
    lines = ["📋 Tracked Roblox users:"]
    for u in users:
        lines.append(f"• {u['label']} — {u['user_id']}")
    return "\n".join(lines)

def telegram_polling_loop(loop):
    offset = 0
    print("[Telegram] polling started")

    while True:
        try:
            r = HTTP.get(
                f"{TELEGRAM_API}/getUpdates",
                params={"timeout": 25, "offset": offset, "allowed_updates": '["message"]'},
                timeout=35,
            )
            if not r.ok:
                print(f"[Telegram] getUpdates failed: {r.status_code} {r.text[:200]}")
                time.sleep(5)
                continue

            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                text = (message.get("text") or "").strip()
                chat = message.get("chat") or {}
                sender = message.get("from") or {}
                chat_id = str(chat.get("id"))
                sender_id = int(sender.get("id", 0))

                # Keep the control bot limited to the configured chat.
                if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
                    continue

                if not text.startswith("/"):
                    continue

                command, *args = text.split()
                command = command.split("@", 1)[0].lower()

                if command == "/start":
                    telegram_send(
                        "Roblox Presence Tracker is running.\n\n"
                        "/active - show players currently online/in-game\n"
                        "/tracked - list tracked users\n"
                        "/check - run a presence check now\n"
                        "/track <roblox_user_id> [label]\n"
                        "/untrack <roblox_user_id>",
                        chat_id,
                    )

                elif command == "/active":
                    telegram_send(telegram_active_text(), chat_id)

                elif command == "/tracked":
                    telegram_send(telegram_tracked_text(), chat_id)

                elif command == "/check":
                    if not telegram_authorized(sender_id):
                        telegram_send("Not authorized.", chat_id)
                        continue
                    future = asyncio.run_coroutine_threadsafe(run_presence_check("telegram"), loop)
                    try:
                        result = future.result(timeout=40)
                        telegram_send(
                            f"✅ Check finished\nChecked: {result['checked']}\nChanges: {result['changed']}",
                            chat_id,
                        )
                    except Exception as exc:
                        telegram_send(f"❌ Check failed: {exc}", chat_id)

                elif command == "/track":
                    if not telegram_authorized(sender_id):
                        telegram_send("Not authorized.", chat_id)
                        continue
                    if not args or not args[0].isdigit():
                        telegram_send("Usage: /track <roblox_user_id> [label]", chat_id)
                        continue
                    user_id = int(args[0])
                    username = get_username(user_id)
                    label = " ".join(args[1:]).strip() or username
                    add_tracked_user(user_id, label)
                    telegram_send(f"✅ Tracking {label} ({username}) — {user_id}", chat_id)

                elif command == "/untrack":
                    if not telegram_authorized(sender_id):
                        telegram_send("Not authorized.", chat_id)
                        continue
                    if not args or not args[0].isdigit():
                        telegram_send("Usage: /untrack <roblox_user_id>", chat_id)
                        continue
                    user_id = int(args[0])
                    removed = remove_tracked_user(user_id)
                    telegram_send("✅ Removed." if removed else "That user was not tracked.", chat_id)

        except Exception as exc:
            print(f"[Telegram] polling error: {exc}")
            time.sleep(5)

# =========================
# DISCORD
# =========================

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

_tracker_task = None
_ready_once = False

def discord_admin(interaction: discord.Interaction) -> bool:
    if DISCORD_ADMIN_USER_IDS:
        return interaction.user.id in DISCORD_ADMIN_USER_IDS
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.manage_guild)

async def get_discord_channel():
    channel = client.get_channel(DISCORD_CHANNEL_ID)
    if channel is None:
        try:
            channel = await client.fetch_channel(DISCORD_CHANNEL_ID)
        except Exception as exc:
            print(f"[Discord] Could not fetch channel: {exc}")
    return channel

async def notify_both(title: str, description: str):
    print(f"[Notify] {title}: {description.replace(chr(10), ' | ')}")

    channel = await get_discord_channel()
    if channel:
        embed = discord.Embed(
            title=title,
            description=description,
            timestamp=datetime.now(timezone.utc),
        )
        try:
            await channel.send(embed=embed)
        except Exception as exc:
            print(f"[Discord] send failed: {exc}")

    tg_text = f"{title}\n\n{description}"
    await asyncio.to_thread(telegram_send, tg_text)

@tree.command(name="track", description="Track a Roblox user's presence")
@app_commands.describe(user_id="Roblox user ID", label="Display label for this player")
async def track_command(interaction: discord.Interaction, user_id: str, label: Optional[str] = None):
    if not discord_admin(interaction):
        await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        return
    if not user_id.isdigit():
        await interaction.response.send_message("Roblox user_id must be a number.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    uid = int(user_id)
    username = await asyncio.to_thread(get_username, uid)
    final_label = (label or username).strip()
    await asyncio.to_thread(add_tracked_user, uid, final_label)
    await interaction.followup.send(f"✅ Tracking **{final_label}** ({username}) — `{uid}`", ephemeral=True)

@tree.command(name="untrack", description="Stop tracking a Roblox user")
async def untrack_command(interaction: discord.Interaction, user_id: str):
    if not discord_admin(interaction):
        await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        return
    if not user_id.isdigit():
        await interaction.response.send_message("Roblox user_id must be a number.", ephemeral=True)
        return

    removed = await asyncio.to_thread(remove_tracked_user, int(user_id))
    await interaction.response.send_message(
        "✅ Removed." if removed else "That Roblox user is not currently tracked.",
        ephemeral=True,
    )

@tree.command(name="tracked", description="List tracked Roblox users")
async def tracked_command(interaction: discord.Interaction):
    users = await asyncio.to_thread(get_tracked_users)
    if not users:
        await interaction.response.send_message("No tracked Roblox users.", ephemeral=True)
        return
    text = "\n".join(f"• **{u['label']}** — `{u['user_id']}`" for u in users)
    await interaction.response.send_message(text, ephemeral=True)

@tree.command(name="active", description="Show tracked players currently online or in-game")
async def active_command(interaction: discord.Interaction):
    rows = await asyncio.to_thread(get_active_rows)
    if not rows:
        await interaction.response.send_message("No tracked players are currently active.")
        return
    lines = []
    for row in rows:
        username = row["username"] or f"User_{row['user_id']}"
        if row["presence_type"] in (2, 3):
            lines.append(f"🎮 **{row['label']}** ({username}) — {row['game_name'] or 'Unknown Game'}")
        else:
            lines.append(f"🟢 **{row['label']}** ({username}) — Online")
    await interaction.response.send_message("\n".join(lines))

@tree.command(name="check", description="Run a Roblox presence check now")
async def check_command(interaction: discord.Interaction):
    if not discord_admin(interaction):
        await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    result = await run_presence_check("discord")
    await interaction.followup.send(
        f"✅ Checked **{result['checked']}** players. Changes: **{result['changed']}**",
        ephemeral=True,
    )

@client.event
async def on_ready():
    global _tracker_task, _ready_once

    print(f"[Discord] Logged in as {client.user} ({client.user.id})")

    if not _ready_once:
        try:
            if DISCORD_GUILD_ID:
                guild = discord.Object(id=DISCORD_GUILD_ID)
                tree.copy_global_to(guild=guild)
                synced = await tree.sync(guild=guild)
            else:
                synced = await tree.sync()
            print(f"[Discord] Synced {len(synced)} slash commands")
        except Exception as exc:
            print(f"[Discord] command sync failed: {exc}")

        if _tracker_task is None or _tracker_task.done():
            _tracker_task = asyncio.create_task(tracker_loop())

        _ready_once = True

    # Makes the bot visibly online with an activity.
    try:
        await client.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Roblox presence",
            ),
        )
    except Exception as exc:
        print(f"[Discord] presence update failed: {exc}")

# =========================
# TRACKER
# =========================

_check_lock = asyncio.Lock()

async def run_presence_check(source="timer"):
    async with _check_lock:
        users = await asyncio.to_thread(get_tracked_users)
        if not users:
            return {"checked": 0, "changed": 0}

        ids = [int(u["user_id"]) for u in users]
        presence_map = await asyncio.to_thread(fetch_presences, ids)

        changed = 0

        for user in users:
            user_id = int(user["user_id"])
            label = user["label"]
            presence = presence_map.get(user_id)

            # If Roblox omitted the user, skip rather than falsely marking offline.
            if not presence:
                continue

            presence_type = int(presence.get("userPresenceType") or 0)
            place_id = presence.get("placeId")
            game_id = presence.get("gameId")
            if place_id is not None:
                try:
                    place_id = int(place_id)
                except (TypeError, ValueError):
                    place_id = None

            username = await asyncio.to_thread(get_username, user_id)
            game_name = None
            game_url = None
            if presence_type in (2, 3) and place_id:
                game_name, game_url = await asyncio.to_thread(get_game_info, place_id)

            old = await asyncio.to_thread(get_saved_state, user_id)

            old_game_name = old["game_name"] if old else None
            old_game_id = old["game_id"] if old else None
            same_game_server_changed = bool(
                old
                and presence_type in (2, 3)
                and int(old["presence_type"] or 0) in (2, 3)
                and old.get("place_id") == place_id
                and old_game_id
                and game_id
                and str(old_game_id) != str(game_id)
            )

            change = build_change_message(
                label,
                username,
                old,
                presence_type,
                game_name,
                game_url,
                old_game_name,
                same_game_server_changed,
            )

            await asyncio.to_thread(
                save_state,
                user_id,
                username,
                presence_type,
                place_id,
                str(game_id) if game_id else None,
                game_name,
            )

            if change:
                changed += 1
                await notify_both(change[0], change[1])

        print(f"[Tracker] source={source} checked={len(users)} changed={changed}")
        return {"checked": len(users), "changed": changed}

async def tracker_loop():
    # Small delay after Discord connects so the rest of the app is ready.
    await asyncio.sleep(3)
    while not client.is_closed():
        try:
            await run_presence_check("timer")
        except Exception as exc:
            print(f"[Tracker] check failed: {exc}")
        await asyncio.sleep(CHECK_INTERVAL)

# =========================
# HEALTH WEB SERVER FOR RENDER
# =========================

web = FastAPI()

@web.get("/")
def root():
    return {
        "ok": True,
        "service": "Roblox Presence Discord + Telegram Bot",
        "discord": str(client.user) if client.user else "connecting",
        "check_interval_seconds": CHECK_INTERVAL,
    }

@web.get("/health")
def health():
    return {"ok": True}

def run_web_server():
    uvicorn.run(web, host="0.0.0.0", port=PORT, log_level="info")

# =========================
# START
# =========================

def main():
    init_db()

    # Render Web Service needs a listening HTTP port.
    threading.Thread(target=run_web_server, daemon=True).start()

    # Telegram long-polling needs the Discord asyncio loop, so start it after
    # discord.py has created its running loop.
    async def runner():
        loop = asyncio.get_running_loop()
        threading.Thread(target=telegram_polling_loop, args=(loop,), daemon=True).start()
        await client.start(DISCORD_BOT_TOKEN)

    asyncio.run(runner())

if __name__ == "__main__":
    main()
