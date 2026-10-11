"""(v4) Nến 1 phút từ Binance spot REST công khai — không key, chỉ đọc giá, không ghi DB.

`GET /api/v3/klines?symbol=…&interval=1m` trả mảng [openTime, open, high, low, close, volume, closeTime, …]; tối đa
1000 nến/lần. Nến có closeTime ≥ giờ máy chủ Binance là nến ĐANG chạy — tách riêng, không bao giờ lưu.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

MAX_LIMIT = 1000


@dataclass(frozen=True)
class Kline:
    ts: datetime  # đầu phút, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_ms: int


class BinanceError(Exception):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class BinanceClient:
    def __init__(self, base_url: str = "https://api.binance.com", timeout: float = 10.0) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def klines(
        self, symbol: str, *, start: datetime | None = None, limit: int = 3
    ) -> tuple[list[Kline], Kline | None, datetime]:
        """(nến đã đóng theo thứ tự thời gian, nến đang chạy hoặc None, giờ máy chủ lúc trả).

        `start` = lấy từ phút đó trở đi (lấy bù); không có `start` = `limit` nến mới nhất (poll)."""
        params: dict[str, str | int] = {"symbol": symbol, "interval": "1m", "limit": min(limit, MAX_LIMIT)}
        if start is not None:
            params["startTime"] = _ms(start)
        try:
            resp = await self._client.get(f"{self._base}/api/v3/klines", params=params)
        except httpx.HTTPError as e:
            raise BinanceError(f"{type(e).__name__}: {e}") from e
        if resp.status_code in (418, 429, 451, 502, 503, 504):
            try:
                wait = float(resp.headers.get("Retry-After", ""))
            except ValueError:  # thiếu hoặc kiểu ngày HTTP
                wait = {451: 300.0}.get(resp.status_code, 60.0)
            raise BinanceError(f"{resp.status_code}: Binance chưa trả được (thử lại sau {wait:.0f}s)", wait)
        if resp.status_code != 200:
            raise BinanceError(f"{resp.status_code}: {resp.text[:200]}")
        try:
            rows = resp.json()
            server_now = datetime.now(UTC)
            date_hdr = resp.headers.get("Date")
            if date_hdr:  # giờ máy chủ Binance — nến "đang chạy" xét theo giờ đó, không theo đồng hồ máy nhà
                server_now = parsedate_to_datetime(date_hdr).astimezone(UTC)
            closed: list[Kline] = []
            live: Kline | None = None
            for r in rows:
                k = Kline(
                    ts=datetime.fromtimestamp(int(r[0]) / 1000, UTC),
                    open=float(r[1]),
                    high=float(r[2]),
                    low=float(r[3]),
                    close=float(r[4]),
                    volume=float(r[5]),
                    close_ms=int(r[6]),
                )
                if k.close_ms < _ms(server_now):
                    closed.append(k)
                else:
                    live = k
            return closed, live, server_now
        except (ValueError, TypeError, IndexError, KeyError) as e:
            raise BinanceError(f"định dạng lạ: {type(e).__name__}: {e}") from e
