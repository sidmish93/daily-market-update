"""Deep market-news ingest: multi-source RSS, enrichment, impact scoring."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable

import feedparser
import httpx

from app.models import CommodityRow, MoverRow, NewsCandidate, SectorRow
from app.utils import strip_em_dashes

logger = logging.getLogger(__name__)

# Broader set: wraps, stocks, economy, and core market feeds
NEWS_FEEDS: list[tuple[str, str]] = [
    ("Moneycontrol", "https://www.moneycontrol.com/rss/latestnews.xml"),
    ("Moneycontrol Markets", "https://www.moneycontrol.com/rss/marketreports.xml"),
    ("Moneycontrol Business", "https://www.moneycontrol.com/rss/business.xml"),
    ("CNBC TV18", "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml"),
    ("CNBC TV18 Business", "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/business.xml"),
    ("NDTV Profit", "https://feeds.feedburner.com/ndtvprofit-latest"),
    ("Economic Times Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Economic Times Stocks", "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"),
    ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
    ("Business Standard Companies", "https://www.business-standard.com/rss/companies-101.rss"),
    ("Mint Markets", "https://www.livemint.com/rss/markets"),
    ("Mint Companies", "https://www.livemint.com/rss/companies"),
]

HIGH_IMPACT_PATTERNS = (
    (r"\b(nifty|sensex)\b.*(fall|rise|slip|surge|rally|drop|close|end)", 4, "benchmarks"),
    (r"\b(index|indices)\b.*(reshuffle|inclusion|exclusion|rebalancing|review)\b", 6, "index_review"),
    (r"\b(q[1-4]|fy\d{2}|earnings|results|net profit|pat)\b", 5, "earnings"),
    (r"\b(crude|brent|opec|hormuz|geopolit|war|sanction|strait of hormuz|middle east tension)\b", 6, "crude_geopolitics"),
    (r"\b(rbi|mpc|repo|inflation|cpi|gdp|fed|fomc|powell|rate cut|rate hike)\b", 5, "macro_policy"),
    (r"\b(fii|dii|foreign fund|institutional|fpis?)\b", 3, "flows"),
    (r"\b(sebi|ban|penalty|raid|probe|fraud|court|dismiss)\b", 5, "legal_regulatory"),
    (r"\b(ipo|qip|ofc?d|fund raise|block deal|bulk deal)\b", 3, "capital_markets"),
    (r"\b(adani|reliance|tcs|infosys|hdfc|sbi|wipro|bse|godrej)\b", 3, "large_cap"),
    (r"\b(market wrap|closing bell|end of day|after market)\b", 4, "market_wrap"),
    (r"\b(rupee|usd\/inr|dollar index|us yields?|treasury|wall street|gift nifty|sgx nifty)\b", 4, "global_macro"),
    (r"\b(tariff|trade war|china stimulus|oil price|brent crude)\b", 4, "global_macro"),
    (r"\b(ceo|md & ceo|managing director).{0,40}\b(quits?|resign\w*|exit|steps? down)\b", 6, "mgmt_change"),
    (r"\b(quits?|resign\w*|sudden exit|steps? down).{0,40}\b(ceo|md|managing director)\b", 6, "mgmt_change"),
)

NOISE_PATTERNS = (
    "buy ",
    "sell ",
    "target of rs",
    "target of ₹",
    "accumulate ",
    "reduce ",
    "brokerage",
    "technical pick",
    "mutual fund sip",
    "stock ideas for today",
    "hot stocks",
    "multibagger",
    "gmp to price",
    "grey market",
    "in 10 points",
    "10 things to know",
    "commodity heatmap",
    "gold and silver rates today",
    "gold, silver rates",
    "taking stock",
    "biggest nifty gainers",
    "market fails to hold",
    "gold and silver prices",
    "in early deals",
    "spot demand",
    "price band",
    "ipo is priced",
    "priced between",
)

# Soft features / calendars that inflate score but add little for a close note
SOFT_STORY_PATTERNS = (
    r"\bjourney that ends\b",
    r"\bfrom .+ to .+, the \d+-year\b",
    r"\bwho is\b",
    r"\bprofile\b",
    r"\blife and career\b",
    r"\bexplained:?\s*what it means for\b",
)

RESULTS_CALENDAR_PATTERNS = (
    r"\b\d{2,}\s+more\b",
    r"\band\s+\d+\s+more\b",
    r"\bq[1-4]\s+results?\s*:",
    r"\bcompanies? (?:to|set to) (?:release|announce|report).{0,40}(?:today|tomorrow)\b",
    r"\bearnings (?:today|this week|calendar)\b",
    r"\bamong companies that will announce\b",
    r"\bwill announce .{0,60}(?:earnings|results)\b",
    r"\bto (?:announce|declare|report|release) .{0,40}(?:q[1-4]|fy\d+|earnings|results)\b",
    r"\bearnings on (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b",
    r"\bresults on (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b",
    r"\bscheduled to (?:announce|report|declare)\b",
)

STALE_STORY_PATTERNS = (
    r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+quarter\b",
    r"\bduring the (?:june|march|september|december|[a-z]+)\s+quarter\b",
    r"\bin the (?:june|march|september|december)\s+quarter\b",
    r"\blast quarter\b",
    r"\bprevious quarter\b",
    r"\bover the (?:past|last) (?:month|quarter|year)\b",
)


def _is_noise(title: str, summary: str) -> bool:
    blob = f"{title} {summary}".lower()
    return any(p in blob for p in NOISE_PATTERNS)


def _is_soft_story(title: str, summary: str = "") -> bool:
    blob = f"{title}. {summary}".lower()
    return any(re.search(p, blob) for p in SOFT_STORY_PATTERNS)


def _is_results_calendar(title: str, summary: str = "") -> bool:
    blob = f"{title}. {summary}".lower()
    if any(re.search(p, blob) for p in RESULTS_CALENDAR_PATTERNS):
        return True
    # "Tata Motors, IRCTC, HAL, Abbott India, and 545 more"
    if title.count(",") >= 3 and re.search(r"\b(q[1-4]|results?|earnings)\b", title, re.I):
        return True
    return False


def _is_stale_story(title: str, summary: str = "") -> bool:
    blob = f"{title}. {summary}".lower()
    return any(re.search(p, blob) for p in STALE_STORY_PATTERNS)


def _story_cluster_key(title: str) -> str:
    """Collapse near-duplicate headlines into one cluster for selection."""
    t = title.lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()

    if "chandrasekaran" in t or (
        "tata" in t and any(k in t for k in ("resign", "re appointment", "reappointment", "step down", "successor"))
    ):
        return "tata_chandrasekaran"
    if ("godrej" in t or "gcpl" in t) and any(
        k in t for k in ("ceo", "sitapati", "malbari", "managing director", "md & ceo")
    ):
        return "godrej_consumer_ceo"
    if _is_results_calendar(title):
        return "results_calendar"
    if "aluminium" in t or "aluminum" in t or ("nalco" in t and "hindalco" in t):
        return "aluminium_metals"
    if "hospital" in t and any(k in t for k in ("apollo", "fortis", "max healthcare", "bse hospital")):
        return "hospital_sector"
    if "mcx" in t and any(k in t for k in ("jpmorgan", "jp morgan", "sebi", "upgrade")):
        return "mcx_upgrade"
    if any(k in t for k in ("crude", "brent", "opec", "hormuz", "oil price")):
        return "crude_oil_macro"
    if any(k in t for k in ("rbi", "mpc", "repo rate", "inflation", "cpi")):
        return "rbi_macro"
    if any(k in t for k in ("rupee", "usd inr", "dollar index", "gift nifty", "wall street")):
        return "global_cues"

    stop = {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "as", "for", "from",
        "after", "with", "up", "down", "by", "at", "is", "are", "was", "were",
        "shares", "stock", "stocks", "fall", "falls", "rise", "rises", "gain", "gains",
    }
    tokens = [w for w in t.split() if w not in stop and len(w) > 2][:5]
    return " ".join(tokens) if tokens else t[:40]


def _impact_score(title: str, summary: str) -> tuple[float, list[str]]:
    blob = f"{title}. {summary}".lower()
    score = 0.0
    tags: list[str] = []
    for pattern, weight, tag in HIGH_IMPACT_PATTERNS:
        if re.search(pattern, blob, re.I):
            score += weight
            tags.append(tag)
    if len(summary) > 180:
        score += 1.5
    if len(summary) > 400:
        score += 1.0
    if _is_soft_story(title, summary):
        score -= 8
        tags.append("soft_story")
    if _is_results_calendar(title, summary):
        score -= 10
        tags.append("results_calendar")
    if _is_stale_story(title, summary):
        score -= 10
        tags.append("stale_period")
    # Prefer post-close language for a ~15:30+ note; down-rank midday stamps
    if re.search(
        r"\b(clos(?:e|es|ed|ing)|ends?|ended|settle(?:d)?|after\s+(?:the\s+)?(?:market|bell)|"
        r"closing\s+bell|market\s+wrap|session\s+(?:end|close))\b",
        blob,
        re.I,
    ):
        score += 2.5
        tags.append("post_close")
    if re.search(r"\bat\s+\d{1,2}(\.\d+)?\s*(am|pm)\b", blob):
        score -= 3.0
        tags.append("intraday_timestamp")
    if re.search(r"\b(trade\s+weak|trade\s+strong|intraday|by\s+noon|mid[- ]day)\b", blob):
        score -= 1.5
        tags.append("intraday_framing")
    return score, tags


def _parse_published_dt(published: str):
    """Best-effort parse of our published string or raw RSS date."""
    if not published:
        return None
    raw = published.strip()
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC$", raw):
            return datetime.strptime(raw, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
        if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
            return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _freshness_bonus(published: str) -> tuple[float, list[str]]:
    """Bias toward today's IST stories; soft-penalize older reprints."""
    from zoneinfo import ZoneInfo

    dt = _parse_published_dt(published)
    if not dt:
        return 0.0, []
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    local = dt.astimezone(ZoneInfo("Asia/Kolkata"))
    age_hours = (now_ist - local).total_seconds() / 3600.0
    if age_hours < 0:
        age_hours = 0
    if local.date() == now_ist.date():
        return 3.0, ["fresh_today"]
    if age_hours <= 36:
        return 0.5, ["fresh_recent"]
    if age_hours <= 72:
        return -2.0, ["stale"]
    return -5.0, ["very_stale"]


