BEGIN;

CREATE TABLE IF NOT EXISTS product_growth_subject_locks (
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, owner_user_id),
    CONSTRAINT ck_product_growth_subject_lock_identifiers
        CHECK (
            tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
            AND owner_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
        )
);

CREATE TABLE IF NOT EXISTS product_growth_agents (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    agent_code TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'suspended', 'closed')),
    rule_version TEXT NOT NULL,
    commission_depth SMALLINT NOT NULL DEFAULT 1
        CHECK (commission_depth = 1),
    first_order_commission_bps INTEGER NOT NULL
        CHECK (first_order_commission_bps BETWEEN 1 AND 10000),
    repeat_order_commission_bps INTEGER NOT NULL
        CHECK (repeat_order_commission_bps BETWEEN 1 AND 10000),
    request_sha256 TEXT NOT NULL
        CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_growth_agent_owner
        UNIQUE (tenant_id, owner_user_id),
    CONSTRAINT uq_product_growth_agent_idempotency
        UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT uq_product_growth_agent_tenant_owner
        UNIQUE (id, tenant_id, owner_user_id),
    CONSTRAINT ck_product_growth_agent_identifiers
        CHECK (
            id ~ '^growth_agent_[0-9a-f]{40}$'
            AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
            AND owner_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND idempotency_key
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND rule_version
                ~ '^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$'
            AND (
                agent_code = ''
                OR agent_code ~ '^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$'
            )
        ),
    CONSTRAINT ck_product_growth_agent_rates
        CHECK (
            first_order_commission_bps >= repeat_order_commission_bps
        ),
    CONSTRAINT ck_product_growth_agent_timestamps
        CHECK (updated_at >= created_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_product_growth_agent_code
    ON product_growth_agents (tenant_id, agent_code)
    WHERE agent_code <> '';

CREATE INDEX IF NOT EXISTS idx_product_growth_agents_status
    ON product_growth_agents (
        tenant_id,
        status,
        created_at,
        id
    );

CREATE TABLE IF NOT EXISTS product_growth_agent_bindings (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    agent_owner_user_id TEXT NOT NULL,
    customer_user_id TEXT NOT NULL,
    relation_depth SMALLINT NOT NULL DEFAULT 1
        CHECK (relation_depth = 1),
    source TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    first_order_commission_bps INTEGER NOT NULL
        CHECK (first_order_commission_bps BETWEEN 1 AND 10000),
    repeat_order_commission_bps INTEGER NOT NULL
        CHECK (repeat_order_commission_bps BETWEEN 1 AND 10000),
    request_sha256 TEXT NOT NULL
        CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_growth_agent_binding_customer
        UNIQUE (tenant_id, customer_user_id),
    CONSTRAINT uq_product_growth_agent_binding_idempotency
        UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT uq_product_growth_agent_binding_tenant_owner
        UNIQUE (id, tenant_id, owner_user_id),
    CONSTRAINT fk_product_growth_agent_binding_agent
        FOREIGN KEY (agent_id, tenant_id, agent_owner_user_id)
        REFERENCES product_growth_agents (id, tenant_id, owner_user_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_product_growth_agent_binding_identifiers
        CHECK (
            id ~ '^growth_binding_[0-9a-f]{40}$'
            AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
            AND owner_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND idempotency_key
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND agent_id ~ '^growth_agent_[0-9a-f]{40}$'
            AND agent_owner_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND customer_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND source
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND rule_version
                ~ '^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$'
        ),
    CONSTRAINT ck_product_growth_agent_binding_owner
        CHECK (
            owner_user_id = customer_user_id
            AND agent_owner_user_id <> customer_user_id
        ),
    CONSTRAINT ck_product_growth_agent_binding_rates
        CHECK (
            first_order_commission_bps
                >= repeat_order_commission_bps
        )
);

CREATE INDEX IF NOT EXISTS idx_product_growth_agent_bindings_agent
    ON product_growth_agent_bindings (
        tenant_id,
        agent_id,
        created_at,
        id
    );

CREATE TABLE IF NOT EXISTS product_growth_invite_codes (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    code_sha256 TEXT NOT NULL
        CHECK (code_sha256 ~ '^[0-9a-f]{64}$'),
    rule_version TEXT NOT NULL,
    request_sha256 TEXT NOT NULL
        CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_growth_invite_code_owner
        UNIQUE (tenant_id, owner_user_id),
    CONSTRAINT uq_product_growth_invite_code_idempotency
        UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT uq_product_growth_invite_code_digest
        UNIQUE (tenant_id, code_sha256),
    CONSTRAINT uq_product_growth_invite_code_tenant_owner
        UNIQUE (id, tenant_id, owner_user_id),
    CONSTRAINT ck_product_growth_invite_code_identifiers
        CHECK (
            id ~ '^growth_invite_code_[0-9a-f]{40}$'
            AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
            AND owner_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND idempotency_key
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND rule_version
                ~ '^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$'
        )
);

CREATE INDEX IF NOT EXISTS idx_product_growth_invite_codes_owner_created
    ON product_growth_invite_codes (
        tenant_id,
        owner_user_id,
        created_at DESC,
        id
    );

CREATE TABLE IF NOT EXISTS product_growth_invite_relations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    inviter_user_id TEXT NOT NULL,
    invitee_user_id TEXT NOT NULL,
    invite_code_id TEXT NOT NULL,
    invite_code_sha256 TEXT NOT NULL
        CHECK (invite_code_sha256 ~ '^[0-9a-f]{64}$'),
    relation_depth SMALLINT NOT NULL DEFAULT 1
        CHECK (relation_depth = 1),
    rule_version TEXT NOT NULL,
    registration_inviter_points BIGINT NOT NULL
        CHECK (registration_inviter_points > 0),
    registration_invitee_points BIGINT NOT NULL
        CHECK (registration_invitee_points > 0),
    first_recharge_rebate_percent INTEGER NOT NULL
        CHECK (first_recharge_rebate_percent BETWEEN 1 AND 100),
    cash_points_per_yuan INTEGER NOT NULL
        CHECK (cash_points_per_yuan > 0),
    cents_per_yuan INTEGER NOT NULL
        CHECK (cents_per_yuan > 0),
    request_sha256 TEXT NOT NULL
        CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    risk_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(risk_snapshot) = 'object'),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_growth_invite_invitee
        UNIQUE (tenant_id, invitee_user_id),
    CONSTRAINT uq_product_growth_invite_idempotency
        UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT uq_product_growth_invite_tenant_owner
        UNIQUE (id, tenant_id, owner_user_id),
    CONSTRAINT fk_product_growth_invite_code
        FOREIGN KEY (invite_code_id, tenant_id, inviter_user_id)
        REFERENCES product_growth_invite_codes (
            id,
            tenant_id,
            owner_user_id
        )
        ON DELETE RESTRICT,
    CONSTRAINT ck_product_growth_invite_identifiers
        CHECK (
            id ~ '^growth_invite_[0-9a-f]{40}$'
            AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
            AND owner_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND idempotency_key
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND inviter_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND invitee_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND invite_code_id
                ~ '^growth_invite_code_[0-9a-f]{40}$'
            AND rule_version
                ~ '^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$'
        ),
    CONSTRAINT ck_product_growth_invite_owner
        CHECK (
            owner_user_id = invitee_user_id
            AND inviter_user_id <> invitee_user_id
        )
);

