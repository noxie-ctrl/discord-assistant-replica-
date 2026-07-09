# Lucy — Your Discord AI Assistant

A Python Discord bot with a customizable AI personality (NVIDIA NIM as the main chat
engine, Groq as a background/fallback engine), full moderation, server utilities, and
mini-games. Both slash (`/`) and prefix (`!`) commands supported.

## 1. Create the Discord bot
1. Go to https://discord.com/developers/applications → **New Application**.
2. Go to the **Bot** tab → click **Reset Token** → copy it (this is `DISCORD_TOKEN`).
3. Under **Privileged Gateway Intents**, enable:
   - Server Members Intent (required)
   - Message Content Intent (required)
4. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: `Administrator` (simplest for full-power admin bot)
   - Copy the generated URL and open it to invite Lucy to your server.

## 2. Get API keys (both free)
- **NVIDIA NIM** (main chat engine): https://build.nvidia.com → sign in → **Get API Key**
  → this is `NVIDIA_API_KEY`. Generous free tier; Lucy defaults to the strongest available
  model (`mistralai/mistral-large-3-675b-instruct-2512`) with automatic fallback to two
  other models if it's ever unavailable.
- **Groq** (background tasks + emergency fallback): https://console.groq.com → **API Keys**
  → create two keys, `GROQ_API_KEY_1` and `GROQ_API_KEY_2`. Groq is used for vent-channel
  triage, long-term memory summarization, and the daily news digest — keeping that traffic
  off the main NIM quota — and as a last-resort fallback for main chat if NIM is fully down.
  Optional but recommended; Lucy works without it, just with less background-task headroom.

## 3. Configure environment
Copy `.env.example` to `.env` and fill in:
```
DISCORD_TOKEN=...
NVIDIA_API_KEY=...
GROQ_API_KEY_1=...
GROQ_API_KEY_2=...
OWNER_ID=1462759265864519722
DATABASE_URL=...   # Railway Postgres reference, e.g. ${{Postgres.DATABASE_URL}}
```

Optional OpenRouter (vision + 4th-tier main-chat fallback):
```
OPENROUTER_API_KEY=...       # your primary OpenRouter key (optional)
OPENROUTER_API_KEY_2=...     # optional secondary key for redundancy
OPENROUTER_MODEL=...         # optional: pin a specific model instead of the default free router
```
Defaults to `openrouter/free` — OpenRouter's own free-model router, which auto-selects a
currently-free model (including a vision-capable one when the request has an image) at
$0/token, no credits required. Free model *names* on OpenRouter rotate often, so this
avoids pinning one that goes stale; set `OPENROUTER_MODEL` if you'd rather pin something
specific. If you don't set OpenRouter keys, vision and the 4th fallback tier stay off and
Lucy still works normally on NIM + Groq.

Optional GitHub repo updates (commits + PRs posted to a channel):
```
GITHUB_TOKEN=...                    # optional but recommended, see below
GITHUB_POLL_INTERVAL_MINUTES=5      # optional, defaults to 5
GITHUB_DIGEST_WEEKDAY=0             # optional, defaults to 0 (Monday); 0=Mon ... 6=Sun
```
Works with no token at all for public repos (GitHub allows 60 unauthenticated
requests/hour, shared across every repo linked on every server this bot is in — fine for
a repo or two, tight beyond that). Create a token at
https://github.com/settings/tokens (a classic token with the `repo` scope works for
private repos; no scopes are needed for public-repo read access) to raise that to
5,000 requests/hour and to let Lucy see private repos. Then in Discord:
```
/githublink repo:owner/repo [channel:#dev-updates]   # admin/mod only
/githubunlink repo:owner/repo                        # admin/mod only
/githublinks                                         # anyone — lists what's linked
/githubdigest [channel:#dev-updates]                 # admin/mod only — set/clear weekly recap
/githubdigestnow [channel:#dev-updates]               # anyone — post a recap right now
```
Lucy checks every linked repo on a timer (`GITHUB_POLL_INTERVAL_MINUTES`) and posts an
embed to the linked channel for new commits and for PRs being opened, merged, or closed.
Linking a repo baselines it at the current HEAD, so you get updates going forward rather
than a dump of the whole commit history. A few things aimed at making Discord the actual
hub for a group project rather than just a notification feed:
- **AI summaries** — commit batches and new PRs get a short, plain-English "what this
  actually does" blurb (Groq/NIM, same engines as chat) instead of raw commit messages,
  so non-technical teammates can follow along too. New PRs also get a rough size label
  (XS/S/M/L/XL) based on lines changed.
- **Auto-threads on new PRs** — each new PR spins up a Discord thread on its
  announcement message, so review discussion has a home instead of spreading across the
  main channel.