def _parse_published(entry: dict) -> str:
    for key in ("published", "updated"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:  # noqa: BLE001
            return str(raw)
    return ""


def _plain(html: str) -> str:
    try:
        from bs4 import BeautifulSoup

        return BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    except Exception:  # noqa: BLE001
        return html


def _context_bonus(
    title: str,
    summary: str,
    movers: Iterable[MoverRow],
    sectors: Iterable[SectorRow],
    commodities: Iterable[CommodityRow],
) -> tuple[float, list[str]]:
    blob = f"{title} {summary}".lower()
    bonus = 0.0
    tags: list[str] = []
    for m in movers:
        token = (m.symbol or m.company or "").lower()
        name = (m.company or "").lower()
        short = " ".join(name.split()[:2]) if name else ""
        linked = False
        if token and len(token) >= 3 and token in blob.replace("&", "").replace(" ", ""):
            linked = True
        elif name and len(name) >= 4 and name in blob:
            linked = True
        elif short and len(short) >= 5 and short in blob:
            linked = True
        if linked:
            bonus += 4 + min(abs(m.change_pct or 0) / 2, 4)
            tags.append(f"mover:{m.symbol or m.company}")
    for s in sectors[:6]:
        if s.name and s.name.lower() in blob:
            bonus += 2
            tags.append(f"sector:{s.name}")
    for c in commodities:
        key = c.commodity.split()[0].lower()
        if key and key in blob:
            bonus += 2.5
            tags.append(f"commodity:{c.commodity}")
    return bonus, tags


def _protect_mover_catalysts(
    items: list[NewsCandidate],
    movers: list[MoverRow],
    *,
    min_abs_pct: float = 3.0,
) -> None:
    """Boost the best company-specific catalyst for large Nifty-200 movers."""
    for m in movers:
        move = abs(m.change_pct or 0)
        if move < min_abs_pct:
            continue
        name = (m.company or "").lower()
        short = " ".join(name.split()[:2]) if name else ""
        symbol = (m.symbol or "").lower()
        best: NewsCandidate | None = None
        best_rank = -1e9
        for item in items:
            title = item.title.lower()
            if symbol == "godrejcp" and ("godrej" in title or "gcpl" in title):
                matched = True
            elif short and len(short) >= 5 and short in title:
                matched = True
            elif symbol and len(symbol) >= 4 and symbol in title.replace(" ", ""):
                matched = True
            else:
                matched = False
            if not matched:
                continue
            # Skip pure gainers/losers wraps even if the name appears
            if any(
                w in title
                for w in (
                    "top gainers",
                    "top losers",
                    "gainers & losers",
                    "gainers and losers",
                    "market highlights",
                )
            ):
                continue
            tags = {t.lower() for t in item.relevance_tags}
            rank = float(item.score) + move
            if tags & {"mgmt_change", "earnings", "legal_regulatory", "capital_markets"}:
                rank += 25
            if "fresh_today" in tags or "fresh_recent" in tags:
                rank += 5
            if rank > best_rank:
                best = item
                best_rank = rank
        if best is None:
            continue
        best.score += 18 + min(move, 8)
        best.relevance_tags = list(
            dict.fromkeys(
                list(best.relevance_tags)
                + [f"mover:{m.symbol or m.company}", "mover_protected"]
            )
        )


async def _fetch_feed(client: httpx.AsyncClient, source: str, url: str) -> list[NewsCandidate]:
    items: list[NewsCandidate] = []
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.text)
        for entry in parsed.entries[:25]:
            title = strip_em_dashes(html.unescape((entry.get("title") or "").strip()))
            summary = strip_em_dashes(
                html.unescape(_plain(entry.get("summary") or entry.get("description") or ""))
            )
            if not title or _is_noise(title, summary):
                continue
            if _is_soft_story(title, summary) or _is_results_calendar(title, summary):
                continue
            if _is_stale_story(title, summary):
                continue
            score, tags = _impact_score(title, summary)
            if score < 2 and not any(
                k in f"{title} {summary}".lower()
                for k in ("nifty", "sensex", "market", "crude", "earnings", "results", "rbi", "index")
            ):
                continue
            items.append(
                NewsCandidate(
                    title=title,
                    summary=summary[:700],
                    source=source,
                    url=entry.get("link") or "",
                    published=_parse_published(entry),
                    score=score,
                    relevance_tags=tags,
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Feed failed %s (%s): %s", source, url, exc)
    return items


async def _enrich_article(client: httpx.AsyncClient, item: NewsCandidate) -> NewsCandidate:
    """Pull article body/meta so the LLM gets substance, not only headlines."""
    if not item.url or len(item.summary) >= 450:
        return item
    try:
        resp = await client.get(item.url, timeout=12.0)
        if resp.status_code != 200:
            return item
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "noscript", "nav", "footer", "aside"]):
            tag.decompose()
        meta = ""
        for attrs in (
            {"name": "description"},
            {"property": "og:description"},
            {"name": "twitter:description"},
        ):
            node = soup.find("meta", attrs=attrs)
            if node and node.get("content"):
                meta = node["content"].strip()
                break
        paras = [
            p.get_text(" ", strip=True)
            for p in soup.find_all("p")
            if len(p.get_text(" ", strip=True)) > 60
        ][:4]
        body = " ".join(paras)
        enriched = strip_em_dashes(html.unescape((meta + " " + body).strip()))
        if len(enriched) > len(item.summary) + 40:
            item.summary = enriched[:1200]
            item.score += 2
            item.relevance_tags = list(item.relevance_tags) + ["enriched"]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Enrich failed for %s: %s", item.url, exc)
    return item


