BEGIN;

CREATE TABLE IF NOT EXISTS product_agent_finance_accounts (
    agent_id TEXT PRIMARY KEY,
    currency TEXT NOT NULL DEFAULT 'CNY'
        CHECK (
            char_length(currency) = 3
            AND currency = upper(currency)
        ),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'suspended', 'closed')),
    earned_cents BIGINT NOT NULL DEFAULT 0 CHECK (earned_cents >= 0),
    clawback_cents BIGINT NOT NULL DEFAULT 0 CHECK (clawback_cents >= 0),
    reserved_cents BIGINT NOT NULL DEFAULT 0 CHECK (reserved_cents >= 0),
    withdrawn_cents BIGINT NOT NULL DEFAULT 0 CHECK (withdrawn_cents >= 0),
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_product_agent_finance_account_identifier
        CHECK (agent_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'),
    CONSTRAINT ck_product_agent_finance_account_balance
        CHECK (reserved_cents + withdrawn_cents <= earned_cents),
    CONSTRAINT ck_product_agent_finance_account_clawback
        CHECK (clawback_cents <= earned_cents),
    CONSTRAINT ck_product_agent_finance_account_timestamps
        CHECK (updated_at >= created_at)
);

ALTER TABLE product_agent_finance_accounts
    ADD COLUMN IF NOT EXISTS clawback_cents BIGINT NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_product_agent_finance_account_clawback'
          AND conrelid = 'product_agent_finance_accounts'::regclass
    ) THEN
        ALTER TABLE product_agent_finance_accounts
            ADD CONSTRAINT ck_product_agent_finance_account_clawback
            CHECK (
                clawback_cents >= 0
                AND clawback_cents <= earned_cents
            );
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_product_agent_finance_accounts_status
    ON product_agent_finance_accounts (status, updated_at DESC, agent_id);

