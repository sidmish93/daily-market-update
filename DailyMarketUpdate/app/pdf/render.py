"""HTML -> PDF rendering via WeasyPrint."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.models import ReportDraft
from app.utils import strip_em_dashes


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


def render_pdf_bytes(draft: ReportDraft, templates_dir: Path) -> bytes:
    html = render_html(draft, templates_dir)
    try:
        from weasyprint import HTML
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "WeasyPrint is not available in this environment. "
            "On Windows, install GTK libraries or export from a Linux/Render host."
        ) from exc
    return HTML(string=html, base_url=str(templates_dir)).write_pdf()
