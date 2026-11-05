# Ben's Butt Spreader

A Discord bot that watches **every member** for spam and pointless messages, and punishes repeat offenders with timeouts that get longer each time, ending in a kick with an invite back. It includes a web dashboard showing every timeout and kick and why it happened.

## What counts as an offense

| Check | Triggers when | Uses AI? |
|---|---|---|
| **GIF spam** | 4+ GIFs or stickers within 60 seconds (Tenor/Giphy links, `.gif` uploads, gifv embeds) | No |
| **Repeating** | 3+ near-identical messages within 2 minutes ("BRO LOOK" / "bro look!!" / "bro look!!!") | No |
| **Not contributing** | 3 messages judged as noise within 10 minutes | Yes, Google Gemini (free tier) |

The owner, admins, mods (anyone who can manage messages or time members out), bots, and any roles in `EXEMPT_ROLE_IDS` are never punished.

### How the AI decides a message is "stupid"

For each text message (3+ characters, not a GIF), the bot sends the AI:

1. Its instructions (`SYSTEM_PROMPT` in [`judge.py`](judge.py)).
2. The last 15 messages in that channel from everyone, so it knows what the conversation is about.
3. The new message.

The AI must answer in a fixed JSON format with one of three verdicts plus a short reason:

- `ok`: normal chat, jokes, short replies like "lol", genuine questions, changing the subject now and then.
- `low_effort`: gibberish, keyboard mashing, emoji spam, or a random question or remark that ignores what everyone's talking about.
- `disruptive`: trolling, baiting, or deliberately derailing.

Anything other than `ok` is a **strike**. A single strike does nothing; 3 strikes within 10 minutes is one offense. That way one misjudged message never gets anyone punished. If the AI can't be reached, gives a broken answer, or the per-minute cap is hit, the message is just skipped.

## Punishment ladder

Every offense, whatever the type, moves the member one step up:

| Offense | Punishment |
|---|---|
| 1st | 10 second timeout |
| 2nd | 20 second timeout |
| 3rd | 30 second timeout |
| 4th and later | Kick. They get a DM with an invite; if they rejoin within 10 minutes they're kicked again, and after that their roles are restored. |

After 3 days with no offenses, they start again at the bottom. You can also reset anyone by hand with `/spreader reset` or the **Reset** buttons on the dashboard. Change the ladder with `PUNISHMENTS=10s,20s,30s,kick` and the reset time with `OFFENSE_RESET_HOURS=72` in `.env` (any list of durations like `45s`, `5m`, `2h`, `1d`, with `kick` anywhere).

Bots can't put someone back into a server after kicking them; Discord only allows that with the user's own login ([Add Guild Member](https://docs.discord.com/developers/resources/guild#add-guild-member)). That's why the kick step DMs an invite instead.

## Dashboard

While the bot runs, open **http://localhost:8080** on the same computer. It shows:

- Counts for the last 24 hours, timeouts, kicks, and all time
- Which checks are catching people, and who's been punished most this week
- Everyone's current offense count and when it resets, with a **Reset** button for each person and **Reset everyone**
- Every punishment: when, who, timeout length or kick, which offense number, the reason, the messages that caused it, and the channel
- Search by username or ID, and filter by reason or action. It refreshes every 15 seconds.

History is saved in `spamguard.db`, so it survives restarts. To look at the dashboard without the bot running, use `python dashboard.py`.

The dashboard is only reachable from the computer the bot runs on. To open it to other devices, set `DASHBOARD_HOST=0.0.0.0` **and** a `DASHBOARD_PASSWORD` (the username is `admin`).

## Setup

### 1. Create the bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications), click **New Application**, and name it.
2. On **General Information**, copy the **Application ID**.
3. Under **Bot**, click **Reset Token** and copy the token. Keep it secret.
4. On the same page, under **Privileged Gateway Intents**, turn on **Server Members Intent** and **Message Content Intent**, then save.

### 2. Invite it to your server

Open this link with your Application ID in place of `YOUR_APP_ID`:

```
https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot+applications.commands&permissions=1099780140035
```

This grants Timeout Members, Kick Members, Manage Messages, Manage Roles, Create Invite, Read Message History, Send Messages and View Channels.

Then in **Server Settings → Roles**, drag the bot's role **above** every role it should be able to punish. Discord won't let a bot punish anyone with a higher role.

### 3. Get a free Gemini API key

Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey), sign in with a Google account, and click **Create API key**. No credit card needed.

The free tier has per-minute and daily limits. The bot caps itself at 12 AI checks a minute (`AI_MAX_PER_MINUTE`); in a busy server, some messages skip the AI check, while GIF and repeat detection always run. Google may use free-tier requests to improve its products, so your server's messages are sent to Google.

Alternatives: `AI_PROVIDER=claude` with an `ANTHROPIC_API_KEY` uses Claude Haiku instead (paid API credit, about $1 per 10,000 messages; a Claude Pro subscription doesn't cover it), or `AI_ENABLED=false` turns the AI check off.

### 4. Configure and run

Needs Python 3.10 or newer (`brew install python@3.12` on a Mac).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`: paste your `DISCORD_TOKEN` and `GEMINI_API_KEY`, and your server ID as `GUILD_ID` (turn on **Settings → Advanced → Developer Mode**, then right-click the server icon → **Copy Server ID**). Every other setting is explained in `.env.example`. Then:

```bash
python bot.py
```

The bot only works while it's running. To keep it online 24/7, run it on an always-on machine or a host such as Railway, Fly.io or a cheap VPS.

## Slash commands

Only people with the **Timeout Members** permission can use these.

| Command | Does |
|---|---|
| `/spreader history @user` | Their offense count, AI strikes, next punishment, and last 5 punishments |
| `/spreader reset @user` | Resets their offense count to zero (a timeout they're in keeps running) |
| `/spreader resetall` | Resets everyone's offense count in the server |
| `/spreader pardon @user` | Ends their timeout and resets their offense count to zero |

## Tests

```bash
python -m unittest -v
```

## Files

- `bot.py`: Discord events, punishments, slash commands
- `detectors.py`: GIF, repeat and strike logic
- `escalation.py`: the punishment ladder
- `judge.py`: the AI "is this contributing?" check (Gemini or Claude)
- `storage.py`: SQLite history and offense counts
- `dashboard.py`, `dashboard.html`: the web dashboard
