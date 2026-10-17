"""Real headless Edge + intercepted synthetic responses; no real accounts/network."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from urllib.parse import urlsplit

from playwright.async_api import async_playwright
from checkin.browser import Observer, interactive_site, run_site
from checkin.core import PROVIDERS
from checkin.vault import Store, Vault


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.account = self.store.add("Browser fixture")
        self.vault = Vault(self.store, self.account)
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(channel="msedge", headless=True)
        self.requests = []
        self.refresh_status = 200
        self.self_status = 200
        self.identity = 42
        self.turnstile = False
        self.checked = False
        self.post_success = True
        self.agent_checked = None
        self.agent_receipts = []
        self.receipt_status = 200
        self.post_status = 200
        self.checkin_get_status = 200
        self.retry_after = None
        self.self_html = False
        self.self_redirect = False
        self.page_html = None
        self.page_status = 200
        original = self.browser.new_context

        async def create(**kwargs):
            context = await original(**kwargs)
            await context.set_offline(True)
            await context.route("**/*", self.respond)
            return context

        self.browser.new_context = create
        for key, provider in PROVIDERS.items():
            self.vault.save(
                key,
                42,
                [
                    {
                        "name": "session",
                        "value": "fixture-old",
                        "domain": urlsplit(provider.origin).hostname,
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                        "sameSite": "Lax",
                        "expires": -1,
                    }
                ],
            )

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()
        self.temp.cleanup()

    async def respond(self, route):
        request = route.request
        path = urlsplit(request.url).path
        self.requests.append((request.method, request.url, await request.all_headers()))
        status = 200
        headers = {}
        if self.retry_after is not None:
            headers["retry-after"] = self.retry_after
        if (
            path in {"/sign-in", "/profile", "/login", "/console/personal"}
            and self.page_html
        ):
            await route.fulfill(
                status=self.page_status, content_type="text/html", body=self.page_html
            )
            return
        if path == "/api/user/auth/refresh":
            status = self.refresh_status
            headers["set-cookie"] = (
                "session=fixture-rotated; Path=/; Secure; HttpOnly; SameSite=Lax"
            )
            data = {
                "user": {"id": self.identity},
                "access_token": "fixture-access",
                "token_type": "Bearer",
            }
        elif path == "/api/user/self":
            if self.self_html:
                await route.fulfill(
                    status=200,
                    content_type="text/html",
                    body="<title>Verification</title>",
                )
                return
            if self.self_redirect:
                await route.fulfill(
                    status=302, headers={"location": "https://unexpected.invalid/leak"}
                )
                return
            status = self.self_status
            data = {"id": self.identity}
            if self.agent_checked is not None:
                data["checked_in"] = self.agent_checked
        elif path == "/api/user/checkin":
            if request.method == "POST":
                status = self.post_status
                if not self.post_success:
                    await route.abort()
                    return
                self.checked = True
                data = {"checkin_date": "2026-10-06"}
            else:
                status = self.checkin_get_status
                data = {"enabled": True, "stats": {"checked_in_today": self.checked}}
        elif path == "/api/log/self":
            status = self.receipt_status
            data = {"items": self.agent_receipts}
        elif path == "/api/status":
            data = {"turnstile_check": self.turnstile}
        else:
            await route.abort()
            return
        await route.fulfill(
            status=status,
            content_type="application/json",
            headers=headers,
            body=json.dumps({"success": status == 200, "data": data}),
        )

    async def run_provider(self, key="justdowork"):
        return await run_site(self.browser, PROVIDERS[key], self.vault)

    async def test_refresh_checkin_and_readback(self):
        result = await self.run_provider()
        self.assertEqual(result.code, "checked_in")
        posts = [
            r for r in self.requests if r[0] == "POST" and r[1].endswith("/checkin")
        ]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["authorization"], "Bearer fixture-access")
        self.assertTrue(
            all(urlsplit(r[1]).hostname == "api.justwoker.icu" for r in self.requests)
        )
        self.assertEqual(
            self.vault.load("justdowork")["cookies"][0]["value"], "fixture-rotated"
        )
        self.assertNotIn(b"fixture-access", self.vault.path("justdowork").read_bytes())

    async def test_already_checked_does_not_post(self):
        self.checked = True
        result = await self.run_provider()
        self.assertEqual(result.code, "already_checked_in")
        self.assertFalse(
            any(r[0] == "POST" and r[1].endswith("/checkin") for r in self.requests)
        )

    async def test_turnstile_requires_user_without_blind_post(self):
        self.turnstile = True
        result = await self.run_provider()
        self.assertEqual(result.code, "verification_required")
        self.assertFalse(
            any(r[0] == "POST" and r[1].endswith("/checkin") for r in self.requests)
        )

    async def test_refresh_cookie_saved_even_if_self_fails(self):
        self.self_status = 500
        self.assertEqual((await self.run_provider()).exit_code, 2)
        self.assertEqual(
            self.vault.load("justdowork")["cookies"][0]["value"], "fixture-rotated"
        )

    async def test_expired_login_stops_requests(self):
        self.refresh_status = 401
        self.assertEqual((await self.run_provider()).code, "login_required")
        self.assertEqual(len(self.requests), 1)

    async def test_wrong_user_never_signs_in(self):
        self.identity = 99
        self.assertEqual((await self.run_provider()).code, "identity_mismatch")
        self.assertEqual(len(self.requests), 1)

    async def test_post_network_failure_not_retried(self):
        self.post_success = False
        self.assertEqual((await self.run_provider()).exit_code, 2)
        self.assertEqual(
            sum(r[0] == "POST" and r[1].endswith("/checkin") for r in self.requests), 1
        )

    async def test_agent_self_is_not_false_success(self):
        self.assertEqual(
            (await self.run_provider("agentrouter")).code, "triggered_unconfirmed"
        )
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0][2]["new-api-user"], "42")

    async def test_agent_today_system_receipt_confirms_credit(self):
        self.agent_receipts = [
            {
                "user_id": 42,
                "type": 4,
                "created_at": int(datetime.now(timezone.utc).timestamp()),
                "content": "每日签到成功，增加额度 ＄25.000000 额度",
            }
        ]
        result = await self.run_provider("agentrouter")
        self.assertEqual(result.code, "already_checked_in")
        self.assertEqual(urlsplit(self.requests[-1][1]).path, "/api/log/self")
        self.assertIn("type=4", self.requests[-1][1])

    async def test_agent_explicit_reward(self):
        self.agent_checked = True
        self.assertEqual((await self.run_provider("agentrouter")).code, "checked_in")

    async def test_agent_receipt_errors_preserve_stop_reason(self):
        for status, code in (
            (403, "verification_required"),
            (429, "rate_limited"),
            (500, "request_failed"),
            (401, "request_failed"),
        ):
            with self.subTest(status=status):
                self.requests.clear()
                self.receipt_status = status
                self.retry_after = "180"
                result = await self.run_provider("agentrouter")
                self.assertEqual(result.code, code)
                self.assertEqual(len(self.requests), 2)
                if status == 429:
                    self.assertEqual(result.retry_after, 180)

    async def test_blocked_post_stops_without_readback(self):
        for status, code in ((403, "verification_required"), (429, "rate_limited")):
            with self.subTest(status=status):
                self.requests.clear()
                self.checked = False
                self.post_status = status
                self.assertEqual((await self.run_provider()).code, code)
                checks = [r[0] for r in self.requests if r[1].endswith("/checkin")]
                self.assertEqual(checks, ["GET", "POST"])

    async def test_interactive_observer_stops_on_rate_limited_auth(self):
        self.refresh_status = 429
        self.retry_after = "90"
        self.page_html = """<!doctype html><script>fetch('/api/user/auth/refresh', {method:'POST'});</script>"""
        result = await interactive_site(
            self.browser, PROVIDERS["justdowork"], self.vault, timeout=5
        )
        self.assertEqual(result.code, "rate_limited")
        self.assertEqual(result.retry_after, 90)
        self.assertFalse(any(r[1].endswith("/api/user/self") for r in self.requests))

    async def test_manual_window_can_recover_after_document_verification(self):
        self.page_status = 403
        self.page_html = """<!doctype html><script>
          setTimeout(() => fetch('/api/user/auth/refresh', {method:'POST'}), 1300);
        </script>"""
        result = await interactive_site(
            self.browser, PROVIDERS["justdowork"], self.vault, login=True, timeout=6
        )
        self.assertEqual(result.code, "saved")
        self.assertEqual(sum(r[1].endswith("/api/user/self") for r in self.requests), 1)

    async def test_background_stops_on_document_verification(self):
        self.page_status = 403
        self.page_html = "<!doctype html><title>Verification</title>"
        result = await interactive_site(
            self.browser,
            PROVIDERS["justdowork"],
            self.vault,
            background=True,
            timeout=5,
        )
        self.assertEqual(result.code, "verification_required")
        self.assertEqual(len(self.requests), 1)

    async def test_html_challenge_is_not_success(self):
        self.self_html = True
        self.assertEqual(
            (await self.run_provider("agentrouter")).code, "verification_required"
        )

    async def test_credentials_never_follow_redirect(self):
        self.self_redirect = True
        self.assertEqual((await self.run_provider()).code, "request_failed")
        self.assertFalse(any("unexpected.invalid" in r[1] for r in self.requests))

    async def test_observer_ignores_github_response(self):
        class Response:
            url = "https://github.com/login/oauth/authorize?code=not-a-real-code"

            async def json(self):
                raise AssertionError("GitHub response must not be read")

        observer = Observer(PROVIDERS["justdowork"])
        observer.on_response(Response())
        self.assertFalse(observer.tasks)

    async def test_interactive_login_exports_only_verified_site_session(self):
        self.page_html = """<!doctype html><script>
          fetch('/api/user/auth/refresh', {method:'POST'});
        </script>"""
        result = await interactive_site(
            self.browser, PROVIDERS["justdowork"], self.vault, login=True, timeout=5
        )
        self.assertEqual(result.code, "saved")
        self.assertEqual(
            self.vault.load("justdowork")["cookies"][0]["value"], "fixture-rotated"
        )

    async def test_interactive_readback_of_completed_checkin(self):
        self.checked = True
        self.page_html = """<!doctype html><script>
          (async () => {
            await fetch('/api/user/auth/refresh', {method:'POST'});
            await fetch('/api/user/checkin');
          })();
        </script>"""
        result = await interactive_site(
            self.browser, PROVIDERS["justdowork"], self.vault, timeout=5
        )
        self.assertEqual(result.code, "checked_in")

    async def test_interactive_failure_keeps_rotated_cookie_without_retry_loop(self):
        self.self_status = 500
        self.page_html = """<!doctype html><script>
          fetch('/api/user/auth/refresh', {method:'POST'});
        </script>"""
        result = await interactive_site(
            self.browser, PROVIDERS["justdowork"], self.vault, timeout=3
        )
        self.assertEqual(result.code, "request_failed")
        self.assertEqual(
            self.vault.load("justdowork")["cookies"][0]["value"], "fixture-rotated"
        )
        self.assertEqual(
            sum(urlsplit(r[1]).path == "/api/user/self" for r in self.requests), 1
        )

    async def test_open_without_saved_session_navigates_to_profile_after_login(self):
        self.vault.path("justdowork").unlink()
        self.checked = True
        self.page_html = """<!doctype html><script>
          (async () => {
            await fetch('/api/user/auth/refresh', {method:'POST'});
            if (location.pathname === '/profile') await fetch('/api/user/checkin');
          })();
        </script>"""
        result = await interactive_site(
            self.browser, PROVIDERS["justdowork"], self.vault, timeout=6
        )
        self.assertEqual(result.code, "checked_in")
        self.assertTrue(any(urlsplit(r[1]).path == "/profile" for r in self.requests))


if __name__ == "__main__":
    unittest.main()
