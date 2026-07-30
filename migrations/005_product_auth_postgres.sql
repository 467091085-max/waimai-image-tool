BEGIN;

CREATE TABLE IF NOT EXISTS product_users (
    id TEXT PRIMARY KEY,
    phone TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled')),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMPTZ,
    CONSTRAINT ck_product_users_id
        CHECK (id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'),
    CONSTRAINT ck_product_users_phone
        CHECK (phone ~ '^\+861[0-9]{10}$'),
    CONSTRAINT ck_product_users_timestamps
        CHECK (
            updated_at >= created_at
            AND (last_login_at IS NULL OR last_login_at >= created_at)
        )
);

CREATE INDEX IF NOT EXISTS idx_product_users_status_created
    ON product_users (status, created_at, id);

CREATE TABLE IF NOT EXISTS product_stores (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
        CHECK (char_length(btrim(name)) BETWEEN 1 AND 120),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'archived')),
    created_by_user_id TEXT
        REFERENCES product_users (id) ON DELETE SET NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_product_stores_id
        CHECK (id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'),
    CONSTRAINT ck_product_stores_timestamps
        CHECK (updated_at >= created_at)
);

CREATE INDEX IF NOT EXISTS idx_product_stores_creator_created
    ON product_stores (created_by_user_id, created_at, id);

CREATE TABLE IF NOT EXISTS product_user_stores (
    user_id TEXT NOT NULL
        REFERENCES product_users (id) ON DELETE CASCADE,
    store_id TEXT NOT NULL
        REFERENCES product_stores (id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'owner'
        CHECK (role IN ('owner', 'admin', 'member')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, store_id)
);

CREATE INDEX IF NOT EXISTS idx_product_user_stores_store
    ON product_user_stores (store_id, user_id);

CREATE TABLE IF NOT EXISTS product_auth_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL
        REFERENCES product_users (id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE
        CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMPTZ,
    CONSTRAINT ck_product_auth_sessions_id
        CHECK (id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'),
    CONSTRAINT ck_product_auth_sessions_timestamps
        CHECK (
            expires_at > created_at
            AND last_seen_at >= created_at
            AND (revoked_at IS NULL OR revoked_at >= created_at)
        )
);

CREATE INDEX IF NOT EXISTS idx_product_auth_sessions_user_created
    ON product_auth_sessions (user_id, created_at DESC, id);

CREATE INDEX IF NOT EXISTS idx_product_auth_sessions_active_expiry
    ON product_auth_sessions (expires_at, id)
    WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS product_registration_security_events (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL
        REFERENCES product_users (id) ON DELETE CASCADE,
    session_id TEXT NOT NULL UNIQUE
        REFERENCES product_auth_sessions (id) ON DELETE CASCADE,
    is_new_user BOOLEAN NOT NULL,
    ip_hash TEXT
        CHECK (ip_hash IS NULL OR ip_hash ~ '^[0-9a-f]{64}$'),
    device_hash TEXT
        CHECK (device_hash IS NULL OR device_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_product_registration_security_events_id
        CHECK (id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$')
);

CREATE INDEX IF NOT EXISTS idx_product_registration_security_ip_created
    ON product_registration_security_events (ip_hash, created_at DESC, user_id)
    WHERE is_new_user AND ip_hash IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_product_registration_security_device_created
    ON product_registration_security_events (
        device_hash,
        created_at DESC,
        user_id
    )
    WHERE is_new_user AND device_hash IS NOT NULL;

COMMIT;
