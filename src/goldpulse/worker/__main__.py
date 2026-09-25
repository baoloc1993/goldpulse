"""Worker: `python -m goldpulse.worker` — luồng A (nạp lịch sử), B (realtime), C (lấy bù), dọn dẹp;
(v2) F (tín hiệu 15 phút → job dự đoán), G (chấm dự đoán + dọn hàng chờ não), H (nạp lịch FED)."""

import asyncio
import signal
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import structlog
from sqlalchemy import text

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
from ..integrations.fred import FredClient
from ..integrations.goldapi import GoldApiClient, GoldApiError
from ..integrations.histdata import FIRST_DAY as HD_FIRST
from ..integrations.histdata import FileNotAvailable, HistDataClient
from ..integrations.histdata import file_key as hd_file_key
from ..logs import setup_logging
from ..repositories import chat as chat_repo
from ..repositories import crawl as crawl_repo
from ..repositories import fed as fed_repo
from ..repositories import feed as feed_repo
from ..repositories import jobs as job_repo
from ..services import fed as fed_svc
from ..services import ingest, predictions, signals
from ..services.calendar import MINUTE, NY, last_complete_day, open_minutes_of_day
from ..services.errors import record_error
from ..services.monitor import LAG_LIMIT_S

log = structlog.get_logger("worker")

