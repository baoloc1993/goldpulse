"""(v4) BTC — luồng tiền/dữ liệu chưa có ở test_btc.py: sổ ngày UTC, ngưỡng cháy hai chiều, day_close, GO/PIVOT,
đóng lệnh khi thiếu nến, gửi hai lần, gộp nến 1h, công thức tín hiệu, tin Telegram, phân trang, S10, migration.

Tiền giả. Mọi kịch bản truyền `now` tường minh (giờ UTC); dữ liệu tự dựng trong test. Tài khoản 10 USD, x20 →
lệnh 200 USD, phí + trượt khứ hồi 0,14% → ngưỡng cháy (10 − 200 × 0,0014) / 200 = 4,86%.
"""

import asyncio
import math
import statistics
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from goldpulse.repositories import inst as repo
from goldpulse.services.btc import SYMBOL, paper
from goldpulse.services.btc import features as feat
from goldpulse.services.btc import notify as btc_notify
from goldpulse.services.btc import strategies as strat
from tests import test_btc as _base
from tests.conftest import needs_db
from tests.test_btc import BAR, DAY, KEY, P0, H, M, _candles, _rows, _sig

ROOT = Path(__file__).resolve().parents[1]
db = _base.db  # fixture dùng chung với test_btc.py (TRUNCATE inst_*, bật lại tài khoản)
client = _base.client


async def _enter(s, bar: datetime, entry: float = P0, now: datetime | None = None, **fired) -> tuple[int, int]:
    """Nến 1m cuối của giờ `bar` (giá vào) rồi mở lệnh theo tín hiệu — đúng `now` = 1 phút sau khi lệnh mở."""
    await _candles(s, bar + 59 * M, [entry])
    return await paper.open_from_signal(s, _sig(bar, **fired), entry, now or bar + H + M)


async def _run(s, bar: datetime, prices: list[float], dips: dict[int, float] | None = None, hours: int = 1) -> int:
    """Nến 1m từ lúc lệnh mở (bar + 1h), rồi chạy luồng đóng lệnh ngay sau hạn."""
    await _candles(s, bar + H, prices, dips)
    return await paper.close_open(s, bar + (hours + 1) * H + M)


# --- (1) sổ ngày --------------------------------------------------------------------------------------------------


@needs_db
async def test_ngay_moi_dat_lai_10_usd_va_lenh_thu_hai_tinh_tren_so_du_sau_lenh_truoc(db):
    """Hỏng nếu số dư hôm qua chuyển sang hôm nay, hoặc lệnh thứ hai trong ngày vẫn 20 × 10 thay vì 20 × số dư mới."""
    b1, b2, b3 = BAR, BAR + 2 * H, BAR + 24 * H  # 10:00 và 12:00 ngày 1/10; 10:00 ngày 2/10
    async with db() as s:
        await _enter(s, b1, model=1)
        assert await _run(s, b1, [P0] * 59 + [P0 * 1.005]) == 1  # +0,5% → +0,72 USD
        d1 = await repo.day_get(s, "BTC-A", DAY)
        assert d1["end_equity"] == pytest.approx(10.72, abs=1e-4)

        await _enter(s, b2, model=1)
        t2 = (await _rows(s, "BTC-A"))[-1]
        assert t2["notional"] == pytest.approx(20 * 10.72, abs=1e-3)
        assert t2["context"]["equity"] == pytest.approx(10.72, abs=1e-4)
        await _run(s, b2, [P0] * 60)  # đứng giá → chỉ mất phí 0,14% trên 214,4
        t2 = (await _rows(s, "BTC-A"))[-1]
        assert t2["pnl"] == pytest.approx(-214.4 * 0.0014, abs=1e-4)
        d1 = await repo.day_get(s, "BTC-A", DAY)
        assert d1["n_trades"] == 2 and d1["n_wins"] == 1
        assert d1["end_equity"] == pytest.approx(10.72 - 0.30016, abs=1e-4)

        await _enter(s, b3, model=1)  # ngày UTC mới
        t3 = (await _rows(s, "BTC-A"))[-1]
        assert t3["notional"] == pytest.approx(200.0) and t3["context"]["equity"] == pytest.approx(10.0)
        await _run(s, b3, [P0] * 60)
        d2 = await repo.day_get(s, "BTC-A", date(2026, 10, 2))
        assert d2["start_equity"] == pytest.approx(10.0) and d2["end_equity"] == pytest.approx(10.0 - 0.28, abs=1e-4)
        assert (await repo.day_get(s, "BTC-A", DAY))["end_equity"] == pytest.approx(10.72 - 0.30016, abs=1e-4)


# --- (2)(3) ngưỡng cháy hai chiều ----------------------------------------------------------------------------------


@needs_db
@pytest.mark.parametrize(
    ("fired", "factor", "burns"),
    [
        (1, 1 - 0.0485, False),  # mua, low sát TRÊN ngưỡng 4,86% → còn sống
        (1, 1 - 0.0487, True),  # mua, low sát DƯỚI ngưỡng → cháy
        (-1, 1 + 0.0485, False),  # bán, high sát dưới ngưỡng
        (-1, 1 + 0.0487, True),  # bán, high sát trên ngưỡng → cháy theo HIGH
        (1, 1.20, False),  # mua mà giá vọt lên 20% (high) → không phải bất lợi
        (-1, 0.70, False),  # bán mà giá rơi 30% (low) → không phải bất lợi
    ],
    ids=["mua-sat-tren", "mua-chay", "ban-sat-duoi", "ban-chay", "mua-vot-len", "ban-roi-xuong"],
)
async def test_nguong_chay_dung_huong_va_dung_nguong(db, fired, factor, burns):
    """Hỏng nếu cháy nhầm khi giá chạy THEO chiều lệnh, hoặc không cháy khi đi ngược chiều quá 4,86%."""
    async with db() as s:
        await _enter(s, BAR, model=fired)
        await _run(s, BAR, [P0] * 60, dips={30: P0 * factor})
        t = (await _rows(s, "BTC-A"))[0]
        day = await repo.day_get(s, "BTC-A", DAY)
        assert t["status"] == "closed"
        if burns:
            assert t["exit_reason"] == "stopout" and t["pnl"] == pytest.approx(-10.0)
            assert t["exit_price"] == pytest.approx(P0 * (1 - fired * 0.0486), abs=0.01)
            assert t["closed_at"] == BAR + H + 31 * M  # nến chạm ngưỡng đóng lúc k.ts + 1m
            assert day["burned"] and day["end_equity"] == 0
        else:
            assert t["exit_reason"] == "horizon" and t["pnl"] == pytest.approx(-0.28, abs=1e-4)
            assert not day["burned"]


