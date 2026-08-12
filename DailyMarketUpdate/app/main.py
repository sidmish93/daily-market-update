"""Daily Market Update generator — FastAPI entrypoint."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.models import ReportDraft
from app.pipeline import generate_report_draft
from app.pdf.render import render_pdf_bytes

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

app = FastAPI(title="Daily Market Update", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class GenerateRequest(BaseModel):
    date: str | None = Field(
        default=None,
        description="Report date in YYYY-MM-DD. Defaults to today (Asia/Kolkata).",
    )
    include_narrative: bool = True


class GenerateResponse(BaseModel):
    draft: ReportDraft
    source_status: dict[str, str]


class PdfExportRequest(BaseModel):
    draft: ReportDraft


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    index_path = STATIC_DIR / "index.html"
    return HTMLResponse(
        index_path.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest) -> GenerateResponse:
    try:
        draft, status = await generate_report_draft(
            report_date=req.date,
            include_narrative=req.include_narrative,
        )
        return GenerateResponse(draft=draft, source_status=status)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/export/pdf")
async def export_pdf(req: PdfExportRequest) -> Response:
    try:
        pdf_bytes = render_pdf_bytes(req.draft, templates_dir=TEMPLATES_DIR)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"PDF export failed: {exc}") from exc

    filename = f"Daily_Market_Update_{req.draft.date_label.replace(' ', '_')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=True,
    )