CREATE TABLE IF NOT EXISTS product_commission_orders (
    id TEXT PRIMARY KEY,
    source_order_id TEXT NOT NULL,
    agent_id TEXT NOT NULL
        REFERENCES product_agent_finance_accounts (agent_id)
        ON DELETE RESTRICT,
    customer_user_id TEXT NOT NULL DEFAULT '',
    order_amount_cents BIGINT NOT NULL CHECK (order_amount_cents > 0),
    commission_amount_cents BIGINT NOT NULL
        CHECK (
            commission_amount_cents > 0
            AND commission_amount_cents <= order_amount_cents
        ),
    refunded_order_amount_cents BIGINT NOT NULL DEFAULT 0,
    reversed_commission_cents BIGINT NOT NULL DEFAULT 0,
    refund_status TEXT NOT NULL DEFAULT 'none',
    commission_rate_bps INTEGER NOT NULL
        CHECK (commission_rate_bps BETWEEN 1 AND 10000),
    currency TEXT NOT NULL DEFAULT 'CNY'
        CHECK (
            char_length(currency) = 3
            AND currency = upper(currency)
        ),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            status IN (
                'pending',
                'eligible',
                'claimed',
                'settled',
                'canceled',
                'refunded'
            )
        ),
    settlement_id TEXT,
    content_sha256 TEXT NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    eligible_at TIMESTAMPTZ,
    claimed_at TIMESTAMPTZ,
    settled_at TIMESTAMPTZ,
    last_refunded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_commission_source_order
        UNIQUE (agent_id, source_order_id),
    CONSTRAINT ck_product_commission_order_identifiers
        CHECK (
            id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND source_order_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND (
                customer_user_id = ''
                OR customer_user_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            )
            AND (
                settlement_id IS NULL
                OR settlement_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            )
        ),
    CONSTRAINT ck_product_commission_order_state
        CHECK (
            (
                status = 'pending'
                AND settlement_id IS NULL
                AND eligible_at IS NULL
                AND claimed_at IS NULL
                AND settled_at IS NULL
            )
            OR (
                status = 'eligible'
                AND settlement_id IS NULL
                AND eligible_at IS NOT NULL
                AND claimed_at IS NULL
                AND settled_at IS NULL
            )
            OR (
                status = 'claimed'
                AND settlement_id IS NOT NULL
                AND eligible_at IS NOT NULL
                AND claimed_at IS NOT NULL
                AND settled_at IS NULL
            )
            OR (
                status = 'settled'
                AND settlement_id IS NOT NULL
                AND eligible_at IS NOT NULL
                AND claimed_at IS NOT NULL
                AND settled_at IS NOT NULL
            )
            OR (
                status IN ('canceled', 'refunded')
                AND settlement_id IS NULL
                AND claimed_at IS NULL
                AND settled_at IS NULL
            )
        ),
    CONSTRAINT ck_product_commission_order_timestamps
        CHECK (
            updated_at >= created_at
            AND (eligible_at IS NULL OR eligible_at >= created_at)
            AND (claimed_at IS NULL OR claimed_at >= eligible_at)
            AND (settled_at IS NULL OR settled_at >= claimed_at)
            AND (last_refunded_at IS NULL OR last_refunded_at >= created_at)
        ),
    CONSTRAINT ck_product_commission_order_refund
        CHECK (
            refunded_order_amount_cents BETWEEN 0 AND order_amount_cents
            AND reversed_commission_cents BETWEEN 0
                AND commission_amount_cents
            AND refund_status IN ('none', 'partial', 'full')
            AND (
                (refund_status = 'none'
                    AND refunded_order_amount_cents = 0
                    AND reversed_commission_cents = 0
                    AND last_refunded_at IS NULL)
                OR (refund_status = 'partial'
                    AND refunded_order_amount_cents > 0
                    AND refunded_order_amount_cents < order_amount_cents
                    AND reversed_commission_cents > 0
                    AND reversed_commission_cents
                        < commission_amount_cents
                    AND last_refunded_at IS NOT NULL)
                OR (refund_status = 'full'
                    AND refunded_order_amount_cents > 0
                    AND reversed_commission_cents
                        = commission_amount_cents
                    AND last_refunded_at IS NOT NULL)
            )
        )
);

ALTER TABLE product_commission_orders
    ADD COLUMN IF NOT EXISTS refunded_order_amount_cents BIGINT
        NOT NULL DEFAULT 0;
ALTER TABLE product_commission_orders
    ADD COLUMN IF NOT EXISTS reversed_commission_cents BIGINT
        NOT NULL DEFAULT 0;
ALTER TABLE product_commission_orders
    ADD COLUMN IF NOT EXISTS refund_status TEXT NOT NULL DEFAULT 'none';
ALTER TABLE product_commission_orders
    ADD COLUMN IF NOT EXISTS last_refunded_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_product_commission_order_refund'
          AND conrelid = 'product_commission_orders'::regclass
    ) THEN
        ALTER TABLE product_commission_orders
            ADD CONSTRAINT ck_product_commission_order_refund
            CHECK (
                refunded_order_amount_cents BETWEEN 0
                    AND order_amount_cents
                AND reversed_commission_cents BETWEEN 0
                    AND commission_amount_cents
                AND refund_status IN ('none', 'partial', 'full')
                AND (
                    (refund_status = 'none'
                        AND refunded_order_amount_cents = 0
                        AND reversed_commission_cents = 0
                        AND last_refunded_at IS NULL)
                    OR (refund_status = 'partial'
                        AND refunded_order_amount_cents > 0
                        AND refunded_order_amount_cents
                            < order_amount_cents
                        AND reversed_commission_cents > 0
                        AND reversed_commission_cents
                            < commission_amount_cents
                        AND last_refunded_at IS NOT NULL)
                    OR (refund_status = 'full'
                        AND refunded_order_amount_cents > 0
                        AND reversed_commission_cents
                            = commission_amount_cents
                        AND last_refunded_at IS NOT NULL)
                )
            );
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_product_commission_orders_agent_status
    ON product_commission_orders (agent_id, status, created_at, id);