@needs_db
async def test_cu_giat_o_nen_vao_khong_tinh_nhung_o_nen_dong_lenh_thi_chay(db):
    """Nến giá vào là điểm khớp → low của chính nó không được tính; nến đóng lệnh thì vẫn kiểm cháy trước `horizon`.
    Lệnh thứ hai cháy mất trọn số dư CÒN LẠI sau lệnh trước (không phải 10)."""
    b2 = BAR + 2 * H
    async with db() as s:
        await _candles(s, BAR + 59 * M, [P0], {0: P0 * 0.9})  # low −10% ngay nến vào
        await paper.open_from_signal(s, _sig(BAR, model=1), P0, BAR + H + M)
        await _run(s, BAR, [P0] * 60)
        t1 = (await _rows(s, "BTC-A"))[0]
        assert t1["exit_reason"] == "horizon" and t1["pnl"] == pytest.approx(-0.28, abs=1e-4)

        await _enter(s, b2, model=1)
        await _run(s, b2, [P0] * 60, dips={59: P0 * 0.9})  # đúng nến 59 = nến đóng lệnh
        t2 = (await _rows(s, "BTC-A"))[-1]
        assert t2["exit_reason"] == "stopout" and t2["closed_at"] == b2 + 2 * H
        assert t2["pnl"] == pytest.approx(-9.72, abs=1e-4)
        d = await repo.day_get(s, "BTC-A", DAY)
        assert d["burned"] and d["end_equity"] == 0 and d["realized_pnl"] == pytest.approx(-10.0, abs=1e-4)


@needs_db
async def test_ngay_chay_khong_mo_them_nhung_ngay_sau_van_choi(db):
    async with db() as s:
        await _enter(s, BAR, model=1)
        await _run(s, BAR, [P0] * 60, dips={5: P0 * 0.9})
        for k in (2, 5, 12):  # 12:00, 15:00, 22:00 cùng ngày → đều bỏ qua vì đã cháy
            await _enter(s, BAR + k * H, model=1)
        rows = await _rows(s, "BTC-A")
        assert [r["skip_reason"] for r in rows] == [None, "burned", "burned", "burned"]
        assert (await repo.open_trade_of(s, "BTC-A")) is None
        nxt = BAR + 24 * H
        assert await _enter(s, nxt, model=1) == (1, 0)  # 00:00 UTC qua rồi → ngày mới, 10 USD mới
        assert (await _rows(s, "BTC-A"))[-1]["notional"] == pytest.approx(200.0)


# --- (3) hướng bán: lãi/lỗ ---------------------------------------------------------------------------------------


@needs_db
@pytest.mark.parametrize(
    ("fired", "exit_factor", "pnl"),
    [
        (-1, 0.995, 200 * (0.005 - 0.0014)),  # bán, giá giảm 0,5% → lãi
        (-1, 1.01, -200 * (0.01 + 0.0014)),  # bán, giá tăng 1% → lỗ (chưa cháy)
        (1, 0.99, -200 * (0.01 + 0.0014)),  # đối chứng chiều mua
    ],
    ids=["ban-lai", "ban-lo", "mua-lo"],
)
async def test_lai_lo_theo_huong_lenh(db, fired, exit_factor, pnl):
    async with db() as s:
        await _enter(s, BAR, model=fired)
        await _run(s, BAR, [P0] * 59 + [P0 * exit_factor])
        t = (await _rows(s, "BTC-A"))[0]
        assert t["direction"] == fired and t["exit_reason"] == "horizon"
        assert t["pnl"] == pytest.approx(pnl, abs=1e-4)


# --- (4) day_close ------------------------------------------------------------------------------------------------


@needs_db
async def test_lenh_4h_dung_ranh_gioi_nua_dem_horizon_hay_day_close(db):
    """Giờ 19:00 giữ 4h hết đúng 24:00 → vẫn là `horizon` (tính đánh giá); giờ 20:00 vượt nửa đêm → `day_close` ở nến
    23:59, KHÔNG tính vào eval_n; lệnh 1h ở giờ 22:00 vẫn là `horizon`. Chưa tới nến 23:59 thì chưa được đóng."""
    c19 = datetime(2026, 10, 1, 19, 0, tzinfo=UTC)
    a22 = datetime(2026, 10, 1, 22, 0, tzinfo=UTC)
    c20 = datetime(2026, 10, 2, 20, 0, tzinfo=UTC)
    async with db() as s:
        await _enter(s, c19, spike=1)
        await _run(s, c19, [P0 * 1.002] * 240, hours=4)
        c1 = (await _rows(s, "BTC-C"))[0]
        assert c1["exit_reason"] == "horizon" and c1["closed_at"] == datetime(2026, 10, 2, 0, 0, tzinfo=UTC)

        await _enter(s, a22, model=1)
        await _run(s, a22, [P0 * 1.002] * 60)
        a1 = (await _rows(s, "BTC-A"))[0]
        assert a1["exit_reason"] == "horizon" and a1["trade_date"] == DAY

        await _enter(s, c20, spike=1)
        await _candles(s, c20 + H, [P0 * 1.002] * 150)  # 21:00..23:29
        assert await paper.close_open(s, datetime(2026, 10, 2, 23, 30, tzinfo=UTC)) == 0  # chưa tới 23:59
        await _candles(s, c20 + H + 150 * M, [P0 * 1.002] * 30)  # …23:59
        assert await paper.close_open(s, datetime(2026, 10, 3, 0, 1, tzinfo=UTC)) == 1
        c2 = (await _rows(s, "BTC-C"))[-1]
        assert c2["exit_reason"] == "day_close" and c2["closed_at"] == datetime(2026, 10, 3, 0, 0, tzinfo=UTC)
        assert c2["trade_date"] == date(2026, 10, 2)
        assert (await repo.day_get(s, "BTC-C", date(2026, 10, 2)))["n_trades"] == 1
        assert await repo.day_get(s, "BTC-C", date(2026, 10, 3)) is None  # không rò sang ngày sau

        tot_c = await repo.totals(s, "BTC-C")
        assert tot_c["trades"] == 2 and tot_c["eval_n"] == 1
        assert tot_c["eval_pnl"] == pytest.approx(c1["pnl"], abs=1e-4)  # lãi của day_close không vào số đánh giá
        assert (await repo.totals(s, "BTC-A"))["eval_n"] == 1


