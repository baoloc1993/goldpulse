"""Số liệu cho trang theo dõi: Tổng quan · Độ phủ · Tra thử."""

import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from ..integrations.histdata import FIRST_DAY as HD_FIRST
from ..repositories import candles as candle_repo
from ..repositories import crawl as crawl_repo
from ..repositories import feed as feed_repo
from .calendar import SESSIONS, VN, active_sessions, is_market_open

# Ngưỡng độ trễ giá realtime — 60s (chủ dự án chốt 2026-09-25; gold-api đổi giá ~30s/lần nên 30s không đạt nổi)
LAG_LIMIT_S = 60
COVERAGE_TARGET = 99.0
LATENCY_TARGET = 95.0
SPEED_TARGET_MS = 1000.0
SPEED_SPAN_YEARS = 20
COVERAGE_CACHE_S = 600

_coverage_cache: dict[str, Any] = {"at": 0.0, "value": None}


def _pct(num: int | float | None, den: int | float | None) -> float | None:
    if not den:
        return None
    return round(100.0 * (num or 0) / den, 2)


def _vn(ts: datetime) -> str:
    return ts.astimezone(VN).strftime("%H:%M")


async def overview(session: AsyncSession, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    sample = await feed_repo.latest_price_sample(session)
    last = await feed_repo.latest_sample(session)

    price: dict[str, Any] | None = None
    if sample:
        lag = (now - sample["source_ts"]).total_seconds() if sample["source_ts"] else None
        stale = lag is None or lag >= LAG_LIMIT_S
        price = {
            "value": sample["price"],
            "lag_seconds": round(lag) if lag is not None else None,
            "source_updated_vn": sample["source_ts"].astimezone(VN).strftime("%H:%M:%S")
            if sample["source_ts"]
            else None,
            "source_ts": sample["source_ts"].isoformat() if sample["source_ts"] else None,  # JS tự tính trễ
            "stale": stale,
            "market_open": is_market_open(now),
            "lag_limit": LAG_LIMIT_S,  # JS dùng đúng một ngưỡng với server
            "poll_failing": bool(last and last["price"] is None and (now - last["polled_at"]).total_seconds() < 60),
        }

    sessions = [{"key": k, "label": SESSIONS[k].label, "closes_vn": _vn(c)} for k, _, c in active_sessions(now)]

    prog = await crawl_repo.progress(session)
    total = sum(prog.values()) - prog["skipped"] - prog["awaiting"]  # ngày phải tải thật, nguồn đã có
    done = prog["ok"] + prog["empty"]
    history_source = await feed_repo.get_status(session, "history_source") or {}
    backfill = await feed_repo.get_status(session, "backfill") or {}
    worker = await feed_repo.get_status(session, "worker") or {}

    cov = await coverage(session)
    first_run = await feed_repo.get_status(session, "first_run")
    latency: dict[str, Any] = {"value": None, "minutes": 0, "since_vn": None}
    if first_run:
        since = datetime.fromisoformat(first_run["at"])
        since = max(since, now - timedelta(days=30))
        open_min, good = await feed_repo.latency_metric(session, since, LAG_LIMIT_S)
        latency = {
            "value": _pct(good, open_min),
            "minutes": open_min,
            "good": good,
            "since_vn": since.astimezone(VN).strftime("%d/%m"),
        }
    p95, n_queries = await feed_repo.query_speed_p95(session, SPEED_SPAN_YEARS)

    worker_seen = worker.get("updated_at")
    last_error = await feed_repo.get_status(session, "last_error")
    recent_error = (
        {
            "where": last_error["where"],
            "error": last_error["error"],
            "at_vn": last_error["updated_at"].astimezone(VN).strftime("%H:%M %d/%m"),
        }
        if last_error and (now - last_error["updated_at"]).total_seconds() < 24 * 3600
        else None
    )
    return {
        "now": now.isoformat(),
        "price": price,
        "sessions": sessions,
        "history": {
            "total_days": total,
            "done_days": done,
            "ok_days": prog["ok"],  # ngày có dữ liệu thật — thứ Bảy `empty` không tính
            "awaiting_days": prog["awaiting"],  # ngày mới nhất nguồn lịch sử chưa phát hành (nến tạm giữ chỗ)
            "error_days": prog["error"],
            "source": history_source.get("state", "unknown"),  # ok | unreachable | throttled | unknown
            "source_message": history_source.get("message"),
            "provider": history_source.get("provider", "dukascopy"),
            "old_pending": await crawl_repo.pending_before(session, HD_FIRST),
            "source_checked_vn": history_source["updated_at"].astimezone(VN).strftime("%H:%M:%S")
            if history_source.get("updated_at")
            else None,
            "source_checked": history_source["updated_at"].isoformat() if history_source.get("updated_at") else None,
            "retry_vn": _vn(datetime.fromisoformat(history_source["retry_at"]))
            if history_source.get("retry_at")
            else None,
            "retry_at": history_source.get("retry_at"),
            "last_backfill_vn": backfill["updated_at"].astimezone(VN).strftime("%H:%M %d/%m")
            if backfill.get("days")
            else None,
        },
        "worker_alive": bool(worker_seen and (now - worker_seen).total_seconds() < 120),
        "last_error": recent_error,  # lỗi hệ thống trong 24 giờ qua — thay Sentry khi chưa bật
        "metrics": {
            "coverage": {"value": cov["total"]["covered_pct"], "target": COVERAGE_TARGET},
            "latency": {**latency, "target": LATENCY_TARGET, "lag_limit": LAG_LIMIT_S},
            "speed": {"value": round(p95) if p95 is not None else None, "count": n_queries, "target": SPEED_TARGET_MS},
        },
    }


async def coverage(session: AsyncSession, fresh: bool = False) -> dict[str, Any]:
    """Độ phủ theo năm + đoạn thiếu (cache 10 phút) + trạng thái nguồn lúc này."""
    src = await feed_repo.get_status(session, "history_source") or {}
    return {
        **await _coverage_years(session, fresh),
        "source": src.get("state", "unknown"),
        "provider": src.get("provider", "dukascopy"),
    }


async def _coverage_years(session: AsyncSession, fresh: bool) -> dict[str, Any]:
    if not fresh and _coverage_cache["value"] and time.monotonic() - _coverage_cache["at"] < COVERAGE_CACHE_S:
        return _coverage_cache["value"]
    rows = await crawl_repo.coverage_by_year(session)
    years = []
    cal_sum = cov_sum = tr_sum = 0
    for r in rows:
        cal, cov, tr = r["calendar"] or 0, r["covered"] or 0, r["traded"] or 0
        cal_sum, cov_sum, tr_sum = cal_sum + cal, cov_sum + cov, tr_sum + tr
        gaps = _gaps(await crawl_repo.gaps_of_year(session, r["year"])) if cal > cov else []
        years.append(
            {
                "year": r["year"],
                "covered_pct": _pct(cov, cal),
                "traded_pct": _pct(tr, cal),
                "days": r["days"],
                "done_days": r["done_days"],
                "pending_days": r["pending_days"],
                "error_days": r["error_days"],
                "gaps": gaps,
            }
        )
    result = {
        "total": {"covered_pct": _pct(cov_sum, cal_sum), "traded_pct": _pct(tr_sum, cal_sum)},
        "years": years,
        "errors": [
            {"day": e["day"].isoformat(), "error": e["last_error"], "attempts": e["attempts"]}
            for e in await crawl_repo.error_days(session)
        ],
    }
    _coverage_cache.update(at=time.monotonic(), value=result)
    return result


def invalidate_coverage_cache() -> None:
    _coverage_cache.update(at=0.0, value=None)


def _gaps(days: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Đoạn thiếu của một năm. Ngày chờ/lỗi liên tiếp gộp thành một đoạn — ngày lễ/thứ Bảy (`empty`,
    đã bị loại khỏi `days`) không cắt đoạn; chỉ một ngày `ok` mới cắt."""
    out: list[dict[str, Any]] = []
    run: dict[str, Any] | None = None
    for d in days:
        status, day = d["status"], d["day"].isoformat()
        if status in ("pending", "error"):
            if run and run["kind"] == status:
                run["last_day"] = day
                run["days"] += 1
            else:
                run = {"kind": status, "from": day, "last_day": day, "days": 1}
                out.append(run)
            continue
        run = None
        for a, b in d["missing_ranges"]:
            out.append({"kind": "missing", "from": a, "to": b, "days": 0})
    return out


async def retry_day(session: AsyncSession, day: date) -> bool:
    ok = await crawl_repo.retry(session, day)
    if ok:
        await wake_history(session)  # worker đang nghỉ vì hết việc → làm ngay, không đợi 10 phút
    invalidate_coverage_cache()
    return ok


async def session_query(
    session: AsyncSession, name: str, year_from: int, year_to: int, isodow: int | None
) -> dict[str, Any]:
    """Tra thống kê phiên + đo thời gian + ghi query_log (metric tốc độ)."""
    t0 = time.perf_counter()
    stats = await candle_repo.session_stats(session, name, year_from, year_to, isodow)
    duration_ms = (time.perf_counter() - t0) * 1000
    n = stats["n"] or 0
    span = year_to - year_from + 1
    await feed_repo.insert_query_log(
        session,
        {"session": name, "year_from": year_from, "year_to": year_to, "isodow": isodow, "span_years": span},
        duration_ms,
        n,
    )
    return {
        "count": n,
        "avg_range": round(stats["avg_range"], 2) if stats["avg_range"] is not None else None,
        "up_pct": _pct(stats["up"], n),
        "down_pct": _pct(stats["down"], n),
        "flat_pct": _pct(stats["flat"], n),
        "first_day": stats["first_day"].isoformat() if stats["first_day"] else None,
        "last_day": stats["last_day"].isoformat() if stats["last_day"] else None,
        "duration_ms": round(duration_ms, 1),
        "span_years": span,
        # Năm trong khoảng tra KHÔNG có phiên nào — để Tra thử nói rõ kết quả đang lấp chỗ trống
        "missing_years": [y for y in range(year_from, year_to + 1) if y not in set(stats["years"] or [])],
    }


async def year_bounds(session: AsyncSession, start: date) -> tuple[int, int]:
    _, last = await candle_repo.data_years(session)
    return start.year, max(last or start.year, datetime.now(UTC).year)


async def db_alive(session: AsyncSession) -> bool:
    try:
        await feed_repo.db_ping(session)
        return True
    except Exception:
        structlog.get_logger("health").warning("db_ping_failed", exc_info=True)
        return False


async def wake_history(session: AsyncSession) -> None:
    """Nút "Thử lại ngay" ở Tổng quan: đánh thức luồng nạp/lấy bù đang tạm dừng."""
    await feed_repo.set_status(session, "history_wake", {"by": "operator"})
