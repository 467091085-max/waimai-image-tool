BEGIN;

CREATE TABLE IF NOT EXISTS product_point_accounts (
    owner_user_id TEXT PRIMARY KEY,
    balance_points BIGINT NOT NULL DEFAULT 0
        CHECK (balance_points >= 0),
    lifetime_credited_points BIGINT NOT NULL DEFAULT 0
        CHECK (lifetime_credited_points >= 0),
    lifetime_debited_points BIGINT NOT NULL DEFAULT 0
        CHECK (lifetime_debited_points >= 0),
    lifetime_refunded_points BIGINT NOT NULL DEFAULT 0
        CHECK (lifetime_refunded_points >= 0),
    version BIGINT NOT NULL DEFAULT 0
        CHECK (version >= 0),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_product_point_account_timestamps
        CHECK (updated_at >= created_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_product_generation_job_owner
    ON product_generation_jobs (id, owner_user_id);

CREATE TABLE IF NOT EXISTS product_point_orders (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL
        REFERENCES product_point_accounts (owner_user_id) ON DELETE RESTRICT,
    order_kind TEXT NOT NULL
        CHECK (order_kind IN ('credit', 'debit', 'refund')),
    points BIGINT NOT NULL CHECK (points >= 0),
    source_order_id TEXT,
    source_order_kind TEXT,
    job_id TEXT,
    request_sha256 TEXT NOT NULL
        CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_point_order_owner
        UNIQUE (id, owner_user_id),
    CONSTRAINT uq_product_point_order_source_target
        UNIQUE (id, owner_user_id, order_kind),
    CONSTRAINT fk_product_point_order_job_owner
        FOREIGN KEY (job_id, owner_user_id)
        REFERENCES product_generation_jobs (id, owner_user_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_product_point_order_refund_source
        FOREIGN KEY (source_order_id, owner_user_id, source_order_kind)
        REFERENCES product_point_orders (id, owner_user_id, order_kind)
        ON DELETE RESTRICT,
    CONSTRAINT ck_product_point_order_shape
        CHECK (
            (
                order_kind = 'credit'
                AND points > 0
                AND source_order_id IS NULL
                AND source_order_kind IS NULL
                AND job_id IS NULL
            )
            OR (
                order_kind = 'debit'
                AND source_order_id IS NULL
                AND source_order_kind IS NULL
            )
            OR (
                order_kind = 'refund'
                AND source_order_id IS NOT NULL
                AND source_order_kind = 'debit'
                AND job_id IS NOT NULL
            )
        ),
    CONSTRAINT ck_product_point_order_timestamps
        CHECK (updated_at >= created_at AND applied_at >= created_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_product_point_order_job_kind
    ON product_point_orders (job_id, order_kind)
    WHERE job_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_product_point_refund_source
    ON product_point_orders (source_order_id)
    WHERE order_kind = 'refund';

CREATE INDEX IF NOT EXISTS idx_product_point_orders_owner_created
    ON product_point_orders (owner_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS product_point_ledger (
    id BIGSERIAL PRIMARY KEY,
    owner_user_id TEXT NOT NULL
        REFERENCES product_point_accounts (owner_user_id) ON DELETE RESTRICT,
    order_id TEXT NOT NULL,
    order_kind TEXT NOT NULL
        CHECK (order_kind IN ('credit', 'debit', 'refund')),
    points BIGINT NOT NULL CHECK (points >= 0),
    delta_points BIGINT NOT NULL,
    balance_after_points BIGINT NOT NULL
        CHECK (balance_after_points >= 0),
    request_sha256 TEXT NOT NULL
        CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_point_ledger_order UNIQUE (order_id),
    CONSTRAINT fk_product_point_ledger_order
        FOREIGN KEY (order_id, owner_user_id, order_kind)
        REFERENCES product_point_orders (id, owner_user_id, order_kind)
        ON DELETE RESTRICT,
    CONSTRAINT ck_product_point_ledger_delta
        CHECK (
            (order_kind = 'debit' AND delta_points = -points)
            OR (
                order_kind IN ('credit', 'refund')
                AND delta_points = points
            )
        )
);

CREATE INDEX IF NOT EXISTS idx_product_point_ledger_owner_created
    ON product_point_ledger (owner_user_id, created_at DESC, id DESC);

COMMIT;
