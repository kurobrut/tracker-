import os
import time
import asyncio
import threading
from datetime import datetime, timezone
from typing import Optional

import requests
import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool
import discord
from discord import app_commands
from fastapi import FastAPI
import uvicorn


# ============================================================
# ENV
# ============================================================

DATABASE_URL = os.environ["DATABASE_URL"]

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
DISCORD_GUILD_ID = (
    int(os.environ["DISCORD_GUILD_ID"])
    if os.getenv("DISCORD_GUILD_ID")
    else None
)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])

CHECK_INTERVAL = max(15, int(os.getenv("CHECK_INTERVAL", "30")))
PORT = int(os.getenv("PORT", "10000"))

DISCORD_ADMIN_USER_IDS = {
    int(x.strip())
    for x in os.getenv("DISCORD_ADMIN_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

TELEGRAM_ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}


# ============================================================
# ROBLOX API
# ============================================================

ROBLOX_PRESENCE_URL = "https://presence.roblox.com/v1/presence/users"

ROBLOX_USERS_URL = (
    "https://users.roblox.com/v1/users/{}"
)

ROBLOX_UNIVERSE_GAMES_URL = (
    "https://games.roblox.com/v1/games?universeIds={}"
)


HTTP = requests.Session()

HTTP.headers.update(
    {
        "User-Agent": "RobloxPresenceTracker/3.0"
    }
)


# ============================================================
# DATABASE POOL
# ============================================================

DB_POOL: Optional[ThreadedConnectionPool] = None


def init_pool():
    global DB_POOL

    if DB_POOL is None:
        DB_POOL = ThreadedConnectionPool(
            1,
            8,
            dsn=DATABASE_URL,
            connect_timeout=10,
        )


def db_conn():
    if DB_POOL is None:
        init_pool()

    return DB_POOL.getconn()


def db_put(conn):
    if DB_POOL is not None and conn is not None:
        DB_POOL.putconn(conn)


# ============================================================
# DATABASE MIGRATION
# ============================================================

def init_db():

    init_pool()

    conn = db_conn()

    try:

        conn.autocommit = False

        with conn.cursor() as cur:

            # ------------------------------------------------
            # TRACKED USERS
            # ------------------------------------------------

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tracked_users (
                    user_id BIGINT PRIMARY KEY,
                    label TEXT NOT NULL
                )
                """
            )

            cur.execute(
                """
                ALTER TABLE tracked_users
                ADD COLUMN IF NOT EXISTS label TEXT
                """
            )

            cur.execute(
                """
                ALTER TABLE tracked_users
                ADD COLUMN IF NOT EXISTS added_at TIMESTAMPTZ
                """
            )

            cur.execute(
                """
                UPDATE tracked_users
                SET label =
                    COALESCE(
                        NULLIF(label, ''),
                        'User_' || user_id::text
                    )
                WHERE label IS NULL
                   OR label = ''
                """
            )

            cur.execute(
                """
                UPDATE tracked_users
                SET added_at = NOW()
                WHERE added_at IS NULL
                """
            )

            cur.execute(
                """
                ALTER TABLE tracked_users
                ALTER COLUMN label SET NOT NULL
                """
            )

            cur.execute(
                """
                ALTER TABLE tracked_users
                ALTER COLUMN added_at
                SET DEFAULT NOW()
                """
            )


            # ------------------------------------------------
            # PRESENCE STATE
            # ------------------------------------------------

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS presence_state (
                    user_id BIGINT PRIMARY KEY
                )
                """
            )

            presence_columns = [
                ("username", "TEXT"),
                ("presence_type", "INTEGER"),
                ("place_id", "BIGINT"),
                ("universe_id", "BIGINT"),
                ("game_id", "TEXT"),
                ("game_name", "TEXT"),
                ("checked_at", "TIMESTAMPTZ"),
            ]

            for column, data_type in presence_columns:

                cur.execute(
                    f"""
                    ALTER TABLE presence_state
                    ADD COLUMN IF NOT EXISTS
                    {column} {data_type}
                    """
                )

            cur.execute(
                """
                UPDATE presence_state
                SET checked_at = NOW()
                WHERE checked_at IS NULL
                """
            )

            cur.execute(
                """
                ALTER TABLE presence_state
                ALTER COLUMN checked_at
                SET DEFAULT NOW()
                """
            )


            # ------------------------------------------------
            # PLACE CACHE
            # ------------------------------------------------

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS place_cache (
                    place_id BIGINT PRIMARY KEY
                )
                """
            )

            cache_columns = [
                ("game_name", "TEXT"),
                ("game_url", "TEXT"),
                ("updated_at", "TIMESTAMPTZ"),
            ]

            for column, data_type in cache_columns:

                cur.execute(
                    f"""
                    ALTER TABLE place_cache
                    ADD COLUMN IF NOT EXISTS
                    {column} {data_type}
                    """
                )

            cur.execute(
                """
                UPDATE place_cache
                SET
                    game_name =
                        COALESCE(
                            game_name,
                            'Unknown Game'
                        ),
                    updated_at =
                        COALESCE(
                            updated_at,
                            NOW()
                        )
                """
            )

            cur.execute(
                """
                ALTER TABLE place_cache
                ALTER COLUMN game_name
                SET DEFAULT 'Unknown Game'
                """
            )

            cur.execute(
                """
                ALTER TABLE place_cache
                ALTER COLUMN updated_at
                SET DEFAULT NOW()
                """
            )

        conn.commit()

        print(
            "[Database] schema ready/migrated successfully"
        )

    except Exception:

        conn.rollback()
        raise

    finally:

        db_put(conn)


# ============================================================
# DATABASE HELPERS
# ============================================================

def get_tracked_users():

    conn = db_conn()

    try:

        with conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    user_id,
                    label
                FROM tracked_users
                ORDER BY
                    COALESCE(
                        added_at,
                        NOW()
                    ) ASC,
                    user_id ASC
                """
            )

            return list(cur.fetchall())

    finally:

        db_put(conn)


