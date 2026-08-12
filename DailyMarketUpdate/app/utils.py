"""Shared formatting helpers."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def today_ist() -> datetime:
    return datetime.now(IST)


def format_date_label(dt: datetime) -> str:
    # e.g. 11 August 2026
    return f"{dt.day} {dt.strftime('%B %Y')}"


def parse_report_date(date_str: str | None) -> datetime:
    if not date_str:
        return today_ist()
    return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=IST)


def format_number(value: float, decimals: int = 2) -> str:
    if abs(value) >= 1000:
        return f"{value:,.{decimals}f}"
    return f"{value:.{decimals}f}"


def format_inr(value: float) -> str:
    return f"₹{format_number(value, 2)}"


def format_change_pct(pct: float) -> str:
    arrow = "▲" if pct > 0 else "▼" if pct < 0 else "●"
    return f"{arrow} {abs(pct):.2f}%"


def direction_from_pct(pct: float) -> str:
    if pct > 0:
        return "up"
    if pct < 0:
        return "down"
    return "flat"


def strip_em_dashes(text: str) -> str:
    return (
        text.replace("\u2014", "-")
        .replace("\u2013", "-")
        .replace("—", "-")
        .replace("–", "-")
    )
