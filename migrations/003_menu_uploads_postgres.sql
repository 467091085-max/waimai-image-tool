BEGIN;

CREATE TABLE IF NOT EXISTS product_menu_uploads (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    object_ref TEXT NOT NULL,
    object_sha256 TEXT NOT NULL
        CHECK (object_sha256 ~ '^[0-9a-f]{64}$'),
    original_filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    file_size BIGINT NOT NULL CHECK (file_size >= 0),
    parser_version TEXT NOT NULL,
    item_count INTEGER NOT NULL CHECK (item_count >= 0),
    store_name TEXT NOT NULL DEFAULT '',
    parsed_summary JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'parsed'
        CHECK (status IN ('parsed', 'frozen', 'failed')),
    failure_reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    frozen_at TIMESTAMPTZ,
    CONSTRAINT ck_product_menu_uploads_id
        CHECK (
            id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND owner_user_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
        ),
    CONSTRAINT ck_product_menu_uploads_object_ref_relative
        CHECK (
            char_length(object_ref) BETWEEN 1 AND 1024
            AND object_ref ~ '^[A-Za-z0-9][A-Za-z0-9._/-]{0,1023}$'
            AND left(object_ref, 1) <> '/'
            AND position(chr(92) IN object_ref) = 0
            AND position('://' IN object_ref) = 0
            AND position('//' IN object_ref) = 0
            AND object_ref !~ '(^|/)[.]{1,2}(/|$)'
            AND object_ref !~ '[[:cntrl:]]'
        ),
    CONSTRAINT ck_product_menu_uploads_filename
        CHECK (
            char_length(original_filename) BETWEEN 1 AND 255
            AND position('/' IN original_filename) = 0
            AND position(chr(92) IN original_filename) = 0
            AND original_filename !~ '[[:cntrl:]]'
        ),
    CONSTRAINT ck_product_menu_uploads_content_type
        CHECK (
            char_length(content_type) BETWEEN 3 AND 255
            AND position('/' IN content_type) > 1
            AND content_type !~ '[[:space:][:cntrl:]]'
        ),
    CONSTRAINT ck_product_menu_uploads_parser_version
        CHECK (
            parser_version ~ '^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$'
        ),
    CONSTRAINT ck_product_menu_uploads_summary_object
        CHECK (jsonb_typeof(parsed_summary) = 'object'),
    CONSTRAINT ck_product_menu_uploads_status_fields
        CHECK (
            (
                status = 'frozen'
                AND frozen_at IS NOT NULL
                AND failure_reason = ''
            )
            OR (
                status = 'failed'
                AND frozen_at IS NULL
                AND failure_reason <> ''
            )
            OR (
                status = 'parsed'
                AND frozen_at IS NULL
                AND failure_reason = ''
            )
        ),
    CONSTRAINT ck_product_menu_uploads_timestamps
        CHECK (
            updated_at >= created_at
            AND (frozen_at IS NULL OR frozen_at >= created_at)
        )
);

CREATE INDEX IF NOT EXISTS idx_product_menu_uploads_owner_created
    ON product_menu_uploads (owner_user_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_product_menu_uploads_owner_status
    ON product_menu_uploads (owner_user_id, status, updated_at DESC);

COMMIT;
