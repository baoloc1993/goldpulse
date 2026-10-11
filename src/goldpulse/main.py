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
from .api.routes import api, btc, chat, health, pages
from .config import settings
from .logs import setup_logging
from .services.errors import record_error

setup_logging()
log = structlog.get_logger("web")

app = FastAPI(title="GoldPulse", docs_url=None, redoc_url=None, openapi_url=None)
app.state.limiter = limiter
MAX_BODY = 64 * 1024  # mọi body hợp lệ < 3 KB (câu chat ≤ 2000 ký tự); 20 MB vẫn bị parse trọn


class SafeStatic(StaticFiles):
    """Tên file có ký tự Windows cấm (`<`, `?`, `|`…) → os.stat ném OSError → 500 + lộ đường dẫn. Coi là 404."""

    def lookup_path(self, path: str):  # type: ignore[override]
        try:
            return super().lookup_path(path)
        except OSError:
            return "", None


class BodyLimit:
    """ASGI: chặn body quá MAX_BODY trước khi route đọc — cả khi có Content-Length lẫn chunked."""

    def __init__(self, inner) -> None:
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        length = headers.get(b"content-length")
        if length is not None and length.isdigit() and int(length) > MAX_BODY:
            return await _too_large(send)
        seen = 0

        async def limited():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > MAX_BODY:
                    raise _BodyTooLarge()
            return msg

        try:
            await self.inner(scope, limited, send)
        except _BodyTooLarge:
            await _too_large(send)


class _BodyTooLarge(Exception):
    pass


async def _too_large(send) -> None:
    body = '{"detail":"Dữ liệu gửi lên quá lớn."}'.encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


app.mount("/static", SafeStatic(directory=Path(__file__).parent / "web" / "static"), name="static")
app.include_router(health.router)
app.include_router(pages.router)
app.include_router(api.router)
app.include_router(chat.pages)
app.include_router(chat.api)
if settings.btc_on:  # (v4) instance chỉ vàng không có /btc — không banner đỏ vô nghĩa
    app.include_router(btc.pages)
    app.include_router(btc.api)


@app.exception_handler(RateLimitExceeded)
async def _too_many(request: Request, exc: RateLimitExceeded) -> Response:
    if request.url.path == "/login":
        return pages.login_redirect(pages.home(), "rate")
    return JSONResponse(
        {"detail": "Thao tác quá nhanh — đợi 1 phút rồi thử lại."}, status_code=429, headers={"Retry-After": "60"}
    )


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
    """Lỗi chưa lường trước: log đủ ngữ cảnh + ghi lại, người dùng chỉ thấy câu tiếng Việt — không traceback."""
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


app.add_middleware(BodyLimit)  # ngoài cùng: chặn trước cả request_id/route
