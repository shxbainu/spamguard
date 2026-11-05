"""Spam detection logic, kept free of discord.py so it can be unit tested."""
from __future__ import annotations

import re
import string
from collections import defaultdict, deque
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable

GIF_LINK = re.compile(
    r"(tenor\.com/|giphy\.com/|gfycat\.com/|klipy\.com/|imgur\.com/\S+\.gifv?\b|\.gif(\?|\b))",
    re.IGNORECASE,
)
_PUNCT = str.maketrans("", "", string.punctuation)


@dataclass(frozen=True)
class MsgRef:
    """Where a message lives, so the bot can delete it later."""

    channel_id: int
    message_id: int


def looks_like_gif(
    content: str,
    attachments: Iterable[tuple[str, str | None]] = (),
    embeds: Iterable[tuple[str | None, str | None]] = (),
    sticker_count: int = 0,
) -> bool:
    """True if a message is a GIF (upload, Tenor/Giphy link, gifv embed) or a sticker.

    attachments: (filename, content_type) pairs.
    embeds: (embed type, url) pairs. Discord gives Tenor/Giphy links the "gifv" type.
    """
    if sticker_count:
        return True
    if GIF_LINK.search(content or ""):
        return True
    for filename, content_type in attachments:
        if (content_type or "").lower() == "image/gif" or filename.lower().endswith(".gif"):
            return True
    for kind, url in embeds:
        if kind == "gifv" or (url and GIF_LINK.search(url)):
            return True
    return False


def normalize(text: str) -> str:
    return " ".join(text.lower().translate(_PUNCT).split())


class GifSpamDetector:
    """Fires when a user posts `limit` GIFs within `window` seconds."""

    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self._seen: dict[int, deque[tuple[float, MsgRef]]] = defaultdict(deque)

    def add(self, user_id: int, ref: MsgRef, now: float) -> list[MsgRef] | None:
        seen = self._seen[user_id]
        seen.append((now, ref))
        while seen and now - seen[0][0] > self.window:
            seen.popleft()
        if len(seen) >= self.limit:
            refs = [r for _, r in seen]
            seen.clear()
            return refs
        return None

    def reset(self, user_id: int) -> None:
        self._seen.pop(user_id, None)


class RepeatDetector:
    """Fires when a user sends `limit` near-identical messages within `window` seconds."""

    def __init__(self, limit: int, window: float, similarity: float = 0.85):
        self.limit = limit
        self.window = window
        self.similarity = similarity
        self._seen: dict[int, deque[tuple[float, str, MsgRef]]] = defaultdict(deque)

    def _similar(self, a: str, b: str) -> bool:
        return a == b or SequenceMatcher(None, a, b).ratio() >= self.similarity

    def add(self, user_id: int, text: str, ref: MsgRef, now: float) -> list[MsgRef] | None:
        key = normalize(text)
        if not key:
            return None
        seen = self._seen[user_id]
        while seen and now - seen[0][0] > self.window:
            seen.popleft()
        matches = [r for _, k, r in seen if self._similar(k, key)]
        seen.append((now, key, ref))
        if len(matches) + 1 >= self.limit:
            seen.clear()
            return matches + [ref]
        return None

    def reset(self, user_id: int) -> None:
        self._seen.pop(user_id, None)


class StrikeTracker:
    """Collects AI "not contributing" strikes; fires at `limit` strikes within `window` seconds."""

    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self._strikes: dict[int, deque[tuple[float, str]]] = defaultdict(deque)

    def add(self, user_id: int, reason: str, now: float) -> list[str] | None:
        strikes = self._strikes[user_id]
        strikes.append((now, reason))
        while strikes and now - strikes[0][0] > self.window:
            strikes.popleft()
        if len(strikes) >= self.limit:
            reasons = [r for _, r in strikes]
            strikes.clear()
            return reasons
        return None

    def count(self, user_id: int) -> int:
        return len(self._strikes.get(user_id, ()))

    def reset(self, user_id: int) -> None:
        self._strikes.pop(user_id, None)


class PerMinuteBudget:
    """Allows at most `limit` uses in any 60-second span, to stay inside an AI provider's free tier."""

    def __init__(self, limit: int):
        self.limit = limit
        self.uses: deque[float] = deque()

    def take(self, now: float) -> bool:
        while self.uses and now - self.uses[0] > 60:
            self.uses.popleft()
        if len(self.uses) >= self.limit:
            return False
        self.uses.append(now)
        return True
