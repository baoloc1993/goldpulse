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
    from .integrations.histdata import FileNotAvailable, HistDataClient
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

    hd = HistDataClient()
    now = datetime.now(UTC)
    try:
        candles = await hd.fetch_file(f"{now.year}/{now.month}")
        line(True, f"HistData.com: tháng này {len(candles):,} nến, tới {candles[-1].ts:%d/%m %H:%M} UTC")
    except FileNotAvailable:
        line(True, "HistData.com tới được (tháng này chưa phát hành file — bình thường đầu tháng)")
    except Exception as e:  # noqa: BLE001
        good &= line(False, f"HistData.com ({type(e).__name__}): {e} — kiểm tra mạng")
    finally:
        await hd.close()

    # Dukascopy chỉ cần khi nạp lại 2004 → 03/2009 (DECISIONS 2026-09-25) — không làm doctor đỏ
    duka = DukascopyClient(proxy=settings.dukascopy_proxy)
    try:
        await duka.fetch_day((now - timedelta(days=7)).date())
        print("  · Dukascopy tới được (chỉ cần khi nạp lại 2004 → 03/2009)")
    except Exception:  # noqa: BLE001
        print("  · Dukascopy không tới được — bình thường khi WARP tắt; chỉ cần khi nạp lại 2004 → 03/2009")
    finally:
        await duka.close()

    print("\nỔn." if good else "\nCó mục ✗ ở trên.")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
