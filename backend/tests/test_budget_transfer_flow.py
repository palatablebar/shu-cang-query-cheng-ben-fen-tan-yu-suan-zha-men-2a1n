"""借用闭环集成测试：需要真实 PostgreSQL（COSTCHARGE_TEST_DSN 或本地默认 DSN）。

无数据库时自动 skip，保证 `pytest tests` 在纯函数环境也能通过。
重点验证：
- 越权申请被拒；
- 并发审批同一转出团队时只允许一条通过（行锁串行）；
- 撤回/过期释放预占；
- 核算冻结快照，后续调整不改写已生成账单。
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")

DSN = os.getenv("COSTCHARGE_TEST_DSN", "")

pytestmark = pytest.mark.skipif(not DSN, reason="需要 COSTCHARGE_TEST_DSN 指向 PostgreSQL 才能跑集成测试")


@pytest.fixture(scope="module")
def env():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from src.models import Base

    engine = create_engine(DSN, pool_size=4, max_overflow=4, future=True)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield engine, Session
    engine.dispose()


def _clean(session) -> None:
    from sqlalchemy import delete

    from src.models import BudgetAlert, BudgetTransfer, Charge, CostRun, QueryLog, Team

    for model in (Charge, BudgetAlert, CostRun, BudgetTransfer, QueryLog, Team):
        session.execute(delete(model))
    session.commit()


@pytest.fixture()
def db(env):
    _, Session = env
    session = Session()
    _clean(session)
    yield session
    session.rollback()
    _clean(session)
    session.close()


def _seed_teams(db):
    from src.models import QueryLog, Team

    teams = {}
    for index, code in enumerate(["team-a", "team-b", "team-c"], start=1):
        team = Team(
            code=code,
            name=code,
            budget_yuan=1000.0 if code == "team-a" else 500.0,
            cost_center=f"CC-{1000 + index}",
            owner="demo",
            prev_month_yuan=0.0,
            member_count=2,
        )
        db.add(team)
    db.flush()
    teams = {team.code: team for team in db.query(Team).all()}
    db.add(
        QueryLog(
            query_id="qh-00001",
            user_email="shen.yi@costcharge.io",
            team_id=teams["team-a"].id,
            sql_digest="SELECT 1",
            scanned_gb=100.0,
            duration_ms=60000,
            cache_hit=False,
            status="success",
            queried_at=datetime(2026, 11, 10),
        )
    )
    db.commit()
    return teams


ADMIN = {"email": "admin@costcharge.io", "role": "admin", "team_id": None}


def test_cross_team_request_rejected(db):
    from src.services import transfer_service

    teams = _seed_teams(db)
    principal_b = {"email": "luo.qq@costcharge.io", "role": "viewer", "team_id": teams["team-b"].id}
    with pytest.raises(Exception) as info:
        transfer_service.create_transfer(
            db,
            {"borrower_team_id": teams["team-a"].id, "lender_team_id": teams["team-c"].id, "amount_yuan": 10, "project": "x"},
            principal_b,
        )
    assert getattr(info.value, "status_code", None) == 403


def test_concurrent_approval_does_not_double_allocate(env):
    _, Session = env
    setup = Session()
    _clean(setup)
    teams = _seed_teams(setup)
    principal_b = {"email": "luo.qq@costcharge.io", "role": "viewer", "team_id": teams["team-b"].id}
    from src.services import transfer_service

    t1 = transfer_service.create_transfer(
        setup, {"lender_team_id": teams["team-a"].id, "amount_yuan": 600, "project": "P1"}, principal_b
    )
    t2 = transfer_service.create_transfer(
        setup, {"lender_team_id": teams["team-a"].id, "amount_yuan": 600, "project": "P2"}, principal_b
    )
    setup.commit()
    ids = [t1.id, t2.id]
    setup.close()

    barrier = threading.Barrier(2)
    outcomes = []

    def approve(transfer_id):
        session = Session()
        try:
            barrier.wait(timeout=10)
            transfer_service.approve_transfer(session, transfer_id, actor=ADMIN)
            session.commit()
            return "approved"
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            return getattr(exc, "code", "ERROR")
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(approve, ids))

    assert sorted(outcomes) == ["CONFLICT", "approved"], outcomes

    session = Session()
    ledger = transfer_service.build_ledgers(session, ADMIN)
    team_a = next(item for item in ledger["items"] if item["team_id"] == teams["team-a"].id)
    assert team_a["lent_yuan"] == 600.0
    assert team_a["effective_budget_yuan"] == 400.0
    session.close()


def test_withdraw_and_expire_release_reservation(db):
    from src.services import transfer_service

    teams = _seed_teams(db)
    principal_b = {"email": "luo.qq@costcharge.io", "role": "viewer", "team_id": teams["team-b"].id}
    transfer = transfer_service.create_transfer(
        db, {"lender_team_id": teams["team-a"].id, "amount_yuan": 300, "project": "P"}, principal_b
    )
    transfer_service.approve_transfer(db, transfer.id, actor=ADMIN)
    db.commit()
    ledger = transfer_service.build_ledgers(db, ADMIN)
    assert next(item for item in ledger["items"] if item["team_id"] == teams["team-a"].id)["lent_yuan"] == 300.0

    transfer_service.withdraw_transfer(db, transfer.id, actor=ADMIN, note="撤回")
    db.commit()
    transfer_service.expire_stale(db)
    db.commit()
    ledger = transfer_service.build_ledgers(db, ADMIN)
    assert next(item for item in ledger["items"] if item["team_id"] == teams["team-a"].id)["lent_yuan"] == 0.0


def test_lender_directory_hides_balances_from_non_admin(db):
    from src.services import transfer_service

    _seed_teams(db)
    viewer = {"email": "chen.mo@costcharge.io", "role": "viewer", "team_id": 3}
    rows = transfer_service.lender_directory(db, viewer)
    assert rows and all("available_yuan" not in row for row in rows), rows
    admin_rows = transfer_service.lender_directory(db, ADMIN)
    assert all("available_yuan" in row for row in admin_rows), admin_rows


def test_lender_can_be_assigned_at_approval(db):
    from src.services import transfer_service

    teams = _seed_teams(db)
    principal_b = {"email": "luo.qq@costcharge.io", "role": "viewer", "team_id": teams["team-b"].id}
    transfer = transfer_service.create_transfer(
        db, {"amount_yuan": 120, "project": "待指派"}, principal_b
    )
    transfer_id = transfer.id
    db.commit()
    assert transfer.lender_team_id is None
    # 未指派且申请时为空 -> 422，不应误通过
    with pytest.raises(Exception) as info:
        transfer_service.approve_transfer(db, transfer_id, actor=ADMIN)
    assert getattr(info.value, "status_code", None) == 422
    db.rollback()
    transfer = transfer_service.get_transfer(db, transfer_id, ADMIN)
    assert transfer.status == "pending"

    transfer_service.approve_transfer(db, transfer_id, actor=ADMIN, lender_team_id=teams["team-c"].id)
    db.commit()
    assert transfer.status == "approved"
    assert transfer.lender_team_id == teams["team-c"].id
    ledger = transfer_service.build_ledgers(db, ADMIN)
    team_c = next(item for item in ledger["items"] if item["team_id"] == teams["team-c"].id)
    assert team_c["lent_yuan"] == 120.0


def test_expired_pending_cannot_be_approved(db):
    from src.services import transfer_service

    teams = _seed_teams(db)
    principal_b = {"email": "luo.qq@costcharge.io", "role": "viewer", "team_id": teams["team-b"].id}
    transfer = transfer_service.create_transfer(
        db, {"lender_team_id": teams["team-a"].id, "amount_yuan": 10, "project": "过期"}, principal_b
    )
    transfer.expires_at = datetime(2020, 1, 1)
    db.commit()
    with pytest.raises(Exception) as info:
        transfer_service.approve_transfer(db, transfer.id, actor=ADMIN)
    assert getattr(info.value, "status_code", None) == 409
    assert getattr(info.value, "detail", {}).get("reason") == "transfer_expired"


def test_run_freezes_snapshot_against_later_changes(db):
    from src.models import Charge
    from src.services import cost_run_service, transfer_service

    teams = _seed_teams(db)
    principal_b = {"email": "luo.qq@costcharge.io", "role": "viewer", "team_id": teams["team-b"].id}
    transfer = transfer_service.create_transfer(
        db, {"lender_team_id": teams["team-a"].id, "amount_yuan": 400, "project": "P"}, principal_b
    )
    transfer_service.approve_transfer(db, transfer.id, actor=ADMIN)
    db.commit()

    run = cost_run_service.create_and_execute(db, period="2026-11", operator_email="admin@costcharge.io", payload={})
    db.commit()
    charge = db.query(Charge).filter(Charge.run_id == run.id, Charge.team_id == teams["team-a"].id).one()
    assert charge.effective_budget_yuan == 600.0
    assert len(charge.transfer_snapshot) == 1

    transfer_service.withdraw_transfer(db, transfer.id, actor=ADMIN, note="撤回")
    teams["team-a"].budget_yuan = 9000.0
    db.commit()
    db.expire_all()
    charge = db.query(Charge).filter(Charge.run_id == run.id, Charge.team_id == teams["team-a"].id).one()
    assert charge.budget_yuan == 1000.0
    assert charge.effective_budget_yuan == 600.0
    assert len(charge.transfer_snapshot) == 1

    ledger = transfer_service.build_ledgers(db, ADMIN)
    assert next(item for item in ledger["items"] if item["team_id"] == teams["team-a"].id)["effective_budget_yuan"] == 9000.0