def add_tracked_user(
    user_id: int,
    label: str
):

    conn = db_conn()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO tracked_users
                    (
                        user_id,
                        label,
                        added_at
                    )
                VALUES
                    (
                        %s,
                        %s,
                        NOW()
                    )

                ON CONFLICT (user_id)
                DO UPDATE SET
                    label = EXCLUDED.label
                """,
                (
                    user_id,
                    label,
                ),
            )

        conn.commit()

    except Exception:

        conn.rollback()
        raise

    finally:

        db_put(conn)


def remove_tracked_user(
    user_id: int
):

    conn = db_conn()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                DELETE FROM presence_state
                WHERE user_id = %s
                """,
                (user_id,),
            )

            cur.execute(
                """
                DELETE FROM tracked_users
                WHERE user_id = %s
                """,
                (user_id,),
            )

            deleted = cur.rowcount > 0

        conn.commit()

        return deleted

    except Exception:

        conn.rollback()
        raise

    finally:

        db_put(conn)


def get_all_saved_states(
    user_ids
):

    if not user_ids:
        return {}

    conn = db_conn()

    try:

        with conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    user_id,
                    username,
                    presence_type,
                    place_id,
                    universe_id,
                    game_id,
                    game_name,
                    checked_at
                FROM presence_state
                WHERE user_id = ANY(%s)
                """,
                (user_ids,),
            )

            return {
                int(row["user_id"]): row
                for row in cur.fetchall()
            }

    finally:

        db_put(conn)


def save_state(
    user_id: int,
    username: str,
    presence_type: int,
    place_id: Optional[int],
    universe_id: Optional[int],
    game_id: Optional[str],
    game_name: Optional[str],
):

    conn = db_conn()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO presence_state
                    (
                        user_id,
                        username,
                        presence_type,
                        place_id,
                        universe_id,
                        game_id,
                        game_name,
                        checked_at
                    )
                VALUES
                    (
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        NOW()
                    )

                ON CONFLICT (user_id)
                DO UPDATE SET
                    username =
                        EXCLUDED.username,

                    presence_type =
                        EXCLUDED.presence_type,

                    place_id =
                        EXCLUDED.place_id,

                    universe_id =
                        EXCLUDED.universe_id,

                    game_id =
                        EXCLUDED.game_id,

                    game_name =
                        EXCLUDED.game_name,

                    checked_at =
                        NOW()
                """,
                (
                    user_id,
                    username,
                    presence_type,
                    place_id,
                    universe_id,
                    game_id,
                    game_name,
                ),
            )

        conn.commit()

    except Exception:

        conn.rollback()
        raise

    finally:

        db_put(conn)


