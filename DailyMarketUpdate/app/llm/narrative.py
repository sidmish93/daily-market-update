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
from app.scrapers.news_updates import review_news_updates, select_news_updates
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
1) Session verdict: what kind of day it was and the main force behind it.
2) Market internals: breadth, mid/small vs benchmarks, leadership vs drag.
3) Domestic stock/sector story that actually drove or offset the tape.
4) Global or commodity macro overlay relevant to India, or a second domestic point if macro is quiet.
Use figures only where they sharpen the point. Avoid laundry lists of every index.
Do not add a forward-looking watchpoint bullet.

NEWS & UPDATES (5-7 items, quality over quantity):
These should be the most informative part of the note.
- Select only high-impact developments from news_candidates (earnings with moves, index changes, legal/regulatory outcomes, crude/geopolitics, large-cap group news, material deals/fundraising).
- One theme = one bullet. Merge duplicate Tata/Chandrasekaran (or any repeated) stories into a single stronger item.
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


def _session_thesis(draft: ReportDraft, focus: list[StockFocusRow]) -> str:
    snap = {r.name: r for r in draft.market_snapshot}
    nifty = snap.get("Nifty 50")
    sensex = snap.get("Sensex")
    if not nifty or nifty.close == "-":
        return ""

    nifty_pct = nifty.change_pct or 0.0
    sectors_down = sorted(
        [s for s in draft.sectors if (s.change_pct or 0) < 0],
        key=lambda s: s.change_pct or 0,
    )
    sectors_up = sorted(
        [s for s in draft.sectors if (s.change_pct or 0) > 0],
        key=lambda s: -(s.change_pct or 0),
    )
    crude = next((c for c in draft.commodities if "Crude" in c.commodity), None)
    top_loser = draft.losers[0] if draft.losers else None

    # Prefer market-level force here; leave Focus #1 detail for the domestic bullet
    force = ""
    if sectors_down and (sectors_down[0].change_pct or 0) <= -0.6:
        force = f"{sectors_down[0].name} weakness ({sectors_down[0].change})"
    elif sectors_up and (sectors_up[0].change_pct or 0) >= 0.6 and nifty_pct > 0:
        force = f"{sectors_up[0].name} strength ({sectors_up[0].change})"
    elif top_loser and (top_loser.change_pct or 0) <= -2.5:
        force = f"heavyweight pressure from {top_loser.company} ({top_loser.change})"
    elif crude and crude.change_pct is not None and abs(crude.change_pct) >= 0.5:
        force = (
            f"firmer crude ({crude.change})"
            if crude.change_pct > 0
            else f"softer crude ({crude.change})"
        )
    elif focus:
        m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", focus[0].move or "")
        move_pct = abs(float(m.group(1))) if m else 0.0
        if move_pct >= 3.0:
            force = f"{focus[0].stock} ({focus[0].move})"

    if nifty_pct <= -0.4:
        tone = "risk-off"
    elif nifty_pct >= 0.4:
        tone = "constructive"
    elif nifty_pct < 0:
        tone = "soft and selective"
    else:
        tone = "steady but selective"

    anchors = f"Nifty closed at {nifty.close} ({nifty.change})"
    if sensex and sensex.close != "-":
        anchors += f", Sensex at {sensex.close} ({sensex.change})"

    if force:
        if nifty_pct < 0:
            return f"Equities ended {tone} as {force} weighed on sentiment; {anchors}."
        return f"Equities ended {tone}, helped by {force}; {anchors}."
    lead = sectors_up[0].name if sectors_up else "select pockets"
    drag = sectors_down[0].name if sectors_down else "laggard sectors"
    return (
        f"Equities ended {tone}, with {lead} supporting the tape while {drag} capped upside; {anchors}."
    )


def _internals_line(draft: ReportDraft) -> str:
    snap = {r.name: r for r in draft.market_snapshot}
    mid = snap.get("Nifty Midcap 100")
    small = snap.get("Nifty Smallcap 100")
    ad = draft.advances_declines
    parts: list[str] = []

    if ad.advance is not None and ad.decline is not None:
        if ad.decline > ad.advance * 1.15:
            breadth = "participation stayed weak"
        elif ad.advance > ad.decline * 1.15:
            breadth = "participation stayed constructive"
        else:
            breadth = "participation was mixed"
        ratio = f", A/D {ad.ratio}" if ad.ratio is not None else ""
        parts.append(
            f"{breadth} ({ad.advance} advances vs {ad.decline} declines{ratio})"
        )

    if mid and small and mid.change != "-" and small.change != "-":
        mid_pct = mid.change_pct or 0.0
        small_pct = small.change_pct or 0.0
        if mid_pct > 0.15 and small_pct > 0.15:
            parts.append(
                f"broader markets held up better (Midcap 100 {mid.change}, Smallcap 100 {small.change})"
            )
        elif mid_pct < -0.15 and small_pct < -0.15:
            parts.append(
                f"risk appetite thinned under the surface (Midcap 100 {mid.change}, Smallcap 100 {small.change})"
            )
        elif abs(mid_pct - small_pct) >= 0.25:
            parts.append(
                f"style split showed Midcap 100 {mid.change} versus Smallcap 100 {small.change}"
            )
        else:
            parts.append(
                f"Midcap 100 {mid.change} and Smallcap 100 {small.change} tracked a similar tone"
            )

    if not parts:
        return ""
    if len(parts) == 1:
        text = parts[0][0].upper() + parts[0][1:]
        return text if text.endswith(".") else text + "."
    return f"{parts[0][0].upper() + parts[0][1:]}, while {parts[1]}."