# --- (5) GO / PIVOT ở biên ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("t", "expected"),
    [
        (
            {"pnl": 0.0, "eval_pnl": 0.0, "eval_n": 100, "days": 50, "burned_days": 0},
            "none",
        ),  # lãi đúng 0 → chưa phải lãi
        ({"pnl": -1.0, "eval_pnl": -1.0, "eval_n": 100, "days": 50, "burned_days": 0}, "none"),
        ({"pnl": 0.0001, "eval_pnl": 0.0001, "eval_n": 29, "days": 29, "burned_days": 0}, "pivot"),  # 29 lệnh
        ({"pnl": 0.0001, "eval_pnl": 0.0001, "eval_n": 30, "days": 30, "burned_days": 0}, "go"),  # đúng 30 lệnh
        ({"pnl": 5.0, "eval_pnl": 5.0, "eval_n": 30, "days": 10, "burned_days": 2}, "go"),  # cháy đúng 20%
        ({"pnl": 5.0, "eval_pnl": 5.0, "eval_n": 30, "days": 15, "burned_days": 3}, "go"),
        ({"pnl": 5.0, "eval_pnl": 5.0, "eval_n": 30, "days": 14, "burned_days": 3}, "pivot"),  # 21,4% > 20%
        ({"pnl": 5.0, "eval_pnl": 5.0, "eval_n": 30, "days": 10, "burned_days": 3}, "pivot"),
        (
            {"pnl": 5.0, "eval_pnl": 5.0, "eval_n": 40, "days": 0, "burned_days": 0},
            "go",
        ),  # chưa có ngày nào: không chia cho 0
        # Dấu lãi theo Σ các ngày (day_close vẫn tính vào số dư — PRD §6): lãi cộng dồn > 0 dù lệnh đánh giá lỗ nhẹ
        ({"pnl": 1.0, "eval_pnl": -2.0, "eval_n": 30, "days": 30, "burned_days": 0}, "go"),
        ({"pnl": -1.0, "eval_pnl": 2.0, "eval_n": 30, "days": 30, "burned_days": 0}, "none"),
    ],
)
def test_verdict_o_bien_29_30_lenh_chay_20_phan_tram_lai_bang_0(t, expected):
    assert paper.verdict(t) == expected


@needs_db
@pytest.mark.parametrize(
    ("n_eval", "n_day_close", "burned", "expected_n", "expected"),
    [
        (30, 0, 0, 30, "go"),
        (29, 1, 0, 29, "pivot"),  # lệnh thứ 30 là day_close → không được đếm
        (30, 0, 6, 30, "go"),  # 6/30 = 20%
        (30, 0, 7, 30, "pivot"),  # 7/30 > 20%
    ],
    ids=["du-30", "30-nhung-1-day_close", "chay-20pct", "chay-23pct"],
)
async def test_verdict_tu_so_that_loai_day_close_va_dem_ngay_chay(
    db, n_eval, n_day_close, burned, expected_n, expected
):
    """Từ sổ lệnh/ngày thật qua `totals` rồi `verdict`: day_close không làm tròn 30 lệnh; ngày cháy đếm theo ngày."""
    async with db() as s:
        base = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        for i in range(n_eval + n_day_close):
            day = (base + timedelta(days=i)).date()
            is_dc = i >= n_eval
            is_burn = i < burned
            reason = "day_close" if is_dc else "stopout" if is_burn else "horizon"
            pnl = -10.0 if is_burn else 5.0
            tid = await repo.insert_trade(
                s,
                {
                    "account_code": "BTC-A",
                    "bar_ts": base + timedelta(days=i),
                    "trade_date": day,
                    "opened_at": base + timedelta(days=i, hours=1),
                    "direction": 1,
                    "entry_price": P0,
                    "notional": 200.0,
                    "due_at": base + timedelta(days=i, hours=2),
                    "status": "open",
                    "skip_reason": None,
                    "context": {},
                },
            )
            closed_at = base + timedelta(days=i, hours=2)
            assert await repo.close_trade(s, tid, exit_price=P0, reason=reason, pnl=pnl, closed_at=closed_at)
            await repo.day_ensure(s, "BTC-A", day, 10.0)
            await repo.day_apply(s, "BTC-A", day, pnl, is_burn)
        tot = await repo.totals(s, "BTC-A")
        assert tot["eval_n"] == expected_n and tot["trades"] == n_eval + n_day_close
        assert tot["burned_days"] == burned
        assert paper.verdict(tot) == expected


# --- (6) đóng lệnh thiếu nến, chạy hai lần ---------------------------------------------------------------------