def get_active_rows():

    conn = db_conn()

    try:

        with conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    t.user_id,
                    t.label,

                    p.username,
                    p.presence_type,
                    p.game_name,
                    p.place_id,
                    p.checked_at

                FROM tracked_users t

                LEFT JOIN presence_state p
                ON p.user_id = t.user_id

                WHERE
                    COALESCE(
                        p.presence_type,
                        0
                    ) <> 0

                ORDER BY
                    t.label ASC
                """
            )

            return list(cur.fetchall())

    finally:

        db_put(conn)


def get_cached_game(
    place_id
):

    conn = db_conn()

    try:

        with conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:

            cur.execute(
                """
                SELECT
                    game_name,
                    game_url
                FROM place_cache
                WHERE place_id = %s
                """,
                (place_id,),
            )

            return cur.fetchone()

    finally:

        db_put(conn)


def save_cached_game(
    place_id,
    game_name,
    game_url
):

    conn = db_conn()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO place_cache
                    (
                        place_id,
                        game_name,
                        game_url,
                        updated_at
                    )

                VALUES
                    (
                        %s,
                        %s,
                        %s,
                        NOW()
                    )

                ON CONFLICT (place_id)
                DO UPDATE SET
                    game_name =
                        EXCLUDED.game_name,

                    game_url =
                        EXCLUDED.game_url,

                    updated_at =
                        NOW()
                """,
                (
                    place_id,
                    game_name,
                    game_url,
                ),
            )

        conn.commit()

    except Exception:

        conn.rollback()
        raise

    finally:

        db_put(conn)


# ============================================================
# ROBLOX HELPERS
# ============================================================

def get_username(
    user_id: int
) -> str:

    try:

        response = HTTP.get(
            ROBLOX_USERS_URL.format(
                user_id
            ),
            timeout=6,
        )

        if response.ok:

            data = response.json()

            return (
                data.get("name")
                or f"User_{user_id}"
            )

    except requests.RequestException as exc:

        print(
            f"[Roblox] username failed "
            f"for {user_id}: {exc}"
        )

    return f"User_{user_id}"


def clean_last_location(
    value: Optional[str]
):

    if not value:
        return None

    value = value.strip()

    if not value:
        return None

    ignored = {
        "website",
        "mobile website",
        "studio",
        "xbox",
        "unknown",
        "roblox",
    }

    if value.lower() in ignored:
        return None

    return value


def get_game_info(
    place_id: Optional[int],
    universe_id: Optional[int] = None,
    last_location: Optional[str] = None,
):

    """
    Resolves the Roblox experience name.

    Priority:
    1. Presence API lastLocation
    2. PostgreSQL cache
    3. Roblox universe details API
    4. Place ID fallback
    """

    location_name = clean_last_location(
        last_location
    )


    # --------------------------------------------------------
    # 1. PRESENCE LAST LOCATION
    # --------------------------------------------------------

    if location_name:

        game_url = None

        if place_id:

            game_url = (
                f"https://www.roblox.com/"
                f"games/{place_id}"
            )

            save_cached_game(
                place_id,
                location_name,
                game_url,
            )

        return (
            location_name,
            game_url,
        )


    # --------------------------------------------------------
    # 2. CACHE
    # --------------------------------------------------------

    if place_id:

        cached = get_cached_game(
            place_id
        )

        if cached:

            cached_name = cached.get(
                "game_name"
            )

            if (
                cached_name
                and cached_name
                != "Unknown Game"
            ):

                return (
                    cached_name,
                    cached.get(
                        "game_url"
                    ),
                )


    # --------------------------------------------------------
    # 3. UNIVERSE API
    # --------------------------------------------------------

    if universe_id:

        try:

            response = HTTP.get(
                ROBLOX_UNIVERSE_GAMES_URL.format(
                    universe_id
                ),
                timeout=6,
            )

            if response.ok:

                payload = response.json()

                games = payload.get(
                    "data"
                ) or []

                if games:

                    game = games[0]

                    game_name = (
                        game.get("name")
                        or "Unknown Game"
                    )

                    root_place_id = (
                        game.get(
                            "rootPlaceId"
                        )
                        or place_id
                    )

                    game_url = None

                    if root_place_id:

                        game_url = (
                            "https://www.roblox.com/"
                            f"games/{root_place_id}"
                        )

                    if place_id:

                        save_cached_game(
                            place_id,
                            game_name,
                            game_url,
                        )

                    return (
                        game_name,
                        game_url,
                    )

            else:

                print(
                    "[Roblox] universe lookup "
                    f"failed {response.status_code}: "
                    f"{response.text[:200]}"
                )

        except requests.RequestException as exc:

            print(
                "[Roblox] universe lookup failed "
                f"for {universe_id}: {exc}"
            )


    # --------------------------------------------------------
    # 4. FALLBACK
    # --------------------------------------------------------

    if place_id:

        return (
            f"Roblox Experience "
            f"(Place {place_id})",

            f"https://www.roblox.com/"
            f"games/{place_id}",
        )


    return (
        "Unknown Game",
        None,
    )


def fetch_presences(
    user_ids
):

    if not user_ids:
        return {}

    try:

        response = HTTP.post(
            ROBLOX_PRESENCE_URL,
            json={
                "userIds": user_ids
            },
            timeout=10,
        )

        response.raise_for_status()

        data = response.json()

        return {
            int(item["userId"]): item
            for item
            in data.get(
                "userPresences",
                []
            )
        }

    except requests.RequestException as exc:

        print(
            "[Roblox] presence request "
            f"failed: {exc}"
        )

        return {}


