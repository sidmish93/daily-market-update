"""Select and polish News & Updates bullets for the free (no-LLM) path."""

from __future__ import annotations

import html as html_lib
import re

from app.models import NewsCandidate, NewsUpdateItem, StockFocusRow
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

_FOCUS_NAME_STOP = {
    "the", "a", "an", "and", "or", "of", "in", "on", "for", "to",
    "india", "indian", "limited", "ltd", "plc", "inc", "corp", "corporation",
    "industries", "industry", "products", "product", "services", "service",
    "company", "companies", "group", "bank", "banks", "finance", "financial",
    "motors", "motor", "electronics", "consumer", "power", "energy", "tech",
    "technologies", "technology", "pharma", "pharmaceuticals", "labs",
    "laboratories", "cement", "steel", "infra", "infrastructure",
}

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
    r"on nifty midcap",
    r"on nifty 500",
    r"on nifty50",
    r"shares? (?:rise|gain|fall|slip).{0,40}\bon nifty\b",
    r"finished the day marginally",
    r"marginally higher, propelled",
    r"propelled by ongoing tensions",
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
        r"^(Taking Stock|Market Live|Live Updates|Breaking|Just In|News|Global Market)\s*:\s*",
        "",
        t,
        flags=re.I,
    )
    t = re.sub(r"^(?:India'?s|Indian)\s+", "", t, flags=re.I)
    t = re.sub(r"\s+[-–—]\s+[A-Za-z0-9][A-Za-z0-9 .&'’]{0,48}$", "", t).strip()
    m = re.match(r"^From\b.+?(?:[-–—]|:)\s*(.+)$", t, re.I)
    if m and len(m.group(1).strip()) >= 35:
        t = m.group(1).strip()
    t = re.sub(r"(?<=[A-Za-z])\s*-\s*(?=[A-Za-z])", " - ", t)
    t = re.sub(r"\s+", " ", t).strip(" -")
    if re.search(r"chandrasekaran|n chandra", t, re.I):
        t = re.sub(
            r"\bresign(?:s|ed|ing)?\b",
            "will not seek reappointment",
            t,
            count=1,
            flags=re.I,
        )
    return t


def _align_news_pct(bullet: str, movers: list | None) -> str:
    if not movers or not bullet:
        return bullet
    low = bullet.lower()
    matched = None
    for m in movers:
        name = (m.company or "").lower()
        short = " ".join(name.split()[:2]) if name else ""
        sym = (m.symbol or "").lower()
        if short and len(short) >= 5 and short in low:
            matched = m
            break
        if sym and len(sym) >= 4 and sym in low.replace(" ", ""):
            matched = m
            break
    if matched is None or matched.change_pct is None:
        return bullet
    pct = abs(matched.change_pct)
    actual = f"{pct:.2f}".rstrip("0").rstrip(".")

    def _repl(m: re.Match[str]) -> str:
        reported = float(m.group(2))
        if abs(reported - pct) < 0.6:
            return m.group(0)
        return f"{m.group(1)} {actual}%"

    return re.sub(
        r"\b((?:shares?|stock)\s+"
        r"(?:fall|falls|fell|drop(?:s|ped)?|slip(?:s|ped)?|slump(?:s|ed)?|"
        r"surge(?:s|d)?|gain(?:s|ed)?|jump(?:s|ed)?|rise(?:s)?|rose|tumble|tumbles|tank|tanks))\s+"
        r"(?!up\s+to\b)(\d+(?:\.\d+)?)\s*%",
        _repl,
        bullet,
        count=1,
        flags=re.I,
    )


def _tokens(text: str) -> set[str]:
    stop = {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "as", "for", "from",
        "after", "with", "up", "down", "by", "at", "is", "are", "was", "were",
        "shares", "stock", "stocks", "fall", "falls", "rise", "rises", "gain", "gains",
        "today", "wednesday", "tuesday", "monday", "thursday", "friday",
    }
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in stop}


def _bullet_from_candidate(item: NewsCandidate, movers: list | None = None) -> str:
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
    bullet = _align_news_pct(bullet, movers)
    if re.search(r"chandrasekaran|n chandra", bullet, re.I):
        bullet = re.sub(
            r"\bresign(?:s|ed|ing)?\b",
            "will not seek reappointment",
            bullet,
            count=1,
            flags=re.I,
        )
    if _is_calendar(bullet) or _is_stale(bullet) or _is_weak_wrap(bullet):
        return ""
    if not _complete(bullet) and len(bullet) < 50:
        return ""
    if re.search(r"\bon (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\.$", bullet, re.I):
        return ""
    return bullet


