"""Select and polish News & Updates bullets for the free (no-LLM) path."""

from __future__ import annotations

import html as html_lib
import re

from app.models import NewsCandidate, NewsUpdateItem
from app.scrapers.news import (
    _is_results_calendar,
    _is_soft_story,
    _is_stale_story,
    _story_cluster_key,
)
from app.utils import strip_em_dashes

SOURCE_PREFIX_RE = re.compile(
    r"^(Moneycontrol(?: Markets| Business)?|CNBC TV18(?: Business)?|NDTV Profit|"
    r"Economic Times(?: Markets| Stocks)?|Business Standard(?: Companies)?|"
    r"Mint(?: Markets| Companies)?)\s*:\s*",
    re.I,
)

CALENDAR_EXTRA = (
    r"\bamong companies that will announce\b",
    r"\bwill announce .{0,60}(?:earnings|results)\b",
    r"\bto (?:announce|declare|report|release) .{0,40}(?:q[1-4]|fy\d+|earnings|results)\b",
    r"\bearnings on (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b",
    r"\bresults on (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b",
    r"\bscheduled to (?:announce|report|declare)\b",
    r"\bearnings season preview\b",
    r"\bwhat to expect from\b",
)

STALE_STORY = (
    r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+quarter\b",
    r"\bduring the (?:june|march|september|december|[a-z]+)\s+quarter\b",
    r"\bin the (?:june|march|september|december)\s+quarter\b",
    r"\blast quarter\b",
    r"\bprevious quarter\b",
    r"\bin q[1-4]fy\d{2}\b(?!.*\b(?:today|session|wednesday|thursday|friday|monday|tuesday)\b)",
    r"\bover the (?:past|last) (?:month|quarter|year)\b",
)

WEAK_WRAP = (
    r"biggest (?:nifty )?gainers",
    r"losers included",
    r"top (?:nifty )?(?:gainers|losers)",
    r"among the other losers",
    r"other losers in the",
    r"bse'?s?\s+['\"]?a['\"]?\s+group",
    r"stocks? to watch",
    r"trade setup",
    r"technical outlook",
    r"gold and silver prices?",
    r"in early deals",
    r"precious metals?",
    r"\bipo is priced\b",
    r"\bprice band\b",
    r"\bissue price\b",
    r"\bpriced between\b",
    r"trading volume .{0,60}jumped",
    r"shares changing hands",
    r"till \d{1,2}:\d{2}\s*(am|pm)",
    r"nine-fold",
    r"stock market crash highlights",
    r"market highlights",
    r"worst performer",
    r"extend(?:s|ed)? declines for",
)

MACRO_TAGS = {
    "crude_geopolitics",
    "macro_policy",
    "global_macro",
    "flows",
}


def _unescape(text: str) -> str:
    text = html_lib.unescape(text or "")
    return text.replace("#39;", "'").replace("&#39;", "'").replace("&amp;", "&")


def _strip_source(text: str) -> str:
    return SOURCE_PREFIX_RE.sub("", strip_em_dashes(_unescape(text)).strip()).strip()


def _is_calendar(title: str, summary: str = "") -> bool:
    if _is_results_calendar(title, summary):
        return True
    blob = f"{title}. {summary}".lower()
    return any(re.search(p, blob) for p in CALENDAR_EXTRA)


def _is_stale(title: str, summary: str = "") -> bool:
    if _is_stale_story(title, summary):
        return True
    blob = f"{title}. {summary}".lower()
    return any(re.search(p, blob, re.I) for p in STALE_STORY)


def _is_weak_wrap(title: str, summary: str = "") -> bool:
    blob = f"{title}. {summary}".lower()
    return any(re.search(p, blob, re.I) for p in WEAK_WRAP)


def _reject_candidate(item: NewsCandidate) -> bool:
    if _is_soft_story(item.title, item.summary):
        return True
    if _is_calendar(item.title, item.summary):
        return True
    if _is_stale(item.title, item.summary):
        return True
    if _is_weak_wrap(item.title, item.summary):
        return True
    tags = set(item.relevance_tags or [])
    if tags & {"very_stale", "soft_story", "results_calendar", "stale_period"}:
        return True
    blob = f"{item.title} {item.summary}".lower()
    title_l = (item.title or "").lower()
    # Pure overseas rates stories (judge from headline; enrichment footers often add Nifty/Sensex noise)
    if re.search(
        r"japanese\s+(?:government\s+)?bonds?|\bbank of japan\b|\bboj\b|\bjgbs?\b",
        title_l,
    ):
        if not re.search(r"\b(india|nifty|sensex|rupee|rbi)\b", title_l):
            return True
    return False