# ============================================================
# CHANGE DETECTION
# ============================================================

def build_change_message(
    label,
    username,
    old,
    new_presence_type,
    game_name,
    game_url,
    old_game_name,
    server_changed,
):

    new_type = int(
        new_presence_type
    )

    old_type = None

    if (
        old
        and old.get(
            "presence_type"
        ) is not None
    ):

        old_type = int(
            old["presence_type"]
        )

    if (
        label.lower()
        == username.lower()
    ):

        display = username

    else:

        display = (
            f"{label} "
            f"({username})"
        )


    # --------------------------------------------------------
    # FIRST SNAPSHOT
    # --------------------------------------------------------

    if old is None:

        if new_type in (2, 3):

            description = (
                f"**{display}** is currently "
                f"playing **"
                f"{game_name or 'Unknown Game'}"
                f"**"
            )

            if game_url:

                description += (
                    f"\n{game_url}"
                )

            return (
                "🎮 Current Status",
                description,
            )

        if new_type == 1:

            return (
                "🟢 Current Status",
                f"**{display}** "
                f"is currently online.",
            )

        return (
            "🔴 Current Status",
            f"**{display}** "
            f"is currently offline.",
        )


    # --------------------------------------------------------
    # SAME HIGH-LEVEL PRESENCE
    # --------------------------------------------------------

    if old_type == new_type:

        if new_type in (2, 3):

            if (
                old_game_name
                and game_name
                and old_game_name
                != game_name
            ):

                description = (
                    f"**{display}** switched "
                    f"from **{old_game_name}** "
                    f"to **{game_name}**"
                )

                if game_url:

                    description += (
                        f"\n{game_url}"
                    )

                return (
                    "🔄 Switched Game",
                    description,
                )

            if server_changed:

                description = (
                    f"**{display}** changed "
                    f"servers in **"
                    f"{game_name or 'Unknown Game'}"
                    f"**"
                )

                if game_url:

                    description += (
                        f"\n{game_url}"
                    )

                return (
                    "🔁 Changed Server",
                    description,
                )

        return None


    # --------------------------------------------------------
    # OFFLINE
    # --------------------------------------------------------

    if new_type == 0:

        return (
            "🔴 Went Offline",
            f"**{display}** went offline.",
        )


    # --------------------------------------------------------
    # ONLINE BUT NOT PLAYING
    # --------------------------------------------------------

    if new_type == 1:

        if old_type in (2, 3):

            return (
                "🟢 Left Game",
                f"**{display}** left the game "
                f"but is still online.",
            )

        return (
            "🟢 Came Online",
            f"**{display}** came online.",
        )


    # --------------------------------------------------------
    # PLAYING
    # --------------------------------------------------------

    if new_type in (2, 3):

        title = (
            "🎮 Started Playing"
        )

        if old_type == 0:

            title = (
                "🎮 Came Online & "
                "Started Playing"
            )

        description = (
            f"**{display}** is playing "
            f"**"
            f"{game_name or 'Unknown Game'}"
            f"**"
        )

        if game_url:

            description += (
                f"\n{game_url}"
            )

        return (
            title,
            description,
        )

    return None


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_API = (
    f"https://api.telegram.org/"
    f"bot{TELEGRAM_BOT_TOKEN}"
)


def telegram_send(
    text: str,
    chat_id: Optional[str] = None
):

    target = str(
        chat_id
        or TELEGRAM_CHAT_ID
    )

    try:

        response = HTTP.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": target,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )

        if not response.ok:

            print(
                "[Telegram] sendMessage failed "
                f"{response.status_code}: "
                f"{response.text[:300]}"
            )

    except requests.RequestException as exc:

        print(
            f"[Telegram] send failed: {exc}"
        )


def telegram_authorized(
    user_id: int
):

    return (
        not TELEGRAM_ADMIN_IDS
        or user_id in TELEGRAM_ADMIN_IDS
    )


def telegram_active_text():

    rows = get_active_rows()

    if not rows:

        return (
            "No tracked players "
            "are currently active."
        )

    lines = [
        "🟢 *Active tracked players:*"
    ]

    for row in rows:

        username = (
            row["username"]
            or f"User_{row['user_id']}"
        )

        if row["presence_type"] in (2, 3):

            lines.append(
                f"• {row['label']} "
                f"({username}) — 🎮 "
                f"{row['game_name'] or 'Unknown Game'}"
            )

        else:

            lines.append(
                f"• {row['label']} "
                f"({username}) — 🟢 Online"
            )

    return "\n".join(
        lines
    )


