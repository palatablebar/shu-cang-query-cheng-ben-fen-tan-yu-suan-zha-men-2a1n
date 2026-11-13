"""跨团队预算借用服务：申请、审批、撤回、过期释放与团队台账。

并发安全
--------
审批在**同一转出团队**上串行：先对 `teams` 行加 `FOR UPDATE` 锁，再重新读取该团队
当前生效的调拨与本期已用，按「剩余额度」校验申请金额。两个并发审批不可能看到同一份
可借余额后各自通过——后到的那个会等锁、再基于已提交的预占重新计算。

可追溯
------
每次状态变化都通过服务返回值交给控制器写领域审计（transfer.*）；
核算时把生效调拨冻结进 `charges.transfer_snapshot`，后续调预算/撤回不会改写历史账单。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.constants.enums import Role, TransferStatus
from src.models.budget_transfer import BudgetTransfer
from src.models.charge import Charge
from src.models.cost_run import CostRun
from src.models.team import Team
from src.services import budget_ledger
from src.utils.errors import AppError, forbidden, not_found
from src.utils.timeutil import iso_utc, local_now, utcnow

DEFAULT_VALID_DAYS = 30
MAX_VALID_DAYS = 180
MIN_AMOUNT_YUAN = 0.01
MAX_AMOUNT_YUAN = 1_000_000.0


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #
def effective_status(transfer: BudgetTransfer, now: datetime | None = None) -> str:
    """展示用状态：已通过但过了有效期，即使后台任务还没扫到，也显示为已过期。"""
    now = now or utcnow()
    if budget_ledger.expired_now(transfer, now):
        return TransferStatus.EXPIRED.value
    return transfer.status


def serialize(transfer: BudgetTransfer, teams: dict[int, Team], now: datetime | None = None) -> dict:
    now = now or utcnow()
    borrower = teams.get(transfer.borrower_team_id)
    lender = teams.get(transfer.lender_team_id)
    status = effective_status(transfer, now)
    return {
        "id": transfer.id,
        "transfer_no": transfer.transfer_no,
        "borrower_team_id": transfer.borrower_team_id,
        "borrower_team_code": borrower.code if borrower else None,
        "borrower_team_name": borrower.name if borrower else None,
        "lender_team_id": transfer.lender_team_id,
        "lender_team_code": lender.code if lender else None,
        "lender_team_name": lender.name if lender else None,
        "amount_yuan": round(transfer.amount_yuan, 2),
        "project": transfer.project,
        "reason": transfer.reason,
        "status": status,
        "requested_by": transfer.requested_by,
        "requested_at": iso_utc(transfer.requested_at),
        "decided_by": transfer.decided_by,
        "decided_at": iso_utc(transfer.decided_at),
        "decision_note": transfer.decision_note,
        "expires_at": iso_utc(transfer.expires_at),
        "closed_at": iso_utc(transfer.closed_at),
        "close_reason": transfer.close_reason,
    }


# --------------------------------------------------------------------------- #
# 取数与权限
# --------------------------------------------------------------------------- #
def _all_teams(db: Session) -> dict[int, Team]:
    return {team.id: team for team in db.scalars(select(Team)).all()}


def _is_party(principal: dict, transfer: BudgetTransfer) -> bool:
    own = principal.get("team_id")
    if own is None:
        return False
    own = int(own)
    if own == int(transfer.borrower_team_id):
        return True
    return transfer.lender_team_id is not None and own == int(transfer.lender_team_id)


def assert_transfer_visible(principal: dict, transfer: BudgetTransfer) -> None:
    if principal.get("role") == Role.VIEWER.value and not _is_party(principal, transfer):
        raise forbidden({"reason": "team_scope", "transfer_id": transfer.id})


def get_transfer(db: Session, transfer_id: int, principal: dict) -> BudgetTransfer:
    transfer = db.get(BudgetTransfer, transfer_id)
    if transfer is None:
        raise not_found("budget_transfer", transfer_id)
    assert_transfer_visible(principal, transfer)
    return transfer


def latest_completed_run(db: Session) -> CostRun | None:
    return db.scalar(
        select(CostRun)
        .where(CostRun.status.in_(("success", "partial")))
        .order_by(CostRun.id.desc())
        .limit(1)
    )


def used_by_team(db: Session) -> tuple[dict[int, float], int | None]:
    """最近一次完成核算的团队已用（冻结金额）；没有核算则为 0。"""
    run = latest_completed_run(db)
    if run is None:
        return {}, None
    rows = db.execute(select(Charge.team_id, Charge.amount_yuan).where(Charge.run_id == run.id)).all()
    return {int(team_id): float(amount or 0.0) for team_id, amount in rows}, run.id


def load_transfers(db: Session, *, team_ids: list[int] | None = None) -> list[BudgetTransfer]:
    stmt = select(BudgetTransfer)
    if team_ids is not None:
        stmt = stmt.where(
            or_(BudgetTransfer.borrower_team_id.in_(team_ids), BudgetTransfer.lender_team_id.in_(team_ids))
        )
    stmt = stmt.order_by(BudgetTransfer.id.desc()).execution_options(populate_existing=True)
    return list(db.scalars(stmt).all())


def scan_team_ids(principal: dict) -> list[int] | None:
    if principal.get("role") == Role.VIEWER.value:
        own = principal.get("team_id")
        if own is None:
            raise forbidden({"reason": "team_missing"})
        return [int(own)]
    return None


# --------------------------------------------------------------------------- #
# 台账
# --------------------------------------------------------------------------- #
def build_ledgers(db: Session, principal: dict) -> dict:
    """当前台账：每团队借入/转出预占/有效预算/已用/剩余，以及最近一次核算的对账结果。"""
    scan_ids = scan_team_ids(principal)
    team_stmt = select(Team).order_by(Team.id)
    if scan_ids is not None:
        team_stmt = team_stmt.where(Team.id.in_(scan_ids))
    teams = list(db.scalars(team_stmt).all())
    team_ids = [team.id for team in teams]

    used_map, run_id = used_by_team(db)
    transfers = load_transfers(db, team_ids=team_ids) if team_ids else []
    now = utcnow()

    ledgers = [
        budget_ledger.team_ledger(team.id, team.budget_yuan, transfers, used_map.get(team.id, 0.0), now)
        for team in teams
    ]
    for ledger, team in zip(ledgers, teams):
        ledger["code"] = team.code
        ledger["name"] = team.name
        ledger["owner"] = team.owner
        ledger["cost_center"] = team.cost_center

    # 最近一次核算冻结的对账结果（按团队）
    reconciliation: dict[int, dict] = {}
    if run_id is not None:
        charge_stmt = select(Charge).where(Charge.run_id == run_id)
        if scan_ids is not None:
            charge_stmt = charge_stmt.where(Charge.team_id.in_(scan_ids))
        for charge in db.scalars(charge_stmt).all():
            if not charge.reconciliation:
                continue
            reconciliation[charge.team_id] = {
                "run_id": run_id,
                "team_id": charge.team_id,
                **charge.reconciliation,
                "transfer_snapshot": charge.transfer_snapshot or [],
            }

    totals = {
        "budget_yuan": round(sum(item["budget_yuan"] for item in ledgers), 2),
        "borrowed_yuan": round(sum(item["borrowed_yuan"] for item in ledgers), 2),
        "lent_yuan": round(sum(item["lent_yuan"] for item in ledgers), 2),
        "effective_budget_yuan": round(sum(item["effective_budget_yuan"] for item in ledgers), 2),
        "used_yuan": round(sum(item["used_yuan"] for item in ledgers), 2),
        "remaining_yuan": round(sum(item["remaining_yuan"] for item in ledgers), 2),
    }
    totals["usage_ratio"] = (
        round(totals["used_yuan"] / totals["effective_budget_yuan"], 4)
        if totals["effective_budget_yuan"] > 0
        else None
    )

    active_rows = db.scalar(
        select(func.count())
        .select_from(BudgetTransfer)
        .where(
            BudgetTransfer.status == TransferStatus.APPROVED.value,
            or_(BudgetTransfer.expires_at.is_(None), BudgetTransfer.expires_at > now),
        )
    ) or 0
    pending_rows = db.scalar(
        select(func.count())
        .select_from(BudgetTransfer)
        .where(
            BudgetTransfer.status == TransferStatus.PENDING.value,
            or_(BudgetTransfer.expires_at.is_(None), BudgetTransfer.expires_at > now),
        )
    ) or 0

    return {
        "items": ledgers,
        "totals": totals,
        "reconciliation": reconciliation,
        "run_id": run_id,
        "active_transfers": active_rows,
        "pending_transfers": pending_rows,
        "server_time": iso_utc(now),
    }


# --------------------------------------------------------------------------- #
# 申请
# --------------------------------------------------------------------------- #
def next_transfer_no(db: Session) -> str:
    prefix = f"BT{local_now():%Y%m%d}"
    count = db.scalar(select(func.count()).select_from(BudgetTransfer).where(BudgetTransfer.transfer_no.like(f"{prefix}%"))) or 0
    return f"{prefix}-{count + 1:03d}"


def resolve_borrower_team(principal: dict, requested_team_id: int | None) -> int:
    """团队成员只能为本团队申请；管理员可代任意团队申请。"""
    own = principal.get("team_id")
    if principal.get("role") == Role.ADMIN.value:
        if requested_team_id is None:
            if own is None:
                raise forbidden({"reason": "team_required"})
            return int(own)
        return int(requested_team_id)
    if own is None:
        raise forbidden({"reason": "team_missing"})
    if requested_team_id is not None and int(requested_team_id) != int(own):
        raise forbidden({"reason": "team_scope", "team_id": requested_team_id})
    return int(own)


def _to_naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def create_transfer(
    db: Session,
    payload: dict,
    principal: dict,
    *,
    seed_key: str | None = None,
) -> BudgetTransfer:
    if seed_key:
        existing = db.scalar(select(BudgetTransfer).where(BudgetTransfer.seed_key == seed_key))
        if existing is not None:
            return existing

    borrower_id = resolve_borrower_team(principal, payload.get("borrower_team_id"))
    lender_id = payload.get("lender_team_id")
    lender_id = int(lender_id) if lender_id is not None else None
    amount = round(float(payload.get("amount_yuan") or 0.0), 2)

    borrower = db.get(Team, borrower_id)
    if borrower is None:
        raise not_found("team", borrower_id)
    if lender_id is not None:
        lender = db.get(Team, lender_id)
        if lender is None:
            raise not_found("team", lender_id)
        if lender_id == borrower_id:
            raise AppError("VALIDATION", 422, {"reason": "same_team", "team_id": borrower_id})
    if amount < MIN_AMOUNT_YUAN or amount > MAX_AMOUNT_YUAN:
        raise AppError("VALIDATION", 422, {"reason": "amount_out_of_range", "amount": amount})

    now = utcnow()
    expires_at = _to_naive_utc(payload.get("expires_at"))
    valid_days = payload.get("valid_days")
    if expires_at is None and valid_days is not None:
        expires_at = now + timedelta(days=int(valid_days))
    if expires_at is None:
        expires_at = now + timedelta(days=DEFAULT_VALID_DAYS)
    if expires_at <= now:
        raise AppError("VALIDATION", 422, {"reason": "expires_in_past"})
    if expires_at > now + timedelta(days=MAX_VALID_DAYS):
        raise AppError("VALIDATION", 422, {"reason": "expires_too_far", "max_days": MAX_VALID_DAYS})

    project = (payload.get("project") or "").strip()
    if not project:
        raise AppError("VALIDATION", 422, {"reason": "project_required"})

    transfer = BudgetTransfer(
        transfer_no=next_transfer_no(db),
        borrower_team_id=borrower_id,
        lender_team_id=lender_id,
        amount_yuan=amount,
        project=project,
        reason=(payload.get("reason") or None),
        status=TransferStatus.PENDING.value,
        requested_by=principal["email"],
        requested_at=now,
        expires_at=expires_at,
        seed_key=seed_key,
    )
    db.add(transfer)
    db.flush()
    return transfer


# --------------------------------------------------------------------------- #
# 审批 / 撤回 / 过期
# --------------------------------------------------------------------------- #
def _lock_team(db: Session, team_id: int) -> Team:
    team = db.execute(
        select(Team)
        .where(Team.id == team_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if team is None:
        raise not_found("team", team_id)
    return team


def _lock_transfer(db: Session, transfer_id: int) -> BudgetTransfer:
    transfer = db.execute(
        select(BudgetTransfer)
        .where(BudgetTransfer.id == transfer_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if transfer is None:
        raise not_found("budget_transfer", transfer_id)
    return transfer


def approve_transfer(
    db: Session,
    transfer_id: int,
    *,
    actor: dict,
    note: str | None = None,
    lender_team_id: int | None = None,
) -> BudgetTransfer:
    """通过申请：在转出团队锁内按当前可借余额校验，防止并发重复分配同一余量。"""
    now = utcnow()
    transfer = _lock_transfer(db, transfer_id)

    if transfer.status == TransferStatus.PENDING.value and budget_ledger.expired_now(transfer, now):
        transfer.status = TransferStatus.EXPIRED.value
        transfer.closed_at = now
        transfer.close_reason = "申请已过有效期"
        db.flush()
        raise AppError("CONFLICT", 409, {"reason": "transfer_expired", "transfer_no": transfer.transfer_no})
    if transfer.status != TransferStatus.PENDING.value:
        raise AppError(
            "CONFLICT",
            409,
            {"reason": "transfer_not_pending", "status": effective_status(transfer, now)},
        )

    # 申请时未指定转出团队：审批时由管理员指派（可改派）
    if lender_team_id is not None:
        if int(lender_team_id) == int(transfer.borrower_team_id):
            raise AppError("VALIDATION", 422, {"reason": "same_team", "team_id": lender_team_id})
        transfer.lender_team_id = int(lender_team_id)
    if transfer.lender_team_id is None:
        raise AppError("VALIDATION", 422, {"reason": "lender_required"})

    lender = _lock_team(db, transfer.lender_team_id)
    used_map, _run_id = used_by_team(db)
    transfers = load_transfers(db, team_ids=[lender.id])
    ledger = budget_ledger.team_ledger(
        lender.id, lender.budget_yuan, transfers, used_map.get(lender.id, 0.0), now
    )
    if not budget_ledger.fits_available(transfer.amount_yuan, ledger["remaining_yuan"]):
        raise AppError(
            "CONFLICT",
            409,
            {
                "reason": "insufficient_available",
                "lender_team_id": lender.id,
                "requested_yuan": round(transfer.amount_yuan, 2),
                "available_yuan": ledger["remaining_yuan"],
            },
        )

    transfer.status = TransferStatus.APPROVED.value
    transfer.decided_by = actor["email"]
    transfer.decided_at = now
    transfer.decision_note = note
    if transfer.expires_at is None or transfer.expires_at <= now:
        transfer.expires_at = now + timedelta(days=DEFAULT_VALID_DAYS)
    db.flush()
    return transfer


def reject_transfer(db: Session, transfer_id: int, *, actor: dict, note: str | None = None) -> BudgetTransfer:
    now = utcnow()
    transfer = _lock_transfer(db, transfer_id)
    if transfer.status != TransferStatus.PENDING.value:
        raise AppError("CONFLICT", 409, {"reason": "transfer_not_pending", "status": effective_status(transfer, now)})
    transfer.status = TransferStatus.REJECTED.value
    transfer.decided_by = actor["email"]
    transfer.decided_at = now
    transfer.decision_note = note
    transfer.closed_at = now
    transfer.close_reason = "管理员驳回"
    db.flush()
    return transfer


def withdraw_transfer(db: Session, transfer_id: int, *, actor: dict, note: str | None = None) -> BudgetTransfer:
    """申请人（或管理员）撤回待审批/已通过申请，立即释放预占。"""
    now = utcnow()
    transfer = _lock_transfer(db, transfer_id)
    is_admin = actor.get("role") == Role.ADMIN.value
    if not is_admin and transfer.requested_by != actor["email"]:
        raise forbidden({"reason": "not_requester", "transfer_id": transfer.id})
    if transfer.status not in (TransferStatus.PENDING.value, TransferStatus.APPROVED.value):
        raise AppError("CONFLICT", 409, {"reason": "transfer_closed", "status": effective_status(transfer, now)})
    if budget_ledger.expired_now(transfer, now):
        transfer.status = TransferStatus.EXPIRED.value
    else:
        transfer.status = TransferStatus.WITHDRAWN.value
    transfer.closed_at = now
    transfer.close_reason = note or "申请人撤回"
    db.flush()
    return transfer


def decide_transfer(
    db: Session,
    transfer_id: int,
    decision: str,
    *,
    actor: dict,
    note: str | None = None,
    lender_team_id: int | None = None,
) -> BudgetTransfer:
    if decision == "approve":
        return approve_transfer(db, transfer_id, actor=actor, note=note, lender_team_id=lender_team_id)
    if decision == "reject":
        return reject_transfer(db, transfer_id, actor=actor, note=note)
    raise AppError("VALIDATION", 422, {"reason": "unknown_decision", "decision": decision})


def expire_stale(db: Session, *, now: datetime | None = None) -> int:
    """把已过有效期的待审批/已通过申请落成 expired（释放预占）。幂等。"""
    now = now or utcnow()
    rows = list(
        db.scalars(
            select(BudgetTransfer).where(
                BudgetTransfer.status.in_((TransferStatus.PENDING.value, TransferStatus.APPROVED.value)),
                BudgetTransfer.expires_at.is_not(None),
                BudgetTransfer.expires_at <= now,
            )
        ).all()
    )
    for transfer in rows:
        transfer.status = TransferStatus.EXPIRED.value
        transfer.closed_at = now
        transfer.close_reason = "超过有效期自动释放"
    if rows:
        db.flush()
    return len(rows)


def lender_directory(db: Session, principal: dict) -> list[dict]:
    """可转出团队目录，供申请人选择。

    非管理员只拿到团队名称/代码（原先 viewer 只能看本团队，不能借目录泄露别家财务）；
    管理员额外拿到可借余额，审批时用来指派转出团队。
    """
    teams = list(db.scalars(select(Team).order_by(Team.id)).all())
    include_balance = principal.get("role") == Role.ADMIN.value
    if not include_balance:
        return [{"team_id": team.id, "code": team.code, "name": team.name} for team in teams]
    used_map, _run_id = used_by_team(db)
    transfers = load_transfers(db, team_ids=[team.id for team in teams]) if teams else []
    now = utcnow()
    rows = []
    for team in teams:
        ledger = budget_ledger.team_ledger(team.id, team.budget_yuan, transfers, used_map.get(team.id, 0.0), now)
        rows.append(
            {
                "team_id": team.id,
                "code": team.code,
                "name": team.name,
                "available_yuan": ledger["remaining_yuan"],
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# 列表
# --------------------------------------------------------------------------- #
def list_transfers(
    db: Session,
    principal: dict,
    *,
    team_id: int | None = None,
    status: str | None = None,
    direction: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    scan_ids = scan_team_ids(principal)
    if scan_ids is not None and team_id is not None and int(team_id) not in scan_ids:
        raise forbidden({"reason": "team_scope", "team_id": team_id})
    if team_id is not None:
        scan_ids = [int(team_id)]

    stmt = select(BudgetTransfer)
    count_stmt = select(func.count()).select_from(BudgetTransfer)
    conditions = []

    if scan_ids is not None:
        if direction == "out":
            conditions.append(BudgetTransfer.lender_team_id.in_(scan_ids))
        elif direction == "in":
            conditions.append(BudgetTransfer.borrower_team_id.in_(scan_ids))
        else:
            conditions.append(
                or_(BudgetTransfer.borrower_team_id.in_(scan_ids), BudgetTransfer.lender_team_id.in_(scan_ids))
            )
    # 管理员未指定 team_id 时 direction 无意义（视野覆盖全部团队），不额外过滤

    if status:
        if status == TransferStatus.EXPIRED.value:
            conditions.append(
                or_(
                    BudgetTransfer.status == TransferStatus.EXPIRED.value,
                    (
                        BudgetTransfer.status.in_((TransferStatus.PENDING.value, TransferStatus.APPROVED.value))
                        & (BudgetTransfer.expires_at.is_not(None))
                        & (BudgetTransfer.expires_at <= utcnow())
                    ),
                )
            )
        else:
            conditions.append(BudgetTransfer.status == status)

    for condition in conditions:
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)

    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(BudgetTransfer.id.desc()).limit(limit).offset(offset)).all()
    teams = _all_teams(db)
    now = utcnow()
    return {
        "items": [serialize(row, teams, now) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