def _significant_tokens(text: str) -> set[str]:
    stop = {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "as", "for", "from",
        "after", "with", "up", "down", "by", "at", "is", "are", "was", "were", "vs",
        "shares", "stock", "stocks", "fall", "falls", "rise", "rises", "gain", "gains",
        "today", "session", "moved", "move", "also", "mattered",
    }
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in stop}


def _focus_name_needles(stock: str) -> list[str]:
    parts = [
        p
        for p in re.findall(r"[a-z0-9]+", (stock or "").lower())
        if p not in _FOCUS_NAME_STOP and len(p) > 2
    ]
    needles: list[str] = []
    if not parts:
        return needles
    needles.append(parts[0])
    if len(parts) >= 2:
        needles.append(f"{parts[0]} {parts[1]}")
    return needles


def _mentions_focus_name(text: str, stock: str) -> bool:
    low = f" {(text or '').lower()} "
    for needle in _focus_name_needles(stock):
        if f" {needle} " in low or low.strip().startswith(needle + " "):
            return True
        # Compact ticker-like forms (e.g. gcpl) already covered by needles when short
        if " " not in needle and re.search(rf"\b{re.escape(needle)}\b", low):
            return True
    return False


def overlaps_focus_story(text: str, focus_rows: list[StockFocusRow] | None) -> bool:
    """True when a News bullet repeats a Stocks-in-Focus catalyst/reason."""
    if not focus_rows or not (text or "").strip():
        return False
    cluster = _story_cluster_key(text)
    text_tokens = _significant_tokens(text)
    for row in focus_rows:
        reason = (row.whats_happening or "").strip()
        stock = (row.stock or "").strip()
        if reason and _story_cluster_key(reason) == cluster:
            return True
        if stock and _mentions_focus_name(text, stock):
            # Same name + shared catalyst tokens => duplicate reason
            reason_tokens = _significant_tokens(reason)
            if reason_tokens and len(text_tokens & reason_tokens) >= 2:
                return True
            # Same Focus name with earnings/results framing is almost always the same story
            if re.search(
                r"\b(q[1-4]|fy\d{2}|results?|earnings?|profit|ceo|resign|exit|deal|order)\b",
                text,
                flags=re.I,
            ):
                return True
    return False


def filter_news_against_focus(
    items: list[NewsUpdateItem],
    focus_rows: list[StockFocusRow] | None,
) -> list[NewsUpdateItem]:
    return [i for i in items if not overlaps_focus_story(i.text, focus_rows)]


def _try_append(
    item: NewsCandidate,
    out: list[NewsUpdateItem],
    seen_clusters: set[str],
    seen_text: set[str],
    movers: list | None = None,
    focus_rows: list[StockFocusRow] | None = None,
) -> bool:
    cluster = _story_cluster_key(item.title)
    if cluster in seen_clusters:
        return False
    # Also block when the candidate title/summary itself is a Focus reason
    probe = f"{item.title}. {item.summary}"
    if overlaps_focus_story(probe, focus_rows) or overlaps_focus_story(item.title, focus_rows):
        return False
    bullet = _bullet_from_candidate(item, movers=movers)
    if len(bullet) < 45:
        return False
    if overlaps_focus_story(bullet, focus_rows):
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
    movers: list | None = None,
    focus_rows: list[StockFocusRow] | None = None,
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
    # Seed with Focus reasons so the same story cannot reappear in News
    for row in focus_rows or []:
        reason = (row.whats_happening or "").strip()
        if reason:
            seen_clusters.add(_story_cluster_key(reason))

    # Up to 2 macro / geopolitics / policy / flows bullets first
    for item in (n for n in pool if _is_macro(n)):
        if sum(1 for _ in out) >= min(2, max_items):
            break
        _try_append(
            item, out, seen_clusters, seen_text, movers=movers, focus_rows=focus_rows
        )

    for item in pool:
        if len(out) >= max_items:
            break
        _try_append(
            item, out, seen_clusters, seen_text, movers=movers, focus_rows=focus_rows
        )

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