CREATE INDEX IF NOT EXISTS idx_product_commission_orders_pending_release
    ON product_commission_orders (status, created_at, agent_id, id)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_product_commission_orders_settlement
    ON product_commission_orders (settlement_id, id)
    WHERE settlement_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS product_commission_settlements (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL
        REFERENCES product_agent_finance_accounts (agent_id)
        ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL
        CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    settlement_no TEXT NOT NULL,
    total_order_amount_cents BIGINT NOT NULL
        CHECK (total_order_amount_cents > 0),
    total_commission_amount_cents BIGINT NOT NULL
        CHECK (
            total_commission_amount_cents > 0
            AND total_commission_amount_cents <= total_order_amount_cents
        ),
    order_count INTEGER NOT NULL CHECK (order_count > 0),
    currency TEXT NOT NULL DEFAULT 'CNY'
        CHECK (
            char_length(currency) = 3
            AND currency = upper(currency)
        ),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'paid', 'failed', 'canceled')),
    settlement_account JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(settlement_account) = 'object'),
    failure_reason TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    paid_at TIMESTAMPTZ,
    released_at TIMESTAMPTZ,
    CONSTRAINT uq_product_commission_settlement_idempotency
        UNIQUE (agent_id, idempotency_key),
    CONSTRAINT ck_product_commission_settlement_identifiers
        CHECK (
            id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND settlement_no ~ '^[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,127}$'
        ),
    CONSTRAINT ck_product_commission_settlement_state
        CHECK (
            (
                status IN ('pending', 'processing')
                AND paid_at IS NULL
                AND released_at IS NULL
            )
            OR (
                status = 'paid'
                AND paid_at IS NOT NULL
                AND released_at IS NULL
            )
            OR (
                status IN ('failed', 'canceled')
                AND paid_at IS NULL
                AND released_at IS NOT NULL
            )
        ),
    CONSTRAINT ck_product_commission_settlement_timestamps
        CHECK (
            updated_at >= created_at
            AND (paid_at IS NULL OR paid_at >= created_at)
            AND (released_at IS NULL OR released_at >= created_at)
        )
);

CREATE INDEX IF NOT EXISTS idx_product_commission_settlements_agent_status
    ON product_commission_settlements (agent_id, status, created_at, id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_product_commission_order_settlement'
          AND conrelid = 'product_commission_orders'::regclass
    ) THEN
        ALTER TABLE product_commission_orders
            ADD CONSTRAINT fk_product_commission_order_settlement
            FOREIGN KEY (settlement_id)
            REFERENCES product_commission_settlements (id)
            ON DELETE RESTRICT
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS product_commission_settlement_items (
    settlement_id TEXT NOT NULL
        REFERENCES product_commission_settlements (id)
        ON DELETE RESTRICT,
    commission_order_id TEXT NOT NULL
        REFERENCES product_commission_orders (id)
        ON DELETE RESTRICT,
    source_order_id TEXT NOT NULL,
    order_amount_cents BIGINT NOT NULL CHECK (order_amount_cents > 0),
    commission_amount_cents BIGINT NOT NULL
        CHECK (
            commission_amount_cents > 0
            AND commission_amount_cents <= order_amount_cents
        ),
    original_commission_amount_cents BIGINT NOT NULL,
    reversed_commission_amount_cents BIGINT NOT NULL DEFAULT 0,
    currency TEXT NOT NULL
        CHECK (
            char_length(currency) = 3
            AND currency = upper(currency)
        ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (settlement_id, commission_order_id),
    CONSTRAINT ck_product_commission_settlement_item_adjustment
        CHECK (
            original_commission_amount_cents > 0
            AND reversed_commission_amount_cents BETWEEN 0
                AND original_commission_amount_cents
            AND commission_amount_cents
                = original_commission_amount_cents
                    - reversed_commission_amount_cents
        )
);

ALTER TABLE product_commission_settlement_items
    DROP CONSTRAINT IF EXISTS uq_product_commission_settlement_item_order;

ALTER TABLE product_commission_settlement_items
    ADD COLUMN IF NOT EXISTS original_commission_amount_cents BIGINT
        NOT NULL DEFAULT 0;
ALTER TABLE product_commission_settlement_items
    ADD COLUMN IF NOT EXISTS reversed_commission_amount_cents BIGINT
        NOT NULL DEFAULT 0;

UPDATE product_commission_settlement_items
SET original_commission_amount_cents = commission_amount_cents
WHERE original_commission_amount_cents = 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname =
                'ck_product_commission_settlement_item_adjustment'
          AND conrelid =
                'product_commission_settlement_items'::regclass
    ) THEN
        ALTER TABLE product_commission_settlement_items
            ADD CONSTRAINT
                ck_product_commission_settlement_item_adjustment
            CHECK (
                original_commission_amount_cents > 0
                AND reversed_commission_amount_cents BETWEEN 0
                    AND original_commission_amount_cents
                AND commission_amount_cents
                    = original_commission_amount_cents
                        - reversed_commission_amount_cents
            );
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_product_commission_settlement_items_settlement
    ON product_commission_settlement_items (settlement_id, commission_order_id);

