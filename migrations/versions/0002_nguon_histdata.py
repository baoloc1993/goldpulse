"""nguồn lịch sử HistData (không cần WARP)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-25
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_candle_source", "candle_1m", type_="check")
    op.drop_constraint("ck_candle_final_source", "candle_1m", type_="check")
    op.create_check_constraint("ck_candle_source", "candle_1m", "source IN ('dukascopy', 'histdata', 'goldapi')")
    op.create_check_constraint("ck_candle_final_source", "candle_1m", "is_final = (source <> 'goldapi')")


def downgrade() -> None:
    op.execute("DELETE FROM candle_1m WHERE source = 'histdata'")
    op.drop_constraint("ck_candle_source", "candle_1m", type_="check")
    op.drop_constraint("ck_candle_final_source", "candle_1m", type_="check")
    op.create_check_constraint("ck_candle_source", "candle_1m", "source IN ('dukascopy', 'goldapi')")
    op.create_check_constraint("ck_candle_final_source", "candle_1m", "is_final = (source = 'dukascopy')")
