"""Mức lãi suất mục tiêu của FED theo ngày từ FRED (CSV công khai, không cần key). Không ghi DB.

DFEDTAR: mục tiêu đơn 1982 → 15/12/2008 · DFEDTARU / DFEDTARL: trần / sàn từ 16/12/2008.
"""

import csv
import io
from dataclasses import dataclass
from datetime import date

import httpx

URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
UA = "Mozilla/5.0 (GoldPulse worker)"


class FredError(Exception):
    pass


@dataclass(frozen=True)
class RatePoint:
    day: date
    lower: float
    upper: float


class FredClient:
    def __init__(self, timeout: float = 30.0) -> None:
        self._client = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": UA}, follow_redirects=True)

    async def close(self) -> None:
        await self._client.aclose()

    async def _series(self, series: str) -> dict[date, float]:
        try:
            resp = await self._client.get(URL.format(series=series))
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise FredError(f"{series}: {type(e).__name__}: {e}") from e
        out: dict[date, float] = {}
        reader = csv.reader(io.StringIO(resp.text))
        header = next(reader, None)
        if not header or header[0] != "observation_date":
            raise FredError(f"{series}: định dạng CSV lạ ({header})")
        for row in reader:
            if len(row) < 2 or row[1] in ("", "."):
                continue
            try:
                out[date.fromisoformat(row[0])] = float(row[1])
            except ValueError:
                continue
        if not out:
            raise FredError(f"{series}: không có điểm dữ liệu nào")
        return out

    async def target_rates(self, since: date) -> list[RatePoint]:
        """Chuỗi (ngày, sàn, trần) từ `since` tới điểm mới nhất FRED có — ghép hai giai đoạn."""
        single = await self._series("DFEDTAR")
        upper = await self._series("DFEDTARU")
        lower = await self._series("DFEDTARL")
        points = [RatePoint(d, v, v) for d, v in single.items() if d >= since]
        points += [RatePoint(d, lower.get(d, u), u) for d, u in upper.items() if d >= since]
        points.sort(key=lambda p: p.day)
        return points