def telegram_tracked_text():

    users = get_tracked_users()

    if not users:

        return (
            "No tracked Roblox users."
        )

    return (
        "📋 *Tracked users:*\n"
        + "\n".join(
            f"• {u['label']} — "
            f"`{u['user_id']}`"
            for u in users
        )
    )


def telegram_polling_loop(
    discord_loop
):

    offset = 0

    print(
        "[Telegram] polling started"
    )

    while True:

        try:

            response = HTTP.get(
                f"{TELEGRAM_API}/getUpdates",
                params={
                    "timeout": 25,
                    "offset": offset,
                    "allowed_updates":
                        '["message"]',
                },
                timeout=35,
            )

            if not response.ok:

                print(
                    "[Telegram] getUpdates "
                    f"failed "
                    f"{response.status_code}: "
                    f"{response.text[:300]}"
                )

                time.sleep(5)

                continue


            updates = (
                response
                .json()
                .get(
                    "result",
                    []
                )
            )


            for update in updates:

                offset = (
                    update["update_id"]
                    + 1
                )

                message = (
                    update.get("message")
                    or {}
                )

                text = (
                    message
                    .get("text")
                    or ""
                ).strip()

                chat = (
                    message.get("chat")
                    or {}
                )

                sender = (
                    message.get("from")
                    or {}
                )

                chat_id = str(
                    chat.get("id")
                )

                sender_id = int(
                    sender.get(
                        "id",
                        0
                    )
                )


                if (
                    TELEGRAM_CHAT_ID
                    and chat_id
                    != str(
                        TELEGRAM_CHAT_ID
                    )
                ):

                    continue


                if not text.startswith(
                    "/"
                ):

                    continue


                command, *args = (
                    text.split()
                )

                command = (
                    command
                    .split(
                        "@",
                        1
                    )[0]
                    .lower()
                )


                # --------------------------------------------
                # START
                # --------------------------------------------

                if command == "/start":

                    telegram_send(
                        "✅ Roblox Presence "
                        "Tracker is online.\n\n"

                        "/active - active users\n"
                        "/tracked - tracked users\n"
                        "/check - check now\n"

                        "/track <user_id> [label]\n"
                        "/untrack <user_id>",
                        chat_id,
                    )


                # --------------------------------------------
                # ACTIVE
                # --------------------------------------------

                elif command == "/active":

                    telegram_send(
                        telegram_active_text(),
                        chat_id,
                    )


                # --------------------------------------------
                # TRACKED
                # --------------------------------------------

                elif command == "/tracked":

                    telegram_send(
                        telegram_tracked_text(),
                        chat_id,
                    )


                # --------------------------------------------
                # CHECK
                # --------------------------------------------

                elif command == "/check":

                    if not telegram_authorized(
                        sender_id
                    ):

                        telegram_send(
                            "❌ Not authorized.",
                            chat_id,
                        )

                        continue


                    future = (
                        asyncio
                        .run_coroutine_threadsafe(
                            run_presence_check(
                                "telegram"
                            ),
                            discord_loop,
                        )
                    )


                    try:

                        result = (
                            future.result(
                                timeout=30
                            )
                        )

                        telegram_send(
                            "✅ Check complete\n"
                            f"Checked: "
                            f"{result['checked']}\n"
                            f"Changes: "
                            f"{result['changed']}",
                            chat_id,
                        )

                    except Exception as exc:

                        telegram_send(
                            f"❌ Check failed: "
                            f"{exc}",
                            chat_id,
                        )


                # --------------------------------------------
                # TRACK
                # --------------------------------------------

                elif command == "/track":

                    if not telegram_authorized(
                        sender_id
                    ):

                        telegram_send(
                            "❌ Not authorized.",
                            chat_id,
                        )

                        continue


                    if (
                        not args
                        or not args[0].isdigit()
                    ):

                        telegram_send(
                            "Usage:\n"
                            "/track "
                            "<roblox_user_id> "
                            "[label]",
                            chat_id,
                        )

                        continue


                    uid = int(
                        args[0]
                    )

                    username = (
                        get_username(
                            uid
                        )
                    )

                    label = (
                        " ".join(
                            args[1:]
                        ).strip()
                        or username
                    )


                    add_tracked_user(
                        uid,
                        label,
                    )


                    telegram_send(
                        f"✅ Tracking "
                        f"{label} "
                        f"({username}) "
                        f"— {uid}",
                        chat_id,
                    )


                # --------------------------------------------
                # UNTRACK
                # --------------------------------------------

                elif command == "/untrack":

                    if not telegram_authorized(
                        sender_id
                    ):

                        telegram_send(
                            "❌ Not authorized.",
                            chat_id,
                        )

                        continue


                    if (
                        not args
                        or not args[0].isdigit()
                    ):

                        telegram_send(
                            "Usage:\n"
                            "/untrack "
                            "<roblox_user_id>",
                            chat_id,
                        )

                        continue


                    removed = (
                        remove_tracked_user(
                            int(
                                args[0]
                            )
                        )
                    )


                    telegram_send(
                        (
                            "✅ Removed."
                            if removed
                            else
                            "User is not tracked."
                        ),
                        chat_id,
                    )


        except Exception as exc:

            print(
                "[Telegram] polling error: "
                f"{exc}"
            )

            time.sleep(5)


