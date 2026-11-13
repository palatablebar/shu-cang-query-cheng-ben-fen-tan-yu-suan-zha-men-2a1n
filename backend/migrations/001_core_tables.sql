-- 核心表结构参考（PostgreSQL 语法，运行时不执行；建表由 SQLAlchemy create_all 完成）

CREATE TABLE users (
    id              SERIAL PRIMARY KEY,
    email           VARCHAR(160) NOT NULL UNIQUE,
    password_hash   VARCHAR(255) NOT NULL,
    display_name    VARCHAR(80)  NOT NULL,
    role            VARCHAR(20)  NOT NULL,
    team_id         INTEGER      REFERENCES teams(id) ON DELETE SET NULL,
    org             VARCHAR(120),
    created_at      TIMESTAMP    NOT NULL
);

CREATE TABLE teams (
    id              SERIAL PRIMARY KEY,
    code            VARCHAR(40)  NOT NULL UNIQUE,
    name            VARCHAR(120) NOT NULL,
    budget_yuan     DOUBLE PRECISION NOT NULL DEFAULT 0,
    cost_center     VARCHAR(40)  NOT NULL,
    owner           VARCHAR(80)  NOT NULL,
    prev_month_yuan DOUBLE PRECISION NOT NULL DEFAULT 0,
    member_count    INTEGER      NOT NULL DEFAULT 0,
    note            TEXT,
    created_at      TIMESTAMP    NOT NULL,
    updated_at      TIMESTAMP    NOT NULL
);

CREATE TABLE query_logs (
    id              SERIAL PRIMARY KEY,
    query_id        VARCHAR(64)  NOT NULL UNIQUE,
    user_email      VARCHAR(160) NOT NULL,
    team_id         INTEGER      NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    sql_digest      VARCHAR(160) NOT NULL,
    scanned_gb      DOUBLE PRECISION NOT NULL DEFAULT 0,
    duration_ms     INTEGER      NOT NULL DEFAULT 0,
    cache_hit       BOOLEAN      NOT NULL DEFAULT FALSE,
    status          VARCHAR(16)  NOT NULL DEFAULT 'success',
    queried_at      TIMESTAMP    NOT NULL,
    cost_yuan       DOUBLE PRECISION NOT NULL DEFAULT 0,
    is_anomaly      BOOLEAN      NOT NULL DEFAULT FALSE,
    anomaly_score   DOUBLE PRECISION NOT NULL DEFAULT 0,
    anomaly_reason  TEXT,
    seed_key        VARCHAR(120) UNIQUE,
    created_at      TIMESTAMP    NOT NULL
);

CREATE TABLE cost_runs (
    id              SERIAL PRIMARY KEY,
    run_no          VARCHAR(40)  NOT NULL UNIQUE,
    period          VARCHAR(20)  NOT NULL,
    scan_price      DOUBLE PRECISION NOT NULL DEFAULT 0.028,
    compute_price   DOUBLE PRECISION NOT NULL DEFAULT 0.0004,
    cache_discount  DOUBLE PRECISION NOT NULL DEFAULT 0.3,
    trigger         VARCHAR(20)  NOT NULL DEFAULT 'manual',
    status          VARCHAR(20)  NOT NULL,
    operator_email  VARCHAR(160) NOT NULL,
    input_count     INTEGER      NOT NULL DEFAULT 0,
    output_count    INTEGER      NOT NULL DEFAULT 0,
    total_cost_yuan DOUBLE PRECISION NOT NULL DEFAULT 0,
    metrics         JSON,
    log_text        TEXT,
    seed_key        VARCHAR(120) UNIQUE,
    started_at      TIMESTAMP    NOT NULL,
    finished_at     TIMESTAMP
);

