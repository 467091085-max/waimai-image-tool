BEGIN;

CREATE TABLE IF NOT EXISTS product_asset_library_entries (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    asset_kind TEXT NOT NULL
        CHECK (asset_kind IN ('product', 'background')),
    taxonomy_version TEXT NOT NULL,
    category_id TEXT NOT NULL,
    category_name TEXT NOT NULL,
    style_id TEXT NOT NULL,
    background_asset_id TEXT NOT NULL DEFAULT '',
    background_sha256 TEXT NOT NULL DEFAULT ''
        CHECK (
            background_sha256 = ''
            OR background_sha256 ~ '^[0-9a-f]{64}$'
        ),
    standard_name TEXT NOT NULL,
    normalized_standard_name TEXT NOT NULL,
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (
            jsonb_typeof(aliases) = 'array'
            AND jsonb_array_length(aliases) <= 64
        ),
    aliases_normalized JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (
            jsonb_typeof(aliases_normalized) = 'array'
            AND jsonb_array_length(aliases_normalized) <= 64
            AND jsonb_array_length(aliases_normalized)
                = jsonb_array_length(aliases)
        ),
    match_keywords JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (
            jsonb_typeof(match_keywords) = 'array'
            AND jsonb_array_length(match_keywords) <= 64
        ),
    match_keywords_normalized JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (
            jsonb_typeof(match_keywords_normalized) = 'array'
            AND jsonb_array_length(match_keywords_normalized) <= 64
            AND jsonb_array_length(match_keywords_normalized)
                = jsonb_array_length(match_keywords)
        ),
    combo_fingerprint_version TEXT NOT NULL DEFAULT '',
    combo_components JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (
            jsonb_typeof(combo_components) = 'array'
            AND jsonb_array_length(combo_components) <= 32
        ),
    combo_fingerprint_sha256 TEXT NOT NULL DEFAULT ''
        CHECK (
            combo_fingerprint_sha256 = ''
            OR combo_fingerprint_sha256 ~ '^[0-9a-f]{64}$'
        ),
    reuse_scope TEXT NOT NULL
        CHECK (reuse_scope IN ('owner', 'tenant')),
    source_kind TEXT NOT NULL
        CHECK (source_kind IN ('generated', 'uploaded', 'imported')),
    source_provider TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    original_object_ref TEXT NOT NULL,
    original_sha256 TEXT NOT NULL
        CHECK (original_sha256 ~ '^[0-9a-f]{64}$'),
    original_size_bytes BIGINT NOT NULL
        CHECK (original_size_bytes > 0),
    derivative_object_ref TEXT,
    derivative_sha256 TEXT
        CHECK (
            derivative_sha256 IS NULL
            OR derivative_sha256 ~ '^[0-9a-f]{64}$'
        ),
    derivative_size_bytes BIGINT
        CHECK (
            derivative_size_bytes IS NULL
            OR derivative_size_bytes > 0
        ),
    status TEXT NOT NULL DEFAULT 'pending_review'
        CHECK (
            status IN (
                'pending_review',
                'approved',
                'rejected',
                'disabled'
            )
        ),
    review_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (review_status IN ('pending', 'approved', 'rejected')),
    reviewer_user_id TEXT,
    review_note TEXT NOT NULL DEFAULT '',
    reviewed_at TIMESTAMPTZ,
    disabled_by_user_id TEXT,
    disable_note TEXT NOT NULL DEFAULT '',
    disabled_at TIMESTAMPTZ,
    content_sha256 TEXT NOT NULL
        CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_asset_library_idempotency
        UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT ck_product_asset_library_identifiers
        CHECK (
            id ~ '^asset-[0-9a-f]{40}$'
            AND tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
            AND owner_user_id ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND idempotency_key
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND taxonomy_version
                ~ '^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$'
            AND category_id ~ '^[a-z0-9][a-z0-9_-]{0,63}$'
            AND style_id ~ '^[a-z0-9][a-z0-9_-]{0,63}$'
            AND (
                background_asset_id = ''
                OR background_asset_id
                    ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            )
            AND (
                reviewer_user_id IS NULL
                OR reviewer_user_id
                    ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            )
            AND (
                disabled_by_user_id IS NULL
                OR disabled_by_user_id
                    ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            )
        ),
    CONSTRAINT ck_product_asset_library_names
        CHECK (
            char_length(category_name) BETWEEN 1 AND 255
            AND category_name = btrim(category_name)
            AND category_name !~ '[[:cntrl:]]'
            AND char_length(standard_name) BETWEEN 1 AND 255
            AND standard_name = btrim(standard_name)
            AND standard_name !~ '[[:cntrl:]]'
            AND char_length(normalized_standard_name) BETWEEN 1 AND 255
            AND normalized_standard_name = lower(normalized_standard_name)
            AND normalized_standard_name
                !~ '[[:space:][:punct:][:cntrl:]]'
        ),
    CONSTRAINT ck_product_asset_library_versions
        CHECK (
            char_length(source_provider) BETWEEN 1 AND 128
            AND source_provider = btrim(source_provider)
            AND source_provider !~ '[[:cntrl:]]'
            AND prompt_version
                ~ '^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$'
            AND char_length(model_name) BETWEEN 1 AND 128
            AND model_name = btrim(model_name)
            AND model_name !~ '[[:cntrl:]]'
            AND model_version
                ~ '^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$'
            AND pipeline_version
                ~ '^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$'
        ),
    CONSTRAINT ck_product_asset_library_original_ref
        CHECK (
            char_length(original_object_ref) BETWEEN 11 AND 1024
            AND original_object_ref
                LIKE ('ai-assets/' || tenant_id || '/%')
            AND original_object_ref
                ~ '^[A-Za-z0-9][A-Za-z0-9._/-]*$'
            AND left(original_object_ref, 1) <> '/'
            AND position(chr(92) IN original_object_ref) = 0
            AND position('://' IN original_object_ref) = 0
            AND position('//' IN original_object_ref) = 0
            AND original_object_ref !~ '(^|/)[.]{1,2}(/|$)'
            AND original_object_ref !~ '[[:cntrl:]]'
        ),
    CONSTRAINT ck_product_asset_library_derivative_ref
        CHECK (
            (
                derivative_object_ref IS NULL
                AND derivative_sha256 IS NULL
                AND derivative_size_bytes IS NULL
            )
            OR (
                derivative_object_ref IS NOT NULL
                AND derivative_sha256 IS NOT NULL
                AND derivative_size_bytes IS NOT NULL
                AND char_length(derivative_object_ref) BETWEEN 11 AND 1024
                AND derivative_object_ref
                    LIKE ('ai-assets/' || tenant_id || '/%')
                AND derivative_object_ref
                    ~ '^[A-Za-z0-9][A-Za-z0-9._/-]*$'
                AND left(derivative_object_ref, 1) <> '/'
                AND position(chr(92) IN derivative_object_ref) = 0
                AND position('://' IN derivative_object_ref) = 0
                AND position('//' IN derivative_object_ref) = 0
                AND derivative_object_ref !~ '(^|/)[.]{1,2}(/|$)'
                AND derivative_object_ref !~ '[[:cntrl:]]'
                AND derivative_object_ref <> original_object_ref
            )
        ),
    CONSTRAINT ck_product_asset_library_combo_fingerprint
        CHECK (
            (
                asset_kind = 'product'
                AND category_id = 'combo'
                AND combo_fingerprint_version = 'combo-components.v1'
                AND combo_fingerprint_sha256 <> ''
                AND jsonb_array_length(combo_components) >= 2
            )
            OR (
                NOT (asset_kind = 'product' AND category_id = 'combo')
                AND combo_fingerprint_version = ''
                AND combo_fingerprint_sha256 = ''
                AND combo_components = '[]'::jsonb
            )
        ),
    CONSTRAINT ck_product_asset_library_background_binding
        CHECK (
            (
                asset_kind = 'background'
                AND background_asset_id = ''
                AND background_sha256 = ''
            )
            OR (
                asset_kind = 'product'
                AND background_asset_id <> ''
                AND background_sha256 <> ''
            )
        ),
    CONSTRAINT ck_product_asset_library_review_state
        CHECK (
            (
                status = 'pending_review'
                AND review_status = 'pending'
                AND reviewer_user_id IS NULL
                AND review_note = ''
                AND reviewed_at IS NULL
                AND disabled_by_user_id IS NULL
                AND disable_note = ''
                AND disabled_at IS NULL
            )
            OR (
                status = 'approved'
                AND review_status = 'approved'
                AND reviewer_user_id IS NOT NULL
                AND reviewed_at IS NOT NULL
                AND disabled_by_user_id IS NULL
                AND disable_note = ''
                AND disabled_at IS NULL
            )
            OR (
                status = 'rejected'
                AND review_status = 'rejected'
                AND reviewer_user_id IS NOT NULL
                AND reviewed_at IS NOT NULL
                AND disabled_by_user_id IS NULL
                AND disable_note = ''
                AND disabled_at IS NULL
            )
            OR (
                status = 'disabled'
                AND review_status = 'approved'
                AND reviewer_user_id IS NOT NULL
                AND reviewed_at IS NOT NULL
                AND disabled_by_user_id IS NOT NULL
                AND disable_note <> ''
                AND disabled_at IS NOT NULL
            )
        ),
    CONSTRAINT ck_product_asset_library_timestamps
        CHECK (
            updated_at >= created_at
            AND (reviewed_at IS NULL OR reviewed_at >= created_at)
            AND (disabled_at IS NULL OR disabled_at >= reviewed_at)
        )
);

