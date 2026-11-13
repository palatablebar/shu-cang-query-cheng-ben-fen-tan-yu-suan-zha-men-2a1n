"""预算借用台账（纯函数，可单测，不依赖数据库/网络）。

口径
----
- 预占：管理员通过、且尚未过期/撤回的申请，从转出团队预算中锁定 `amount_yuan`；
- 有效预算 = 配置预算 + 借入 - 转出预占；
- 剩余额度 = 有效预算 - 本期已用（最近一次核算的分摊金额）；
- 可借出余额 = 剩余额度（审批时的硬约束，并发审批必须在同一转出团队上串行）。

所有金额按分（2 位小数）比较，避免浮点尾差把「刚好借满」误判为超额。
"""
from __future__ import annotations

from datetime import datetime

from src.constants.enums import ACTIVE_TRANSFER_STATUSES, TransferStatus

MONEY_EPSILON = 0.005


def is_active(transfer, now: datetime) -> bool:
    """approved 且未到期才形成预占；到期即使状态还没被任务翻成 expired 也不再占额度。"""
    if getattr(transfer, "status", None) not in ACTIVE_TRANSFER_STATUSES:
        return False
    expires_at = getattr(transfer, "expires_at", None)
    return expires_at is None or expires_at > now


def expired_now(transfer, now: datetime) -> bool:
    """待审批/已通过且已过有效期：两种状态都应视为已过期并释放占用。"""
    expires_at = getattr(transfer, "expires_at", None)
    return (
        getattr(transfer, "status", None)
        in (TransferStatus.PENDING.value, TransferStatus.APPROVED.value)
        and expires_at is not None
        and expires_at <= now
    )


def _round(value: float) -> float:
    return round(float(value or 0.0), 2)


def team_ledger(team_id: int, budget_yuan: float, transfers, used_yuan: float, now: datetime) -> dict:
    """单团队台账：借入 / 转出预占 / 有效预算 / 已用 / 剩余 / 可借出。"""
    borrowed = 0.0
    lent = 0.0
    active_ids: list[int] = []
    for transfer in transfers:
        if not is_active(transfer, now):
            continue
        amount = _round(getattr(transfer, "amount_yuan", 0.0))
        if int(getattr(transfer, "borrower_team_id", -1)) == int(team_id):
            borrowed += amount
        lender_id = getattr(transfer, "lender_team_id", None)
        if lender_id is not None and int(lender_id) == int(team_id):
            lent += amount
        active_ids.append(int(getattr(transfer, "id", 0)))

    borrowed = _round(borrowed)
    lent = _round(lent)
    budget = _round(budget_yuan)
    used = _round(used_yuan)
    effective = _round(budget + borrowed - lent)
    remaining = _round(effective - used)
    usage_ratio = round(used / effective, 4) if effective > 0 else None
    return {
        "team_id": int(team_id),
        "budget_yuan": budget,
        "borrowed_yuan": borrowed,
        "lent_yuan": lent,
        "effective_budget_yuan": effective,
        "used_yuan": used,
        "remaining_yuan": remaining,
        "usage_ratio": usage_ratio,
        "over_budget": used > effective + MONEY_EPSILON,
        "active_transfer_ids": sorted(active_ids),
    }


def fits_available(amount_yuan: float, available_yuan: float) -> bool:
    """审批硬约束：申请金额不能超过转出团队当前可借出余额。"""
    return float(amount_yuan) <= float(available_yuan) + MONEY_EPSILON


def available_after(available_yuan: float, amount_yuan: float) -> float:
    return _round(float(available_yuan) - float(amount_yuan))


def reconciliation_status(used_yuan: float, effective_budget_yuan: float) -> str:
    """对账结果：查询成本是否被有效预算覆盖。"""
    return "over" if float(used_yuan) > float(effective_budget_yuan) + MONEY_EPSILON else "ok"


def transfer_snapshot(transfers, now: datetime | None = None) -> list[dict]:
    """把核算时刻**生效**的调拨固化成可追溯快照（不含后续修改）。

    只有 approved 且未过期的记录才计入；pending/rejected/withdrawn/expired 不进快照。
    """
    now = now or datetime.utcnow()
    rows = []
    for transfer in transfers:
        if not is_active(transfer, now):
            continue
        rows.append(
            {
                "id": int(getattr(transfer, "id", 0)),
                "transfer_no": getattr(transfer, "transfer_no", ""),
                "borrower_team_id": int(getattr(transfer, "borrower_team_id", 0)),
                "lender_team_id": (
                    int(transfer.lender_team_id) if getattr(transfer, "lender_team_id", None) is not None else None
                ),
                "amount_yuan": _round(getattr(transfer, "amount_yuan", 0.0)),
                "expires_at": (
                    getattr(transfer, "expires_at").isoformat(timespec="seconds")
                    if getattr(transfer, "expires_at", None)
                    else None
                ),
            }
        )
    return sorted(rows, key=lambda row: row["id"])
