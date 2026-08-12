"""HTML -> PDF rendering via WeasyPrint, with ReportLab fallback for Windows."""

from __future__ import annotations

import html as html_lib
import io
import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.models import ReportDraft
from app.utils import strip_em_dashes

logger = logging.getLogger(__name__)

_UNICODE_FONT_CANDIDATES = (
    Path(r"C:\Windows\Fonts\segoeui.ttf"),
    Path(r"C:\Windows\Fonts\arial.ttf"),
    Path(r"C:\Windows\Fonts\calibri.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
)


def _register_unicode_fonts() -> tuple[str, str]:
    """Return (regular, bold) font names that can render INR when possible."""
    for path in _UNICODE_FONT_CANDIDATES:
        if not path.exists():
            continue
        try:
            regular = "ReportSans"
            bold = "ReportSans-Bold"
            pdfmetrics.registerFont(TTFont(regular, str(path)))
            bold_path = path
            if path.name.lower() == "segoeui.ttf":
                bold_path = path.with_name("segoeuib.ttf")
            elif path.name.lower() == "arial.ttf":
                bold_path = path.with_name("arialbd.ttf")
            elif path.name.lower() == "calibri.ttf":
                bold_path = path.with_name("calibrib.ttf")
            elif "DejaVuSans" in path.name:
                bold_path = path.with_name("DejaVuSans-Bold.ttf")
            elif "LiberationSans-Regular" in path.name:
                bold_path = path.with_name("LiberationSans-Bold.ttf")
            if bold_path.exists() and bold_path != path:
                pdfmetrics.registerFont(TTFont(bold, str(bold_path)))
            else:
                bold = regular
            return regular, bold
        except Exception:  # noqa: BLE001
            continue
    return "Times-Roman", "Times-Bold"


def _clean_draft(draft: ReportDraft) -> ReportDraft:
    """Ensure no em dashes remain in narrative fields before export."""
    data = draft.model_dump()
    data["narrative"]["key_takeaways"] = [
        strip_em_dashes(x) for x in data["narrative"]["key_takeaways"]
    ]
    cleaned_news = []
    for item in data["narrative"]["news_updates"]:
        if isinstance(item, str):
            cleaned_news.append({"text": strip_em_dashes(item), "source": ""})
        else:
            cleaned_news.append(
                {
                    "text": strip_em_dashes(item.get("text") or ""),
                    "source": strip_em_dashes(item.get("source") or ""),
                }
            )
    data["narrative"]["news_updates"] = cleaned_news
    for row in data["stocks_in_focus"]:
        row["whats_happening"] = strip_em_dashes(row["whats_happening"])
        row["move"] = strip_em_dashes(row["move"])
        row["stock"] = strip_em_dashes(row["stock"])
    return ReportDraft.model_validate(data)


def render_html(draft: ReportDraft, templates_dir: Path) -> str:
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("report.html")
    clean = _clean_draft(draft)
    return template.render(report=clean)


def _plain(text: object) -> str:
    """Plain text for non-HTML table cells (no &amp; entities)."""
    return html_lib.unescape("" if text is None else str(text))


def _esc(text: object) -> str:
    """Escape for ReportLab Paragraph markup."""
    s = _plain(text)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _section_heading(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(f"<b>{_esc(text)}</b>", style)


def _bullet_para(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(f"• {_esc(text)}", style)


def _make_table(
    headers: list[str],
    rows: list[list],
    col_widths: list[float] | None = None,
    *,
    font_regular: str,
    font_bold: str,
) -> Table:
    data = [headers] + rows
    table = Table(data, colWidths=col_widths, hAlign="LEFT", repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, 0), font_bold),
                ("FONTNAME", (0, 1), (-1, -1), font_regular),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3f3f3")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1a1a1a")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _render_pdf_reportlab(draft: ReportDraft) -> bytes:
    clean = _clean_draft(draft)
    font_regular, font_bold = _register_unicode_fonts()
    use_rupee = font_regular not in {"Times-Roman", "Helvetica"}

    def txt(value: object) -> str:
        s = _plain(value)
        if not use_rupee:
            s = s.replace("₹", "Rs ")
        return s

    def para_txt(value: object) -> str:
        s = txt(value)
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
    )
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "ReportTitle",
        parent=styles["Heading1"],
        fontName=font_bold,
        fontSize=20,
        leading=24,
        spaceAfter=2,
        textColor=colors.HexColor("#1a1a1a"),
    )
    date_style = ParagraphStyle(
        "ReportDate",
        parent=styles["Normal"],
        fontName=font_regular,
        fontSize=11,
        leading=14,
        spaceAfter=12,
        textColor=colors.HexColor("#333333"),
    )
    h2 = ParagraphStyle(
        "ReportH2",
        parent=styles["Heading2"],
        fontName=font_bold,
        fontSize=12,
        leading=15,
        spaceBefore=12,
        spaceAfter=6,
        textColor=colors.HexColor("#1a1a1a"),
    )
    body = ParagraphStyle(
        "ReportBody",
        parent=styles["Normal"],
        fontName=font_regular,
        fontSize=10,
        leading=13,
        alignment=TA_LEFT,
        spaceAfter=3,
    )
    cell = ParagraphStyle(
        "ReportCell",
        parent=body,
        fontSize=9,
        leading=11,
        spaceAfter=0,
    )

    story: list = []
    story.append(Paragraph("Daily Market Update", title))
    story.append(Paragraph(para_txt(clean.date_label), date_style))

    story.append(_section_heading("Key Takeaways", h2))
    for t in clean.narrative.key_takeaways:
        raw = _plain(t)
        if not use_rupee:
            raw = raw.replace("₹", "Rs ")
        story.append(_bullet_para(raw, body))

    def add_table_section(heading: str, headers: list[str], rows: list[list], widths: list[float]) -> None:
        block = [
            _section_heading(heading, h2),
            _make_table(headers, rows, widths, font_regular=font_regular, font_bold=font_bold),
        ]
        story.append(KeepTogether(block))

    add_table_section(
        "Market Snapshot",
        ["Index", "Close", "Change"],
        [[txt(r.name), txt(r.close), txt(r.change)] for r in clean.market_snapshot],
        [70 * mm, 45 * mm, 45 * mm],
    )
    add_table_section(
        "Global Markets",
        ["Name", "Level", "Change"],
        [[txt(r.name), txt(r.close), txt(r.change)] for r in clean.global_markets],
        [70 * mm, 45 * mm, 45 * mm],
    )
    add_table_section(
        "Sectors Trending Today",
        ["Sector", "Change"],
        [[txt(r.name), txt(r.change)] for r in clean.sectors],
        [110 * mm, 50 * mm],
    )
    add_table_section(
        "Top Gainers - NIFTY 50",
        ["Company", "Price", "Change"],
        [[txt(r.company), txt(r.price), txt(r.change)] for r in clean.gainers],
        [80 * mm, 40 * mm, 40 * mm],
    )
    add_table_section(
        "Top Losers - NIFTY 50",
        ["Company", "Price", "Change"],
        [[txt(r.company), txt(r.price), txt(r.change)] for r in clean.losers],
        [80 * mm, 40 * mm, 40 * mm],
    )

    story.append(_section_heading("Advances / Declines", h2))
    ad = clean.advances_declines
    story.append(Paragraph(f"Advance - {para_txt(ad.advance if ad.advance is not None else '-')}", body))
    story.append(Paragraph(f"Decline - {para_txt(ad.decline if ad.decline is not None else '-')}", body))
    story.append(Paragraph(f"A/D ratio: {para_txt(ad.ratio if ad.ratio is not None else '-')}", body))
    if ad.note:
        story.append(Paragraph(f"<i>{para_txt(ad.note)}</i>", body))
    story.append(Spacer(1, 4))

    story.append(_section_heading("News & Updates", h2))
    for item in clean.narrative.news_updates:
        text = item if isinstance(item, str) else (item.text or "")
        if text:
            raw = _plain(text)
            if not use_rupee:
                raw = raw.replace("₹", "Rs ")
            story.append(_bullet_para(raw, body))

    focus_rows = [
        [
            Paragraph(para_txt(r.stock), cell),
            Paragraph(para_txt(r.move), cell),
            Paragraph(para_txt(r.whats_happening), cell),
        ]
        for r in clean.stocks_in_focus
    ]
    add_table_section(
        "Stocks in Focus",
        ["Stock", "Move", "What's Happening"],
        focus_rows if focus_rows else [["-", "-", "-"]],
        [45 * mm, 25 * mm, 90 * mm],
    )
    add_table_section(
        "Commodity Market Watch",
        ["Commodity", "Price", "1-Day Change"],
        [[txt(r.commodity), txt(r.price), txt(r.change)] for r in clean.commodities],
        [60 * mm, 50 * mm, 50 * mm],
    )

    doc.build(story)
    return buf.getvalue()


def render_pdf_bytes(draft: ReportDraft, templates_dir: Path) -> bytes:
    html = render_html(draft, templates_dir)
    try:
        from weasyprint import HTML

        return HTML(string=html, base_url=str(templates_dir)).write_pdf()
    except Exception as exc:  # noqa: BLE001
        logger.warning("WeasyPrint unavailable (%s); using ReportLab fallback.", exc)
        return _render_pdf_reportlab(draft)
