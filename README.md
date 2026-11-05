# SpamGuard

A Discord bot that watches specific users and puts them in a 3-minute timeout when they:

- **Spam GIFs**: 4+ GIFs or stickers within 60 seconds (Tenor/Giphy links, `.gif` uploads, gifv embeds)
- **Repeat themselves**: 3+ near-identical messages within 2 minutes (catches "BRO LOOK" / "bro look!!" / "bro look!!!")
- **Don't add anything**: an AI reads each of their messages alongside the recent chat and decides whether it contributes. Three "not contributing" verdicts within 10 minutes means a timeout. By default this uses Google Gemini's **free tier**; Claude Haiku is a paid option (about $1 per 10,000 messages).

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

### 2. Get a free Gemini API key

1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey) and sign in with a Google account.
2. Click **Create API key** and copy it. No credit card needed.

The free tier has a daily request limit, which is far more than one watched user will hit. If it's ever reached, the bot skips the AI check until the limit resets; GIF and repeat detection keep working. Note that Google may use free-tier requests to improve its products, so the watched user's messages and the recent chat get sent to Google.

**Alternatives:** set `AI_PROVIDER=claude` and `ANTHROPIC_API_KEY` to use Claude Haiku instead (paid API credit; a Claude Pro subscription doesn't cover it). Or set `AI_ENABLED=false` to skip the AI check entirely.

### 3. Configure and run

Needs Python 3.10 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`: paste your `DISCORD_TOKEN` and `GEMINI_API_KEY`, and put the user ID to watch in `TARGET_USER_IDS`. To copy an ID, turn on **Settings → Advanced → Developer Mode**, then right-click the user and choose **Copy User ID**. Then:

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

## How the AI decides

For every message from a watched user (skipping GIFs, which the GIF check handles), the bot sends the AI:

1. Its instructions (`SYSTEM_PROMPT` in [`judge.py`](judge.py)).
2. The last 15 messages in that channel from everyone, for context.
3. The new message.

The AI must answer in a fixed JSON format with one of three verdicts plus a short reason:

- `ok`: normal chat, jokes, short replies, genuine questions.
- `low_effort`: gibberish, keyboard mashing, emoji spam, or a random question or remark that ignores what everyone's talking about.
- `disruptive`: trolling, baiting, or deliberately derailing.

Anything other than `ok` is a strike. One strike does nothing on its own; 3 strikes within 10 minutes triggers the timeout. That way a single misjudged message never gets anyone punished. If the AI can't be reached or gives a malformed answer, the message is skipped rather than counted.

## Tuning the AI

Edit `SYSTEM_PROMPT` in [`judge.py`](judge.py) to change what counts as noise. By default it's lenient: jokes, banter and short replies like "lol" are fine, and only gibberish, random non-sequiturs, trolling and derailing count as strikes. Each strike is logged to the console with the reason, so watch the first few hours. If it's too soft or too harsh, edit the prompt or change `AI_STRIKES`.

## Tests

```bash
python -m unittest -v
```

## Files

- `bot.py`: Discord events, punishment, slash commands
- `detectors.py`: GIF, repeat and strike logic (no Discord code, so it's unit tested)
- `judge.py`: the AI "is this contributing?" check (Gemini or Claude)
