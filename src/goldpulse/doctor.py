"""`make doctor`: kiểm biến môi trường + kết nối DB / gold-api / Dukascopy + (v2) FRED, não Claude — in cái gì thiếu."""

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

    if settings.btc_on:  # (v4) nguồn nến BTC — Binance công khai, không key
        from .integrations.binance import BinanceClient

        bn = BinanceClient(settings.binance_base_url)
        try:
            closed, live, server_now = await bn.klines("BTCUSDT", limit=3)
            lag = (server_now - (closed[-1].ts + timedelta(minutes=1))).total_seconds() if closed else None
            line(True, f"Binance: BTCUSDT {live.close if live else closed[-1].close:,.2f} (nến đóng trễ {lag:.0f}s)")
        except Exception as e:  # noqa: BLE001
            good &= line(False, f"Binance: {e} — thử BINANCE_BASE_URL khác (api1.binance.com…)")
        finally:
            await bn.close()
    if settings.btc_on and not settings.gold_on:
        return 0 if good else 1  # instance chỉ BTC: không cần HistData/Dukascopy/FRED/não

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

    # --- (v2) FRED + não bot ---
    from .integrations.fred import FredClient

    fred = FredClient()
    try:
        pts = await fred.target_rates((now - timedelta(days=30)).date())
        line(True, f"FRED: lãi suất mục tiêu {pts[-1].lower:.2f}–{pts[-1].upper:.2f}% (ngày {pts[-1].day:%d/%m/%Y})")
    except Exception as e:  # noqa: BLE001
        good &= line(False, f"FRED: {e} — S7 dùng bảng đã nạp")
    finally:
        await fred.close()
    good &= await check_brain(settings)

    print("\nỔn." if good else "\nCó mục ✗ ở trên.")
    return 0 if good else 1


async def check_brain(settings) -> bool:
    """Chỉ kiểm cấu hình + đăng nhập, KHÔNG hỏi Claude thật (không tốn credit)."""
    from .integrations.claude_cli import child_env

    backend = settings.brain_backend
    if backend == "fake":
        return line(not settings.is_production, "BRAIN_BACKEND=fake (chỉ cho test/dogfood — cấm ở production)")
    if backend == "api":
        return line(
            bool(settings.anthropic_api_key),
            f"BRAIN_BACKEND=api · model {settings.claude_api_model} · có ANTHROPIC_API_KEY",
        )
    try:
        proc = await asyncio.create_subprocess_exec(
            settings.claude_cli_bin, "auth", "status", env=child_env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        out, _ = await asyncio.wait_for(proc.communicate(), 30)
    except FileNotFoundError:
        return line(False, f"BRAIN_BACKEND=cli nhưng không có lệnh `{settings.claude_cli_bin}` — cài Claude Code")
    except TimeoutError:
        return line(False, "BRAIN_BACKEND=cli: `claude auth status` treo quá 30 giây")
    text = out.decode("utf-8", "replace").strip()
    ok = proc.returncode == 0 and '"loggedIn": false' not in text
    return line(
        ok,
        f"BRAIN_BACKEND=cli · model {settings.claude_cli_model} · "
        + (
            "Claude Code đã đăng nhập"
            if ok
            else f"Claude Code CHƯA đăng nhập — chạy `claude` rồi /login ({text[:120]})"
        ),
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