@needs_db
async def test_thieu_nen_dong_lenh_doi_du_10_phut_roi_dong_o_nen_cuoi_co(db):
    """Nến 11:59 không về: trước 12:10 lệnh còn mở; từ 12:10 và nguồn đã có nến sau đó thì đóng ở nến cuối có."""
    async with db() as s:
        await _enter(s, BAR, model=1)
        await _candles(s, BAR + H, [P0] * 30 + [P0 * 1.002])  # chỉ có 11:00..11:30, nến cuối giá +0,2%
        assert await paper.close_open(s, BAR + H + 20 * M) == 0  # chưa tới hạn
        assert await paper.close_open(s, BAR + 2 * H + 9 * M) == 0  # quá hạn 9 phút: vẫn chờ
        assert (await repo.open_trade_of(s, "BTC-A")) is not None
        # hết ân hạn nhưng nguồn chưa đi qua phút 11:59 (chưa có nến nào sau đó: Binance/worker đang chết) → vẫn chờ
        assert await paper.close_open(s, BAR + 2 * H + 10 * M) == 0
        await _candles(s, BAR + 2 * H + 5 * M, [P0 * 1.5])  # nguồn có lại, nến 12:05 (giá lạ, KHÔNG được dùng để đóng)
        assert await paper.close_open(s, BAR + 2 * H + 10 * M) == 1
        t = (await _rows(s, "BTC-A"))[0]
        assert t["exit_reason"] == "horizon" and t["exit_price"] == pytest.approx(P0 * 1.002)
        assert t["closed_at"] == BAR + H + 31 * M
        assert t["pnl"] == pytest.approx(200 * (0.002 - 0.0014), abs=1e-4)


@needs_db
async def test_dong_lenh_hai_lan_khong_dong_doi_khong_nhan_doi_so_ngay(db):
    """Worker tick hai lần / hai tiến trình cùng đóng một lệnh: sổ ngày chỉ cộng MỘT lần."""
    async with db() as s:
        await _enter(s, BAR, model=1)
        assert await _run(s, BAR, [P0] * 59 + [P0 * 1.005]) == 1
        assert await paper.close_open(s, BAR + 5 * H) == 0
        assert await paper.close_open(s, BAR + 5 * H) == 0
        d = await repo.day_get(s, "BTC-A", DAY)
        assert d["n_trades"] == 1 and d["n_wins"] == 1 and d["realized_pnl"] == pytest.approx(0.72, abs=1e-4)
        # đóng thẳng một lệnh đã đóng (tiến trình thứ hai đến sau) → bị từ chối, sổ ngày không đổi
        acc = next(a for a in await repo.accounts(s, SYMBOL) if a["code"] == "BTC-A")
        t = (await _rows(s, "BTC-A"))[0]
        assert await paper._close(s, acc, t, P0, "horizon", BAR + 2 * H) is False
        d2 = await repo.day_get(s, "BTC-A", DAY)
        assert d2["n_trades"] == 1 and d2["realized_pnl"] == d["realized_pnl"]

        # cháy rồi chạy lại: vẫn đúng một lần cháy, một lần trừ tiền
        b2 = BAR + 2 * H
        await _enter(s, b2, model=1)
        await _run(s, b2, [P0] * 60, dips={3: P0 * 0.9})
        await paper.close_open(s, b2 + 9 * H)
        d3 = await repo.day_get(s, "BTC-A", DAY)
        assert d3["n_trades"] == 2 and d3["burned"] and d3["end_equity"] == 0


@needs_db
async def test_mat_tron_nen_trong_khung_giu_lenh_van_phai_duoc_dong(db):
    async with db() as s:
        await _enter(s, BAR, model=1)
        await _candles(s, BAR + 2 * H + 5 * M, [P0] * 10)  # nguồn có lại sau hạn, khung 11:00–11:59 trống hẳn
        n = await paper.close_open(s, BAR + 6 * H)
        assert n == 1


@needs_db
async def test_gui_tin_hieu_hai_lan_khong_tao_lenh_trung_ke_ca_khi_lenh_cu_da_dong(db):
    """Worker khởi động lại xử lại cùng một giờ: mỗi tài khoản vẫn đúng một dòng — không mở lại sau khi đã đóng."""
    sig = _sig(BAR, model=1, model_trend=1)
    async with db() as s:
        await _candles(s, BAR + 59 * M, [P0])
        assert await paper.open_from_signal(s, sig, P0, BAR + H + M) == (2, 0)
        assert await paper.open_from_signal(s, sig, P0, BAR + H + 2 * M) == (0, 0)
        await _run(s, BAR, [P0] * 60)
        assert await paper.open_from_signal(s, sig, P0, BAR + 2 * H + M) == (0, 0)
        for code in ("BTC-A", "BTC-B"):
            rows = await _rows(s, code)
            assert len(rows) == 1 and rows[0]["status"] == "closed"
        assert (await repo.day_get(s, "BTC-A", DAY))["n_trades"] == 1
        # dòng bỏ qua cũng không nhân đôi
        await repo.set_enabled(s, "BTC-A", False)
        bar2 = BAR + 3 * H
        await _candles(s, bar2 + 59 * M, [P0])
        sig2 = _sig(bar2, model=1)
        assert await paper.open_from_signal(s, sig2, P0, bar2 + H + M) == (0, 1)
        assert await paper.open_from_signal(s, sig2, P0, bar2 + H + M) == (0, 0)
        # tín hiệu: ghi hai lần chỉ còn một hàng
        assert await repo.insert_signal(s, SYMBOL, BAR, BAR, P0, {"x": 1}, {"model": 1}, "m") is True
        assert await repo.insert_signal(s, SYMBOL, BAR, BAR, P0, {"x": 2}, {"model": -1}, "m") is False
        got = (await s.execute(text("SELECT fired FROM inst_signal WHERE bar_ts = :b"), {"b": BAR})).all()
        assert [r[0] for r in got] == [{"model": 1}]  # hàng đầu giữ nguyên, không bị ghi đè


# --- (7) nến 1m ---------------------------------------------------------------------------------------------------


