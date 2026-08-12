"""Event-driven Stocks in Focus selection within the Nifty 200 universe."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models import MoverRow, NewsCandidate, StockFocusRow
from app.scrapers.market_data import PREFERRED_DISPLAY_NAMES, _load_nse_symbol_names, _shorten_company_name
from app.utils import strip_em_dashes

EVENT_HINTS = (
    "result",
    "earnings",
    "net profit",
    "pat ",
    "q1",
    "q2",
    "q3",
    "q4",
    "fy2",
    "guidance",
    "outlook",
    "order win",
    "wins order",
    "bags order",
    "contract",
    "acquisition",
    "stake sale",
    "block deal",
    "bulk deal",
    "signed a deal",
    "inks deal",
    "qip",
    "ipo",
    "sebi",
    "court",
    "dismiss",
    "fraud",
    "probe",
    "raid",
    "penalty",
    "index",
    "inclusion",
    "exclusion",
    "reshuffle",
    "nifty 50",
    "replac",
    "upgrade",
    "downgrade",
    "rating",
    "dividend",
    "buyback",
    "demerger",
    "merger",
    "jv ",
    "joint venture",
    "fda",
    "approval",
    "clears",
    "surges after",
    "jumps after",
    "falls after",
    "slides after",
    "rises after",
    "resign",
    "resignation",
    "re-appointment",
    "reappointment",
    "step down",
    "steps down",
    "quits",
    "sudden exit",
    "ceo",
    "managing director",
    "chairman",
    "plunges",
    "crashes on",
    "hits 52-week",
    "downgrade",
    "tank",
)

WEAK_LISTICLE_HINTS = (
    "top gainers",
    "top losers",
    "gainers & losers",
    "gainers and losers",
    "stocks to watch",
    "market live",
    "closing bell",
    "stock market crash highlights",
    "market highlights",
    "worst performer",
    "extend declines",
    "extend gains",
    "live blog",
    "sensex, nifty",
    "nifty, sensex",
)

ROUNDUP_HINTS = (
    r"\band\s+\d+\s+more\b",
    r"\b\d+\s+more\s+on\b",
    r"\blive updates\b",
    r"\bcompanies\s+to\s+(?:announce|report|release)\b",
    r"\bresults\s+today\b",
    r"\bearnings\s+calendar\b",
    r"\bstocks?\s+trade\s+(?:weak|strong)\b",
    r"\bamong\s+(?:the\s+)?(?:top|biggest)\b",
)

AMBIGUOUS_SYMBOLS = {
    "OIL",
    "GAS",
    "GOLD",
    "BANK",
    "IDEA",
    "PAGE",
    "UNION",
    "POWER",
    "TECH",
    "INDIA",
}


@dataclass
class EventFocusCandidate:
    mover: MoverRow
    news: list[NewsCandidate]
    score: float


def _aliases_for(mover: MoverRow) -> list[str]:
    sym = (mover.symbol or "").upper()
    names: set[str] = set()
    if mover.company:
        names.add(mover.company.lower())
    preferred = PREFERRED_DISPLAY_NAMES.get(sym, "")
    if preferred:
        names.add(preferred.lower())
    full = _load_nse_symbol_names().get(sym, "")
    if full:
        names.add(full.lower())
        names.add(_shorten_company_name(full).lower())

    extras = {
        "NAUKRI": ["info edge", "info edge (india)"],
        "POLICYBZR": ["policybazaar", "pb fintech", "policy bazaar"],
        "ZYDUSLIFE": ["zydus lifesciences", "zydus life"],
        "DRREDDY": ["dr reddy", "dr. reddy", "dr reddy's"],
        "BHARATFORG": ["bharat forge"],
        "IDEA": ["vodafone idea"],
        "GLAND": ["gland pharma"],
        "BSE": ["bse ltd", "bse limited"],
        "WIPRO": ["wipro"],
        "MAXHEALTH": ["max healthcare"],
        "TATACONSUM": ["tata consumer", "tata consumer products"],
        "NESTLEIND": ["nestle india", "nestlé india"],
        "GODREJPROP": ["godrej properties"],
        "OIL": ["oil india"],
        "ONGC": ["ongc", "oil and natural gas"],
        "MCX": ["mcx", "multi commodity exchange"],
        "VEDL": ["vedanta"],
        "SIEMENS": ["siemens"],
        "MRF": ["mrf"],
        "BANKINDIA": ["bank of india"],
        "BOSCHLTD": ["bosch"],
        "POLYCAB": ["polycab"],
        "HAL": ["hal", "hindustan aeronautics"],
        "PNB": ["pnb", "punjab national bank"],
        "BHEL": ["bhel"],
        "GODREJCP": ["godrej consumer", "godrej consumer products", "gcpl"],
        "NATIONALUM": ["nalco", "national aluminium"],
        "ASTRAL": ["astral"],
        "PIIND": ["pi industries", "pi ind"],
    }
    names.update(extras.get(sym, []))

    # Include ticker aliases (len>=3) except known ambiguous short tokens
    if sym and sym not in AMBIGUOUS_SYMBOLS and len(sym) >= 3:
        names.add(sym.lower())

    cleaned = []
    for n in names:
        n = " ".join(n.split()).strip()
        if len(n) < 3:
            continue
        if n in {"india", "bank", "oil", "gas", "gold", "power", "tech"}:
            continue
        cleaned.append(n)
    return sorted(set(cleaned), key=len, reverse=True)


def _is_event_news(item: NewsCandidate) -> bool:
    title = item.title.lower()
    blob = f"{item.title} {item.summary}".lower()
    if any(h in title for h in WEAK_LISTICLE_HINTS):
        return False
    if any(h in blob for h in EVENT_HINTS):
        return True
    tags = {t.lower() for t in item.relevance_tags}
    return bool(
        tags
        & {
            "earnings",
            "mgmt_change",
            "legal_regulatory",
            "index_review",
            "capital_markets",
        }
    )


def _is_roundup_headline(item: NewsCandidate) -> bool:
    title = item.title.lower()
    blob = f"{item.title} {item.summary}".lower()
    if any(re.search(pat, title) for pat in ROUNDUP_HINTS):
        return True
    if any(h in title for h in WEAK_LISTICLE_HINTS):
        return True
    if title.count(",") >= 3 and any(k in title for k in ("result", "q1", "q2", "q3", "q4")):
        return True
    # Shared basket: 2+ listed names with a common move verb / "fall up to"
    name_hits = len(
        re.findall(
            r"\b(?:max healthcare|fortis|apollo|tcs|infosys|reliance|hdfc|wipro|hindalco|nalco)\b",
            title,
        )
    )
    if name_hits >= 2 and re.search(
        r"\b(fall|gain|slip|surge|rally|drop|weak|decline|fall up to|gain up to)\b",
        title,
    ):
        return True
    if re.search(r"\bhospital stocks?\b|\bpharma stocks?\b|\bit stocks?\b|\bbank stocks?\b", blob):
        if name_hits >= 2 or "fall up to" in title or "gain up to" in title:
            return True
    # "fall/gain up to" without a company-specific catalyst is usually a basket blurb
    if re.search(r"\b(?:fall|gain) up to\b", title) and not re.search(
        r"\b(resign\w*|result\w*|profit|plunge\w*|q[1-4]|upgrade|downgrade|order|deal|52-week)\b",
        title,
    ):
        return True
    return False


def _is_company_specific(item: NewsCandidate, aliases: list[str]) -> bool:
    if _is_roundup_headline(item):
        return False
    title = item.title.lower().strip()
    for alias in aliases:
        if not alias:
            continue
        if title.startswith(alias):
            return True
        if re.search(
            rf"\b{re.escape(alias)}\b.{{0,80}}\b("
            r"q[1-4]|fy\d{2}|result\w*|profit|pat|loss|order|deal|index|nifty|inflow|"
            r"resign\w*|quits?|exit|plunges?|slides?|surges?|jumps?|crashes?|hits|upgrade|downgrade|"
            r"sebi|probe|penalty|ceo|tank"
            r")\b",
            title,
        ):
            return True
    return False


def _alias_in_text(alias: str, blob: str) -> bool:
    alias = alias.strip().lower()
    if not alias:
        return False
    if " " in alias:
        return alias in blob
    return bool(re.search(rf"\b{re.escape(alias)}\b", blob))


def _title_mentions(item: NewsCandidate, aliases: list[str]) -> bool:
    title = item.title.lower()
    return any(_alias_in_text(alias, title) for alias in aliases)


def _snippet_for(item: NewsCandidate, aliases: list[str]) -> str:
    title = strip_em_dashes(item.title or "")
    summary = strip_em_dashes(item.summary or "")
    # Prefer a clean company-led headline over market-wrap paste
    if _is_company_specific(item, aliases) and not _is_roundup_headline(item):
        # Drop hollow prefixes
        cleaned = re.sub(
            r"^(Stock Market Crash Highlights|Market Highlights|Live Updates)\s*:\s*",
            "",
            title,
            flags=re.I,
        ).strip()
        return cleaned[:240]

    sentences = re.split(r"(?<=[.!?])\s+", summary)
    for sentence in sentences:
        low = sentence.lower()
        if any(_alias_in_text(alias, low) for alias in aliases) and any(
            k in low
            for k in (
                "profit",
                "pat",
                "revenue",
                "result",
                "loss",
                "order",
                "index",
                "nifty",
                "crore",
                "inflow",
                "resign",
                "plunge",
            )
        ):
            return sentence.strip()[:240]
    if any(_alias_in_text(alias, title.lower()) for alias in aliases) and not _is_roundup_headline(item):
        return title.strip()[:240]
    for sentence in sentences:
        low = sentence.lower()
        if any(_alias_in_text(alias, low) for alias in aliases):
            return sentence.strip()[:240]
    return title.strip()[:240] if title else (sentences[0] if sentences else summary)[:240]


def _best_article(hits: list[NewsCandidate], aliases: list[str]) -> NewsCandidate | None:
    # Strict: only company-specific event headlines
    specific = [n for n in hits if _is_company_specific(n, aliases)]
    if not specific:
        return None

    def rank(n: NewsCandidate) -> float:
        score = float(n.score)
        if _is_company_specific(n, aliases):
            score += 25
        if _is_roundup_headline(n):
            score -= 40
        tags = {t.lower() for t in n.relevance_tags}
        if tags & {"mgmt_change", "earnings", "legal_regulatory", "index_review", "capital_markets"}:
            score += 8
        if "fresh_today" in tags:
            score += 3
        return score

    return max(specific, key=rank)


def _is_strong_catalyst(item: NewsCandidate) -> bool:
    """Secondary-band (1.5-3%) names need a clear catalyst, not a soft mention."""
    tags = {t.lower() for t in item.relevance_tags}
    if tags & {"mgmt_change", "earnings", "legal_regulatory", "index_review", "capital_markets"}:
        return True
    blob = f"{item.title} {item.summary}".lower()
    return bool(
        re.search(
            r"\b(q[1-4]|fy\d{2}|result\w*|net profit|pat|ceo|resign\w*|quits?|"
            r"upgrade|downgrade|order win|bags order|block deal|sebi|probe|"
            r"inclusion|exclusion|merger|acquisition)\b",
            blob,
        )
    )


def _importance_score(mover: MoverRow, article: NewsCandidate, *, primary: bool) -> float:
    move = abs(mover.change_pct or 0)
    tags = {t.lower() for t in article.relevance_tags}
    score = float(article.score) + move * (1.4 if primary else 0.9)
    if tags & {"mgmt_change"}:
        score += 18
    if tags & {"earnings"}:
        score += 14
    if tags & {"legal_regulatory", "index_review", "capital_markets"}:
        score += 10
    if "fresh_today" in tags:
        score += 4
    if primary:
        score += 8  # prefer big movers when quality is similar
    return score


def select_event_driven_focus(
    nifty200: list[MoverRow],
    news: list[NewsCandidate],
    *,
    min_move_pct: float = 1.5,
    primary_move_pct: float = 3.0,
    max_items: int = 5,
) -> list[EventFocusCandidate]:
    """
    Hybrid Stocks in Focus:
    1) Primary pool: Nifty 200 abs move >= 3% with company-specific news.
    2) Secondary pool: abs move >= 1.5% only if the news is a strong catalyst.
    3) Rank by importance (event type + move + freshness); return up to 5.
    Never pad with pure price movers or shared roundups.
    """
    if not nifty200 or not news:
        return []

    ranked_news = sorted(news, key=lambda n: -n.score)
    event_news = [n for n in ranked_news if _is_event_news(n) and not _is_roundup_headline(n)]
    if not event_news:
        return []

    primary: list[tuple[float, MoverRow, NewsCandidate, list[NewsCandidate]]] = []
    secondary: list[tuple[float, MoverRow, NewsCandidate, list[NewsCandidate]]] = []

    for mover in nifty200:
        move = abs(mover.change_pct or 0)
        if move < min_move_pct:
            continue
        aliases = _aliases_for(mover)
        if not aliases:
            continue
        hits = [n for n in event_news if _title_mentions(n, aliases)]
        best = _best_article(hits, aliases)
        if best is None:
            continue

        is_primary = move >= primary_move_pct
        if not is_primary and not _is_strong_catalyst(best):
            continue

        score = _importance_score(mover, best, primary=is_primary)
        bucket = primary if is_primary else secondary
        bucket.append((score, mover, best, hits))

    # Within each band: largest absolute move first, then event importance
    primary.sort(key=lambda x: (-abs(x[1].change_pct or 0), -x[0]))
    secondary.sort(key=lambda x: (-abs(x[1].change_pct or 0), -x[0]))
    ordered = primary + secondary

    selected: dict[str, EventFocusCandidate] = {}
    used_article_keys: set[str] = set()

    for score, mover, best, hits in ordered:
        key = mover.symbol or mover.company
        if key in selected:
            continue

        article_key = best.url or best.title
        if article_key in used_article_keys:
            aliases = _aliases_for(mover)
            alternatives = [
                n
                for n in hits
                if _is_company_specific(n, aliases) and (n.url or n.title) not in used_article_keys
            ]
            best = _best_article(alternatives, aliases)
            if best is None:
                continue
            article_key = best.url or best.title
            score = _importance_score(mover, best, primary=abs(mover.change_pct or 0) >= primary_move_pct)

        selected[key] = EventFocusCandidate(mover=mover, news=[best], score=score)
        used_article_keys.add(article_key)
        if len(selected) >= max_items:
            break

    # Preserve hybrid priority: primary band first, then largest moves, then score
    primary_keys = {
        (m.symbol or m.company)
        for _, m, _, _ in primary
    }
    rows = list(selected.values())
    rows.sort(
        key=lambda c: (
            0 if (c.mover.symbol or c.mover.company) in primary_keys else 1,
            -abs(c.mover.change_pct or 0),
            -c.score,
        )
    )
    return rows[:max_items]


def _align_pct_with_move(snippet: str, mover: MoverRow) -> str:
    """Prefer session LTP % over an intraday share-move % in the headline.

    Only rewrites share-price phrasing (e.g. 'shares fall 10%'), never
    earnings figures like 'profit jumps 17%'.
    """
    pct = mover.change_pct
    if pct is None or not snippet:
        return snippet
    actual = f"{abs(pct):.2f}".rstrip("0").rstrip(".")

    def _repl(m: re.Match[str]) -> str:
        reported = float(m.group(2))
        if abs(reported - abs(pct)) < 0.6:
            return m.group(0)
        return f"{m.group(1)} {actual}%"

    return re.sub(
        r"\b((?:shares?|stock)\s+"
        r"(?:fall|falls|fell|drop(?:s|ped)?|slip(?:s|ped)?|slump(?:s|ed)?|"
        r"surge(?:s|d)?|gain(?:s|ed)?|jump(?:s|ed)?|rise(?:s)?|rose))\s+"
        r"(?!up\s+to\b)(\d+(?:\.\d+)?)\s*%",
        _repl,
        snippet,
        count=1,
        flags=re.I,
    )


def _polish_focus_snippet(snippet: str, item: NewsCandidate) -> str:
    """Tone down overstated catalysts when the article body is more precise."""
    snippet = re.sub(
        r"\s+[-–—]\s+[A-Za-z0-9][A-Za-z0-9 .&'’]{0,48}$",
        "",
        snippet,
    ).strip()
    snippet = re.sub(r"^(?:India'?s|Indian)\s+", "", snippet, flags=re.I).strip()
    blob = f"{item.title} {item.summary}".lower()
    if re.search(r"chandrasekaran|n chandra", snippet, re.I) and re.search(
        r"not seek reappointment|will continue|term ends|february 2027|feb(?:ruary)?\s*20,? 2027",
        blob,
        re.I,
    ):
        snippet = re.sub(
            r"\bresign(?:s|ed|ing)?\b",
            "will not seek reappointment",
            snippet,
            count=1,
            flags=re.I,
        )
    return snippet


def candidates_to_focus_rows(candidates: list[EventFocusCandidate]) -> list[StockFocusRow]:
    rows: list[StockFocusRow] = []
    for c in candidates:
        top = c.news[0]
        aliases = _aliases_for(c.mover)
        snippet = _polish_focus_snippet(_snippet_for(top, aliases), top)
        snippet = _align_pct_with_move(snippet, c.mover)
        rows.append(
            StockFocusRow(
                stock=c.mover.company,
                move=c.mover.change,
                whats_happening=strip_em_dashes(snippet.rstrip(".") + "."),
                direction=c.mover.direction,
            )
        )
    return rows
