"""Jinja2 shell for the ReadEra closed team interface.

The HTML structure is rendered server-side; a small vanilla-JS adapter
connects its forms to the existing, authenticated /v1 API. Unlike a separate
session system, this retains the same in-memory team key security model as
the already tested React pilot. No API key is ever put in templates or URLs.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

ASSETS = Path(__file__).resolve().parents[2] / "web_ui"
TEMPLATES = Jinja2Templates(directory=str(ASSETS / "templates"))
router = APIRouter(prefix="/app", include_in_schema=False)
_ALLOWED = {"", "overview", "books", "import", "team", "jobs"}

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