@needs_db
async def test_nap_nen_lai_khong_nhan_doi_va_gop_1h_dung_open_high_low_close_volume(db):
    t10 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    t12 = t10 + 2 * H
    rows = [
        (t10 + 2 * M, 105.0, 108.0, 101.0, 106.0, 3.0),  # cố ý nạp lộn thứ tự
        (t10, 100.0, 110.0, 99.0, 104.0, 1.0),
        (t10 + M, 104.0, 107.0, 100.0, 105.0, 2.0),
        (t12, 200.0, 210.0, 190.0, 205.0, 5.0),  # giờ 11:00 không có phút nào
        (t12 + 59 * M, 205.0, 207.0, 195.0, 206.0, 7.0),
        (t10 - M, 1.0, 1.0, 1.0, 1.0, 1.0),  # 09:59 — ngoài khoảng
        (t10 + 3 * H, 9.0, 9.0, 9.0, 9.0, 9.0),  # 13:00 — mốc cuối loại trừ
    ]
    async with db() as s:
        assert await repo.upsert_candles(s, SYMBOL, []) == 0
        await repo.upsert_candles(s, SYMBOL, rows)
        await repo.upsert_candles(s, SYMBOL, rows)  # lấy bù đè
        await repo.upsert_candles(s, "ETHUSDT", [(t10, 5.0, 5.0, 5.0, 5.0, 5.0)])  # cặp khác, cùng giờ
        n = (await s.execute(text("SELECT count(*) FROM inst_candle WHERE symbol = :s"), {"s": SYMBOL})).scalar()
        assert n == len(rows)
        bars = await repo.hourly_bars(s, SYMBOL, t10, t10 + 3 * H)
        assert [b["bar_ts"] for b in bars] == [t10, t12]  # giờ rỗng bị bỏ, không bịa nến
        b10, b12 = bars
        assert (b10["open"], b10["high"], b10["low"], b10["close"], b10["volume"]) == (100.0, 110.0, 99.0, 106.0, 6.0)
        assert (b12["open"], b12["high"], b12["low"], b12["close"], b12["volume"]) == (200.0, 210.0, 190.0, 206.0, 12.0)
        # nguồn sửa lại một nến đã đóng → bản mới thắng, không tạo hàng mới
        await repo.upsert_candles(s, SYMBOL, [(t10 + 2 * M, 105.0, 120.0, 101.0, 118.0, 4.0)])
        b10 = (await repo.hourly_bars(s, SYMBOL, t10, t10 + H))[0]
        assert (b10["high"], b10["close"], b10["volume"]) == (120.0, 118.0, 7.0)


# --- (8) công thức tín hiệu --------------------------------------------------------------------------------------


def _bars_from(closes: list[float], start: datetime = datetime(2026, 5, 1, tzinfo=UTC)) -> list[dict]:
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(
            {
                "bar_ts": start + i * H,
                "open": o,
                "high": max(o, c) * 1.001,
                "low": min(o, c) * 0.999,
                "close": c,
                "volume": 100.0 + i % 7,
            }
        )
        prev = c
    return out


def _walk(n: int) -> list[float]:
    """Bước ngẫu nhiên tất định (LCG) ±0,5% mỗi giờ."""
    x, p, out = 12345, 50_000.0, []
    for _ in range(n):
        x = (x * 1103515245 + 12345) % 2**31
        p *= 1 + ((x >> 8) % 2001 - 1000) / 200_000
        out.append(p)
    return out


def _lr(closes: list[float]) -> list[float]:
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]


def test_cong_thuc_dac_trung_khop_so_tinh_tay():
    # quantile nội suy tuyến tính (như pandas), không phụ thuộc thứ tự đầu vào
    assert feat.quantile([4, 1, 3, 2], 0.2) == pytest.approx(1.6)
    assert feat.quantile([4, 1, 3, 2], 0.5) == pytest.approx(2.5)
    assert feat.quantile([4, 1, 3, 2], 0.0) == 1 and feat.quantile([4, 1, 3, 2], 1.0) == 4
    # rolling cần đủ cửa sổ và không có NaN; độ lệch chuẩn MẪU (ddof=1): [1,2,4] → sqrt(7/3)
    assert feat.rolling([1, 2, None, 4, 5], 2, sum) == [None, 3, None, None, 9]
    assert feat.rolling([1.0, 2.0, 4.0], 3, feat._std)[2] == pytest.approx(math.sqrt(7 / 3))
    # EMA 24h kiểu adjust=True, span 3 (a = 0,5): 1 · (2+0,5)/(1,5) · (3+0,5·2,5)/1,75
    assert feat.ewm_span_adjust([1.0, 2.0, 3.0], 3) == pytest.approx([1.0, 2.5 / 1.5, 4.25 / 1.75])
    # RSI n=2 (alpha 0,5): lãi [_,1,0,1], lỗ [_,0,.5,0] → ewm adjust=False
    r = feat.rsi([10.0, 11.0, 10.5, 11.5], 2)
    assert r[0] is None and r[1] == 100.0
    assert r[2] == pytest.approx(100 - 100 / 3) and r[3] == pytest.approx(100 - 100 / 7)


def test_dac_trung_mot_nen_hinh_dang_gio_va_cuoi_tuan():
    bars = [
        {
            "bar_ts": datetime(2026, 10, 3, 6, 0, tzinfo=UTC),
            "open": 100.0,
            "high": 110.0,
            "low": 90.0,
            "close": 105.0,
            "volume": 1.0,
        },
        {
            "bar_ts": datetime(2026, 10, 3, 7, 0, tzinfo=UTC),
            "open": 105.0,
            "high": 105.0,
            "low": 105.0,
            "close": 105.0,
            "volume": 1.0,
        },
    ]
    f0, f1 = feat.compute(bars)
    assert (
        f0["body"] == pytest.approx(0.25) and f0["upwick"] == pytest.approx(0.25) and f0["lowick"] == pytest.approx(0.5)
    )
    assert f0["hsin"] == pytest.approx(1.0) and f0["hcos"] == pytest.approx(0.0, abs=1e-12)
    assert f0["wkend"] == 1.0  # thứ Bảy 3/10/2026
    assert f1["r1"] == pytest.approx(0.0) and f1["body"] is None  # biên độ 0 → NaN, không chia cho 0
    assert feat.compute([{**bars[0], "bar_ts": datetime(2026, 10, 1, tzinfo=UTC)}])[0]["wkend"] == 0.0  # thứ Năm


def test_chua_du_nen_thi_khong_chien_thuat_nao_bat():
    out = strat.evaluate(_bars_from(_walk(100)))
    assert all(not any(r["fired"].values()) for r in out) and not out[-1]["values"]["ok"]