CREATE INDEX IF NOT EXISTS idx_product_growth_invites_inviter
    ON product_growth_invite_relations (
        tenant_id,
        inviter_user_id,
        created_at,
        id
    );

CREATE TABLE IF NOT EXISTS product_growth_business_events (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    business_key TEXT NOT NULL,
    event_type TEXT NOT NULL
        CHECK (
            event_type IN (
                'consumer.registration_reward',
                'consumer.first_recharge_reward',
                'consumer.first_recharge_refund'
            )
        ),
    invite_relation_id TEXT NOT NULL,
    source_order_id TEXT NOT NULL DEFAULT '',
    rule_version TEXT NOT NULL,
    payload JSONB NOT NULL
        CHECK (jsonb_typeof(payload) = 'object'),
    payload_sha256 TEXT NOT NULL
        CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    outcome TEXT NOT NULL
        CHECK (outcome IN ('applied', 'ignored')),
    outcome_reason TEXT NOT NULL DEFAULT '',
    result_payload JSONB NOT NULL
        CHECK (jsonb_typeof(result_payload) = 'object'),
    result_sha256 TEXT NOT NULL
        CHECK (result_sha256 ~ '^[0-9a-f]{64}$'),
    occurred_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_growth_event_idempotency
        UNIQUE (tenant_id, owner_user_id, idempotency_key),
    CONSTRAINT uq_product_growth_event_source
        UNIQUE (tenant_id, source_event_id),
    CONSTRAINT uq_product_growth_event_business
        UNIQUE (tenant_id, event_type, business_key),
    CONSTRAINT uq_product_growth_event_tenant_owner
        UNIQUE (id, tenant_id, owner_user_id),
    CONSTRAINT fk_product_growth_event_invite
        FOREIGN KEY (invite_relation_id, tenant_id, owner_user_id)
        REFERENCES product_growth_invite_relations (
            id,
            tenant_id,
            owner_user_id
        )
        ON DELETE RESTRICT,
    CONSTRAINT ck_product_growth_event_identifiers
        CHECK (
            id ~ '^growth_event_[0-9a-f]{40}$'
            AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
            AND owner_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND idempotency_key
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND source_event_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND business_key
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND invite_relation_id
                ~ '^growth_invite_[0-9a-f]{40}$'
            AND (
                source_order_id = ''
                OR source_order_id
                    ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            )
            AND rule_version
                ~ '^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$'
        ),
    CONSTRAINT ck_product_growth_event_shape
        CHECK (
            (
                event_type = 'consumer.registration_reward'
                AND source_order_id = ''
            )
            OR (
                event_type IN (
                    'consumer.first_recharge_reward',
                    'consumer.first_recharge_refund'
                )
                AND source_order_id <> ''
            )
        ),
    CONSTRAINT ck_product_growth_event_outcome
        CHECK (
            (outcome = 'applied' AND outcome_reason = '')
            OR (outcome = 'ignored' AND outcome_reason <> '')
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_product_growth_applied_reward
    ON product_growth_business_events (
        tenant_id,
        invite_relation_id,
        event_type
    )
    WHERE outcome = 'applied'
      AND event_type IN (
          'consumer.registration_reward',
          'consumer.first_recharge_reward'
      );

CREATE INDEX IF NOT EXISTS idx_product_growth_events_owner_created
    ON product_growth_business_events (
        tenant_id,
        owner_user_id,
        created_at DESC,
        id DESC
    );

CREATE TABLE IF NOT EXISTS product_growth_reward_grants (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    business_event_id TEXT NOT NULL,
    event_owner_user_id TEXT NOT NULL,
    invite_relation_id TEXT NOT NULL,
    reward_kind TEXT NOT NULL
        CHECK (
            reward_kind IN (
                'registration_inviter',
                'registration_invitee',
                'first_recharge_inviter'
            )
    ),
    points BIGINT NOT NULL CHECK (points > 0),
    debt_offset_points BIGINT NOT NULL DEFAULT 0,
    net_wallet_points BIGINT,
    wallet_order_id TEXT,
    rule_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_growth_reward_grant_event
        UNIQUE (
            tenant_id,
            business_event_id,
            reward_kind,
            owner_user_id
        ),
    CONSTRAINT uq_product_growth_reward_wallet_order
        UNIQUE (wallet_order_id),
    CONSTRAINT fk_product_growth_reward_event
        FOREIGN KEY (
            business_event_id,
            tenant_id,
            event_owner_user_id
        )
        REFERENCES product_growth_business_events (
            id,
            tenant_id,
            owner_user_id
        )
        ON DELETE RESTRICT,
    CONSTRAINT fk_product_growth_reward_invite
        FOREIGN KEY (
            invite_relation_id,
            tenant_id,
            event_owner_user_id
        )
        REFERENCES product_growth_invite_relations (
            id,
            tenant_id,
            owner_user_id
        )
        ON DELETE RESTRICT,
    CONSTRAINT fk_product_growth_reward_wallet
        FOREIGN KEY (wallet_order_id, owner_user_id)
        REFERENCES product_point_orders (id, owner_user_id)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT ck_product_growth_reward_identifiers
        CHECK (
            id ~ '^growth_grant_[0-9a-f]{40}$'
            AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
            AND owner_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND business_event_id
                ~ '^growth_event_[0-9a-f]{40}$'
            AND event_owner_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND invite_relation_id
                ~ '^growth_invite_[0-9a-f]{40}$'
            AND (
                wallet_order_id IS NULL
                OR wallet_order_id
                    ~ '^growth_credit_[0-9a-f]{40}$'
            )
            AND rule_version
                ~ '^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$'
        )
);

DROP TRIGGER IF EXISTS trg_product_growth_reward_grants_immutable
    ON product_growth_reward_grants;

ALTER TABLE product_growth_reward_grants
    ADD COLUMN IF NOT EXISTS debt_offset_points BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS net_wallet_points BIGINT;

UPDATE product_growth_reward_grants
SET net_wallet_points = points - debt_offset_points
WHERE net_wallet_points IS NULL;

ALTER TABLE product_growth_reward_grants
    ALTER COLUMN wallet_order_id DROP NOT NULL,
    ALTER COLUMN debt_offset_points SET NOT NULL,
    ALTER COLUMN net_wallet_points SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_product_growth_reward_split'
          AND conrelid = 'product_growth_reward_grants'::regclass
    ) THEN
        ALTER TABLE product_growth_reward_grants
            ADD CONSTRAINT ck_product_growth_reward_split
            CHECK (
                debt_offset_points >= 0
                AND net_wallet_points >= 0
                AND debt_offset_points + net_wallet_points = points
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_product_growth_reward_wallet_effect'
          AND conrelid = 'product_growth_reward_grants'::regclass
    ) THEN
        ALTER TABLE product_growth_reward_grants
            ADD CONSTRAINT ck_product_growth_reward_wallet_effect
            CHECK (
                (
                    net_wallet_points = 0
                    AND wallet_order_id IS NULL
                )
                OR (
                    net_wallet_points > 0
                    AND wallet_order_id IS NOT NULL
                )
            );
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS idx_product_growth_rewards_owner_created
    ON product_growth_reward_grants (
        tenant_id,
        owner_user_id,
        created_at DESC,
        id DESC
    );

CREATE TABLE IF NOT EXISTS product_growth_reward_reversal_states (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    invite_relation_id TEXT NOT NULL,
    source_order_id TEXT NOT NULL,
    original_business_event_id TEXT NOT NULL,
    original_reward_grant_id TEXT NOT NULL,
    reward_owner_user_id TEXT NOT NULL,
    original_paid_cents BIGINT NOT NULL
        CHECK (original_paid_cents > 0),
    original_reward_points BIGINT NOT NULL
        CHECK (original_reward_points > 0),
    max_cumulative_refunded_cents BIGINT NOT NULL DEFAULT 0
        CHECK (
            max_cumulative_refunded_cents >= 0
            AND max_cumulative_refunded_cents <= original_paid_cents
        ),
    reversed_points BIGINT NOT NULL DEFAULT 0
        CHECK (
            reversed_points >= 0
            AND reversed_points <= original_reward_points
        ),
    recovered_points BIGINT NOT NULL DEFAULT 0
        CHECK (
            recovered_points >= 0
            AND recovered_points <= reversed_points
        ),
    outstanding_points BIGINT NOT NULL DEFAULT 0
        CHECK (
            outstanding_points >= 0
            AND outstanding_points <= reversed_points
        ),
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_growth_reversal_state_order
        UNIQUE (tenant_id, source_order_id),
    CONSTRAINT uq_product_growth_reversal_state_tenant_owner
        UNIQUE (id, tenant_id, owner_user_id),
    CONSTRAINT fk_product_growth_reversal_state_event
        FOREIGN KEY (
            original_business_event_id,
            tenant_id,
            owner_user_id
        )
        REFERENCES product_growth_business_events (
            id,
            tenant_id,
            owner_user_id
        )
        ON DELETE RESTRICT,
    CONSTRAINT fk_product_growth_reversal_state_invite
        FOREIGN KEY (
            invite_relation_id,
            tenant_id,
            owner_user_id
        )
        REFERENCES product_growth_invite_relations (
            id,
            tenant_id,
            owner_user_id
        )
        ON DELETE RESTRICT,
    CONSTRAINT fk_product_growth_reversal_state_grant
        FOREIGN KEY (original_reward_grant_id)
        REFERENCES product_growth_reward_grants (id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_product_growth_reversal_state_identifiers
        CHECK (
            id ~ '^growth_reversal_state_[0-9a-f]{40}$'
            AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
            AND owner_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND invite_relation_id
                ~ '^growth_invite_[0-9a-f]{40}$'
            AND source_order_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND original_business_event_id
                ~ '^growth_event_[0-9a-f]{40}$'
            AND original_reward_grant_id
                ~ '^growth_grant_[0-9a-f]{40}$'
            AND reward_owner_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
        ),
    CONSTRAINT ck_product_growth_reversal_state_timestamps
        CHECK (updated_at >= created_at),
    CONSTRAINT ck_product_growth_reversal_state_balance
        CHECK (recovered_points + outstanding_points = reversed_points)
);

CREATE INDEX IF NOT EXISTS idx_product_growth_reversal_state_owner
    ON product_growth_reward_reversal_states (
        tenant_id,
        owner_user_id,
        updated_at DESC,
        id
    );

CREATE TABLE IF NOT EXISTS product_growth_reward_debts (
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    outstanding_points BIGINT NOT NULL DEFAULT 0
        CHECK (outstanding_points >= 0),
    lifetime_assessed_points BIGINT NOT NULL DEFAULT 0
        CHECK (lifetime_assessed_points >= 0),
    lifetime_recovered_points BIGINT NOT NULL DEFAULT 0
        CHECK (lifetime_recovered_points >= 0),
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, owner_user_id),
    CONSTRAINT ck_product_growth_reward_debt_identifiers
        CHECK (
            tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
            AND owner_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
        ),
    CONSTRAINT ck_product_growth_reward_debt_balance
        CHECK (
            lifetime_recovered_points <= lifetime_assessed_points
            AND outstanding_points
                = lifetime_assessed_points - lifetime_recovered_points
        ),
    CONSTRAINT ck_product_growth_reward_debt_timestamps
        CHECK (updated_at >= created_at)
);

CREATE INDEX IF NOT EXISTS idx_product_growth_reward_debts_outstanding
    ON product_growth_reward_debts (
        tenant_id,
        outstanding_points DESC,
        owner_user_id
    )
    WHERE outstanding_points > 0;

CREATE TABLE IF NOT EXISTS product_growth_reward_debt_recoveries (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    business_event_id TEXT NOT NULL,
    event_owner_user_id TEXT NOT NULL,
    reward_grant_id TEXT NOT NULL,
    reward_kind TEXT NOT NULL
        CHECK (
            reward_kind IN (
                'registration_inviter',
                'registration_invitee',
                'first_recharge_inviter'
            )
        ),
    gross_reward_points BIGINT NOT NULL
        CHECK (gross_reward_points > 0),
    recovered_points BIGINT NOT NULL
        CHECK (recovered_points > 0),
    net_wallet_points BIGINT NOT NULL
        CHECK (net_wallet_points >= 0),
    debt_outstanding_before BIGINT NOT NULL
        CHECK (debt_outstanding_before > 0),
    debt_outstanding_after BIGINT NOT NULL
        CHECK (debt_outstanding_after >= 0),
    rule_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_growth_debt_recovery_grant
        UNIQUE (reward_grant_id),
    CONSTRAINT uq_product_growth_debt_recovery_event_owner
        UNIQUE (
            tenant_id,
            business_event_id,
            reward_kind,
            owner_user_id
        ),
    CONSTRAINT fk_product_growth_debt_recovery_event
        FOREIGN KEY (
            business_event_id,
            tenant_id,
            event_owner_user_id
        )
        REFERENCES product_growth_business_events (
            id,
            tenant_id,
            owner_user_id
        )
        ON DELETE RESTRICT,
    CONSTRAINT fk_product_growth_debt_recovery_grant
        FOREIGN KEY (reward_grant_id)
        REFERENCES product_growth_reward_grants (id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_product_growth_debt_recovery_debt
        FOREIGN KEY (tenant_id, owner_user_id)
        REFERENCES product_growth_reward_debts (
            tenant_id,
            owner_user_id
        )
        ON DELETE RESTRICT,
    CONSTRAINT ck_product_growth_debt_recovery_identifiers
        CHECK (
            id ~ '^growth_debt_recovery_[0-9a-f]{40}$'
            AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
            AND owner_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND business_event_id
                ~ '^growth_event_[0-9a-f]{40}$'
            AND event_owner_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND reward_grant_id
                ~ '^growth_grant_[0-9a-f]{40}$'
            AND rule_version
                ~ '^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$'
        ),
    CONSTRAINT ck_product_growth_debt_recovery_balance
        CHECK (
            recovered_points + net_wallet_points
                = gross_reward_points
            AND debt_outstanding_before - recovered_points
                = debt_outstanding_after
        )
);

CREATE INDEX IF NOT EXISTS idx_product_growth_debt_recoveries_owner
    ON product_growth_reward_debt_recoveries (
        tenant_id,
        owner_user_id,
        created_at DESC,
        id DESC
    );

CREATE TABLE IF NOT EXISTS product_growth_reward_reversals (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    business_event_id TEXT NOT NULL,
    event_owner_user_id TEXT NOT NULL,
    reversal_state_id TEXT NOT NULL,
    original_reward_grant_id TEXT NOT NULL,
    reversal_kind TEXT NOT NULL
        CHECK (reversal_kind = 'first_recharge_inviter'),
    assessed_points BIGINT NOT NULL CHECK (assessed_points > 0),
    recovered_points BIGINT NOT NULL
        CHECK (
            recovered_points >= 0
            AND recovered_points <= assessed_points
        ),
    outstanding_points_added BIGINT NOT NULL
        CHECK (
            outstanding_points_added >= 0
            AND outstanding_points_added <= assessed_points
        ),
    outstanding_points_after BIGINT NOT NULL
        CHECK (outstanding_points_after >= 0),
    wallet_order_id TEXT,
    cumulative_refunded_cents BIGINT NOT NULL
        CHECK (cumulative_refunded_cents > 0),
    cumulative_reversed_points BIGINT NOT NULL
        CHECK (cumulative_reversed_points > 0),
    rule_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_growth_reversal_event
        UNIQUE (tenant_id, business_event_id),
    CONSTRAINT fk_product_growth_reversal_event
        FOREIGN KEY (
            business_event_id,
            tenant_id,
            event_owner_user_id
        )
        REFERENCES product_growth_business_events (
            id,
            tenant_id,
            owner_user_id
        )
        ON DELETE RESTRICT,
    CONSTRAINT fk_product_growth_reversal_state
        FOREIGN KEY (
            reversal_state_id,
            tenant_id,
            event_owner_user_id
        )
        REFERENCES product_growth_reward_reversal_states (
            id,
            tenant_id,
            owner_user_id
        )
        ON DELETE RESTRICT,
    CONSTRAINT fk_product_growth_reversal_original_grant
        FOREIGN KEY (original_reward_grant_id)
        REFERENCES product_growth_reward_grants (id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_product_growth_reversal_wallet
        FOREIGN KEY (wallet_order_id, owner_user_id)
        REFERENCES product_point_orders (id, owner_user_id)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT ck_product_growth_reversal_identifiers
        CHECK (
            id ~ '^growth_reversal_[0-9a-f]{40}$'
            AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
            AND owner_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND business_event_id
                ~ '^growth_event_[0-9a-f]{40}$'
            AND event_owner_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND reversal_state_id
                ~ '^growth_reversal_state_[0-9a-f]{40}$'
            AND original_reward_grant_id
                ~ '^growth_grant_[0-9a-f]{40}$'
            AND (
                wallet_order_id IS NULL
                OR wallet_order_id
                    ~ '^growth_debit_[0-9a-f]{40}$'
            )
            AND rule_version
                ~ '^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$'
        ),
    CONSTRAINT ck_product_growth_reversal_recovery
        CHECK (
            recovered_points + outstanding_points_added
                = assessed_points
            AND (
                (recovered_points = 0 AND wallet_order_id IS NULL)
                OR (recovered_points > 0 AND wallet_order_id IS NOT NULL)
            )
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_product_growth_reversal_wallet_order
    ON product_growth_reward_reversals (wallet_order_id)
    WHERE wallet_order_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_product_growth_reversals_owner_created
    ON product_growth_reward_reversals (
        tenant_id,
        owner_user_id,
        created_at DESC,
        id DESC
    );

CREATE TABLE IF NOT EXISTS product_growth_audit_events (
    action_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    actor_user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('succeeded', 'ignored')),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    content_sha256 TEXT NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_growth_audit_tenant_action
        UNIQUE (tenant_id, action_id),
    CONSTRAINT ck_product_growth_audit_identifiers
        CHECK (
            action_id ~ '^growth_audit_[0-9a-f]{40}$'
            AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
            AND owner_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND actor_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND action
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND target_type
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND target_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
        )
);

CREATE INDEX IF NOT EXISTS idx_product_growth_audit_owner_created
    ON product_growth_audit_events (
        tenant_id,
        owner_user_id,
        created_at DESC,
        action_id DESC
    );

CREATE OR REPLACE FUNCTION product_growth_reject_immutable_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% is immutable', TG_TABLE_NAME
        USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS trg_product_growth_business_events_immutable
    ON product_growth_business_events;
CREATE TRIGGER trg_product_growth_business_events_immutable
BEFORE UPDATE OR DELETE ON product_growth_business_events
FOR EACH ROW
EXECUTE FUNCTION product_growth_reject_immutable_mutation();

DROP TRIGGER IF EXISTS trg_product_growth_agent_bindings_immutable
    ON product_growth_agent_bindings;
CREATE TRIGGER trg_product_growth_agent_bindings_immutable
BEFORE UPDATE OR DELETE ON product_growth_agent_bindings
FOR EACH ROW
EXECUTE FUNCTION product_growth_reject_immutable_mutation();

DROP TRIGGER IF EXISTS trg_product_growth_invite_codes_immutable
    ON product_growth_invite_codes;
CREATE TRIGGER trg_product_growth_invite_codes_immutable
BEFORE UPDATE OR DELETE ON product_growth_invite_codes
FOR EACH ROW
EXECUTE FUNCTION product_growth_reject_immutable_mutation();

DROP TRIGGER IF EXISTS trg_product_growth_invite_relations_immutable
    ON product_growth_invite_relations;
CREATE TRIGGER trg_product_growth_invite_relations_immutable
BEFORE UPDATE OR DELETE ON product_growth_invite_relations
FOR EACH ROW
EXECUTE FUNCTION product_growth_reject_immutable_mutation();

DROP TRIGGER IF EXISTS trg_product_growth_reward_grants_immutable
    ON product_growth_reward_grants;
CREATE TRIGGER trg_product_growth_reward_grants_immutable
BEFORE UPDATE OR DELETE ON product_growth_reward_grants
FOR EACH ROW
EXECUTE FUNCTION product_growth_reject_immutable_mutation();

DROP TRIGGER IF EXISTS trg_product_growth_debt_recoveries_immutable
    ON product_growth_reward_debt_recoveries;
CREATE TRIGGER trg_product_growth_debt_recoveries_immutable
BEFORE UPDATE OR DELETE ON product_growth_reward_debt_recoveries
FOR EACH ROW
EXECUTE FUNCTION product_growth_reject_immutable_mutation();

DROP TRIGGER IF EXISTS trg_product_growth_reward_reversals_immutable
    ON product_growth_reward_reversals;
CREATE TRIGGER trg_product_growth_reward_reversals_immutable
BEFORE UPDATE OR DELETE ON product_growth_reward_reversals
FOR EACH ROW
EXECUTE FUNCTION product_growth_reject_immutable_mutation();

DROP TRIGGER IF EXISTS trg_product_growth_audit_events_immutable
    ON product_growth_audit_events;
CREATE TRIGGER trg_product_growth_audit_events_immutable
BEFORE UPDATE OR DELETE ON product_growth_audit_events
FOR EACH ROW
EXECUTE FUNCTION product_growth_reject_immutable_mutation();

COMMIT;
