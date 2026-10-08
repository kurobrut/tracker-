# Roblox Presence Tracker — Render + Discord + Telegram

This version is designed to run continuously on Render.

## What it does

- Keeps a real Discord bot connected to Discord Gateway, so the bot shows Online.
- Checks tracked Roblox users every `CHECK_INTERVAL` seconds.
- Uses PostgreSQL to remember each user's previous status.
- Sends Discord + Telegram notifications for:
  - online
  - offline
  - started playing
  - switched games
  - changed server
  - left a game but stayed online
- Discord commands:
  - `/track`
  - `/untrack`
  - `/tracked`
  - `/active`
  - `/check`
- Telegram commands:
  - `/start`
  - `/track <roblox_user_id> [label]`
  - `/untrack <roblox_user_id>`
  - `/tracked`
  - `/active`
  - `/check`

## 1. Discord bot

In the Discord Developer Portal:

1. Create an Application.
2. Open **Bot** and create/copy the Bot Token.
3. Invite the bot with:
   - `bot`
   - `applications.commands`
4. Give it:
   - View Channels
   - Send Messages
   - Embed Links

Copy:
- Bot Token
- Server/Guild ID
- notification Channel ID
- your Discord User ID

## 2. Telegram bot

1. Open Telegram and message `@BotFather`.
2. Run `/newbot`.
3. Copy the Bot Token.
4. Send any message to your new bot.
5. To learn your Telegram numeric user/chat ID, you can temporarily open:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   after sending the bot a message.
6. Use that numeric chat ID as `TELEGRAM_CHAT_ID`.
7. Put your own Telegram user ID in `TELEGRAM_ADMIN_IDS`.

For a group:
- add the bot to the group
- send a command in the group
- use that group's chat ID for `TELEGRAM_CHAT_ID`

## 3. PostgreSQL

Use your existing external PostgreSQL URL.

Example:

`postgresql://user:password@host:5432/database?sslmode=require`

The bot automatically creates:

- `tracked_users`
- `presence_state`
- `place_cache`

## 4. Render

Create a new **Web Service** from the GitHub repository.

Use:

- Build Command: `pip install -r requirements.txt`
- Start Command: `python main.py`

The included `render.yaml` has these settings already.

Add these Environment Variables in Render:

### Required

`DATABASE_URL`

`DISCORD_BOT_TOKEN`

`DISCORD_CHANNEL_ID`

`DISCORD_GUILD_ID`

`TELEGRAM_BOT_TOKEN`

`TELEGRAM_CHAT_ID`

### Recommended admin restrictions

`DISCORD_ADMIN_USER_IDS`

Example:
`123456789012345678,987654321098765432`

`TELEGRAM_ADMIN_IDS`

Example:
`123456789,987654321`

### Tracker timing

`CHECK_INTERVAL=30`

Do not set it extremely low. 15–30 seconds is a reasonable starting point.

## 5. First test

After Render says the service is Live:

1. Discord bot should show Online.
2. Run `/track` in Discord, or:
   `/track 123456789 Ali`
   in Telegram.
3. Run `/tracked`.
4. Run `/check`.
5. Run `/active`.

The first successful check after a user is added sends that user's current status.
After that, messages are only sent when their saved state changes.

## 6. Render health URL

The service exposes:

- `/`
- `/health`

Example:

`https://your-service.onrender.com/health`

It should return:

`{"ok":true}`

The HTTP server is there because this is deployed as a Render Web Service. The Discord bot and Telegram poller run continuously in the same process.

## Notes

- If the Render service is suspended/sleeping by your plan, the bots cannot stay continuously connected while it is asleep. Use an always-on Render instance/plan if you need truly continuous tracking.
- Do not share your Discord bot token, Telegram bot token, or PostgreSQL password.
- `TELEGRAM_CHAT_ID` determines where Telegram notifications are delivered.
- `TELEGRAM_ADMIN_IDS` restricts Telegram management commands. If left empty, anyone in the configured Telegram chat can run management commands.