@pytest.mark.parametrize(
    ("k", "expected"),
    [(3.05, 1), (-3.05, -1), (2.95, 0), (-2.95, 0)],
    ids=["tang-3.05sigma", "giam-3.05sigma", "tang-2.95-chua-du", "giam-2.95-chua-du"],
)
def test_spike_bat_khi_nen_giat_tu_3_sigma_cua_24_nen_truoc(k, expected):
    closes = _walk(780)
    sigma_prev = statistics.stdev(_lr(closes)[-24:])  # σ24 CỦA NẾN TRƯỚC, tính bằng thư viện chuẩn
    closes.append(closes[-1] * math.exp(k * sigma_prev))
    last = strat.evaluate(_bars_from(closes), only_last=True)[0]
    assert last["values"]["ok"] and last["values"]["z1h"] == pytest.approx(k, rel=1e-6)
    assert last["fired"]["spike"] == expected


def _squeeze_series(final_factor: float, calm_amp: float = 0.0001) -> list[float]:
    walk = _walk(760)
    level = walk[-1]
    calm = [level * (1 + calm_amp * (-1) ** k) for k in range(30)]
    return walk + calm + [level * final_factor]


@pytest.mark.parametrize(
    ("final_factor", "expected"),
    [(1.01, 1), (0.99, -1), (1.0001, 0)],
    ids=["bung-len", "bung-xuong", "van-trong-dai"],
)
def test_squeeze_chi_bat_khi_nen_truoc_hep_va_close_ra_ngoai_dai(final_factor, expected):
    last = strat.evaluate(_bars_from(_squeeze_series(final_factor)), only_last=True)[0]
    v = last["values"]
    assert v["ok"] and v["bb_width_prev"] < v["bb_q20_prev"]  # đã nén: độ rộng nến trước < phân vị 20%
    assert last["fired"]["squeeze"] == expected


def test_squeeze_khong_bat_khi_nen_truoc_khong_hep_du_close_vot_ra_ngoai():
    walk = _walk(760)
    wide = [walk[-1] * (1 + 0.05 * (-1) ** k) for k in range(30)]  # 30 nến dao động ±5% → dải rất rộng
    last = strat.evaluate(_bars_from(walk + wide + [walk[-1] * 1.10]), only_last=True)[0]
    v = last["values"]
    assert v["bb_width_prev"] > v["bb_q20_prev"]
    assert last["fired"]["squeeze"] == 0


@pytest.mark.parametrize("ramp", [1.01, 0.99], ids=["xu-huong-len", "xu-huong-xuong"])
@pytest.mark.parametrize(
    ("p", "model_on", "trend_possible"),
    [
        (0.5 + 0.14, True, True),  # conf ≥ thr_top5 (0,1322)
        (0.5 - 0.14, True, True),
        (0.5 + 0.124, False, True),  # thr_top10 (0,1166) ≤ conf < thr_top5 → chỉ mô hình + xu hướng
        (0.5 - 0.124, False, True),
        (0.5 + 0.10, False, False),  # dưới cả hai ngưỡng
        (0.5 - 0.10, False, False),
    ],
)
def test_nguong_mo_hinh_va_thuan_xu_huong(monkeypatch, ramp, p, model_on, trend_possible):
    md = strat.model()
    assert md["thr_top10"] < 0.124 < md["thr_top5"] < 0.14  # giả định của bảng tham số còn đúng
    monkeypatch.setattr(strat, "p_up", lambda f, m: p)
    closes = _walk(780)
    for _ in range(6):
        closes.append(closes[-1] * ramp)
    last = strat.evaluate(_bars_from(closes), only_last=True)[0]
    dm = 1 if p > 0.5 else -1
    gap = last["values"]["ema_gap"]
    assert (gap > 0) == (ramp > 1)
    assert last["fired"]["model"] == (dm if model_on else 0)
    same_side = dm == (1 if gap > 0 else -1)
    assert last["fired"]["model_trend"] == (dm if trend_possible and same_side else 0)


def test_thieu_dac_trung_thi_mo_hinh_khong_ra_xac_suat():
    md = strat.model()
    assert strat.p_up({k: 0.0 for k in md["features"]}, md) is not None
    assert strat.p_up({**{k: 0.0 for k in md["features"]}, md["features"][0]: None}, md) is None


# --- (9) Telegram ------------------------------------------------------------------------------------------------


@needs_db
async def test_telegram_moc_dat_khi_chay_lan_dau_vao_ra_bo_qua_gui_dung_mot_lan(db):
    bar2 = BAR + 3 * H
    async with db() as s:
        # có sẵn một lệnh đã đóng TRƯỚC khi bot bật → không gửi bù
        await _enter(s, BAR, model=1)
        await _run(s, BAR, [P0] * 59 + [P0 * 1.005])
        msgs, state = await btc_notify.collect(s, None, BAR + 2 * H + M)
        assert msgs == [] and state["last_trade_id"] == 1
        msgs, state = await btc_notify.collect(s, state, BAR + 2 * H + 2 * M)
        assert msgs == []

        # giờ mới: A vào, B (đang tắt) bị bỏ qua → MỘT tin
        await repo.set_enabled(s, "BTC-B", False)
        await _candles(s, bar2 + 59 * M, [P0])
        await paper.open_from_signal(s, _sig(bar2, model=1, model_trend=1), P0, bar2 + H + M)
        msgs, state = await btc_notify.collect(s, state, bar2 + H + M)
        assert len(msgs) == 1
        m = msgs[0]
        assert "Vào: A Mô hình MUA" in m and "lệnh 214.40 USD" in m  # 20 × 10,72 (số dư sau lệnh +0,72 trước đó)
        assert "Bỏ qua: B Thuận xu hướng MUA — đang tắt" in m and m.endswith("Tiền giả.")
        msgs, state = await btc_notify.collect(s, state, bar2 + H + 2 * M)
        assert msgs == []  # không gửi lại

        # đóng lệnh: lỗ phí 0,28 → một tin đóng, có số hôm nay và cộng dồn
        await _candles(s, bar2 + H, [P0] * 60)
        await paper.close_open(s, bar2 + 2 * H + M)
        msgs, state = await btc_notify.collect(s, state, bar2 + 2 * H + M)
        assert len(msgs) == 1
        c = msgs[0]
        assert "A Mô hình đóng −" in c and "hết giờ giữ" in c and "Không tính vào" not in c
        # ngày 1/10: +0,72 rồi lệnh 214,4 mất phí 0,14% = −0,30 → hôm nay +0,42, số dư 10,42
        assert "đóng −0.30 USD" in c and "Hôm nay +0.42 (số dư 10.42) · cộng dồn +0.42" in c
        msgs, _ = await btc_notify.collect(s, state, bar2 + 2 * H + 2 * M)
        assert msgs == []


