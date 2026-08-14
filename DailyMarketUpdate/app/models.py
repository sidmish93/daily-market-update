"""Canonical report models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class IndexRow(BaseModel):
    name: str
    close: str
    change: str
    change_pct: float | None = None
    direction: str = "flat"  # up | down | flat


class SectorRow(BaseModel):
    name: str
    change: str
    change_pct: float | None = None
    direction: str = "flat"


class MoverRow(BaseModel):
    company: str
    price: str
    change: str
    change_pct: float | None = None
    direction: str = "flat"
    symbol: str = ""


class StockFocusRow(BaseModel):
    stock: str
    move: str
    whats_happening: str
    direction: str = "flat"


class AdvancesDeclines(BaseModel):
    advance: int | None = None
    decline: int | None = None
    ratio: float | None = None
    note: str = ""


class CommodityRow(BaseModel):
    commodity: str
    price: str
    change: str
    change_pct: float | None = None
    direction: str = "flat"


class NewsUpdateItem(BaseModel):
    text: str
    source: str = ""  # shown in draft UI only; omitted from PDF


class NarrativeBlock(BaseModel):
    # Analytical bullets (not a restatement of the tables)
    key_takeaways: list[str] = Field(default_factory=list)
    news_updates: list[NewsUpdateItem] = Field(default_factory=list)


class NewsCandidate(BaseModel):
    title: str
    summary: str = ""
    source: str
    url: str = ""
    published: str = ""
    score: float = 0
    relevance_tags: list[str] = Field(default_factory=list)


class ReportDraft(BaseModel):
    date_iso: str
    date_label: str
    market_snapshot: list[IndexRow] = Field(default_factory=list)
    global_markets: list[IndexRow] = Field(default_factory=list)
    sectors: list[SectorRow] = Field(default_factory=list)
    gainers: list[MoverRow] = Field(default_factory=list)
    losers: list[MoverRow] = Field(default_factory=list)
    nifty200_movers: list[MoverRow] = Field(default_factory=list)
    advances_declines: AdvancesDeclines = Field(default_factory=AdvancesDeclines)
    commodities: list[CommodityRow] = Field(default_factory=list)
    stocks_in_focus: list[StockFocusRow] = Field(default_factory=list)
    narrative: NarrativeBlock = Field(default_factory=NarrativeBlock)
    news_candidates: list[NewsCandidate] = Field(default_factory=list)
