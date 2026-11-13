from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import TRANSFER_APPROVE_ROLES, TRANSFER_CREATE_ROLES
from src.controllers import transfer_controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.schemas.transfer import TransferCreateIn, TransferDecisionIn, TransferWithdrawIn
from src.utils.pagination import page_params

router = APIRouter(prefix="/transfers", tags=["transfers"])


@router.get("")
def list_transfers(
    team_id: int | None = None,
    status: str | None = None,
    direction: str | None = None,
    limit: int = Query(50),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return transfer_controller.list_transfers(
        db,
        principal,
        team_id=team_id,
        status=status,
        direction=direction,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/lenders")
def lenders(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return transfer_controller.lender_directory(db, principal)


@router.get("/ledger")
def ledger(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return transfer_controller.reconcile(db, principal)


@router.post("", status_code=201)
def create_transfer(
    payload: TransferCreateIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*TRANSFER_CREATE_ROLES)),
) -> dict:
    return transfer_controller.create_transfer(db, payload.model_dump(), principal, request)


@router.post("/expire-stale")
def expire_stale(
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*TRANSFER_APPROVE_ROLES)),
) -> dict:
    return transfer_controller.expire_stale(db, principal, request)


@router.get("/{transfer_id}")
def get_transfer(
    transfer_id: int, db: Session = Depends(get_db), principal: dict = Depends(require_roles(*READ_ROLES))
) -> dict:
    return transfer_controller.get_transfer(db, transfer_id, principal)


@router.post("/{transfer_id}/decision")
def decide_transfer(
    transfer_id: int,
    payload: TransferDecisionIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*TRANSFER_APPROVE_ROLES)),
) -> dict:
    return transfer_controller.decide_transfer(db, transfer_id, payload.model_dump(), principal, request)


@router.post("/{transfer_id}/withdraw")
def withdraw_transfer(
    transfer_id: int,
    payload: TransferWithdrawIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return transfer_controller.withdraw_transfer(db, transfer_id, payload.model_dump(), principal, request)

