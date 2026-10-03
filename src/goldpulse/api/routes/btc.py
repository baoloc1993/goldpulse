"""(v4) S10 BTC · S11 Nhật ký lệnh BTC — trang HTML + API JSON, sau khoá truy cập (PERSONAS §2 v4).

Chỉ đọc bảng `inst_*`; ghi duy nhất là bật/tắt tài khoản. Không route nào mở/đóng/sửa lệnh, không gọi sàn hay Claude.
"""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ...schemas import Json
from ...services.btc import paper as btc_svc
from ..deps import get_session, require_operator_api, require_operator_page
from ..limiter import limiter
from .pages import templates

pages = APIRouter(dependencies=[Depends(require_operator_page)])
api = APIRouter(prefix="/api/btc", dependencies=[Depends(require_operator_api)])
DB = Annotated[AsyncSession, Depends(get_session)]
CODE_RE = r"^BTC-[A-Z]$"
INT4 = 2**31 - 1
Code = Annotated[str, Path(pattern=CODE_RE)]


class EnabledIn(BaseModel):
    enabled: bool = Field(strict=True)


@pages.get("/btc", response_class=HTMLResponse, include_in_schema=False)
async def page_btc(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "btc.html", {"screen": "s10", "title": "BTC · tài khoản giấy"})


@pages.get("/btc/{code}", response_class=HTMLResponse, include_in_schema=False)
async def page_btc_account(request: Request, code: str, session: DB) -> HTMLResponse:
    # Mã sai kiểu → 404 tử tế, không 422 JSON (bài học dogfood S9 vòng 3)
    if not re.fullmatch(CODE_RE, code) or not await btc_svc.account_exists(session, code):
        raise HTTPException(404, "Không có tài khoản này")
    return templates.TemplateResponse(
        request, "btc_trades.html", {"screen": "s11", "title": f"BTC · {code}", "code": code}
    )


@api.get("/accounts")
async def btc_accounts(session: DB) -> Json:
    return await btc_svc.screen(session)


@api.get("/accounts/{code}")
async def btc_account(code: Code, session: DB) -> Json:
    d = await btc_svc.account_screen(session, code)
    if d is None:
        raise HTTPException(404, "Không có tài khoản này")
    return d


@api.get("/accounts/{code}/trades")
async def btc_trades(code: Code, session: DB, before: Annotated[int, Query(ge=1, le=INT4)]) -> Json:
    if not await btc_svc.account_exists(session, code):
        raise HTTPException(404, "Không có tài khoản này")
    return {"rows": await btc_svc.trades_page(session, code, before)}


@api.post("/accounts/{code}/enabled")
@limiter.limit("30/minute")
async def btc_enabled(request: Request, code: Code, body: EnabledIn, session: DB) -> Json:
    if not await btc_svc.set_enabled(session, code, body.enabled):
        raise HTTPException(404, "Không có tài khoản này")
    await session.commit()  # client tải lại ngay phải thấy trạng thái mới
    return {"code": code, "enabled": body.enabled}