CREATE TABLE IF NOT EXISTS product_agent_withdrawals (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL
        REFERENCES product_agent_finance_accounts (agent_id)
        ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL
        CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    amount_cents BIGINT NOT NULL CHECK (amount_cents > 0),
    currency TEXT NOT NULL DEFAULT 'CNY'
        CHECK (
            char_length(currency) = 3
            AND currency = upper(currency)
        ),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'paid', 'canceled')),
    account_snapshot JSONB NOT NULL
        CHECK (
            jsonb_typeof(account_snapshot) = 'object'
            AND account_snapshot <> '{}'::jsonb
        ),
    balance_snapshot JSONB NOT NULL
        CHECK (jsonb_typeof(balance_snapshot) = 'object'),
    status_reason TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_at TIMESTAMPTZ,
    rejected_at TIMESTAMPTZ,
    paid_at TIMESTAMPTZ,
    canceled_at TIMESTAMPTZ,
    CONSTRAINT uq_product_agent_withdrawal_idempotency
        UNIQUE (agent_id, idempotency_key),
    CONSTRAINT ck_product_agent_withdrawal_identifiers
        CHECK (
            id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
        ),
    CONSTRAINT ck_product_agent_withdrawal_state
        CHECK (
            (
                status = 'pending'
                AND approved_at IS NULL
                AND rejected_at IS NULL
                AND paid_at IS NULL
                AND canceled_at IS NULL
            )
            OR (
                status = 'approved'
                AND approved_at IS NOT NULL
                AND rejected_at IS NULL
                AND paid_at IS NULL
                AND canceled_at IS NULL
            )
            OR (
                status = 'rejected'
                AND rejected_at IS NOT NULL
                AND paid_at IS NULL
                AND canceled_at IS NULL
            )
            OR (
                status = 'paid'
                AND approved_at IS NOT NULL
                AND rejected_at IS NULL
                AND paid_at IS NOT NULL
                AND canceled_at IS NULL
            )
            OR (
                status = 'canceled'
                AND rejected_at IS NULL
                AND paid_at IS NULL
                AND canceled_at IS NOT NULL
            )
        ),
    CONSTRAINT ck_product_agent_withdrawal_timestamps
        CHECK (
            updated_at >= created_at
            AND (approved_at IS NULL OR approved_at >= created_at)
            AND (rejected_at IS NULL OR rejected_at >= created_at)
            AND (paid_at IS NULL OR paid_at >= approved_at)
            AND (canceled_at IS NULL OR canceled_at >= created_at)
        )
);

