"""预算借用控制器：申请 / 审批 / 撤回 / 过期释放 / 台账，全部落领域审计。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from src.services import audit_service, transfer_service
from src.utils.errors import forbidden


def _ip(request) -> str | None:
    return request.client.host if request is not None and request.client else None


def _record(db: Session, principal: dict, action: str, transfer, detail: dict, request) -> None:
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action=action,
        entity="budget_transfer",
        entity_id=transfer.id,
        detail={
            "transfer_no": transfer.transfer_no,
            "borrower_team_id": transfer.borrower_team_id,
            "lender_team_id": transfer.lender_team_id,
            "amount_yuan": round(transfer.amount_yuan, 2),
            "status": transfer.status,
            **detail,
        },
        ip=_ip(request),
    )


def _serialize(db: Session, transfer) -> dict:
    return transfer_service.serialize(transfer, transfer_service._all_teams(db))


def list_transfers(db: Session, principal: dict, **filters) -> dict:
    return transfer_service.list_transfers(db, principal, **filters)


def lender_directory(db: Session, principal: dict) -> dict:
    return {"items": transfer_service.lender_directory(db, principal)}


def get_transfer(db: Session, transfer_id: int, principal: dict) -> dict:
    transfer = transfer_service.get_transfer(db, transfer_id, principal)
    return _serialize(db, transfer)


def create_transfer(db: Session, payload: dict, principal: dict, request) -> dict:
    transfer = transfer_service.create_transfer(db, payload, principal)
    _record(
        db,
        principal,
        "transfer.create",
        transfer,
        {
            "project": transfer.project,
            "requested_by": transfer.requested_by,
            "expires_at": transfer.expires_at.isoformat(timespec="seconds") if transfer.expires_at else None,
        },
        request,
    )
    db.commit()
    return _serialize(db, transfer)


def decide_transfer(db: Session, transfer_id: int, payload: dict, principal: dict, request) -> dict:
    decision = payload["decision"]
    transfer = transfer_service.decide_transfer(
        db,
        transfer_id,
        decision,
        actor=principal,
        note=payload.get("note"),
        lender_team_id=payload.get("lender_team_id"),
    )
    _record(
        db,
        principal,
        "transfer.approve" if decision == "approve" else "transfer.reject",
        transfer,
        {"decision_note": payload.get("note")},
        request,
    )
    db.commit()
    return _serialize(db, transfer)


def withdraw_transfer(db: Session, transfer_id: int, payload: dict, principal: dict, request) -> dict:
    transfer = transfer_service.withdraw_transfer(db, transfer_id, actor=principal, note=payload.get("note"))
    _record(db, principal, "transfer.withdraw", transfer, {"close_reason": transfer.close_reason}, request)
    db.commit()
    return _serialize(db, transfer)


def reconcile(db: Session, principal: dict) -> dict:
    """核算对账视图：有效预算 vs 最近一次核算的查询成本，附冻结的调拨快照。"""
    if principal.get("role") == "viewer" and principal.get("team_id") is None:
        raise forbidden({"reason": "team_missing"})
    return transfer_service.build_ledgers(db, principal)


def expire_stale(db: Session, principal: dict, request) -> dict:
    count = transfer_service.expire_stale(db)
    if count:
        audit_service.record(
            db,
            actor_email=principal["email"],
            actor_role=principal["role"],
            action="transfer.expire",
            entity="budget_transfer",
            entity_id=None,
            detail={"expired": count},
            ip=_ip(request),
        )
    db.commit()
    return {"expired": count}
