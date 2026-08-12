"""Orchestrates scrape + narrative into a ReportDraft."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.llm.narrative import generate_narrative
from app.models import AdvancesDeclines, ReportDraft
from app.scrapers.market_data import (
    fetch_advances_declines,
    fetch_commodities_best_effort,
    fetch_global_indices,
    fetch_indian_indices,
    fetch_nifty50_movers,
    fetch_nifty200_movers,
    fetch_sector_performance,
)
from app.scrapers.news import fetch_market_news
from app.utils import format_date_label, parse_report_date

logger = logging.getLogger(__name__)


async def _run_sync(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


async def generate_report_draft(
    report_date: str | None = None,
    include_narrative: bool = True,
) -> tuple[ReportDraft, dict[str, str]]:
    dt = parse_report_date(report_date)
    status: dict[str, str] = {}

    async def track(name: str, coro) -> Any:
        try:
            result = await coro
            if result is None:
                status[name] = "failed"
            elif name == "nifty50_movers":
                gainers, losers = result
                status[name] = "ok" if (gainers or losers) else "empty"
            elif name == "nifty200_movers":
                status[name] = "ok" if result else "empty"
            elif name == "advances_declines":
                if result.advance is None and result.decline is None:
                    status[name] = "empty"
                else:
                    status[name] = "ok"
            elif hasattr(result, "__len__") and len(result) == 0:
                status[name] = "empty"
            else:
                status[name] = "ok"
            return result
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s failed", name)
            status[name] = f"failed: {exc}"
            return None

    # 1) Market tables first
    indian, globals_, sectors, movers, nifty200, ad, commodities = await asyncio.gather(
        track("indian_indices", _run_sync(fetch_indian_indices)),
        track("global_indices", _run_sync(fetch_global_indices)),
        track("sectors", _run_sync(fetch_sector_performance)),
        track("nifty50_movers", fetch_nifty50_movers()),
        track("nifty200_movers", fetch_nifty200_movers()),
        track("advances_declines", fetch_advances_declines()),
        track("commodities", fetch_commodities_best_effort()),
    )

    gainers, losers = ([], [])
    if movers:
        gainers, losers = movers

    # 2) Deep news pass scored against today's tape (bias toward larger Nifty 200 moves)
    movers_for_news = sorted(
        (nifty200 or []),
        key=lambda m: abs(m.change_pct or 0),
        reverse=True,
    )[:40]
    news = await track(
        "news",
        fetch_market_news(
            limit=50,
            movers=movers_for_news + gainers + losers,
            sectors=sectors or [],
            commodities=commodities or [],
        ),
    )

    draft = ReportDraft(
        date_iso=dt.strftime("%Y-%m-%d"),
        date_label=format_date_label(dt),
        market_snapshot=indian or [],
        global_markets=globals_ or [],
        sectors=sectors or [],
        gainers=gainers,
        losers=losers,
        nifty200_movers=nifty200 or [],
        advances_declines=ad if ad is not None else AdvancesDeclines(note="Unavailable"),
        commodities=commodities or [],
        news_candidates=news or [],
    )

    # 3) Insight writing
    if include_narrative:
        try:
            narrative, focus = await generate_narrative(draft)
            draft.narrative = narrative
            draft.stocks_in_focus = focus
            status["narrative"] = "ok"
        except Exception as exc:  # noqa: BLE001
            status["narrative"] = f"failed: {exc}"
    else:
        status["narrative"] = "skipped"

    return draft, status
