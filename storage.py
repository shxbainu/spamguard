"""SQLite storage: punishment history, offense counts, /shutup uses, a rolling log of what the bot saw and
decided (checked messages, its replies and rulings) for the dashboard, the quote book, aura points, and when the
grass-check person was online or playing games."""
from __future__ import annotations

import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS punishments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT NOT NULL,
    channel_name TEXT,
    action TEXT NOT NULL,            -- timeout, kick, or failed
    duration_seconds INTEGER NOT NULL,
    offense INTEGER NOT NULL,
    category TEXT NOT NULL,          -- gif, repeat, ai, or shutup
    reason TEXT NOT NULL,
    evidence TEXT
);
CREATE INDEX IF NOT EXISTS punishments_created ON punishments (created_at);
CREATE TABLE IF NOT EXISTS offenders (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    offenses INTEGER NOT NULL,
    last_offense REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS shutups (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,        -- who ran /shutup
    target_id INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    guild_id INTEGER NOT NULL,
    kind TEXT NOT NULL,              -- check (an AI-judged message), reply (bot chat), ruling (/settleargument)
    user_id INTEGER,                 -- who wrote the message, or who the bot answered
    user_name TEXT,
    channel_name TEXT,
    content TEXT,                    -- the member's message, or what the bot said
    verdict TEXT,                    -- checks: ok, low_effort or disruptive
    reason TEXT,                     -- why: the AI's reason, or a ruling's reasoning
    model TEXT,                      -- which model decided or answered
    escalated INTEGER NOT NULL DEFAULT 0,  -- checks: a second model was asked
    overruled INTEGER NOT NULL DEFAULT 0,  -- checks: the second model disagreed with the first
    detail TEXT                      -- extra context: the first model's call, strike count, what was replied to
);
CREATE INDEX IF NOT EXISTS events_created ON events (created_at);
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,        -- when it was saved
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    said_at REAL NOT NULL,           -- when the message was sent
    author_id INTEGER NOT NULL,
    author_name TEXT NOT NULL,
    content TEXT NOT NULL,
    has_image INTEGER NOT NULL DEFAULT 0,
    saved_by_id INTEGER NOT NULL,
    saved_by_name TEXT NOT NULL,
    UNIQUE (guild_id, message_id)
);
CREATE TABLE IF NOT EXISTS aura (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT NOT NULL,
    points INTEGER NOT NULL,
    kind TEXT NOT NULL,              -- comeback, callout, called_out, false_claim, bad_take, debate, a game, gift or shop
    reason TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS aura_user ON aura (guild_id, user_id);
CREATE TABLE IF NOT EXISTS presence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,              -- online (any status but offline) or game
    name TEXT,                       -- the game's name
    started REAL NOT NULL,
    ended REAL NOT NULL              -- kept up to date while it's still going
);
CREATE INDEX IF NOT EXISTS presence_user ON presence (user_id, ended);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS inventory (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    item TEXT NOT NULL,              -- shop items bought with aura, e.g. shutup or longshutup
    count INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id, item)
);
"""

# Aura kinds from the casino; /aura check leaves them out of someone's recent history so it shows what they did.
GAME_KINDS = ("coinflip", "slots", "blackjack", "higherlower", "roulette", "duel")
# Kinds earned from a message (the per-person cooldown only looks at these).
MESSAGE_KINDS = ("comeback", "callout", "false_claim", "bad_take")

EVENT_FIELDS = (
    "guild_id", "kind", "user_id", "user_name", "channel_name", "content", "verdict", "reason", "model",
    "escalated", "overruled", "detail",
)


class Store:
    def __init__(self, path: str, event_days: float = 7):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.event_days = event_days
        self._events_since_prune = 0
        self.prune_events(time.time())

    # ---- offense counting --------------------------------------------------

    def offenses(self, guild_id: int, user_id: int, now: float, reset_after: float) -> int:
        """Current offense count; it drops back to 0 after `reset_after` seconds of good behaviour."""
        row = self.db.execute(
            "SELECT offenses, last_offense FROM offenders WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        if row is None or now - row["last_offense"] > reset_after:
            return 0
        return row["offenses"]

    def next_offense(self, guild_id: int, user_id: int, now: float, reset_after: float) -> int:
        """Counts a new offense and returns its number (1 = first)."""
        offense = self.offenses(guild_id, user_id, now, reset_after) + 1
        self.db.execute(
            "INSERT INTO offenders (guild_id, user_id, offenses, last_offense) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET offenses = excluded.offenses, last_offense = excluded.last_offense",
            (guild_id, user_id, offense, now),
        )
        self.db.commit()
        return offense

    def forgive(self, guild_id: int, user_id: int) -> None:
        self.db.execute("DELETE FROM offenders WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        self.db.commit()

    def forgive_all(self, guild_id: int | None = None) -> None:
        if guild_id is None:
            self.db.execute("DELETE FROM offenders")
        else:
            self.db.execute("DELETE FROM offenders WHERE guild_id = ?", (guild_id,))
        self.db.commit()

    def active_offenders(self, now: float, reset_after: float) -> list[dict]:
        """Members whose offense count hasn't reset yet, highest first, with their latest name."""
        rows = self.db.execute(
            "SELECT o.guild_id, o.user_id, o.offenses, o.last_offense, "
            "(SELECT p.user_name FROM punishments p WHERE p.user_id = o.user_id ORDER BY p.created_at DESC LIMIT 1) AS user_name "
            "FROM offenders o WHERE o.last_offense >= ? ORDER BY o.offenses DESC, o.last_offense DESC",
            (now - reset_after,),
        )
        return [dict(row, resets_at=row["last_offense"] + reset_after) for row in rows]

    # ---- /shutup allowance ---------------------------------------------------

    def shutups_used(self, guild_id: int, user_id: int, now: float, window: float) -> list[float]:
        """Times this member used /shutup within the last `window` seconds, oldest first."""
        rows = self.db.execute(
            "SELECT created_at FROM shutups WHERE guild_id = ? AND user_id = ? AND created_at > ? ORDER BY created_at",
            (guild_id, user_id, now - window),
        )
        return [row[0] for row in rows]

    def record_shutup(self, guild_id: int, user_id: int, target_id: int, now: float) -> None:
        self.db.execute(
            "INSERT INTO shutups (guild_id, user_id, target_id, created_at) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, target_id, now),
        )
        self.db.commit()

    # ---- activity log ----------------------------------------------------------

    def log_event(self, *, created_at: float | None = None, **fields) -> int:
        """Records something the bot saw or did. Events older than `event_days` are cleared out as it goes."""
        unknown = set(fields) - set(EVENT_FIELDS)
        if unknown:
            raise TypeError(f"unknown event fields: {sorted(unknown)}")
        now = created_at or time.time()
        names = ["created_at", *fields]
        cursor = self.db.execute(
            f"INSERT INTO events ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
            [now, *(int(v) if isinstance(v, bool) else v for v in fields.values())],
        )
        self.db.commit()
        self._events_since_prune += 1
        if self._events_since_prune >= 200:
            self.prune_events(time.time())
        return cursor.lastrowid

    def prune_events(self, now: float) -> None:
        self.db.execute("DELETE FROM events WHERE created_at < ?", (now - self.event_days * 86400,))
        self.db.commit()
        self._events_since_prune = 0

    def events(
        self,
        limit: int = 200,
        kind: str | None = None,
        user: str | None = None,
        flagged: bool = False,
        escalated: bool = False,
    ) -> list[dict]:
        query, args = "SELECT * FROM events WHERE 1 = 1", []
        if kind:
            query += " AND kind = ?"
            args.append(kind)
        if user:
            query += " AND (user_name LIKE ? OR CAST(user_id AS TEXT) = ?)"
            args += [f"%{user}%", user]
        if flagged:
            query += " AND verdict IS NOT NULL AND verdict != 'ok'"
        if escalated:
            query += " AND escalated = 1"
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(limit)
        return [dict(row) for row in self.db.execute(query, args)]

    def activity(self, now: float) -> dict:
        """Counts for the last 24 hours, for the dashboard's summary cards."""
        def count(where: str, *args) -> int:
            return self.db.execute(
                f"SELECT COUNT(*) FROM events WHERE created_at >= ? AND {where}", (now - 86400, *args),
            ).fetchone()[0]

        models = self.db.execute(
            "SELECT model, COUNT(*) AS count FROM events WHERE created_at >= ? AND model IS NOT NULL "
            "GROUP BY model ORDER BY count DESC",
            (now - 86400,),
        )
        return {
            "checked": count("kind = 'check'"),
            "flagged": count("kind = 'check' AND verdict != 'ok'"),
            "second_opinions": count("kind = 'check' AND escalated = 1"),
            "overruled": count("kind = 'check' AND overruled = 1"),
            "replies": count("kind = 'reply'"),
            "rulings": count("kind = 'ruling'"),
            "by_model": [dict(row) for row in models],
            "kept_days": self.event_days,
        }

    # ---- quote book ------------------------------------------------------------

    def add_quote(self, **fields) -> int | None:
        """Saves a quote and returns its number, or None if that message is already in the book."""
        names = ["created_at", *fields]
        try:
            cursor = self.db.execute(
                f"INSERT INTO quotes ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                [time.time(), *(int(v) if isinstance(v, bool) else v for v in fields.values())],
            )
        except sqlite3.IntegrityError:
            return None
        self.db.commit()
        return cursor.lastrowid

    def quote(self, guild_id: int, quote_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM quotes WHERE guild_id = ? AND id = ?", (guild_id, quote_id)).fetchone()
        return dict(row) if row else None

    def random_quote(self, guild_id: int, author_id: int | None = None, about: str | None = None) -> dict | None:
        query, args = "SELECT * FROM quotes WHERE guild_id = ?", [guild_id]
        if author_id:
            query += " AND author_id = ?"
            args.append(author_id)
        if about:
            query += " AND content LIKE ?"
            args.append(f"%{about}%")
        row = self.db.execute(query + " ORDER BY RANDOM() LIMIT 1", args).fetchone()
        return dict(row) if row else None

    def quotes_by(self, guild_id: int, author_id: int, limit: int = 3) -> list[str]:
        rows = self.db.execute(
            "SELECT content FROM quotes WHERE guild_id = ? AND author_id = ? AND content != '' ORDER BY RANDOM() LIMIT ?",
            (guild_id, author_id, limit),
        )
        return [row[0] for row in rows]

    def count_quotes(self, guild_id: int) -> int:
        return self.db.execute("SELECT COUNT(*) FROM quotes WHERE guild_id = ?", (guild_id,)).fetchone()[0]

    def delete_quote(self, guild_id: int, quote_id: int) -> None:
        self.db.execute("DELETE FROM quotes WHERE guild_id = ? AND id = ?", (guild_id, quote_id))
        self.db.commit()

    # ---- aura -------------------------------------------------------------------

    def add_aura(self, guild_id: int, user_id: int, user_name: str, points: int, kind: str,
                 reason: str | None = None, message: str | None = None, now: float | None = None) -> None:
        self.db.execute(
            "INSERT INTO aura (created_at, guild_id, user_id, user_name, points, kind, reason, message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now or time.time(), guild_id, user_id, user_name, points, kind, reason, message),
        )
        self.db.commit()

    def aura_total(self, guild_id: int, user_id: int) -> int:
        row = self.db.execute("SELECT COALESCE(SUM(points), 0) FROM aura WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        return row.fetchone()[0]

    def aura_history(self, guild_id: int, user_id: int, limit: int = 5) -> list[dict]:
        """Recent aura changes, not counting casino games."""
        rows = self.db.execute(
            f"SELECT * FROM aura WHERE guild_id = ? AND user_id = ? AND kind NOT IN ({', '.join('?' * len(GAME_KINDS))}) "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (guild_id, user_id, *GAME_KINDS, limit),
        )
        return [dict(row) for row in rows]

    def last_aura(self, guild_id: int, user_id: int) -> float:
        """When this member last gained or lost aura from one of their messages, or 0."""
        row = self.db.execute(
            f"SELECT MAX(created_at) FROM aura WHERE guild_id = ? AND user_id = ? AND kind IN ({', '.join('?' * len(MESSAGE_KINDS))})",
            (guild_id, user_id, *MESSAGE_KINDS),
        ).fetchone()
        return row[0] or 0

    def spend_aura(self, guild_id: int, user_id: int, user_name: str, amount: int, kind: str, reason: str | None = None) -> bool:
        """Takes `amount` aura if they have that much; False (and nothing taken) if they don't."""
        if amount <= 0 or self.aura_total(guild_id, user_id) < amount:
            return False
        self.add_aura(guild_id, user_id, user_name, -amount, kind, reason)
        return True

    def aura_rank(self, guild_id: int, user_id: int) -> tuple[int, int]:
        """(this person's place on the leaderboard, how many people are on it); place 0 if they aren't."""
        rows = self.db.execute(
            "SELECT user_id FROM aura WHERE guild_id = ? GROUP BY user_id ORDER BY SUM(points) DESC", (guild_id,),
        ).fetchall()
        ids = [row[0] for row in rows]
        return (ids.index(user_id) + 1 if user_id in ids else 0), len(ids)

    def aura_from_moments(self, guild_id: int, user_id: int) -> int:
        """Aura gained or lost from comebacks, callouts, false claims and debates."""
        row = self.db.execute(
            "SELECT COALESCE(SUM(points), 0) FROM aura WHERE guild_id = ? AND user_id = ? AND kind IN "
            "('comeback', 'callout', 'called_out', 'false_claim', 'bad_take', 'debate')",
            (guild_id, user_id),
        )
        return row.fetchone()[0]

    def games_played(self, guild_id: int, user_id: int) -> int:
        row = self.db.execute(
            f"SELECT COUNT(*) FROM aura WHERE guild_id = ? AND user_id = ? AND reason = 'bet' AND kind IN ({', '.join('?' * len(GAME_KINDS))})",
            (guild_id, user_id, *GAME_KINDS),
        )
        return row.fetchone()[0]

    def game_net(self, guild_id: int, user_id: int) -> int:
        """Aura won (or lost, if negative) in the casino overall."""
        row = self.db.execute(
            f"SELECT COALESCE(SUM(points), 0) FROM aura WHERE guild_id = ? AND user_id = ? AND kind IN ({', '.join('?' * len(GAME_KINDS))})",
            (guild_id, user_id, *GAME_KINDS),
        )
        return row.fetchone()[0]

    def aura_leaderboard(self, guild_id: int, limit: int = 10) -> list[dict]:
        rows = self.db.execute(
            "SELECT user_id, (SELECT a2.user_name FROM aura a2 WHERE a2.guild_id = a.guild_id AND a2.user_id = a.user_id "
            "ORDER BY a2.created_at DESC LIMIT 1) AS user_name, SUM(points) AS points "
            "FROM aura a WHERE guild_id = ? GROUP BY user_id ORDER BY points DESC LIMIT ?",
            (guild_id, limit),
        )
        return [dict(row) for row in rows]

    # ---- online time and games (grass check) -----------------------------------

    def start_presence(self, user_id: int, kind: str, name: str | None, now: float) -> int:
        cursor = self.db.execute(
            "INSERT INTO presence (user_id, kind, name, started, ended) VALUES (?, ?, ?, ?, ?)", (user_id, kind, name, now, now),
        )
        self.db.commit()
        return cursor.lastrowid

    def touch_presence(self, session_ids: list[int], now: float) -> None:
        """Moves the end of sessions that are still going up to now (so a restart loses at most a few minutes)."""
        if session_ids:
            self.db.execute(
                f"UPDATE presence SET ended = ? WHERE id IN ({', '.join('?' * len(session_ids))})", (now, *session_ids),
            )
            self.db.commit()

    def presence_totals(self, user_id: int, since: float, until: float) -> dict:
        """Seconds online and seconds per game between `since` and `until` (overlapping sessions are clipped)."""
        rows = self.db.execute(
            "SELECT kind, name, started, ended FROM presence WHERE user_id = ? AND ended > ? AND started < ?",
            (user_id, since, until),
        )
        online, games = 0.0, {}
        for row in rows:
            seconds = max(0.0, min(row["ended"], until) - max(row["started"], since))
            if row["kind"] == "online":
                online += seconds
            else:
                games[row["name"]] = games.get(row["name"], 0.0) + seconds
        return {"online": online, "games": dict(sorted(games.items(), key=lambda item: -item[1]))}

    def prune_presence(self, now: float, days: float = 30) -> None:
        self.db.execute("DELETE FROM presence WHERE ended < ?", (now - days * 86400,))
        self.db.commit()

    def get_state(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))
        self.db.commit()

    def delete_state(self, key: str) -> None:
        self.db.execute("DELETE FROM state WHERE key = ?", (key,))
        self.db.commit()

    def states(self, prefix: str) -> dict[str, str]:
        rows = self.db.execute("SELECT key, value FROM state WHERE key LIKE ?", (prefix.replace("%", "") + "%",))
        return {row[0]: row[1] for row in rows}

    # ---- shop items -------------------------------------------------------------

    def items(self, guild_id: int, user_id: int) -> dict[str, int]:
        rows = self.db.execute("SELECT item, count FROM inventory WHERE guild_id = ? AND user_id = ? AND count > 0", (guild_id, user_id))
        return {row[0]: row[1] for row in rows}

    def add_item(self, guild_id: int, user_id: int, item: str, count: int = 1) -> None:
        self.db.execute(
            "INSERT INTO inventory (guild_id, user_id, item, count) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id, item) DO UPDATE SET count = count + excluded.count",
            (guild_id, user_id, item, count),
        )
        self.db.commit()

    def use_item(self, guild_id: int, user_id: int, item: str) -> bool:
        """Uses up one of an item; False if they don't have any."""
        cursor = self.db.execute(
            "UPDATE inventory SET count = count - 1 WHERE guild_id = ? AND user_id = ? AND item = ? AND count > 0",
            (guild_id, user_id, item),
        )
        self.db.commit()
        return cursor.rowcount > 0

    # ---- punishment history ------------------------------------------------

    def record(
        self,
        *,
        guild_id: int,
        user_id: int,
        user_name: str,
        channel_name: str | None,
        action: str,
        duration_seconds: int,
        offense: int,
        category: str,
        reason: str,
        evidence: str | None = None,
        created_at: float | None = None,
    ) -> int:
        cursor = self.db.execute(
            "INSERT INTO punishments (created_at, guild_id, user_id, user_name, channel_name, action, "
            "duration_seconds, offense, category, reason, evidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (created_at or time.time(), guild_id, user_id, user_name, channel_name, action,
             duration_seconds, offense, category, reason, evidence),
        )
        self.db.commit()
        return cursor.lastrowid

    def punishments(
        self,
        limit: int = 200,
        user: str | None = None,
        category: str | None = None,
        action: str | None = None,
    ) -> list[dict]:
        query, args = "SELECT * FROM punishments WHERE 1 = 1", []
        if user:
            query += " AND (user_name LIKE ? OR CAST(user_id AS TEXT) = ?)"
            args += [f"%{user}%", user]
        if category:
            query += " AND category = ?"
            args.append(category)
        if action:
            query += " AND action = ?"
            args.append(action)
        query += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [dict(row) for row in self.db.execute(query, args)]

    def stats(self, now: float) -> dict:
        def count(where: str = "1 = 1", *args) -> int:
            return self.db.execute(f"SELECT COUNT(*) FROM punishments WHERE {where}", args).fetchone()[0]

        day_ago, week_ago = now - 86400, now - 7 * 86400
        top = self.db.execute(
            "SELECT user_id, MAX(user_name) AS user_name, COUNT(*) AS count FROM punishments "
            "WHERE created_at >= ? GROUP BY user_id ORDER BY count DESC LIMIT 5",
            (week_ago,),
        )
        return {
            "total": count(),
            "last_24h": count("created_at >= ?", day_ago),
            "timeouts": count("action = 'timeout'"),
            "kicks": count("action = 'kick'"),
            "by_category": {c: count("category = ?", c) for c in ("gif", "repeat", "ai", "shutup")},
            "top_users_7d": [dict(row) for row in top],
        }
