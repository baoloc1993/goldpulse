"""(v4) inst_candle · inst_feed · inst_signal · inst_account · inst_trade · inst_day — bảng cặp mới (ARCHITECTURE §6d).

Không đọc/ghi bảng vàng. Lệnh giấy không xoá; web chỉ đổi `enabled`.
"""

import json
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# --- nến -----------------------------------------------------------------------------------------------------------


async def upsert_candles(session: AsyncSession, symbol: str, rows: Iterable[tuple], source: str = "binance") -> int:
    """rows: (ts, open, high, low, close, volume) của nến ĐÃ ĐÓNG. Lấy bù đè cùng giá trị — idempotent."""
    params = [
        {"s": symbol, "ts": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5], "src": source} for r in rows
    ]
    if not params:
        return 0
    await session.execute(
        text("""
            INSERT INTO inst_candle (symbol, ts, open, high, low, close, volume, source)
            VALUES (:s, :ts, :o, :h, :l, :c, :v, :src)
            ON CONFLICT (symbol, ts) DO UPDATE SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                close = EXCLUDED.close, volume = EXCLUDED.volume
        """),
        params,
    )
    return len(params)


async def candle_span(session: AsyncSession, symbol: str) -> tuple[datetime | None, datetime | None]:
    row = (
        await session.execute(text("SELECT min(ts), max(ts) FROM inst_candle WHERE symbol = :s"), {"s": symbol})
    ).one()
    return row[0], row[1]


