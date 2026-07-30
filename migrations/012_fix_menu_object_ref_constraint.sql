BEGIN;

ALTER TABLE product_menu_uploads
    DROP CONSTRAINT IF EXISTS ck_product_menu_uploads_object_ref_relative;

ALTER TABLE product_menu_uploads
    ADD CONSTRAINT ck_product_menu_uploads_object_ref_relative
    CHECK (
        char_length(object_ref) BETWEEN 1 AND 1024
        AND object_ref ~ '^[A-Za-z0-9][A-Za-z0-9._/-]*$'
        AND left(object_ref, 1) <> '/'
        AND position(chr(92) IN object_ref) = 0
        AND position('://' IN object_ref) = 0
        AND position('//' IN object_ref) = 0
        AND object_ref !~ '(^|/)[.]{1,2}(/|$)'
        AND object_ref !~ '[[:cntrl:]]'
    );

COMMIT;
