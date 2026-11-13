"""跨团队预算借用入参。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class TransferCreateIn(BaseModel):
    borrower_team_id: int | None = Field(default=None, gt=0)
    # 可不指定转出团队：由管理员在审批时按各团队剩余额度指派
    lender_team_id: int | None = Field(default=None, gt=0)
    amount_yuan: float = Field(gt=0, le=1_000_000)
    project: str = Field(min_length=2, max_length=120)
    reason: str | None = Field(default=None, max_length=1000)
    valid_days: int | None = Field(default=None, ge=1, le=180)
    expires_at: datetime | None = None


class TransferDecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=1000)
    # 申请时未指定转出团队则必须在这里指派
    lender_team_id: int | None = Field(default=None, gt=0)


class TransferWithdrawIn(BaseModel):
    note: str | None = Field(default=None, max_length=1000)