async def hourly_bars(session: AsyncSession, symbol: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Nến 1h gộp từ nến 1m trong [start, end) — giờ không có phút nào thì KHÔNG có hàng (như resample().dropna())."""
    rows = (
        await session.execute(
            text("""
                SELECT date_trunc('hour', ts) AS bar_ts,
                       (array_agg(open ORDER BY ts))[1] AS open, max(high) AS high, min(low) AS low,
                       (array_agg(close ORDER BY ts DESC))[1] AS close, sum(volume) AS volume, count(*) AS n
                FROM inst_candle WHERE symbol = :s AND ts >= :a AND ts < :b
                GROUP BY 1 ORDER BY 1
            """),
            {"s": symbol, "a": start, "b": end},
        )
    ).mappings()
    return [dict(r) for r in rows]


async def candles_between(session: AsyncSession, symbol: str, after: datetime, upto: datetime) -> list[dict[str, Any]]:
    """Nến 1m có after < ts ≤ upto, theo thời gian."""
    rows = (
        await session.execute(
            text("""
                SELECT ts, open, high, low, close FROM inst_candle
                WHERE symbol = :s AND ts > :a AND ts <= :b ORDER BY ts
            """),
            {"s": symbol, "a": after, "b": upto},
        )
    ).mappings()
    return [dict(r) for r in rows]


async def candle_at(session: AsyncSession, symbol: str, ts: datetime) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                text("SELECT ts, open, high, low, close FROM inst_candle WHERE symbol = :s AND ts = :t"),
                {"s": symbol, "t": ts},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def latest_candle(session: AsyncSession, symbol: str, at: datetime) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                text("SELECT ts, close FROM inst_candle WHERE symbol = :s AND ts <= :at ORDER BY ts DESC LIMIT 1"),
                {"s": symbol, "at": at},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


# --- giá hiện tại + độ trễ -----------------------------------------------------------------------------------------


async def insert_feed(
    session: AsyncSession,
    symbol: str,
    polled_at: datetime,
    price: float | None,
    last_closed_ts: datetime | None,
    lag: float | None,
    ok: bool,
    error: str | None = None,
) -> None:
    await session.execute(
        text("""
            INSERT INTO inst_feed (symbol, polled_at, price, last_closed_ts, lag_seconds, ok, error)
            VALUES (:s, :p, :price, :lc, :lag, :ok, :e) ON CONFLICT (symbol, polled_at) DO NOTHING
        """),
        {"s": symbol, "p": polled_at, "price": price, "lc": last_closed_ts, "lag": lag, "ok": ok, "e": error},
    )


async def latest_feed(session: AsyncSession, symbol: str) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                text("SELECT * FROM inst_feed WHERE symbol = :s ORDER BY polled_at DESC LIMIT 1"), {"s": symbol}
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def latest_price(session: AsyncSession, symbol: str) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                text("""
                    SELECT polled_at, price FROM inst_feed WHERE symbol = :s AND price IS NOT NULL
                    ORDER BY polled_at DESC LIMIT 1
                """),
                {"s": symbol},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def feed_lag_ratio(session: AsyncSession, symbol: str, since: datetime) -> dict[str, Any]:
    """AC-16: tỉ lệ lần poll mà nến đã đóng mới nhất trễ < 60 s (lần poll lỗi tính là trễ)."""
    row = (
        (
            await session.execute(
                text("""
                    SELECT count(*) AS n, count(*) FILTER (WHERE ok AND lag_seconds < 60) AS fresh
                    FROM inst_feed WHERE symbol = :s AND polled_at >= :a
                """),
                {"s": symbol, "a": since},
            )
        )
        .mappings()
        .one()
    )
    return {"n": int(row["n"]), "fresh": int(row["fresh"])}


async def cleanup_feed(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM inst_feed WHERE polled_at < now() - interval '30 days'"))


# --- tín hiệu ------------------------------------------------------------------------------------------------------


async def signal_bars(session: AsyncSession, symbol: str, since: datetime) -> set[datetime]:
    rows = await session.execute(
        text("SELECT bar_ts FROM inst_signal WHERE symbol = :s AND bar_ts >= :a"), {"s": symbol, "a": since}
    )
    return {r[0] for r in rows}


async def insert_signal(
    session: AsyncSession,
    symbol: str,
    bar_ts: datetime,
    computed_at: datetime,
    close: float,
    values: dict[str, Any],
    fired: dict[str, int],
    model: str,
) -> bool:
    res = await session.execute(
        text("""
            INSERT INTO inst_signal (symbol, bar_ts, computed_at, close, values, fired, model)
            VALUES (:s, :b, :c, :close, CAST(:v AS jsonb), CAST(:f AS jsonb), :m)
            ON CONFLICT (symbol, bar_ts) DO NOTHING RETURNING bar_ts
        """),
        {
            "s": symbol,
            "b": bar_ts,
            "c": computed_at,
            "close": close,
            "v": json.dumps(values),
            "f": json.dumps(fired),
            "m": model,
        },
    )
    return res.first() is not None


async def last_signal(session: AsyncSession, symbol: str) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                text("SELECT * FROM inst_signal WHERE symbol = :s ORDER BY bar_ts DESC LIMIT 1"), {"s": symbol}
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


# --- tài khoản + lệnh ----------------------------------------------------------------------------------------------


async def accounts(session: AsyncSession, symbol: str) -> list[dict[str, Any]]:
    rows = (
        await session.execute(text("SELECT * FROM inst_account WHERE symbol = :s ORDER BY code"), {"s": symbol})
    ).mappings()
    return [dict(r) for r in rows]


async def get_account(session: AsyncSession, code: str) -> dict[str, Any] | None:
    row = (await session.execute(text("SELECT * FROM inst_account WHERE code = :c"), {"c": code})).mappings().first()
    return dict(row) if row else None


async def set_enabled(session: AsyncSession, code: str, enabled: bool) -> bool:
    res = await session.execute(
        text("UPDATE inst_account SET enabled = :e, updated_at = now() WHERE code = :c"), {"c": code, "e": enabled}
    )
    return res.rowcount == 1


async def insert_trade(session: AsyncSession, row: dict[str, Any]) -> int:
    res = await session.execute(
        text("""
            INSERT INTO inst_trade (account_code, bar_ts, trade_date, opened_at, direction, entry_price, notional,
                                    due_at, status, skip_reason, context)
            VALUES (:account_code, :bar_ts, :trade_date, :opened_at, :direction, :entry_price, :notional,
                    :due_at, :status, :skip_reason, CAST(:context AS jsonb))
            ON CONFLICT (account_code, bar_ts) DO NOTHING
            RETURNING id
        """),
        {**row, "context": json.dumps(row.get("context") or {}, default=str)},
    )
    got = res.scalar()
    return int(got) if got is not None else 0


async def open_trades(session: AsyncSession, symbol: str) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text("""
                SELECT t.* FROM inst_trade t JOIN inst_account a ON a.code = t.account_code
                WHERE t.status = 'open' AND a.symbol = :s ORDER BY t.id
            """),
            {"s": symbol},
        )
    ).mappings()
    return [dict(r) for r in rows]


async def open_trade_of(session: AsyncSession, code: str) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                text("SELECT * FROM inst_trade WHERE status = 'open' AND account_code = :c"), {"c": code}
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
            UPDATE inst_trade SET status = 'closed', exit_price = :p, exit_reason = :r, pnl = :pnl, closed_at = :t
            WHERE id = :id AND status = 'open'
        """),
        {"id": trade_id, "p": exit_price, "r": reason, "pnl": pnl, "t": closed_at},
    )
    return res.rowcount == 1