CREATE TABLE IF NOT EXISTS product_asset_library_object_refs (
    object_ref TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    object_role TEXT NOT NULL
        CHECK (object_role IN ('original', 'derivative')),
    object_sha256 TEXT NOT NULL
        CHECK (object_sha256 ~ '^[0-9a-f]{64}$'),
    object_size_bytes BIGINT NOT NULL
        CHECK (object_size_bytes > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_asset_library_object_asset_role
        UNIQUE (asset_id, object_role),
    CONSTRAINT fk_product_asset_library_object_asset
        FOREIGN KEY (asset_id)
        REFERENCES product_asset_library_entries (id)
        ON DELETE CASCADE
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT ck_product_asset_library_object_tenant
        CHECK (
            tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
            AND object_ref LIKE ('ai-assets/' || tenant_id || '/%')
        )
);

CREATE TABLE IF NOT EXISTS product_asset_library_status_events (
    event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    from_status TEXT NOT NULL
        CHECK (from_status IN ('pending_review', 'approved')),
    to_status TEXT NOT NULL
        CHECK (to_status IN ('approved', 'rejected', 'disabled')),
    actor_user_id TEXT NOT NULL,
    event_note TEXT NOT NULL DEFAULT '',
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_product_asset_library_event_asset
        FOREIGN KEY (asset_id)
        REFERENCES product_asset_library_entries (id)
        ON DELETE CASCADE,
    CONSTRAINT ck_product_asset_library_event_identifiers
        CHECK (
            tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
            AND actor_user_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
        ),
    CONSTRAINT ck_product_asset_library_event_transition
        CHECK (
            (
                from_status = 'pending_review'
                AND to_status IN ('approved', 'rejected')
            )
            OR (
                from_status = 'approved'
                AND to_status = 'disabled'
            )
        )
);

CREATE OR REPLACE FUNCTION product_asset_library_claim_object_ref(
    claimed_ref TEXT,
    claimed_tenant TEXT,
    claimed_asset TEXT,
    claimed_role TEXT,
    claimed_sha256 TEXT,
    claimed_size BIGINT
) RETURNS VOID AS $$
BEGIN
    INSERT INTO product_asset_library_object_refs (
        object_ref,
        tenant_id,
        asset_id,
        object_role,
        object_sha256,
        object_size_bytes
    ) VALUES (
        claimed_ref,
        claimed_tenant,
        claimed_asset,
        claimed_role,
        claimed_sha256,
        claimed_size
    )
    ON CONFLICT DO NOTHING;

    IF FOUND THEN
        RETURN;
    END IF;

    PERFORM 1
    FROM product_asset_library_object_refs
    WHERE object_ref = claimed_ref
      AND tenant_id = claimed_tenant
      AND asset_id = claimed_asset
      AND object_role = claimed_role
      AND object_sha256 = claimed_sha256
      AND object_size_bytes = claimed_size;

    IF FOUND THEN
        RETURN;
    END IF;

    RAISE EXCEPTION USING
        ERRCODE = '23505',
        MESSAGE = 'asset object_ref is already registered';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION product_asset_library_claim_object_refs()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM product_asset_library_claim_object_ref(
        NEW.original_object_ref,
        NEW.tenant_id,
        NEW.id,
        'original',
        NEW.original_sha256,
        NEW.original_size_bytes
    );

    IF NEW.derivative_object_ref IS NOT NULL THEN
        PERFORM product_asset_library_claim_object_ref(
            NEW.derivative_object_ref,
            NEW.tenant_id,
            NEW.id,
            'derivative',
            NEW.derivative_sha256,
            NEW.derivative_size_bytes
        );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION product_asset_library_enforce_status_transition()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status = NEW.status THEN
        RETURN NEW;
    END IF;

    IF NOT (
        (
            OLD.status = 'pending_review'
            AND NEW.status IN ('approved', 'rejected')
        )
        OR (
            OLD.status = 'approved'
            AND NEW.status = 'disabled'
        )
    ) THEN
        RAISE EXCEPTION 'invalid asset status transition: % -> %',
            OLD.status,
            NEW.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION product_asset_library_audit_status_transition()
RETURNS TRIGGER AS $$
DECLARE
    transition_actor TEXT;
    transition_note TEXT;
BEGIN
    IF OLD.status = NEW.status THEN
        RETURN NEW;
    END IF;

    IF OLD.status = 'approved' AND NEW.status = 'disabled' THEN
        transition_actor := NEW.disabled_by_user_id;
        transition_note := NEW.disable_note;
    ELSE
        transition_actor := NEW.reviewer_user_id;
        transition_note := NEW.review_note;
    END IF;

    INSERT INTO product_asset_library_status_events (
        asset_id,
        tenant_id,
        from_status,
        to_status,
        actor_user_id,
        event_note
    ) VALUES (
        NEW.id,
        NEW.tenant_id,
        OLD.status,
        NEW.status,
        transition_actor,
        transition_note
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_product_asset_library_claim_object_refs
    ON product_asset_library_entries;
CREATE TRIGGER trg_product_asset_library_claim_object_refs
BEFORE INSERT ON product_asset_library_entries
FOR EACH ROW
EXECUTE FUNCTION product_asset_library_claim_object_refs();

DROP TRIGGER IF EXISTS trg_product_asset_library_enforce_status_transition
    ON product_asset_library_entries;
CREATE TRIGGER trg_product_asset_library_enforce_status_transition
BEFORE UPDATE OF status ON product_asset_library_entries
FOR EACH ROW
EXECUTE FUNCTION product_asset_library_enforce_status_transition();

DROP TRIGGER IF EXISTS trg_product_asset_library_audit_status_transition
    ON product_asset_library_entries;
CREATE TRIGGER trg_product_asset_library_audit_status_transition
AFTER UPDATE OF status ON product_asset_library_entries
FOR EACH ROW
EXECUTE FUNCTION product_asset_library_audit_status_transition();

DROP INDEX IF EXISTS uq_product_asset_library_original_ref;
CREATE UNIQUE INDEX uq_product_asset_library_original_ref
    ON product_asset_library_entries (original_object_ref);

DROP INDEX IF EXISTS uq_product_asset_library_derivative_ref;
CREATE UNIQUE INDEX uq_product_asset_library_derivative_ref
    ON product_asset_library_entries (derivative_object_ref)
    WHERE derivative_object_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_product_asset_library_owner_created
    ON product_asset_library_entries (
        tenant_id,
        owner_user_id,
        created_at DESC,
        id DESC
    );

DROP INDEX IF EXISTS idx_product_asset_library_exact_reuse;
CREATE INDEX idx_product_asset_library_exact_reuse
    ON product_asset_library_entries (
        tenant_id,
        taxonomy_version,
        category_id,
        style_id,
        background_asset_id,
        background_sha256,
        pipeline_version,
        asset_kind,
        normalized_standard_name,
        status,
        reuse_scope,
        owner_user_id
    );

CREATE INDEX IF NOT EXISTS idx_product_asset_library_aliases
    ON product_asset_library_entries
    USING GIN (aliases_normalized);

DROP INDEX IF EXISTS idx_product_asset_library_keywords;
COMMENT ON COLUMN product_asset_library_entries.match_keywords IS
    'Review/search metadata only; never used for direct asset reuse.';
COMMENT ON COLUMN product_asset_library_entries.match_keywords_normalized IS
    'Review/search metadata only; never used for direct asset reuse.';

CREATE INDEX IF NOT EXISTS idx_product_asset_library_status_events
    ON product_asset_library_status_events (
        tenant_id,
        asset_id,
        occurred_at DESC,
        event_id DESC
    );

COMMIT;
