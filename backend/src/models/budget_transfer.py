"""跨团队预算借用：转出团队把预算额度借给借用团队，审批通过后形成「预占」。

状态机：pending -> approved -> withdrawn / expired；pending -> rejected。
只有 approved 且未过期的记录计入转出团队的预占；撤回/过期/驳回即释放预占。
申请时可不指定转出团队（lender_team_id 为空），由管理员审批时按剩余额度指派。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class BudgetTransfer(Base):
    __tablename__ = "budget_transfers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    transfer_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    borrower_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    lender_team_id: Mapped[int | None] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), index=True, nullable=True
    )
    amount_yuan: Mapped[float] = mapped_column(Float, default=0.0)
    project: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    requested_by: Mapped[str] = mapped_column(String(160), index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(160), nullable=True)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
