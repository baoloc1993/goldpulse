"""Dependency: session DB + kiểm khoá truy cập vận hành (PERSONAS §2)."""

import hashlib
import hmac
from collections.abc import AsyncIterator

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import SessionLocal

# Cookie không phân biệt cổng: instance chỉ BTC dùng tên riêng, đăng nhập 8081 không đè cookie 8080 (review v4)
COOKIE = "gp_ops" if settings.gold_on else "gp_ops_btc"
COOKIE_MAX_AGE = 90 * 24 * 3600


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


def session_token() -> str:
    """Giá trị cookie — dẫn xuất từ khoá, không bao giờ lưu khoá thô trong cookie."""
    return hmac.new(settings.ops_access_key.encode(), b"goldpulse-ops-v1", hashlib.sha256).hexdigest()


def key_matches(candidate: str) -> bool:
    return hmac.compare_digest(candidate.strip().encode(), settings.ops_access_key.encode())


def is_operator(request: Request) -> bool:
    return hmac.compare_digest(request.cookies.get(COOKIE, "").encode(), session_token().encode())


class LoginRequired(Exception):
    """Trang HTML không có khoá → chuyển sang S4."""


def require_operator_page(request: Request) -> None:
    if not is_operator(request):
        raise LoginRequired()


def require_operator_api(request: Request) -> None:
    if not is_operator(request):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Cần khoá truy cập")
