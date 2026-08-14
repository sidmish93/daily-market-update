"""Insight-focused narrative generation for Key Takeaways and News & Updates."""

from __future__ import annotations

import json
import logging
import os
import re

from anthropic import Anthropic

from app.models import NarrativeBlock, NewsUpdateItem, ReportDraft, StockFocusRow
from app.scrapers.focus import candidates_to_focus_rows, select_event_driven_focus
from app.scrapers.news import _is_results_calendar, _is_soft_story, _story_cluster_key
from app.scrapers.news_updates import (
    filter_news_against_focus,
    overlaps_focus_story,
    review_news_updates,
    select_news_updates,
)
from app.utils import strip_em_dashes

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a senior India equities strategist writing the insight sections of a Daily Market Update.

The report already contains tables for prices and % changes. Your job is NOT to restate those tables.
Your job is to explain what mattered, why it mattered, and what a serious market reader should notice.

Hard rules:
- Formal tone.
- Never use em dashes or en dashes. Use commas, colons, or hyphens.
- Do not invent company-specific facts that are not supported by the provided news or market context.
- Numbers from the market tables may be used sparingly to anchor an insight.
- Do not put publication names inside the news text itself. Put them only in the separate "source" field.
- No slang, emojis, or exclamation marks.
- Output valid JSON only.
"""

USER_TEMPLATE = """Write the insight sections for the Daily Market Update dated {date_label}.

Return JSON:
{{
  "key_takeaways": ["...", "...", "...", "...", "..."],
  "news_updates": [
    {{"text": "Theme: insight sentence", "source": "Moneycontrol"}},
    {{"text": "...", "source": "Mint"}}
  ],
  "stocks_in_focus": [
    {{"stock": "Name", "move": "▲ x.xx% or ▼ x.xx%", "whats_happening": "Catalyst explanation.", "direction": "up|down|flat"}}
  ]
}}

KEY TAKEAWAYS (exactly 4 bullets):
Write as an analyst brief, not as subsection labels.
Recommended arc:
1) Session verdict: Sensex/Nifty story (call out divergence when they disagree), with the main force and 1-2 named stock drivers.
2) Internals as a judgment: broader vs headline contrast, then breadth as support.
3) Domestic sector/stock story (name stocks inside the sector line when useful).
4) Macro with a why when news supports it (CPI/Fed/RBI/crude geopolitics); otherwise a second domestic point. Do not dump prices alone.
Lead with the point; avoid canned openers such as "Equities ended", "Stock-specific flows centred on", or "On the macro side".
Use figures only where they sharpen the point. Avoid laundry lists of every index.
Do not add a forward-looking watchpoint bullet.

NEWS & UPDATES (5-7 items, quality over quantity):
These should be the most informative part of the note.
- Select only high-impact developments from news_candidates (earnings with moves, index changes, legal/regulatory outcomes, crude/geopolitics, large-cap group news, material deals/fundraising).
- One theme = one bullet. Merge duplicate Tata/Chandrasekaran (or any repeated) stories into a single stronger item.
- Do NOT repeat any Stocks in Focus catalyst/reason. If a name is already in event_driven_focus_candidates, leave that story for Stocks in Focus and pick a different News item.
- Drop soft profiles, career retrospectives, and results calendars ("X, Y, Z and 545 more").
- Prefer close-of-day framing. Avoid mid-session timestamps like "at 2 PM".
- Each item has:
  - text: complete insight bullet (what happened + why it matters). Optional short theme label before a colon is good. Never truncate mid-word or mid-name.
  - source: the publication name from the matched news_candidate (for the editor only; it will not appear in the PDF).
- Never put the publication name inside the text field.
- Prefer enriched summaries over thin headlines.
- Where a stock moved on the news, mention the move.

STOCKS IN FOCUS:
- Use ONLY the preselected list in event_driven_focus_candidates.
- These are Nifty 200 names already matched to an event/news catalyst. Do not invent extra names from top gainers/losers unless they appear in that list.
- Return 0 to N rows matching that list size (flexible count). If the list is empty, return [].
- whats_happening must explain the news catalyst only. Do not restate the percentage move, and do not add boilerplate such as "in the Nifty 200 universe" or "event-driven flow". The move already appears in the Move column.
- Keep stock names and move strings consistent with the candidate payload.

