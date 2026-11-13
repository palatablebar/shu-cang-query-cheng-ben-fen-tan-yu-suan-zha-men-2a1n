"""预算台账纯函数单测：预占口径、有效预算、可借余额、过期与对账快照。

不依赖数据库/网络，可在 pytest 里独立运行。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from src.services import budget_ledger

NOW = datetime(2026, 11, 13, 12, 0, 0)


@dataclass
class FakeTransfer:
    id: int
    borrower_team_id: int
    lender_team_id: int
    amount_yuan: float
    status: str = "approved"
    expires_at: datetime | None = None
    transfer_no: str = ""


def _t(transfer_id, borrower, lender, amount, status="approved", expires_in_days=30):
    return FakeTransfer(
        id=transfer_id,
        borrower_team_id=borrower,
        lender_team_id=lender,
        amount_yuan=amount,
        status=status,
        expires_at=NOW + timedelta(days=expires_in_days) if expires_in_days is not None else None,
        transfer_no=f"BT-{transfer_id:03d}",
    )


def test_empty_ledger_equals_configured_budget():
    ledger = budget_ledger.team_ledger(1, 1000.0, [], 200.0, NOW)
    assert ledger["borrowed_yuan"] == 0.0
    assert ledger["lent_yuan"] == 0.0
    assert ledger["effective_budget_yuan"] == 1000.0
    assert ledger["remaining_yuan"] == 800.0
    assert ledger["usage_ratio"] == 0.2
    assert ledger["over_budget"] is False


def test_approved_transfer_is_reserved_for_lender_and_borrower():
    transfer = _t(1, borrower=2, lender=1, amount=400)
    lender = budget_ledger.team_ledger(1, 1000.0, [transfer], 0.0, NOW)
    borrower = budget_ledger.team_ledger(2, 500.0, [transfer], 0.0, NOW)
    assert lender["lent_yuan"] == 400.0
    assert lender["effective_budget_yuan"] == 600.0
    assert lender["remaining_yuan"] == 600.0
    assert borrower["borrowed_yuan"] == 400.0
    assert borrower["effective_budget_yuan"] == 900.0


def test_pending_and_closed_transfers_do_not_reserve():
    transfers = [
        _t(1, 2, 1, 500, status="pending"),
        _t(2, 2, 1, 300, status="rejected"),
        _t(3, 2, 1, 700, status="withdrawn"),
        _t(4, 2, 1, 900, status="expired"),
    ]
    ledger = budget_ledger.team_ledger(1, 1000.0, transfers, 0.0, NOW)
    assert ledger["lent_yuan"] == 0.0
    assert ledger["effective_budget_yuan"] == 1000.0


def test_expired_approved_transfer_releases_reservation():
    stale = _t(1, 2, 1, 500, expires_in_days=None)
    stale.expires_at = NOW - timedelta(seconds=1)
    ledger = budget_ledger.team_ledger(1, 1000.0, [stale], 0.0, NOW)
    assert ledger["lent_yuan"] == 0.0
    assert ledger["effective_budget_yuan"] == 1000.0
    assert budget_ledger.expired_now(stale, NOW) is True
    # pending 过期同样应被识别
    pending = _t(2, 2, 1, 10, status="pending", expires_in_days=None)
    pending.expires_at = NOW
    assert budget_ledger.expired_now(pending, NOW) is True


def test_remaining_is_effective_budget_minus_used():
    transfer = _t(1, 2, 1, 300)
    ledger = budget_ledger.team_ledger(1, 1000.0, [transfer], 500.0, NOW)
    assert ledger["effective_budget_yuan"] == 700.0
    assert ledger["remaining_yuan"] == 200.0


def test_borrowing_more_than_remaining_goes_over_budget():
    transfer = _t(1, 2, 1, 300)
    ledger = budget_ledger.team_ledger(2, 100.0, [transfer], 50.0, NOW)
    # 有效预算 400，已用 50 -> 剩余 350，未超支
    assert ledger["over_budget"] is False
    ledger2 = budget_ledger.team_ledger(2, 10.0, [transfer], 50.0, NOW)
    # 有效预算 310，已用 50 -> 仍不超；把用量抬到 320 才超
    assert ledger2["over_budget"] is False
    ledger3 = budget_ledger.team_ledger(2, 10.0, [transfer], 320.0, NOW)
    assert ledger3["effective_budget_yuan"] == 310.0
    assert ledger3["remaining_yuan"] == -10.0
    assert ledger3["over_budget"] is True


def test_fits_available_boundary_uses_cents():
    assert budget_ledger.fits_available(100.0, 100.0) is True
    assert budget_ledger.fits_available(100.01, 100.0) is False
    # 浮点尾差：99.999999 视为 100
    assert budget_ledger.fits_available(99.999999, 100.0) is True
    assert budget_ledger.available_after(100.0, 40.0) == 60.0


def test_reconciliation_status():
    assert budget_ledger.reconciliation_status(90.0, 100.0) == "ok"
    assert budget_ledger.reconciliation_status(100.0, 100.0) == "ok"
    assert budget_ledger.reconciliation_status(100.01, 100.0) == "over"


def test_transfer_snapshot_only_active_and_sorted():
    active = _t(2, 2, 1, 200)
    pending = _t(1, 2, 1, 100, status="pending")
    rows = budget_ledger.transfer_snapshot([active, pending], NOW)
    assert len(rows) == 1
    assert rows[0]["id"] == 2
    assert rows[0]["amount_yuan"] == 200.0
    assert rows[0]["expires_at"]


def test_ledger_is_deterministic():
    transfers = [_t(1, 2, 1, 120.5), _t(2, 3, 1, 80.25, status="pending")]
    first = budget_ledger.team_ledger(1, 1000.0, transfers, 333.33, NOW)
    second = budget_ledger.team_ledger(1, 1000.0, transfers, 333.33, NOW)
    assert first == second


def test_amounts_round_to_cents():
    transfers = [_t(1, 2, 1, 0.006), _t(2, 2, 1, 0.004)]
    ledger = budget_ledger.team_ledger(1, 1000.0, transfers, 0.0, NOW)
    # 0.006 -> 0.01，0.004 -> 0.00
    assert ledger["lent_yuan"] == 0.01


@pytest.mark.parametrize(
    "budget,used,expected_ratio",
    [(1000.0, 250.0, 0.25), (0.0, 10.0, None), (1.0, 2.0, 2.0)],
)
def test_usage_ratio(budget, used, expected_ratio):
    assert budget_ledger.team_ledger(1, budget, [], used, NOW)["usage_ratio"] == expected_ratio
