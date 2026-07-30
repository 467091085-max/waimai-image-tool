BEGIN;

CREATE TABLE IF NOT EXISTS product_library_import_batches (
    id TEXT PRIMARY KEY
        CHECK (id ~ '^library_import_[0-9a-f]{40}$'),
    tenant_id TEXT NOT NULL
        CHECK (
            tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
        ),
    actor_user_id TEXT NOT NULL
        CHECK (
            actor_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
        ),
    idempotency_key TEXT NOT NULL
        CHECK (
            idempotency_key
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
        ),
    request_sha256 TEXT NOT NULL
        CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    asset_count INTEGER NOT NULL
        CHECK (asset_count BETWEEN 1 AND 200),
    content_sha256 TEXT NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_library_import_idempotency
        UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_product_library_import_actor_created
    ON product_library_import_batches (
        actor_user_id,
        created_at DESC,
        id DESC
    );

CREATE TABLE IF NOT EXISTS product_library_import_items (
    batch_id TEXT NOT NULL,
    item_index INTEGER NOT NULL
        CHECK (item_index BETWEEN 0 AND 199),
    member_name TEXT NOT NULL
        CHECK (
            char_length(member_name) BETWEEN 1 AND 512
            AND member_name = btrim(member_name)
            AND position(chr(92) IN member_name) = 0
            AND position('://' IN member_name) = 0
            AND position('//' IN member_name) = 0
            AND member_name !~ '(^|/)[.]{1,2}(/|$)'
            AND member_name !~ '[[:cntrl:]]'
        ),
    asset_id TEXT NOT NULL,
    object_ref TEXT NOT NULL,
    object_sha256 TEXT NOT NULL
        CHECK (object_sha256 ~ '^[0-9a-f]{64}$'),
    object_size_bytes BIGINT NOT NULL
        CHECK (object_size_bytes > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (batch_id, item_index),
    CONSTRAINT uq_product_library_import_member
        UNIQUE (batch_id, member_name),
    CONSTRAINT uq_product_library_import_asset
        UNIQUE (batch_id, asset_id),
    CONSTRAINT fk_product_library_import_batch
        FOREIGN KEY (batch_id)
        REFERENCES product_library_import_batches (id),
    CONSTRAINT fk_product_library_import_asset
        FOREIGN KEY (asset_id)
        REFERENCES product_asset_library_entries (id),
    CONSTRAINT fk_product_library_import_object_ref
        FOREIGN KEY (object_ref)
        REFERENCES product_asset_library_object_refs (object_ref)
);

CREATE INDEX IF NOT EXISTS idx_product_library_import_asset
    ON product_library_import_items (asset_id, batch_id);

DROP TRIGGER IF EXISTS trg_product_library_import_batch_immutable
    ON product_library_import_batches;
CREATE TRIGGER trg_product_library_import_batch_immutable
    BEFORE UPDATE OR DELETE ON product_library_import_batches
    FOR EACH ROW
    EXECUTE FUNCTION product_admin_security_reject_mutation();

DROP TRIGGER IF EXISTS trg_product_library_import_item_immutable
    ON product_library_import_items;
CREATE TRIGGER trg_product_library_import_item_immutable
    BEFORE UPDATE OR DELETE ON product_library_import_items
    FOR EACH ROW
    EXECUTE FUNCTION product_admin_security_reject_mutation();

COMMIT;
