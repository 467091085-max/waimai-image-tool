from __future__ import annotations

import importlib
import hashlib
import os
import threading
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

import object_storage_service
from shared.product_auth_store import ProductAuthStore


TEST_POSTGRES_DSN = str(os.environ.get("TEST_POSTGRES_DSN") or "").strip()
ROOT = Path(__file__).resolve().parents[1]
AUTH_MIGRATION = ROOT / "migrations" / "005_product_auth_postgres.sql"
EXPORT_MIGRATION = ROOT / "migrations" / "010_product_exports_postgres.sql"
EXPORT_RESERVATION_MIGRATION = (
    ROOT
    / "migrations"
    / "014_product_export_nonce_reservations.sql"
)


def _schema_dsn(dsn: str, schema: str) -> str:
    parsed = urlsplit(dsn)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("options", f"-csearch_path={schema}"))
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            parsed.fragment,
        )
    )


@pytest.mark.skipif(
    not TEST_POSTGRES_DSN,
    reason="TEST_POSTGRES_DSN is required for the real export protocol test",
)
def test_live_export_package_and_nonce_flow_stay_in_postgres(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    sql_module = pytest.importorskip("psycopg.sql")
    schema = f"export_api_test_{uuid4().hex}"
    scoped_dsn = _schema_dsn(TEST_POSTGRES_DSN, schema)
    session_secret = "export-auth-session-" + ("s" * 32)
    signing_secret = "export-object-signing-" + ("k" * 32)
    storage_path = tmp_path / "must-not-exist.sqlite3"
    object_store = object_storage_service.ObjectStorageService(
        tmp_path / "objects"
    )
    export_dir = tmp_path / "exports"
    zip_path = export_dir / "run" / "result.zip"
    zip_path.parent.mkdir(parents=True)
    zip_path.write_bytes(b"real-postgres-export-protocol")
    manifest_key = "generated/manifests/job-export/a" + ("a" * 63) + ".json"
    manifest_bytes = b'{"generationBatch":{"requestSha256":"' + (
        b"a" * 64
    ) + b'"}}'
    object_store.put_bytes(manifest_bytes, object_key=manifest_key)

    admin = psycopg.connect(TEST_POSTGRES_DSN, autocommit=True)
    user_id = ""
    try:
        admin.execute(
            sql_module.SQL("CREATE SCHEMA {}").format(
                sql_module.Identifier(schema)
            )
        )
        with psycopg.connect(scoped_dsn, autocommit=True) as connection:
            connection.execute(AUTH_MIGRATION.read_text(encoding="utf-8"))
            connection.execute(
                EXPORT_MIGRATION.read_text(encoding="utf-8")
            )
            connection.execute(
                EXPORT_RESERVATION_MIGRATION.read_text(
                    encoding="utf-8"
                )
            )

        env = {
            "APP_ENV": "staging",
            "PRODUCT_POSTGRES_ENABLED": "true",
            "DATABASE_URL": scoped_dsn,
            "AUTH_SESSION_HASH_SECRET": session_secret,
            "OBJECT_SIGNING_SECRET": signing_secret,
            "STORAGE_DB_PATH": str(storage_path),
        }
        for name, value in env.items():
            monkeypatch.setenv(name, value)

        with psycopg.connect(scoped_dsn, autocommit=False) as connection:
            auth_store = ProductAuthStore(
                connection,
                token_hash_secret=session_secret,
            )
            created = auth_store.get_or_create_user(
                phone="13800138000"
            )
            user_id = str(created.user["id"])
            issued = auth_store.issue_session(user_id=user_id)

        app_module = importlib.import_module("app")
        app_module = importlib.reload(app_module)
        app_module.app.config.update(TESTING=True)
        monkeypatch.setattr(
            app_module.object_storage_service,
            "get_object_storage_service",
            lambda: object_store,
        )
        monkeypatch.setattr(app_module, "EXPORT_DIR", export_dir)

        generation_context = {
            "exportId": "export_protocol_1",
            "ownerUserId": user_id,
            "idempotencyKey": "export_protocol_idem_1",
            "generationJobId": "job_export_1",
            "generationRequestSha256": "a" * 64,
            "exportRequestSha256": "c" * 64,
            "manifestObjectRef": manifest_key,
            "manifestSha256": hashlib.sha256(
                manifest_bytes
            ).hexdigest(),
            "watermark": {"enabled": False},
        }
        with app_module.app.test_request_context(
            "/api/export",
            method="POST",
            headers={"Idempotency-Key": "export-protocol-1"},
        ):
            exported = app_module.object_storage_export_payload(
                {
                    "download": "/download/run/result.zip",
                    "images": 1,
                    "rows": 1,
                    "platforms": ["meituan"],
                    "watermark": False,
                },
                scope="all",
                platforms=["meituan"],
                generation_context=generation_context,
            )

        client = app_module.app.test_client()
        headers = {"Authorization": f"Bearer {issued.token}"}
        exported_object_key = unquote(
            urlsplit(exported["download"]).path.removeprefix("/objects/")
        )
        retry_zip_path = export_dir / "retry" / "result.zip"
        retry_zip_path.parent.mkdir(parents=True)
        retry_zip_path.write_bytes(b"nondeterministic-retry-bytes")
        with app_module.app.test_request_context(
            "/api/export",
            method="POST",
            headers={"Idempotency-Key": "export-protocol-1"},
        ):
            replayed = app_module.object_storage_export_payload(
                {
                    "download": "/download/retry/result.zip",
                    "images": 1,
                    "rows": 1,
                    "platforms": ["meituan"],
                    "watermark": False,
                },
                scope="all",
                platforms=["meituan"],
                generation_context=generation_context,
            )
        replayed_object_key = unquote(
            urlsplit(replayed["download"]).path.removeprefix("/objects/")
        )
        assert replayed["exportPackageId"] == exported["exportPackageId"]
        assert replayed_object_key == exported_object_key
        assert object_store.list_prefix("exports/") == [
            exported_object_key
        ]
        object_store.put_bytes(
            b"tampered-export",
            object_key=exported_object_key,
        )
        integrity_failure = client.get(
            exported["download"],
            headers={
                **headers,
                "X-Request-Id": "download-integrity-failure",
            },
        )
        assert integrity_failure.status_code == 409
        assert (
            integrity_failure.get_json()["code"]
            == "export_object_integrity_mismatch"
        )
        with psycopg.connect(scoped_dsn) as connection:
            before_retry = connection.execute(
                """
                SELECT download_count
                FROM product_export_packages
                WHERE id = 'export_protocol_1'
                """
            ).fetchone()
        assert before_retry == (0,)
        object_store.put_file(
            zip_path,
            object_key=exported_object_key,
        )
        real_download_file_limited = (
            object_store.download_file_limited
        )

        def fail_download(*_args: object, **_kwargs: object) -> Path:
            raise RuntimeError("temporary object-store outage")

        monkeypatch.setattr(
            object_store,
            "download_file_limited",
            fail_download,
        )
        storage_failure = client.get(
            exported["download"],
            headers={
                **headers,
                "X-Request-Id": "download-storage-failure",
            },
        )
        assert storage_failure.status_code == 503
        assert storage_failure.get_json()["code"] == (
            "object_storage_unavailable"
        )
        monkeypatch.setattr(
            object_store,
            "download_file_limited",
            real_download_file_limited,
        )
        with psycopg.connect(scoped_dsn) as connection:
            connection.execute(
                """
                UPDATE product_export_token_nonces
                SET reservation_id = 'crashed-request-reservation',
                    reserved_at = CURRENT_TIMESTAMP,
                    reservation_expires_at = expires_at
                WHERE consumed_at IS NULL
                """
            )
            connection.commit()
        first_download_entered = threading.Event()
        release_first_download = threading.Event()
        download_calls = 0
        download_calls_lock = threading.Lock()

        def blocking_download(
            *args: object,
            **kwargs: object,
        ) -> Path:
            nonlocal download_calls
            with download_calls_lock:
                download_calls += 1
                call_number = download_calls
            if call_number == 1:
                first_download_entered.set()
                assert release_first_download.wait(timeout=10)
            return real_download_file_limited(*args, **kwargs)

        monkeypatch.setattr(
            object_store,
            "download_file_limited",
            blocking_download,
        )
        first_result: dict[str, object] = {}

        def first_download() -> None:
            thread_client = app_module.app.test_client()
            first_result["response"] = thread_client.get(
                exported["download"],
                headers={
                    **headers,
                    "X-Request-Id": "download-one",
                },
            )

        first_thread = threading.Thread(target=first_download)
        first_thread.start()
        assert first_download_entered.wait(timeout=10)
        concurrent = app_module.app.test_client().get(
            exported["download"],
            headers={
                **headers,
                "X-Request-Id": "download-one",
            },
        )
        assert concurrent.status_code == 409
        assert concurrent.get_json()["code"] == "export_token_in_use"
        assert concurrent.get_json()["reason"] == "token_in_use"
        assert download_calls == 1
        release_first_download.set()
        first_thread.join(timeout=10)
        assert not first_thread.is_alive()
        first = first_result["response"]
        assert hasattr(first, "status_code")
        monkeypatch.setattr(
            object_store,
            "download_file_limited",
            real_download_file_limited,
        )
        replay_download = mock.Mock(
            side_effect=AssertionError(
                "replayed token must be rejected before object download"
            )
        )
        monkeypatch.setattr(
            object_store,
            "download_file_limited",
            replay_download,
        )
        second = client.get(
            exported["download"],
            headers={**headers, "X-Request-Id": "download-two"},
        )

        assert first.status_code == 200
        assert first.data == zip_path.read_bytes()
        assert second.status_code == 403
        assert second.get_json()["reason"] == "token_replayed"
        replay_download.assert_not_called()
        monkeypatch.setattr(
            object_store,
            "download_file_limited",
            real_download_file_limited,
        )

        real_consume_once = (
            app_module.PostgresExportNonceConsumer.consume_once
        )

        def fail_nonce_consume(
            _consumer: object,
            *_args: object,
            **_kwargs: object,
        ) -> object:
            raise RuntimeError("temporary postgres consume failure")

        monkeypatch.setattr(
            app_module.PostgresExportNonceConsumer,
            "consume_once",
            fail_nonce_consume,
        )
        real_finalize_denial = (
            app_module.PostgresExportNonceConsumer.finalize_denial
        )
        denial_finalize_entered = threading.Event()
        allow_denial_finalize = threading.Event()

        def blocking_finalize_denial(
            consumer: object,
            reason: str,
        ) -> None:
            denial_finalize_entered.set()
            assert allow_denial_finalize.wait(timeout=10)
            real_finalize_denial(consumer, reason)

        monkeypatch.setattr(
            app_module.PostgresExportNonceConsumer,
            "finalize_denial",
            blocking_finalize_denial,
        )
        failure_window_download = mock.Mock(
            wraps=real_download_file_limited
        )
        monkeypatch.setattr(
            object_store,
            "download_file_limited",
            failure_window_download,
        )
        failed_request_id = "download-consume-failure"
        consume_failure_result: dict[str, object] = {}

        def failed_download() -> None:
            failure_client = app_module.app.test_client()
            consume_failure_result["response"] = failure_client.get(
                replayed["download"],
                headers={
                    **headers,
                    "X-Request-Id": failed_request_id,
                },
            )

        failure_thread = threading.Thread(target=failed_download)
        failure_thread.start()
        assert denial_finalize_entered.wait(timeout=10)
        concurrent_failure_retry = app_module.app.test_client().get(
            replayed["download"],
            headers={
                **headers,
                "X-Request-Id": failed_request_id,
            },
        )
        assert concurrent_failure_retry.status_code == 409
        assert concurrent_failure_retry.get_json()["reason"] == (
            "token_in_use"
        )
        assert failure_window_download.call_count == 1
        allow_denial_finalize.set()
        failure_thread.join(timeout=10)
        assert not failure_thread.is_alive()
        consume_failure = consume_failure_result["response"]
        assert hasattr(consume_failure, "status_code")
        assert consume_failure.status_code == 503
        assert consume_failure.get_json()["reason"] == (
            "nonce_consumer_error"
        )
        monkeypatch.setattr(
            app_module.PostgresExportNonceConsumer,
            "consume_once",
            real_consume_once,
        )
        monkeypatch.setattr(
            app_module.PostgresExportNonceConsumer,
            "finalize_denial",
            real_finalize_denial,
        )
        repeated_download = mock.Mock(
            side_effect=AssertionError(
                "a repeated denied request must not redownload the object"
            )
        )
        monkeypatch.setattr(
            object_store,
            "download_file_limited",
            repeated_download,
        )
        consume_failure_retry = app_module.app.test_client().get(
            replayed["download"],
            headers={
                **headers,
                "X-Request-Id": failed_request_id,
            },
        )
        assert consume_failure_retry.status_code == 503
        assert consume_failure_retry.get_json()["reason"] == (
            "nonce_consumer_error"
        )
        repeated_download.assert_not_called()
        assert storage_path.exists() is False

        with psycopg.connect(scoped_dsn) as connection:
            package = connection.execute(
                """
                SELECT owner_user_id, zip_object_ref, zip_sha256,
                       download_count
                FROM product_export_packages
                WHERE id = 'export_protocol_1'
                """
            ).fetchone()
            nonce = connection.execute(
                """
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE consumed_at IS NOT NULL)
                FROM product_export_token_nonces
                """
            ).fetchone()
            audits = connection.execute(
                """
                SELECT allowed, deny_reason
                FROM product_export_access_audits
                ORDER BY created_at, action_id
                """
            ).fetchall()
        assert package is not None
        assert package[0] == user_id
        assert package[1].startswith("exports/v2/")
        assert package[1].endswith(".zip")
        assert len(package[2]) == 64
        assert package[3] == 1
        assert nonce == (2, 1)
        assert sorted(audits) == [
            (False, "nonce_consumer_error"),
            (False, "object_integrity_mismatch"),
            (False, "object_storage_unavailable"),
            (False, "token_in_use"),
            (False, "token_in_use"),
            (False, "token_replayed"),
            (True, ""),
        ]
    finally:
        try:
            admin.execute(
                sql_module.SQL(
                    "DROP SCHEMA IF EXISTS {} CASCADE"
                ).format(sql_module.Identifier(schema))
            )
        finally:
            admin.close()
