# Ben's Butt Spreader

A Discord bot that watches **every member** for spam and pointless messages, and punishes repeat offenders with timeouts that get longer each time, ending in a kick with an invite back. It includes a web dashboard showing every timeout and kick and why it happened, plus an AI chat with a personality you set, and fun commands for everyone: `/shutup` and `/settleargument`.

## What counts as an offense

| Check | Triggers when | Uses AI? |
|---|---|---|
| **GIF spam** | 4+ GIFs or stickers within 60 seconds (Tenor/Giphy links, `.gif` uploads, gifv embeds) | No |
| **Repeating** | 3+ near-identical messages within 2 minutes ("BRO LOOK" / "bro look!!" / "bro look!!!") | No |
| **Not contributing** | 3 messages in a row judged as noise, within 10 minutes | Yes: a fast model on Groq's free tier, with Gemini as a second opinion when it's unsure |

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

Anything other than `ok` is a **strike**. A single strike does nothing; 3 strikes **in a row** within 10 minutes is one offense, and any message judged normal in between resets the count. That way one or two misjudged messages never get anyone punished. If the AI can't be reached, gives a broken answer, or the per-minute cap is hit, the message is just skipped.

## AI chat

The bot chats in character:

- **In one channel you pick** (`CHAT_CHANNEL_ID`), it replies to **every** message. That channel is for messing around, so it's never checked for spam.
- **In any other channel**, it replies when someone **@mentions** it or **replies** to one of its messages.

It remembers the last 30 messages in the channel (`CHAT_CONTEXT_MESSAGES`), including its own replies, and after a restart it reads them back from Discord the first time it chats in a channel. It also sees what it recently said in other channels, so it doesn't contradict itself. When a message (or the message it replies to) has a picture, Gemini looks at it; Groq models can't see pictures and are told one was shared. When someone asks it a question, it has to actually answer: a reply of three words or fewer to a question gets rewritten. Each message is labelled with the person's nickname and username (`Benny (@empireben)`), so a personality can mention people by either name.

### Changing its personality

The personality is set only in `.env`, so nobody can change it from Discord. Write it on one line in double quotes, then restart the bot:

```
PERSONA="A chaotic, sarcastic gremlin who loves bad puns and has a petty rivalry with empireben."
```

Leave it empty for the default (a sarcastic, pun-loving gremlin). Whatever it says, the bot's built-in rules keep it to playful roasting: no slurs, nothing sexual, and no attacks on anyone's real-life traits.

Limits so it can't spam or burn through the free tier: it answers each person at most once every 4 seconds (`CHAT_COOLDOWN_SECONDS`), and sends at most 20 replies a minute across the server (`CHAT_MAX_PER_MINUTE`, shared with `/settleargument`). Messages that arrive while it's at a limit just don't get an answer. Its replies can never ping anyone, so it can't `@everyone`.

### Which AI does what

Each job can use a different AI, and each provider has its own free allowance, so splitting them stretches the free tiers further:

| Job | Setting | Default in `.env.example` | Why |
|---|---|---|---|
| "Not contributing" check | `AI_PROVIDER` / `AI_MODEL` | Groq `openai/gpt-oss-20b`, then `qwen/qwen3.8-27b` | Fast and cheap for checking every message |
| Second opinion when that check isn't sure | `AI_UNSURE_PROVIDER` / `AI_UNSURE_MODEL` | Gemini 3.5 Flash-Lite | Smarter, so it's saved for the hard calls |
| `/settleargument` | `SETTLE_PROVIDER` / `SETTLE_MODEL` | Gemini 3.5 Flash-Lite, then Groq `openai/gpt-oss-120b`, then `openai/gpt-oss-20b` | Scores higher than gpt-oss-120b on benchmarks |
| AI chat channel | `CHAT_PROVIDER` / `CHAT_MODEL` | Gemini 3.5 Flash-Lite, then Groq `openai/gpt-oss-120b`, `qwen/qwen3.8-27b`, `openai/gpt-oss-20b` | Same brain as mentions, so it sounds the same everywhere; Gemini can also see pictures |
| @mentions and replies in other channels | `MENTION_PROVIDER` / `MENTION_MODEL` | Same as `/settleargument` | Where a useful answer matters most |

