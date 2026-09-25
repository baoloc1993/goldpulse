"""Worker: `python -m goldpulse.worker` — luồng A (nạp lịch sử), B (realtime), C (lấy bù), dọn dẹp."""

import asyncio
import signal
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import structlog

from ..config import settings
from ..db import engine, session_scope
from ..integrations.dukascopy import (
    BadFile,
    DayNotFound,
    DukascopyClient,
    RawCandle,
    Throttled,
    Unreachable,
)
from ..integrations.goldapi import GoldApiClient, GoldApiError
from ..integrations.histdata import FIRST_DAY as HD_FIRST
from ..integrations.histdata import FileNotAvailable, HistDataClient
from ..integrations.histdata import file_key as hd_file_key
from ..logs import setup_logging
from ..repositories import crawl as crawl_repo
from ..repositories import feed as feed_repo
from ..services import ingest
from ..services.calendar import MINUTE, NY, last_complete_day, open_minutes_of_day
from ..services.monitor import LAG_LIMIT_S

log = structlog.get_logger("worker")

UNREACHABLE_PAUSE_S = 300
THROTTLE_MIN_S = 60
THROTTLE_MAX_S = 900
IDLE_S = 600
HD_BATCH = 400  # ngày một lượt — một file năm HistData phủ ~300 ngày


@dataclass
class Outcome:
    """Kết quả một lượt nạp — quyết định có tạm dừng cả hàng chờ không."""

    stored: int = 0
    unreachable: int = 0
    throttled: int = 0
    failed: int = 0
    msg: str = ""