def _domestic_driver_line(draft: ReportDraft, focus: list[StockFocusRow]) -> str:
    sectors_up = _dedupe_sector_names(
        [
            s.name
            for s in sorted(
                [x for x in draft.sectors if (x.change_pct or 0) > 0],
                key=lambda s: -(s.change_pct or 0),
            )[:3]
        ]
    )
    sectors_down = _dedupe_sector_names(
        [
            s.name
            for s in sorted(
                [x for x in draft.sectors if (x.change_pct or 0) < 0],
                key=lambda s: s.change_pct or 0,
            )[:3]
        ]
    )

    if focus:
        lead = focus[0]
        cat = _catalyst_short(lead, limit=85)
        second = ""
        if len(focus) > 1:
            other = focus[1]
            other_cat = _catalyst_short(other, limit=55)
            if other_cat:
                second = f" {other.stock} ({other.move}) also stayed in focus on {other_cat}."
            else:
                second = f" {other.stock} ({other.move}) also stayed in focus."
        if cat:
            return (
                f"Stock-specific flows centred on {lead.stock} ({lead.move}) after {cat}."
                f"{second}"
            )
        return f"Stock-specific flows centred on {lead.stock} ({lead.move}).{second}"

    if draft.losers and (draft.losers[0].change_pct or 0) <= -2:
        loser = draft.losers[0]
        sector_bit = ""
        if sectors_down:
            sector_bit = f", alongside weakness in {', '.join(sectors_down[:2])}"
        return (
            f"Large-cap selling was led by {loser.company} ({loser.change}){sector_bit}."
        )

    if sectors_up or sectors_down:
        up = ", ".join(sectors_up[:2]) if sectors_up else "select pockets"
        down = ", ".join(sectors_down[:2]) if sectors_down else "laggard groups"
        return f"Sector rotation favoured {up}, while {down} lagged."
    return ""


def _macro_or_second_domestic(draft: ReportDraft, focus: list[StockFocusRow]) -> str:
    crude = next((c for c in draft.commodities if "Crude" in c.commodity), None)
    vix = next((r for r in draft.market_snapshot if r.name == "India VIX"), None)
    us = next(
        (g for g in draft.global_markets if any(k in g.name.lower() for k in ("s&p", "nasdaq", "dow"))),
        None,
    )

    bites: list[str] = []
    if crude and crude.change_pct is not None and abs(crude.change_pct) >= 0.2:
        direction = "higher" if crude.change_pct > 0 else "lower"
        bites.append(f"crude moved {direction} to {crude.price} ({crude.change})")
    if us and us.change_pct is not None and abs(us.change_pct) >= 0.2:
        bites.append(f"{us.name} finished at {us.close} ({us.change})")
    if vix and vix.change_pct is not None and abs(vix.change_pct) >= 1.0:
        tone = "eased" if vix.change_pct < 0 else "picked up"
        bites.append(f"India VIX {tone} to {vix.close} ({vix.change})")

    if bites:
        if len(bites) == 1:
            return f"On the macro side, {bites[0]}."
        if len(bites) == 2:
            return f"On the macro side, {bites[0]}, while {bites[1]}."
        return f"On the macro side, {bites[0]}, {bites[1]}, and {bites[2]}."

    # Macro quiet: use a second domestic point instead of filler
    if len(focus) >= 2:
        row = focus[1]
        cat = _catalyst_short(row, limit=70)
        if cat:
            return f"Separately, {row.stock} ({row.move}) reflected {cat}."
        return f"Separately, {row.stock} ({row.move}) remained one of the clearer single-specific moves."
    if draft.gainers:
        g = draft.gainers[0]
        return f"On the upside, {g.company} ({g.change}) stood out among Nifty 50 gainers."
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


async def generate_narrative(draft: ReportDraft) -> tuple[NarrativeBlock, list[StockFocusRow]]:
    event_candidates = select_event_driven_focus(
        draft.nifty200_movers,
        draft.news_candidates,
        min_move_pct=1.5,
        primary_move_pct=3.0,
        max_items=5,
    )
    fallback_focus = candidates_to_focus_rows(event_candidates)

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        logger.info("No ANTHROPIC_API_KEY set; using insight fallback narrative.")
        narrative, _ = _fallback_narrative(draft, fallback_focus)
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
            seen_clusters.add(cluster)
            news_items.append(NewsUpdateItem(text=_clip_sentence(text_b), source=source))
            if len(news_items) >= 7:
                break

        draft.stocks_in_focus = fallback_focus

        narrative = NarrativeBlock(
            key_takeaways=_sanitize_takeaways(
                [_strip_source_prefix(x) for x in data.get("key_takeaways", [])],
                draft,
            ),
            news_updates=news_items,
        )

        # Stocks in Focus stays deterministic (hybrid move + company-specific news).
        # Do not let the model rewrite reasons with market wraps / shared baskets.
        if not narrative.key_takeaways or not narrative.news_updates:
            fb_narr, _ = _fallback_narrative(draft, fallback_focus)
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
        narrative, _ = _fallback_narrative(draft, fallback_focus)
        return narrative, fallback_focus
