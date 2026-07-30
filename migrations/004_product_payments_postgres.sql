BEGIN;

CREATE TABLE IF NOT EXISTS product_payment_orders (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    provider TEXT NOT NULL
        CHECK (provider IN ('alipay', 'wechat')),
    provider_order_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    package_id TEXT NOT NULL,
    catalog_version TEXT NOT NULL,
    currency TEXT NOT NULL
        CHECK (
            char_length(currency) = 3
            AND currency = upper(currency)
        ),
    amount_cents BIGINT NOT NULL CHECK (amount_cents > 0),
    points BIGINT NOT NULL CHECK (points > 0),
    catalog_snapshot JSONB NOT NULL
        CHECK (jsonb_typeof(catalog_snapshot) = 'object'),
    catalog_snapshot_sha256 TEXT NOT NULL
        CHECK (catalog_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    provider_payload JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(provider_payload) = 'object'),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            status IN (
                'pending',
                'paid',
                'partially_refunded',
                'refunded',
                'failed',
                'closed'
            )
        ),
    credited_points BIGINT NOT NULL DEFAULT 0
        CHECK (credited_points >= 0 AND credited_points <= points),
    refunded_amount_cents BIGINT NOT NULL DEFAULT 0
        CHECK (
            refunded_amount_cents >= 0
            AND refunded_amount_cents <= amount_cents
        ),
    refunded_points BIGINT NOT NULL DEFAULT 0
        CHECK (
            refunded_points >= 0
            AND refunded_points <= credited_points
        ),
    credit_point_order_id TEXT,
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    paid_at TIMESTAMPTZ,
    refunded_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    CONSTRAINT uq_product_payment_order_owner
        UNIQUE (id, owner_user_id),
    CONSTRAINT uq_product_payment_order_idempotency
        UNIQUE (owner_user_id, idempotency_key),
    CONSTRAINT uq_product_payment_provider_order
        UNIQUE (provider, provider_order_id),
    CONSTRAINT uq_product_payment_credit_point_order
        UNIQUE (credit_point_order_id),
    CONSTRAINT fk_product_payment_credit_point_order
        FOREIGN KEY (credit_point_order_id, owner_user_id)
        REFERENCES product_point_orders (id, owner_user_id)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT ck_product_payment_order_identifiers
        CHECK (
            id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND owner_user_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND char_length(provider_order_id) BETWEEN 1 AND 255
            AND provider_order_id = btrim(provider_order_id)
            AND provider_order_id !~ '[[:cntrl:]]'
        ),
    CONSTRAINT ck_product_payment_order_catalog
        CHECK (
            catalog_snapshot ?& ARRAY[
                'packageId',
                'catalogVersion',
                'currency',
                'amountCents',
                'points',
                'snapshotDigest'
            ]
            AND jsonb_typeof(catalog_snapshot->'packageId') = 'string'
            AND jsonb_typeof(catalog_snapshot->'catalogVersion') = 'string'
            AND jsonb_typeof(catalog_snapshot->'currency') = 'string'
            AND jsonb_typeof(catalog_snapshot->'amountCents') = 'number'
            AND jsonb_typeof(catalog_snapshot->'points') = 'number'
            AND jsonb_typeof(catalog_snapshot->'snapshotDigest') = 'string'
            AND package_id <> ''
            AND catalog_version <> ''
            AND catalog_snapshot->>'packageId' = package_id
            AND catalog_snapshot->>'catalogVersion' = catalog_version
            AND catalog_snapshot->>'currency' = currency
            AND (catalog_snapshot->>'amountCents')::BIGINT = amount_cents
            AND (catalog_snapshot->>'points')::BIGINT = points
            AND catalog_snapshot->>'snapshotDigest' = catalog_snapshot_sha256
        ),
    CONSTRAINT ck_product_payment_order_state
        CHECK (
            (
                status = 'pending'
                AND credited_points = 0
                AND refunded_amount_cents = 0
                AND refunded_points = 0
                AND credit_point_order_id IS NULL
                AND paid_at IS NULL
                AND refunded_at IS NULL
                AND closed_at IS NULL
            )
            OR (
                status = 'paid'
                AND credited_points = points
                AND refunded_amount_cents = 0
                AND refunded_points = 0
                AND credit_point_order_id IS NOT NULL
                AND paid_at IS NOT NULL
                AND refunded_at IS NULL
                AND closed_at IS NULL
            )
            OR (
                status = 'partially_refunded'
                AND credited_points = points
                AND refunded_amount_cents > 0
                AND refunded_amount_cents < amount_cents
                AND credit_point_order_id IS NOT NULL
                AND paid_at IS NOT NULL
                AND refunded_at IS NOT NULL
                AND closed_at IS NULL
            )
            OR (
                status = 'refunded'
                AND credited_points = points
                AND refunded_amount_cents = amount_cents
                AND refunded_points = points
                AND credit_point_order_id IS NOT NULL
                AND paid_at IS NOT NULL
                AND refunded_at IS NOT NULL
                AND closed_at IS NULL
            )
            OR (
                status IN ('failed', 'closed')
                AND credited_points = 0
                AND refunded_amount_cents = 0
                AND refunded_points = 0
                AND credit_point_order_id IS NULL
                AND paid_at IS NULL
                AND refunded_at IS NULL
                AND closed_at IS NOT NULL
            )
        ),
    CONSTRAINT ck_product_payment_order_timestamps
        CHECK (
            updated_at >= created_at
            AND (paid_at IS NULL OR paid_at >= created_at)
            AND (refunded_at IS NULL OR refunded_at >= paid_at)
            AND (closed_at IS NULL OR closed_at >= created_at)
        )
);

