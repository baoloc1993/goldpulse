"""cặp mới độc lập với vàng: inst_candle · inst_feed · inst_signal · inst_account · inst_trade · inst_day
(chỉ THÊM bảng, không đụng bảng vàng) + seed 4 tài khoản BTC-A..BTC-D

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-03
"""

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# 4 chiến thuật khác biệt nhất của research/btc_strategies.py (chốt 2026-10-03)
ACCOUNTS = [
    ("BTC-A", "Mô hình", {"kind": "model", "min_conf": "thr_top5", "hold_hours": 1}),
    ("BTC-B", "Thuận xu hướng", {"kind": "model_trend", "min_conf": "thr_top10", "hold_hours": 1}),
    ("BTC-C", "Đà nến giật", {"kind": "spike", "k_sigma": 3.0, "hold_hours": 4}),
    ("BTC-D", "Nén rồi bung", {"kind": "squeeze", "window": 720, "quantile": 0.2, "hold_hours": 4}),
]


def upgrade() -> None:
    op.create_table(
        "inst_candle",
        sa.Column("symbol", sa.Text, primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("open", sa.Float, nullable=False),
        sa.Column("high", sa.Float, nullable=False),
        sa.Column("low", sa.Float, nullable=False),
        sa.Column("close", sa.Float, nullable=False),
        sa.Column("volume", sa.Float, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("inserted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "inst_feed",
        sa.Column("symbol", sa.Text, primary_key=True),
        sa.Column("polled_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("price", sa.Float),
        sa.Column("last_closed_ts", sa.DateTime(timezone=True)),
        sa.Column("lag_seconds", sa.Float),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("error", sa.Text),
    )
    op.create_table(
        "inst_signal",
        sa.Column("symbol", sa.Text, primary_key=True),
        sa.Column("bar_ts", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("close", sa.Float, nullable=False),
        sa.Column("values", JSONB, nullable=False),
        sa.Column("fired", JSONB, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
    )
    op.create_table(
        "inst_account",
        sa.Column("code", sa.Text, primary_key=True),
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("strategy", JSONB, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("start_equity", sa.Float, nullable=False, server_default="10"),
        sa.Column("leverage", sa.Float, nullable=False, server_default="20"),
        sa.Column("fee_side", sa.Float, nullable=False, server_default="0.0005"),
        sa.Column("slip_side", sa.Float, nullable=False, server_default="0.0002"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "inst_trade",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("account_code", sa.Text, sa.ForeignKey("inst_account.code"), nullable=False),
        sa.Column("bar_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trade_date", sa.Date, nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("direction", sa.SmallInteger, nullable=False),
        sa.Column("entry_price", sa.Float, nullable=False),
        sa.Column("notional", sa.Float, nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("exit_price", sa.Float),
        sa.Column("exit_reason", sa.Text),
        sa.Column("pnl", sa.Float),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("skip_reason", sa.Text),
        sa.Column("context", JSONB, nullable=False, server_default="{}"),
        sa.CheckConstraint("direction IN (1, -1)", name="ck_inst_direction"),
        sa.CheckConstraint("status IN ('open', 'closed', 'skipped')", name="ck_inst_status"),
        sa.CheckConstraint(
            "exit_reason IS NULL OR exit_reason IN ('horizon', 'day_close', 'stopout')", name="ck_inst_exit"
        ),
        sa.CheckConstraint(
            "skip_reason IS NULL OR skip_reason IN ('open_trade', 'burned', 'disabled', 'stale')", name="ck_inst_skip"
        ),
        # Một giờ tín hiệu tối đa một dòng mỗi tài khoản — worker khởi động lại không mở trùng
        sa.UniqueConstraint("account_code", "bar_ts", name="ux_inst_trade_account_bar"),
    )
    op.create_index("ix_inst_trade_account_id", "inst_trade", ["account_code", "id"])
    op.create_index(
        "ux_inst_trade_open", "inst_trade", ["account_code"], unique=True, postgresql_where=sa.text("status = 'open'")
    )
    op.create_table(
        "inst_day",
        sa.Column("account_code", sa.Text, sa.ForeignKey("inst_account.code"), primary_key=True),
        sa.Column("trade_date", sa.Date, primary_key=True),
        sa.Column("start_equity", sa.Float, nullable=False),
        sa.Column("end_equity", sa.Float, nullable=False),
        sa.Column("realized_pnl", sa.Float, nullable=False, server_default="0"),
        sa.Column("n_trades", sa.Integer, nullable=False, server_default="0"),
        sa.Column("n_wins", sa.Integer, nullable=False, server_default="0"),
        sa.Column("burned", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    for code, name, strategy in ACCOUNTS:
        op.execute(
            sa.text(
                "INSERT INTO inst_account (code, symbol, name, strategy) VALUES (:c, 'BTCUSDT', :n, CAST(:s AS jsonb))"
            ).bindparams(c=code, n=name, s=json.dumps(strategy))
        )


def downgrade() -> None:
    op.execute("DELETE FROM worker_status WHERE key IN ('btc', 'btc_telegram')")
    for t in ("inst_day", "inst_trade", "inst_account", "inst_signal", "inst_feed", "inst_candle"):
        op.drop_table(t)
