"""成本运行控制器：发起运行 → 写分摊/告警 → 审计。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.budget_alert import BudgetAlert
from src.models.charge import Charge
from src.services import alert_service, audit_service, charge_service, cost_run_service, team_service
from src.utils.errors import forbidden


def create_run(db: Session, payload: dict, principal: dict, request) -> dict:
    prices = cost_run_service.resolve_prices(payload, principal["role"])
    if payload.get("period") and principal["role"] != "admin":
        raise forbidden({"reason": "period_admin_only"})
    run = cost_run_service.create_and_execute(
        db,
        period=payload.get("period"),
        operator_email=principal["email"],
        payload={**prices, "trigger": payload.get("trigger") or "manual"},
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="run.create",
        entity="cost_run",
        entity_id=run.id,
        detail={
            "run_no": run.run_no,
            "period": run.period,
            "total_cost_yuan": run.total_cost_yuan,
            "anomalies": (run.metrics or {}).get("anomaly_count", 0),
        },
        ip=request.client.host if request.client else None,
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="run.complete",
        entity="cost_run",
        entity_id=run.id,
        detail={"status": run.status, "output": run.output_count},
        ip=request.client.host if request.client else None,
    )
    # 对账快照留痕：把本次核算冻结的调拨与超支团队记进审计，便于事后追溯
    snapshot_rows = db.scalars(select(Charge).where(Charge.run_id == run.id)).all()
    frozen = sum(len(row.transfer_snapshot or []) for row in snapshot_rows)
    if frozen:
        audit_service.record(
            db,
            actor_email=principal["email"],
            actor_role=principal["role"],
            action="transfer.reconcile",
            entity="cost_run",
            entity_id=run.id,
            detail={
                "run_no": run.run_no,
                "frozen_transfers": frozen,
                "over_budget_teams": (run.metrics or {}).get("over_budget_teams", 0),
            },
            ip=request.client.host if request.client else None,
        )
    db.commit()
    return cost_run_service.serialize(run)


def list_runs(db: Session, **filters) -> dict:
    return cost_run_service.list_runs(db, **filters)


def get_run(db: Session, run_id: int, principal: dict) -> dict:
    run = cost_run_service.get_run(db, run_id)
    charges = db.scalars(select(Charge).where(Charge.run_id == run.id).order_by(Charge.amount_yuan.desc())).all()
    alerts = db.scalars(select(BudgetAlert).where(BudgetAlert.run_id == run.id).order_by(BudgetAlert.ratio.desc())).all()
    teams = {team.id: team for team in team_service.list_all(db)}
    scope = team_service.scoped_team_id(principal, None) if principal.get("role") == "viewer" else None
    if scope is not None:
        charges = [row for row in charges if row.team_id == scope]
        alerts = [row for row in alerts if row.team_id == scope]
    data = cost_run_service.serialize(run)
    data["charges"] = [charge_service.serialize(row, teams.get(row.team_id)) for row in charges]
    data["alerts"] = [alert_service.serialize(row, teams.get(row.team_id)) for row in alerts]
    if scope is not None:
        data["total_cost_yuan"] = round(sum(row.amount_yuan for row in charges), 2)
        data["metrics"] = {**data["metrics"], "total_cost_yuan": data["total_cost_yuan"], "scoped": True}
    return data
