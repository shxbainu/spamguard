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
| "Not contributing" check | `AI_PROVIDER` / `AI_MODEL` | Groq `qwen/qwen3.8-27b`, then `openai/gpt-oss-20b` | Fast and cheap for checking every message; qwen spots bad takes much better than gpt-oss-20b |
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

### Profile status

The bot shows a custom status on its profile, switching every `STATUS_MINUTES` (10 by default). Set your own with `STATUSES` in `.env`, separated by `|`:

```
STATUSES="watching over the server for ShxbaInu|serving ShxbaInu|destroying ben's cheeks|forcing luke to touch grass"
```

A status bought in the shop replaces the rotation for an hour. The bot always spells the nickname Kim John-Un (never Jong-Un), in chat, rulings and statuses.

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
4. On the same page, under **Privileged Gateway Intents**, turn on **Presence Intent**, **Server Members Intent** and **Message Content Intent**, then save. (Presence is only for the grass check; without it the bot still starts, minus that.)

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

`/settleargument` uses the AI's own knowledge and doesn't search the web, so it can be wrong or out of date on recent events, and it says so when something is just opinion. The ruling itself (topic, verdict, reasoning, facts) is written as a neutral referee: no jokes, emojis or bias toward anyone the personality likes or dislikes, and the facts are real-world evidence, not a recap of the chat. Only the one-line reply above it is in the bot's personality. Tune the commands with `SHUTUP_PER_WEEK`, `SHUTUP_SECONDS` and `SETTLE_COOLDOWN_SECONDS` in `.env`.

| Command | Does |
|---|---|
| Right-click a message → **Apps → Quote this** | Saves it to the quote book (not your own messages). |
| `/quote random [user] [about]` | A random saved quote, optionally from one person or containing a word. |
| `/quote show number` / `/quote delete number` | Show one by number, or remove it (the person quoted, whoever saved it, or a mod). |
| `/aura check [user]` | Someone's aura and what they gained or lost it for. |
| `/aura leaderboard` | Most to least aura. |
| `/debate start motion [minutes] [rounds]` | Starts a debate; people pick For or Against with buttons, then the starter presses Start. |
| `/debate stop` | Ends the debate (whoever started it, or a mod). |
| `/grass` | How long the grass-check person has been on Discord and gaming today. |
| Right-click a message → **Apps → Fact check**, or `/factcheck claim` | A formal fact-check with a rating and web sources. |
| `/coinflip` `/slots` `/roulette` `/blackjack` `/higherlower` | Casino games; bet aura. |
| `/duel @user bet [game]` | You both bet the same aura and play quick draw, 10 seconds, split or steal, or pure luck. |
| `/daily` | Free aura once a day. |
| `/predict [yes/no] [bet]` | Bet on today's yes/no question (pays 2x), or see it. |
| `/rep [user]` | How the bot feels about someone, from -100 to 100. |
| `/give @user amount` | Give someone aura. |
| `/shop` / `/buy item` | Spend aura: extra or 1-minute /shutups, a shutup shield, renaming someone, a roast, the bot's status. |
| `/shutup @user long:True` | Uses a 1-minute /shutup you bought. Bought regular ones are used automatically after your weekly 3. |

### Quote book

Right-click any message → **Apps → Quote this**. When the bot chats with someone it knows up to 3 of their saved quotes, and now and then throws one back at them when it fits (like when they contradict it).

### Aura

The moderation check already reads each message, so it also spots standout moments and rates how big each one is, from 1 to 5. Most messages get nothing, and the smarter second-opinion model (`AI_UNSURE_MODEL`) has to agree on both the moment and its size. When it happens the bot replies with one short line in its voice that matches the size, and the points:

| Moment | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|
| Comeback (decent → legendary) | +25 | +50 | +100 | +250 | +500 |
| Calling out something false (small slip → total takedown); the person called out loses the same | +25 | +50 | +100 | +200 | +300 |
| Saying something false (small mistake → absurdly wrong) | −25 | −50 | −100 | −250 | −500 |
| A take almost everyone would call terrible ("oranges taste like shit"); real splits like pineapple on pizza don't count | −25 | −50 | −100 | −250 | −500 |

Winning a `/debate` is +100 each, and a message fact-checked as false is −100 for its author and +50 for the checker. Most moments land at 1 or 2; 4 and 5 are for genuinely exceptional ones.

