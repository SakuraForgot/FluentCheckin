import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"
import asyncio
import tempfile
import threading
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from checkin.ui import MainWindow, Worker
from checkin.vault import Store
from checkin.core import Result


class UITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.a = self.store.add("A")
        self.b = self.store.add("B")
        self.store.bind_github(self.a, "alice")
        self.store.bind_github(self.b, "bob")
        self.window = MainWindow(self.store)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        if self.window.worker:
            self.window.worker.cancel()
            self.window.worker.wait(3000)
            self.app.processEvents()
        # Stop decorative animations before removing their offscreen targets.
        self.window.navigationInterface.panel.indicator.stopAnimation()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def test_account_selection_and_disabled_site_are_isolated(self):
        row = next(
            i
            for i, account in enumerate(self.window.account_rows)
            if account["id"] == self.b
        )
        self.window.account_table.selectRow(row)
        self.app.processEvents()
        self.window.site_checks["agentrouter"].setChecked(False)
        self.assertEqual(self.window.selected_id, self.b)
        self.assertTrue(
            next(r for r in self.store.sites(self.a) if r["site"] == "agentrouter")[
                "enabled"
            ]
        )
        self.assertFalse(
            next(r for r in self.store.sites(self.b) if r["site"] == "agentrouter")[
                "enabled"
            ]
        )
        self.assertEqual(len(self.window.run_jobs(self.b)), 1)

    def test_add_opens_github_directly_without_notes(self):
        with patch.object(self.window, "start_jobs") as start:
            self.window.add_button.click()
            account_id = self.window.pending_account_id
            self.assertIsNotNone(account_id)
            start.assert_called_once_with([(account_id, "github", None)])
            self.assertIsNone(self.store.account(account_id)["github_login"])

    def wait_for_worker(self):
        for _ in range(100):
            QTest.qWait(25)
            if self.window.worker is None:
                return
        self.fail("worker did not finish")

    def test_cancelled_add_does_not_leave_an_empty_account(self):
        async def cancelled(store, jobs, emit, notify, started):
            aid = jobs[0][0]
            result = Result("cancelled", "窗口已关闭。")
            store.record(aid, "github", result)
            emit(aid, "github", result)

        with patch("checkin.ui.batch", cancelled):
            self.window.add_button.click()
            self.assertIs(
                self.window.stackedWidget.currentWidget(), self.window.accounts_page
            )
            self.wait_for_worker()
        self.assertEqual(len(self.store.accounts()), 2)
        self.assertIsNone(self.window.pending_account_id)

    def test_successful_add_uses_github_name_and_selects_new_account(self):
        async def login(store, jobs, emit, notify, started):
            aid = jobs[0][0]
            store.save_github(aid, "charlie", {"cookies": [], "origins": []})
            result = Result("saved", "登录已保存")
            store.record(aid, "github", result)
            emit(aid, "github", result)

        with patch("checkin.ui.batch", login):
            self.window.add_button.click()
            self.wait_for_worker()
        self.assertEqual(
            self.store.account(self.window.selected_id)["label"], "charlie"
        )
        self.assertEqual(self.window.selected_label.text(), "charlie")
        self.assertTrue(self.window.checkin_button.isEnabled())
        self.assertIs(
            self.window.stackedWidget.currentWidget(), self.window.accounts_page
        )

    def test_duplicate_login_cleanup_preserves_existing_account(self):
        from checkin.vault import VaultError

        async def duplicate(store, jobs, emit, notify, started):
            aid = jobs[0][0]
            try:
                store.save_github(aid, "alice", {"cookies": [], "origins": []})
            except VaultError as exc:
                result = Result("storage_error", str(exc))
                store.record(aid, "github", result)
                emit(aid, "github", result)

        with patch("checkin.ui.batch", duplicate):
            self.window.add_button.click()
            self.wait_for_worker()
        self.assertEqual(len(self.store.accounts()), 2)
        self.assertEqual(self.store.account(self.a)["github_login"], "alice")
        self.assertIn("已添加", self.window.login_progress.text())

    def test_one_click_uses_only_selected_account_sites_and_shows_progress(self):
        self.window.selected_id = self.b
        self.window.refresh()
        self.window.site_checks["agentrouter"].setChecked(False)
        release = threading.Event()

        async def checking(store, jobs, emit, notify, started):
            self.assertEqual(jobs, [(self.b, "run", "justdowork")])
            started(self.b, "justdowork")
            notify("检查今天的签到状态")
            while not release.is_set():
                await asyncio.sleep(0.01)
            result = Result("already_checked_in", "今天已签到")
            store.record(self.b, "justdowork", result)
            emit(self.b, "justdowork", result)

        with patch("checkin.ui.batch", checking):
            self.window.checkin_button.click()
            try:
                for _ in range(100):
                    QTest.qWait(25)
                    if self.window.job_states[(self.b, "justdowork")][0] == "正在执行":
                        break
                self.assertIs(
                    self.window.stackedWidget.currentWidget(), self.window.tasks_page
                )
                self.assertEqual(self.window.account_filter.currentData(), self.b)
                self.assertEqual(
                    self.window.job_states[(self.b, "justdowork")][0], "正在执行"
                )
            finally:
                release.set()
                self.wait_for_worker()
        self.assertEqual(
            self.window.job_states[(self.b, "justdowork")][0], "当天已签到"
        )
        self.assertEqual(self.window.progress_bar.value(), 1)
        self.assertEqual(self.store.account(self.a)["label"], "alice")

    def test_schedule_disabled_by_default_and_only_once_per_day(self):
        with patch.object(self.window, "start_jobs") as start:
            self.window.schedule_tick()
            start.assert_not_called()
            self.store.set_setting("schedule_enabled", True)
            self.store.set_setting("schedule_time", "00:00")
            self.window.schedule_tick()
            self.window.schedule_tick()
            self.assertEqual(start.call_count, 1)

    def test_browser_selection_custom_path_and_reset_are_independent(self):
        self.window.browser_combo.setCurrentText("Brave")
        self.assertEqual(self.store.setting("browser"), "brave")
        path = os.path.join(self.temp.name, "brave.exe")
        with open(path, "wb"):
            pass
        with patch("checkin.ui.QFileDialog.getOpenFileName", return_value=(path, "")):
            self.window.choose_browser_path()
        self.assertEqual(self.store.setting("browser_paths"), {"brave": path})
        self.assertEqual(self.window.browser_location.text(), path)
        self.window.browser_combo.setCurrentText("Google Chrome")
        self.assertEqual(self.store.setting("browser"), "chrome")
        self.window.reset_browser_path()
        self.assertEqual(self.store.setting("browser_paths"), {"brave": path})
        self.window.browser_combo.setCurrentText("Brave")
        self.window.reset_browser_path()
        self.assertEqual(self.store.setting("browser_paths"), {})

    def test_all_accounts_button_respects_each_accounts_site_selection(self):
        self.store.enable(self.a, "agentrouter", False)
        self.store.enable(self.b, "justdowork", False)
        self.window.selected_id = self.b
        self.window.refresh()
        self.window.account_filter.setCurrentIndex(
            self.window.account_filter.findData(self.b)
        )
        with patch.object(self.window, "start_jobs") as start:
            self.window.run_all_button.click()
            start.assert_called_once_with(
                [(self.a, "run", "justdowork"), (self.b, "run", "agentrouter")]
            )
        self.assertIsNone(self.window.account_filter.currentData())

    def test_small_window_scrolls_vertically_not_horizontally(self):
        self.window.setMinimumSize(860, 620)
        self.window.resize(860, 620)
        QTest.qWait(150)
        self.assertEqual(self.window.accounts_page.horizontalScrollBar().maximum(), 0)
        self.assertGreater(self.window.accounts_page.verticalScrollBar().maximum(), 0)

    def test_worker_cancels_without_blocking_gui(self):
        async def slow(*args):
            await asyncio.sleep(60)

        with patch("checkin.ui.batch", slow):
            worker = Worker(self.store.root, [])
            worker.start()
            QTest.qWait(100)
            worker.cancel()
            self.assertTrue(worker.wait(3000))
            worker.deleteLater()
