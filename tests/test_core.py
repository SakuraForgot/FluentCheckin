import tempfile
import unittest
from datetime import datetime, timezone

from checkin.core import (
    PROVIDERS,
    agent_result,
    agent_receipt_confirmed,
    checkin_status,
    response_problem,
    retry_after_seconds,
    site_cookies,
    user_id,
)
from checkin.vault import Store, Vault, VaultError


def cookie(domain, value="test-only-value"):
    return {
        "name": "session",
        "value": value,
        "domain": domain,
        "path": "/",
        "expires": -1,
        "secure": True,
        "httpOnly": True,
        "sameSite": "Lax",
    }


class CoreTests(unittest.TestCase):
    def test_retry_after_seconds_and_http_date(self):
        now = datetime(2026, 10, 6, tzinfo=timezone.utc)
        self.assertEqual(retry_after_seconds("120", now), 120)
        self.assertEqual(retry_after_seconds("Tue, 06 Oct 2026 00:02:00 GMT", now), 120)
        self.assertEqual(retry_after_seconds("Mon, 05 Oct 2026 00:00:00 GMT", now), 0)
        for invalid in (None, "garbage", "-4", "1.5", "9" * 500):
            self.assertIsNone(retry_after_seconds(invalid, now))
        problem = response_problem({"status": 429, "retry_after": "120", "html": True})
        self.assertEqual((problem.code, problem.retry_after), ("rate_limited", 120))

    def test_no_identity_coercion_from_boolean_or_float(self):
        for value in (True, False, 0, -1, 1.5, "1.0", "١", "", None):
            self.assertIsNone(user_id(value))
        self.assertEqual(user_id("123"), 123)

    def test_cookie_export_drops_github_and_unrelated_sites(self):
        data = [
            cookie(x)
            for x in [
                "agentrouter.org",
                ".agentrouter.org",
                ".github.com",
                "agentrouter.org.evil.test",
                ".org",
                "api.justwoker.icu",
            ]
        ]
        result = site_cookies(data, PROVIDERS["agentrouter"])
        self.assertEqual(len(result), 2)
        self.assertTrue(all(c["domain"] == "agentrouter.org" for c in result))

    def test_parent_cookie_is_narrowed_and_export_has_no_extra_fields(self):
        item = cookie(".justwoker.icu") | {"unknown": "private"}
        result = site_cookies([item], PROVIDERS["justdowork"])
        self.assertEqual(result[0]["domain"], "api.justwoker.icu")
        self.assertNotIn("unknown", result[0])

    def test_server_messages_never_enter_logs(self):
        value = response_problem(
            {"status": 200, "body": {"success": False, "message": "session=PRIVATE"}}
        )
        self.assertNotIn("PRIVATE", value.message)

    def test_auth_html_and_rate_limit_are_not_success(self):
        for data in (
            {"status": 401},
            {"status": 403},
            {"status": 429},
            {"status": 200, "html": True},
            {"status": 0},
        ):
            self.assertEqual(response_problem(data).exit_code, 2)

    def test_user_info_is_not_proof_of_reward(self):
        self.assertEqual(
            agent_result({"data": {"id": 42}}).code, "triggered_unconfirmed"
        )
        self.assertEqual(agent_result({"data": {"checked_in": False}}).exit_code, 2)
        self.assertEqual(agent_result({"data": {"checked_in": True}}).exit_code, 0)

    def test_checkin_requires_explicit_server_boolean(self):
        for value in (
            {},
            {"data": {"enabled": True}},
            {"data": {"enabled": True, "stats": {"checked_in_today": "true"}}},
        ):
            self.assertIsNone(checkin_status(value))
        self.assertTrue(
            checkin_status(
                {"data": {"enabled": True, "stats": {"checked_in_today": True}}}
            )
        )

    def test_agent_receipt_requires_exact_event_identity_and_current_day(self):
        now = datetime(2026, 10, 6, 2, tzinfo=timezone.utc)
        record = {
            "type": 4,
            "user_id": 42,
            "created_at": int(now.timestamp()),
            "content": "每日签到成功，增加额度 ＄25.000000 额度",
        }

        def check(item):
            return agent_receipt_confirmed(
                {"success": True, "data": {"items": [item]}}, 42, now
            )

        self.assertTrue(check(record))
        for change in (
            {"user_id": 88},
            {"type": 1},
            {"created_at": int(now.timestamp()) - 86400},
            {"created_at": int(now.timestamp()) + 60},
            {"content": "登录成功"},
            {"content": "每日签到成功，增加额度 ＄0.000000 额度"},
            {"content": "not a receipt: 每日签到成功，增加额度 ＄25.000000 额度"},
            {"created_at": True},
        ):
            with self.subTest(change=change):
                self.assertFalse(check(record | change))
        self.assertFalse(
            agent_receipt_confirmed(
                {"success": False, "data": {"items": [record]}}, 42, now
            )
        )


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.account = self.store.add("Account A")
        self.vault = Vault(self.store, self.account)
        self.vault.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_plain_local_roundtrip_excludes_github(self):
        self.vault.save(
            "agentrouter",
            42,
            [cookie("agentrouter.org"), cookie(".github.com", "github-secret")],
        )
        raw = self.vault.path("agentrouter").read_bytes()
        self.assertIn(b"test-only-value", raw)
        self.assertNotIn(b"github-secret", raw)
        state = self.vault.load("agentrouter")
        self.assertEqual(state["user_id"], 42)
        self.assertEqual(len(state["cookies"]), 1)
        self.assertEqual(state["cookies"][0]["value"], "test-only-value")
        self.assertFalse(list(self.vault.root.glob("*.tmp")))

    def test_corrupt_file_is_rejected(self):
        self.vault.path("agentrouter").write_bytes(b"not-encrypted")
        with self.assertRaises(VaultError):
            self.vault.load("agentrouter")

    def test_old_notes_migrate_to_github_name_without_changing_sessions(self):
        self.store.bind_github(self.account, "Alice")
        self.vault.save("agentrouter", 42, [cookie("agentrouter.org")])
        self.store.enable(self.account, "justdowork", False)
        from checkin.core import Result

        self.store.record(self.account, "agentrouter", Result("checked_in", "ok"))
        with self.store.db() as db:
            db.execute(
                "UPDATE accounts SET label='old note' WHERE id=?", (self.account,)
            )
            db.execute(
                "UPDATE history SET label='old note' WHERE account_id=?",
                (self.account,),
            )
        before = self.vault.path("agentrouter").read_bytes()
        restored = Store(self.temp.name)
        self.assertEqual(restored.account(self.account)["label"], "Alice")
        self.assertEqual(restored.history()[0]["label"], "Alice")
        self.assertFalse(
            next(r for r in restored.sites(self.account) if r["site"] == "justdowork")[
                "enabled"
            ]
        )
        self.assertEqual(self.vault.path("agentrouter").read_bytes(), before)

    def test_lock_prevents_concurrent_rotation(self):
        with self.vault.lock("agentrouter"):
            with self.assertRaises(VaultError):
                with self.vault.lock("agentrouter"):
                    self.fail("Second writer must not acquire the lock")

    def test_unknown_site_cannot_select_files(self):
        with self.assertRaises(VaultError):
            self.vault.path("../secret")

    def test_two_accounts_survive_store_restart_without_mixing(self):
        other = self.store.add("Account B")
        self.vault.save("agentrouter", 42, [cookie("agentrouter.org", "account-a")])
        Vault(self.store, other).save(
            "agentrouter", 88, [cookie("agentrouter.org", "account-b")]
        )
        restored = Store(self.temp.name)
        self.assertEqual([a["id"] for a in restored.accounts()], [self.account, other])
        self.assertEqual(
            Vault(restored, self.account).load("agentrouter")["cookies"][0]["value"],
            "account-a",
        )
        self.assertEqual(Vault(restored, other).load("agentrouter")["user_id"], 88)

    def test_mismatched_identity_cannot_overwrite_site(self):
        self.vault.save("agentrouter", 42, [cookie("agentrouter.org")])
        with self.assertRaises(VaultError):
            self.vault.save(
                "agentrouter", 88, [cookie("agentrouter.org", "wrong-account")]
            )
        self.assertEqual(self.vault.load("agentrouter")["user_id"], 42)

    def test_github_export_filters_sites_and_rejects_duplicate_account(self):
        state = {
            "cookies": [cookie(".github.com", "gh-a"), cookie("agentrouter.org")],
            "origins": [],
        }
        self.store.save_github(self.account, "user-a", state)
        self.assertEqual(len(self.store.load_github(self.account)["cookies"]), 1)
        other = self.store.add("Another")
        with self.assertRaises(VaultError):
            self.store.save_github(other, "USER-A", state)
        with self.assertRaises(VaultError):
            self.store.save_github(self.account, "user-b", state)

    def test_removal_is_scoped_to_one_account(self):
        other = self.store.add("Account B")
        self.vault.save("agentrouter", 42, [cookie("agentrouter.org")])
        Vault(self.store, other).save("agentrouter", 88, [cookie("agentrouter.org")])
        self.store.remove(self.account)
        self.assertEqual(len(self.store.accounts()), 1)
        self.assertEqual(Vault(self.store, other).load("agentrouter")["user_id"], 88)


if __name__ == "__main__":
    unittest.main()
