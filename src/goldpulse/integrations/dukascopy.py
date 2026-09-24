"""Tải + giải mã file nến 1 phút Dukascopy (.bi5). Không ghi DB.

URL: datafeed.dukascopy.com/datafeed/XAUUSD/YYYY/<tháng-1, 2 số>/DD/BID_candles_min_1.bi5
Nội dung: LZMA; mỗi nến 24 byte big-endian — giây lệch từ 00:00 UTC, open, close, low, high
(int32, ÷1000), volume (float32).
"""

import asyncio
import lzma
import ssl
import struct
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

import httpx

BASE = "https://datafeed.dukascopy.com/datafeed/XAUUSD"
RECORD = struct.Struct(">5if")
POINT = 1000.0


@dataclass(frozen=True)
class RawCandle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class Unreachable(Exception):
    """Không tới được máy chủ (DNS/TCP/TLS) — thường do WARP tắt. Không tính vào attempts."""


class Throttled(Exception):
    """Dukascopy trả 429/5xx hoặc treo — lỗi chập chờn (cùng một file lúc 503 lúc 200). Thử lại có giãn cách."""


class DayNotFound(Exception):
    """File của ngày không tồn tại (404)."""


class BadFile(Exception):
    """File tải về nhưng giải nén / giải mã hỏng."""


def day_url(day: date) -> str:
    return f"{BASE}/{day.year:04d}/{day.month - 1:02d}/{day.day:02d}/BID_candles_min_1.bi5"


def decode(day: date, payload: bytes) -> list[RawCandle]:
    """Giải mã nội dung .bi5 của một ngày. Rỗng → []."""
    if not payload:
        return []
    try:
        raw = lzma.decompress(payload)
    except lzma.LZMAError as e:
        raise BadFile(f"File hỏng khi giải nén: {e}") from e
    if len(raw) % RECORD.size:
        raise BadFile(f"Độ dài file lẻ ({len(raw)} byte)")
    base = datetime.combine(day, time(0), tzinfo=UTC)
    out = []
    for secs, o, c, lo, hi, vol in RECORD.iter_unpack(raw):
        out.append(
            RawCandle(
                ts=base + timedelta(seconds=secs),
                open=round(o / POINT, 3),
                high=round(hi / POINT, 3),
                low=round(lo / POINT, 3),
                close=round(c / POINT, 3),
                volume=float(vol),
            )
        )
    return out


class DukascopyClient:
    def __init__(self, proxy: str | None = None, timeout: float = 90.0) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout,
            proxy=proxy,
            # Dukascopy trả 429 cho User-Agent không phải trình duyệt (kiểm thật 2026-09-24)
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
                )
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch_day(self, day: date) -> list[RawCandle]:
        url = day_url(day)
        try:
            resp = await self._client.get(url)
        except (httpx.ConnectError, httpx.ConnectTimeout, ssl.SSLError) as e:
            raise Unreachable(f"{type(e).__name__}: {e}") from e
        except httpx.TransportError as e:
            # Đọc treo / đứt giữa chừng: Dukascopy đang quá tải, không phải mất mạng (đo thật 2026-09-24)
            raise Throttled(f"{type(e).__name__}") from e
        if resp.status_code in (429, 502, 503, 504):
            raise Throttled(f"HTTP {resp.status_code}")
        if resp.status_code == 404:
            raise DayNotFound("Lỗi 404 — Dukascopy không có file ngày này")
        if resp.status_code != 200:
            raise BadFile(f"HTTP {resp.status_code}")
        # Giải nén LZMA tốn CPU → threadpool, không chặn event loop
        return await asyncio.to_thread(decode, day, resp.content)
