"""Gói dữ liệu cố định cho não (ARCHITECTURE §6 "Gói dữ liệu quá dài", §7 E4/F3).

Não KHÔNG có tool: mọi con số nó được nói ra đều nằm trong gói này. Không nhét nến phút thô.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..repositories import candles as candle_repo
from ..repositories import chat as chat_repo
from ..repositories import feed as feed_repo
from . import fed, model15m, predictions, signals
from .calendar import SESSIONS, VN, active_sessions, is_market_open, session_window, sessions_touching
from .monitor import LAG_LIMIT_S

WEEKDAYS = ["thứ Hai", "thứ Ba", "thứ Tư", "thứ Năm", "thứ Sáu", "thứ Bảy", "Chủ nhật"]
HISTORY_MESSAGES = 20
HISTORY_CHARS = 800
PRICE_NOTE = "Giá realtime từ gold-api.com (trung bình ~1,2 USD cao hơn giá Bid chính thức); nến lịch sử là Bid."


def next_open(now: datetime) -> datetime | None:
    """Phút mở cửa kế tiếp (tối đa 3 ngày tới) — dùng để nói 'thị trường mở lại lúc …'."""
    t = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(3 * 24 * 4):
        if is_market_open(t):
            return t
        t += timedelta(minutes=15)
        t = t.replace(minute=t.minute - t.minute % 15)
    return None


async def live_state(session: AsyncSession, now: datetime) -> dict[str, Any]:
    """Giá + độ trễ + thị trường mở + được phép gợi ý/ghi dự đoán không (AC-9)."""
    sample = await feed_repo.latest_price_sample(session)
    lag = (now - sample["source_ts"]).total_seconds() if sample and sample["source_ts"] else None
    fresh = lag is not None and lag < LAG_LIMIT_S
    market_open = is_market_open(now)
    if not market_open:
        reason = "market_closed"
    elif not fresh:
        reason = "price_stale"
    else:
        reason = None
    nxt = next_open(now) if not market_open else None
    src = sample["source_ts"] if sample else None
    return {
        "price": round(sample["price"], 2) if sample else None,
        "price_time_vn": src.astimezone(VN).strftime("%H:%M %d/%m") if src else None,
        "lag_seconds": round(lag) if lag is not None else None,
        "fresh": fresh,
        "market_open": market_open,
        "can_suggest": reason is None,
        "no_suggest_reason": reason,
        "next_open_vn": nxt.astimezone(VN).strftime("%H:%M %d/%m") if nxt else None,
        "lag_limit": LAG_LIMIT_S,
    }


def _session_targets(now: datetime) -> list[tuple[str, datetime]]:
    """Phiên đang chạy; không có phiên nào (đóng cửa) → phiên mở kế tiếp. Trả (phiên, giờ mở UTC)."""
    act = [(k, session_window(k, d)[0]) for k, d, _ in active_sessions(now)]
    if act:
        return act
    upcoming = sorted(
        ((session_window(k, d)[0], k) for k, d in sessions_touching(now, now + timedelta(days=3))),
        key=lambda x: x[0],
    )
    for o, k in upcoming:
        if o > now and is_market_open(o):
            return [(k, o)]
    return []


async def session_stats(session: AsyncSession, now: datetime) -> list[dict[str, Any]]:
    """Thống kê phiên trên toàn kho (2004 → nay) + riêng đúng thứ của phiên đó."""
    first, last = await candle_repo.data_years(session)
    if first is None or last is None:
        return []
    out = []
    for key, opened in _session_targets(now):
        local_day = opened.astimezone(SESSIONS[key].tz).date()
        dow = local_day.isoweekday()
        entry: dict[str, Any] = {
            "session": key,
            "label": SESSIONS[key].label,
            "running": opened <= now,
            "opens_vn": opened.astimezone(VN).strftime("%H:%M %d/%m"),
            "weekday": WEEKDAYS[dow - 1],
        }
        for tag, isodow in (("all_days", None), ("same_weekday", dow if dow <= 5 else None)):
            if tag == "same_weekday" and isodow is None:
                continue
            s = await candle_repo.session_stats(session, key, first, last, isodow)
            n = s["n"] or 0
            entry[tag] = {
                "sessions": n,
                "years": f"{s['first_day']:%Y}–{s['last_day']:%Y}" if s["first_day"] else None,
                "avg_range_usd": round(s["avg_range"], 2) if s["avg_range"] is not None else None,
                "up_pct": round(100 * s["up"] / n, 1) if n else None,
                "down_pct": round(100 * s["down"] / n, 1) if n else None,
                "flat_pct": round(100 * s["flat"] / n, 1) if n else None,
            }
        out.append(entry)
    return out


async def _model_view(session: AsyncSession, bar_end: datetime) -> dict[str, Any] | None:
    mdl = await model15m.evaluate(session, bar_end)
    if not mdl:
        return None
    md = model15m.model()
    return {
        "p_up": mdl["p_up"],
        "direction": mdl["direction"],
        "confident": mdl["trigger"],
        "note": (
            "Mô hình logistic trên 23 đặc trưng giá, nến 15m vừa đóng. Chỉ đáng tin khi `confident` = true "
            f"(2% nến tự tin nhất): kiểm 2025–2026 đúng {md['metrics']['test_2025_2026']['accuracy_pct']}% trên "
            f"{md['metrics']['test_2025_2026']['n']} lần. Khi `confident` = false thì gần như tung đồng xu (~51%)."
        ),
    }


async def market_pack(session: AsyncSession, now: datetime) -> dict[str, Any]:
    """Phần dùng chung cho chat + dự đoán tự động."""
    live = await live_state(session, now)
    bar_end = signals.bar_boundary(now)
    sig = await signals.compute(session, bar_end)
    ctx = await signals.context(session, bar_end, live["price"] or sig["last_close"])
    acc = await predictions.summary(session)
    return {
        "now_vn": f"{WEEKDAYS[now.astimezone(VN).weekday()]} {now.astimezone(VN):%d/%m/%Y %H:%M}",
        "timezone": "Giờ Việt Nam (UTC+7)",
        "live": {**live, "note": PRICE_NOTE},
        "signals_15m": {
            **{k: v for k, v in sig.items() if k != "recent"},
            "computed_on_bars_until_vn": sig["recent"][-1]["vn"] if sig["recent"] else None,
            "rules": "bollinger %b<0→lên, >100→xuống · streak ≥6 nến giảm→lên, ≥6 tăng→xuống · rsi9 <20→lên, >80→xuống",
            "backtest": "Mỗi tín hiệu 53–57% đúng hướng nến 15m kế tiếp (2012–2026); chiều 'xuống' chỉ ~51% từ 2022. "
            "Không tín hiệu: ~50% (tung đồng xu).",
        },
        "bars_15m_recent": sig["recent"],
        "model_15m": await _model_view(session, bar_end),
        "context_no_predictive_power": {
            **ctx,
            "note": "Vị trí trong biên 5/20/60 ngày và RSI 14 khung 1h/4h/1D KHÔNG đoán được 15 phút kế tiếp"
            " (backtest).",
        },
        "sessions": await session_stats(session, now),
        "fed": await fed.pack(session, now),
        "bot_accuracy": {
            k: acc[k] for k in ("rate", "scored", "correct", "wrong", "flat", "unscored", "pending", "verdict")
        },
    }


async def build_chat(session: AsyncSession, thread_id: int, answer_id: int, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    pack = await market_pack(session, now)
    # Mỗi tin cắt 800 ký tự: 20 × 2000 ký tự sẽ vượt trần ~12k token của gói (ARCHITECTURE §6)
    history = [
        {"role": m["role"], "content": m["content"][:HISTORY_CHARS]}
        for m in await chat_repo.messages_before(session, thread_id, answer_id, HISTORY_MESSAGES)
    ]
    pack["conversation"] = history
    return pack


async def build_prediction(session: AsyncSession, job_payload: dict[str, Any], now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    pack = await market_pack(session, now)
    pack.pop("sessions", None)  # dự đoán 15 phút không cần thống kê phiên 20 năm — gói gọn hơn
    pack["trigger"] = {"bar_end": job_payload.get("bar_ts"), "signals_at_trigger": job_payload.get("signals")}
    return pack
