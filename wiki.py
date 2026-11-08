"""Looks things up on Wikipedia (free, no API key, costs no AI tokens) so fact-checks and debate decisions can cite
real pages. The AI gets the opening of the top few articles and says which ones it actually relied on."""
from __future__ import annotations

import logging
import re

import aiohttp

log = logging.getLogger("spamguard.wiki")

API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "BensButtSpreader/1.0 (Discord bot; https://github.com/shxbainu/spamguard)"}
TIMEOUT = aiohttp.ClientTimeout(total=8)
STOPWORDS = set(
    "a an the is are was were be been being has have had do does did of to in on at for with from by about as and or but "
    "not no it its this that these those i you he she they we my your his her their our can could would should will "
    "just really very so than then there here who what when where why how which".split()
)


def keywords(text: str, limit: int = 6) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9'-]+", text) if w.lower() not in STOPWORDS]
    return " ".join(words[:limit])


async def lookup(query: str, limit: int = 3) -> list[dict]:
    """Up to `limit` articles for `query`: {"title", "url", "text"} with the article's opening paragraphs.
    Empty if Wikipedia can't be reached or has nothing."""
    query = query.strip()[:300]
    if not query:
        return []
    try:
        async with aiohttp.ClientSession(headers=HEADERS, timeout=TIMEOUT) as session:
            titles = await search(session, query, limit)
            if len(titles) < 2 and keywords(query) != query:
                titles += [t for t in await search(session, keywords(query), limit) if t not in titles]
            if not titles:
                return []
            params = {
                "action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1, "exchars": 1500,
                "titles": "|".join(titles[:limit]), "redirects": 1, "format": "json",
            }
            async with session.get(API, params=params) as response:
                pages = (await response.json()).get("query", {}).get("pages", {})
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        log.warning("Couldn't reach Wikipedia: %s", e)
        return []
    found = {page["title"]: page.get("extract", "") for page in pages.values() if page.get("extract")}
    order = [title for title in titles if title in found] + [title for title in found if title not in titles]
    return [
        {"title": title, "url": "https://en.wikipedia.org/wiki/" + title.replace(" ", "_"), "text": found[title]}
        for title in order[:limit]
    ]


async def search(session: aiohttp.ClientSession, query: str, limit: int) -> list[str]:
    params = {"action": "query", "list": "search", "srsearch": query, "srlimit": limit, "srprop": "", "format": "json"}
    async with session.get(API, params=params) as response:
        data = await response.json()
    return [result["title"] for result in data.get("query", {}).get("search", [])]


def evidence_block(articles: list[dict]) -> str:
    """The articles as numbered reference material for the AI."""
    if not articles:
        return ""
    parts = [f'<source n="{n}" title="{a["title"]}">\n{a["text"]}\n</source>' for n, a in enumerate(articles, 1)]
    return (
        "\n\nReference material from Wikipedia (some of it may be unrelated; ignore anything that doesn't bear on "
        "the question). Set \"sources_used\" to the numbers of every source that supports your answer, or [] if none do.\n"
        + "\n".join(parts)
    )


def used_sources(articles: list[dict], used) -> list[tuple[str, str]]:
    """(title, url) of the articles the AI said it relied on."""
    numbers = []
    for value in used if isinstance(used, list) else []:
        try:
            numbers.append(int(value))
        except (TypeError, ValueError):
            continue
    return [(articles[n - 1]["title"], articles[n - 1]["url"]) for n in dict.fromkeys(numbers) if 1 <= n <= len(articles)]
