import os
import json
import requests
import psycopg
from datetime import datetime, timezone

DISCORD_API = "https://discord.com/api/v10"
ROBLOX_PRESENCE = "https://presence.roblox.com/v1/presence/users"
ROBLOX_USER = "https://users.roblox.com/v1/users/{user_id}"
ROBLOX_PLACE = "https://games.roblox.com/v1/games/multiget-place-details?placeIds={place_id}"

COLORS = {
    "playing": 0x77DD77,
    "online": 0x89CFF0,
    "offline": 0xFF6961,
}


def env(name, required=True, default=None):
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def get_conn():
    return psycopg.connect(env("DATABASE_URL"))


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tracked_users (
                    user_id BIGINT PRIMARY KEY,
                    label TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS presence_state (
                    user_id BIGINT PRIMARY KEY REFERENCES tracked_users(user_id) ON DELETE CASCADE,
                    status_key TEXT,
                    presence_type INTEGER,
                    place_id BIGINT,
                    game_id TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS place_cache (
                    place_id BIGINT PRIMARY KEY,
                    place_name TEXT NOT NULL,
                    universe_id BIGINT,
                    game_url TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
        conn.commit()


def is_admin(discord_user_id):
    raw = os.environ.get("ADMIN_USER_IDS", "").strip()
    if not raw:
        return True
    allowed = {x.strip() for x in raw.split(",") if x.strip()}
    return str(discord_user_id) in allowed


def discord_headers():
    return {
        "Authorization": f"Bot {env('DISCORD_BOT_TOKEN')}",
        "Content-Type": "application/json",
        "User-Agent": "RobloxPresenceBot/1.0"
    }


def send_channel_message(content=None, embeds=None):
    channel_id = env("DISCORD_CHANNEL_ID")
    payload = {}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds
    r = requests.post(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        headers=discord_headers(),
        json=payload,
        timeout=15,
    )
    if not r.ok:
        raise RuntimeError(f"Discord send failed: {r.status_code} {r.text}")
    return r.json()


def get_username(user_id):
    try:
        r = requests.get(ROBLOX_USER.format(user_id=user_id), timeout=10)
        if r.ok:
            return r.json().get("name") or f"User_{user_id}"
    except requests.RequestException:
        pass
    return f"User_{user_id}"


def get_game(place_id):
    if not place_id:
        return "Unknown Game", ""

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT place_name, game_url FROM place_cache WHERE place_id=%s", (int(place_id),))
            row = cur.fetchone()
            if row:
                return row[0], row[1] or f"https://www.roblox.com/games/{place_id}"

    game_name = "Unknown Game"
    universe_id = None
    game_url = f"https://www.roblox.com/games/{place_id}"

    try:
        r = requests.get(ROBLOX_PLACE.format(place_id=place_id), timeout=10)
        if r.ok:
            data = r.json()
            if data:
                info = data[0]
                game_name = info.get("name") or game_name
                universe_id = info.get("universeId")
                safe = game_name.replace(" ", "-").replace("/", "-")
                game_url = f"https://www.roblox.com/games/{place_id}/{safe}"
    except requests.RequestException:
        pass

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO place_cache(place_id, place_name, universe_id, game_url, updated_at)
                VALUES(%s,%s,%s,%s,NOW())
                ON CONFLICT(place_id) DO UPDATE SET
                    place_name=EXCLUDED.place_name,
                    universe_id=EXCLUDED.universe_id,
                    game_url=EXCLUDED.game_url,
                    updated_at=NOW()
            """, (int(place_id), game_name, universe_id, game_url))
        conn.commit()

    return game_name, game_url


def get_tracked_users():
    init_db()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, COALESCE(NULLIF(label,''), user_id::text) FROM tracked_users ORDER BY created_at")
            return cur.fetchall()


def add_tracked_user(user_id, label=None):
    init_db()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tracked_users(user_id, label)
                VALUES(%s,%s)
                ON CONFLICT(user_id) DO UPDATE SET label=EXCLUDED.label
            """, (int(user_id), label))
        conn.commit()


def remove_tracked_user(user_id):
    init_db()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tracked_users WHERE user_id=%s RETURNING user_id", (int(user_id),))
            row = cur.fetchone()
        conn.commit()
    return bool(row)


def build_status(label, username, presence):
    presence_type = presence.get("userPresenceType", 0)
    place_id = presence.get("placeId")
    game_id = presence.get("gameId")

    display = f"{label} ({username})" if label and label != username else username

    if presence_type in (2, 3):
        game_name, game_url = get_game(place_id)
        description = f"🎮 **{display}** is playing **{game_name}**"
        if game_url:
            description += f"\n🔗 {game_url}"
        status_key = json.dumps({
            "presence": presence_type,
            "place_id": place_id,
            "game_id": game_id,
        }, sort_keys=True)
        color = COLORS["playing"]
    elif presence_type == 1:
        description = f"🟢 **{display}** is online (not in game)"
        status_key = json.dumps({"presence": 1}, sort_keys=True)
        color = COLORS["online"]
    else:
        description = f"🔴 **{display}** is offline"
        status_key = json.dumps({"presence": 0}, sort_keys=True)
        color = COLORS["offline"]

    return {
        "description": description,
        "status_key": status_key,
        "presence_type": presence_type,
        "place_id": place_id,
        "game_id": game_id,
        "color": color,
    }


def _state_dict_from_row(row):
    if not row:
        return None
    return {
        "status_key": row[0],
        "presence_type": row[1],
        "place_id": row[2],
        "game_id": row[3],
    }


def _change_message(display, old_state, status):
    new_type = status["presence_type"]
    new_place = status["place_id"]
    new_game_id = str(status["game_id"]) if status["game_id"] else None

    # First observation after tracking/redeploy: report current state.
    if old_state is None:
        return "Now tracking", status["description"]

    old_type = old_state["presence_type"]
    old_place = old_state["place_id"]
    old_game_id = old_state["game_id"]

    if new_type == 0 and old_type != 0:
        return "Went Offline", f"🔴 **{display}** went offline"

    if new_type == 1 and old_type == 0:
        return "Came Online", f"🟢 **{display}** came online"

    if new_type == 1 and old_type in (2, 3):
        old_name, _ = get_game(old_place) if old_place else ("a game", "")
        return "Left Game", f"🟢 **{display}** left **{old_name}** and is still online"

    if new_type in (2, 3):
        new_name, new_url = get_game(new_place)
        link = f"\n🔗 {new_url}" if new_url else ""

        if old_type == 0:
            return "Started Playing", f"🎮 **{display}** came online and started playing **{new_name}**{link}"

        if old_type == 1:
            return "Started Playing", f"🎮 **{display}** started playing **{new_name}**{link}"

        if old_type in (2, 3) and old_place != new_place:
            old_name, _ = get_game(old_place) if old_place else ("Unknown Game", "")
            return "Switched Game", f"🔄 **{display}** switched from **{old_name}** to **{new_name}**{link}"

        if old_type in (2, 3) and old_game_id != new_game_id:
            return "Changed Server", f"🔁 **{display}** changed servers in **{new_name}**{link}"

    return "Presence Update", status["description"]


def check_all(send_notifications=True):
    users = get_tracked_users()
    if not users:
        return {"checked": 0, "changed": 0, "message": "No tracked users"}

    ids = [int(x[0]) for x in users]
    label_map = {int(uid): label for uid, label in users}

    r = requests.post(ROBLOX_PRESENCE, json={"userIds": ids}, timeout=15)
    if not r.ok:
        raise RuntimeError(f"Roblox presence failed: {r.status_code} {r.text}")

    presences = {int(p["userId"]): p for p in r.json().get("userPresences", [])}
    changed = 0

    for user_id in ids:
        presence = presences.get(user_id, {"userId": user_id, "userPresenceType": 0})
        username = get_username(user_id)
        label = label_map[user_id]
        display = f"{label} ({username})" if label and label != username else username
        status = build_status(label, username, presence)

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status_key, presence_type, place_id, game_id FROM presence_state WHERE user_id=%s",
                    (user_id,),
                )
                old_state = _state_dict_from_row(cur.fetchone())
                old_key = old_state["status_key"] if old_state else None

                if old_key != status["status_key"]:
                    changed += 1
                    if send_notifications:
                        title, description = _change_message(display, old_state, status)
                        send_channel_message(embeds=[{
                            "title": title,
                            "description": description,
                            "color": status["color"],
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }])

                cur.execute("""
                    INSERT INTO presence_state(user_id, status_key, presence_type, place_id, game_id, updated_at)
                    VALUES(%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT(user_id) DO UPDATE SET
                        status_key=EXCLUDED.status_key,
                        presence_type=EXCLUDED.presence_type,
                        place_id=EXCLUDED.place_id,
                        game_id=EXCLUDED.game_id,
                        updated_at=NOW()
                """, (
                    user_id,
                    status["status_key"],
                    status["presence_type"],
                    status["place_id"],
                    str(status["game_id"]) if status["game_id"] else None,
                ))
            conn.commit()

    return {"checked": len(ids), "changed": changed}