def _is_macro(item: NewsCandidate) -> bool:
    tags = set(item.relevance_tags or [])
    if tags & MACRO_TAGS:
        return True
    blob = f"{item.title} {item.summary}".lower()
    return bool(
        re.search(
            r"\b(crude|brent|opec|rbi|mpc|repo|inflation|cpi|gdp|fed|fomc|rupee|"
            r"usd\/inr|dollar index|yields?|tariff|geopolit|hormuz|gift nifty|"
            r"wall street|fii|dii|oil price|middle east)\b",
            blob,
        )
    )


def _has_tape_or_catalyst(item: NewsCandidate) -> bool:
    tags = set(item.relevance_tags or [])
    if any(t.startswith(("mover:", "sector:", "commodity:")) for t in tags):
        return True
    if _is_macro(item):
        return True
    return bool(
        tags
        & {
            "earnings",
            "mgmt_change",
            "legal_regulatory",
            "index_review",
            "crude_geopolitics",
            "large_cap",
            "post_close",
            "macro_policy",
            "global_macro",
            "capital_markets",
            "flows",
        }
    )


def _priority(item: NewsCandidate) -> float:
    tags = set(item.relevance_tags or [])
    score = float(item.score or 0)
    if any(t.startswith("mover:") for t in tags):
        score += 4
    if any(t.startswith("sector:") for t in tags):
        score += 2
    if any(t.startswith("commodity:") for t in tags):
        score += 2.5
    if tags & MACRO_TAGS or _is_macro(item):
        score += 4.5
    if "fresh_today" in tags:
        score += 2.5
    if "post_close" in tags:
        score += 2
    if "weak_tape_link" in tags and not _is_macro(item):
        score -= 4
    if "market_wrap" in tags:
        score -= 3
    if "intraday_timestamp" in tags or "intraday_framing" in tags:
        score -= 2
    if "stale" in tags:
        score -= 3
    return score


def _clip(text: str, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return text
    if len(text) <= limit:
        if not text.endswith((".", "?", "!")):
            text += "."
        return text
    window = text[:limit]
    ends = list(re.finditer(r"[.!?]", window))
    if ends and ends[-1].start() >= int(limit * 0.4):
        return text[: ends[-1].end()].strip()
    return window.rsplit(" ", 1)[0].rstrip(",:;.- ") + "."


def _complete(text: str) -> bool:
    text = text.strip()
    if len(text) < 45 or not text[0].isupper():
        return False
    if not text.endswith((".", "?", "!")):
        return False
    if re.search(
        r"\b(the|a|an|and|or|of|as|that|with|after|amid|chairman|persistent|domest|aug)\.$",
        text,
        re.I,
    ):
        return False
    if re.search(r"\b[A-Z]\.$", text):
        return False
    if re.match(r"^(his|her|their|pandey.?s|the comments?|these comments?)\b", text, re.I):
        return False
    if "#39;" in text:
        return False
    return True


def _polish_headline(title: str) -> str:
    t = _strip_source(title).rstrip(".")
    t = re.sub(
        r"^(Taking Stock|Market Live|Live Updates|Breaking|Just In|News)\s*:\s*",
        "",
        t,
        flags=re.I,
    )
    m = re.match(r"^From\b.+?(?:[-–—]|:)\s*(.+)$", t, re.I)
    if m and len(m.group(1).strip()) >= 35:
        t = m.group(1).strip()
    t = re.sub(r"(?<=[A-Za-z])\s*-\s*(?=[A-Za-z])", " - ", t)
    t = re.sub(r"\s+", " ", t).strip(" -")
    return t


def _tokens(text: str) -> set[str]:
    stop = {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "as", "for", "from",
        "after", "with", "up", "down", "by", "at", "is", "are", "was", "were",
        "shares", "stock", "stocks", "fall", "falls", "rise", "rises", "gain", "gains",
        "today", "wednesday", "tuesday", "monday", "thursday", "friday",
    }
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in stop}


