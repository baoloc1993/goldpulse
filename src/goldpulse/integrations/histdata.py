"""Tải + giải mã nến 1 phút XAUUSD của HistData.com — tới thẳng, không cần WARP/proxy. Không ghi DB.

Đo thật 2026-09-25:
- Có từ 15/03/2009. Năm đã qua: một file/năm; năm hiện tại: một file/tháng, trễ ~1 tuần.
- Tải: GET trang (lấy token `tk` trong form) → POST get.php → file zip chứa CSV.
- CSV `YYYYMMDD HHMMSS;open;high;low;close;0` — giờ New York CÓ đổi mùa hè (khớp Dukascopy từng phút
  khi quy đổi bằng America/New_York; lệch đúng 1 giờ nếu coi là EST cố định như tài liệu HistData ghi).
- Chỉ liệt kê phút có tick → mỗi dòng là một phút có giao dịch thật; volume luôn 0 nên đặt 1.0 làm dấu.
"""

import io
import re
import ssl
import zipfile
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx

from .dukascopy import BadFile, RawCandle, Throttled, Unreachable

BASE = "https://www.histdata.com"
FIRST_DAY = date(2009, 3, 15)
NY = ZoneInfo("America/New_York")
_INPUT = re.compile(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"')


class FileNotAvailable(Exception):
    """HistData không có file cho khoảng này (trước 2009-03 hoặc tháng chưa phát hành)."""


def file_key(ny_day: date, today: date) -> str:
    """Khoá file chứa ngày New York `ny_day`: năm đã qua → 'YYYY', năm hiện tại → 'YYYY/M'."""
    if ny_day.year < today.year:
        return f"{ny_day.year}"
    return f"{ny_day.year}/{ny_day.month}"


def parse_csv(text: str) -> list[RawCandle]:
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            stamp, o, h, lo, c, _ = line.split(";")
            local = datetime.strptime(stamp, "%Y%m%d %H%M%S").replace(tzinfo=NY)
        except ValueError as e:
            raise BadFile(f"Dòng CSV HistData không đọc được: {line[:60]!r}") from e
        out.append(
            RawCandle(
                ts=local.astimezone(UTC),
                open=round(float(o), 3),
                high=round(float(h), 3),
                low=round(float(lo), 3),
                close=round(float(c), 3),
                volume=1.0,  # HistData chỉ ghi phút có tick
            )
        )
    return out


def unzip_csv(payload: bytes) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(payload))
        name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        return zf.read(name).decode("ascii")
    except (zipfile.BadZipFile, StopIteration, UnicodeDecodeError) as e:
        raise BadFile(f"File HistData hỏng: {e}") from e


class HistDataClient:
    def __init__(self, timeout: float = 90.0) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
                )
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch_file(self, key: str) -> list[RawCandle]:
        """Tải một file ('2012' hoặc '2026/9'), trả nến đã quy về UTC, theo thứ tự thời gian."""
        page = f"{BASE}/download-free-forex-historical-data/?/ascii/1-minute-bar-quotes/xauusd/{key}"
        try:
            html = await self._client.get(page)
            if html.status_code in (429, 502, 503, 504):
                raise Throttled(f"HTTP {html.status_code}")
            form = dict(_INPUT.findall(html.text))
            if not form.get("tk"):
                raise FileNotAvailable(f"HistData không có file XAUUSD {key}")
            resp = await self._client.post(f"{BASE}/get.php", data=form, headers={"Referer": page})
        except (httpx.ConnectError, httpx.ConnectTimeout, ssl.SSLError) as e:
            raise Unreachable(f"{type(e).__name__}: {e}") from e
        except httpx.TransportError as e:
            raise Throttled(type(e).__name__) from e
        if resp.status_code in (429, 502, 503, 504):
            raise Throttled(f"HTTP {resp.status_code}")
        if resp.status_code != 200 or resp.content[:2] != b"PK":
            raise BadFile(f"HistData trả HTTP {resp.status_code}, không phải file zip")
        return sorted(parse_csv(unzip_csv(resp.content)), key=lambda c: c.ts)
