"""Synthetic two-account GitHub/OAuth recovery, using isolated browser contexts."""

import json
import tempfile
import unittest
from urllib.parse import urlsplit
from playwright.async_api import async_playwright
from checkin.github import login_github
from checkin.browser import interactive_site, run_site
from checkin.core import PROVIDERS
from checkin.vault import Store, Vault


def gh_cookie(login):
    return {
        "name": "dotcom_user",
        "value": login,
        "domain": "github.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "sameSite": "Lax",
        "expires": -1,
    }


class GitHubFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.a = self.store.add("A")
        self.b = self.store.add("B")
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(channel="msedge", headless=True)
        self.login_as = "alice"
        self.expired = False
        self.consent = False
        self.wrong_uid = False
        self.dialog_title = None
        self.requests = []
        original = self.browser.new_context

        async def new_context(**kwargs):
            context = await original(**kwargs)
            await context.set_offline(True)
            await context.route("**/*", self.route)
            return context

        self.browser.new_context = new_context

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()
        self.temp.cleanup()

    async def route(self, route):
        r = route.request
        u = urlsplit(r.url)
        headers = await r.all_headers()
        self.requests.append((u.hostname, u.path, headers.get("cookie", "")))
        if u.hostname == "github.com":
            cookies = headers.get("cookie", "")
            login = (
                "bob"
                if "dotcom_user=bob" in cookies
                else "alice"
                if "dotcom_user=alice" in cookies
                else None
            )
            if u.path == "/login":
                await route.fulfill(
                    content_type="text/html",
                    headers={
                        "set-cookie": f"dotcom_user={self.login_as}; Path=/; Secure; HttpOnly; SameSite=Lax"
                    },
                    body=f'<meta name="user-login" content="{self.login_as}">',
                )
            elif u.path == "/settings/profile":
                if self.expired or not login:
                    await route.fulfill(
                        content_type="text/html",
                        body="<script>location.href='https://github.com/session-required'</script>",
                    )
                else:
                    await route.fulfill(
                        content_type="text/html",
                        body=f'<meta name="user-login" content="{login}">',
                    )
            elif u.path == "/login/oauth/authorize":
                if self.consent:
                    await route.fulfill(
                        content_type="text/html",
                        body=f'<meta name="user-login" content="{login}"><button>Authorize example</button>',
                    )
                else:
                    await route.fulfill(
                        content_type="text/html",
                        body=f"<script>location.href='https://api.justwoker.icu/oauth/github?fixture={login}'</script>",
                    )
            else:
                await route.fulfill(
                    content_type="text/html", body="GitHub login required"
                )
            return
        if u.hostname != "api.justwoker.icu":
            await route.abort()
            return
        if u.path == "/sign-in":
            body = """<button onclick="location.href='https://github.com/login/oauth/authorize'">Continue with GitHub</button>"""
            if self.dialog_title:
                body = f"""<meta charset="utf-8"><div id="login" aria-hidden="true" inert>{body}</div>
                <div role="dialog" aria-labelledby="title"><h2 id="title">{self.dialog_title}</h2>
                <button onclick="document.querySelector('#login').removeAttribute('aria-hidden'); document.querySelector('#login').removeAttribute('inert'); this.parentElement.remove()">关闭</button>
                <button>Close</button></div>"""
            await route.fulfill(
                content_type="text/html",
                body=body,
            )
            return
        if u.path == "/oauth/github":
            login = "bob" if "bob" in u.query else "alice"
            await route.fulfill(
                content_type="text/html",
                body=f"""<script>
                (async()=>{{ await (await fetch('/api/oauth/github?fixture={login}')).json(); location.href='/profile'; }})();
                </script>""",
            )
            return
        if u.path == "/profile":
            await route.fulfill(
                content_type="text/html",
                body="""<script>fetch('/api/user/auth/refresh', {method:'POST'});</script>""",
            )
            return
        login = (
            "bob"
            if "site_session=bob" in headers.get("cookie", "") or "bob" in u.query
            else "alice"
        )
        uid = 88 if login == "bob" else 42
        if self.wrong_uid:
            uid = 999
        response_headers = {}
        if u.path in {"/api/oauth/github", "/api/user/auth/refresh"}:
            response_headers["set-cookie"] = (
                f"site_session={login}; Path=/; Secure; HttpOnly; SameSite=Lax"
            )
            data = {
                "user": {"id": uid},
                "access_token": "fixture-" + login,
                "token_type": "Bearer",
            }
        elif u.path == "/api/user/self":
            data = {"id": uid}
        elif u.path == "/api/user/checkin":
            data = {"enabled": True, "stats": {"checked_in_today": True}}
        else:
            await route.abort()
            return
        await route.fulfill(
            content_type="application/json",
            headers=response_headers,
            body=json.dumps({"success": True, "data": data}),
        )

    async def test_two_github_logins_reopen_and_connect_to_matching_site_accounts(self):
        for account, login in [(self.a, "alice"), (self.b, "bob")]:
            self.login_as = login
            result = await login_github(self.browser, self.store, account, timeout=5)
            self.assertEqual(result.code, "saved", self.requests)
        # Reopen the store and recreate browser contexts, as after application restart.
        restored = Store(self.temp.name)
        for account, uid in [(self.a, 42), (self.b, 88)]:
            vault = Vault(restored, account)
            result = await interactive_site(
                self.browser,
                PROVIDERS["justdowork"],
                vault,
                login=True,
                timeout=10,
                background=True,
                use_github=True,
            )
            self.assertEqual(result.code, "saved", self.requests)
            self.assertEqual(vault.load("justdowork")["user_id"], uid)
            self.assertEqual(
                (await run_site(self.browser, PROVIDERS["justdowork"], vault)).code,
                "already_checked_in",
            )
        self.assertFalse(
            any(
                "dotcom_user" in c
                for host, _, c in self.requests
                if host == "api.justwoker.icu"
            )
        )
        self.assertFalse(
            any(
                "site_session" in c
                for host, _, c in self.requests
                if host == "github.com"
            )
        )

    async def test_fresh_oauth_ignores_site_cookie_but_keeps_identity_guard(self):
        self.store.save_github(
            self.a, "alice", {"cookies": [gh_cookie("alice")], "origins": []}
        )
        vault = Vault(self.store, self.a)
        vault.save(
            "justdowork",
            42,
            [dict(gh_cookie("old"), name="site_session", domain="api.justwoker.icu")],
        )
        self.wrong_uid = True
        result = await interactive_site(
            self.browser,
            PROVIDERS["justdowork"],
            vault,
            login=True,
            timeout=8,
            background=True,
            use_github=True,
            force_oauth=True,
        )
        self.assertEqual(result.code, "identity_mismatch")
        site_cookies = [
            c
            for host, path, c in self.requests
            if host == "api.justwoker.icu" and path == "/sign-in"
        ]
        self.assertEqual(site_cookies, [""])
        self.assertEqual(vault.load("justdowork")["user_id"], 42)

    async def test_expired_github_requires_intervention(self):
        self.store.save_github(
            self.a, "alice", {"cookies": [gh_cookie("alice")], "origins": []}
        )
        self.expired = True
        result = await interactive_site(
            self.browser,
            PROVIDERS["justdowork"],
            Vault(self.store, self.a),
            login=True,
            timeout=5,
            background=True,
            use_github=True,
        )
        self.assertEqual(result.code, "github_required")

    async def test_site_notice_no_longer_hides_github_login(self):
        self.store.save_github(
            self.a, "alice", {"cookies": [gh_cookie("alice")], "origins": []}
        )
        for title in ("公告", "Notice"):
            with self.subTest(title=title):
                self.dialog_title = title
                result = await interactive_site(
                    self.browser,
                    PROVIDERS["justdowork"],
                    Vault(self.store, self.a),
                    login=True,
                    timeout=8,
                    background=True,
                    use_github=True,
                )
                self.assertEqual(result.code, "saved", result.message)

    async def test_unknown_consent_modal_is_not_dismissed(self):
        self.store.save_github(
            self.a, "alice", {"cookies": [gh_cookie("alice")], "origins": []}
        )
        self.dialog_title = "同意服务条款"
        result = await interactive_site(
            self.browser,
            PROVIDERS["justdowork"],
            Vault(self.store, self.a),
            login=True,
            timeout=3,
            background=True,
            use_github=True,
        )
        self.assertEqual(result.code, "timeout")
        self.assertIn("GitHub 登录入口", result.message)
        self.assertFalse(
            any(path == "/api/oauth/github" for _, path, _ in self.requests)
        )

    async def test_new_authorization_not_accepted_in_background(self):
        self.store.save_github(
            self.a, "alice", {"cookies": [gh_cookie("alice")], "origins": []}
        )
        self.consent = True
        result = await interactive_site(
            self.browser,
            PROVIDERS["justdowork"],
            Vault(self.store, self.a),
            login=True,
            timeout=6,
            background=True,
            use_github=True,
        )
        self.assertEqual(result.code, "authorization_required")
        self.assertIsNone(Vault(self.store, self.a).load("justdowork"))

    async def test_valid_site_does_not_require_github_to_be_valid(self):
        self.store.save_github(
            self.a, "alice", {"cookies": [gh_cookie("alice")], "origins": []}
        )
        vault = Vault(self.store, self.a)
        vault.save("justdowork", 42, [])
        self.expired = True
        # Site's independent refresh session is valid in this fixture.
        result = await interactive_site(
            self.browser,
            PROVIDERS["justdowork"],
            vault,
            timeout=6,
            background=True,
            use_github=True,
        )
        self.assertEqual(result.code, "saved")
        self.assertFalse(any(host == "github.com" for host, _, _ in self.requests))
