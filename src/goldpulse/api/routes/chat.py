"""(v2) S5 Chat + S6 Dự đoán + S7 Lịch FED · (v3) S8/S9 Tài khoản giấy — trang HTML + API JSON.

Mọi route cần khoá truy cập (PERSONAS §2).

Không có route nào gọi Claude hay gửi lệnh giao dịch: câu hỏi chỉ được xếp vào bot_job cho tiến trình não.
"""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ...schemas import Json
from ...services import chat as chat_svc
from ...services import fed as fed_svc
from ...services import paper as paper_svc
from ...services import predictions as pred_svc
from ..deps import get_session, require_operator_api, require_operator_page
from ..limiter import limiter
from .pages import templates

pages = APIRouter(dependencies=[Depends(require_operator_page)])
api = APIRouter(prefix="/api", dependencies=[Depends(require_operator_api)])
DB = Annotated[AsyncSession, Depends(get_session)]
INT4 = 2**31 - 1  # id là integer Postgres — vượt quá thì 422 thay vì 500 (dogfood breaker 2026-09-26)
MsgId = Annotated[int, Path(ge=1, le=INT4)]

V2_SCREENS = {"chat": ("s5", "Chat"), "predictions": ("s6", "Dự đoán 15 phút"), "fed": ("s7", "Lịch quyết định FED")}


def _page(name: str):
    screen_id, title = V2_SCREENS[name]

    async def page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, f"{name}.html", {"screen": screen_id, "title": title})

    page.__name__ = f"page_{name}"
    return page


for _name in V2_SCREENS:
    pages.add_api_route(
        f"/{_name}", _page(_name), methods=["GET"], response_class=HTMLResponse, include_in_schema=False
    )


def _raise(e: chat_svc.ChatError) -> None:
    raise HTTPException(e.status, e.message) from e


class ChatIn(BaseModel):
    # Trần thô chống body khổng lồ; quá 2000 ký tự thì service trả câu "tối đa 2000 ký tự"
    content: str = Field(max_length=chat_svc.MAX_LEN * 50)
    thread_id: int | None = Field(default=None, ge=1, le=INT4)


@api.get("/chat/header")
async def chat_header(session: DB) -> Json:
    return await chat_svc.header(session)


@api.get("/chat/thread")
async def chat_thread(session: DB, thread_id: Annotated[int | None, Query(ge=1, le=INT4)] = None) -> Json:
    try:
        return await chat_svc.thread_view(session, thread_id)
    except chat_svc.ChatError as e:
        _raise(e)
        raise


@api.post("/chat/messages", status_code=202)
@limiter.limit("20/minute")
async def chat_post(request: Request, body: ChatIn, session: DB) -> Json:
    try:
        out = await chat_svc.post(session, body.thread_id, body.content)
        # Commit TRƯỚC khi trả phản hồi: dependency có yield commit sau khi phản hồi đã gửi → client dùng ngay
        # thread_id / message_id có thể gặp 404 (dogfood breaker 2026-09-26)
        await session.commit()
        return out
    except chat_svc.ChatError as e:
        _raise(e)
        raise


@api.get("/chat/messages/{message_id}")
async def chat_message(message_id: MsgId, session: DB) -> Json:
    try:
        return await chat_svc.get(session, message_id)
    except chat_svc.ChatError as e:
        _raise(e)
        raise


@api.post("/chat/messages/{message_id}/resend", status_code=202)
@limiter.limit("20/minute")
async def chat_resend(request: Request, message_id: MsgId, session: DB) -> Json:
    try:
        out = await chat_svc.resend(session, message_id)
        await session.commit()
        return out
    except chat_svc.ChatError as e:
        _raise(e)
        raise


@api.post("/chat/threads", status_code=201)
@limiter.limit("20/minute")
async def chat_new(request: Request, session: DB) -> Json:
    out = await chat_svc.new_thread(session)
    await session.commit()
    return out


@api.get("/predictions")
async def predictions(session: DB) -> Json:
    return await pred_svc.screen(session)


@api.get("/predictions/rows")
async def prediction_rows(session: DB, before: Annotated[int, Query(ge=1, le=INT4)]) -> Json:
    return {"rows": await pred_svc.page(session, before)}


@api.get("/fed")
async def fed(session: DB) -> Json:
    return await fed_svc.screen(session)


# --- (v3) S8 Tài khoản giấy · S9 Nhật ký lệnh — tiền giả, web chỉ được đổi `enabled` (PERSONAS §2) -------------
AccCode = Annotated[str, Path(pattern="^[A-Z]$")]


class EnabledIn(BaseModel):
    enabled: bool = Field(strict=True)


@pages.get("/paper", response_class=HTMLResponse, include_in_schema=False)
async def page_paper(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "paper.html", {"screen": "s8", "title": "Tài khoản giấy"})


@pages.get("/paper/{code}", response_class=HTMLResponse, include_in_schema=False)
async def page_paper_account(request: Request, code: Annotated[str, Path(max_length=8)], session: DB) -> HTMLResponse:
    # Trang HTML: mã sai kiểu (/paper/a) → 404 tử tế như /paper/Z, không 422 JSON (dogfood edge 2026-09-27)
    if not re.fullmatch(r"[A-Z]", code) or not await paper_svc.account_exists(session, code):
        raise HTTPException(404, "Không có tài khoản này")
    return templates.TemplateResponse(
        request, "paper_trades.html", {"screen": "s9", "title": f"Tài khoản {code}", "code": code}
    )


@api.get("/paper/accounts")
async def paper_accounts(session: DB) -> Json:
    return await paper_svc.screen(session)


@api.get("/paper/accounts/{code}")
async def paper_account(code: AccCode, session: DB) -> Json:
    d = await paper_svc.account_screen(session, code)
    if d is None:
        raise HTTPException(404, "Không có tài khoản này")
    return d


@api.get("/paper/accounts/{code}/trades")
async def paper_trades(code: AccCode, session: DB, before: Annotated[int, Query(ge=1, le=INT4)]) -> Json:
    if not await paper_svc.account_exists(session, code):
        raise HTTPException(404, "Không có tài khoản này")
    return {"rows": await paper_svc.trades_page(session, code, before)}


@api.post("/paper/accounts/{code}/enabled")
@limiter.limit("30/minute")
async def paper_enabled(request: Request, code: AccCode, body: EnabledIn, session: DB) -> Json:
    if not await paper_svc.set_enabled(session, code, body.enabled):
        raise HTTPException(404, "Không có tài khoản này")
    await session.commit()  # commit trước khi trả — client tải lại ngay phải thấy trạng thái mới
    return {"code": code, "enabled": body.enabled}