def _mover_has_usable_catalyst(mover: MoverRow, items: list[NewsCandidate]) -> bool:
    name = (mover.company or "").lower()
    short = " ".join(name.split()[:2]) if name else ""
    symbol = (mover.symbol or "").lower()
    wrap_bits = (
        "top gainers",
        "top losers",
        "gainers & losers",
        "gainers and losers",
        "market highlights",
        "among the other losers",
        "bse's 'a' group",
    )
    for item in items:
        title = item.title.lower()
        if any(w in title for w in wrap_bits):
            continue
        matched = False
        if short and len(short) >= 5 and short in title:
            matched = True
        elif symbol == "godrejcp" and ("godrej" in title or "gcpl" in title):
            matched = True
        elif symbol and len(symbol) >= 4 and symbol.lower() in title.replace(" ", ""):
            matched = True
        if not matched:
            continue
        if any(
            k in title
            for k in (
                "ceo",
                "quit",
                "resign",
                "result",
                "profit",
                "loss",
                "deal",
                "funding",
                "order",
                "upgrade",
                "downgrade",
                "q1",
                "q2",
                "q3",
                "q4",
            )
        ):
            return True
        tags = {t.lower() for t in item.relevance_tags}
        if tags & {"mgmt_change", "earnings", "legal_regulatory", "capital_markets"}:
            return True
    return False


