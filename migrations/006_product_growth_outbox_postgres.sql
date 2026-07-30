BEGIN;

CREATE TABLE IF NOT EXISTS product_growth_outbox (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL
        CHECK (
            event_type IN (
                'growth.first_payment_reward.requested',
                'growth.payment_refund.requested',
                'growth.invite_reward.requested',
                'growth.agent_commission.requested'
            )
        ),
    dedupe_key TEXT NOT NULL,
    payload JSONB NOT NULL
        CHECK (jsonb_typeof(payload) = 'object'),
    payload_sha256 TEXT NOT NULL
        CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'succeeded', 'dead_letter')),
    max_attempts INTEGER NOT NULL DEFAULT 8
        CHECK (max_attempts BETWEEN 1 AND 100),
    attempt_count INTEGER NOT NULL DEFAULT 0
        CHECK (attempt_count BETWEEN 0 AND max_attempts),
    available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    claimed_by TEXT,
    claim_token TEXT,
    claimed_until TIMESTAMPTZ,
    fence BIGINT NOT NULL DEFAULT 0 CHECK (fence >= 0),
    result_payload JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(result_payload) = 'object'),
    last_error TEXT NOT NULL DEFAULT '',
    succeeded_at TIMESTAMPTZ,
    dead_lettered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_growth_outbox_dedupe UNIQUE (dedupe_key),
    CONSTRAINT ck_product_growth_outbox_identifiers
        CHECK (
            id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND dedupe_key ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$'
            AND (
                claimed_by IS NULL
                OR claimed_by ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            )
            AND (
                claim_token IS NULL
                OR claim_token ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            )
        ),
    CONSTRAINT ck_product_growth_outbox_state
        CHECK (
            (
                status = 'pending'
                AND claimed_by IS NULL
                AND claim_token IS NULL
                AND claimed_until IS NULL
                AND succeeded_at IS NULL
                AND dead_lettered_at IS NULL
            )
            OR (
                status = 'claimed'
                AND claimed_by IS NOT NULL
                AND claim_token IS NOT NULL
                AND claimed_until IS NOT NULL
                AND fence > 0
                AND succeeded_at IS NULL
                AND dead_lettered_at IS NULL
            )
            OR (
                status = 'succeeded'
                AND claimed_by IS NOT NULL
                AND claim_token IS NOT NULL
                AND claimed_until IS NOT NULL
                AND fence > 0
                AND succeeded_at IS NOT NULL
                AND dead_lettered_at IS NULL
                AND last_error = ''
            )
            OR (
                status = 'dead_letter'
                AND claimed_by IS NOT NULL
                AND claim_token IS NOT NULL
                AND claimed_until IS NOT NULL
                AND fence > 0
                AND succeeded_at IS NULL
                AND dead_lettered_at IS NOT NULL
                AND last_error <> ''
            )
        ),
    CONSTRAINT ck_product_growth_outbox_timestamps
        CHECK (
            updated_at >= created_at
            AND available_at >= created_at
            AND (succeeded_at IS NULL OR succeeded_at >= created_at)
            AND (dead_lettered_at IS NULL OR dead_lettered_at >= created_at)
        )
);

CREATE INDEX IF NOT EXISTS idx_product_growth_outbox_dispatch
    ON product_growth_outbox (available_at, created_at, id)
    WHERE status IN ('pending', 'claimed');

CREATE INDEX IF NOT EXISTS idx_product_growth_outbox_claim_expiry
    ON product_growth_outbox (claimed_until, id)
    WHERE status = 'claimed';

CREATE INDEX IF NOT EXISTS idx_product_growth_outbox_terminal
    ON product_growth_outbox (status, updated_at DESC, id)
    WHERE status IN ('succeeded', 'dead_letter');

COMMIT;
