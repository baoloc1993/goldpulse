"""Dự đoán hướng 15 phút: tạo · chấm · tỉ lệ đúng (ARCHITECTURE §6b, §7 F/G; metric PRD §6).

Chấm: price_due = close nến phút cuối < due_at trong 5 phút; không có → unscored; |Δ| < 0,01 → flat.
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..repositories import candles as candle_repo
from ..repositories import feed as feed_repo
from ..repositories import jobs as job_repo
from ..repositories import predictions as pred_repo
from .calendar import VN

SCORE_GRACE_S = 90  # nến phút cuối trước due_at đã được ghi
PRICE_WINDOW = timedelta(minutes=5)
FLAT_USD = 0.01
GO, KILL = 55.0, 50.0
MIN_SCORED = 100


async def price_at(session: AsyncSession, at: datetime) -> float | None:
    """Close nến phút cuối trước `at` (trong 5 phút) — cùng quy tắc ở hai đầu dự đoán."""
    c = await candle_repo.close_before(session, at, at - PRICE_WINDOW)
    return float(c["close"]) if c else None


async def create(
    session: AsyncSession,
    *,
    made_at: datetime,
    source: str,
    direction: str,
    confidence: str,
    price: float,
    signals: dict[str, Any],
    reasoning: str,
    backend: str,
    model: str | None,
    job_id: int | None,
) -> int:
    horizon = settings.prediction_horizon_minutes
    return await pred_repo.insert(
        session,
        {
            "made_at": made_at,
            "source": source,
            "direction": direction,
            "confidence": confidence,
            "price_at": price,
            "horizon_minutes": horizon,
            "due_at": made_at + timedelta(minutes=horizon),
            "signals": signals,
            "reasoning": reasoning[:500],
            "backend": backend,
            "model": model,
            "job_id": job_id,
        },
    )


def outcome_of(direction: str, p0: float, p1: float | None) -> str:
    if p1 is None:
        return "unscored"
    d = p1 - p0
    if abs(d) < FLAT_USD:
        return "flat"
    return "correct" if (d > 0) == (direction == "up") else "wrong"


async def score_due(session: AsyncSession) -> int:
    """Chấm mọi dự đoán đã tới hạn + 90s. Trả số dự đoán vừa chấm."""
    rows = await pred_repo.due(session, SCORE_GRACE_S)
    for r in rows:
        p1 = await price_at(session, r["due_at"])
        await pred_repo.score(session, r["id"], p1, outcome_of(r["direction"], r["price_at"], p1))
    return len(rows)


def _rate(correct: int, wrong: int) -> float | None:
    return round(100.0 * correct / (correct + wrong), 1) if correct + wrong else None


def verdict(rate: float | None, scored: int) -> str:
    """GO / PIVOT / KILL chỉ khi đủ 100 lần đã chấm (PRD §6); chưa đủ → 'pending'."""
    if rate is None or scored < MIN_SCORED:
        return "pending"
    return "go" if rate >= GO else "pivot" if rate >= KILL else "kill"


async def summary(session: AsyncSession) -> dict[str, Any]:
    rows = (await pred_repo.stats(session))["rows"]

    def agg(pred) -> dict[str, Any]:
        c = sum(r["n"] for r in rows if pred(r) and r["outcome"] == "correct")
        w = sum(r["n"] for r in rows if pred(r) and r["outcome"] == "wrong")
        return {
            "correct": c,
            "wrong": w,
            "scored": c + w,
            "rate": _rate(c, w),
            "flat": sum(r["n"] for r in rows if pred(r) and r["outcome"] == "flat"),
            "unscored": sum(r["n"] for r in rows if pred(r) and r["outcome"] == "unscored"),
            "pending": sum(r["n"] for r in rows if pred(r) and r["outcome"] == "pending"),
            "total": sum(r["n"] for r in rows if pred(r)),
        }

    total = agg(lambda r: True)
    return {
        **total,
        "verdict": verdict(total["rate"], total["scored"]),
        "min_scored": MIN_SCORED,
        "go": GO,
        "kill": KILL,
        "by_source": {s: agg(lambda r, s=s: r["source"] == s) for s in ("auto", "chat")},
        "by_direction": {d: agg(lambda r, d=d: r["direction"] == d) for d in ("up", "down")},
    }


def row_view(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "made_at": r["made_at"].isoformat(),
        "made_vn": r["made_at"].astimezone(VN).strftime("%d/%m %H:%M"),
        "due_vn": r["due_at"].astimezone(VN).strftime("%H:%M"),
        "source": r["source"],
        "direction": r["direction"],
        "confidence": r["confidence"],
        "price_at": r["price_at"],
        "price_due": r["price_due"],
        "outcome": r["outcome"],
        "reasoning": r["reasoning"],
        "signals": r["signals"],
        "backend": r["backend"],
        "model": r["model"],
    }


async def page(session: AsyncSession, before_id: int | None, limit: int = 50) -> list[dict[str, Any]]:
    return [row_view(r) for r in await pred_repo.page(session, before_id, limit)]


async def screen(session: AsyncSession) -> dict[str, Any]:
    """Dữ liệu S6: tỉ lệ + tách nguồn/hướng · tín hiệu gần nhất · 50 hàng đầu · số job dự đoán bỏ qua vì lỗi."""
    from .context_pack import live_state  # context_pack import module này — tránh vòng lặp import

    live = await live_state(session, datetime.now(VN))
    sig = await feed_repo.get_status(session, "signal")
    counts = await job_repo.counts(session, datetime(2000, 1, 1, tzinfo=VN))
    return {
        "summary": await summary(session),
        "signal": {
            "bar_vn": datetime.fromisoformat(sig["bar_end"]).astimezone(VN).strftime("%H:%M %d/%m"),
            "fired": sig.get("fired") or [],
            "pct_b": sig.get("pct_b"),
            "streak": sig.get("streak"),
            "rsi9": sig.get("rsi9"),
            "market_ok": sig.get("market_ok"),
        }
        if sig
        else None,
        "skipped_errors": counts.get("signal_prediction:error", 0),
        "live": {k: live[k] for k in ("can_suggest", "no_suggest_reason", "lag_seconds", "market_open")},
        "rows": await page(session, None),
    }
