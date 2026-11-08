"""Looks things up on Wikipedia (free, no API key, costs no AI tokens) so fact-checks and debate decisions can cite
real pages. The AI gets the most relevant paragraphs of the top few articles and backs its answer with exact quotes
from them; every quote is checked word-for-word against the article before it's shown."""
from __future__ import annotations

import asyncio
import logging
import re

import aiohttp

log = logging.getLogger("spamguard.wiki")

API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "BensButtSpreader/1.0 (Discord bot; https://github.com/shxbainu/spamguard)"}
TIMEOUT = aiohttp.ClientTimeout(total=8)
PASSAGE_CHARS = 2500  # per article, about 600 tokens
STOPWORDS = set(
    "a an the is are was were be been being has have had do does did of to in on at for with from by about as and or but "
    "not no it its this that these those i you he she they we my your his her their our can could would should will "
    "just really very so than then there here who what when where why how which".split()
)


def keywords(text: str, limit: int = 6) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9'-]+", text) if w.lower() not in STOPWORDS]
    return " ".join(words[:limit])


def passages(text: str, query: str, limit: int = PASSAGE_CHARS) -> str:
    """The article's opening paragraph plus the paragraphs that share the most words with `query`, in their
    original order, within `limit` characters."""
    paragraphs = [p.strip() for p in text.split("\n") if p.strip() and not p.strip().startswith("==")]
    if not paragraphs:
        return ""
    words = {w.lower() for w in re.findall(r"[A-Za-z0-9'-]+", query) if w.lower() not in STOPWORDS}
    score = lambda p: len(words & {w.lower() for w in re.findall(r"[A-Za-z0-9'-]+", p)})
    ranked = sorted(range(1, len(paragraphs)), key=lambda i: -score(paragraphs[i]))
    keep, size = [0], len(paragraphs[0])
    for i in ranked:
        if score(paragraphs[i]) == 0 or size + len(paragraphs[i]) > limit:
            continue
        keep.append(i)
        size += len(paragraphs[i])
    return "\n\n".join(paragraphs[i] for i in sorted(keep))[: limit + 500]


async def lookup(query: str, limit: int = 3) -> list[dict]:
    """Up to `limit` articles for `query`: {"title", "url", "text"} with the article's most relevant paragraphs.
    Empty if Wikipedia can't be reached or has nothing."""
    query = query.strip()[:300]
    if not query:
        return []
    try:
        async with aiohttp.ClientSession(headers=HEADERS, timeout=TIMEOUT) as session:
            titles = await search(session, query, limit)
            if len(titles) < 2 and keywords(query) != query:
                titles += [t for t in await search(session, keywords(query), limit) if t not in titles]
            texts = await asyncio.gather(*(article(session, title) for title in titles[:limit]))
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        log.warning("Couldn't reach Wikipedia: %s", e)
        return []
    found = []
    for (title, text) in texts:
        if text and (passage := passages(text, query)):
            found.append({"title": title, "url": "https://en.wikipedia.org/wiki/" + title.replace(" ", "_"), "text": passage})
    return found


async def article(session: aiohttp.ClientSession, title: str) -> tuple[str, str]:
    """(final title after redirects, plain text) of one article."""
    params = {"action": "query", "prop": "extracts", "explaintext": 1, "titles": title, "redirects": 1, "format": "json"}
    async with session.get(API, params=params) as response:
        pages = (await response.json()).get("query", {}).get("pages", {})
    page = next(iter(pages.values()), {})
    return page.get("title", title), page.get("extract", "")


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
        "the question). Back up your answer with \"quotes\": up to 3 short passages (one sentence, at most 40 words "
        "each) copied exactly, word for word, from the sources, each with the number of the source it came from. "
        "Only quote text that supports your answer; use [] if none of the sources do. Never change, shorten or "
        "combine the wording inside a quote.\n"
        + "\n".join(parts)
    )


def normalize(text: str) -> str:
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", text).strip().lower()


def verified_quotes(articles: list[dict], quotes) -> list[tuple[str, str, str]]:
    """(title, url, quote) for each quote the AI gave that really appears in the article it named (word for word,
    ignoring case, spacing and curly quotes); made-up or reworded quotes are dropped."""
    verified = []
    for item in quotes if isinstance(quotes, list) else []:
        try:
            number, quote = int(item["source"]), str(item["quote"]).strip().strip('"\u201c\u201d')
        except (KeyError, TypeError, ValueError):
            continue
        if not (1 <= number <= len(articles)) or len(quote) < 15:
            continue
        source = normalize(articles[number - 1]["text"])
        parts = [normalize(part) for part in re.split(r"\.\.\.|\u2026", quote) if part.strip()]
        if parts and all(part in source for part in parts):
            if quote not in [q for _, _, q in verified]:
                verified.append((articles[number - 1]["title"], articles[number - 1]["url"], quote[:300]))
        else:
            log.info("Dropped a quote that isn't in %s: %r", articles[number - 1]["title"], quote[:100])
    return verified[:3]


def quoted_sources(quotes: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """(title, url) of the articles the verified quotes came from, without repeats."""
    return list(dict.fromkeys((title, url) for title, url, _ in quotes))
