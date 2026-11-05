# Ben's Butt Spreader

A Discord bot that watches **every member** for spam and pointless messages, and punishes repeat offenders with timeouts that get longer each time, ending in a kick with an invite back. It includes a web dashboard showing every timeout and kick and why it happened, plus an AI chat with a personality you set, and fun commands for everyone: `/shutup` and `/settleargument`.

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

## AI chat

The bot chats in character:

- **In one channel you pick** (`CHAT_CHANNEL_ID`), it replies to **every** message. That channel is for messing around, so it's never checked for spam.
- **In any other channel**, it replies when someone **@mentions** it or **replies** to one of its messages.

It sees the last 15 messages in the channel, including its own replies, so it can follow the conversation. Each message is labelled with the person's nickname and username (`Benny (@empireben)`), so a personality can mention people by either name.

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
| Spam and "not contributing" checks | `AI_PROVIDER` / `AI_MODEL` | Gemini Flash-Lite | Many small, simple checks |
| `/settleargument` | `SETTLE_PROVIDER` / `SETTLE_MODEL` | Groq `openai/gpt-oss-120b`, then `openai/gpt-oss-20b`, then Gemini | The best free reasoning model |
| AI chat | `CHAT_PROVIDER` / `CHAT_MODEL` | Groq `openai/gpt-oss-120b`, then `qwen/qwen3.8-27b`, then `openai/gpt-oss-20b`, then Gemini | The most coherent free model; smaller ones force jokes that don't make sense |

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

### 2. Install the bot

In the VM's SSH window:

```bash
sudo apt update && sudo apt install -y git gh python3-venv
gh auth login
gh repo clone shxbainu/spamguard
cd spamguard
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
nano .env
```

`gh auth login` is needed because the repo is private: choose GitHub.com → HTTPS → "Login with a web browser" and enter the code it shows. In `nano`, paste the contents of your `.env` from your computer, then save with **Ctrl+O**, **Enter**, **Ctrl+X**.

Stop the bot on your own computer first, so two copies aren't running at once.

### 3. Keep it running

This makes the bot start on boot and restart if it crashes:

```bash
sudo tee /etc/systemd/system/spreader.service > /dev/null <<EOF
[Unit]
Description=Ben's Butt Spreader
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$HOME/spamguard
ExecStart=$HOME/spamguard/.venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now spreader
```

Useful commands on the VM:

| Command | Does |
|---|---|
| `sudo systemctl status spreader` | Is it running? |
| `journalctl -u spreader -f` | Live logs (Ctrl+C to stop watching) |
| `cd ~/spamguard && git pull && sudo systemctl restart spreader` | Update to the newest code |

You can close the SSH window; the bot keeps running.

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
