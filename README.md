# Lucy — Your Discord AI Assistant

A Python Discord bot with a customizable AI personality (powered by NVIDIA NIM, free tier),
full moderation, and server utilities. Both slash (`/`) and prefix (`!`) commands supported.

## 1. Create the Discord bot
1. Go to https://discord.com/developers/applications → **New Application**.
2. Go to the **Bot** tab → click **Reset Token** → copy it (this is `DISCORD_TOKEN`).
3. Under **Privileged Gateway Intents**, enable:
   - Presence Intent (optional)
   - Server Members Intent (required)
   - Message Content Intent (required)
4. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: `Administrator` (simplest for full-power admin bot)
   - Copy the generated URL and open it to invite Lucy to your server.

## 2. Get an NVIDIA NIM API key (free)
1. Go to https://build.nvidia.com → sign in → pick a chat model (e.g. `meta/llama-3.3-70b-instruct`).
2. Click **Get API Key** → copy it (this is `NVIDIA_API_KEY`). NIM has a generous free tier.

## 3. Configure environment
Copy `.env.example` to `.env` and fill in:
```
DISCORD_TOKEN=...
NVIDIA_API_KEY=...
NIM_MODEL=meta/llama-3.3-70b-instruct
DEFAULT_PREFIX=!
```

## 4. Run locally (optional test)
```bash
pip install -r requirements.txt
python main.py
```

## 5. Deploy free on Railway
1. Push this folder to a GitHub repo.
2. Go to https://railway.app → **New Project → Deploy from GitHub repo**.
3. Add the environment variables from `.env` in Railway's **Variables** tab.
4. Railway auto-detects the `Procfile` and runs `python main.py` as a worker.

⚠️ **Note on SQLite + Railway free tier:** the filesystem resets on redeploy unless you attach a
persistent Volume (Railway supports this in the free tier — mount it at `/app/data`). Without a
volume, warnings/personality/chat memory reset each time you redeploy (bot still works fine day-to-day).

## 6. Configure Lucy in your server
Run these once, in Discord:
- `/setpersonality` — customize name, age, traits, backstory, speaking style, boundaries (one field at a time)
- `/profile` — view Lucy's current profile
- `/setchattrigger` — choose when she jumps into chat (mention / dedicated channel / name-said / all)
- `/setchatchannel` — pick her dedicated chat channel (if using that mode)
- `/setlogchannel` — where moderation actions get logged
- `/setwelcome` — welcome channel + message for new members

## What's included
- **Moderation:** ban, kick, mute/unmute (timeout), unban, warn, warnings, purge — logged to your log channel
- **Utility:** welcome messages, role give/remove, ticket system, server/user info
- **AI Chat:** Lucy responds in character using NVIDIA NIM, remembers last ~12 messages per channel
- **Personality:** fully editable per-server via slash commands, no code editing needed

## Adding more later
This is a clean foundation — economy/games, leveling, reaction roles, and auto-mod filters can all
be added as new cogs in `cogs/` without touching existing code.
# discord-assistant-replica-
# discord-assistant-replica-
# discord-assistant-replica-
