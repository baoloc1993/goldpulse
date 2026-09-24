"""/health — công khai, chỉ trả ok/down, không lộ số liệu (PERSONAS §2)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...services import monitor
from ..deps import get_session

router = APIRouter()


@router.get("/health")
async def health(session: Annotated[AsyncSession, Depends(get_session)]) -> JSONResponse:
    ok = await monitor.db_alive(session)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 503)
