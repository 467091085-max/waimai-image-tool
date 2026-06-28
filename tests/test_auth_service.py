from __future__ import annotations

import json
import re
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

import auth_service as auth


class AuthServiceSchemaTests(unittest.TestCase):
    def test_init_auth_schema_creates_required_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            auth.init_auth_schema(conn)
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            table_names = {row["name"] for row in rows}
            self.assertGreaterEqual(
                table_names,
                {"users", "stores", "user_stores", "otp_challenges", "sessions"},
            )
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        finally:
            conn.close()


class AuthServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        auth.init_auth_schema(self.conn)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def tearDown(self) -> None:
        self.conn.close()

    def test_request_otp_normalizes_phone_and_records_mock_code_metadata(self) -> None:
        challenge = auth.request_otp(
            self.conn,
            " +86 138-0013-8000 ",
            ip="127.0.0.1",
            user_agent="unit-test",
            now=self.now,
        )

        self.assertEqual(challenge["phone"], "+8613800138000")
        self.assertRegex(challenge["code"], r"^\d{6}$")
        self.assertEqual(
            challenge["expires_at"],
            (self.now + timedelta(seconds=auth.OTP_TTL_SECONDS)).isoformat(),
        )

        row = self.conn.execute(
            "SELECT * FROM otp_challenges WHERE id = ?",
            (challenge["challenge_id"],),
        ).fetchone()
        self.assertEqual(row["phone"], "+8613800138000")
        self.assertEqual(row["ip"], "127.0.0.1")
        self.assertEqual(row["user_agent"], "unit-test")
        self.assertEqual(row["phone_requests_1h"], 1)
        self.assertEqual(row["ip_requests_1h"], 1)
        self.assertNotEqual(row["code_hash"], challenge["code"])
        metadata = json.loads(row["metadata_json"])
        self.assertEqual(metadata["request_counts"], {"phone_1h": 1, "ip_1h": 1})

    def test_request_and_verify_errors_have_stable_codes(self) -> None:
        with self.assertRaises(auth.AuthError) as invalid_phone:
            auth.request_otp(self.conn, "8613800138000", now=self.now)
        self.assertEqual(invalid_phone.exception.code, auth.ERR_INVALID_PHONE)

        with self.assertRaises(auth.AuthError) as missing_challenge:
            auth.verify_otp(self.conn, "otp_missing", "123456", now=self.now)
        self.assertEqual(missing_challenge.exception.code, auth.ERR_CHALLENGE_NOT_FOUND)

        challenge = auth.request_otp(self.conn, "13800138000", now=self.now)
        with self.assertRaises(auth.AuthError) as mismatch:
            auth.verify_otp(self.conn, challenge["challenge_id"], "000000", now=self.now)
        self.assertEqual(mismatch.exception.code, auth.ERR_OTP_MISMATCH)

        row = self.conn.execute(
            "SELECT attempts, used_at FROM otp_challenges WHERE id = ?",
            (challenge["challenge_id"],),
        ).fetchone()
        self.assertEqual(row["attempts"], 1)
        self.assertIsNone(row["used_at"])

    def test_request_otp_enforces_send_cooldown(self) -> None:
        auth.request_otp(self.conn, "13800138000", now=self.now)

        with self.assertRaises(auth.AuthError) as limited:
            auth.request_otp(self.conn, "13800138000", now=self.now + timedelta(seconds=30))

        self.assertEqual(limited.exception.code, auth.ERR_OTP_RATE_LIMITED)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM otp_challenges").fetchone()[0],
            1,
        )

    def test_verify_otp_locks_after_attempt_limit(self) -> None:
        challenge = auth.request_otp(self.conn, "13800138000", now=self.now)
        for index in range(5):
            with self.assertRaises(auth.AuthError) as mismatch:
                auth.verify_otp(
                    self.conn,
                    challenge["challenge_id"],
                    f"{index:06d}",
                    now=self.now + timedelta(seconds=index + 1),
                )
            self.assertEqual(mismatch.exception.code, auth.ERR_OTP_MISMATCH)

        with self.assertRaises(auth.AuthError) as locked:
            auth.verify_otp(
                self.conn,
                challenge["challenge_id"],
                challenge["code"],
                now=self.now + timedelta(seconds=10),
            )

        self.assertEqual(locked.exception.code, auth.ERR_OTP_ATTEMPT_LIMITED)
        row = self.conn.execute(
            "SELECT attempts, used_at FROM otp_challenges WHERE id = ?",
            (challenge["challenge_id"],),
        ).fetchone()
        self.assertEqual(row["attempts"], 5)
        self.assertIsNone(row["used_at"])

    def test_expired_otp_is_rejected(self) -> None:
        challenge = auth.request_otp(self.conn, "13800138000", now=self.now)

        with self.assertRaises(auth.AuthError) as expired:
            auth.verify_otp(
                self.conn,
                challenge["challenge_id"],
                challenge["code"],
                now=self.now + timedelta(seconds=auth.OTP_TTL_SECONDS),
            )

        self.assertEqual(expired.exception.code, auth.ERR_OTP_EXPIRED)
        user_count = self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        self.assertEqual(user_count, 0)

    def test_otp_is_one_time_and_existing_phone_reuses_user(self) -> None:
        challenge = auth.request_otp(self.conn, "13800138000", now=self.now)
        login = auth.verify_otp(
            self.conn,
            challenge["challenge_id"],
            challenge["code"],
            now=self.now + timedelta(seconds=1),
        )
        user_id = login["user"]["id"]

        with self.assertRaises(auth.AuthError) as reused:
            auth.verify_otp(
                self.conn,
                challenge["challenge_id"],
                challenge["code"],
                now=self.now + timedelta(seconds=2),
            )
        self.assertEqual(reused.exception.code, auth.ERR_OTP_USED)

        next_challenge = auth.request_otp(
            self.conn,
            "+8613800138000",
            now=self.now + timedelta(seconds=61),
        )
        next_login = auth.verify_otp(
            self.conn,
            next_challenge["challenge_id"],
            next_challenge["code"],
            now=self.now + timedelta(seconds=62),
        )
        self.assertEqual(next_login["user"]["id"], user_id)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)

    def test_session_lookup_logout_and_token_hash_storage(self) -> None:
        challenge = auth.request_otp(self.conn, "13800138000", now=self.now)
        login = auth.verify_otp(
            self.conn,
            challenge["challenge_id"],
            challenge["code"],
            now=self.now + timedelta(seconds=1),
        )
        token = login["session"]["token"]

        self.assertGreater(len(token), 32)
        session_row = self.conn.execute("SELECT token_hash FROM sessions").fetchone()
        self.assertNotEqual(session_row["token_hash"], token)
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", session_row["token_hash"]))

        session = auth.get_session(self.conn, token)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(session["user"]["phone"], "+8613800138000")
        self.assertNotIn("token", session)

        self.assertTrue(auth.logout(self.conn, token))
        self.assertIsNone(auth.get_session(self.conn, token))
        self.assertFalse(auth.logout(self.conn, token))

    def test_registration_session_context_derives_reward_risk_inputs(self) -> None:
        first_challenge = auth.request_otp(
            self.conn,
            "13800138000",
            ip="203.0.113.10",
            user_agent="same-device",
            now=self.now,
        )
        first_login = auth.verify_otp(
            self.conn,
            first_challenge["challenge_id"],
            first_challenge["code"],
            now=self.now + timedelta(seconds=1),
        )
        first_context = auth.registration_session_context(
            self.conn,
            user_id=first_login["user"]["id"],
            session_created_at=first_login["session"]["created_at"],
            session_id=first_login["session"]["id"],
            ip="203.0.113.10",
            user_agent="same-device",
            now=self.now + timedelta(seconds=2),
        )

        self.assertTrue(first_context["phone_verified"])
        self.assertTrue(first_context["human_verified"])
        self.assertFalse(first_context["same_phone_registered"])
        self.assertEqual(first_context["same_device_recent_registrations"], 0)
        self.assertEqual(first_context["same_ip_recent_registrations"], 0)
        self.assertFalse(first_context["risk_blocked"])

        second_challenge = auth.request_otp(
            self.conn,
            "13900139000",
            ip="203.0.113.10",
            user_agent="same-device",
            now=self.now + timedelta(seconds=3),
        )
        second_login = auth.verify_otp(
            self.conn,
            second_challenge["challenge_id"],
            second_challenge["code"],
            now=self.now + timedelta(seconds=4),
        )
        second_context = auth.registration_session_context(
            self.conn,
            user_id=second_login["user"]["id"],
            session_created_at=second_login["session"]["created_at"],
            session_id=second_login["session"]["id"],
            ip="203.0.113.10",
            user_agent="same-device",
            now=self.now + timedelta(seconds=5),
        )

        self.assertFalse(second_context["same_phone_registered"])
        self.assertEqual(second_context["same_device_recent_registrations"], 1)
        self.assertEqual(second_context["same_ip_recent_registrations"], 1)

        repeat_challenge = auth.request_otp(
            self.conn,
            "13900139000",
            ip="203.0.113.10",
            user_agent="same-device",
            now=self.now + timedelta(seconds=65),
        )
        repeat_login = auth.verify_otp(
            self.conn,
            repeat_challenge["challenge_id"],
            repeat_challenge["code"],
            now=self.now + timedelta(seconds=66),
        )
        repeat_context = auth.registration_session_context(
            self.conn,
            user_id=repeat_login["user"]["id"],
            session_created_at=repeat_login["session"]["created_at"],
            session_id=repeat_login["session"]["id"],
            ip="203.0.113.10",
            user_agent="same-device",
            now=self.now + timedelta(seconds=67),
        )

        self.assertEqual(repeat_login["user"]["id"], second_login["user"]["id"])
        self.assertTrue(repeat_context["same_phone_registered"])

    def test_create_store_and_list_user_stores(self) -> None:
        challenge = auth.request_otp(self.conn, "13800138000", now=self.now)
        login = auth.verify_otp(
            self.conn,
            challenge["challenge_id"],
            challenge["code"],
            now=self.now + timedelta(seconds=1),
        )
        user_id = login["user"]["id"]

        store = auth.create_store(self.conn, user_id, "  测试门店  ")
        self.assertEqual(store["name"], "测试门店")
        self.assertEqual(store["created_by_user_id"], user_id)

        stores = auth.list_user_stores(self.conn, user_id)
        self.assertEqual(len(stores), 1)
        self.assertEqual(stores[0]["id"], store["id"])
        self.assertEqual(stores[0]["role"], "owner")


if __name__ == "__main__":
    unittest.main()
