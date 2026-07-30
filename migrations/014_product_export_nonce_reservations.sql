BEGIN;

ALTER TABLE product_export_token_nonces
    ADD COLUMN IF NOT EXISTS reservation_id TEXT,
    ADD COLUMN IF NOT EXISTS reserved_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reservation_expires_at TIMESTAMPTZ;

ALTER TABLE product_export_token_nonces
    DROP CONSTRAINT IF EXISTS ck_product_export_nonce_reservation;

ALTER TABLE product_export_token_nonces
    ADD CONSTRAINT ck_product_export_nonce_reservation
    CHECK (
        (
            reservation_id IS NULL
            AND reserved_at IS NULL
            AND reservation_expires_at IS NULL
        )
        OR (
            reservation_id IS NOT NULL
            AND reservation_id
                ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$'
            AND reserved_at IS NOT NULL
            AND reservation_expires_at IS NOT NULL
            AND reservation_expires_at > reserved_at
            AND consumed_at IS NULL
        )
    );

CREATE INDEX IF NOT EXISTS idx_product_export_nonces_reservation_expiry
    ON product_export_token_nonces (
        reservation_expires_at,
        token_nonce_digest
    )
    WHERE reservation_id IS NOT NULL
      AND consumed_at IS NULL;

COMMIT;
