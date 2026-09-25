"""Web: `uvicorn goldpulse.main:app` — trang theo dõi + API + /health."""

import uuid
from pathlib import Path
from urllib.parse import urlencode

import structlog
from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api.deps import LoginRequired
from .api.limiter import limiter
from .api.routes import api, chat, health, pages
from .logs import setup_logging
from .services.errors import record_error

setup_logging()
log = structlog.get_logger("web")

app = FastAPI(title="GoldPulse", docs_url=None, redoc_url=None, openapi_url=None)
app.state.limiter = limiter
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")
app.include_router(health.router)
app.include_router(pages.router)
app.include_router(api.router)
app.include_router(chat.pages)
app.include_router(chat.api)


@app.exception_handler(RateLimitExceeded)
async def _too_many(request: Request, exc: RateLimitExceeded) -> Response:
    if request.url.path == "/login":
        return pages.login_redirect("/overview", "rate")
    return JSONResponse({"detail": "Thao tác quá nhanh — đợi 1 phút rồi thử lại."}, status_code=429)


@app.exception_handler(RequestValidationError)
async def _bad_input(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    if any(e.get("type") in ("json_invalid", "model_attributes_type") or e["loc"] == ("body",) for e in errors):
        return JSONResponse({"detail": "Dữ liệu gửi lên không đúng định dạng."}, status_code=422)
    fields = ", ".join(str(e["loc"][-1]) for e in errors)
    return JSONResponse({"detail": f"Tham số không hợp lệ: {fields}."}, status_code=422)


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


@app.exception_handler(Exception)
async def _server_error(request: Request, exc: Exception) -> Response:
    """Lỗi chưa lường trước: log đủ ngữ cảnh + ghi để S1 hiện, người dùng chỉ thấy câu tiếng Việt — không traceback."""
    log.exception("unhandled_error", path=request.url.path)
    await record_error(f"web {request.method} {request.url.path}", exc)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Máy chủ gặp lỗi — thử lại sau ít phút."}, status_code=500)
    return PlainTextResponse("Máy chủ GoldPulse gặp lỗi — thử lại sau ít phút.", status_code=500)


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
    response.headers["x-frame-options"] = "DENY"
    # Không script/style inline, không nguồn ngoài — trang chỉ dùng /static của chính nó
    response.headers["content-security-policy"] = (
        "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    if not request.url.path.startswith("/static/"):
        response.headers["cache-control"] = "no-store"  # số liệu vận hành, không để proxy/trình duyệt giữ bản cũ
    return response