# ============================================================
# DISCORD
# ============================================================

intents = discord.Intents.default()

client = discord.Client(
    intents=intents
)

tree = app_commands.CommandTree(
    client
)


_tracker_task = None

_ready_once = False


def discord_admin(
    interaction: discord.Interaction
):

    if DISCORD_ADMIN_USER_IDS:

        return (
            interaction.user.id
            in DISCORD_ADMIN_USER_IDS
        )


    perms = getattr(
        interaction.user,
        "guild_permissions",
        None,
    )

    return bool(
        perms
        and perms.manage_guild
    )


async def get_discord_channel():

    channel = client.get_channel(
        DISCORD_CHANNEL_ID
    )

    if channel is not None:
        return channel


    try:

        return await client.fetch_channel(
            DISCORD_CHANNEL_ID
        )

    except Exception as exc:

        print(
            "[Discord] fetch channel "
            f"failed: {exc}"
        )

        return None


async def notify_both(
    title,
    description
):

    channel = (
        await get_discord_channel()
    )


    if channel:

        try:

            embed = discord.Embed(
                title=title,
                description=description,
                timestamp=datetime.now(
                    timezone.utc
                ),
            )


            await channel.send(
                embed=embed
            )

        except Exception as exc:

            print(
                "[Discord] notification "
                f"failed: {exc}"
            )


    await asyncio.to_thread(
        telegram_send,
        f"{title}\n\n{description}",
    )


# ============================================================
# DISCORD COMMANDS
# ============================================================

@tree.command(
    name="track",
    description="Track a Roblox user",
)
@app_commands.describe(
    user_id="Roblox user ID",
    label="Optional display name",
)
async def track_command(
    interaction: discord.Interaction,
    user_id: str,
    label: Optional[str] = None,
):

    await interaction.response.defer(
        ephemeral=True
    )


    if not discord_admin(
        interaction
    ):

        await interaction.followup.send(
            "❌ You are not authorized.",
            ephemeral=True,
        )

        return


    if not user_id.isdigit():

        await interaction.followup.send(
            "❌ Roblox user_id "
            "must be numeric.",
            ephemeral=True,
        )

        return


    uid = int(
        user_id
    )


    username = (
        await asyncio.to_thread(
            get_username,
            uid,
        )
    )


    final_label = (
        label
        or username
    ).strip()


    await asyncio.to_thread(
        add_tracked_user,
        uid,
        final_label,
    )


    await interaction.followup.send(
        f"✅ Tracking "
        f"**{final_label}** "
        f"({username}) — "
        f"`{uid}`",
        ephemeral=True,
    )


@tree.command(
    name="untrack",
    description="Stop tracking a Roblox user",
)
async def untrack_command(
    interaction: discord.Interaction,
    user_id: str,
):

    await interaction.response.defer(
        ephemeral=True
    )


    if not discord_admin(
        interaction
    ):

        await interaction.followup.send(
            "❌ You are not authorized.",
            ephemeral=True,
        )

        return


    if not user_id.isdigit():

        await interaction.followup.send(
            "❌ Roblox user_id "
            "must be numeric.",
            ephemeral=True,
        )

        return


    removed = (
        await asyncio.to_thread(
            remove_tracked_user,
            int(user_id),
        )
    )


    await interaction.followup.send(
        (
            "✅ Removed."
            if removed
            else
            "That user was not tracked."
        ),
        ephemeral=True,
    )


@tree.command(
    name="tracked",
    description="List tracked Roblox users",
)
async def tracked_command(
    interaction: discord.Interaction
):

    await interaction.response.defer(
        ephemeral=True
    )


    users = (
        await asyncio.to_thread(
            get_tracked_users
        )
    )


    if not users:

        await interaction.followup.send(
            "No tracked Roblox users.",
            ephemeral=True,
        )

        return


    text = "\n".join(
        f"• **{u['label']}** — "
        f"`{u['user_id']}`"

        for u in users
    )


    await interaction.followup.send(
        text[:1900],
        ephemeral=True,
    )


