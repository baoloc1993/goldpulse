"""Trang HTML: S4 Nhập khoá · S1 Tổng quan · S2 Độ phủ · S3 Tra thử. Số liệu do JS tải từ /api."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ...config import settings
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


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request) -> HTMLResponse:
    if is_operator(request):
        return RedirectResponse("/overview", status_code=303)  # type: ignore[return-value]
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse, include_in_schema=False)
@limiter.limit("10/minute")
async def login(request: Request, key: Annotated[str, Form()] = "") -> HTMLResponse:
    if not key.strip():
        return templates.TemplateResponse(
            request, "login.html", {"error": "Nhập khoá truy cập để vào."}, status_code=400
        )
    if not key_matches(key):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Khoá không đúng. Kiểm tra lại OPS_ACCESS_KEY."}, status_code=401
        )
    resp = RedirectResponse("/overview", status_code=303)
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
            {"screen": screen_id, "title": title, "years": _years()},
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