@needs_db
async def test_telegram_chay_va_het_ngay_ghi_ro_ly_do_va_khong_tinh_danh_gia(db):
    a18 = datetime(2026, 10, 1, 18, 0, tzinfo=UTC)
    c20 = datetime(2026, 10, 1, 20, 0, tzinfo=UTC)
    async with db() as s:
        _, state = await btc_notify.collect(s, None, a18)
        await _enter(s, a18, model=1)
        await _run(s, a18, [P0] * 60, dips={10: P0 * 0.9})  # cháy
        await _enter(s, c20, spike=1)
        await _candles(s, c20 + H, [P0 * 0.999] * 180)  # …23:59
        await paper.close_open(s, datetime(2026, 10, 2, 0, 1, tzinfo=UTC))
        msgs, _ = await btc_notify.collect(s, state, datetime(2026, 10, 2, 0, 1, tzinfo=UTC))
        text_all = "\n".join(msgs)
        burn = next(m for m in msgs if "cháy — lỗ hết số dư" in m)
        assert "đóng −10.00 USD" in burn and "Tài khoản cháy hôm nay" in burn and "số dư 0.00" in burn
        dc = next(m for m in msgs if "hết ngày" in m)
        assert "Không tính vào số lệnh đánh giá 8 tuần." in dc and "C Đà nến giật" in dc
        assert text_all.count("Không tính vào") == 1
        assert sum(m.startswith("🟢") for m in msgs) == 2  # hai tin vào lệnh


# --- (10) API / S10 / phân quyền -----------------------------------------------------------------------------------


async def _login(client, nxt: str = "/btc") -> None:
    r = await client.post("/login", data={"key": KEY, "next": nxt})
    assert r.status_code in (302, 303)


@needs_db
async def test_phan_trang_lenh_before_khong_trung_khong_thieu_va_khong_lan_tai_khoan(db, client):
    base = datetime(2026, 9, 1, tzinfo=UTC)
    async with db() as s:
        for code, n in (("BTC-A", 120), ("BTC-B", 3)):
            for i in range(n):
                await repo.insert_trade(
                    s,
                    {
                        "account_code": code,
                        "bar_ts": base + i * H,
                        "trade_date": base.date(),
                        "opened_at": base + i * H + H,
                        "direction": 1,
                        "entry_price": P0,
                        "notional": 200.0,
                        "due_at": base + i * H + 2 * H,
                        "status": "skipped",
                        "skip_reason": "stale",
                        "context": {},
                    },
                )
        ids_a = [r["id"] for r in reversed(await repo.trades_page(s, "BTC-A", None, 500))]
    await _login(client)
    page = (await client.get("/api/btc/accounts/BTC-A")).json()["rows"]
    seen = [r["id"] for r in page]
    assert len(seen) == 50 and seen == sorted(seen, reverse=True)
    while True:
        r = await client.get(f"/api/btc/accounts/BTC-A/trades?before={seen[-1]}")
        assert r.status_code == 200
        rows = r.json()["rows"]
        if not rows:
            break
        seen += [x["id"] for x in rows]
    assert sorted(seen) == ids_a and len(set(seen)) == 120  # đủ 120, không trùng, không lẫn BTC-B
    # tham số hỏng → 422, không 500
    for bad in ("0", "-3", "abc", "9999999999"):
        assert (await client.get(f"/api/btc/accounts/BTC-A/trades?before={bad}")).status_code == 422, bad
    assert (await client.get("/api/btc/accounts/BTC-A/trades")).status_code == 422


@needs_db
async def test_s10_hom_nay_gom_lai_tam_cua_lenh_dang_mo_va_so_du_tai_khoan(db):
    b10, a11 = BAR, BAR + H
    now = BAR + 2 * H + 30 * M  # 12:30
    async with db() as s:
        await _enter(s, b10, model_trend=1)
        await _run(s, b10, [P0] * 60)  # B: đóng đứng giá → −0,28
        await _enter(s, a11, model=1)  # A: mở 12:00, còn mở lúc 12:30
        await repo.insert_feed(s, SYMBOL, now, P0 * 1.01, now - 2 * M, 0.0, True)
        d = await paper.screen(s, now)
        accs = {a["code"]: a for a in d["accounts"]}
        assert accs["BTC-A"]["open"]["unrealized"] == pytest.approx(200 * (0.01 - 0.0014), abs=1e-4)
        assert accs["BTC-A"]["equity"] == pytest.approx(10 + 1.72, abs=1e-2)
        assert accs["BTC-A"]["today"]["pnl"] == 0 and accs["BTC-A"]["today"]["trades"] == 0  # chưa thực hiện
        assert accs["BTC-B"]["today"]["pnl"] == pytest.approx(-0.28, abs=1e-2) and accs["BTC-B"]["open"] is None
        assert d["today_pnl"] == pytest.approx(1.72 - 0.28, abs=1e-2)
        assert d["today_trades"] == 1 and d["today_wins"] == 0