CREATE INDEX IF NOT EXISTS idx_product_payment_orders_owner_created
    ON product_payment_orders (owner_user_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_product_payment_orders_status_created
    ON product_payment_orders (status, created_at, id);

CREATE TABLE IF NOT EXISTS product_payment_events (
    id BIGSERIAL PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    provider TEXT NOT NULL
        CHECK (provider IN ('alipay', 'wechat')),
    provider_order_id TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    event_kind TEXT NOT NULL
        CHECK (
            event_kind IN (
                'payment_succeeded',
                'refund_succeeded',
                'payment_failed',
                'payment_closed'
            )
        ),
    event_type TEXT NOT NULL,
    target_status TEXT NOT NULL
        CHECK (
            target_status IN (
                'paid',
                'partially_refunded',
                'refunded',
                'failed',
                'closed'
            )
        ),
    amount_cents BIGINT,
    points_delta BIGINT NOT NULL DEFAULT 0,
    refunded_amount_cents_after BIGINT NOT NULL DEFAULT 0
        CHECK (refunded_amount_cents_after >= 0),
    refunded_points_after BIGINT NOT NULL DEFAULT 0
        CHECK (refunded_points_after >= 0),
    wallet_order_id TEXT,
    payload JSONB NOT NULL
        CHECK (jsonb_typeof(payload) = 'object'),
    content_sha256 TEXT NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_payment_provider_event
        UNIQUE (provider, provider_event_id),
    CONSTRAINT uq_product_payment_event_owner
        UNIQUE (id, owner_user_id),
    CONSTRAINT fk_product_payment_event_order
        FOREIGN KEY (order_id, owner_user_id)
        REFERENCES product_payment_orders (id, owner_user_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_product_payment_event_wallet_order
        FOREIGN KEY (wallet_order_id, owner_user_id)
        REFERENCES product_point_orders (id, owner_user_id)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT ck_product_payment_event_identifiers
        CHECK (
            owner_user_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND char_length(provider_order_id) BETWEEN 1 AND 255
            AND provider_order_id = btrim(provider_order_id)
            AND provider_order_id !~ '[[:cntrl:]]'
            AND char_length(provider_event_id) BETWEEN 1 AND 255
            AND provider_event_id = btrim(provider_event_id)
            AND provider_event_id !~ '[[:cntrl:]]'
            AND char_length(event_type) BETWEEN 1 AND 255
            AND event_type = btrim(event_type)
            AND event_type !~ '[[:cntrl:]]'
        ),
    CONSTRAINT ck_product_payment_event_wallet_shape
        CHECK (
            (points_delta = 0 AND wallet_order_id IS NULL)
            OR (points_delta <> 0 AND wallet_order_id IS NOT NULL)
        ),
    CONSTRAINT ck_product_payment_event_shape
        CHECK (
            (
                event_kind = 'payment_succeeded'
                AND target_status = 'paid'
                AND amount_cents > 0
                AND points_delta >= 0
                AND refunded_amount_cents_after = 0
                AND refunded_points_after = 0
            )
            OR (
                event_kind = 'refund_succeeded'
                AND target_status IN ('partially_refunded', 'refunded')
                AND amount_cents > 0
                AND points_delta <= 0
                AND refunded_amount_cents_after > 0
                AND refunded_points_after >= 0
            )
            OR (
                event_kind = 'payment_failed'
                AND target_status = 'failed'
                AND amount_cents IS NULL
                AND points_delta = 0
                AND refunded_amount_cents_after = 0
                AND refunded_points_after = 0
            )
            OR (
                event_kind = 'payment_closed'
                AND target_status = 'closed'
                AND amount_cents IS NULL
                AND points_delta = 0
                AND refunded_amount_cents_after = 0
                AND refunded_points_after = 0
            )
        )
);

CREATE INDEX IF NOT EXISTS idx_product_payment_events_order_created
    ON product_payment_events (order_id, created_at, id);

CREATE INDEX IF NOT EXISTS idx_product_payment_events_owner_created
    ON product_payment_events (owner_user_id, created_at DESC, id DESC);

COMMIT;