@tree.command(
    name="active",
    description=(
        "Show tracked players "
        "currently online or playing"
    ),
)
async def active_command(
    interaction: discord.Interaction
):

    await interaction.response.defer()


    rows = (
        await asyncio.to_thread(
            get_active_rows
        )
    )


    if not rows:

        await interaction.followup.send(
            "No tracked players "
            "are currently active."
        )

        return


    lines = []


    for row in rows:

        username = (
            row["username"]
            or f"User_{row['user_id']}"
        )


        if (
            row["presence_type"]
            in (2, 3)
        ):

            lines.append(
                f"🎮 **{row['label']}** "
                f"({username}) — "
                f"{row['game_name'] or 'Unknown Game'}"
            )

        else:

            lines.append(
                f"🟢 **{row['label']}** "
                f"({username}) — Online"
            )


    await interaction.followup.send(
        "\n".join(
            lines
        )[:1900]
    )


@tree.command(
    name="check",
    description="Run a presence check now",
)
async def check_command(
    interaction: discord.Interaction
):

    await interaction.response.defer(
        ephemeral=True
    )


    if not discord_admin(
        interaction
    ):

        await interaction.followup.send(
            "❌ You are not authorized.",
            ephemeral=True,
        )

        return


    try:

        result = (
            await run_presence_check(
                "discord"
            )
        )


        await interaction.followup.send(
            f"✅ Checked "
            f"**{result['checked']}** "
            f"players.\n"
            f"Changes: "
            f"**{result['changed']}**",
            ephemeral=True,
        )


    except Exception as exc:

        await interaction.followup.send(
            f"❌ Check failed: "
            f"`{exc}`",
            ephemeral=True,
        )


# ============================================================
# DISCORD READY
# ============================================================

@client.event
async def on_ready():

    global _tracker_task
    global _ready_once


    print(
        "[Discord] Logged in as "
        f"{client.user} "
        f"({client.user.id})"
    )


    try:

        await client.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Roblox players",
            ),
        )

    except Exception as exc:

        print(
            "[Discord] presence set "
            f"failed: {exc}"
        )


    if not _ready_once:

        try:

            if DISCORD_GUILD_ID:

                guild = discord.Object(
                    id=DISCORD_GUILD_ID
                )

                tree.copy_global_to(
                    guild=guild
                )

                synced = await tree.sync(
                    guild=guild
                )

            else:

                synced = await tree.sync()


            print(
                "[Discord] synced "
                f"{len(synced)} commands"
            )


        except Exception as exc:

            print(
                "[Discord] command sync "
                f"failed: {exc}"
            )


        if (
            _tracker_task is None
            or _tracker_task.done()
        ):

            _tracker_task = (
                asyncio.create_task(
                    tracker_loop()
                )
            )


        _ready_once = True


# ============================================================
# TRACKER
# ============================================================

_check_lock = asyncio.Lock()