CREATE INDEX IF NOT EXISTS idx_product_agent_withdrawals_agent_status
    ON product_agent_withdrawals (agent_id, status, created_at, id);

CREATE INDEX IF NOT EXISTS idx_product_agent_withdrawals_review
    ON product_agent_withdrawals (status, created_at, id)
    WHERE status IN ('pending', 'approved');

CREATE TABLE IF NOT EXISTS product_agent_finance_ledger (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL
        REFERENCES product_agent_finance_accounts (agent_id)
        ON DELETE RESTRICT,
    currency TEXT NOT NULL
        CHECK (
            char_length(currency) = 3
            AND currency = upper(currency)
        ),
    entry_kind TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    earned_delta_cents BIGINT NOT NULL DEFAULT 0,
    clawback_delta_cents BIGINT NOT NULL DEFAULT 0,
    reserved_delta_cents BIGINT NOT NULL DEFAULT 0,
    withdrawn_delta_cents BIGINT NOT NULL DEFAULT 0,
    balance_after JSONB NOT NULL
        CHECK (jsonb_typeof(balance_after) = 'object'),
    content_sha256 TEXT NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_agent_finance_ledger_source
        UNIQUE (entry_kind, source_type, source_id),
    CONSTRAINT ck_product_agent_finance_ledger_identifiers
        CHECK (
            id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND source_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
        ),
    CONSTRAINT ck_product_agent_finance_ledger_shape
        CHECK (
            (
                entry_kind = 'commission_credit'
                AND source_type = 'settlement'
                AND earned_delta_cents > 0
                AND clawback_delta_cents = 0
                AND reserved_delta_cents = 0
                AND withdrawn_delta_cents = 0
            )
            OR (
                entry_kind = 'commission_clawback'
                AND source_type IN ('settlement', 'commission_refund')
                AND earned_delta_cents = 0
                AND clawback_delta_cents > 0
                AND reserved_delta_cents = 0
                AND withdrawn_delta_cents = 0
            )
            OR (
                entry_kind = 'withdrawal_reserve'
                AND source_type = 'withdrawal'
                AND earned_delta_cents = 0
                AND clawback_delta_cents = 0
                AND reserved_delta_cents > 0
                AND withdrawn_delta_cents = 0
            )
            OR (
                entry_kind = 'withdrawal_release'
                AND source_type = 'withdrawal'
                AND earned_delta_cents = 0
                AND clawback_delta_cents = 0
                AND reserved_delta_cents < 0
                AND withdrawn_delta_cents = 0
            )
            OR (
                entry_kind = 'withdrawal_paid'
                AND source_type = 'withdrawal'
                AND earned_delta_cents = 0
                AND clawback_delta_cents = 0
                AND reserved_delta_cents < 0
                AND withdrawn_delta_cents = -reserved_delta_cents
            )
        ),
    CONSTRAINT ck_product_agent_finance_ledger_entry_kind
        CHECK (
            entry_kind IN (
                'commission_credit',
                'commission_clawback',
                'withdrawal_reserve',
                'withdrawal_release',
                'withdrawal_paid'
            )
        ),
    CONSTRAINT ck_product_agent_finance_ledger_source_type
        CHECK (
            source_type IN (
                'settlement',
                'commission_refund',
                'withdrawal'
            )
        )
);

ALTER TABLE product_agent_finance_ledger
    ADD COLUMN IF NOT EXISTS clawback_delta_cents BIGINT
        NOT NULL DEFAULT 0;

DO $$
DECLARE
    finance_constraint_name TEXT;
