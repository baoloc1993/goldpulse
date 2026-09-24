"""Trang HTML: S4 Nhập khoá · S1 Tổng quan · S2 Độ phủ · S3 Tra thử. Số liệu do JS tải từ /api."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ...config import settings
from ...services.calendar import VN, session_hours_vn
from ..deps import COOKIE, COOKIE_MAX_AGE, is_operator, key_matches, require_operator_page, session_token
from ..limiter import limiter

templates = Jinja2Templates(directory=Path(__file__).resolve().parents[2] / "web" / "templates")
router = APIRouter()

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
    """Chỉ nhận đường dẫn nội bộ của trang theo dõi — chặn open redirect (//evil.com, http://…)."""
    allowed = {f"/{name}" for name in SCREENS}
    return value if value in allowed else "/overview"


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, next: str | None = None, het: int = 0) -> HTMLResponse:
    if is_operator(request):
        return RedirectResponse(safe_next(next), status_code=303)  # type: ignore[return-value]
    notice = "Phiên đã hết — nhập lại khoá để tiếp tục." if het else None
    return templates.TemplateResponse(request, "login.html", {"error": None, "notice": notice, "next": safe_next(next)})


@router.post("/login", response_class=HTMLResponse, include_in_schema=False)
@limiter.limit("10/minute")
async def login(
    request: Request, key: Annotated[str, Form()] = "", next: Annotated[str, Form()] = "/overview"
) -> HTMLResponse:
    ctx = {"notice": None, "next": safe_next(next)}
    if not key.strip():
        return templates.TemplateResponse(
            request, "login.html", {**ctx, "error": "Nhập khoá truy cập để vào."}, status_code=400
        )
    if not key_matches(key):
        return templates.TemplateResponse(
            request,
            "login.html",
            {**ctx, "error": "Khoá không đúng — dán lại khoá đã được gửi cho bạn."},
            status_code=401,
        )
    resp = RedirectResponse(safe_next(next), status_code=303)
    resp.set_cookie(
        COOKIE,
        session_token(),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=settings.is_production,
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
