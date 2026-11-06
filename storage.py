"""SQLite storage: punishment history, offense counts, /shutup uses, and a rolling log of what the bot saw and
decided (checked messages, its replies and rulings) for the dashboard."""
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
"""

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