BEGIN
    FOR finance_constraint_name IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'product_agent_finance_ledger'::regclass
          AND contype = 'c'
          AND (
              pg_get_constraintdef(oid) LIKE '%entry_kind%'
              OR pg_get_constraintdef(oid) LIKE '%source_type%'
          )
    LOOP
        EXECUTE format(
            'ALTER TABLE product_agent_finance_ledger '
            'DROP CONSTRAINT %I',
            finance_constraint_name
        );
    END LOOP;

    ALTER TABLE product_agent_finance_ledger
        ADD CONSTRAINT ck_product_agent_finance_ledger_entry_kind
        CHECK (
            entry_kind IN (
                'commission_credit',
                'commission_clawback',
                'withdrawal_reserve',
                'withdrawal_release',
                'withdrawal_paid'
            )
        );
    ALTER TABLE product_agent_finance_ledger
        ADD CONSTRAINT ck_product_agent_finance_ledger_source_type
        CHECK (
            source_type IN (
                'settlement',
                'commission_refund',
                'withdrawal'
            )
        );
    ALTER TABLE product_agent_finance_ledger
        ADD CONSTRAINT ck_product_agent_finance_ledger_shape
        CHECK (
            (
                entry_kind = 'commission_credit'
                AND source_type = 'settlement'
                AND earned_delta_cents > 0
                AND clawback_delta_cents = 0
                AND reserved_delta_cents = 0
                AND withdrawn_delta_cents = 0
            )
            OR (
                entry_kind = 'commission_clawback'
                AND source_type IN ('settlement', 'commission_refund')
                AND earned_delta_cents = 0
                AND clawback_delta_cents > 0
                AND reserved_delta_cents = 0
                AND withdrawn_delta_cents = 0
            )
            OR (
                entry_kind = 'withdrawal_reserve'
                AND source_type = 'withdrawal'
                AND earned_delta_cents = 0
                AND clawback_delta_cents = 0
                AND reserved_delta_cents > 0
                AND withdrawn_delta_cents = 0
            )
            OR (
                entry_kind = 'withdrawal_release'
                AND source_type = 'withdrawal'
                AND earned_delta_cents = 0
                AND clawback_delta_cents = 0
                AND reserved_delta_cents < 0
                AND withdrawn_delta_cents = 0
            )
            OR (
                entry_kind = 'withdrawal_paid'
                AND source_type = 'withdrawal'
                AND earned_delta_cents = 0
                AND clawback_delta_cents = 0
                AND reserved_delta_cents < 0
                AND withdrawn_delta_cents = -reserved_delta_cents
            )
        );
END
$$;

CREATE INDEX IF NOT EXISTS idx_product_agent_finance_ledger_agent_created
    ON product_agent_finance_ledger (agent_id, created_at, id);

CREATE TABLE IF NOT EXISTS product_finance_audit_events (
    action_id TEXT PRIMARY KEY,
    actor_user_id TEXT NOT NULL,
    action_domain TEXT NOT NULL
        CHECK (
            action_domain IN (
                'points',
                'payment',
                'commission',
                'withdrawal',
                'risk'
            )
        ),
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('succeeded', 'failed', 'denied')),
    reason TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    content_sha256 TEXT NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_product_finance_audit_identifiers
        CHECK (
            action_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND actor_user_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND action ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND target_type ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND target_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
        )
);

CREATE INDEX IF NOT EXISTS idx_product_finance_audit_actor_created
    ON product_finance_audit_events (actor_user_id, created_at DESC, action_id);

CREATE INDEX IF NOT EXISTS idx_product_finance_audit_target_created
    ON product_finance_audit_events (
        target_type,
        target_id,
        created_at DESC,
        action_id
    );

CREATE INDEX IF NOT EXISTS idx_product_finance_audit_domain_created
    ON product_finance_audit_events (
        action_domain,
        action,
        created_at DESC,
        action_id
    );

