"""Tài khoản giấy (v3): 4 luật vào lệnh · mở theo dự đoán tự động · đóng/cháy mỗi phút · sổ ngày · số liệu cho web.

Tiền GIẢ — mọi thứ là hàng trong Postgres, module này không
import `integrations/` và không có cấu hình sàn nào. Giá vào = `prediction.price_at`; giá ra = cùng quy tắc chấm dự
đoán (`predictions.price_at(due_at)`); không mô phỏng trượt giá ngoài spread.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from ..repositories import candles as candle_repo
from ..repositories import feed as feed_repo
from ..repositories import paper as repo
from . import predictions
from .calendar import NY, VN, is_market_open

log = structlog.get_logger("paper")
STALE_S = 300  # dự đoán quá 5 phút tuổi (worker vắng) → không vào giá cũ
GRACE_S = predictions.SCORE_GRACE_S  # nến phút cuối trước hạn đã ghi
# Kiểm cháy chỉ bằng nến tươi (nguồn chết thì chờ, không cháy ảo). ts là ĐẦU phút, nến phút trước ghi sau vài
# giây → 2 phút để hở điểm mù đầu phút (bug-hunter v3)
MTM_MAX_AGE = timedelta(minutes=3)
CURVE_DAYS = 20
STATUS_KEY = "paper"
LIVE_KEYS = ("price", "lag_seconds", "fresh", "market_open", "price_time_vn", "lag_limit", "next_open_vn")
# Ghi trước khi nhìn số
GO_MIN_TRADES = 40
GO_MAX_BURN_RATIO = 0.20


def trade_date_of(ts: datetime) -> date:
    """Ngày giao dịch NY: 18:00 hôm trước → 17:00 hôm nay = ngày NY của giờ đóng."""
    return (ts.astimezone(NY) + timedelta(hours=6)).date()


def rule_allows(rule: dict[str, Any], signals: dict[str, Any]) -> bool:
    kind = rule.get("kind", "all")
    if kind == "volatility":
        vol = signals.get("vol96")
        return vol is not None and float(vol) >= float(rule["min_vol"])
    if kind == "confidence":
        p = (signals.get("model") or {}).get("p_up")
        return p is not None and abs(float(p) - 0.5) >= float(rule["min_conf"])
    return True


def pnl_of(acc: dict[str, Any], direction: str, entry: float, exit_price: float) -> float:
    move = (exit_price - entry) if direction == "up" else (entry - exit_price)
    return round(acc["oz"] * (move - acc["spread"]), 4)


async def _equity_today(session: AsyncSession, acc: dict[str, Any], day: date) -> tuple[float, bool]:
    d = await repo.day_get(session, acc["code"], day)
    if not d:
        return float(acc["start_equity"]), False
    return float(d["start_equity"]) + float(d["realized_pnl"]), bool(d["burned"])


async def _decide(session: AsyncSession, acc: dict[str, Any], p: dict[str, Any], now: datetime) -> str | None:
    """None = mở lệnh; chuỗi = lý do bỏ qua (thứ tự: luật → tài khoản → giờ → tuổi dự đoán)."""
    if not rule_allows(acc["rule"], p["signals"] or {}):
        return "rule"
    if not acc["enabled"]:
        return "disabled"
    if await repo.open_trade_of(session, acc["code"]):
        return "open_trade"
    equity, burned = await _equity_today(session, acc, trade_date_of(p["made_at"]))
    if burned:
        return "burned"
    if equity < p["price_at"] / acc["leverage"]:
        return "margin"
    horizon = int(acc["rule"].get("horizon_minutes", 15))
    # Thị trường phải mở SUỐT thời gian giữ + 5 phút chấm (không chỉ hai đầu — luật D 60 phút; bug-hunter v3)
    if not all(is_market_open(p["made_at"] + timedelta(minutes=m)) for m in range(0, horizon + 6, 5)):
        return "market_closing"
    if (now - p["made_at"]).total_seconds() > STALE_S:
        return "stale"
    return None


async def open_for_new(session: AsyncSession, now: datetime | None = None) -> int:
    """Luồng I: mọi prediction(auto) mới kể từ lần xử trước → mỗi tài khoản một dòng paper_trade (open hoặc skipped).
    Trả số lệnh mở."""
    now = now or datetime.now(UTC)
    st = await feed_repo.get_status(session, STATUS_KEY)
    last_id = int((st or {}).get("last_prediction_id") or 0)
    preds = await repo.new_auto_predictions(session, last_id)
    if not preds:
        return 0
    accounts = await repo.accounts(session)
    opened = 0
    for p in preds:
        try:
            async with session.begin_nested():  # một dự đoán hỏng (dữ liệu lạ) không kẹt cả hàng — bỏ qua, đi tiếp
                opened += await _open_one(session, accounts, p, now)
        except Exception:
            log.exception("paper_open_failed", prediction_id=p["id"])
        last_id = p["id"]
    await feed_repo.set_status(session, STATUS_KEY, {"last_prediction_id": last_id, "at": now.isoformat()})
    return opened


async def _open_one(session: AsyncSession, accounts: list[dict[str, Any]], p: dict[str, Any], now: datetime) -> int:
    opened = 0
    day = trade_date_of(p["made_at"])
    for acc in accounts:
        reason = await _decide(session, acc, p, now)
        horizon = int(acc["rule"].get("horizon_minutes", 15))
        sig = p["signals"] or {}
        row = {
            "account_code": acc["code"],
            "prediction_id": p["id"],
            "trade_date": day,
            "opened_at": p["made_at"],
            "direction": p["direction"],
            "oz": acc["oz"],
            "entry_price": p["price_at"],
            "spread": acc["spread"],
            "due_at": p["made_at"] + timedelta(minutes=horizon),
            "status": "skipped" if reason else "open",
            "skip_reason": reason,
            "context": {
                "p_up": (sig.get("model") or {}).get("p_up"),
                "vol96": sig.get("vol96"),
                "fired": [f.get("name") for f in (sig.get("fired") or []) if isinstance(f, dict)],
            },
        }
        if reason is None:
            await repo.day_ensure(session, acc["code"], day, float(acc["start_equity"]))
        if await repo.insert_trade(session, row) and reason is None:
            opened += 1
    return opened


async def _close(
    session: AsyncSession, acc: dict[str, Any], t: dict[str, Any], price: float, reason: str, at: datetime
) -> bool:
    # Lỗ một lệnh không vượt số dư còn lại của ngày (như sàn có bảo vệ số dư âm): giá giật qua ngưỡng giữa hai nhịp
    # kiểm cháy / worker vừa sống lại không được ghi −15, −20 USD cho tài khoản 10 USD
    equity, _ = await _equity_today(session, acc, t["trade_date"])
    pnl = max(pnl_of(acc, t["direction"], t["entry_price"], price), -round(equity, 4))
    burned = reason == "stopout" or equity + pnl <= 0
    if not await repo.close_trade(session, t["id"], exit_price=price, reason=reason, pnl=pnl, closed_at=at):
        return False
    await repo.day_ensure(session, acc["code"], t["trade_date"], float(acc["start_equity"]))
    await repo.day_apply(session, acc["code"], t["trade_date"], pnl, burned)
    return True


async def close_due(session: AsyncSession, now: datetime | None = None) -> int:
    """Luồng J: tới hạn → đóng ở giá `price_due`; chưa tới hạn → kiểm cháy bằng nến tươi, thị trường đóng → đóng."""
    now = now or datetime.now(UTC)
    accounts = {a["code"]: a for a in await repo.accounts(session)}
    closed = 0
    for t in await repo.open_trades(session):
        acc = accounts[t["account_code"]]
        if now >= t["due_at"] + timedelta(seconds=GRACE_S):
            p1 = await predictions.price_at(session, t["due_at"])
            if p1 is not None:
                closed += await _close(session, acc, t, p1, "horizon", t["due_at"])
            else:  # không có nến trong 5 phút trước hạn (đóng cửa / worker chết) → nến cuối có
                floor = t["due_at"].replace(second=0, microsecond=0)
                c = await candle_repo.close_before(session, floor, floor - timedelta(days=3))
                if c:
                    closed += await _close(session, acc, t, float(c["close"]), "market_close", t["due_at"])
                else:
                    log.warning("paper_no_candle_to_close", trade_id=t["id"], due_at=t["due_at"].isoformat())
            continue
        if now >= t["due_at"]:
            continue  # đã tới hạn, chờ nến trước hạn được ghi (nhánh trên) — không cháy/đóng bằng giá sau hạn
        c = await repo.latest_close(session, now - MTM_MAX_AGE, now)
        if not c:
            continue  # nguồn giá đứng — không cháy ảo, không đóng
        if not is_market_open(now):
            closed += await _close(session, acc, t, float(c["close"]), "market_close", now)
            continue
        equity, _ = await _equity_today(session, acc, t["trade_date"])
        if equity + pnl_of(acc, t["direction"], t["entry_price"], float(c["close"])) <= 0:
            closed += await _close(session, acc, t, float(c["close"]), "stopout", now)
    return closed


async def tick(session: AsyncSession, now: datetime | None = None) -> tuple[int, int]:
    """Một nhịp (test/seed): đóng trước (giải phóng chỗ), rồi mở. Worker gọi hai hàm ở hai giao dịch riêng."""
    closed = await close_due(session, now)
    opened = await open_for_new(session, now)
    return opened, closed


def verdict(t: dict[str, Any]) -> str:
    """Phán quyết trên lệnh đã đóng trừ `market_close`: go · pivot (lãi nhưng chưa đủ 40) · none."""
    if t["eval_pnl"] <= 0:
        return "none"
    burn_ok = t["days"] == 0 or t["burned_days"] / t["days"] <= GO_MAX_BURN_RATIO
    # ≥ 40 lệnh + lãi nhưng cháy > 20% ngày: luật chưa nói rõ → PIVOT (chạy tiếp, không GO)
    return "go" if t["eval_n"] >= GO_MIN_TRADES and burn_ok else "pivot"


def _trade_view(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": t["id"],
        "prediction_id": t["prediction_id"],
        "opened_vn": t["opened_at"].astimezone(VN).strftime("%d/%m %H:%M"),
        "due_vn": t["due_at"].astimezone(VN).strftime("%H:%M"),
        "closed_vn": t["closed_at"].astimezone(VN).strftime("%H:%M") if t["closed_at"] else None,
        "direction": t["direction"],
        "entry_price": t["entry_price"],
        "exit_price": t["exit_price"],
        "exit_reason": t["exit_reason"],
        "pnl": t["pnl"],
        "status": t["status"],
        "skip_reason": t["skip_reason"],
        "context": t["context"] or {},
        "spread": t["spread"],
        "oz": t["oz"],
    }


async def account_view(session: AsyncSession, acc: dict[str, Any], now: datetime, mark: float | None) -> dict[str, Any]:
    today = trade_date_of(now)
    day = await repo.day_get(session, acc["code"], today)
    start = float(acc["start_equity"])
    realized = float(day["realized_pnl"]) if day else 0.0
    burned = bool(day and day["burned"])
    open_t = await repo.open_trade_of(session, acc["code"])
    unrealized = None
    if open_t and mark is not None:
        unrealized = pnl_of(acc, open_t["direction"], open_t["entry_price"], mark)
    equity = max(0.0, start + realized + (unrealized or 0.0))
    tot = await repo.totals(session, acc["code"])
    return {
        "code": acc["code"],
        "name": acc["name"],
        "rule": acc["rule"],
        "enabled": acc["enabled"],
        "oz": acc["oz"],
        "spread": acc["spread"],
        "leverage": acc["leverage"],
        "start_equity": start,
        "equity": round(equity, 2),
        "today": {
            "pnl": round(realized, 2),
            "trades": int(day["n_trades"]) if day else 0,
            "wins": int(day["n_wins"]) if day else 0,
            "burned": burned,
        },
        "open": {**_trade_view(open_t), "unrealized": unrealized, "mark": mark} if open_t else None,
        "total": {**tot, "verdict": verdict(tot)},
        "curve": [
            {"date": d["trade_date"].isoformat(), "end": float(d["end_equity"]), "burned": bool(d["burned"])}
            for d in await repo.days_recent(session, acc["code"], CURVE_DAYS)
        ],
    }


async def screen(session: AsyncSession, now: datetime | None = None) -> dict[str, Any]:
    """Dữ liệu Tài khoản giấy."""
    from .chat import worker_alive  # chat import predictions → tránh vòng lặp ở mức module
    from .context_pack import live_state

    now = now or datetime.now(UTC)
    live = await live_state(session, now.astimezone(VN))
    c = await repo.latest_close(session, now - timedelta(days=3), now)
    mark = float(c["close"]) if c else None
    accounts = [await account_view(session, a, now, mark) for a in await repo.accounts(session)]
    return {
        "now_vn": now.astimezone(VN).strftime("%H:%M %d/%m"),
        "trade_date": trade_date_of(now).isoformat(),
        "live": {k: live[k] for k in LIVE_KEYS},
        "worker_alive": await worker_alive(session, now),
        "today_pnl": round(
            sum(a["today"]["pnl"] + (a["open"]["unrealized"] or 0.0 if a["open"] else 0.0) for a in accounts), 2
        ),
        "today_trades": sum(a["today"]["trades"] for a in accounts),
        "today_wins": sum(a["today"]["wins"] for a in accounts),
        "accounts": accounts,
        "thresholds": {"go_min_trades": GO_MIN_TRADES, "go_max_burn_ratio": GO_MAX_BURN_RATIO},
    }


async def account_screen(session: AsyncSession, code: str, now: datetime | None = None) -> dict[str, Any] | None:
    """Dữ liệu Nhật ký lệnh."""
    now = now or datetime.now(UTC)
    acc = await repo.get_account(session, code)
    if not acc:
        return None
    c = await repo.latest_close(session, now - timedelta(days=3), now)
    from .context_pack import live_state

    view = await account_view(session, acc, now, float(c["close"]) if c else None)
    live = await live_state(session, now.astimezone(VN))
    view["live"] = {k: live[k] for k in ("market_open", "next_open_vn")}
    view["rows"] = await trades_page(session, code, None)
    return view


async def trades_page(session: AsyncSession, code: str, before_id: int | None, limit: int = 50) -> list[dict[str, Any]]:
    return [_trade_view(t) for t in await repo.trades_page(session, code, before_id, limit)]


async def account_exists(session: AsyncSession, code: str) -> bool:
    return await repo.get_account(session, code) is not None


async def set_enabled(session: AsyncSession, code: str, enabled: bool) -> bool:
    return await repo.set_enabled(session, code, enabled)


async def pack(session: AsyncSession, now: datetime) -> dict[str, Any]:
    """Khối `paper` trong gói dữ liệu cho não — 4 hàng tóm tắt, nói rõ tiền giả."""
    c = await repo.latest_close(session, now - timedelta(days=3), now)
    mark = float(c["close"]) if c else None
    out = []
    for a in await repo.accounts(session):
        v = await account_view(session, a, now, mark)
        out.append(
            {
                "code": v["code"],
                "name": v["name"],
                "enabled": v["enabled"],
                "equity_now_usd": v["equity"],
                "today_pnl_usd": v["today"]["pnl"],
                "today_trades": v["today"]["trades"],
                "total_pnl_usd": round(v["total"]["pnl"], 2),
                "total_trades": v["total"]["trades"],
                "burned_days": f"{v['total']['burned_days']}/{v['total']['days']}",
            }
        )
    return {
        "note": "Tài khoản GIẤY — tiền giả, sổ ghi trong hệ thống, không nối sàn nào. Mỗi tài khoản 10 USD, "
        "bắt đầu lại mỗi ngày giao dịch; mỗi tài khoản một luật vào lệnh theo dự đoán tự động.",
        "accounts": out,
    }


async def record_view(session: AsyncSession, now: datetime | None = None) -> None:
    await repo.bump_view(session, (now or datetime.now(UTC)).astimezone(VN).date().isoformat())