Each person can gain or lose aura from a message at most once every `AURA_COOLDOWN_MINUTES` (10). Mods can earn aura too (they just can't be punished). Nothing in the AI chat channel counts, since that channel isn't moderated, except being nice to the bot.

**Being nice to the bot:** thanking it or complimenting it ("ty", "good bot", "ur funny") in a mention, a reply to it, or the chat channel has a 75% chance (`NICE_AURA_CHANCE`) of earning +10 to +25, at most once every 30 minutes (`NICE_AURA_COOLDOWN_MINUTES`). It's spotted from the wording, so it costs no AI tokens; sarcasm like "thanks for nothing" or "thanks but ur dumb" doesn't count, and every nice message starts the cooldown, so spamming "thanks" doesn't beat the odds. The points are tacked onto the bot's reply. Turn it off with `AURA_ENABLED=false`.

### Fact check

Right-click a message → **Apps → Fact check** (or `/factcheck claim:"..."`). It's written like a published fact-check: the claim, a rating (true, mostly true, misleading, false, unverifiable, or opinion), the evidence, a correction if needed, and **sources**. Each message can only be checked once, and each person can check once every 30 seconds. A message rated **false** costs its author 100 aura and gives the person who checked it 50.

Where the sources come from:

1. **Gemini with Google Search**, if your key has search allowance. Free keys usually don't (it answers "quota exceeded"), so after one refusal the bot stops trying for an hour.
2. Otherwise **Wikipedia**: the bot looks the claim up (free, no key, no AI tokens), gives the AI the most relevant paragraphs of the top three articles, and the AI backs its answer with up to 3 **exact quotes**. The bot checks every quote word for word against the article it names and drops any that are made up or reworded, so what's shown is really in the article, with a link to it. If nothing relevant turns up, it says the check relies on the AI's own knowledge.
3. Groq's gpt-oss models can also browse the web (`GROQ_WEB_SEARCH=true`), but one search reads whole pages and uses 30,000–90,000 tokens of the 200,000 free daily allowance, which would starve the chat, so it's off by default.

Debate decisions use the same sources (looking up the motion). `/settleargument` lists sources only when Gemini search works.

### Casino and shop

Aura is the currency. Everyone gets 500 the first time they use the casino (`CASINO_START_AURA`), plus `/daily`: 100, and 20 more for each day in a row up to 200 (`DAILY_AURA`). After that, aura comes from chat (comebacks, callouts, being nice to the bot), debates, fact-checks, `/give` and winning bets. Bets can be a number, `1k`, `half` or `all`; you can't bet aura you don't have, so negative aura means you're broke until you earn some.

| Game | How it pays |
|---|---|
| `/coinflip bet call` | Call it right: 2x |
| `/slots bet` | Pair 1.5x, three of a kind 10x, three 🍑 25x |
| `/roulette bet on` | red/black/odd/even/low/high 2x; a number (0–36) or green 36x |
| `/blackjack bet` | Hit, stand or double down; win 2x, blackjack 2.5x, tie gets your bet back |
| `/higherlower bet` | Guess whether the next card is higher or lower; each right guess grows the pot (riskier guesses grow it more), cash out any time, one wrong guess loses it |
| `/duel @user bet game` | Both bet the same and play for the pot (default: a random one of the first three below) |
| `/daily` | Free aura once a day (more for days in a row) |

Duel games:

| Game | How it works |
|---|---|
| 🤠 Quick draw | Wait for **DRAW!**, then hit the button first; clicking early loses |
| ⏱️ 10 seconds | A hidden clock starts; hit **Stop** when you think 10 seconds have passed, closest wins. Timing starts once Discord has the message and each click counts when it reaches the bot, so both players lose the same network delay |
| 🤝 Split or steal | Pick in secret. Both split: each gets their bet back plus 25%. One steals: they take the pot. Both steal: both lose it. Not picking counts as split |
| 🎲 Pure luck | 50/50 |

If nobody wins (time runs out), both bets go back.

Shop (`/shop` to see it, `/buy` to spend):

| Item | Price | Does |
|---|---|---|
| `shutup` | 250 | One more /shutup after your weekly 3 |
| `longshutup` | 600 | A 1-minute /shutup (`/shutup @user long:True`) |
| `shield` | 400 | Nobody can /shutup you for 24 hours |
| `nickname` | 500 | Rename someone for an hour (`/buy nickname user text`); needs the bot to have **Manage Nicknames** |
| `roast` | 150 | The bot roasts someone (`/buy roast user`) |
| `status` | 750 | Set the bot's status for an hour (`/buy status text`) |

Set `CASINO_CHANNEL_ID` to keep the games in one channel. `/aura check` shows someone's casino winnings or losses, items and shield; the casino doesn't fill up their aura history.

### Reputation with the bot

The bot keeps a score from **-100 to 100** for how it feels about each person, and `/rep` shows it with the last few things that moved it. Being nice to it ("ty", "good bot", "ur funny") is +3; being rude ("stfu", "clanker", "ur so dumb", "nobody asked") is -5. It only counts messages aimed at the bot (a mention, a reply to it, or the chat channel), at most one change per person every 2 minutes. Its chat replies follow the score: warmer with people it likes, colder and shorter with people it doesn't, and now and then it brings up the last rude thing you said. It's worked out from the wording, so it uses no extra AI calls (just a line added to the chat prompt). Turn it off with `REP_ENABLED=false`.

### Daily prediction

Every day at `PREDICT_OPEN` (10:00) the bot posts one yes/no question in `PREDICT_CHANNEL_ID` (default: the casino channel, else the chat channel). Bet with `/predict yes 100` (once a day, up to `PREDICT_MAX_BET`, 500) until `PREDICT_CLOSE` (18:00); at `PREDICT_SETTLE` (23:30) it posts the answer and right bets pay 2x. Times use `GRASS_TIMEZONE`.

- **About the server** (no AI at all, settled from the bot's own records): will the server send more than N messages today (N from yesterday), will the bot time anyone out, will anyone gain or lose 100+ aura from one message, will anyone lose 1,000+ in the casino.
- **Sports or news** (`PREDICT_WORLD_CHANCE`, half the days): only when the `/settleargument` model can search the web (Gemini). One search writes the question, one checks the result that night. Without real search results it falls back to a server question, and if the result can't be confirmed every bet goes back.

### The Daily Aura

At `NEWS_TIME` (22:00) the bot posts a tabloid front page in `NEWS_CHANNEL_ID` (default: the chat channel) about what really happened that day: big aura moments, casino wins and losses of 500+, timeouts. If fewer than `NEWS_MIN_EVENTS` (3) notable things happened, it posts nothing and makes no AI call. Otherwise it's one call to the chat model. Turn it off with `NEWS_ENABLED=false`.

### Debates

1. `/debate start motion:"This house would ban homework"`. Optional: `minutes` per speech (1–5, default 2) and `rounds` (1 = one speech each, 2 = opening + closing, 3 = opening + rebuttal + closing).
2. People click **Join For** or **Join Against**; whoever started it clicks **Start**.
3. The bot opens a thread and calls speakers in turn, alternating sides (sides with several people take turns). The speaker types their argument and clicks **Done**, or the timer runs out (with a 30-second warning). Only the speaker can type during a speech; anyone else's message is removed.
4. Everyone except the debaters gets 60 seconds to vote, then the AI judge (the impartial `/settleargument` model) writes a formal decision: the winning side, the reasoning, facts it checked (with web sources when Gemini is available) and the best speaker. The audience vote is shown next to it. Winners get +100 aura.

Debates live in memory, so restarting the bot ends one that's running.

### Grass check

Every day at `GRASS_TIME` (21:00, `GRASS_TIMEZONE` America/New_York) the bot posts in `GRASS_CHANNEL_ID` (default: the chat channel), pings `GRASS_USER` (default `destructorluke`), and shows how long they were on Discord that day, which games they played and for how long, and whether they're online now, with a line in the bot's voice. If they play one game for `GRASS_NAG_HOURS` (3) hours straight it says something once. `/grass` shows the same stats any time.

"On Discord" counts time Discord shows them online, idle or do not disturb on any device, so it's Discord time, not phone screen time. Games only show if they share their activity (Discord settings → Activity Privacy). It needs **Presence Intent** turned on in the Developer Portal; set `GRASS_USER=` (empty) to turn it off, `GRASS_PING=false` to post without pinging.

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
- `aura.py`, `casino.py`, `duels.py`, `shop.py`: aura, the casino, duels and the shop
- `rep.py`: reputation with the bot
- `predict.py`: the daily prediction
- `news.py`: the Daily Aura
- `dashboard.py`, `dashboard.html`: the web dashboard
