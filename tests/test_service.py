import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from checkin.core import Result
from checkin.service import perform, batch, wait_interval
from checkin.vault import Store


class Runtime:
    async def __aenter__(self):
        return SimpleNamespace(
            chromium=SimpleNamespace(
                launch=AsyncMock(return_value=SimpleNamespace(close=AsyncMock()))
            )
        )

    async def __aexit__(self, *args):
        pass


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.a = self.store.add("A")
        self.store.bind_github(self.a, "alice")
        self.interval = patch("checkin.service.wait_interval", AsyncMock()).start()
        patch(
            "checkin.service.launch_options", return_value={"channel": "msedge"}
        ).start()
        self.addCleanup(patch.stopall)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_confirmed_today_skips_without_starting_browser(self):
        for code in ("checked_in", "already_checked_in"):
            with (
                self.subTest(code=code),
                patch.object(
                    self.store, "now", return_value="2026-10-06T01:00:00+00:00"
                ),
            ):
                self.store.record(self.a, "justdowork", Result(code, "confirmed"))
                with patch("playwright.async_api.async_playwright") as runtime:
                    result = await perform(self.store, self.a, "run", "justdowork")
                self.assertEqual(result.code, "skipped_today")
                self.assertEqual(result.exit_code, 0)
                runtime.assert_not_called()

    async def test_repeated_skips_do_not_extend_confirmation_across_midnight(self):
        with patch.object(self.store, "now", return_value="2026-10-06T15:00:00+00:00"):
            self.store.record(self.a, "justdowork", Result("checked_in", "confirmed"))
        with patch.object(self.store, "now", return_value="2026-10-06T15:59:59+00:00"):
            with patch("playwright.async_api.async_playwright") as runtime:
                await batch(self.store, [(self.a, "run", "justdowork")])
                await batch(self.store, [(self.a, "run", "justdowork")])
                runtime.assert_not_called()
            self.assertEqual(self.store.history()[0]["code"], "skipped_today")
        with (
            patch.object(self.store, "now", return_value="2026-10-06T16:00:00+00:00"),
            patch("playwright.async_api.async_playwright", return_value=Runtime()),
            patch(
                "checkin.browser.run_site",
                AsyncMock(return_value=Result("checked_in", "new day")),
            ) as run,
        ):
            result = await perform(self.store, self.a, "run", "justdowork")
            run.assert_awaited_once()
            self.assertEqual(result.code, "checked_in")

    async def test_unconfirmed_and_invalid_dates_never_skip(self):
        cases = [
            ("saved", "2026-10-06T01:00:00+00:00"),
            ("triggered_unconfirmed", "2026-10-06T01:00:00+00:00"),
            ("request_failed", "2026-10-06T01:00:00+00:00"),
            ("checked_in", "2026-10-07T01:00:00+00:00"),
            ("checked_in", "invalid"),
            ("checked_in", "2026-10-06T01:00:00"),
        ]
        for code, date in cases:
            with self.subTest(code=code, date=date):
                aid = self.store.add()
                with patch.object(self.store, "now", return_value=date):
                    self.store.record(aid, "justdowork", Result(code, "fixture"))
                with (
                    patch.object(
                        self.store, "now", return_value="2026-10-06T02:00:00+00:00"
                    ),
                    patch(
                        "playwright.async_api.async_playwright", return_value=Runtime()
                    ),
                    patch(
                        "checkin.browser.run_site",
                        AsyncMock(return_value=Result("request_failed", "fixture")),
                    ) as run,
                ):
                    await perform(self.store, aid, "run", "justdowork")
                    run.assert_awaited_once()

    async def test_skip_is_scoped_to_account_and_site_and_manual_open_bypasses_it(self):
        b = self.store.add()
        self.store.record(self.a, "justdowork", Result("checked_in", "confirmed"))
        with (
            patch("playwright.async_api.async_playwright", return_value=Runtime()),
            patch(
                "checkin.browser.run_site",
                AsyncMock(return_value=Result("checked_in", "fixture")),
            ) as run,
            patch(
                "checkin.browser.interactive_site",
                AsyncMock(return_value=Result("saved", "fixture")),
            ) as interactive,
        ):
            await perform(self.store, b, "run", "justdowork")
            await perform(self.store, self.a, "run", "agentrouter")
            await perform(self.store, self.a, "open", "justdowork")
            self.assertEqual(run.await_count, 2)
            interactive.assert_awaited_once()

    async def test_expired_site_recovers_then_checks_once(self):
        run = AsyncMock(
            side_effect=[
                Result("login_required", "expired"),
                Result("already_checked_in", "done"),
            ]
        )
        recover = AsyncMock(return_value=Result("saved", "restored"))
        with (
            patch("playwright.async_api.async_playwright", return_value=Runtime()),
            patch("checkin.browser.run_site", run),
            patch("checkin.browser.interactive_site", recover),
        ):
            result = await perform(self.store, self.a, "run", "justdowork")
        self.assertEqual(result.code, "already_checked_in")
        self.assertEqual(run.await_count, 2)
        self.assertEqual(recover.await_count, 1)
        self.assertTrue(recover.call_args.kwargs["background"])

    async def test_verification_does_not_start_relogin_loop(self):
        with (
            patch("playwright.async_api.async_playwright", return_value=Runtime()),
            patch(
                "checkin.browser.run_site",
                AsyncMock(return_value=Result("verification_required", "challenge")),
            ),
            patch("checkin.browser.interactive_site", AsyncMock()) as recover,
        ):
            result = await perform(self.store, self.a, "run", "justdowork")
        self.assertEqual(result.code, "verification_required")
        recover.assert_not_awaited()

    async def test_agent_query_failures_never_relogin(self):
        for code in (
            "request_failed",
            "rate_limited",
            "verification_required",
            "schema_changed",
            "site_rejected",
        ):
            self.store.set_setting("site_control_agentrouter", {})
            with (
                self.subTest(code=code),
                patch("playwright.async_api.async_playwright", return_value=Runtime()),
                patch(
                    "checkin.browser.run_site",
                    AsyncMock(return_value=Result(code, "fixture")),
                ),
                patch("checkin.browser.interactive_site", AsyncMock()) as oauth,
            ):
                result = await perform(self.store, self.a, "run", "agentrouter")
                self.assertEqual(result.code, code)
                oauth.assert_not_awaited()

    async def test_agent_interval_persists_and_confirmed_accounts_do_not_wait(self):
        b = self.store.add("B")
        with (
            patch("checkin.service.time.time", return_value=1000),
            patch("checkin.service.random.randint", return_value=47) as draw,
            patch(
                "checkin.service._perform",
                AsyncMock(return_value=Result("checked_in", "ok")),
            ) as run,
        ):
            await batch(
                self.store, [(self.a, "run", "agentrouter"), (b, "run", "agentrouter")]
            )
            self.assertEqual(run.await_count, 2)
            self.assertEqual(self.interval.await_args_list[-1].args[0], 47)
            draw.assert_called_with(30, 60)
            self.interval.reset_mock()
            await perform(Store(self.temp.name), self.a, "run", "agentrouter")
            self.interval.assert_not_awaited()
            self.assertEqual(
                self.store.setting("site_control_agentrouter")["next_run"], 1047
            )
            # An unconfirmed account in a new application instance still waits.
            await perform(
                Store(self.temp.name), self.store.add("C"), "run", "agentrouter"
            )
            self.assertEqual(self.interval.await_args.args[0], 47)

    async def test_rate_limit_stops_same_site_but_other_site_continues(self):
        b = self.store.add("B")
        with (
            patch("checkin.service.time.time", return_value=1000),
            patch(
                "checkin.service._perform",
                AsyncMock(
                    side_effect=[
                        Result("rate_limited", "limited", 120),
                        Result("checked_in", "ok"),
                    ]
                ),
            ) as run,
        ):
            await batch(
                self.store,
                [
                    (self.a, "run", "agentrouter"),
                    (b, "run", "agentrouter"),
                    (b, "run", "justdowork"),
                ],
            )
            self.assertEqual(run.await_count, 2)
            self.assertEqual(
                self.store.setting("site_control_agentrouter")["retry_until"], 1120
            )
            self.assertEqual(
                [r["code"] for r in self.store.history()],
                ["checked_in", "site_paused", "rate_limited"],
            )
            for action in ("run", "open", "connect"):
                result = await perform(Store(self.temp.name), b, action, "agentrouter")
                self.assertEqual(result.code, "cooldown")
            self.assertEqual(run.await_count, 2)
        with (
            patch("checkin.service.time.time", return_value=1121),
            patch(
                "checkin.service._perform",
                AsyncMock(return_value=Result("checked_in", "ok")),
            ) as run,
        ):
            await perform(self.store, b, "run", "agentrouter")
            run.assert_awaited_once()

    async def test_missing_retry_after_defaults_to_fifteen_minutes(self):
        with (
            patch("checkin.service.time.time", return_value=1000),
            patch(
                "checkin.service._perform",
                AsyncMock(return_value=Result("rate_limited", "limited")),
            ),
        ):
            await perform(self.store, self.a, "run", "justdowork")
        self.assertEqual(
            self.store.setting("site_control_justdowork")["retry_until"], 1900
        )

    async def test_verification_persists_until_manual_confirmed_success(self):
        with patch(
            "checkin.service._perform",
            AsyncMock(
                side_effect=[
                    Result("verification_required", "challenge"),
                    Result("timeout", "not finished"),
                    Result("checked_in", "confirmed"),
                    Result("checked_in", "ok"),
                ]
            ),
        ) as run:
            await perform(self.store, self.a, "run", "justdowork")
            restored = Store(self.temp.name)
            self.assertEqual(
                (await perform(restored, self.a, "run", "justdowork")).code,
                "site_paused",
            )
            await perform(restored, self.a, "open", "justdowork")
            self.assertEqual(
                (await perform(restored, self.a, "run", "justdowork")).code,
                "site_paused",
            )
            await perform(restored, self.a, "open", "justdowork")
            self.assertEqual(
                (await perform(restored, self.a, "run", "justdowork")).code,
                "checked_in",
            )
            self.assertEqual(run.await_count, 4)

    async def test_same_site_lock_blocks_other_accounts_and_releases_after_cancel(self):
        b = self.store.add("B")
        from checkin.vault import VaultError

        with self.store.site_lock("agentrouter"):
            with self.assertRaises(VaultError):
                await perform(Store(self.temp.name), b, "run", "agentrouter")
        with patch(
            "checkin.service._perform", AsyncMock(side_effect=asyncio.CancelledError())
        ):
            with self.assertRaises(asyncio.CancelledError):
                await perform(self.store, self.a, "run", "agentrouter")
        self.assertGreater(
            self.store.setting("site_control_agentrouter")["next_run"], 0
        )
        with self.store.site_lock("agentrouter"):
            pass

    async def test_wait_countdown_and_cancellation(self):
        notices = []
        with (
            patch("checkin.service.time.monotonic", side_effect=[100, 100, 101, 102]),
            patch("checkin.service.asyncio.sleep", AsyncMock()) as sleep,
        ):
            await wait_interval(2, notices.append, "JustDoWork")
            self.assertEqual([c.args[0] for c in sleep.await_args_list], [1, 1])
            self.assertIn("2 秒", notices[0])
            self.assertIn("JustDoWork", notices[0])
            self.assertIn("1 秒", notices[1])
        with patch(
            "checkin.service.asyncio.sleep",
            AsyncMock(side_effect=asyncio.CancelledError()),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await wait_interval(60, notices.append, "JustDoWork")

    async def test_just_interval_persists_independently_and_success_skips_wait(self):
        b = self.store.add("B")
        self.store.set_setting("site_control_agentrouter", {"next_run": 9999})
        with (
            patch("checkin.service.time.time", return_value=1000),
            patch("checkin.service.random.randint", return_value=53) as draw,
            patch(
                "checkin.service._perform",
                AsyncMock(return_value=Result("checked_in", "ok")),
            ) as run,
        ):
            await batch(self.store, [(self.a, "run", "justdowork")])
            self.assertLessEqual(self.interval.await_args.args[0], 0)
            self.assertEqual(self.interval.await_args.args[2], "JustDoWork")
            self.assertEqual(
                self.store.setting("site_control_justdowork")["next_run"], 1060
            )
            self.assertEqual(
                self.store.setting("site_control_agentrouter")["next_run"], 9999
            )
            draw.assert_not_called()
            restored = Store(self.temp.name)
            await perform(restored, b, "run", "justdowork")
            self.assertEqual(self.interval.await_args.args[0], 60)
            self.interval.reset_mock()
            self.assertEqual(
                (await perform(restored, self.a, "run", "justdowork")).code,
                "skipped_today",
            )
            self.interval.assert_not_awaited()
            self.assertEqual(run.await_count, 2)

    async def test_agent_without_receipt_runs_one_fresh_oauth_not_login_only(self):
        with (
            patch("playwright.async_api.async_playwright", return_value=Runtime()),
            patch(
                "checkin.browser.run_site",
                AsyncMock(return_value=Result("triggered_unconfirmed", "no receipt")),
            ),
            patch(
                "checkin.browser.interactive_site",
                AsyncMock(return_value=Result("checked_in", "confirmed")),
            ) as oauth,
        ):
            result = await perform(self.store, self.a, "run", "agentrouter")
        self.assertEqual(result.code, "checked_in")
        oauth.assert_awaited_once()
        self.assertTrue(oauth.call_args.kwargs["force_oauth"])
        self.assertTrue(oauth.call_args.kwargs["use_github"])
        self.assertFalse(oauth.call_args.kwargs.get("login", False))

    async def test_cancellation_is_recorded_and_propagated(self):
        with patch(
            "checkin.service.perform", AsyncMock(side_effect=asyncio.CancelledError())
        ):
            with self.assertRaises(asyncio.CancelledError):
                await batch(self.store, [(self.a, "run", "justdowork")])
        self.assertEqual(self.store.history()[0]["code"], "cancelled")

    async def test_failure_does_not_skip_other_accounts_or_log_exception_secrets(self):
        b = self.store.add("B")
        with patch(
            "checkin.service.perform",
            AsyncMock(
                side_effect=[
                    RuntimeError("TOKEN=DO_NOT_LOG"),
                    Result("checked_in", "done"),
                ]
            ),
        ):
            await batch(
                self.store, [(self.a, "run", "agentrouter"), (b, "run", "agentrouter")]
            )
        rows = self.store.history()
        self.assertEqual(len(rows), 2)
        self.assertNotIn("DO_NOT_LOG", str(rows))
        self.assertEqual(rows[0]["code"], "checked_in")
