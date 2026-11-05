"""SQLite storage for punishment history and each user's offense count."""
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
    category TEXT NOT NULL,          -- gif, repeat, or ai
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
"""


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

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
            "by_category": {c: count("category = ?", c) for c in ("gif", "repeat", "ai")},
            "top_users_7d": [dict(row) for row in top],
        }
