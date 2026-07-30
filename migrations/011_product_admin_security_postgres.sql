BEGIN;

CREATE TABLE IF NOT EXISTS product_admin_audit_events (
    event_seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    action_id TEXT PRIMARY KEY,
    actor_user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'succeeded'
        CHECK (status IN ('succeeded', 'failed', 'denied')),
    reason TEXT NOT NULL DEFAULT ''
        CHECK (char_length(reason) <= 2048),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (
            jsonb_typeof(metadata) = 'object'
            AND octet_length(metadata::text) <= 65536
        ),
    content_sha256 TEXT NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_product_admin_audit_identifiers
        CHECK (
            action_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND actor_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND action ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND target_type
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND target_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
        )
);

CREATE INDEX IF NOT EXISTS idx_product_admin_audit_actor_created
    ON product_admin_audit_events (
        actor_user_id,
        created_at DESC,
        event_seq DESC
    );

CREATE INDEX IF NOT EXISTS idx_product_admin_audit_action_created
    ON product_admin_audit_events (
        action,
        created_at DESC,
        event_seq DESC
    );

CREATE INDEX IF NOT EXISTS idx_product_admin_audit_target_created
    ON product_admin_audit_events (
        target_type,
        target_id,
        created_at DESC,
        event_seq DESC
    );

CREATE INDEX IF NOT EXISTS idx_product_admin_audit_status_created
    ON product_admin_audit_events (
        status,
        created_at DESC,
        event_seq DESC
    );

CREATE TABLE IF NOT EXISTS product_risk_decisions (
    event_seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    action_id TEXT PRIMARY KEY,
    actor_user_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    subject_type TEXT NOT NULL
        CHECK (
            subject_type IN (
                'user',
                'ip',
                'phone',
                'device',
                'agent',
                'asset'
            )
        ),
    subject_value TEXT NOT NULL
        CHECK (
            btrim(subject_value) <> ''
            AND char_length(subject_value) <= 512
        ),
    risk_level TEXT NOT NULL DEFAULT 'info'
        CHECK (
            risk_level IN ('info', 'low', 'medium', 'high', 'critical')
        ),
    decision TEXT NOT NULL
        CHECK (decision IN ('allow', 'deny', 'review')),
    deny_reason TEXT NOT NULL DEFAULT ''
        CHECK (
            char_length(deny_reason) <= 2048
            AND (
                decision <> 'deny'
                OR btrim(deny_reason) <> ''
            )
        ),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (
            jsonb_typeof(metadata) = 'object'
            AND octet_length(metadata::text) <= 65536
        ),
    content_sha256 TEXT NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_product_risk_decision_identifiers
        CHECK (
            action_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND actor_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND event_type
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
        ),
    CONSTRAINT ck_product_risk_decision_subject
        CHECK (
            (
                subject_type IN ('user', 'agent', 'asset')
                AND subject_value
                    ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            )
            OR (
                subject_type = 'ip'
                AND subject_value ~ '^[0-9A-Fa-f:.]{2,45}$'
            )
            OR subject_type IN ('phone', 'device')
        )
);

CREATE INDEX IF NOT EXISTS idx_product_risk_subject_latest
    ON product_risk_decisions (
        subject_type,
        subject_value,
        created_at DESC,
        event_seq DESC
    );

CREATE INDEX IF NOT EXISTS idx_product_risk_decision_created
    ON product_risk_decisions (
        decision,
        created_at DESC,
        event_seq DESC
    );

CREATE INDEX IF NOT EXISTS idx_product_risk_level_created
    ON product_risk_decisions (
        risk_level,
        created_at DESC,
        event_seq DESC
    );

CREATE INDEX IF NOT EXISTS idx_product_risk_actor_created
    ON product_risk_decisions (
        actor_user_id,
        created_at DESC,
        event_seq DESC
    );

CREATE TABLE IF NOT EXISTS product_asset_access_events (
    event_seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    action_id TEXT PRIMARY KEY,
    actor_user_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    asset_id TEXT NOT NULL
        CHECK (
            char_length(asset_id) BETWEEN 1 AND 512
            AND asset_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+/-]*$'
            AND position(chr(92) IN asset_id) = 0
            AND position('://' IN asset_id) = 0
            AND position('//' IN asset_id) = 0
            AND asset_id !~ '(^|/)[.]{1,2}(/|$)'
            AND asset_id !~ '[[:cntrl:]]'
        ),
    asset_type TEXT NOT NULL,
    action TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    agent_id TEXT NOT NULL DEFAULT '',
    ip_address TEXT NOT NULL DEFAULT ''
        CHECK (
            ip_address = ''
            OR (
                char_length(ip_address) BETWEEN 2 AND 45
                AND ip_address ~ '^[0-9A-Fa-f:.]+$'
            )
        ),
    allowed BOOLEAN NOT NULL,
    deny_reason TEXT NOT NULL DEFAULT ''
        CHECK (
            char_length(deny_reason) <= 2048
            AND (
                (allowed AND deny_reason = '')
                OR (NOT allowed AND btrim(deny_reason) <> '')
            )
        ),
    user_agent TEXT NOT NULL DEFAULT ''
        CHECK (char_length(user_agent) <= 2048),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (
            jsonb_typeof(metadata) = 'object'
            AND octet_length(metadata::text) <= 65536
        ),
    content_sha256 TEXT NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_product_asset_access_identifiers
        CHECK (
            action_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND actor_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND request_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND asset_type
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND action ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND (
                user_id = ''
                OR user_id
                    ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            )
            AND (
                agent_id = ''
                OR agent_id
                    ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            )
        )
);

CREATE INDEX IF NOT EXISTS idx_product_asset_access_asset_created
    ON product_asset_access_events (
        asset_id,
        created_at DESC,
        event_seq DESC
    );

CREATE INDEX IF NOT EXISTS idx_product_asset_access_user_created
    ON product_asset_access_events (
        user_id,
        created_at DESC,
        event_seq DESC
    )
    WHERE user_id <> '';

CREATE INDEX IF NOT EXISTS idx_product_asset_access_request_created
    ON product_asset_access_events (
        request_id,
        created_at DESC,
        event_seq DESC
    );

CREATE INDEX IF NOT EXISTS idx_product_asset_access_actor_created
    ON product_asset_access_events (
        actor_user_id,
        created_at DESC,
        event_seq DESC
    );

CREATE INDEX IF NOT EXISTS idx_product_asset_access_denied_created
    ON product_asset_access_events (
        created_at DESC,
        event_seq DESC
    )
    WHERE NOT allowed;

CREATE OR REPLACE FUNCTION product_admin_security_reject_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% is immutable', TG_TABLE_NAME
        USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS trg_product_admin_audit_immutable
    ON product_admin_audit_events;
CREATE TRIGGER trg_product_admin_audit_immutable
    BEFORE UPDATE OR DELETE ON product_admin_audit_events
    FOR EACH ROW
    EXECUTE FUNCTION product_admin_security_reject_mutation();

DROP TRIGGER IF EXISTS trg_product_risk_decision_immutable
    ON product_risk_decisions;
CREATE TRIGGER trg_product_risk_decision_immutable
    BEFORE UPDATE OR DELETE ON product_risk_decisions
    FOR EACH ROW
    EXECUTE FUNCTION product_admin_security_reject_mutation();

DROP TRIGGER IF EXISTS trg_product_asset_access_immutable
    ON product_asset_access_events;
CREATE TRIGGER trg_product_asset_access_immutable
    BEFORE UPDATE OR DELETE ON product_asset_access_events
    FOR EACH ROW
    EXECUTE FUNCTION product_admin_security_reject_mutation();

COMMIT;
