# Vercel Discord Roblox Presence Bot

A real Discord bot (Bot Token + Channel ID) that checks tracked Roblox users and posts an update whenever their presence changes.

## Updates it sends
- Offline -> online
- Online -> started a game
- Game A -> Game B
- Same game -> different Roblox server
- In game -> online but not playing
- Online/in game -> offline

State is stored in PostgreSQL so separate Vercel invocations do not resend the same event.

## Important Vercel limitation
Vercel does not keep an infinite Python loop alive. `vercel.json` runs `/api/check` once per minute using Vercel Cron. That means changes are normally detected within about 1 minute on plans that permit minute-level cron scheduling.

If you need checks every 5-30 seconds, run the same checker on an always-on service such as Render/Railway/Fly.io instead of Vercel.

## Environment variables
DATABASE_URL
DISCORD_BOT_TOKEN
DISCORD_CHANNEL_ID
DISCORD_APPLICATION_ID
DISCORD_PUBLIC_KEY
DISCORD_GUILD_ID
CRON_SECRET
ADMIN_KEY
ADMIN_USER_IDS

## Slash commands
- /track user_id label
- /untrack user_id
- /tracked
- /check

## Vercel cron
`vercel.json` calls `/api/check` every minute:

    * * * * *
