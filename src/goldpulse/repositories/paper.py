"""paper_account · paper_trade · paper_day — sổ giao dịch giấy (ARCHITECTURE §6c). Không xoá; web chỉ đổi `enabled`."""

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def accounts(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(text("SELECT * FROM paper_account ORDER BY code"))).mappings()
    return [dict(r) for r in rows]


async def get_account(session: AsyncSession, code: str) -> dict[str, Any] | None:
    row = (await session.execute(text("SELECT * FROM paper_account WHERE code = :c"), {"c": code})).mappings().first()
    return dict(row) if row else None


async def set_enabled(session: AsyncSession, code: str, enabled: bool) -> bool:
    res = await session.execute(
        text("UPDATE paper_account SET enabled = :e, updated_at = now() WHERE code = :c"), {"c": code, "e": enabled}
    )
    return res.rowcount == 1


async def new_auto_predictions(session: AsyncSession, after_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """Dự đoán TỰ ĐỘNG mới (id tăng dần) — bỏ backend fake (dữ liệu test) và chat (Claude tự chọn hướng)."""
    rows = (
        await session.execute(
            text("""
                SELECT id, made_at, direction, price_at, signals, backend FROM prediction
                WHERE id > :a AND source = 'auto' AND backend IS DISTINCT FROM 'fake'
                ORDER BY id LIMIT :n
            """),
            {"a": after_id, "n": limit},
        )
    ).mappings()
    return [dict(r) for r in rows]


async def insert_trade(session: AsyncSession, row: dict[str, Any]) -> int:
    res = await session.execute(
        text("""
            INSERT INTO paper_trade (account_code, prediction_id, trade_date, opened_at, direction, oz, entry_price,
                                     spread, due_at, status, skip_reason, context)
            VALUES (:account_code, :prediction_id, :trade_date, :opened_at, :direction, :oz, :entry_price,
                    :spread, :due_at, :status, :skip_reason, CAST(:context AS jsonb))
            ON CONFLICT (account_code, prediction_id) DO NOTHING
            RETURNING id
        """),
        {**row, "context": json.dumps(row.get("context") or {}, default=str)},
    )
    got = res.scalar()
    return int(got) if got is not None else 0


async def open_trades(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(text("SELECT * FROM paper_trade WHERE status = 'open' ORDER BY id"))).mappings()
    return [dict(r) for r in rows]


async def open_trade_of(session: AsyncSession, code: str) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                text("SELECT * FROM paper_trade WHERE status = 'open' AND account_code = :c"), {"c": code}
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def close_trade(
    session: AsyncSession, trade_id: int, *, exit_price: float, reason: str, pnl: float, closed_at: datetime
) -> bool:
    res = await session.execute(
        text("""
            UPDATE paper_trade SET status = 'closed', exit_price = :p, exit_reason = :r, pnl = :pnl, closed_at = :t
            WHERE id = :id AND status = 'open'
        """),
        {"id": trade_id, "p": exit_price, "r": reason, "pnl": pnl, "t": closed_at},
    )
    return res.rowcount == 1


async def day_get(session: AsyncSession, code: str, day: date) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                text("SELECT * FROM paper_day WHERE account_code = :c AND trade_date = :d"), {"c": code, "d": day}
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def day_ensure(session: AsyncSession, code: str, day: date, start_equity: float) -> None:
    await session.execute(
        text("""
            INSERT INTO paper_day (account_code, trade_date, start_equity, end_equity)
            VALUES (:c, :d, :s, :s) ON CONFLICT (account_code, trade_date) DO NOTHING
        """),
        {"c": code, "d": day, "s": start_equity},
    )


async def day_apply(session: AsyncSession, code: str, day: date, pnl: float, burned: bool) -> None:
    """Cộng một lệnh vừa đóng vào sổ ngày; end_equity = start + realized (không âm)."""
    await session.execute(
        text("""
            UPDATE paper_day SET realized_pnl = realized_pnl + :pnl, n_trades = n_trades + 1,
                n_wins = n_wins + CASE WHEN :pnl > 0 THEN 1 ELSE 0 END,
                burned = burned OR :b,
                end_equity = greatest(0, start_equity + realized_pnl + :pnl), updated_at = now()
            WHERE account_code = :c AND trade_date = :d
        """),
        {"c": code, "d": day, "pnl": pnl, "b": burned},
    )


async def days_recent(session: AsyncSession, code: str, n: int) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text("SELECT * FROM paper_day WHERE account_code = :c ORDER BY trade_date DESC LIMIT :n"),
            {"c": code, "n": n},
        )
    ).mappings()
    return [dict(r) for r in reversed(list(rows))]


async def totals(session: AsyncSession, code: str) -> dict[str, Any]:
    """Tích luỹ mọi ngày + phần dùng cho GO/PIVOT/KILL (lệnh đã đóng, loại `market_close` — PRD §6)."""
    d = (
        (
            await session.execute(
                text("""
                    SELECT count(*) AS days, coalesce(sum(realized_pnl), 0) AS pnl,
                           coalesce(sum(n_trades), 0) AS trades,
                           coalesce(sum(n_wins), 0) AS wins, count(*) FILTER (WHERE burned) AS burned_days
                    FROM paper_day WHERE account_code = :c
                """),
                {"c": code},
            )
        )
        .mappings()
        .one()
    )
    e = (
        (
            await session.execute(
                text("""
                    SELECT count(*) AS n, coalesce(sum(pnl), 0) AS pnl, count(*) FILTER (WHERE pnl > 0) AS wins
                    FROM paper_trade WHERE account_code = :c AND status = 'closed' AND exit_reason <> 'market_close'
                """),
                {"c": code},
            )
        )
        .mappings()
        .one()
    )
    return {
        "days": int(d["days"]),
        "pnl": float(d["pnl"]),
        "trades": int(d["trades"]),
        "wins": int(d["wins"]),
        "burned_days": int(d["burned_days"]),
        "eval_n": int(e["n"]),
        "eval_pnl": float(e["pnl"]),
        "eval_wins": int(e["wins"]),
    }


async def trades_page(session: AsyncSession, code: str, before_id: int | None, limit: int) -> list[dict[str, Any]]:
    sql = "SELECT * FROM paper_trade WHERE account_code = :c"
    params: dict[str, Any] = {"c": code, "n": limit}
    if before_id is not None:
        sql += " AND id < :b"
        params["b"] = before_id
    sql += " ORDER BY id DESC LIMIT :n"
    rows = (await session.execute(text(sql), params)).mappings()
    return [dict(r) for r in rows]


async def trades_today(session: AsyncSession, day: date) -> dict[str, dict[str, int]]:
    rows = await session.execute(
        text("""
            SELECT account_code, count(*) FILTER (WHERE status = 'closed') AS closed,
                   count(*) FILTER (WHERE status = 'closed' AND pnl > 0) AS wins
            FROM paper_trade WHERE trade_date = :d GROUP BY account_code
        """),
        {"d": day},
    )
    return {c: {"closed": int(n), "wins": int(w)} for c, n, w in rows}


async def latest_close(session: AsyncSession, not_before: datetime, at: datetime) -> dict[str, Any] | None:
    """Nến phút mới nhất trong [not_before, at] — giá tạm tính lãi/lỗ lệnh mở, kiểm cháy. Chặn trên bởi `at` để
    tua lại lịch sử (seed dogfood) không nhìn giá tương lai."""
    row = (
        (
            await session.execute(
                text("SELECT ts, close FROM candle_1m WHERE ts >= :nb AND ts <= :at ORDER BY ts DESC LIMIT 1"),
                {"nb": not_before, "at": at},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None