async def day_get(session: AsyncSession, code: str, day: date) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                text("SELECT * FROM inst_day WHERE account_code = :c AND trade_date = :d"), {"c": code, "d": day}
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def day_ensure(session: AsyncSession, code: str, day: date, start_equity: float) -> None:
    await session.execute(
        text("""
            INSERT INTO inst_day (account_code, trade_date, start_equity, end_equity)
            VALUES (:c, :d, :s, :s) ON CONFLICT (account_code, trade_date) DO NOTHING
        """),
        {"c": code, "d": day, "s": start_equity},
    )


async def day_apply(session: AsyncSession, code: str, day: date, pnl: float, burned: bool) -> None:
    await session.execute(
        text("""
            UPDATE inst_day SET realized_pnl = realized_pnl + :pnl, n_trades = n_trades + 1,
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
            text("SELECT * FROM inst_day WHERE account_code = :c ORDER BY trade_date DESC LIMIT :n"),
            {"c": code, "n": n},
        )
    ).mappings()
    return [dict(r) for r in reversed(list(rows))]


async def totals(session: AsyncSession, code: str) -> dict[str, Any]:
    """Cộng dồn mọi ngày + phần dùng cho GO/PIVOT/KILL (lệnh đã đóng trừ `day_close` — PRD §6 vòng 4)."""
    d = (
        (
            await session.execute(
                text("""
                    SELECT count(*) AS days, coalesce(sum(realized_pnl), 0) AS pnl,
                           coalesce(sum(n_trades), 0) AS trades,
                           coalesce(sum(n_wins), 0) AS wins, count(*) FILTER (WHERE burned) AS burned_days
                    FROM inst_day WHERE account_code = :c
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
                    FROM inst_trade WHERE account_code = :c AND status = 'closed' AND exit_reason <> 'day_close'
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
    sql = "SELECT * FROM inst_trade WHERE account_code = :c"
    params: dict[str, Any] = {"c": code, "n": limit}
    if before_id is not None:
        sql += " AND id < :b"
        params["b"] = before_id
    sql += " ORDER BY id DESC LIMIT :n"
    rows = (await session.execute(text(sql), params)).mappings()
    return [dict(r) for r in rows]


# --- báo Telegram BTC: chỉ đọc -------------------------------------------------------------------------------------


async def max_trade_id(session: AsyncSession, symbol: str) -> int:
    return int(
        (
            await session.execute(
                text("""
                    SELECT coalesce(max(t.id), 0) FROM inst_trade t JOIN inst_account a ON a.code = t.account_code
                    WHERE a.symbol = :s
                """),
                {"s": symbol},
            )
        ).scalar()
        or 0
    )


async def trades_after(session: AsyncSession, symbol: str, after_id: int, limit: int = 200) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text("""
                SELECT t.*, a.name AS account_name FROM inst_trade t JOIN inst_account a ON a.code = t.account_code
                WHERE a.symbol = :s AND t.id > :a ORDER BY t.id LIMIT :n
            """),
            {"s": symbol, "a": after_id, "n": limit},
        )
    ).mappings()
    return [dict(r) for r in rows]


async def closed_since(session: AsyncSession, symbol: str, since: datetime) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text("""
                SELECT t.*, a.name AS account_name FROM inst_trade t JOIN inst_account a ON a.code = t.account_code
                WHERE a.symbol = :s AND t.status = 'closed' AND t.closed_at >= :a ORDER BY t.closed_at, t.id
            """),
            {"s": symbol, "a": since},
        )
    ).mappings()
    return [dict(r) for r in rows]
