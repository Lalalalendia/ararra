"""Jinja2 shell for the ReadEra closed team interface.

The HTML structure is rendered server-side; vanilla JS exchanges the existing
team key once for an opaque, revocable HttpOnly browser session. The /v1 API
continues accepting X-API-Key for existing non-browser clients.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

ASSETS = Path(__file__).resolve().parents[2] / "web_ui"
TEMPLATES = Jinja2Templates(directory=str(ASSETS / "templates"))
router = APIRouter(prefix="/app", include_in_schema=False)
_ALLOWED = {"", "overview", "books", "import", "team", "jobs", "editor", "cabinet"}

_PRIVATE_HTML_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
}


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
@router.get("/{page}", response_class=HTMLResponse)
def read_ui(request: Request, page: str = ""):
    if page not in _ALLOWED:
        raise HTTPException(status_code=404, detail="Page not found")
    return TEMPLATES.TemplateResponse(
        request=request,
        name="shell.html",
        context={"active_page": page or "overview"},
        headers=_PRIVATE_HTML_HEADERS,
    )
