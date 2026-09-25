"""(v2) S5 Chat + S6 Dự đoán + S7 Lịch FED — trang HTML và API JSON, mọi route cần khoá truy cập (PERSONAS §2).

Không có route nào gọi Claude hay gửi lệnh giao dịch: câu hỏi chỉ được xếp vào bot_job cho tiến trình não.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ...schemas import Json
from ...services import chat as chat_svc
from ...services import fed as fed_svc
from ...services import predictions as pred_svc
from ..deps import get_session, require_operator_api, require_operator_page
from ..limiter import limiter
from .pages import templates

pages = APIRouter(dependencies=[Depends(require_operator_page)])
api = APIRouter(prefix="/api", dependencies=[Depends(require_operator_api)])
DB = Annotated[AsyncSession, Depends(get_session)]

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
    content: str = Field(max_length=chat_svc.MAX_LEN * 2)
    thread_id: int | None = Field(default=None, ge=1)


@api.get("/chat/header")
async def chat_header(session: DB) -> Json:
    return await chat_svc.header(session)


@api.get("/chat/thread")
async def chat_thread(session: DB, thread_id: Annotated[int | None, Query(ge=1)] = None) -> Json:
    try:
        return await chat_svc.thread_view(session, thread_id)
    except chat_svc.ChatError as e:
        _raise(e)
        raise


@api.post("/chat/messages", status_code=202)
@limiter.limit("20/minute")
async def chat_post(request: Request, body: ChatIn, session: DB) -> Json:
    try:
        return await chat_svc.post(session, body.thread_id, body.content)
    except chat_svc.ChatError as e:
        _raise(e)
        raise


@api.get("/chat/messages/{message_id}")
async def chat_message(message_id: int, session: DB) -> Json:
    try:
        return await chat_svc.get(session, message_id)
    except chat_svc.ChatError as e:
        _raise(e)
        raise


@api.post("/chat/messages/{message_id}/resend", status_code=202)
@limiter.limit("20/minute")
async def chat_resend(request: Request, message_id: int, session: DB) -> Json:
    try:
        return await chat_svc.resend(session, message_id)
    except chat_svc.ChatError as e:
        _raise(e)
        raise


@api.post("/chat/threads", status_code=201)
@limiter.limit("20/minute")
async def chat_new(request: Request, session: DB) -> Json:
    return await chat_svc.new_thread(session)


@api.get("/predictions")
async def predictions(session: DB) -> Json:
    return await pred_svc.screen(session)


@api.get("/predictions/rows")
async def prediction_rows(session: DB, before: Annotated[int, Query(ge=1)]) -> Json:
    return {"rows": await pred_svc.page(session, before)}


@api.get("/fed")
async def fed(session: DB) -> Json:
    return await fed_svc.screen(session)