TODAY'S CONTEXT:
{payload}
"""


def _strip_source_prefix(text: str) -> str:
    cleaned = strip_em_dashes(text).strip()
    cleaned = re.sub(
        r"^(Moneycontrol(?: Markets| Business)?|CNBC TV18(?: Business)?|NDTV Profit|"
        r"Economic Times(?: Markets| Stocks)?|Business Standard(?: Companies)?|"
        r"Mint(?: Markets| Companies)?)\s*:\s*",
        "",
        cleaned,
        flags=re.I,
    )
    return cleaned.strip()


def _clip_sentence(text: str, limit: int = 280) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return text
    if len(text) <= limit:
        if not text.endswith((".", "?", "!")):
            text += "."
        return text
    window = text[:limit]
    ends = list(re.finditer(r"[.!?]", window))
    if ends and ends[-1].start() >= int(limit * 0.45):
        return text[: ends[-1].end()].strip()
    cut = window.rsplit(" ", 1)[0].rstrip(",:;.- ")
    return cut + "."


def _looks_complete_sentence(text: str) -> bool:
    text = text.strip()
    if len(text) < 50 or not text[0].isupper():
        return False
    if not text.endswith((".", "?", "!")):
        return False
    # Reject sentences that clearly end mid-thought
    if re.search(
        r"\b(the|a|an|and|or|of|as|that|with|after|amid|chairman|persistent|domest)\.$",
        text,
        re.I,
    ):
        return False
    # "… Chairman N." / cut-off initials
    if re.search(r"\b[A-Z]\.$", text):
        return False
    # Orphan continuations that need the headline for context
    if re.match(
        r"^(his|her|their|pandey.?s|the comments?|these comments?)\b",
        text,
        re.I,
    ):
        return False
    return False if "day#39;" in text.lower() or "#39;" in text else True


def _significant_tokens(text: str) -> set[str]:
    stop = {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "as", "for", "from",
        "after", "with", "up", "down", "by", "at", "is", "are", "was", "were", "vs",
        "shares", "stock", "stocks", "fall", "falls", "rise", "rises", "gain", "gains",
        "today", "wednesday", "tuesday", "monday", "thursday", "friday",
    }
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if len(w) > 2 and w not in stop}


def _clean_headline(title: str) -> str:
    title = html_unescape_basic(_strip_source_prefix(title)).strip()
    # Drop hollow theme prefixes that add a useless colon layer
    title = re.sub(
        r"^(Taking Stock|Market Live|Live Updates|Breaking|Just In)\s*:\s*",
        "",
        title,
        flags=re.I,
    )
    return title.rstrip(".")


def _news_priority(item) -> float:
    tags = set(item.relevance_tags or [])
    score = float(item.score or 0)
    if any(t.startswith("mover:") for t in tags):
        score += 4
    if any(t.startswith("sector:") for t in tags):
        score += 2
    if any(t.startswith("commodity:") for t in tags):
        score += 2
    if "fresh_today" in tags:
        score += 2
    if "post_close" in tags:
        score += 1.5
    if "weak_tape_link" in tags:
        score -= 3
    if "market_wrap" in tags:
        score -= 2
    return score


def _fallback_news_bullet(title: str, summary: str) -> str:
    """One clean sentence. Never glue 'title: summary' (that creates random colons)."""
    title = _clean_headline(title)
    detail = html_unescape_basic(_strip_source_prefix(summary))
    first = ""
    if detail:
        first = re.split(r"(?<=[.!?])\s+", detail.strip())[0].strip()
        first = re.sub(r"\bat\s+\d{1,2}(\.\d+)?\s*(am|pm)\b[^.]*?,?\s*", "", first, flags=re.I)
        first = re.sub(r"^[A-Z][^:]{1,40}:\s*", "", first).strip()
        first = first.lstrip(" ,:-")
        if first and not first.endswith((".", "?", "!")):
            first = ""

    title_tokens = _significant_tokens(title)
    first_tokens = _significant_tokens(first) if first else set()
    overlap = (
        len(title_tokens & first_tokens) / max(len(title_tokens), 1)
        if first_tokens
        else 0.0
    )

    if _looks_complete_sentence(first) and overlap < 0.75 and len(first) >= 70:
        bullet = first
    else:
        bullet = title

    return _clip_sentence(bullet)


def html_unescape_basic(text: str) -> str:
    import html as _html

    text = _html.unescape(text or "")
    return text.replace("#39;", "'").replace("&#39;", "'")


def _dedupe_sector_names(names: list[str]) -> list[str]:
    """Avoid awkward pairs like 'PSU Bank, Bank'."""
    cleaned: list[str] = []
    lowered: list[str] = []
    for name in names:
        n = (name or "").strip()
        if not n:
            continue
        low = n.lower()
        if any(low == x or low in x or x in low for x in lowered):
            # Keep the more specific (longer) label already present
            continue
        # If a more specific name arrives later, replace the generic one
        replaced = False
        for i, prev in enumerate(lowered):
            if prev in low and len(low) > len(prev):
                cleaned[i] = n
                lowered[i] = low
                replaced = True
                break
        if not replaced:
            cleaned.append(n)
            lowered.append(low)
    return cleaned


def _catalyst_short(focus_row: StockFocusRow, limit: int = 90) -> str:
    reason = (focus_row.whats_happening or "").strip().rstrip(".")
    if not reason:
        return ""
    # Drop hollow transcript / meta labels that leak into focus copy
    if re.search(r"\bearnings call transcript\b|\bq[1-4]\s+20\d{2}\s+stock\b", reason, flags=re.I):
        reason = re.split(
            r"\bearnings call transcript\b|\bq[1-4]\s+20\d{2}\s+stock\b",
            reason,
            maxsplit=1,
            flags=re.I,
        )[0].strip(" :,-")
        if len(reason) < 18:
            return ""
    stock_l = (focus_row.stock or "").lower().strip()
    short = reason
    if stock_l and short.lower().startswith(stock_l):
        short = short[len(focus_row.stock) :].lstrip(" ,:-")
    parts = stock_l.split()
    if len(parts) >= 2:
        prefix = " ".join(parts[:2])
        if short.lower().startswith(prefix):
            short = short[len(prefix) :].lstrip(" ,:-")
    # Prefer a tight CEO / management-exit noun phrase
    m_ceo = re.search(
        r"\b((?:sudden\s+)?ceo\s+(?:exit|quit\w*|resign\w*|departure))",
        short,
        flags=re.I,
    )
    if m_ceo:
        short = m_ceo.group(1).strip()
    else:
        m_after = re.search(r"\bafter\b\s+(.+)$", short, flags=re.I)
        if m_after and len(m_after.group(1).strip()) >= 20:
            short = m_after.group(1).strip()
    short = re.sub(
        r"^(?:shares?|stock|logs?|posts?|as)\b[\s,:-]*",
        "",
        short,
        flags=re.I,
    ).strip()
    short = re.sub(r"\s+[-–—]\s+[A-Za-z0-9].*$", "", short).strip()
    short = re.sub(
        r"\b(worst day in \d+ years|logs? worst day)\b[:,\s]*",
        "",
        short,
        flags=re.I,
    ).strip(" ,:-")
    short = _clip_sentence(short, limit=limit).rstrip(".")
    if not short or len(short) < 12:
        return ""
    if re.search(r"\btranscript\b|\bstock\s*$", short, flags=re.I):
        return ""
    first = short.split()[0]
    if not (first.isupper() and 2 <= len(first) <= 5):
        short = short[0].lower() + short[1:] if short[0].isupper() else short
    return short


def _short_name(company: str) -> str:
    name = (company or "").strip()
    name = re.sub(
        r"\s+(Limited|Ltd\.?|PLC|Inc\.?|Corporation|Corp\.?|Industries|Industry)$",
        "",
        name,
        flags=re.I,
    ).strip()
    # Prefer the distinctive head of long names
    parts = name.split()
    if len(parts) >= 3 and len(name) > 24:
        return " ".join(parts[:2])
    return name


def _named_movers(rows: list, limit: int = 2) -> list[str]:
    names: list[str] = []
    for row in rows or []:
        label = _short_name(getattr(row, "company", "") or getattr(row, "stock", ""))
        if label and label not in names:
            names.append(label)
        if len(names) >= limit:
            break
    return names


def _join_names(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _sector_force_clause(draft: ReportDraft, nifty_pct: float) -> str:
    sectors_down = sorted(
        [s for s in draft.sectors if (s.change_pct or 0) < 0],
        key=lambda s: s.change_pct or 0,
    )
    sectors_up = sorted(
        [s for s in draft.sectors if (s.change_pct or 0) > 0],
        key=lambda s: -(s.change_pct or 0),
    )
    losers = _named_movers(draft.losers, 2)
    gainers = _named_movers(draft.gainers, 2)

    if sectors_down and (sectors_down[0].change_pct or 0) <= -0.5:
        sector = sectors_down[0].name
        if losers:
            return f"{sector} pressure led by {_join_names(losers)}"
        return f"{sector} weakness ({sectors_down[0].change})"
    if nifty_pct > 0 and sectors_up and (sectors_up[0].change_pct or 0) >= 0.5:
        sector = sectors_up[0].name
        if gainers:
            return f"{sector} strength led by {_join_names(gainers)}"
        return f"{sector} strength ({sectors_up[0].change})"
    if losers and draft.losers and (draft.losers[0].change_pct or 0) <= -2.0:
        return f"heavyweight pressure from {_join_names(losers)}"
    if gainers and draft.gainers and (draft.gainers[0].change_pct or 0) >= 2.0 and nifty_pct > 0:
        return f"leadership from {_join_names(gainers)}"
    return ""


def _session_thesis(draft: ReportDraft, focus: list[StockFocusRow]) -> str:
    snap = {r.name: r for r in draft.market_snapshot}
    nifty = snap.get("Nifty 50")
    sensex = snap.get("Sensex")
    if not nifty or nifty.close == "-":
        return ""

    nifty_pct = nifty.change_pct or 0.0
    sensex_pct = sensex.change_pct if sensex and sensex.close != "-" else None
    force = _sector_force_clause(draft, nifty_pct)
    if not force and focus:
        m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", focus[0].move or "")
        move_pct = abs(float(m.group(1))) if m else 0.0
        if move_pct >= 3.0:
            force = f"{_short_name(focus[0].stock)} ({focus[0].move})"

    force_tail = f", as {force}" if force else ""

    # Call out Sensex vs Nifty divergence when signs disagree
    if sensex is not None and sensex_pct is not None and abs(nifty_pct) >= 0.05:
        if (sensex_pct > 0.05 and nifty_pct < -0.05) or (sensex_pct < -0.05 and nifty_pct > 0.05):
            if sensex_pct > 0:
                return (
                    f"The Sensex rose to {sensex.close} ({sensex.change}), while the Nifty "
                    f"slipped to {nifty.close} ({nifty.change}){force_tail}."
                )
            return (
                f"The Sensex slipped to {sensex.close} ({sensex.change}), while the Nifty "
                f"held higher at {nifty.close} ({nifty.change}){force_tail}."
            )

    if nifty_pct <= -0.25:
        verb = "finished lower"
    elif nifty_pct >= 0.25:
        verb = "finished higher"
    elif nifty_pct < 0:
        verb = "ended mildly lower"
    else:
        verb = "ended mildly higher"

    if sensex and sensex.close != "-":
        return (
            f"Benchmarks {verb}, with the Nifty at {nifty.close} ({nifty.change}) "
            f"and the Sensex at {sensex.close} ({sensex.change}){force_tail}."
        )
    return f"The Nifty {verb} at {nifty.close} ({nifty.change}){force_tail}."


def _internals_line(draft: ReportDraft) -> str:
    snap = {r.name: r for r in draft.market_snapshot}
    nifty = snap.get("Nifty 50")
    mid = snap.get("Nifty Midcap 100")
    small = snap.get("Nifty Smallcap 100")
    ad = draft.advances_declines
    nifty_pct = nifty.change_pct if nifty else 0.0

    breadth = ""
    if ad.advance is not None and ad.decline is not None:
        ratio = f", A/D {ad.ratio}" if ad.ratio is not None else ""
        if ad.decline > ad.advance * 1.15:
            breadth = (
                f"breadth stayed negative ({ad.advance} advances vs "
                f"{ad.decline} declines{ratio})"
            )
        elif ad.advance > ad.decline * 1.15:
            breadth = (
                f"breadth stayed constructive ({ad.advance} advances vs "
                f"{ad.decline} declines{ratio})"
            )
        else:
            breadth = (
                f"breadth was mixed ({ad.advance} advances vs "
                f"{ad.decline} declines{ratio})"
            )

    if not (mid and small and mid.change != "-" and small.change != "-"):
        if not breadth:
            return ""
        return breadth[0].upper() + breadth[1:] + "."

    mid_pct = mid.change_pct or 0.0
    small_pct = small.change_pct or 0.0
    mid_small = f"Midcap 100 {mid.change} and Smallcap 100 {small.change}"
    headline_soft = (nifty_pct or 0.0) < -0.05
    headline_firm = (nifty_pct or 0.0) > 0.05
    broader_firm = mid_pct > 0.1 and small_pct > 0.1
    broader_soft = mid_pct < -0.1 and small_pct < -0.1

    if broader_firm and headline_soft:
        core = (
            f"The broader market was firmer even as the headline indices slipped, "
            f"with {mid_small}"
        )
    elif broader_soft and headline_firm:
        core = (
            f"The broader market lagged a steadier headline tape, with {mid_small}"
        )
    elif broader_firm:
        core = f"Risk appetite held up under the surface, with {mid_small}"
    elif broader_soft:
        core = f"Risk appetite thinned under the surface, with {mid_small}"
    elif abs(mid_pct - small_pct) >= 0.25:
        core = (
            f"Style was split under the surface: Midcap 100 {mid.change} versus "
            f"Smallcap 100 {small.change}"
        )
    else:
        core = f"Mid and small caps tracked a similar tone, with {mid_small}"

    if breadth:
        return f"{core}, though {breadth}." if "firmer" in core else f"{core}, and {breadth}."
    return core + "."


def _domestic_driver_line(draft: ReportDraft, focus: list[StockFocusRow]) -> str:
    sectors_up = sorted(
        [x for x in draft.sectors if (x.change_pct or 0) > 0],
        key=lambda s: -(s.change_pct or 0),
    )
    sectors_down = sorted(
        [x for x in draft.sectors if (x.change_pct or 0) < 0],
        key=lambda s: s.change_pct or 0,
    )
    up_names = _dedupe_sector_names([s.name for s in sectors_up[:3]])
    down_names = _dedupe_sector_names([s.name for s in sectors_down[:3]])
    losers = _named_movers(draft.losers, 3)
    gainers = _named_movers(draft.gainers, 2)

    # Prefer Focus catalyst story; fall back to sector + named stocks
    if focus:
        lead = focus[0]
        cat = _catalyst_short(lead, limit=85)
        name = _short_name(lead.stock)
        second = ""
        if len(focus) > 1:
            other = focus[1]
            other_name = _short_name(other.stock)
            other_cat = _catalyst_short(other, limit=55)
            if other_cat:
                second = f" {other_name} ({other.move}) also mattered after {other_cat}."
            else:
                second = f" {other_name} ({other.move}) also stood out."
        if cat:
            return f"{name} ({lead.move}) moved after {cat}.{second}"
        return f"{name} ({lead.move}) was one of the clearer single-stock moves.{second}"

    if sectors_down and (sectors_down[0].change_pct or 0) <= -0.7 and losers:
        weak = sectors_down[0]
        lead_up = ""
        if sectors_up and (sectors_up[0].change_pct or 0) >= 0.4:
            lead_up = f", while {sectors_up[0].name} led the gainers"
        return (
            f"{weak.name} was the weakest group ({weak.change}), with "
            f"{_join_names(losers)} among the top Nifty losers{lead_up}."
        )

    if losers and draft.losers and (draft.losers[0].change_pct or 0) <= -2:
        sector_bit = f" as {down_names[0]} lagged" if down_names else ""
        return f"{_join_names(losers)} led the large-cap declines{sector_bit}."

    if up_names or down_names:
        up = ", ".join(up_names[:2]) if up_names else "select pockets"
        down = ", ".join(down_names[:2]) if down_names else "laggard groups"
        gainer_bit = f", helped by {_join_names(gainers)}" if gainers else ""
        return f"Sector rotation favoured {up}{gainer_bit}, while {down} lagged."
    return ""


def _macro_news_why(draft: ReportDraft) -> str:
    """Pull a short causal cue from news (CPI/Fed/RBI/crude geopolitics)."""
    scored: list[tuple[float, str]] = []
    for item in draft.news_candidates or []:
        blob = f"{item.title} {item.summary}"
        low = blob.lower()
        # Skip commodity-price wraps that only name-drop the Fed
        if re.search(r"\b(gold|silver|bitcoin|crypto)\b", low) and not re.search(
            r"\b(cpi|inflation|pce)\b", low
        ):
            continue
        score = 0.0
        if re.search(r"\b(cpi|inflation|pce)\b", low):
            score += 4
        if re.search(r"\b(fed|fomc)\b", low) and re.search(
            r"\b(rate|cut|hike|hold|pause|policy|cpi|inflation)\b", low
        ):
            score += 3
            # Fed path chatter alone is weak without an inflation print
            if not re.search(r"\b(cpi|inflation|pce|jobs|payroll|pmi)\b", low):
                score -= 1.5
        if re.search(r"\b(rbi|mpc|repo rate)\b", low):
            score += 4
        if re.search(r"\b(crude|brent|opec|iran|hormuz)\b", low) and re.search(
            r"\b(geopolit|tension|sanction|supply|opec|iran|hormuz)\b", low
        ):
            score += 2.5
        if score < 3.5:
            continue
        if re.match(r"^(gold|silver|oil|crude)\b", (item.title or "").strip(), flags=re.I):
            continue
        if re.search(r"\b(tame|soft|cooler|cooled|eased|patient|dovish|mild)\b", low):
            score += 1.2
        if re.search(r"\b(hot|sticky|hawkish|surge|jump)\b", low):
            score += 0.6
        cue = _clean_headline(item.title)
        cue = _clip_sentence(cue, limit=100).rstrip(".")
        if len(cue) < 28:
            continue
        # Prefer a causal fragment when the headline is long
        m = re.search(
            r"\b(after|as|amid)\b\s+(.+)$",
            cue,
            flags=re.I,
        )
        if m and len(m.group(2).strip()) >= 24:
            cue = m.group(2).strip()
        # Sentence-case the clause for "after …"
        cue = cue[0].lower() + cue[1:] if cue else cue
        cue = re.sub(
            r"\b(?!Fed|FOMC|RBI|CPI|PCE|US|UK|AI|CEO|GDP)([A-Z][a-z]+)\b",
            lambda mm: mm.group(1).lower(),
            cue,
        )
        scored.append((score + float(item.score or 0) * 0.05, cue))
    if not scored:
        return ""
    scored.sort(key=lambda x: -x[0])
    return scored[0][1]


def _macro_or_second_domestic(draft: ReportDraft, focus: list[StockFocusRow]) -> str:
    crude = next((c for c in draft.commodities if "Crude" in c.commodity), None)
    vix = next((r for r in draft.market_snapshot if r.name == "India VIX"), None)
    us = next(
        (
            g
            for g in draft.global_markets
            if any(k in g.name.lower() for k in ("s&p", "nasdaq", "dow"))
        ),
        None,
    )
    why = _macro_news_why(draft)

    us_move = us.change_pct if us and us.close != "-" else None
    crude_move = crude.change_pct if crude and crude.change != "-" else None

    if why and us is not None and us_move is not None and abs(us_move) >= 0.15:
        direction = "advanced" if us_move > 0 else "retreated"
        extra = ""
        if crude is not None and crude_move is not None and abs(crude_move) >= 0.2:
            extra = f", while crude held near {crude.price} ({crude.change})"
        return (
            f"Offshore, US equities {direction} after {why}, with the {us.name} "
            f"at {us.close} ({us.change}){extra}."
        )

    if us is not None and us_move is not None and abs(us_move) >= 0.2:
        bits = [f"the {us.name} finished at {us.close} ({us.change})"]
        if crude is not None and crude_move is not None and abs(crude_move) >= 0.2:
            direction = "higher" if crude_move > 0 else "lower"
            bits.append(f"crude moved {direction} to {crude.price} ({crude.change})")
        if vix and vix.change_pct is not None and abs(vix.change_pct) >= 1.0:
            tone = "eased" if vix.change_pct < 0 else "picked up"
            bits.append(f"India VIX {tone} to {vix.close} ({vix.change})")
        if len(bits) == 1:
            return f"Global cues stayed relevant as {bits[0]}."
        if len(bits) == 2:
            return f"Global cues were mixed: {bits[0]}, while {bits[1]}."
        return f"Global cues stayed in focus: {bits[0]}, {bits[1]}, and {bits[2]}."

    if crude is not None and crude_move is not None and abs(crude_move) >= 0.35:
        direction = "higher" if crude_move > 0 else "lower"
        return (
            f"Crude moved {direction} to {crude.price} ({crude.change}), "
            f"keeping an inflation overlay on the tape."
        )

    # Macro quiet: second domestic point
    if len(focus) >= 2:
        row = focus[1]
        cat = _catalyst_short(row, limit=70)
        name = _short_name(row.stock)
        if cat:
            return f"{name} ({row.move}) also reflected {cat}."
        return f"{name} ({row.move}) remained one of the clearer single-stock moves."
    if draft.gainers:
        g = draft.gainers[0]
        return f"{_short_name(g.company)} ({g.change}) stood out among Nifty 50 gainers."
    sectors_up = sorted(
        [s for s in draft.sectors if (s.change_pct or 0) > 0],
        key=lambda s: -(s.change_pct or 0),
    )
    if sectors_up:
        return f"Support came mainly from {sectors_up[0].name} ({sectors_up[0].change})."
    return ""


def _fallback_narrative(
    draft: ReportDraft,
    focus: list[StockFocusRow] | None = None,
    forced_news_updates: list[NewsUpdateItem] | None = None,
) -> tuple[NarrativeBlock, list[StockFocusRow]]:
    if focus is None:
        focus = candidates_to_focus_rows(
            select_event_driven_focus(draft.nifty200_movers, draft.news_candidates)
        )

    takeaways: list[str] = []
    thesis = _session_thesis(draft, focus)
    if thesis:
        takeaways.append(thesis)
    internals = _internals_line(draft)
    if internals:
        takeaways.append(internals)
    domestic = _domestic_driver_line(draft, focus)
    if domestic:
        takeaways.append(domestic)
    macro = _macro_or_second_domestic(draft, focus)
    if macro:
        takeaways.append(macro)

    news_updates = select_news_updates(
        draft.news_candidates,
        max_items=6,
        movers=draft.nifty200_movers or [],
        focus_rows=focus,
        forced_items=forced_news_updates,
    )
    # Self-check; drop offenders if any slipped through
    issues = review_news_updates(news_updates)
    if issues:
        logger.info("News updates review flags: %s", "; ".join(issues))
        cleaned: list[NewsUpdateItem] = []
        for item in news_updates:
            tmp_issues = review_news_updates([item])
            if not tmp_issues:
                cleaned.append(item)
        if cleaned:
            news_updates = cleaned

    draft.stocks_in_focus = focus
    return (
        NarrativeBlock(
            key_takeaways=_sanitize_takeaways(takeaways, draft),
            news_updates=news_updates[:6],
        ),
        focus,
    )


def _is_bad_takeaway(text: str) -> bool:
    t = (text or "").lower()
    banned = (
        "top gainers",
        "top losers",
        "gainers & losers",
        "gainers and losers",
        "market highlights",
        "crash highlights",
        "stocks to watch",
        "key watch remains top",
    )
    return any(b in t for b in banned)


def _sanitize_takeaways(takeaways: list[str], draft: ReportDraft) -> list[str]:
    _ = draft
    cleaned = [strip_em_dashes(x) for x in takeaways if x and not _is_bad_takeaway(x)]
    # Strip any leftover watchpoint-style lines
    cleaned = [
        x
        for x in cleaned
        if "into the next session" not in x.lower()
        and not re.match(r"^watch\s+.+\s+for follow-through\b", x, flags=re.I)
        and not re.match(r"^watch whether\b", x, flags=re.I)
    ]
    return cleaned[:4]


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


async def generate_narrative(
    draft: ReportDraft,
    forced_news_updates: list[NewsUpdateItem] | None = None,
) -> tuple[NarrativeBlock, list[StockFocusRow]]:
    event_candidates = select_event_driven_focus(
        draft.nifty200_movers,
        draft.news_candidates,
        min_move_pct=1.5,
        primary_move_pct=3.0,
        max_items=5,
    )
    fallback_focus = candidates_to_focus_rows(event_candidates)
    forced = list(forced_news_updates or [])

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        logger.info("No ANTHROPIC_API_KEY set; using insight fallback narrative.")
        narrative, _ = _fallback_narrative(
            draft, fallback_focus, forced_news_updates=forced
        )
        return narrative, fallback_focus

    payload = {
        "market_snapshot": [r.model_dump() for r in draft.market_snapshot],
        "global_markets": [r.model_dump() for r in draft.global_markets],
        "sectors": [r.model_dump() for r in draft.sectors],
        "gainers": [r.model_dump() for r in draft.gainers],
        "losers": [r.model_dump() for r in draft.losers],
        "event_driven_focus_candidates": [
            {
                "stock": c.mover.company,
                "symbol": c.mover.symbol,
                "move": c.mover.change,
                "change_pct": c.mover.change_pct,
                "direction": c.mover.direction,
                "score": c.score,
                "matched_news": [
                    {
                        "title": h.title,
                        "summary": h.summary[:600],
                        "source": h.source,
                        "score": h.score,
                        "tags": h.relevance_tags,
                    }
                    for h in c.news[:2]
                ],
            }
            for c in event_candidates
        ],
        "advances_declines": draft.advances_declines.model_dump(),
        "commodities": [r.model_dump() for r in draft.commodities],
        "news_candidates": [
            {
                "title": n.title,
                "summary": n.summary[:900],
                "score": n.score,
                "tags": n.relevance_tags,
                "published": n.published,
                "source": n.source,
            }
            for n in sorted(draft.news_candidates, key=lambda x: -x.score)[:16]
        ],
    }

    client = Anthropic(api_key=api_key)
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=3800,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": USER_TEMPLATE.format(
                        date_label=draft.date_label,
                        payload=json.dumps(payload, ensure_ascii=False),
                    ),
                }
            ],
        )
        text = "".join(block.text for block in msg.content if hasattr(block, "text"))
        data = _extract_json(text)

        news_items: list[NewsUpdateItem] = []
        seen_clusters: set[str] = set()
        for row in data.get("news_updates", [])[:10]:
            if isinstance(row, dict):
                text_b = _strip_source_prefix(str(row.get("text") or row.get("bullet") or ""))
                source = strip_em_dashes(str(row.get("source") or "")).strip()
            else:
                text_b = _strip_source_prefix(str(row))
                source = ""
            if not text_b or _is_soft_story(text_b) or _is_results_calendar(text_b):
                continue
            cluster = _story_cluster_key(text_b)
            if cluster in seen_clusters:
                continue
            if overlaps_focus_story(text_b, fallback_focus):
                continue
            if forced and re.search(
                r"\b(fii|fpi|diis?)\b", text_b, flags=re.I
            ) and re.search(
                r"\b(net (?:buy|sell)|fund flows?|bought|sold|inflow|outflow)\b",
                text_b,
                flags=re.I,
            ):
                continue
            seen_clusters.add(cluster)
            news_items.append(NewsUpdateItem(text=_clip_sentence(text_b), source=source))
            if len(news_items) >= 7:
                break

        draft.stocks_in_focus = fallback_focus

        news_final = filter_news_against_focus(news_items, fallback_focus)
        # Always lead with official NSE FII/DII capital-market flows when available
        if forced:
            lead = [x for x in forced if x and x.text.strip()]
            rest = [
                n
                for n in news_final
                if all(_story_cluster_key(n.text) != _story_cluster_key(f.text) for f in lead)
            ]
            news_final = (lead + rest)[:7]

        narrative = NarrativeBlock(
            key_takeaways=_sanitize_takeaways(
                [_strip_source_prefix(x) for x in data.get("key_takeaways", [])],
                draft,
            ),
            news_updates=news_final,
        )

        # Stocks in Focus stays deterministic (hybrid move + company-specific news).
        # Do not let the model rewrite reasons with market wraps / shared baskets.
        if not narrative.key_takeaways or not narrative.news_updates:
            fb_narr, _ = _fallback_narrative(
                draft, fallback_focus, forced_news_updates=forced
            )
            if not narrative.key_takeaways:
                narrative.key_takeaways = _sanitize_takeaways(fb_narr.key_takeaways, draft)
            if not narrative.news_updates:
                narrative.news_updates = fb_narr.news_updates
        else:
            narrative.key_takeaways = _sanitize_takeaways(narrative.key_takeaways, draft)
        return narrative, fallback_focus
    except Exception as exc:  # noqa: BLE001
        logger.warning("Claude narrative failed: %s", exc)
        draft.stocks_in_focus = fallback_focus
        narrative, _ = _fallback_narrative(
            draft, fallback_focus, forced_news_updates=forced
        )
        return narrative, fallback_focus
