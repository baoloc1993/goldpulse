"""API JSON cho trang theo dõi — mọi route cần khoá truy cập (PERSONAS §2)."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import settings
from ...schemas import Json, RetryResult, SessionName, SessionQuery, SessionQueryResult
from ...services import monitor
from ..deps import get_session, require_operator_api

router = APIRouter(prefix="/api", dependencies=[Depends(require_operator_api)])
DB = Annotated[AsyncSession, Depends(get_session)]


@router.get("/overview")
async def overview(session: DB) -> Json:
    return await monitor.overview(session)


@router.get("/coverage")
async def coverage(session: DB) -> Json:
    return await monitor.coverage(session)


@router.get("/query", response_model=SessionQueryResult)
async def query(
    session: DB,
    session_name: Annotated[SessionName, Query(alias="session")],
    year_from: Annotated[int, Query(ge=2000, le=2100)],
    year_to: Annotated[int, Query(ge=2000, le=2100)],
    dow: Annotated[int | None, Query(ge=1, le=5)] = None,
) -> Json:
    try:
        q = SessionQuery(session=session_name, year_from=year_from, year_to=year_to, dow=dow)
    except ValueError as e:
        raise HTTPException(422, "Năm bắt đầu phải trước hoặc bằng năm kết thúc") from e
    return await monitor.session_query(session, q.session, q.year_from, q.year_to, q.dow)


@router.post("/crawl-days/{day}/retry", response_model=RetryResult)
async def retry_day(day: date, session: DB) -> RetryResult:
    if day < settings.history_start_date:
        raise HTTPException(404, "Ngày nằm ngoài kho")
    queued = await monitor.retry_day(session, day)
    if not queued:
        raise HTTPException(404, "Ngày này không ở trạng thái lỗi")
    return RetryResult(day=day.isoformat(), queued=True)


@router.post("/history/wake")
async def wake(session: DB) -> Json:
    await monitor.wake_history(session)
    return {"ok": True}
