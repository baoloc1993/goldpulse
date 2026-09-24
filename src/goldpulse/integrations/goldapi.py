"""Poll giá XAU realtime từ gold-api.com. Không ghi DB."""

from dataclasses import dataclass
from datetime import datetime

import httpx

URL = "https://api.gold-api.com/price/XAU"


@dataclass(frozen=True)
class Quote:
    price: float
    updated_at: datetime  # giờ nguồn cập nhật (UTC) — độ trễ tính theo mốc này


class GoldApiError(Exception):
    pass


class GoldApiClient:
    def __init__(self, timeout: float = 8.0) -> None:
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch(self) -> Quote:
        try:
            resp = await self._client.get(URL)
            resp.raise_for_status()
            data = resp.json()
            return Quote(
                price=float(data["price"]),
                updated_at=datetime.fromisoformat(data["updatedAt"].replace("Z", "+00:00")),
            )
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
            raise GoldApiError(f"{type(e).__name__}: {e}") from e