@needs_db
async def test_ma_tai_khoan_chi_nhan_btc_dung_cap_va_khong_nhan_ma_vang(db, client):
    async with db() as s:
        # một cặp tương lai gắn tiền tố BTC- nhưng khác cặp → tuyệt đối không lộ qua màn BTC
        await s.execute(
            text(
                "INSERT INTO inst_account (code, symbol, name, strategy)"
                " VALUES ('BTC-Z', 'ETHUSDT', 'x', CAST('{}' AS jsonb))"
            )
        )
    try:
        async with db() as s:
            for code, want in (("BTC-A", True), ("BTC-Z", False), ("A", False), ("btc-a", False), ("", False)):
                assert await paper.account_exists(s, code) is want, code
            assert await paper.set_enabled(s, "BTC-Z", False) is False
            assert await paper.account_screen(s, "BTC-Z") is None
            z = await repo.get_account(s, "BTC-Z")
            assert z["enabled"] is True  # tắt không thành công cũng không đổi gì
        await _login(client)
        assert (await client.get("/btc/BTC-Z")).status_code == 404
        assert (await client.get("/api/btc/accounts/BTC-Z")).status_code == 404
        assert (await client.post("/api/btc/accounts/BTC-Z/enabled", json={"enabled": False})).status_code == 404
        for bad in ("A", "BTC-AA", "BTC-1", "BTC-a"):
            assert (await client.get(f"/api/btc/accounts/{bad}")).status_code == 422, bad
        assert "BTC-Z" not in [a["code"] for a in (await client.get("/api/btc/accounts")).json()["accounts"]]
    finally:
        async with db() as s:
            await s.execute(text("DELETE FROM inst_account WHERE code = 'BTC-Z'"))


@needs_db
async def test_tat_tai_khoan_giua_lenh_mo_lenh_van_dong_dung_va_tin_hieu_sau_bi_bo_qua(db, client):
    await _login(client)
    async with db() as s:
        await _enter(s, BAR, model=1)
    r = await client.post("/api/btc/accounts/BTC-A/enabled", json={"enabled": False})
    assert r.status_code == 200 and r.json() == {"code": "BTC-A", "enabled": False}
    async with db() as s:
        assert (await repo.open_trade_of(s, "BTC-A")) is not None  # tắt không đóng cưỡng bức, không xoá
        assert await _run(s, BAR, [P0] * 59 + [P0 * 1.005]) == 1
        t = (await _rows(s, "BTC-A"))[0]
        assert t["status"] == "closed" and t["pnl"] == pytest.approx(200 * (0.005 - 0.0014), abs=1e-4)
        assert (await repo.day_get(s, "BTC-A", DAY))["n_trades"] == 1
        bar2 = BAR + 3 * H
        await _candles(s, bar2 + 59 * M, [P0])
        assert await paper.open_from_signal(s, _sig(bar2, model=1), P0, bar2 + H + M) == (0, 1)
        assert (await _rows(s, "BTC-A"))[-1]["skip_reason"] == "disabled"
    acc = next(a for a in (await client.get("/api/btc/accounts")).json()["accounts"] if a["code"] == "BTC-A")
    assert acc["enabled"] is False and acc["total"]["trades"] == 1 and acc["open"] is None


# --- (11) migration ----------------------------------------------------------------------------------------------


@needs_db
async def test_ha_cap_0004_roi_len_head_giu_nguyen_bang_vang_va_dung_lai_bang_btc(db):
    """Rollback vòng 4 chỉ bỏ bảng inst_*; sổ vàng (paper_*, prediction) giữ từng hàng; nâng lại có đủ 4 tài khoản."""
    from goldpulse.db import engine
    from tests.test_paper import _pred

    tables = (("paper_account", "code"), ("paper_day", "account_code, trade_date"), ("paper_trade", "id"))

    async def snap(s) -> dict:
        out = {}
        for name, order in (*tables, ("prediction", "id")):
            out[name] = [dict(r) for r in (await s.execute(text(f"SELECT * FROM {name} ORDER BY {order}"))).mappings()]
        out["status"] = [
            dict(r)
            for r in (await s.execute(text("SELECT key, value FROM worker_status WHERE key = 'paper'"))).mappings()
        ]
        return out

    async with db() as s:
        await s.execute(text("TRUNCATE paper_trade, paper_day, prediction, worker_status RESTART IDENTITY CASCADE"))
        pid = await _pred(s, datetime(2026, 9, 23, 14, 0, tzinfo=UTC))
        pid = pid if isinstance(pid, int) else pid["id"]
        await s.execute(
            text(
                "INSERT INTO paper_trade (account_code, prediction_id, trade_date, opened_at, direction, oz,"
                " entry_price,"
                " spread, due_at, closed_at, exit_price, exit_reason, pnl, status)"
                " VALUES ('A', :p, '2026-09-23', now(), 'up', 1, 4300, 0.3, now(), now(), 4302,"
                " 'horizon', 1.7, 'closed')"
            ),
            {"p": pid},
        )
        await s.execute(
            text(
                "INSERT INTO paper_day (account_code, trade_date, start_equity, end_equity, realized_pnl,"
                " n_trades, n_wins)"
                " VALUES ('A', '2026-09-23', 10, 11.7, 1.7, 1, 1)"
            )
        )
        await s.execute(text("UPDATE paper_account SET enabled = false WHERE code = 'D'"))
        await s.execute(
            text("INSERT INTO worker_status (key, value, updated_at) VALUES ('paper', '{\"n\": 7}', now())")
        )
        before = await snap(s)
    assert len(before["paper_trade"]) == 1 and len(before["paper_day"]) == 1

    await engine.dispose()  # không giữ kết nối nào khi DROP TABLE
    try:
        for target, verb in (("0004", "downgrade"), ("head", "upgrade")):
            await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-m", "alembic", verb, target],
                cwd=ROOT,
                check=True,
                capture_output=True,
                timeout=120,
            )
        async with db() as s:
            assert await snap(s) == before
            accs = await repo.accounts(s, SYMBOL)
            assert [a["code"] for a in accs] == ["BTC-A", "BTC-B", "BTC-C", "BTC-D"]
            assert (await s.execute(text("SELECT count(*) FROM inst_trade"))).scalar() == 0
    finally:
        await engine.dispose()
        async with db() as s:
            await s.execute(text("TRUNCATE paper_trade, paper_day, prediction, worker_status RESTART IDENTITY CASCADE"))
            await s.execute(text("UPDATE paper_account SET enabled = true"))
