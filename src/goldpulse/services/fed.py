"""FED: ghép mức lãi suất FRED với lịch FOMC tĩnh + phản ứng vàng (ARCHITECTURE §6b, §7 H).

Không gọi HTTP — worker đưa chuỗi FRED vào `build_decisions`.
"""

import json
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..integrations.fred import RatePoint
from ..repositories import fed as fed_repo
from ..repositories import feed as feed_repo
from .calendar import NY

FOMC_FILE = Path(__file__).resolve().parents[3] / "data" / "fomc_meetings.json"
ANNOUNCE = time(14, 0)  # giờ New York; zoneinfo tự đổi giờ mùa hè
FIRST_MEETING = date(2004, 1, 1)


@lru_cache(maxsize=1)
def meetings() -> list[dict[str, Any]]:
    """Lịch FOMC trong repo (biên soạn từ federalreserve.gov): [{date, kind, label}] cũ → mới."""
    data = json.loads(FOMC_FILE.read_text(encoding="utf-8"))
    return [
        {**m, "date": date.fromisoformat(m["date"])}
        for m in sorted(data["meetings"], key=lambda m: m["date"])
        if m["date"] >= FIRST_MEETING.isoformat()
    ]


def announced_at(day: date, local: str | None = None) -> datetime:
    """14:00 New York ngày họp; kỳ khẩn có giờ công bố riêng (`announce_local`, giờ NY) trong lịch."""
    if local:
        return datetime.fromisoformat(local).replace(tzinfo=NY).astimezone(UTC)
    return datetime.combine(day, ANNOUNCE, tzinfo=NY).astimezone(UTC)


def next_meeting(now: datetime) -> dict[str, Any] | None:
    """Kỳ họp kế tiếp = kỳ chưa công bố (giờ công bố > now) — ngày họp sau 14:00 NY là kỳ SAU, không "còn 0 ngày"."""
    today = now.astimezone(NY).date()
    for m in meetings():
        if announced_at(m["date"], m.get("announce_local")) > now:
            return {"date": m["date"].isoformat(), "days": (m["date"] - today).days, "label": m["label"]}
    return None


def _bp(a: float, b: float) -> int:
    return int(round((b - a) * 100))


def build_decisions(points: list[RatePoint]) -> list[dict[str, Any]]:
    """Một dòng fed_decision cho mỗi kỳ họp đã có mức lãi suất sau họp trên FRED.

    Mức trước = điểm FRED cuối cùng TRƯỚC ngày họp; mức sau = điểm đầu tiên trong (ngày họp, ngày họp + 2] có mức
    KHÁC mức trước — FRED ghi theo NGÀY HIỆU LỰC: thường hôm sau, có khi +2 (cuộc gọi 02/03/2020, công bố 03/03,
    hiệu lực 04/03); không đổi thì lấy điểm đầu tiên sau ngày họp (kỳ vừa họp hôm qua hiện ngay, không chờ +3).
    Không vượt sang ngày trước kỳ họp kế tiếp để khỏi đếm lẫn thay đổi của kỳ sau. Ngày FRED đổi mức mà không nằm trong
    [họp, họp + 2 ngày] của kỳ nào (cắt khẩn giữa hai kỳ) → dòng `unscheduled` riêng.
    """
    if not points:
        return []
    pts = sorted(points, key=lambda p: p.day)
    days = [p.day for p in pts]

    def last_before(d: date) -> RatePoint | None:
        lo, hi = 0, len(days)
        while lo < hi:
            mid = (lo + hi) // 2
            if days[mid] < d:
                lo = mid + 1
            else:
                hi = mid
        return pts[lo - 1] if lo else None

    def first_after(d: date) -> RatePoint | None:
        lo, hi = 0, len(days)
        while lo < hi:
            mid = (lo + hi) // 2
            if days[mid] <= d:
                lo = mid + 1
            else:
                hi = mid
        return pts[lo] if lo < len(pts) else None

    rows: list[dict[str, Any]] = []
    cal = [m for m in meetings() if m["date"] < pts[-1].day]  # cần điểm FRED sau ngày họp
    all_days = [m["date"] for m in meetings()]
    for m in cal:
        later = [d for d in all_days if d > m["date"]]
        cap = (
            min(m["date"] + timedelta(days=2), later[0] - timedelta(days=1)) if later else m["date"] + timedelta(days=2)
        )
        before, after = last_before(m["date"]), first_after(m["date"])
        if not before or not after:
            continue
        # Mức đổi trong cửa sổ hiệu lực (≤ cap) → lấy điểm đổi đầu tiên; không đổi → giữ điểm ngay sau ngày họp
        p = after
        while p.day <= max(cap, m["date"]) and (p.upper, p.lower) == (before.upper, before.lower):
            nxt = first_after(p.day)
            if nxt is None or nxt.day > max(cap, m["date"]):
                break
            p = nxt
        if (p.upper, p.lower) != (before.upper, before.lower):
            after = p
        rows.append(
            {
                "meeting_date": m["date"],
                "announced_at": announced_at(m["date"], m.get("announce_local")),
                "rate_lower": after.lower,
                "rate_upper": after.upper,
                "change_bp": _bp(before.upper, after.upper),
                "kind": m["kind"],
                "source_note": m["label"],
            }
        )
    for prev, cur in zip(pts, pts[1:], strict=False):
        if not cal or cur.day < cal[0]["date"]:
            continue
        if cur.upper == prev.upper and cur.lower == prev.lower:
            continue
        if any(md <= cur.day <= md + timedelta(days=2) for md in all_days):
            continue
        rows.append(
            {
                "meeting_date": cur.day,
                "announced_at": announced_at(cur.day),
                "rate_lower": cur.lower,
                "rate_upper": cur.upper,
                "change_bp": _bp(prev.upper, cur.upper),
                "kind": "unscheduled",
                "source_note": "FRED đổi mức ngoài lịch FOMC (giờ công bố ước 14:00 NY)",
            }
        )
    return rows