async def run_presence_check(
    source="timer"
):

    async with _check_lock:

        users = (
            await asyncio.to_thread(
                get_tracked_users
            )
        )


        if not users:

            print(
                "[Tracker] no tracked users"
            )

            return {
                "checked": 0,
                "changed": 0,
            }


        user_ids = [
            int(
                user["user_id"]
            )
            for user in users
        ]


        # ----------------------------------------------------
        # LOAD OLD STATES ONCE
        # ----------------------------------------------------

        old_states = (
            await asyncio.to_thread(
                get_all_saved_states,
                user_ids,
            )
        )


        # ----------------------------------------------------
        # ROBLOX PRESENCE REQUEST
        # ----------------------------------------------------

        presence_map = (
            await asyncio.to_thread(
                fetch_presences,
                user_ids,
            )
        )


        changed = 0
        actually_checked = 0


        for user in users:

            uid = int(
                user["user_id"]
            )

            label = user["label"]


            presence = (
                presence_map.get(
                    uid
                )
            )


            if presence is None:

                print(
                    "[Tracker] Roblox omitted "
                    f"user {uid}; skipping"
                )

                continue


            actually_checked += 1


            presence_type = int(
                presence.get(
                    "userPresenceType"
                )
                or 0
            )


            place_id = (
                presence.get(
                    "placeId"
                )
            )


            universe_id = (
                presence.get(
                    "universeId"
                )
            )


            game_id = (
                presence.get(
                    "gameId"
                )
            )


            last_location = (
                presence.get(
                    "lastLocation"
                )
                or ""
            ).strip()


            # ------------------------------------------------
            # NORMALIZE IDS
            # ------------------------------------------------

            try:

                place_id = (
                    int(place_id)
                    if place_id
                    else None
                )

            except (
                TypeError,
                ValueError
            ):

                place_id = None


            try:

                universe_id = (
                    int(universe_id)
                    if universe_id
                    else None
                )

            except (
                TypeError,
                ValueError
            ):

                universe_id = None


            # ------------------------------------------------
            # OLD STATE
            # ------------------------------------------------

            old = (
                old_states.get(
                    uid
                )
            )


            # ------------------------------------------------
            # DEBUG CURRENT ROBLOX GAME DATA
            # ------------------------------------------------

            if presence_type in (2, 3):

                print(
                    f"[Roblox] {uid} in-game: "
                    f"lastLocation="
                    f"{last_location!r} "
                    f"placeId={place_id} "
                    f"universeId={universe_id} "
                    f"gameId={game_id}"
                )


            # ------------------------------------------------
            # USERNAME
            # ------------------------------------------------

            username = None


            if old:

                username = (
                    old.get(
                        "username"
                    )
                )


            if not username:

                username = (
                    await asyncio.to_thread(
                        get_username,
                        uid,
                    )
                )


            # ------------------------------------------------
            # GAME INFO
            # ------------------------------------------------

            game_name = None
            game_url = None


            if (
                presence_type in (2, 3)
            ):

                game_name, game_url = (
                    await asyncio.to_thread(
                        get_game_info,
                        place_id,
                        universe_id,
                        last_location,
                    )
                )


            # ------------------------------------------------
            # OLD GAME
            # ------------------------------------------------

            old_game_name = (
                old.get(
                    "game_name"
                )
                if old
                else None
            )


            old_game_id = (
                old.get(
                    "game_id"
                )
                if old
                else None
            )


            # ------------------------------------------------
            # SERVER CHANGE
            # ------------------------------------------------

            server_changed = bool(

                old

                and presence_type in (2, 3)

                and int(
                    old.get(
                        "presence_type"
                    )
                    or 0
                )
                in (2, 3)

                and old.get(
                    "place_id"
                )
                == place_id

                and old_game_id

                and game_id

                and str(
                    old_game_id
                )
                != str(
                    game_id
                )
            )


            # ------------------------------------------------
            # BUILD NOTIFICATION
            # ------------------------------------------------

            change = (
                build_change_message(

                    label=label,

                    username=username,

                    old=old,

                    new_presence_type=
                        presence_type,

                    game_name=
                        game_name,

                    game_url=
                        game_url,

                    old_game_name=
                        old_game_name,

                    server_changed=
                        server_changed,
                )
            )


            # ------------------------------------------------
            # SAVE CURRENT STATE
            # ------------------------------------------------

            await asyncio.to_thread(
                save_state,

                uid,

                username,

                presence_type,

                place_id,

                universe_id,

                (
                    str(game_id)
                    if game_id
                    else None
                ),

                game_name,
            )


            # ------------------------------------------------
            # SEND
            # ------------------------------------------------

            if change:

                changed += 1

                await notify_both(
                    change[0],
                    change[1],
                )


        print(
            f"[Tracker] source={source} "
            f"checked={actually_checked} "
            f"changed={changed}"
        )


        return {
            "checked":
                actually_checked,

            "changed":
                changed,
        }


async def tracker_loop():

    await asyncio.sleep(
        2
    )


    while not client.is_closed():

        started = (
            time.monotonic()
        )


        try:

            await run_presence_check(
                "timer"
            )

        except Exception as exc:

            print(
                "[Tracker] check failed: "
                f"{exc}"
            )


        elapsed = (
            time.monotonic()
            - started
        )


        wait = max(
            1,
            CHECK_INTERVAL
            - elapsed,
        )


        await asyncio.sleep(
            wait
        )


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

web = FastAPI()


@web.get("/")
def root():

    return {
        "ok": True,

        "service":
            "Roblox Discord + Telegram "
            "Presence Bot",

        "discord":
            (
                str(client.user)
                if client.user
                else "connecting"
            ),

        "check_interval":
            CHECK_INTERVAL,
    }


@web.get("/health")
def health():

    return {
        "ok": True
    }


def run_web_server():

    uvicorn.run(
        web,
        host="0.0.0.0",
        port=PORT,
        log_level="warning",
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "[Startup] preparing database..."
    )

    init_db()


    print(
        "[Startup] starting "
        "Render health server..."
    )

    threading.Thread(
        target=run_web_server,
        daemon=True,
    ).start()


    async def runner():

        loop = (
            asyncio.get_running_loop()
        )


        print(
            "[Startup] starting "
            "Telegram polling..."
        )

        threading.Thread(
            target=telegram_polling_loop,
            args=(loop,),
            daemon=True,
        ).start()


        print(
            "[Startup] connecting Discord..."
        )

        await client.start(
            DISCORD_BOT_TOKEN
        )


    asyncio.run(
        runner()
    )


if __name__ == "__main__":
    main()
