"""Trang HTML: S4 Nhập khoá · S1 Tổng quan · S2 Độ phủ · S3 Tra thử. Số liệu do JS tải từ /api."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ...config import settings
from ...services.calendar import VN, session_hours_vn
from ..deps import COOKIE, COOKIE_MAX_AGE, is_operator, key_matches, require_operator_page, session_token
from ..limiter import limiter

templates = Jinja2Templates(directory=Path(__file__).resolve().parents[2] / "web" / "templates")
router = APIRouter()

# (v2) màn S5–S7 dựng ở routes/chat.py — ở đây chỉ để `next` sau đăng nhập được quay về đúng màn
V2_PATHS = ("chat", "predictions", "fed")

SCREENS = {
    "overview": ("s1", "Tổng quan"),
    "coverage": ("s2", "Độ phủ dữ liệu"),
    "query": ("s3", "Tra thử theo phiên"),
}


def _years() -> list[int]:
    return list(range(settings.history_start_date.year, datetime.now(UTC).year + 1))


@router.get("/", include_in_schema=False)
async def root(request: Request) -> RedirectResponse:
    return RedirectResponse("/overview" if is_operator(request) else "/login", status_code=303)


def safe_next(value: str | None) -> str:
    """Chỉ nhận các màn nội bộ (giữ query string, vd bộ lọc S3) — chặn open redirect (//evil.com, http://…)."""
    if not value:
        return "/overview"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.path not in {f"/{name}" for name in (*SCREENS, *V2_PATHS)}:
        return "/overview"
    return parts.path + (f"?{urlencode(parse_qsl(parts.query))}" if parts.query else "")


LOGIN_ERRORS = {
    "empty": "Nhập khoá truy cập để vào.",
    "wrong": "Khoá không đúng — dán lại khoá đã được gửi cho bạn.",
    "rate": "Thử sai quá nhiều lần — đợi 1 phút rồi nhập lại.",
}


def login_redirect(next_path: str, err: str) -> RedirectResponse:
    """Nộp khoá lỗi → 303 về GET /login?err=… (POST-redirect-GET): bấm Back không gặp trang "gửi lại biểu mẫu"."""
    return RedirectResponse(f"/login?{urlencode({'next': safe_next(next_path), 'err': err})}", status_code=303)


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, next: str | None = None, het: int = 0, err: str | None = None) -> HTMLResponse:
    if is_operator(request):
        return RedirectResponse(safe_next(next), status_code=303)  # type: ignore[return-value]
    notice = "Phiên đã hết — nhập lại khoá để tiếp tục." if het else None
    return templates.TemplateResponse(
        request, "login.html", {"error": LOGIN_ERRORS.get(err or ""), "notice": notice, "next": safe_next(next)}
    )


@router.post("/login", response_class=HTMLResponse, include_in_schema=False)
@limiter.limit("10/minute")
async def login(
    request: Request, key: Annotated[str, Form()] = "", next: Annotated[str, Form()] = "/overview"
) -> HTMLResponse:
    if not key.strip():
        return login_redirect(next, "empty")  # type: ignore[return-value]
    if not key_matches(key):
        return login_redirect(next, "wrong")  # type: ignore[return-value]
    resp = RedirectResponse(safe_next(next), status_code=303)
    resp.set_cookie(
        COOKIE,
        session_token(),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=settings.secure_cookie,
    )
    return resp  # type: ignore[return-value]


def _screen(name: str):
    screen_id, title = SCREENS[name]

    async def page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            f"{name}.html",
            {
                "screen": screen_id,
                "title": title,
                "years": _years(),
                "hours": session_hours_vn(datetime.now(VN).date()),
            },
        )

    page.__name__ = f"page_{name}"
    return page


for _name in SCREENS:
    router.add_api_route(
        f"/{_name}",
        _screen(_name),
        methods=["GET"],
        response_class=HTMLResponse,
        include_in_schema=False,
        dependencies=[Depends(require_operator_page)],
    )