CREATE TABLE IF NOT EXISTS product_commission_refunds (
    id TEXT PRIMARY KEY,
    commission_order_id TEXT NOT NULL
        REFERENCES product_commission_orders (id)
        ON DELETE RESTRICT,
    agent_id TEXT NOT NULL
        REFERENCES product_agent_finance_accounts (agent_id)
        ON DELETE RESTRICT,
    source_order_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    action_id TEXT NOT NULL
        REFERENCES product_finance_audit_events (action_id)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    request_sha256 TEXT NOT NULL
        CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    refund_amount_cents BIGINT NOT NULL
        CHECK (refund_amount_cents > 0),
    requested_cumulative_refunded_amount_cents BIGINT,
    cumulative_refunded_amount_cents BIGINT NOT NULL
        CHECK (cumulative_refunded_amount_cents > 0),
    commission_reversal_delta_cents BIGINT NOT NULL
        CHECK (commission_reversal_delta_cents >= 0),
    cumulative_reversed_commission_cents BIGINT NOT NULL
        CHECK (cumulative_reversed_commission_cents >= 0),
    account_clawback_delta_cents BIGINT NOT NULL DEFAULT 0
        CHECK (account_clawback_delta_cents >= 0),
    order_status_before TEXT NOT NULL,
    order_status_after TEXT NOT NULL,
    settlement_id TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_commission_refund_idempotency
        UNIQUE (agent_id, idempotency_key),
    CONSTRAINT ck_product_commission_refund_identifiers
        CHECK (
            id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND commission_order_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND source_order_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND idempotency_key
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND action_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND (
                settlement_id IS NULL
                OR settlement_id
                    ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            )
        ),
    CONSTRAINT ck_product_commission_refund_amounts
        CHECK (
            requested_cumulative_refunded_amount_cents IS NULL
            OR requested_cumulative_refunded_amount_cents > 0
        ),
    CONSTRAINT ck_product_commission_refund_reversal
        CHECK (
            commission_reversal_delta_cents
                <= cumulative_reversed_commission_cents
            AND account_clawback_delta_cents
                <= commission_reversal_delta_cents
            AND (
                (
                    order_status_before = 'settled'
                    AND account_clawback_delta_cents
                        = commission_reversal_delta_cents
                )
                OR (
                    order_status_before <> 'settled'
                    AND account_clawback_delta_cents = 0
                )
            )
        ),
    CONSTRAINT ck_product_commission_refund_states
        CHECK (
            order_status_before IN (
                'pending',
                'eligible',
                'claimed',
                'settled',
                'canceled',
                'refunded'
            )
            AND order_status_after IN (
                'pending',
                'eligible',
                'claimed',
                'settled',
                'canceled',
                'refunded'
            )
        )
);

CREATE INDEX IF NOT EXISTS idx_product_commission_refunds_order_created
    ON product_commission_refunds (
        commission_order_id,
        created_at,
        id
    );

CREATE INDEX IF NOT EXISTS idx_product_commission_refunds_agent_created
    ON product_commission_refunds (
        agent_id,
        created_at,
        id
    );

CREATE OR REPLACE FUNCTION product_finance_reject_immutable_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% is immutable', TG_TABLE_NAME
        USING ERRCODE = '55000';
END
$$;

DROP TRIGGER IF EXISTS trg_product_finance_audit_immutable
    ON product_finance_audit_events;
CREATE TRIGGER trg_product_finance_audit_immutable
    BEFORE UPDATE OR DELETE ON product_finance_audit_events
    FOR EACH ROW
    EXECUTE FUNCTION product_finance_reject_immutable_mutation();

DROP TRIGGER IF EXISTS trg_product_finance_ledger_immutable
    ON product_agent_finance_ledger;
CREATE TRIGGER trg_product_finance_ledger_immutable
    BEFORE UPDATE OR DELETE ON product_agent_finance_ledger
    FOR EACH ROW
    EXECUTE FUNCTION product_finance_reject_immutable_mutation();

DROP TRIGGER IF EXISTS trg_product_commission_refund_immutable
    ON product_commission_refunds;
CREATE TRIGGER trg_product_commission_refund_immutable
    BEFORE UPDATE OR DELETE ON product_commission_refunds
    FOR EACH ROW
    EXECUTE FUNCTION product_finance_reject_immutable_mutation();

COMMIT;