- **Weekly recap** (`/githubdigest`) — once a week (`GITHUB_DIGEST_WEEKDAY`), Lucy posts
  one consolidated AI write-up of everything that happened across every linked repo —
  meant to be read in ~10 seconds to catch up on the week. `/githubdigestnow` posts one
  on demand (e.g. before a meeting) without disturbing the weekly schedule.
- **Ask Lucy about activity** — since every update is logged, you can just ask her things
  like "what changed in the database repo this week?" in normal chat and she'll answer
  from that history instead of guessing.
- **Ask Lucy about the actual codebase** — she can also read the repo itself: broad
  questions ("what does this project do", "how's it organized") pull the README and
  top-level file layout; specific questions ("how is auth handled", "what's in
  utils/database.py") search the code or read a named file directly and answer from the
  real content. Code search (not file reading) specifically requires `GITHUB_TOKEN` —
  GitHub's search API rejects unauthenticated requests — everything else works without
  one, just at the lower public rate limit.

Testing the vision feature locally
---------------------------------
If you set `OPENROUTER_API_KEY` (and optionally `OPENROUTER_API_KEY_2`), you can test
the image description path with the `/describeimage` admin command (works even if the
bot isn't set to auto-describe attachments). Example:

```
# in Discord, as an admin or mod:
/describeimage image_url:https://example.com/pic.jpg
```

Locally, run the bot in a venv and ensure env vars are set in your shell or `.env`:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export OPENROUTER_API_KEY="your_key_here"
python main.py
```

## 4. Run locally (optional test)
```bash
pip install -r requirements.txt
python main.py
```

## 5. Deploy free on Railway
1. Push this folder to a GitHub repo.
2. Go to https://railway.app → **New Project → Deploy from GitHub repo**.
3. Add a Postgres plugin, and reference its `DATABASE_URL` on the worker service.
4. Add the rest of the environment variables from `.env` in Railway's **Variables** tab.
5. Railway auto-detects the `Procfile` and runs `python main.py` as a worker.

## 6. Configure Lucy in your server
Run these once, in Discord:
- `/setpersonality` — customize name, age, traits, backstory, speaking style, boundaries
- `/resetpersonality` — reset to `personality_default.json`. Since your server already has
  a saved personality row, editing `personality_default.json` alone won't change how Lucy
  talks on your existing server — run this once after a deploy that changes the default to
  actually apply it (new servers get the current default automatically on first message).
- `/profile` — view Lucy's current profile
- `/setchattrigger` — choose when she jumps into chat (mention / dedicated channel / both / name-said / all)
- `/setchatchannel` — pick her dedicated chat channel (if using that mode)
- `/setventchannel` — a channel she quietly watches, flagging the owner if someone seems
  like they need real human support (not a public reply — a private heads-up)
- `/disableventchannel` / `/ventstatus` — turn vent watching off/on without redeploying
- `/setlogchannel` — where moderation actions get logged
- `/setwelcome` — welcome channel + message for new members

## What's included
- **Moderation:** ban, kick, mute/unmute (timeout), unban, warn, warnings, purge — logged to your log channel
- **Utility:** welcome messages, role give/remove, ticket system, server/user info
- **AI Chat:** Lucy talks like an active member of the server, not a generic assistant —
  she adapts tone (banter vs. sincerity vs. Hinglish), has real opinions on niche/gaming/anime/
  internet-culture topics, knows the current date/time in IST, and stays casually aware of
  real current headlines. She naturally warms up to people over time (acquaintance → friend →
  close friend → best friend, based on how much you've talked and how well it's gone), while
  the owner always gets top priority and candor.
- **Vent support:** quietly flags the owner (privately, via DM) when someone genuinely seems
  to need a real person to check in — separate from normal banter/venting.
- **Games:** Tic-Tac-Toe, Connect Four (both 2-player), Rock-Paper-Scissors, Guess the Number
  (solo / vs a member / vs Lucy), and Trivia (solo or open free-for-all, multiple categories
  including gaming/anime/sports/internet culture) — all feeding a shared coin economy with
  `/balance`, `/leaderboard`, `/gamestats`.
- **Personality:** fully editable per-server via slash commands, no code editing needed.

## Engines
- **NVIDIA NIM** (`mistralai/mistral-large-3-675b-instruct-2512` → `mistralai/mistral-nemotron`
  → `meta/llama-3.3-70b-instruct`) handles main conversations and tool calls.
- **Groq** (`llama-3.1-8b-instant` for cheap background work, `llama-3.3-70b-versatile` for
  the news digest and emergency main-chat fallback) round-robins across your two keys so a
  single key's rate limit doesn't stall anything.
- **OpenRouter** (optional) handles vision, and is the 4th-tier main-chat fallback — tried
  only if every NIM candidate *and* Groq have failed, so a simultaneous NIM+Groq outage
  still doesn't take Lucy fully offline.