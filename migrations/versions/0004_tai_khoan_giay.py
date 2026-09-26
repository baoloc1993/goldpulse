"""vòng 3 — tài khoản giấy: paper_account · paper_trade · paper_day (chỉ THÊM bảng) + seed 4 tài khoản A–D

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-27
"""

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# PRD §7 "Tài khoản giấy (v3)" — 4 luật = 4 giả thuyết; tham số từ research/paper_filters.py (DECISIONS 2026-09-27)
ACCOUNTS = [
    ("A", "Gốc", {"kind": "all", "horizon_minutes": 15}),
    ("B", "Biến động", {"kind": "volatility", "min_vol": 2.0, "horizon_minutes": 15}),
    ("C", "Tự tin", {"kind": "confidence", "min_conf": 0.0852, "horizon_minutes": 15}),
    ("D", "1 giờ", {"kind": "all", "horizon_minutes": 60}),
]


def upgrade() -> None:
    op.create_table(
        "paper_account",
        sa.Column("code", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("rule", JSONB, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("start_equity", sa.Float, nullable=False, server_default="10"),
        sa.Column("oz", sa.Float, nullable=False, server_default="1"),
        sa.Column("leverage", sa.Integer, nullable=False, server_default="1000"),
        sa.Column("spread", sa.Float, nullable=False, server_default="0.3"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "paper_trade",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("account_code", sa.Text, sa.ForeignKey("paper_account.code"), nullable=False),
        sa.Column("prediction_id", sa.Integer, sa.ForeignKey("prediction.id"), nullable=False),
        sa.Column("trade_date", sa.Date, nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("direction", sa.Text, nullable=False),
        sa.Column("oz", sa.Float, nullable=False),
        sa.Column("entry_price", sa.Float, nullable=False),
        sa.Column("spread", sa.Float, nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("exit_price", sa.Float),
        sa.Column("exit_reason", sa.Text),
        sa.Column("pnl", sa.Float),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("skip_reason", sa.Text),
        sa.Column("context", JSONB, nullable=False, server_default="{}"),
        sa.CheckConstraint("direction IN ('up', 'down')", name="ck_paper_direction"),
        sa.CheckConstraint("status IN ('open', 'closed', 'skipped')", name="ck_paper_status"),
        sa.CheckConstraint(
            "exit_reason IS NULL OR exit_reason IN ('horizon', 'stopout', 'market_close', 'disabled')",
            name="ck_paper_exit",
        ),
        sa.CheckConstraint(
            "skip_reason IS NULL OR skip_reason IN "
            "('rule', 'open_trade', 'margin', 'burned', 'disabled', 'market_closing', 'stale')",
            name="ck_paper_skip",
        ),
        # Một dự đoán tối đa một lệnh mỗi tài khoản (ARCHITECTURE §6c)
        sa.UniqueConstraint("account_code", "prediction_id", name="ux_paper_trade_account_prediction"),
    )
    op.create_index("ix_paper_trade_account_id", "paper_trade", ["account_code", "id"])
    # Tối đa một lệnh đang mở mỗi tài khoản
    op.create_index(
        "ux_paper_trade_open", "paper_trade", ["account_code"], unique=True, postgresql_where=sa.text("status = 'open'")
    )
    op.create_table(
        "paper_day",
        sa.Column("account_code", sa.Text, sa.ForeignKey("paper_account.code"), primary_key=True),
        sa.Column("trade_date", sa.Date, primary_key=True),
        sa.Column("start_equity", sa.Float, nullable=False),
        sa.Column("end_equity", sa.Float, nullable=False),
        sa.Column("realized_pnl", sa.Float, nullable=False, server_default="0"),
        sa.Column("n_trades", sa.Integer, nullable=False, server_default="0"),
        sa.Column("n_wins", sa.Integer, nullable=False, server_default="0"),
        sa.Column("burned", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    for code, name, rule in ACCOUNTS:
        op.execute(
            sa.text("INSERT INTO paper_account (code, name, rule) VALUES (:c, :n, CAST(:r AS jsonb))").bindparams(
                c=code, n=name, r=json.dumps(rule)
            )
        )


def downgrade() -> None:
    op.drop_table("paper_day")
    op.drop_table("paper_trade")
    op.drop_table("paper_account")
