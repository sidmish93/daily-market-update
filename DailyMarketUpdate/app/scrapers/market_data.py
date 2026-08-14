"""Market data adapters optimized for Render (stable APIs over brittle HTML)."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import yfinance as yf

from app.models import AdvancesDeclines, CommodityRow, IndexRow, MoverRow, SectorRow
from app.utils import (
    direction_from_pct,
    format_change_pct,
    format_inr,
    format_number,
)

logger = logging.getLogger(__name__)

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

INDIAN_INDICES = [
    ("Sensex", "^BSESN"),
    ("Nifty 50", "^NSEI"),
    ("Nifty Midcap 100", "NIFTY_MIDCAP_100.NS"),
    ("Nifty Smallcap 100", "^CNXSC"),
    ("India VIX", "^INDIAVIX"),
]

# Fallbacks if primary Yahoo symbols fail
INDIAN_INDEX_FALLBACKS = {
    "Nifty Midcap 100": ["^NSEMDCP50", "NIFTYMIDCAP100.NS"],
    "Nifty Smallcap 100": ["NIFTY_SMLCAP_100.NS", "NIFTYSMLCAP100.NS"],
    "India VIX": ["INDIAVIX.NS"],
}

GLOBAL_INDICES = [
    ("S&P 500", "^GSPC"),
    ("Nasdaq", "^IXIC"),
    ("FTSE 100", "^FTSE"),
    ("Nikkei 225", "^N225"),
    ("Hang Seng", "^HSI"),
]

SECTOR_INDICES = [
    ("Nifty IT", "^CNXIT"),
    ("Nifty Pharma", "^CNXPHARMA"),
    ("Nifty FMCG", "^CNXFMCG"),
    ("Nifty Bank", "^NSEBANK"),
    ("Nifty Financial Services", "NIFTY_FIN_SERVICE.NS"),
    ("Nifty Auto", "^CNXAUTO"),
    ("Nifty Metal", "^CNXMETAL"),
    ("Nifty Realty", "^CNXREALTY"),
    ("Nifty Energy", "^CNXENERGY"),
    ("Nifty PSU Bank", "^CNXPSUBANK"),
    ("Nifty Healthcare", "NIFTY_HEALTHCARE.NS"),
    ("Nifty Consumer Durables", "NIFTY_CONSR_DURBL.NS"),
]

COMMODITY_SYMBOLS = [
    ("Crude Oil", "CL=F", "usd"),
    ("Natural Gas", "NG=F", "usd"),
    ("Gold", "GC=F", "usd"),
    ("Silver", "SI=F", "usd"),
]

# Report-friendly short names (NSE tickers often differ from brand names).
PREFERRED_DISPLAY_NAMES = {
    "NAUKRI": "Info Edge",
    "DRREDDY": "Dr Reddys Labs",
    "ETERNAL": "Eternal",
    "TCS": "TCS",
    "TITAN": "Titan Company",
    "INFY": "Infosys",
    "TATACONSUM": "Tata Cons. Prod",
    "MAXHEALTH": "Max Healthcare",
    "NESTLEIND": "Nestle",
    "ULTRACEMCO": "Ultratech Cement",
    "APOLLOHOSP": "Apollo Hospitals",
    "POLICYBZR": "PB Fintech",
    "ZYDUSLIFE": "Zydus Lifesciences",
    "BHARTIARTL": "Bharti Airtel",
    "M&MFIN": "M&M Finance",
    "GODREJPROP": "Godrej Properties",
}

_SYMBOL_NAME_CACHE: dict[str, str] | None = None


def _shorten_company_name(name: str) -> str:
    cleaned = name.strip()
    for suffix in (
        " Limited",
        " Ltd.",
        " Ltd",
        " LLC",
        " PLC",
        " Corporation",
        " Corp.",
        " Corp",
    ):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
    cleaned = cleaned.replace("(India)", "").replace("(india)", "")
    return " ".join(cleaned.split())


def _load_nse_symbol_names() -> dict[str, str]:
    """Load SYMBOL -> company name from NSE equity list (cached in-process)."""
    global _SYMBOL_NAME_CACHE
    if _SYMBOL_NAME_CACHE is not None:
        return _SYMBOL_NAME_CACHE

    mapping: dict[str, str] = {}
    url = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    try:
        with httpx.Client(
            headers={
                "User-Agent": NSE_HEADERS["User-Agent"],
                "Referer": "https://www.nseindia.com/",
            },
            timeout=25.0,
            follow_redirects=True,
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            lines = resp.text.splitlines()
            for line in lines[1:]:
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 2:
                    continue
                symbol, company = parts[0].upper(), parts[1]
                if symbol and company:
                    mapping[symbol] = company
    except Exception as exc:  # noqa: BLE001
        logger.warning("NSE equity name list failed: %s", exc)

    _SYMBOL_NAME_CACHE = mapping
    return mapping


def company_display_name(symbol: str) -> str:
    """Map NSE ticker to a report-friendly company name."""
    sym = (symbol or "").upper().strip()
    if not sym:
        return symbol
    if sym in PREFERRED_DISPLAY_NAMES:
        return PREFERRED_DISPLAY_NAMES[sym]
    full = _load_nse_symbol_names().get(sym)
    if full:
        return _shorten_company_name(full)
    return sym



def _quote_change(ticker: str) -> tuple[float, float] | None:
    """Return (last, pct_change) for a Yahoo ticker."""
    try:
        t = yf.Ticker(ticker)
        info = t.fast_info
        last = getattr(info, "last_price", None)
        prev = getattr(info, "previous_close", None)
        if last is None or prev is None or prev == 0:
            hist = t.history(period="5d")
            if hist.empty or len(hist) < 2:
                return None
            last = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2])
        last_f = float(last)
        prev_f = float(prev)
        pct = ((last_f - prev_f) / prev_f) * 100.0
        return last_f, pct
    except Exception as exc:  # noqa: BLE001
        logger.warning("yfinance failed for %s: %s", ticker, exc)
        return None


def _index_row(name: str, ticker: str, fallbacks: list[str] | None = None) -> IndexRow | None:
    candidates = [ticker] + (fallbacks or [])
    for sym in candidates:
        data = _quote_change(sym)
        if data is None:
            continue
        last, pct = data
        return IndexRow(
            name=name,
            close=format_number(last, 2),
            change=format_change_pct(pct),
            change_pct=round(pct, 2),
            direction=direction_from_pct(pct),
        )
    return None


NSE_SECTOR_NAME_MAP = {
    "NIFTY IT": "IT",
    "NIFTY PHARMA": "Pharma",
    "NIFTY FMCG": "FMCG",
    "NIFTY BANK": "Bank",
    "NIFTY FINANCIAL SERVICES": "Financial Services",
    "NIFTY FIN SERVICE": "Financial Services",
    "NIFTY AUTO": "Auto",
    "NIFTY METAL": "Metal",
    "NIFTY REALTY": "Realty",
    "NIFTY ENERGY": "Energy",
    "NIFTY PSU BANK": "PSU Bank",
    "NIFTY HEALTHCARE": "Healthcare",
    "NIFTY HEALTHCARE INDEX": "Healthcare",
    "NIFTY CONSUMER DURABLES": "Consumer Durables",
    "NIFTY CONSR DURBL": "Consumer Durables",
}


def _fetch_nse_all_indices_rows() -> list[dict]:
    try:
        with httpx.Client(timeout=20.0, headers=NSE_HEADERS, follow_redirects=True) as client:
            try:
                client.get("https://www.nseindia.com")
            except Exception:  # noqa: BLE001
                pass
            resp = client.get("https://www.nseindia.com/api/allIndices")
            resp.raise_for_status()
            return list(resp.json().get("data") or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("NSE allIndices failed: %s", exc)
        return []


def _fetch_nse_index_map() -> dict[str, IndexRow]:
    """Official NSE LTP / change for major indices (preferred over Yahoo)."""
    wanted = {
        "NIFTY 50": "Nifty 50",
        "NIFTY MIDCAP 100": "Nifty Midcap 100",
        "NIFTY SMALLCAP 100": "Nifty Smallcap 100",
        "NIFTY SMLCAP 100": "Nifty Smallcap 100",
        "INDIA VIX": "India VIX",
    }
    out: dict[str, IndexRow] = {}
    for row in _fetch_nse_all_indices_rows():
        raw_name = (row.get("index") or "").strip().upper()
        label = wanted.get(raw_name)
        if not label or label in out:
            continue
        last = row.get("last")
        pct = row.get("percentChange")
        if last is None or pct is None:
            continue
        last_f = float(last)
        pct_f = float(pct)
        out[label] = IndexRow(
            name=label,
            close=format_number(last_f, 2),
            change=format_change_pct(pct_f),
            change_pct=round(pct_f, 2),
            direction=direction_from_pct(pct_f),
        )
    return out


def _fetch_nse_sector_map() -> dict[str, SectorRow]:
    """Official NSE sectoral % changes (preferred over Yahoo)."""
    out: dict[str, SectorRow] = {}
    for row in _fetch_nse_all_indices_rows():
        raw_name = (row.get("index") or "").strip().upper()
        label = NSE_SECTOR_NAME_MAP.get(raw_name)
        if not label or label in out:
            continue
        pct = row.get("percentChange")
        if pct is None:
            continue
        pct_f = float(pct)
        out[label] = SectorRow(
            name=label,
            change=format_change_pct(pct_f),
            change_pct=round(pct_f, 2),
            direction=direction_from_pct(pct_f),
        )
    return out


def _fetch_moneycontrol_sensex() -> IndexRow | None:
    url = "https://priceapi.moneycontrol.com/pricefeed/notapplicable/inidicesindia/in%3BSEN"
    headers = {
        **NSE_HEADERS,
        "Referer": "https://www.moneycontrol.com/",
    }
    try:
        with httpx.Client(timeout=15.0, headers=headers, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json().get("data") or {}
            last = float(data["pricecurrent"])
            pct = float(data["pricepercentchange"])
            return IndexRow(
                name="Sensex",
                close=format_number(last, 2),
                change=format_change_pct(pct),
                change_pct=round(pct, 2),
                direction=direction_from_pct(pct),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Moneycontrol Sensex failed: %s", exc)
        return None


def fetch_indian_indices() -> list[IndexRow]:
    nse_map = _fetch_nse_index_map()
    sensex = _fetch_moneycontrol_sensex()
    rows: list[IndexRow] = []
    for name, ticker in INDIAN_INDICES:
        if name == "Sensex" and sensex:
            rows.append(sensex)
            continue
        if name in nse_map:
            rows.append(nse_map[name])
            continue
        row = _index_row(name, ticker, INDIAN_INDEX_FALLBACKS.get(name))
        if row:
            rows.append(row)
        else:
            rows.append(
                IndexRow(name=name, close="-", change="-", change_pct=None, direction="flat")
            )
    return rows


def fetch_global_indices() -> list[IndexRow]:
    rows: list[IndexRow] = []
    for name, ticker in GLOBAL_INDICES:
        row = _index_row(name, ticker)
        if row:
            rows.append(row)
        else:
            rows.append(
                IndexRow(name=name, close="-", change="-", change_pct=None, direction="flat")
            )
    return rows


def fetch_sector_performance(top_n: int = 3) -> list[SectorRow]:
    """Top sector gainers and losers by % change (NSE sectoral indices)."""
    nse_map = _fetch_nse_sector_map()
    scored: list[SectorRow] = []
    for name, ticker in SECTOR_INDICES:
        label = name.replace("Nifty ", "")
        if label in nse_map:
            scored.append(nse_map[label])
            continue
        data = _quote_change(ticker)
        if data is None:
            continue
        _, pct = data
        scored.append(
            SectorRow(
                name=label,
                change=format_change_pct(pct),
                change_pct=round(pct, 2),
                direction=direction_from_pct(pct),
            )
        )
    gainers = sorted(
        [s for s in scored if (s.change_pct or 0) > 0],
        key=lambda r: r.change_pct or 0,
        reverse=True,
    )[:top_n]
    losers = sorted(
        [s for s in scored if (s.change_pct or 0) < 0],
        key=lambda r: r.change_pct or 0,
    )[:top_n]
    return gainers + losers


async def _with_nse_client(callback):
    """Create a fresh NSE client, warm cookies, run callback, then close."""
    async with httpx.AsyncClient(
        headers=NSE_HEADERS,
        timeout=25.0,
        follow_redirects=True,
    ) as client:
        try:
            await client.get("https://www.nseindia.com")
        except Exception:  # noqa: BLE001
            pass
        return await callback(client)


async def fetch_nifty50_movers(top_n: int = 4) -> tuple[list[MoverRow], list[MoverRow]]:
    """Nifty 50 gainers/losers from NSE live-analysis-variations API."""

    def to_mover(d: dict[str, Any]) -> MoverRow:
        pct = float(d.get("perChange") or d.get("pChange") or 0)
        last = float(d.get("ltp") or d.get("lastPrice") or 0)
        symbol = str(d.get("symbol") or "")
        return MoverRow(
            company=company_display_name(symbol),
            symbol=symbol,
            price=format_inr(last),
            change=format_change_pct(pct),
            change_pct=round(pct, 2),
            direction=direction_from_pct(pct),
        )

    async def _fetch(client: httpx.AsyncClient) -> tuple[list[MoverRow], list[MoverRow]]:
        # NSE uses the misspelling "loosers" for the losers endpoint.
        g_resp = await client.get(
            "https://www.nseindia.com/api/live-analysis-variations?index=gainers"
        )
        l_resp = await client.get(
            "https://www.nseindia.com/api/live-analysis-variations?index=loosers"
        )
        g_resp.raise_for_status()
        l_resp.raise_for_status()
        gainers_raw = ((g_resp.json().get("NIFTY") or {}).get("data")) or []
        losers_raw = ((l_resp.json().get("NIFTY") or {}).get("data")) or []
        gainers_raw = sorted(
            gainers_raw, key=lambda d: float(d.get("perChange") or 0), reverse=True
        )
        losers_raw = sorted(losers_raw, key=lambda d: float(d.get("perChange") or 0))
        return (
            [to_mover(d) for d in gainers_raw[:top_n]],
            [to_mover(d) for d in losers_raw[:top_n]],
        )

    try:
        return await _with_nse_client(_fetch)
    except Exception as exc:  # noqa: BLE001
        logger.warning("NSE movers failed: %s", exc)
        return [], []


async def fetch_advances_declines() -> AdvancesDeclines:
    """Capital market Advances/Declines from NSE market-data/advance page API."""

    async def _fetch(client: httpx.AsyncClient) -> AdvancesDeclines:
        # Visit the page first so cookies match the official Advances/Declines screen.
        try:
            await client.get("https://www.nseindia.com/market-data/advance")
        except Exception:  # noqa: BLE001
            pass
        resp = await client.get("https://www.nseindia.com/api/live-analysis-advance")
        resp.raise_for_status()
        payload = resp.json() or {}
        count = ((payload.get("advance") or {}).get("count")) or {}
        adv = count.get("Advances")
        dec = count.get("Declines")
        if adv is None or dec is None:
            return AdvancesDeclines(
                note="Could not fetch breadth from NSE Advances/Declines. Please enter manually."
            )
        adv_i, dec_i = int(adv), int(dec)
        ratio = round(adv_i / dec_i, 2) if dec_i else None
        return AdvancesDeclines(advance=adv_i, decline=dec_i, ratio=ratio, note="")

    try:
        return await _with_nse_client(_fetch)
    except Exception as exc:  # noqa: BLE001
        logger.warning("NSE advances/declines failed: %s", exc)
        return AdvancesDeclines(
            note="Could not fetch breadth from NSE Advances/Declines. Please enter manually."
        )


async def fetch_nifty200_movers(top_n: int | None = None, min_abs_pct: float = 0.0) -> list[MoverRow]:
    """Nifty 200 constituents with day moves (full universe unless filtered)."""

    async def _fetch(client: httpx.AsyncClient) -> list[MoverRow]:
        resp = await client.get(
            "https://www.nseindia.com/api/equity-stock-indices",
            params={"index": "NIFTY 200"},
        )
        resp.raise_for_status()
        data = resp.json().get("data") or []
        stocks = [d for d in data if str(d.get("symbol", "")).upper() != "NIFTY 200"]
        rows: list[MoverRow] = []
        for d in stocks:
            pct = float(d.get("pChange") or 0)
            if abs(pct) < min_abs_pct:
                continue
            last = float(d.get("lastPrice") or 0)
            symbol = str(d.get("symbol") or "")
            rows.append(
                MoverRow(
                    company=company_display_name(symbol),
                    symbol=symbol,
                    price=format_inr(last),
                    change=format_change_pct(pct),
                    change_pct=round(pct, 2),
                    direction=direction_from_pct(pct),
                )
            )
        rows.sort(key=lambda r: abs(r.change_pct or 0), reverse=True)
        if top_n is not None:
            return rows[:top_n]
        return rows

    try:
        return await _with_nse_client(_fetch)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nifty 200 movers failed: %s", exc)
        return []


def fetch_commodities_yahoo_fallback() -> list[CommodityRow]:
    """USD futures fallback if Moneycontrol MCX is unavailable."""
    rows: list[CommodityRow] = []
    for name, ticker, _unit in COMMODITY_SYMBOLS:
        data = _quote_change(ticker)
        if data is None:
            rows.append(
                CommodityRow(
                    commodity=name,
                    price="-",
                    change="-",
                    change_pct=None,
                    direction="flat",
                )
            )
            continue
        last, pct = data
        rows.append(
            CommodityRow(
                commodity=name,
                price=f"${format_number(last, 2)}",
                change=format_change_pct(pct),
                change_pct=round(pct, 2),
                direction=direction_from_pct(pct),
            )
        )
    return rows


def _mcx_expiry_iso(exp_date: str) -> str | None:
    """Convert Moneycontrol '05 Oct 2026' to '2026-10-05'."""
    from datetime import datetime

    raw = (exp_date or "").strip()
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


async def fetch_mcx_commodities_moneycontrol() -> list[CommodityRow] | None:
    """MCX quotes from Moneycontrol (contract pricefeed; major list for expiry discovery)."""
    list_url = (
        "https://priceapi.moneycontrol.com/technicalCompanyData/commodity/"
        "getMajorCommodities?tabName=MCX&deviceType=W"
    )
    wanted = {
        "CRUDEOIL": "Crude Oil",
        "NATURALGAS": "Natural Gas",
        "GOLD": "Gold",
        "SILVER": "Silver",
    }
    headers = {
        "User-Agent": NSE_HEADERS["User-Agent"],
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.moneycontrol.com/commodity/",
        "Origin": "https://www.moneycontrol.com",
    }
    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            headers=headers,
            follow_redirects=True,
        ) as client:
            resp = await client.get(list_url)
            resp.raise_for_status()
            payload = resp.json() or {}
            items = ((payload.get("data") or {}).get("list")) or []
            meta: dict[str, dict[str, Any]] = {}
            for item in items:
                symbol = str(item.get("symbol") or "").upper()
                if symbol not in wanted:
                    continue
                meta[symbol] = item

            found: dict[str, CommodityRow] = {}
            for symbol, nice in wanted.items():
                item = meta.get(symbol) or {}
                last: float | None = None
                pct: float | None = None

                # Prefer live contract feed (fresher than major-commodities summary)
                exp_iso = _mcx_expiry_iso(str(item.get("expDate") or ""))
                if exp_iso:
                    feed_url = (
                        "https://priceapi.moneycontrol.com/pricefeed/mcx/"
                        f"commodityfutures/{symbol}?expiry={exp_iso}"
                    )
                    try:
                        feed = await client.get(feed_url)
                        if feed.status_code == 200:
                            data = (feed.json() or {}).get("data") or {}
                            if data.get("lastPrice") is not None:
                                last = float(str(data["lastPrice"]).replace(",", ""))
                            if data.get("perChange") is not None:
                                pct = float(str(data["perChange"]).replace(",", ""))
                            elif last is not None and data.get("prevClose") is not None:
                                prev = float(str(data["prevClose"]).replace(",", ""))
                                if prev:
                                    pct = ((last - prev) / prev) * 100.0
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("MCX contract feed failed %s: %s", symbol, exc)

                # Fallback to major-commodities row
                if last is None or pct is None:
                    try:
                        last = float(str(item.get("lastPrice") or "0").replace(",", ""))
                        pct = float(str(item.get("priceChangePercentage") or "0").replace(",", ""))
                    except ValueError:
                        continue

                found[nice] = CommodityRow(
                    commodity=nice,
                    price=format_inr(last),
                    change=format_change_pct(pct),
                    change_pct=round(pct, 2),
                    direction=direction_from_pct(pct),
                )

            ordered = [found[n] for n in ["Crude Oil", "Natural Gas", "Gold", "Silver"] if n in found]
            if len(ordered) >= 3:
                return ordered
            logger.warning("Moneycontrol MCX returned incomplete list: %s", list(found))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Moneycontrol MCX fetch failed: %s", exc)
    return None


async def fetch_commodities_best_effort() -> list[CommodityRow]:
    """Prefer Moneycontrol MCX INR quotes; fall back to Yahoo USD futures only if needed."""
    mcx = await fetch_mcx_commodities_moneycontrol()
    if mcx:
        return mcx
    return fetch_commodities_yahoo_fallback()
