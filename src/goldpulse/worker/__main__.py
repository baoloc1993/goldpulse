"""Worker: `python -m goldpulse.worker` — luồng A (nạp lịch sử), B (realtime), C (lấy bù), dọn dẹp."""

import asyncio
import signal
from datetime import UTC, date, datetime, timedelta

import structlog

from ..config import settings
from ..db import engine, session_scope
from ..integrations.dukascopy import (
    BadFile,
    DayNotFound,
    DukascopyClient,
    Throttled,
    Unreachable,
)
from ..integrations.goldapi import GoldApiClient, GoldApiError
from ..logs import setup_logging
from ..repositories import crawl as crawl_repo
from ..repositories import feed as feed_repo
from ..services import ingest
from ..services.calendar import MINUTE, last_complete_day, open_minutes_of_day
from ..services.monitor import LAG_LIMIT_S

log = structlog.get_logger("worker")

UNREACHABLE_PAUSE_S = 300
THROTTLE_MIN_S = 60
THROTTLE_MAX_S = 900
IDLE_S = 600
BACKFILL_EVERY_S = 3600


class Worker:
    def __init__(self) -> None:
        self.stop = asyncio.Event()
        self.duka = DukascopyClient(proxy=settings.dukascopy_proxy)
        self.gold = GoldApiClient()
        # A và C dùng chung một giới hạn — Dukascopy bóp theo IP, không theo luồng
        self.duka_slots = asyncio.Semaphore(max(1, settings.dukascopy_concurrency))
        self.throttle_s = THROTTLE_MIN_S

    async def sleep(self, seconds: float, wake_key: str | None = None) -> None:
        """Ngủ có thể bị đánh thức: dừng mềm, hoặc người vận hành bấm 'Thử lại ngay'."""
        end = asyncio.get_running_loop().time() + seconds
        started = datetime.now(UTC)
        while not self.stop.is_set():
            left = end - asyncio.get_running_loop().time()
            if left <= 0:
                return
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=min(5.0, left))
            except TimeoutError:
                pass
            if wake_key:
                async with session_scope() as s:
                    wake = await feed_repo.get_status(s, wake_key)
                if wake and wake["updated_at"] > started:
                    log.info("woken", key=wake_key)
                    return

    async def fetch(self, day: date):
        async with self.duka_slots:
            try:
                return await self.duka.fetch_day(day)
            finally:
                await asyncio.sleep(settings.dukascopy_delay_seconds)

    async def set_source(self, state: str, message: str | None = None) -> None:
        async with session_scope() as s:
            await feed_repo.set_status(s, "history_source", {"state": state, "message": message})

    # --- A. Nạp lịch sử --------------------------------------------------------------
    async def ensure_days(self) -> None:
        last = last_complete_day(datetime.now(UTC))
        async with session_scope() as s:
            have = await crawl_repo.existing_last_day(s)
        start = (have + timedelta(days=1)) if have else settings.history_start_date
        rows = []
        d = start
        while d <= last:
            rows.append((d, len(open_minutes_of_day(d))))
            d += timedelta(days=1)
        for i in range(0, len(rows), 1000):
            async with session_scope() as s:
                await crawl_repo.insert_pending(s, rows[i : i + 1000])
        if rows:
            log.info("crawl_days_added", count=len(rows), first=str(rows[0][0]), last=str(rows[-1][0]))

    async def history_loop(self) -> None:
        while not self.stop.is_set():
            await self.ensure_days()
            async with session_scope() as s:
                batch = await crawl_repo.next_batch(s, max(1, settings.dukascopy_concurrency) * 2)
            if not batch:
                async with session_scope() as s:
                    waiting = (await crawl_repo.progress(s))["pending"]
                # Còn ngày đang nghỉ sau lỗi chập chờn → quay lại sớm; hết việc → nghỉ dài
                await self.sleep(60 if waiting else IDLE_S)
                continue
            results = await asyncio.gather(*(self.fetch(d) for d in batch), return_exceptions=True)
            unreachable = throttled = stored = 0
            last_msg = ""
            for day, res in zip(batch, results, strict=True):
                if isinstance(res, Unreachable):
                    unreachable += 1  # không tính vào attempts — thường do WARP tắt
                    last_msg = str(res)
                    continue
                async with session_scope() as s:
                    if isinstance(res, Throttled):
                        throttled += 1
                        last_msg = str(res)
                        await crawl_repo.mark_soft_fail(s, day, f"Dukascopy chập chờn ({res})")
                    elif isinstance(res, DayNotFound | BadFile):
                        await crawl_repo.mark_error(s, day, str(res))
                        log.warning("crawl_day_error", day=str(day), error=str(res))
                    elif isinstance(res, BaseException):
                        await crawl_repo.mark_error(s, day, f"{type(res).__name__}: {res}")
                        log.error("crawl_day_exception", day=str(day), error=repr(res))
                    else:
                        summary = await ingest.store_history_day(s, day, res)
                        stored += 1
                        log.info("crawl_day_ok", day=str(day), status=summary.status, candles=summary.candles)
            # Chỉ tạm dừng cả hàng chờ khi CẢ lô hỏng — lỗi lẻ tẻ thì ngày đó nghỉ, ngày khác chạy tiếp
            if stored:
                self.throttle_s = THROTTLE_MIN_S
                await self.set_source("ok")
            elif unreachable and unreachable + throttled == len(batch):
                await self.set_source("unreachable", last_msg)
                log.warning("history_paused", state="unreachable", seconds=UNREACHABLE_PAUSE_S, reason=last_msg)
                await self.sleep(UNREACHABLE_PAUSE_S, wake_key="history_wake")
            elif throttled == len(batch):
                await self.set_source("throttled", last_msg)
                log.warning("history_paused", state="throttled", seconds=self.throttle_s, reason=last_msg)
                await self.sleep(self.throttle_s, wake_key="history_wake")
                self.throttle_s = min(self.throttle_s * 2, THROTTLE_MAX_S)

    # --- B. Giá realtime -------------------------------------------------------------
    async def realtime_loop(self) -> None:
        current_minute: datetime | None = None
        last_beat = datetime.min.replace(tzinfo=UTC)
        while not self.stop.is_set():
            now = datetime.now(UTC)
            try:
                q = await self.gold.fetch()
                lag = (now - q.updated_at).total_seconds()
                async with session_scope() as s:
                    await feed_repo.insert_sample(s, now, q.updated_at, q.price, lag, lag < LAG_LIMIT_S)
            except GoldApiError as e:
                log.warning("goldapi_error", error=str(e))
                async with session_scope() as s:
                    await feed_repo.insert_sample(s, now, None, None, None, False)
            minute = now.replace(second=0, microsecond=0)
            if current_minute and minute > current_minute:
                await self.close_minute(current_minute)
            current_minute = minute
            if (now - last_beat).total_seconds() >= 30:
                async with session_scope() as s:
                    await feed_repo.set_status(s, "worker", {"pid": _pid()})
                last_beat = now
            await self.sleep(max(0.5, settings.realtime_poll_seconds - (datetime.now(UTC) - now).total_seconds()))

    async def close_minute(self, minute: datetime) -> None:
        """Hết phút → dựng nến tạm từ các mẫu tươi của phút đó (không đè nến final)."""
        try:
            async with session_scope() as s:
                prices = await feed_repo.ok_prices_in(s, minute, minute + MINUTE)
                await ingest.store_temp_minute(s, minute, prices)
        except Exception:
            log.exception("temp_candle_failed", minute=minute.isoformat())

    # --- C. Lấy bù --------------------------------------------------------------------
    async def backfill_loop(self) -> None:
        while not self.stop.is_set():
            now = datetime.now(UTC)
            # Chỉ ngày đã kết thúc: file của ngày đang chạy có thể chưa đủ, nến đứng giá sẽ đè nến tạm
            days = [
                (now - timedelta(hours=48)).date() + timedelta(days=i)
                for i in range(((now.date() - timedelta(days=1)) - (now - timedelta(hours=48)).date()).days + 1)
            ]
            done, problems = [], []
            for d in days:
                try:
                    candles = await self.fetch(d)
                except (Unreachable, Throttled) as e:
                    problems.append(f"{d}: {e}")
                    await self.set_source("unreachable" if isinstance(e, Unreachable) else "throttled", str(e))
                    break
                except (DayNotFound, BadFile) as e:
                    problems.append(f"{d}: {e}")
                    continue
                async with session_scope() as s:
                    await ingest.store_backfill_day(s, d, candles)
                done.append(str(d))
            async with session_scope() as s:
                await feed_repo.set_status(s, "backfill", {"days": done, "problems": problems})
            log.info("backfill", days=done, problems=problems)
            await self.sleep(BACKFILL_EVERY_S if not problems else UNREACHABLE_PAUSE_S, wake_key="history_wake")

    async def cleanup_loop(self) -> None:
        while not self.stop.is_set():
            async with session_scope() as s:
                await feed_repo.cleanup(s)
            await self.sleep(86400)

    async def run(self) -> None:
        async with session_scope() as s:
            await feed_repo.set_status_if_absent(s, "first_run", {"at": datetime.now(UTC).isoformat()})
        log.info("worker_start", history_start=str(settings.history_start_date))
        tasks = [
            asyncio.create_task(self._guard(self.history_loop), name="history"),
            asyncio.create_task(self._guard(self.realtime_loop), name="realtime"),
            asyncio.create_task(self._guard(self.backfill_loop), name="backfill"),
            asyncio.create_task(self._guard(self.cleanup_loop), name="cleanup"),
        ]
        await self.stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.duka.close()
        await self.gold.close()
        await engine.dispose()
        log.info("worker_stop")

    async def _guard(self, loop_fn) -> None:
        """Một vòng lặp chết (DB rớt…) thì log rồi chạy lại sau 10s — không kéo worker chết theo."""
        while not self.stop.is_set():
            try:
                await loop_fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("loop_crashed", loop=loop_fn.__name__)
                await self.sleep(10)


def _pid() -> int:
    import os

    return os.getpid()


def main() -> None:
    setup_logging()
    w = Worker()

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, w.stop.set)
            except NotImplementedError:  # Windows
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(w.stop.set))
        await w.run()

    asyncio.run(runner())


if __name__ == "__main__":
    main()
