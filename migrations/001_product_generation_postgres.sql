BEGIN;

CREATE TABLE IF NOT EXISTS product_generation_jobs (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL
        CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    request_payload JSONB NOT NULL,
    menu_upload_id TEXT NOT NULL,
    menu_object_ref TEXT NOT NULL,
    menu_object_sha256 TEXT NOT NULL
        CHECK (menu_object_sha256 ~ '^[0-9a-f]{64}$'),
    selected_background_ref TEXT NOT NULL,
    selected_background_sha256 TEXT NOT NULL
        CHECK (selected_background_sha256 ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'canceled')),
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    fence BIGINT NOT NULL DEFAULT 0 CHECK (fence >= 0),
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    requested_count INTEGER NOT NULL CHECK (requested_count >= 0),
    completed_count INTEGER NOT NULL DEFAULT 0 CHECK (completed_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    manifest_ref TEXT,
    manifest_sha256 TEXT
        CHECK (manifest_sha256 IS NULL OR manifest_sha256 ~ '^[0-9a-f]{64}$'),
    debit_order_id TEXT NOT NULL,
    debit_points INTEGER NOT NULL CHECK (debit_points >= 0),
    refund_order_id TEXT NOT NULL,
    refund_target_points INTEGER NOT NULL DEFAULT 0
        CHECK (refund_target_points >= 0 AND refund_target_points <= debit_points),
    error_message TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    CONSTRAINT uq_product_generation_job_request
        UNIQUE (owner_user_id, idempotency_key),
    CONSTRAINT uq_product_generation_job_debit_order
        UNIQUE (debit_order_id),
    CONSTRAINT uq_product_generation_job_refund_order
        UNIQUE (refund_order_id),
    CONSTRAINT ck_product_generation_job_counts
        CHECK (completed_count + failed_count <= requested_count),
    CONSTRAINT ck_product_generation_job_manifest_pair
        CHECK ((manifest_ref IS NULL) = (manifest_sha256 IS NULL)),
    CONSTRAINT ck_product_generation_job_success_manifest
        CHECK (
            status <> 'succeeded'
            OR (manifest_ref IS NOT NULL AND manifest_sha256 IS NOT NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_product_generation_jobs_owner_created
    ON product_generation_jobs (owner_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_product_generation_jobs_status_created
    ON product_generation_jobs (status, created_at);

CREATE INDEX IF NOT EXISTS idx_product_generation_jobs_cancel_requested
    ON product_generation_jobs (updated_at)
    WHERE cancel_requested = TRUE AND status = 'running';

CREATE TABLE IF NOT EXISTS product_generation_outbox (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL
        REFERENCES product_generation_jobs (id) ON DELETE CASCADE,
    event_type TEXT NOT NULL DEFAULT 'product_generation.requested',
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'published', 'canceled', 'dead_letter')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    claimed_by TEXT,
    claim_token TEXT,
    claimed_until TIMESTAMPTZ,
    published_at TIMESTAMPTZ,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_generation_outbox_event
        UNIQUE (job_id, event_type),
    CONSTRAINT ck_product_generation_outbox_claim
        CHECK (
            status NOT IN ('claimed', 'published')
            OR (
                claimed_by IS NOT NULL
                AND claim_token IS NOT NULL
                AND claimed_until IS NOT NULL
            )
        )
);

CREATE INDEX IF NOT EXISTS idx_product_generation_outbox_dispatch
    ON product_generation_outbox (available_at, id)
    WHERE status IN ('pending', 'claimed');

CREATE INDEX IF NOT EXISTS idx_product_generation_outbox_claim_expiry
    ON product_generation_outbox (claimed_until)
    WHERE status = 'claimed';

CREATE TABLE IF NOT EXISTS product_generation_settlements (
    job_id TEXT PRIMARY KEY
        REFERENCES product_generation_jobs (id) ON DELETE CASCADE,
    owner_user_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'applied', 'failed', 'manual_review')),
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    debit_order_id TEXT NOT NULL,
    debit_points INTEGER NOT NULL CHECK (debit_points >= 0),
    refund_order_id TEXT NOT NULL,
    refund_target_points INTEGER NOT NULL DEFAULT 0
        CHECK (refund_target_points >= 0 AND refund_target_points <= debit_points),
    refund_applied_points INTEGER NOT NULL DEFAULT 0
        CHECK (
            refund_applied_points >= 0
            AND refund_applied_points <= refund_target_points
        ),
    claim_token TEXT,
    claimed_by TEXT,
    claimed_at TIMESTAMPTZ,
    applied_at TIMESTAMPTZ,
    finalized_at TIMESTAMPTZ,
    provider_reference TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_generation_settlement_debit_order
        UNIQUE (debit_order_id),
    CONSTRAINT uq_product_generation_settlement_refund_order
        UNIQUE (refund_order_id),
    CONSTRAINT uq_product_generation_settlement_claim_token
        UNIQUE (claim_token),
    CONSTRAINT ck_product_generation_settlement_claim
        CHECK (
            (
                status = 'pending'
                AND claim_token IS NULL
                AND claimed_by IS NULL
                AND claimed_at IS NULL
                AND finalized_at IS NULL
            )
            OR (
                status IN ('claimed', 'applied', 'failed', 'manual_review')
                AND claim_token IS NOT NULL
                AND claimed_by IS NOT NULL
                AND claimed_at IS NOT NULL
                AND (
                    (status = 'claimed' AND finalized_at IS NULL)
                    OR (
                        status IN ('applied', 'failed', 'manual_review')
                        AND finalized_at IS NOT NULL
                    )
                )
            )
        ),
    CONSTRAINT ck_product_generation_settlement_applied
        CHECK (
            status <> 'applied'
            OR (
                refund_applied_points = refund_target_points
                AND applied_at IS NOT NULL
                AND provider_reference <> ''
            )
        ),
    CONSTRAINT ck_product_generation_settlement_problem
        CHECK (
            status NOT IN ('failed', 'manual_review')
            OR error_message <> ''
        )
);

CREATE INDEX IF NOT EXISTS idx_product_generation_settlements_pending
    ON product_generation_settlements (created_at, job_id)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_product_generation_settlements_owner_status
    ON product_generation_settlements (owner_user_id, status, updated_at);

CREATE TABLE IF NOT EXISTS product_generation_results (
    id BIGSERIAL PRIMARY KEY,
    job_id TEXT NOT NULL
        REFERENCES product_generation_jobs (id) ON DELETE CASCADE,
    job_fence BIGINT NOT NULL CHECK (job_fence > 0),
    menu_row INTEGER NOT NULL CHECK (menu_row >= 0),
    platform TEXT NOT NULL,
    variant TEXT NOT NULL DEFAULT 'default',
    status TEXT NOT NULL DEFAULT 'reserved'
        CHECK (status IN ('reserved', 'generated', 'failed', 'rejected')),
    object_ref TEXT,
    object_sha256 TEXT
        CHECK (object_sha256 IS NULL OR object_sha256 ~ '^[0-9a-f]{64}$'),
    prompt_sha256 TEXT
        CHECK (prompt_sha256 IS NULL OR prompt_sha256 ~ '^[0-9a-f]{64}$'),
    provider_request_id TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_generation_result_slot
        UNIQUE (job_id, menu_row, platform, variant),
    CONSTRAINT ck_product_generation_result_platform
        CHECK (platform <> '' AND platform = lower(platform)),
    CONSTRAINT ck_product_generation_result_variant
        CHECK (variant <> '' AND variant = lower(variant)),
    CONSTRAINT ck_product_generation_result_object_pair
        CHECK ((object_ref IS NULL) = (object_sha256 IS NULL)),
    CONSTRAINT ck_product_generation_result_generated_object
        CHECK (
            status <> 'generated'
            OR (object_ref IS NOT NULL AND object_sha256 IS NOT NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_product_generation_results_job_status
    ON product_generation_results (job_id, status, menu_row);

CREATE INDEX IF NOT EXISTS idx_product_generation_results_job_fence
    ON product_generation_results (job_id, job_fence, menu_row);

CREATE INDEX IF NOT EXISTS idx_product_generation_results_object_digest
    ON product_generation_results (object_sha256)
    WHERE object_sha256 IS NOT NULL;

COMMIT;
