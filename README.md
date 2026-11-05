# SpamGuard

A Discord bot that watches specific users and puts them in a 3-minute timeout when they:

- **Spam GIFs**: 4+ GIFs or stickers within 60 seconds (Tenor/Giphy links, `.gif` uploads, gifv embeds)
- **Repeat themselves**: 3+ near-identical messages within 2 minutes (catches "BRO LOOK" / "bro look!!" / "bro look!!!")
- **Don't add anything**: each message is judged by Claude Haiku against the recent chat. Three "not contributing" verdicts within 10 minutes means a timeout. Haiku is very cheap, about $0.0001 per message checked.

Every number above can be changed in `.env`.

## Timeout vs. kick

You asked for "kick for 3 minutes, then add them back". Discord doesn't let a bot add someone back to a server after a kick. That needs the user's own OAuth login with the `guilds.join` scope ([Add Guild Member](https://docs.discord.com/developers/resources/guild#add-guild-member)). So there are two modes:

| `ACTION=` | What happens |
|---|---|
| `timeout` (default) | Uses Discord's built-in [timeout](https://support.discord.com/hc/en-us/articles/4413305239191-Time-Out-FAQ). They stay in the server but can't type, react or join voice for 3 minutes, and it lifts on its own. This is what mod bots like Dyno, MEE6 and Carl-bot use for temporary punishments. |
| `kick` | Kicks them and DMs a rejoin invite first. If they rejoin before the 3 minutes are up, they're kicked again. When they rejoin after it, their roles are given back. |

## Setup

### 1. Create the bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications), click **New Application**, and name it.
2. Under **Bot**, click **Reset Token** and copy the token. Keep it secret.
3. On the same page, under **Privileged Gateway Intents**, turn on **Message Content Intent** and **Server Members Intent**.
4. Under **OAuth2 → URL Generator**, tick the scopes `bot` and `applications.commands`, then these bot permissions:
   - **Moderate Members** (timeouts)
   - **Manage Messages** (deleting the spam)
   - **Read Message History**, **Send Messages**, **View Channels**
   - Only for `ACTION=kick`: **Kick Members**, **Create Instant Invite**, **Manage Roles**
5. Open the generated URL and add the bot to your server.
6. In **Server Settings → Roles**, drag the bot's role **above** the role of anyone it should punish. Discord won't let a bot time out the server owner, admins, or anyone with a higher role.

### 2. Get a Claude API key

Create one at [console.anthropic.com](https://console.anthropic.com/settings/keys). To skip the AI check entirely, set `AI_ENABLED=false`; GIF and repeat detection still work.

### 3. Configure and run

Needs Python 3.10 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`: paste your `DISCORD_TOKEN` and `ANTHROPIC_API_KEY`, and put the user ID to watch in `TARGET_USER_IDS`. To copy an ID, turn on **Settings → Advanced → Developer Mode**, then right-click the user and choose **Copy User ID**. Then:

```bash
python bot.py
```

The bot has to stay running to work. To keep it online 24/7, run it on a small always-on machine or a host such as Railway, Fly.io or a cheap VPS.

## Slash commands

Only people with the **Timeout Members** permission can use these.

| Command | Does |
|---|---|
| `/spamguard watch @user` | Start watching someone (saved to `watched.json`) |
| `/spamguard unwatch @user` | Stop watching them |
| `/spamguard list` | Who's watched, and their current AI strike count |
| `/spamguard pardon @user` | End their timeout early and clear their strikes |

Set `GUILD_ID` in `.env` so the commands show up right away. Without it, Discord can take a while to show them.

## Tuning the AI

The rules Claude follows are in `SYSTEM_PROMPT` in [`judge.py`](judge.py). By default it's lenient: jokes, banter and short replies like "lol" are fine, and only gibberish, random non-sequiturs, trolling and derailing count as strikes. Each strike is logged to the console with the reason, so watch the first few hours. If it's too soft or too harsh, edit the prompt or change `AI_STRIKES`.

## Tests

```bash
python -m unittest -v
```

## Files

- `bot.py`: Discord events, punishment, slash commands
- `detectors.py`: GIF, repeat and strike logic (no Discord code, so it's unit tested)
- `judge.py`: the Claude Haiku "is this contributing?" check