UNREACHABLE_PAUSE_S = 300
THROTTLE_MIN_S = 60
THROTTLE_MAX_S = 900
IDLE_S = 600
HD_BATCH = 400  # ngày một lượt — một file năm HistData phủ ~300 ngày
SIGNAL_DELAY_S = 60  # tính tín hiệu 60s sau mốc 15 phút — nến phút cuối của nến 15m đã được ghi
CHAT_EXPIRE_S = 600  # câu hỏi chờ não quá 10 phút → lỗi "Quá lâu — gửi lại" (ARCHITECTURE §6)
FED_HOUR_UTC = 6
FED_SINCE = date(2003, 6, 1)  # mức lãi suất trước kỳ họp đầu tiên 2004


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
        # File năm đã tải được thì không đổi nữa → nhớ luôn; "không có file" và file tháng → nhớ 30 phút
        if hit is not None and (("/" not in key and hit[1] is not None) or time.monotonic() - hit[0] < 1800):
            return hit[1]
        try:
            candles: list[RawCandle] | None = await self.hd.fetch_file(key)
        except FileNotAvailable:
            candles = None
        except (Unreachable, Throttled, BadFile) as e:
            return e
        await self.set_source("ok", provider="histdata")  # tải được (kể cả "chưa có file") = nguồn sống
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
                    # Cả lô dùng chung vài file — tải lại cho từng ngày chỉ làm HistData chặn nặng hơn
                    break
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
                # Đủ hay chưa: file của CHÍNH ngày đó phủ tới cuối ngày; hoặc file đó là kỳ đã đóng (năm cũ /
                # tháng đã qua — ngày lễ đóng sớm như 31/12 thì dữ liệu tiếp theo nằm ở file kỳ sau). File tháng
                # đang chạy (có thể là bản nhớ tạm cũ) không được mượn mép của file khác.
                own_key = hd_file_key(day, today)
                closed = "/" not in own_key or (day.year, day.month) < (today.year, today.month)
                edge = max((f[-1].ts for f in files if f), default=None)
                if not (ingest.day_complete(day, own[-1].ts) or (closed and ingest.day_complete(day, edge))):
                    continue  # HistData chưa phát hành tới hết ngày này (trễ ~1 tuần)
                summary = await ingest.store_histdata_day(s, day, ingest.slice_and_fill(day, data, until=edge))
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
                # Trễ tính tới lúc NHẬN giá, không phải lúc gửi — request chậm 3s là trễ thêm 3s
                lag = (datetime.now(UTC) - q.updated_at).total_seconds()
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

    # --- F. Tín hiệu 15 phút → job dự đoán (v2) ---------------------------------------
    async def signal_loop(self) -> None:
        """Mỗi mốc :00/:15/:30/:45 + 60s: tính tín hiệu trên nến 15m vừa đóng, ghi worker_status.signal; có tín
        hiệu và thị trường mở suốt 15 phút tới → job signal_prediction (unique bar_ts — khởi động lại không trùng)."""
        while not self.stop.is_set():
            now = datetime.now(UTC)
            bar_end = signals.bar_boundary(now)
            wait = (bar_end + timedelta(minutes=signals.BAR_MINUTES, seconds=SIGNAL_DELAY_S) - now).total_seconds()
            if (now - bar_end).total_seconds() < SIGNAL_DELAY_S:
                wait = (bar_end + timedelta(seconds=SIGNAL_DELAY_S) - now).total_seconds()
                await self.sleep(wait)
                continue
            async with session_scope() as s:
                last = await feed_repo.get_status(s, "signal")
            if not last or last.get("bar_end") != bar_end.isoformat():
                await self.signal_once(bar_end)
            await self.sleep(max(1.0, wait))

    async def signal_once(self, bar_end: datetime) -> None:
        # Worker vừa khởi động giữa nến: tín hiệu của mốc đã qua > 5 phút không còn là "lúc này" → chỉ ghi trạng thái
        allowed = signals.auto_allowed(bar_end) and (datetime.now(UTC) - bar_end).total_seconds() < 300
        async with session_scope() as s:
            sig = await signals.compute(s, bar_end)
            sig.pop("recent", None)
            job_id = None
            # Nến mới nhất phải là nến vừa đóng ngay trước mốc — 18:00 NY mở cửa thì nến cuối là 16:45 (trước giờ
            # nghỉ / cuối tuần), không phải "nến vừa đóng" (AC-10)
            fresh_bar = sig["last_bar_ts"] == (bar_end - timedelta(minutes=signals.BAR_MINUTES)).isoformat()
            if allowed and fresh_bar and sig["has_signal"]:
                job_id = await job_repo.enqueue(s, "signal_prediction", {"bar_ts": bar_end.isoformat(), "signals": sig})
            await feed_repo.set_status(s, "signal", {**sig, "market_ok": allowed, "job_id": job_id})
        log.info("signal", bar_end=bar_end.isoformat(), fired=[f["name"] for f in sig["fired"]], job=job_id)

    # --- G. Chấm dự đoán + dọn hàng chờ não (v2) -------------------------------------
    async def score_loop(self) -> None:
        """Mỗi phút: chấm dự đoán tới hạn; job 'running' quá 180s (não chết giữa chừng) về pending; câu hỏi chat
        chờ quá 10 phút → lỗi; job dự đoán tự động chờ quá 5 phút → bỏ. Chạy ở worker vì não có thể đang chết."""
        while not self.stop.is_set():
            async with session_scope() as s:
                scored = await predictions.score_due(s)
                await job_repo.requeue_stuck(s)
                expired = await job_repo.expire_pending(
                    s, "chat", CHAT_EXPIRE_S, "Quá lâu — não bot không trả lời trong 10 phút"
                )
                await chat_repo.fail_by_jobs(s, expired, "Quá lâu — não bot không trả lời trong 10 phút. Gửi lại nhé.")
                await chat_repo.fail_orphans(
                    s, "Bot dừng giữa chừng nhiều lần — bấm Gửi lại, hoặc kiểm tra `make brain` trên máy chủ."
                )
                await job_repo.expire_pending(s, "signal_prediction", 300, "Quá hạn — não không xử kịp nến này")
            if scored or expired:
                log.info("scored", predictions=scored, chat_expired=len(expired))
            await self.sleep(60)

    # --- H. Lịch FED (v2) -------------------------------------------------------------
    async def fed_loop(self) -> None:
        """Lúc khởi động + mỗi ngày 06:00 UTC: FRED + lịch FOMC tĩnh → upsert fed_decision. FRED hỏng → giữ bảng cũ."""
        fred = FredClient()
        try:
            while not self.stop.is_set():
                async with session_scope() as s:
                    prev = await feed_repo.get_status(s, "fed") or {}
                try:
                    rows = fed_svc.build_decisions(await fred.target_rates(FED_SINCE))
                    async with session_scope() as s:
                        await fed_repo.upsert_many(s, rows)
                        n = await fed_repo.count(s)
                        await feed_repo.set_status(
                            s, "fed", {"loaded_at": datetime.now(UTC).isoformat(), "count": n, "last_error": None}
                        )
                    log.info("fed_loaded", meetings=n)
                except Exception as e:  # noqa: BLE001 — lỗi gì cũng ghi cho S7 rồi chờ lượt sau
                    log.warning("fed_failed", error=str(e), kind=type(e).__name__)
                    async with session_scope() as s:
                        await feed_repo.set_status(s, "fed", {**prev, "last_error": f"{type(e).__name__}: {e}"[:300]})
                now = datetime.now(UTC)
                nxt = now.replace(hour=FED_HOUR_UTC, minute=0, second=0, microsecond=0)
                if nxt <= now:
                    nxt += timedelta(days=1)
                await self.sleep((nxt - now).total_seconds())
        finally:
            await fred.close()

    async def cleanup_loop(self) -> None:
        while not self.stop.is_set():
            async with session_scope() as s:
                await feed_repo.cleanup(s)
            await self.sleep(86400)

    async def run(self) -> None:
        # Hai worker (vd chạy tay thêm một lần, hoặc make dev cùng lúc production) cùng tải một file, cộng đôi
        # attempts → giữ advisory lock suốt đời worker; không lấy được thì thoát ngay
        self.lock_conn = await engine.connect()
        got = (await self.lock_conn.execute(text("SELECT pg_try_advisory_lock(20260924)"))).scalar()
        if not got:
            log.error("worker_already_running")
            await self.lock_conn.close()
            await engine.dispose()
            raise SystemExit("Đã có một worker GoldPulse khác đang chạy trên kho này — dừng.")
        async with session_scope() as s:
            await feed_repo.set_status_if_absent(s, "first_run", {"at": datetime.now(UTC).isoformat()})
        log.info("worker_start", history_start=str(settings.history_start_date))
        tasks = [
            asyncio.create_task(self._guard(self.history_loop), name="history"),
            asyncio.create_task(self._guard(self.realtime_loop), name="realtime"),
            asyncio.create_task(self._guard(self.backfill_loop), name="backfill"),
            asyncio.create_task(self._guard(self.cleanup_loop), name="cleanup"),
            asyncio.create_task(self._guard(self.signal_loop), name="signal"),
            asyncio.create_task(self._guard(self.score_loop), name="score"),
            asyncio.create_task(self._guard(self.fed_loop), name="fed"),
        ]
        await self.stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.duka.close()
        await self.hd.close()
        await self.lock_conn.close()
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
            except Exception as e:
                log.exception("loop_crashed", loop=loop_fn.__name__)
                await record_error(f"worker/{loop_fn.__name__}", e)
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
