"""Schema request/response — tách khỏi model DB."""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

SessionName = Literal["asia", "europe", "us"]


class SessionQuery(BaseModel):
    session: SessionName
    year_from: int = Field(ge=2000, le=2100)
    year_to: int = Field(ge=2000, le=2100)
    dow: int | None = Field(default=None, ge=1, le=5, description="ISO: 1 = thứ Hai … 5 = thứ Sáu")

    @model_validator(mode="after")
    def _order(self) -> "SessionQuery":
        if self.year_from > self.year_to:
            raise ValueError("Năm bắt đầu phải trước hoặc bằng năm kết thúc")
        return self


class SessionQueryResult(BaseModel):
    count: int
    avg_range: float | None
    up_pct: float | None
    down_pct: float | None
    flat_pct: float | None
    first_day: str | None
    last_day: str | None
    duration_ms: float
    span_years: int


class RetryResult(BaseModel):
    day: str
    queued: bool


class Health(BaseModel):
    ok: bool


Json = dict[str, Any]
