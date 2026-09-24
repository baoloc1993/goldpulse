"""Web: `uvicorn goldpulse.main:app` — trang theo dõi + API + /health."""

import uuid
from pathlib import Path
from urllib.parse import urlencode

import structlog
from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api.deps import LoginRequired
from .api.limiter import limiter
from .api.routes import api, health, pages
from .logs import setup_logging

setup_logging()
log = structlog.get_logger("web")

app = FastAPI(title="GoldPulse", docs_url=None, redoc_url=None, openapi_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")
app.include_router(health.router)
app.include_router(pages.router)
app.include_router(api.router)


@app.exception_handler(LoginRequired)
async def _to_login(request: Request, exc: LoginRequired) -> RedirectResponse:
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?{urlencode({'next': target})}", status_code=303)


@app.exception_handler(StarletteHTTPException)
async def _not_found(request: Request, exc: StarletteHTTPException) -> Response:
    """404 ở trang HTML → trang tối cùng giao diện; API vẫn trả JSON."""
    if exc.status_code == 404 and not request.url.path.startswith(("/api/", "/static/")):
        return pages.templates.TemplateResponse(request, "not_found.html", {}, status_code=404)
    return await http_exception_handler(request, exc)


@app.middleware("http")
async def request_id(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    structlog.contextvars.bind_contextvars(request_id=rid)
    try:
        response = await call_next(request)
    finally:
        structlog.contextvars.clear_contextvars()
    response.headers["x-request-id"] = rid
    response.headers["x-content-type-options"] = "nosniff"
    response.headers["referrer-policy"] = "no-referrer"
    return response