async def _backfill_large_mover_news(
    client: httpx.AsyncClient,
    movers: list[MoverRow],
    existing: list[NewsCandidate],
) -> list[NewsCandidate]:
    """Pull targeted Google News when main RSS has dropped a big-mover catalyst."""
    from urllib.parse import quote_plus

    targets = [
        m
        for m in movers
        if abs(m.change_pct or 0) >= 5.0 and not _mover_has_usable_catalyst(m, existing)
    ]
    targets = sorted(targets, key=lambda m: -abs(m.change_pct or 0))[:5]
    if not targets:
        return []

    out: list[NewsCandidate] = []
    for mover in targets:
        name = mover.company or mover.symbol or ""
        short = " ".join(name.split()[:2]) if name else name
        query = f'"{short}" (CEO OR quit OR resign OR results OR profit OR funding OR deal OR Q1)'
        url = (
            "https://news.google.com/rss/search?q="
            + quote_plus(query)
            + "&hl=en-IN&gl=IN&ceid=IN:en"
        )
        try:
            resp = await client.get(url, timeout=15.0)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.info("Mover news backfill failed for %s: %s", short, exc)
            continue
        parsed = feedparser.parse(resp.text)
        for entry in parsed.entries[:8]:
            title = strip_em_dashes(html.unescape((entry.get("title") or "").strip()))
            summary = strip_em_dashes(
                html.unescape(_plain(entry.get("summary") or entry.get("description") or ""))
            )
            if not title or _is_noise(title, summary):
                continue
            if _is_soft_story(title, summary) or _is_results_calendar(title, summary):
                continue
            if _is_stale_story(title, summary):
                continue
            low = title.lower()
            if any(
                w in low
                for w in (
                    "top gainers",
                    "top losers",
                    "gainers & losers",
                    "gainers and losers",
                )
            ):
                continue
            # Must still mention the company
            if short.lower() not in low and (mover.symbol or "").lower() not in low.replace(" ", ""):
                if not (
                    mover.symbol == "GODREJCP"
                    and ("godrej" in low or "gcpl" in low)
                ):
                    continue
            score, tags = _impact_score(title, summary)
            item = NewsCandidate(
                title=title,
                summary=summary,
                source="Google News",
                url=(entry.get("link") or "").strip(),
                published=_parse_published(entry),
                score=score + 12 + min(abs(mover.change_pct or 0), 8),
                relevance_tags=list(
                    dict.fromkeys(
                        tags
                        + [
                            f"mover:{mover.symbol or mover.company}",
                            "mover_backfill",
                            "fresh_today",
                        ]
                    )
                ),
            )
            out.append(item)
            if len([x for x in out if f"mover:{mover.symbol or mover.company}" in x.relevance_tags]) >= 2:
                break
    return out


