"""`make doctor`: kiểm biến môi trường + kết nối DB / gold-api / Dukascopy, in cái gì thiếu."""

import asyncio
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "deployment" / ".env.example"
LOCAL = ROOT / "deployment" / "local" / ".env"
REQUIRED = {"DATABASE_URL", "OPS_ACCESS_KEY"}


def line(ok: bool, msg: str) -> bool:
    print(f"  {'✓' if ok else '✗'} {msg}")
    return ok


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    good = True
    print("GoldPulse doctor\n")
    names = set(re.findall(r"^#?\s*([A-Z_]+)=", EXAMPLE.read_text(encoding="utf-8"), re.M))
    if not LOCAL.exists():
        line(False, "Thiếu deployment/local/.env — copy từ deployment/.env.example rồi điền")
        return 1
    have = dict(
        (k.strip(), v.strip())
        for k, _, v in (ln.partition("=") for ln in LOCAL.read_text(encoding="utf-8").splitlines())
        if k.strip() and not k.startswith("#")
    )
    for n in sorted(REQUIRED):
        good &= line(bool(have.get(n)), f"biến {n}")
    unknown = set(have) - names
    if unknown:
        line(False, f"biến chưa khai ở .env.example: {', '.join(sorted(unknown))}")
        good = False
    if not good:
        return 1

    from .config import settings
    from .db import engine, session_scope
    from .integrations.dukascopy import DukascopyClient
    from .integrations.goldapi import GoldApiClient
    from .services import monitor

    try:
        async with session_scope() as s:
            alive = await monitor.db_alive(s)
        good &= line(alive, "Postgres kết nối được")
    except Exception as e:  # noqa: BLE001 — doctor chỉ báo, không ném
        good &= line(False, f"Postgres: {e}")
    finally:
        await engine.dispose()

    gold = GoldApiClient()
    try:
        q = await gold.fetch()
        lag = (datetime.now(UTC) - q.updated_at).total_seconds()
        line(True, f"gold-api.com: {q.price:,.2f} (trễ {lag:.0f}s)")
    except Exception as e:  # noqa: BLE001
        good &= line(False, f"gold-api.com: {e}")
    finally:
        await gold.close()

    duka = DukascopyClient(proxy=settings.dukascopy_proxy)
    day = (datetime.now(UTC) - timedelta(days=7)).date()
    try:
        candles = await duka.fetch_day(day)
        line(True, f"Dukascopy: {day} có {len(candles)} nến")
    except Exception as e:  # noqa: BLE001
        good &= line(False, f"Dukascopy ({type(e).__name__}): {e} — WARP đã bật chưa?")
    finally:
        await duka.close()

    print("\nỔn." if good else "\nCó mục ✗ ở trên.")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