def _bullet_from_candidate(item: NewsCandidate) -> str:
    title = _polish_headline(item.title)
    detail = _strip_source(item.summary)
    first = ""
    if detail:
        first = re.split(r"(?<=[.!?])\s+", detail.strip())[0].strip()
        first = re.sub(r"\bat\s+\d{1,2}(\.\d+)?\s*(am|pm)\b[^.]*?,?\s*", "", first, flags=re.I)
        first = re.sub(r"^[A-Z][^:]{1,40}:\s*", "", first).strip()
        first = first.lstrip(" ,:-")
        if first and not first.endswith((".", "?", "!")):
            first = ""

    title_toks = _tokens(title)
    first_toks = _tokens(first) if first else set()
    overlap = len(title_toks & first_toks) / max(len(title_toks), 1) if first_toks else 0.0

    if _complete(first) and overlap < 0.72 and len(first) >= 70:
        title_event = bool(
            re.search(
                r"\b(fall|fell|gain|surge|quit|resign|profit|result|q[1-4]|deal|funding|upgrade|downgrade)\b",
                title,
                re.I,
            )
        )
        first_event = bool(
            re.search(
                r"\b(fall|fell|gain|surge|quit|resign|profit|result|q[1-4]|deal|funding|upgrade|downgrade)\b",
                first,
                re.I,
            )
        )
        bullet = title if title_event and not first_event else first
    else:
        bullet = title

    bullet = _clip(bullet)
    if _is_calendar(bullet) or _is_stale(bullet) or _is_weak_wrap(bullet):
        return ""
    if not _complete(bullet) and len(bullet) < 50:
        return ""
    if re.search(r"\bon (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\.$", bullet, re.I):
        return ""
    return bullet


def _try_append(
    item: NewsCandidate,
    out: list[NewsUpdateItem],
    seen_clusters: set[str],
    seen_text: set[str],
) -> bool:
    cluster = _story_cluster_key(item.title)
    if cluster in seen_clusters:
        return False
    bullet = _bullet_from_candidate(item)
    if len(bullet) < 45:
        return False
    key = re.sub(r"[^a-z0-9]+", "", bullet.lower())[:80]
    if key in seen_text:
        return False
    seen_clusters.add(cluster)
    seen_text.add(key)
    out.append(NewsUpdateItem(text=bullet, source=item.source))
    return True


def select_news_updates(
    candidates: list[NewsCandidate],
    max_items: int = 6,
) -> list[NewsUpdateItem]:
    """Pick distinct bullets; reserve room for macro + stock/sector catalysts."""
    ranked = sorted(
        [n for n in candidates if not _reject_candidate(n)],
        key=_priority,
        reverse=True,
    )
    preferred = [n for n in ranked if _has_tape_or_catalyst(n)]
    pool = preferred if len(preferred) >= 3 else ranked

    out: list[NewsUpdateItem] = []
    seen_clusters: set[str] = set()
    seen_text: set[str] = set()

    # Up to 2 macro / geopolitics / policy / flows bullets first
    for item in (n for n in pool if _is_macro(n)):
        if sum(1 for _ in out) >= min(2, max_items):
            break
        _try_append(item, out, seen_clusters, seen_text)

    for item in pool:
        if len(out) >= max_items:
            break
        _try_append(item, out, seen_clusters, seen_text)

    if not out:
        out = [
            NewsUpdateItem(
                text="High-impact market headlines were limited at generation time. Please refine this section manually.",
                source="",
            )
        ]
    return out[:max_items]


def review_news_updates(items: list[NewsUpdateItem]) -> list[str]:
    """Return quality issues found in selected bullets (empty = pass)."""
    issues: list[str] = []
    texts = [i.text for i in items]
    clusters = [_story_cluster_key(t) for t in texts]
    if len(clusters) != len(set(clusters)):
        issues.append("duplicate story clusters")
    for i, t in enumerate(texts, 1):
        if t.count(":") >= 2:
            issues.append(f"bullet {i}: multiple colons")
        if re.search(r":\s*[A-Z][^:]{0,40}:\s*", t):
            issues.append(f"bullet {i}: glued theme labels")
        if _is_calendar(t):
            issues.append(f"bullet {i}: results calendar")
        if _is_stale(t):
            issues.append(f"bullet {i}: stale period story")
        if _is_weak_wrap(t):
            issues.append(f"bullet {i}: weak market wrap")
        if re.search(r"\bon (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\.$", t, re.I):
            issues.append(f"bullet {i}: truncated month")
        if len(t) < 45:
            issues.append(f"bullet {i}: too short")
        if "#39;" in t or "�" in t:
            issues.append(f"bullet {i}: encoding junk")
    return issues
