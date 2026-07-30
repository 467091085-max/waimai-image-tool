BEGIN;

CREATE TABLE IF NOT EXISTS product_export_packages (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    generation_job_id TEXT NOT NULL,
    generation_request_sha256 TEXT NOT NULL
        CHECK (generation_request_sha256 ~ '^[0-9a-f]{64}$'),
    export_request_sha256 TEXT NOT NULL
        CHECK (export_request_sha256 ~ '^[0-9a-f]{64}$'),
    manifest_object_ref TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL
        CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    platform_set JSONB NOT NULL,
    watermark JSONB NOT NULL,
    zip_object_ref TEXT NOT NULL,
    zip_sha256 TEXT NOT NULL
        CHECK (zip_sha256 ~ '^[0-9a-f]{64}$'),
    zip_size_bytes BIGINT NOT NULL CHECK (zip_size_bytes >= 0),
    status TEXT NOT NULL DEFAULT 'ready'
        CHECK (status IN ('ready', 'failed', 'expired')),
    content_sha256 TEXT NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    download_count BIGINT NOT NULL DEFAULT 0
        CHECK (download_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_export_package_owner
        UNIQUE (id, owner_user_id),
    CONSTRAINT uq_product_export_package_idempotency
        UNIQUE (owner_user_id, idempotency_key),
    CONSTRAINT ck_product_export_package_identifiers
        CHECK (
            id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND owner_user_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND generation_job_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
        ),
    CONSTRAINT ck_product_export_package_manifest_ref
        CHECK (
            char_length(manifest_object_ref) BETWEEN 1 AND 1024
            AND manifest_object_ref ~ '^[A-Za-z0-9][A-Za-z0-9._/-]*$'
            AND left(manifest_object_ref, 1) <> '/'
            AND position(chr(92) IN manifest_object_ref) = 0
            AND position('://' IN manifest_object_ref) = 0
            AND position('//' IN manifest_object_ref) = 0
            AND manifest_object_ref !~ '(^|/)[.]{1,2}(/|$)'
            AND manifest_object_ref !~ '[[:cntrl:]]'
        ),
    CONSTRAINT ck_product_export_package_zip_ref
        CHECK (
            char_length(zip_object_ref) BETWEEN 1 AND 1024
            AND zip_object_ref ~ '^[A-Za-z0-9][A-Za-z0-9._/-]*$'
            AND left(zip_object_ref, 1) <> '/'
            AND position(chr(92) IN zip_object_ref) = 0
            AND position('://' IN zip_object_ref) = 0
            AND position('//' IN zip_object_ref) = 0
            AND zip_object_ref !~ '(^|/)[.]{1,2}(/|$)'
            AND zip_object_ref !~ '[[:cntrl:]]'
        ),
    CONSTRAINT ck_product_export_package_platform_set
        CHECK (
            jsonb_typeof(platform_set) = 'array'
            AND jsonb_array_length(platform_set) BETWEEN 1 AND 16
            AND NOT jsonb_path_exists(
                platform_set,
                '$[*] ? (@.type() != "string")'
            )
        ),
    CONSTRAINT ck_product_export_package_watermark
        CHECK (jsonb_typeof(watermark) = 'object'),
    CONSTRAINT ck_product_export_package_timestamps
        CHECK (updated_at >= created_at)
);

CREATE INDEX IF NOT EXISTS idx_product_export_packages_owner_created
    ON product_export_packages (owner_user_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_product_export_packages_owner_status
    ON product_export_packages (
        owner_user_id,
        status,
        created_at DESC,
        id DESC
    );

CREATE INDEX IF NOT EXISTS idx_product_export_packages_generation_job
    ON product_export_packages (generation_job_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_product_export_packages_request
    ON product_export_packages (
        owner_user_id,
        export_request_sha256,
        created_at DESC
    );

CREATE TABLE IF NOT EXISTS product_export_token_nonces (
    token_nonce_digest TEXT PRIMARY KEY
        CHECK (token_nonce_digest ~ '^[0-9a-f]{64}$'),
    owner_user_id TEXT NOT NULL,
    export_id TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    consumed_action_id TEXT,
    consumed_request_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_export_nonce_scope
        UNIQUE (token_nonce_digest, export_id, owner_user_id),
    CONSTRAINT fk_product_export_nonce_package
        FOREIGN KEY (export_id, owner_user_id)
        REFERENCES product_export_packages (id, owner_user_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_product_export_nonce_identifiers
        CHECK (
            owner_user_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND export_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND (
                consumed_action_id IS NULL
                OR consumed_action_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            )
            AND (
                consumed_request_id IS NULL
                OR consumed_request_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            )
        ),
    CONSTRAINT ck_product_export_nonce_consumption
        CHECK (
            (
                consumed_at IS NULL
                AND consumed_action_id IS NULL
                AND consumed_request_id IS NULL
            )
            OR (
                consumed_at IS NOT NULL
                AND consumed_action_id IS NOT NULL
                AND consumed_request_id IS NOT NULL
            )
        )
);

CREATE INDEX IF NOT EXISTS idx_product_export_nonces_export_expiry
    ON product_export_token_nonces (
        export_id,
        owner_user_id,
        expires_at
    );

CREATE INDEX IF NOT EXISTS idx_product_export_nonces_unconsumed_expiry
    ON product_export_token_nonces (expires_at, token_nonce_digest)
    WHERE consumed_at IS NULL;

CREATE TABLE IF NOT EXISTS product_export_access_audits (
    action_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    owner_user_id TEXT NOT NULL,
    export_id TEXT NOT NULL,
    token_nonce_digest TEXT
        CHECK (
            token_nonce_digest IS NULL
            OR token_nonce_digest ~ '^[0-9a-f]{64}$'
        ),
    ip_digest TEXT NOT NULL
        CHECK (ip_digest ~ '^[0-9a-f]{64}$'),
    allowed BOOLEAN NOT NULL,
    deny_reason TEXT NOT NULL DEFAULT '',
    one_time_required BOOLEAN NOT NULL DEFAULT TRUE,
    nonce_consumed BOOLEAN NOT NULL DEFAULT FALSE,
    download_count_after BIGINT NOT NULL
        CHECK (download_count_after >= 0),
    request_sha256 TEXT NOT NULL
        CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    content_sha256 TEXT NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_product_export_access_package
        FOREIGN KEY (export_id, owner_user_id)
        REFERENCES product_export_packages (id, owner_user_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_product_export_access_identifiers
        CHECK (
            action_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND request_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND owner_user_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND export_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
        ),
    CONSTRAINT ck_product_export_access_decision
        CHECK (
            (
                allowed
                AND deny_reason = ''
                AND nonce_consumed
                AND token_nonce_digest IS NOT NULL
            )
            OR (
                NOT allowed
                AND deny_reason <> ''
                AND NOT nonce_consumed
            )
        ),
    CONSTRAINT ck_product_export_access_one_time
        CHECK (one_time_required)
);

CREATE INDEX IF NOT EXISTS idx_product_export_access_owner_created
    ON product_export_access_audits (
        owner_user_id,
        created_at DESC,
        action_id DESC
    );

CREATE INDEX IF NOT EXISTS idx_product_export_access_export_created
    ON product_export_access_audits (
        export_id,
        created_at DESC,
        action_id DESC
    );

CREATE INDEX IF NOT EXISTS idx_product_export_access_nonce_created
    ON product_export_access_audits (
        token_nonce_digest,
        created_at DESC,
        action_id DESC
    )
    WHERE token_nonce_digest IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_product_export_access_denied_created
    ON product_export_access_audits (
        deny_reason,
        created_at DESC,
        action_id DESC
    )
    WHERE NOT allowed;

COMMIT;