def _delta(a: float | None, b: float | None) -> float | None:
    return round(b - a, 2) if a is not None and b is not None else None


def reaction_row(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "meeting_date": r["meeting_date"].isoformat(),
        "announced_at": r["announced_at"].isoformat(),
        "rate_lower": r["rate_lower"],
        "rate_upper": r["rate_upper"],
        "change_bp": r["change_bp"],
        "kind": r["kind"],
        "price_at": round(r["p0"], 2) if r["p0"] is not None else None,
        "d15m": _delta(r["p0"], r["p15"]),
        "d1h": _delta(r["p0"], r["p60"]),
        "d1d": _delta(r["p0"], r["p1d"]),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Trung bình phản ứng + số lần tăng, chỉ trên kỳ có đủ nến ở mốc đó (ghi rõ số kỳ có dữ liệu)."""
    out: dict[str, Any] = {"meetings": len(rows)}
    for key in ("d15m", "d1h", "d1d"):
        vals = [r[key] for r in rows if r[key] is not None]
        out[key] = {
            "n": len(vals),
            "avg": round(sum(vals) / len(vals), 2) if vals else None,
            "up": sum(v > 0 for v in vals),
        }
    return out


async def listing(session: AsyncSession) -> list[dict[str, Any]]:
    return [reaction_row(r) for r in await fed_repo.with_reactions(session)]


async def pack(session: AsyncSession, now: datetime) -> dict[str, Any]:
    """Phần FED của gói dữ liệu cho não: 8 kỳ gần nhất, 8 lần hạ + 8 lần tăng gần nhất (tóm tắt), kỳ kế tiếp."""
    recent = [reaction_row(r) for r in await fed_repo.with_reactions(session, limit=8, before=now)]
    cuts = [reaction_row(r) for r in await fed_repo.with_reactions(session, limit=8, change="cut", before=now)]
    hikes = [reaction_row(r) for r in await fed_repo.with_reactions(session, limit=8, change="hike", before=now)]
    return {
        "next_meeting": next_meeting(now),
        "recent": recent,
        "last_cuts": {"rows": cuts, "summary": summarize(cuts)},
        "last_hikes": {"rows": hikes, "summary": summarize(hikes)},
        "note": "Phản ứng = giá vàng tại mốc − giá ngay trước giờ công bố (14:00 New York; kỳ khẩn theo giờ thật)"
        " (USD, nến phút có giao dịch trong kho; kỳ công bố lúc thị trường đóng không có số)",
    }


async def screen(session: AsyncSession, now: datetime | None = None) -> dict[str, Any]:
    """Dữ liệu S7: kỳ kế tiếp · thẻ nguồn (lần nạp, số kỳ, lỗi FRED) · mọi kỳ họp mới nhất trước + phản ứng."""
    now = now or datetime.now(UTC)
    st = await feed_repo.get_status(session, "fed") or {}
    w = await feed_repo.get_status(session, "worker")
    return {
        "next_meeting": next_meeting(now),
        "source": {
            "loaded_at": st.get("loaded_at"),
            "count": st.get("count"),
            "last_error": st.get("last_error"),
        },
        "rows": await listing(session),
        "worker_alive": bool(w and (now - w["updated_at"]).total_seconds() < 120),
    }