The "not contributing" check reports whether it's sure. Only when it isn't (a borderline message, or slang and in-jokes it can't place) does the bot ask the second-opinion model, which keeps Gemini's daily allowance for settling arguments and mentions. If the cheap models are all rate-limited, that message's AI check is skipped instead of falling back to Gemini.

Reasoning models (gpt-oss, Qwen) think harder where answers matter: high for `/settleargument`, medium for mentions in other channels, low in the chat channel to keep it fast and within the free limits.

Providers: `gemini`, `groq`, `openrouter` (free models, but only 50 requests a day unless you've bought $10 of credit) and `claude` (paid). If a provider's key is missing, the feature that needs it turns off and the bot logs a warning; the rest keeps working.

**Backup models:** each `*_MODEL` setting can be a comma-separated list. The bot tries the models in order and uses the first that answers, so when one is busy ("over capacity"), rate-limited or down, the next one takes over. Prefix an entry with `provider:` to use a different provider, e.g. `CHAT_MODEL=openai/gpt-oss-20b,qwen/qwen3.8-27b,gemini:gemini-3.5-flash-lite`. Each request gives up after 15 seconds. Models whose provider has no API key in `.env` are skipped.

For the chat, put the model you like best first. Bigger models hold a conversation much better: gpt-oss-20b is fast but tends to make forced comebacks that don't quite make sense. Chat and `/settleargument` both use gpt-oss-120b by default, so they share its 1,000-requests-a-day free limit.

The chat history is sent to the AI as a real back-and-forth conversation (its own earlier replies as its turns), which helps it follow what's going on. Keep `PERSONA` about who the bot is and how it feels about people; the bot's built-in chat rules already handle the casual texting style, and stacking extra style rules, slang lists or catchphrases in the personality makes it overact.

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

While the bot runs, open the dashboard (on your own computer: **http://localhost:8080**; on a host, see below). It has four tabs:

- **Overview**: today's punishments, messages checked, how many were flagged, how often Gemini gave a second opinion and how often it overruled the first check, bot replies, everyone's current offense level with **Reset** buttons, the latest flags with reasons, why people were punished, who was punished most this week, and which AI models did the work.
- **Message log**: every message the "not contributing" check judged: who said it, where, the verdict, the AI's reason, which model decided, the first model's call when a second opinion was asked, and how many strikes in a row that person is on. Filter by person, flagged only, or second opinions only.
- **Punishments**: every timeout and kick, the offense number, and the messages behind it (for AI offenses, each flagged message with the AI's reason).
- **Bot activity**: every chat reply and `/settleargument` ruling, what it was replying to, the ruling's reasoning, and which model answered.

It refreshes every 15 seconds. Punishment history is kept forever; the message log and bot activity keep the last 7 days (`LOG_DAYS`) and clean themselves up. Set `LOG_MESSAGES=false` to stop logging message text.

The dashboard is only reachable from the computer the bot runs on. To open it to other devices, set `DASHBOARD_HOST=0.0.0.0` **and** a `DASHBOARD_PASSWORD` (the username is `admin`). On a host like Wispbyte, also set `DASHBOARD_PORT` to the port in your server's address and open `http://ADDRESS:PORT`.

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

### 3b. Get a free Groq API key (for chat and /settleargument)

1. Go to [console.groq.com](https://console.groq.com) and sign in with Google or GitHub. No card needed.
2. Click **API Keys → Create API Key** and copy it right away; Groq only shows it once.
3. Put it in `.env` as `GROQ_API_KEY`.

The free tier allows about 30 requests a minute and 1,000 a day per model; your exact limits are under **Settings → Limits** in the Groq console.

### 4. Configure and run

Needs Python 3.10 or newer (`brew install python@3.12` on a Mac).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`: paste your `DISCORD_TOKEN`, `GEMINI_API_KEY` and `GROQ_API_KEY`. For IDs, turn on **Settings → Advanced → Developer Mode** in Discord, then:

- `GUILD_ID`: right-click the server icon → **Copy Server ID**
- `CHAT_CHANNEL_ID`: right-click the channel the AI should chat in → **Copy Channel ID**

Every other setting is explained in `.env.example`. Then:

```bash
python bot.py
```

The bot only works while it's running. To keep it online without your computer, see [Run it 24/7 for free](#run-it-247-for-free).

## Slash commands

### For everyone

| Command | Does |
|---|---|
| `/shutup @user` | Times them out for 10 seconds. Each member gets **3 per week** (rolling 7 days, counted per member). Can't target yourself, bots, admins, or anyone already timed out, and a failed attempt doesn't use one up. It doesn't add to anyone's offense count, but it shows on the dashboard. |
| `/settleargument [comment] [messages]` | The AI reads the last 50 messages in the channel (5–100 with `messages`), works out who's arguing what, and posts a verdict: who's right, why, and a few facts backing it up. Leave a `comment` ("tell Dave he's wrong") and it answers you directly, in its current personality. Once per channel every 30 seconds. |

`/settleargument` uses the AI's own knowledge and doesn't search the web, so it can be wrong or out of date on recent events, and it says so when something is just opinion. Its personality changes the wording, not who wins. Tune the commands with `SHUTUP_PER_WEEK`, `SHUTUP_SECONDS` and `SETTLE_COOLDOWN_SECONDS` in `.env`.

### For mods

Only people with the **Timeout Members** permission can use these.

| Command | Does |
|---|---|
| `/spreader history @user` | Their offense count, AI strikes, next punishment, and last 5 punishments |
| `/spreader reset @user` | Resets their offense count to zero (a timeout they're in keeps running) |
| `/spreader resetall` | Resets everyone's offense count in the server |
| `/spreader pardon @user` | Ends their timeout and resets their offense count to zero |

## Run it 24/7 for free

The most reliable free option is Google Cloud's **Always Free e2-micro** VM: a small Linux server that stays on all the time, free within the limits below. Google asks for a card to verify you, but doesn't charge if you stay inside the free tier.

The free tier covers exactly **one** e2-micro in **us-west1, us-central1 or us-east1** with a standard disk of up to 30 GB. A VM in any other region, a bigger machine, or a second VM is billed.

### 1. Create the VM

1. Sign in at [console.cloud.google.com](https://console.cloud.google.com), create a project, and turn on billing (needed even for the free tier).
2. Go to **Compute Engine → VM instances → Create instance**.
3. Set **Region** to `us-central1` (or us-west1 / us-east1), **Machine type** to `e2-micro`, and under **Boot disk** pick **Debian 12** with a **Standard persistent disk**, 10–30 GB.
4. Click **Create**, then click **SSH** next to the VM to open a terminal in your browser.

### 2. Install and start the bot

1. **Stop the bot on your own computer** (`pkill -f bot.py`), so two copies don't answer every message twice.
2. In the VM's SSH window, click **Upload file** (the up-arrow button at the top) and upload these from your computer's `spamguard` folder, one at a time:
   - `deploy/setup.sh`
   - `.env` (hidden in Finder: press **Cmd+Shift+.** in the file picker to show it)
   - `spamguard.db`, if you want to keep the punishment history
3. Run:

   ```bash
   bash setup.sh
   ```

   Because the repo is private, it first asks you to log in to GitHub: open the link it shows on your phone or computer and enter the code. It then installs everything, starts the bot, and shows the startup log, which should include `Logged in as …` and your personality.

The bot now starts on boot and restarts if it crashes, so you can close the SSH window.

| Command (on the VM) | Does |
|---|---|
| `bash ~/spamguard/deploy/setup.sh` | Update to the newest code and restart |
| `journalctl -u spreader -f` | Live logs (Ctrl+C to stop watching) |
| `sudo systemctl status spreader` | Is it running? |
| `nano ~/spamguard/.env`, then `sudo systemctl restart spreader` | Change a setting or the personality |

### 3. Keep it free

- Stay on **one** `e2-micro` in us-west1, us-central1 or us-east1 with a standard disk. The bot uses under 100 MB of the VM's 1 GB of memory.
- Google Cloud may show a "free trial" credit too; the Always Free VM keeps working after it ends. Set a budget alert (**Billing → Budgets & alerts**, e.g. $1) so you'd hear about any charge.

### 4. See the dashboard

The dashboard stays private on the VM. To open it, install the [gcloud CLI](https://cloud.google.com/sdk/docs/install) on your computer and run:

```bash
gcloud compute ssh YOUR_VM_NAME --zone YOUR_ZONE -- -L 8080:localhost:8080
```

While that's open, **http://localhost:8080** on your computer shows the VM's dashboard.

### Other free options

- **Oracle Cloud Always Free**: more powerful VMs, but sign-up often fails, free ARM capacity is often unavailable, and Oracle can reclaim VMs it considers idle.
- **Free Discord bot hosts** (for example bot-hosting.net): no card, but small (around 256 MB of RAM), many need you to click "renew" every few days, there's no dashboard access, and you're handing your bot token to a small company you don't know.
- **An always-on device at home**: an old laptop or a Raspberry Pi, set up the same way as the VM.

## Tests

```bash
python -m unittest -v
```

## Files

- `bot.py`: Discord events, punishments, AI chat replies, slash commands
- `detectors.py`: GIF, repeat and strike logic
- `escalation.py`: the punishment ladder
- `judge.py`: everything that talks to the AI (Gemini, Groq, OpenRouter or Claude): the "is this contributing?" check, argument settling and chat
- `storage.py`: SQLite history, offense counts and /shutup uses
- `dashboard.py`, `dashboard.html`: the web dashboard