CREATE TABLE charges (
    id                SERIAL PRIMARY KEY,
    run_id            INTEGER NOT NULL REFERENCES cost_runs(id) ON DELETE CASCADE,
    team_id           INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    amount_yuan       DOUBLE PRECISION NOT NULL DEFAULT 0,
    ratio             DOUBLE PRECISION NOT NULL DEFAULT 0,
    query_count       INTEGER NOT NULL DEFAULT 0,
    cached_saved_yuan DOUBLE PRECISION NOT NULL DEFAULT 0,
    mom_ratio         DOUBLE PRECISION,
    budget_yuan             DOUBLE PRECISION NOT NULL DEFAULT 0,
    effective_budget_yuan   DOUBLE PRECISION NOT NULL DEFAULT 0,
    borrowed_yuan           DOUBLE PRECISION NOT NULL DEFAULT 0,
    lent_yuan               DOUBLE PRECISION NOT NULL DEFAULT 0,
    remaining_yuan          DOUBLE PRECISION NOT NULL DEFAULT 0,
    transfer_snapshot       JSON,
    reconciliation          JSON,
    user_split        JSON,
    top_query         JSON,
    created_at        TIMESTAMP NOT NULL,
    CONSTRAINT uq_charges_run_team UNIQUE (run_id, team_id)
);

CREATE TABLE budget_alerts (
    id             SERIAL PRIMARY KEY,
    run_id         INTEGER NOT NULL REFERENCES cost_runs(id) ON DELETE CASCADE,
    team_id        INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    forecast_yuan  DOUBLE PRECISION NOT NULL DEFAULT 0,
    ratio          DOUBLE PRECISION NOT NULL DEFAULT 0,
    budget_yuan            DOUBLE PRECISION NOT NULL DEFAULT 0,
    effective_budget_yuan  DOUBLE PRECISION NOT NULL DEFAULT 0,
    level          VARCHAR(16) NOT NULL DEFAULT 'normal',
    gate_action    VARCHAR(24) NOT NULL DEFAULT 'watch',
    status         VARCHAR(16) NOT NULL DEFAULT 'pending',
    note           TEXT,
    reviewer       VARCHAR(160),
    reviewed_at    TIMESTAMP,
    created_at     TIMESTAMP NOT NULL,
    CONSTRAINT uq_alerts_run_team UNIQUE (run_id, team_id)
);

CREATE TABLE audit_logs (
    id              SERIAL PRIMARY KEY,
    actor_email     VARCHAR(160) NOT NULL,
    actor_role      VARCHAR(20)  NOT NULL,
    action          VARCHAR(64)  NOT NULL,
    entity          VARCHAR(40)  NOT NULL,
    entity_id       VARCHAR(64),
    detail          JSON,
    layer           VARCHAR(16)  NOT NULL DEFAULT 'domain',
    ip              VARCHAR(64),
    seed_key        VARCHAR(120) UNIQUE,
    created_at      TIMESTAMP    NOT NULL
);

-- 跨团队预算借用：审批通过（approved）且未过期才形成转出预占
CREATE TABLE budget_transfers (
    id               SERIAL PRIMARY KEY,
    transfer_no      VARCHAR(40)  NOT NULL UNIQUE,
    borrower_team_id INTEGER      NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    lender_team_id   INTEGER      REFERENCES teams(id) ON DELETE CASCADE,
    amount_yuan      DOUBLE PRECISION NOT NULL DEFAULT 0,
    project          VARCHAR(120) NOT NULL,
    reason           TEXT,
    status           VARCHAR(16)  NOT NULL DEFAULT 'pending',
    requested_by     VARCHAR(160) NOT NULL,
    requested_at     TIMESTAMP    NOT NULL,
    decided_by       VARCHAR(160),
    decided_at       TIMESTAMP,
    decision_note    TEXT,
    expires_at       TIMESTAMP,
    closed_at        TIMESTAMP,
    close_reason     VARCHAR(160),
    seed_key         VARCHAR(120) UNIQUE,
    created_at       TIMESTAMP    NOT NULL,
    updated_at       TIMESTAMP    NOT NULL
);

CREATE INDEX ix_budget_transfers_borrower_team_id ON budget_transfers (borrower_team_id);
CREATE INDEX ix_budget_transfers_lender_team_id   ON budget_transfers (lender_team_id);
CREATE INDEX ix_budget_transfers_status           ON budget_transfers (status);
CREATE INDEX ix_budget_transfers_expires_at       ON budget_transfers (expires_at);