class Worker:
    def __init__(self) -> None:
        self.stop = asyncio.Event()
        self.duka = DukascopyClient(proxy=settings.dukascopy_proxy)
        self.hd = HistDataClient()
        self.hd_cache: dict[str, tuple[float, list[RawCandle] | None]] = {}
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

    async def set_source(
        self, state: str, message: str | None = None, retry_in: float | None = None, provider: str = "histdata"
    ) -> None:
        value: dict = {"state": state, "message": message, "provider": provider}
        if retry_in:
            value["retry_at"] = (datetime.now(UTC) + timedelta(seconds=retry_in)).isoformat()
        async with session_scope() as s:
            await feed_repo.set_status(s, "history_source", value)

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
                # Hai hàng chờ riêng: ngày cũ kẹt ở Dukascopy (cần WARP) không được chặn HistData
                old = await crawl_repo.next_batch(s, max(1, settings.dukascopy_concurrency) * 2, before=HD_FIRST)
                new = await crawl_repo.next_batch(s, HD_BATCH, since=HD_FIRST)
            if not old and not new:
                async with session_scope() as s:
                    waiting = (await crawl_repo.progress(s))["pending"]
                # Còn ngày đang nghỉ sau lỗi chập chờn → quay lại sớm; hết việc → nghỉ dài
                await self.sleep(60 if waiting else IDLE_S, wake_key="history_wake")
                continue
            out = Outcome()
            if new:
                await self.history_histdata(new, out)
            if old:
                await self.history_dukascopy(old, out)
            # Chỉ tạm dừng cả hàng chờ khi CẢ lô hỏng — lỗi lẻ tẻ thì ngày đó nghỉ, ngày khác chạy tiếp
            tried = out.stored + out.unreachable + out.throttled + out.failed
            if out.stored:
                self.throttle_s = THROTTLE_MIN_S
            elif out.unreachable and out.unreachable + out.throttled == tried:
                log.warning("history_paused", state="unreachable", seconds=UNREACHABLE_PAUSE_S, reason=out.msg)
                await self.sleep(UNREACHABLE_PAUSE_S, wake_key="history_wake")
            elif tried and out.throttled == tried:
                log.warning("history_paused", state="throttled", seconds=self.throttle_s, reason=out.msg)
                await self.sleep(self.throttle_s, wake_key="history_wake")
                self.throttle_s = min(self.throttle_s * 2, THROTTLE_MAX_S)
            elif not tried:
                # Chỉ còn ngày HistData chưa phát hành (trễ ~1 tuần) — chờ lượt sau, đừng tải lại liên tục
                await self.sleep(settings.backfill_hours * 3600, wake_key="history_wake")

    async def histdata_file(self, key: str) -> list[RawCandle] | Exception | None:
        """File HistData theo khoá, nhớ tạm vài file gần nhất (một file năm ~350 nghìn nến).
        None = HistData không có file này; Exception = tải lỗi."""
        hit = self.hd_cache.get(key)
        if hit is not None and ("/" not in key or time.monotonic() - hit[0] < 1800):
            return hit[1]
        try:
            candles: list[RawCandle] | None = await self.hd.fetch_file(key)
        except FileNotAvailable:
            candles = None
        except (Unreachable, Throttled, BadFile) as e:
            return e
        self.hd_cache[key] = (time.monotonic(), candles)
        while len(self.hd_cache) > 3:
            self.hd_cache.pop(next(iter(self.hd_cache)))
        return candles

    async def history_histdata(self, days: list[date], out: "Outcome") -> None:
        """Luồng A với HistData: ngày UTC D = NY D-1 19/20:00 → D 19/20:00, cần file chứa các ngày NY quanh D."""
        today = datetime.now(NY).date()
        for day in days:
            if self.stop.is_set():
                return
            # NY D-3 … D+1: giá đóng cửa trước cuối tuần/lễ làm nền nến đứng giá đầu ngày, và dữ liệu sau mép
            # ngày để biết ngày đã đủ (ngày lễ đóng sớm) — ở ranh giới năm là hai file năm liền nhau
            keys = sorted({hd_file_key(day + timedelta(days=k), today) for k in range(-3, 2)})
            files = [await self.histdata_file(k) for k in keys]
            errors = [f for f in files if isinstance(f, Exception)]
            if any(isinstance(e, Unreachable) for e in errors):
                out.unreachable += 1
                out.msg = str(errors[0])
                await self.set_source("unreachable", out.msg, UNREACHABLE_PAUSE_S, provider="histdata")
                return  # mất mạng thì các ngày sau cũng vậy
            summary = None
            async with session_scope() as s:
                if any(isinstance(e, Throttled) for e in errors):
                    out.throttled += 1
                    out.msg = str(errors[0])
                    await crawl_repo.mark_soft_fail(s, day, f"HistData chập chờn ({out.msg})")
                    continue
                if errors:
                    out.failed += 1
                    await crawl_repo.mark_error(s, day, str(errors[0]))
                    continue
                own = files[keys.index(hd_file_key(day, today))]
                if own is None:
                    if (today - day).days > 45:
                        out.failed += 1
                        await crawl_repo.mark_error(s, day, "HistData không có dữ liệu cho ngày này")
                    continue  # tháng mới chưa phát hành — để pending, lượt sau thử lại
                data = sorted((c for f in files if f for c in f), key=lambda c: c.ts)
                if not ingest.day_complete(day, data[-1].ts if data else None):
                    continue  # HistData chưa phát hành tới hết ngày này (trễ ~1 tuần)
                summary = await ingest.store_histdata_day(s, day, ingest.slice_and_fill(day, data))
                out.stored += 1
            await self.set_source("ok", provider="histdata")
            log.info("crawl_day_ok", day=str(day), source="histdata", status=summary.status, candles=summary.candles)

    async def history_dukascopy(self, days: list[date], out: "Outcome") -> None:
        """Luồng A với Dukascopy — chỉ cho ngày trước 15/03/2009 (HistData không có); cần WARP."""
        results = await asyncio.gather(*(self.fetch(d) for d in days), return_exceptions=True)
        stored = unreachable = 0
        for day, res in zip(days, results, strict=True):
            if isinstance(res, Unreachable):
                unreachable += 1  # không tính vào attempts — thường do WARP tắt
                out.msg = str(res)
                continue
            async with session_scope() as s:
                if isinstance(res, Throttled):
                    out.throttled += 1
                    await crawl_repo.mark_soft_fail(s, day, f"Dukascopy chập chờn ({res})")
                elif isinstance(res, DayNotFound | BadFile):
                    out.failed += 1
                    await crawl_repo.mark_error(s, day, str(res))
                    log.warning("crawl_day_error", day=str(day), error=str(res))
                elif isinstance(res, BaseException):
                    out.failed += 1
                    await crawl_repo.mark_error(s, day, f"{type(res).__name__}: {res}")
                    log.error("crawl_day_exception", day=str(day), error=repr(res))
                else:
                    summary = await ingest.store_history_day(s, day, res)
                    stored += 1
                    log.info("crawl_day_ok", day=str(day), source="dukascopy", status=summary.status)
        out.stored += stored
        out.unreachable += unreachable
        async with session_scope() as s:
            await feed_repo.set_status(
                s, "dukascopy_source", {"state": "unreachable" if unreachable and not stored else "ok"}
            )

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
        """Mỗi BACKFILL_HOURS giờ (mặc định 24): tải lại file HistData tháng hiện tại (+ tháng trước ở đầu
        tháng), nến final đè nến tạm của 14 ngày gần nhất. KHÔNG ghi crawl_day — ngày chỉ thành ok qua luồng A."""
        while not self.stop.is_set():
            now = datetime.now(UTC)
            today = datetime.now(NY).date()
            keys = {hd_file_key(today, today), hd_file_key(today - timedelta(days=14), today)}
            done, problems = [], []
            data: list[RawCandle] = []
            for key in sorted(keys):
                self.hd_cache.pop(key, None)  # luôn lấy bản mới nhất
                f = await self.histdata_file(key)
                if isinstance(f, Unreachable | Throttled):
                    problems.append(f"{key}: {f}")
                    state = "unreachable" if isinstance(f, Unreachable) else "throttled"
                    await self.set_source(state, str(f), UNREACHABLE_PAUSE_S, provider="histdata")
                elif isinstance(f, Exception):
                    problems.append(f"{key}: {f}")
                elif f:
                    data.extend(f)
            data.sort(key=lambda c: c.ts)
            if data:
                last = data[-1].ts
                for i in range(14, 0, -1):
                    day = (now - timedelta(days=i)).date()
                    candles = ingest.slice_and_fill(day, data, until=last)
                    if not candles:
                        continue
                    async with session_scope() as s:
                        await ingest.store_histdata_backfill(s, day, candles)
                    done.append(str(day))
            async with session_scope() as s:
                await feed_repo.set_status(
                    s, "backfill", {"days": done, "problems": problems, "until": data[-1].ts if data else None}
                )
            log.info("backfill", days=done, problems=problems)
            await self.sleep(
                settings.backfill_hours * 3600 if not problems else UNREACHABLE_PAUSE_S, wake_key="history_wake"
            )

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
        await self.hd.close()
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