async def fetch_market_news(
    limit: int = 16,
    movers: list[MoverRow] | None = None,
    sectors: list[SectorRow] | None = None,
    commodities: list[CommodityRow] | None = None,
) -> list[NewsCandidate]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    movers = movers or []
    sectors = sectors or []
    commodities = commodities or []

    all_items: list[NewsCandidate] = []
    async with httpx.AsyncClient(timeout=20.0, headers=headers, follow_redirects=True) as client:
        batches = await asyncio.gather(
            *[_fetch_feed(client, source, url) for source, url in NEWS_FEEDS]
        )
        for batch in batches:
            all_items.extend(batch)

        # If a large Nifty-200 mover lost its catalyst from RSS, backfill it
        try:
            backfill = await _backfill_large_mover_news(client, list(movers), all_items)
            if backfill:
                logger.info("Backfilled %s mover catalyst headlines", len(backfill))
                all_items.extend(backfill)
        except Exception as exc:  # noqa: BLE001
            logger.info("Mover backfill skipped: %s", exc)

        # Deduplicate exact titles, then keep one item per story cluster
        seen: set[str] = set()
        unique: list[NewsCandidate] = []
        for item in all_items:
            key = "".join(ch for ch in item.title.lower() if ch.isalnum())[:90]
            if not key or key in seen:
                continue
            seen.add(key)
            bonus, tags = _context_bonus(item.title, item.summary, movers, sectors, commodities)
            fresh, fresh_tags = _freshness_bonus(item.published)
            item.score += bonus + fresh
            item.relevance_tags = list(
                dict.fromkeys(list(item.relevance_tags) + tags + fresh_tags)
            )
            # Soft filter: keep tape-linked / high-impact catalysts ahead of orphan blurbs
            tape_linked = any(
                t.startswith(("mover:", "sector:", "commodity:")) for t in item.relevance_tags
            )
            catalyst = bool(
                {
                    "earnings",
                    "mgmt_change",
                    "legal_regulatory",
                    "index_review",
                    "crude_geopolitics",
                    "large_cap",
                    "post_close",
                    "mover_backfill",
                }
                & set(item.relevance_tags)
            )
            if not tape_linked and not catalyst:
                item.score -= 2.5
                item.relevance_tags = list(item.relevance_tags) + ["weak_tape_link"]
            unique.append(item)

        _protect_mover_catalysts(unique, list(movers))
        unique.sort(key=lambda n: (-n.score, 0 if n.published else 1, n.published or ""))
        clustered: list[NewsCandidate] = []
        seen_clusters: set[str] = set()
        for item in unique:
            cluster = _story_cluster_key(item.title)
            if cluster in seen_clusters:
                continue
            seen_clusters.add(cluster)
            clustered.append(item)

        shortlist = clustered[: max(limit * 2, 20)]

        # Enrich the strongest candidates with article text
        to_enrich = shortlist[:12]
        enriched = await asyncio.gather(*[_enrich_article(client, item) for item in to_enrich])
        enriched_map = {e.title: e for e in enriched}
        final = [enriched_map.get(i.title, i) for i in shortlist]
        final.sort(key=lambda n: (-n.score, 0 if n.published else 1, n.published or ""))
        return final[:limit]
