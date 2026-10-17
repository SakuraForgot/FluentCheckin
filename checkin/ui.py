"""Fluent desktop shell. Browser work stays off the GUI thread."""

import asyncio
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

from PySide6.QtCore import QThread, QTime, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CheckBox,
    ComboBox,
    FluentIcon as FIF,
    FluentWindow,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    NavigationItemPosition,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
    SubtitleLabel,
    SwitchButton,
    TableWidget,
    Theme,
    TimeEdit,
    TitleLabel,
    setTheme,
)
from .core import PROVIDERS
from .service import batch
from .vault import Store, Vault, VaultError
from .runtime import BROWSERS, BrowserUnavailable, browser_path


STATUS = {
    "not_connected": "尚未连接",
    "saved": "会话已保存",
    "checked_in": "签到成功",
    "already_checked_in": "当天已签到",
    "skipped_today": "今日已确认 · 已跳过",
    "login_required": "需要登录",
    "github_required": "GitHub 需登录",
    "authorization_required": "需要授权确认",
    "verification_required": "需要官网验证",
    "rate_limited": "触发限流",
    "cooldown": "限流冷却中",
    "site_paused": "站点已暂停",
    "triggered_unconfirmed": "到账未确认",
    "unconfirmed": "结果未确认",
    "request_failed": "请求失败",
    "browser_error": "浏览器异常",
    "storage_error": "会话处理失败",
    "identity_mismatch": "账号不匹配",
    "session_conflict": "会话冲突",
    "schema_changed": "接口需检查",
    "site_rejected": "站点未接受",
    "cancelled": "已取消",
    "timeout": "等待超时",
}


def display_time(value):
    if not value:
        return "—"
    return (
        datetime.fromisoformat(value)
        .astimezone(timezone(timedelta(hours=8)))
        .strftime("%m-%d %H:%M")
    )


def table(headers, parent):
    view = TableWidget(parent)
    view.setColumnCount(len(headers))
    view.setHorizontalHeaderLabels(headers)
    view.verticalHeader().hide()
    view.setBorderVisible(True)
    view.setBorderRadius(8)
    view.setWordWrap(False)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    view.setMinimumHeight(210)
    return view


def fill(view, rows):
    view.setRowCount(len(rows))
    for i, row in enumerate(rows):
        view.setRowHeight(i, 48)
        for j, value in enumerate(row):
            item = QTableWidgetItem(str(value))
            item.setToolTip(str(value))
            view.setItem(i, j, item)


class Page(ScrollArea):
    def __init__(self, name, title, description, parent):
        super().__init__(parent)
        self.setObjectName(name)
        self.setWidgetResizable(True)
        self.setStyleSheet(
            "QScrollArea {border: none; background: transparent;} QWidget#pageBody {background: transparent;}"
        )
        self.body = QWidget()
        self.body.setObjectName("pageBody")
        self.setWidget(self.body)
        self.layout = QVBoxLayout(self.body)
        self.layout.setContentsMargins(28, 28, 28, 24)
        self.layout.setSpacing(16)
        self.layout.addWidget(TitleLabel(title, self))
        text = BodyLabel(description, self)
        text.setWordWrap(True)
        self.layout.addWidget(text)


class Worker(QThread):
    progress = Signal(str)
    result = Signal(str, str, object)
    failed = Signal(str)
    job_started = Signal(str, str)

    def __init__(self, root, jobs):
        super().__init__()
        self.root, self.jobs = root, jobs
        self.loop = None
        self.task = None
        self.cancel_requested = False

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.task = self.loop.create_task(
                batch(
                    Store(self.root),
                    self.jobs,
                    self.result.emit,
                    self.progress.emit,
                    self.job_started.emit,
                )
            )
            if self.cancel_requested:
                self.task.cancel()
            self.loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            pass
        except Exception:
            self.failed.emit("任务未能完成，请检查本地数据目录与浏览器环境。")
        finally:
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()

    def cancel(self):
        self.cancel_requested = True
        if self.loop and self.task and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.task.cancel)


class MainWindow(FluentWindow):
    def __init__(self, store):
        super().__init__()
        self.store = store
        self.worker = None
        self.account_rows = []
        self.selected_id = None
        self.pending_account_id = None
        self.job_states = {}
        self.active_job = None
        self.job_total = 0
        self.job_done = 0
        self.login_work = False
        self.mutation_buttons = []
        self.setWindowTitle("Fluent Check-in")
        self.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "app.ico")))
        self.navigationInterface.setExpandWidth(190)
        self.navigationInterface.setMinimumExpandWidth(900)
        self.accounts_page = Page(
            "accounts",
            "账号管理",
            "添加 GitHub 账号，勾选站点，一键签到。登录状态只保存在本机。",
            self,
        )
        self.tasks_page = Page(
            "tasks",
            "签到任务",
            "按账号依次检查，分别记录每个站点的结果。需要验证时可打开官网处理。",
            self,
        )
        self.history_page = Page(
            "history",
            "执行记录",
            "最近 300 条本地记录 · 时间为北京时间 · 不记录 Cookie、令牌或授权回调地址。",
            self,
        )
        self.settings_page = Page(
            "settings",
            "设置",
            "按你的习惯调整外观与执行方式。所有配置和登录状态均保存在本机。",
            self,
        )
        self.build_accounts()
        self.build_tasks()
        self.build_history()
        self.build_settings()
        self.addSubInterface(self.accounts_page, FIF.PEOPLE, "账号管理")
        self.addSubInterface(self.tasks_page, FIF.ACCEPT, "签到任务")
        self.addSubInterface(self.history_page, FIF.HISTORY, "执行记录")
        self.addSubInterface(
            self.settings_page, FIF.SETTING, "设置", NavigationItemPosition.BOTTOM
        )
        self.apply_theme(self.store.setting("theme", "跟随系统"))
        self.refresh()
        screen = self.screen().availableGeometry()
        self.setMinimumSize(min(860, screen.width()), min(620, screen.height()))
        self.resize(min(1180, screen.width()), min(790, screen.height()))
        self.move(screen.center() - self.rect().center())
        QTimer.singleShot(0, self.expand_navigation)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.schedule_tick)
        self.timer.start(15000)

    def expand_navigation(self):
        if self.width() >= 1000:
            self.navigationInterface.expand(useAni=False)

    def button(self, text, callback, primary=False, icon=None):
        cls = PrimaryPushButton if primary else PushButton
        button = cls(text, self)
        if icon is not None:
            button.setIcon(icon)
        button.clicked.connect(callback)
        self.mutation_buttons.append(button)
        return button

    def card(self, layout):
        card = CardWidget(self)
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 18, 20, 18)
        box.setSpacing(12)
        layout.addWidget(card)
        return box

    def build_accounts(self):
        box = self.accounts_page.layout
        toolbar = QHBoxLayout()
        toolbar.addWidget(SubtitleLabel("我的 GitHub 账号", self))
        toolbar.addStretch()
        self.add_button = self.button("添加账号", self.add_account, icon=FIF.GITHUB)
        toolbar.addWidget(self.add_button)
        self.run_all_button = self.button(
            "全部账号一键签到", self.run_all_accounts, True, FIF.PLAY
        )
        toolbar.addWidget(self.run_all_button)
        box.addLayout(toolbar)
        self.account_table = table(["GitHub 账号", "AgentRouter", "JustDoWork"], self)
        self.account_table.itemSelectionChanged.connect(self.selection_changed)
        box.addWidget(self.account_table, 1)
        self.empty_hint = BodyLabel(
            "点击“添加账号”直接打开 GitHub，在官方窗口完成登录即可。", self
        )
        self.empty_hint.setWordWrap(True)
        box.addWidget(self.empty_hint)
        login_row = QHBoxLayout()
        self.login_progress = BodyLabel("", self)
        self.login_progress.setWordWrap(True)
        self.login_progress.hide()
        login_row.addWidget(self.login_progress, 1)
        self.login_cancel = PushButton("取消登录", self)
        self.login_cancel.clicked.connect(self.cancel_work)
        self.login_cancel.hide()
        login_row.addWidget(self.login_cancel)
        box.addLayout(login_row)
        details = self.card(box)
        row = QHBoxLayout()
        self.selected_label = StrongBodyLabel("选择一个账号", self)
        row.addWidget(self.selected_label, 1)
        self.github_button = self.button(
            "重新登录", lambda: self.start_selected("github"), icon=FIF.GITHUB
        )
        row.addWidget(self.github_button)
        self.remove_button = self.button(
            "移除账号", self.remove_account, icon=FIF.DELETE
        )
        row.addWidget(self.remove_button)
        details.addLayout(row)
        details.addWidget(SubtitleLabel("选择签到站点", self))
        self.site_checks = {}
        self.site_labels = {}
        for key, provider in PROVIDERS.items():
            row = QHBoxLayout()
            check = CheckBox(provider.name, self)
            check.stateChanged.connect(lambda state, k=key: self.toggle_site(k, state))
            self.site_checks[key] = check
            row.addWidget(check)
            row.addStretch()
            state = CaptionLabel("尚未连接", self)
            self.site_labels[key] = state
            row.addWidget(state)
            details.addLayout(row)
        hint = CaptionLabel(
            "按每个账号勾选的站点执行；当天已明确签到成功的站点会自动跳过。", self
        )
        hint.setWordWrap(True)
        details.addWidget(hint)
        self.checkin_button = self.button(
            "当前账号签到", self.run_current_account, True, FIF.PLAY
        )
        details.addWidget(self.checkin_button, alignment=Qt.AlignmentFlag.AlignRight)

    def build_tasks(self):
        box = self.tasks_page.layout
        bar = QHBoxLayout()
        self.account_filter = ComboBox(self)
        self.account_filter.setMinimumWidth(160)
        self.account_filter.currentIndexChanged.connect(self.refresh_tasks)
        bar.addWidget(self.account_filter)
        bar.addStretch()
        bar.addWidget(
            self.button("开始签到", self.run_selected_accounts, True, FIF.PLAY)
        )
        self.cancel_button = PushButton("取消任务", self)
        self.cancel_button.clicked.connect(self.cancel_work)
        self.cancel_button.setEnabled(False)
        bar.addWidget(self.cancel_button)
        box.addLayout(bar)
        self.progress_label = StrongBodyLabel("等待开始", self)
        self.progress_label.setWordWrap(True)
        box.addWidget(self.progress_label)
        self.progress_bar = ProgressBar(self)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        box.addWidget(self.progress_bar)
        self.task_table = table(["账号", "站点", "状态", "时间", "说明"], self)
        box.addWidget(self.task_table, 1)
        self.task_table.itemSelectionChanged.connect(self.update_task_action)
        self.task_action = self.button("打开官网处理", self.open_task, icon=FIF.LINK)
        self.task_action.setEnabled(False)
        box.addWidget(self.task_action, alignment=Qt.AlignmentFlag.AlignRight)
        tip = BodyLabel(
            "需要授权或验证时，选中对应任务并点击“打开官网处理”。“到账未确认”不会算作成功。",
            self,
        )
        tip.setWordWrap(True)
        box.addWidget(tip)

    def build_history(self):
        self.history_table = table(["时间", "账号", "站点", "结果", "说明"], self)
        self.history_page.layout.addWidget(self.history_table, 1)

    def build_settings(self):
        box = self.settings_page.layout
        settings = self.card(box)
        settings.addWidget(SubtitleLabel("外观", self))
        self.theme_combo = ComboBox(self)
        self.theme_combo.addItems(["跟随系统", "浅色", "深色"])
        self.theme_combo.setCurrentText(self.store.setting("theme", "跟随系统"))
        self.theme_combo.currentTextChanged.connect(self.apply_theme)
        settings.addWidget(self.theme_combo)
        runtime = self.card(box)
        runtime.addWidget(SubtitleLabel("浏览器", self))
        self.browser_combo = ComboBox(self)
        self.browser_combo.addItems(list(BROWSERS.values()))
        selected_browser = self.store.setting("browser", "msedge")
        self.browser_combo.setCurrentIndex(
            list(BROWSERS).index(selected_browser)
            if selected_browser in BROWSERS
            else 0
        )
        self.browser_combo.currentIndexChanged.connect(self.select_browser)
        runtime.addWidget(self.browser_combo)
        self.browser_location = CaptionLabel("", self)
        self.browser_location.setWordWrap(True)
        runtime.addWidget(self.browser_location)
        browser_buttons = QHBoxLayout()
        browser_buttons.addWidget(
            self.button("选择浏览器程序", self.choose_browser_path)
        )
        browser_buttons.addWidget(self.button("恢复自动查找", self.reset_browser_path))
        browser_buttons.addStretch()
        runtime.addLayout(browser_buttons)
        self.refresh_browser_location()
        hint = CaptionLabel(
            "支持 Edge、Chrome 和 Brave。使用独立会话；非标准安装可选择 EXE，Chromium 需另行安装。",
            self,
        )
        hint.setWordWrap(True)
        runtime.addWidget(hint)
        schedule = self.card(box)
        schedule.addWidget(SubtitleLabel("每日签到", self))
        row = QHBoxLayout()
        self.schedule_switch = SwitchButton("关闭", self)
        self.schedule_switch.setOnText("已开启")
        self.schedule_switch.setOffText("关闭")
        self.schedule_switch.setChecked(self.store.setting("schedule_enabled", False))
        self.schedule_switch.checkedChanged.connect(
            lambda value: self.store.set_setting("schedule_enabled", value)
        )
        row.addWidget(self.schedule_switch)
        row.addStretch()
        row.addWidget(BodyLabel("北京时间", self))
        self.time_edit = TimeEdit(self)
        self.time_edit.setDisplayFormat("HH:mm")
        self.time_edit.setTime(
            QTime.fromString(self.store.setting("schedule_time", "09:00"), "HH:mm")
        )
        self.time_edit.timeChanged.connect(
            lambda value: self.store.set_setting(
                "schedule_time", value.toString("HH:mm")
            )
        )
        row.addWidget(self.time_edit)
        schedule.addLayout(row)
        hint = BodyLabel(
            "仅在软件运行期间执行；当天错过时间后启动，会补执行一次。需要人工验证的账号会保留提示。",
            self,
        )
        hint.setWordWrap(True)
        schedule.addWidget(hint)
        data = self.card(box)
        data.addWidget(SubtitleLabel("本地数据", self))
        path = BodyLabel(str(self.store.root), self)
        path.setWordWrap(True)
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        data.addWidget(path)
        hint = CaptionLabel(
            "登录状态以本地 JSON 保存，不使用 DPAPI。会话文件具有登录权限，请勿分享；诊断记录不包含会话内容。",
            self,
        )
        hint.setWordWrap(True)
        data.addWidget(hint)
        button = PushButton("打开数据文件夹", self)
        button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.root)))
        )
        data.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
        box.addStretch()

    def toast(self, text, error=False):
        (InfoBar.error if error else InfoBar.info)(
            title="提示",
            content=text,
            parent=self,
            position=InfoBarPosition.TOP_RIGHT,
            duration=4500,
        )

    def add_account(self):
        if self.worker:
            return
        self.pending_account_id = self.store.add()
        self.selected_id = self.pending_account_id
        self.refresh()
        self.start_jobs([(self.selected_id, "github", None)])

    def remove_account(self):
        if not self.selected_id:
            return self.toast("请先选择账号。")
        dialog = MessageBox(
            "移除本地账号",
            "删除此账号在本机保存的登录状态和执行记录。网站账号及 GitHub 授权关系不会被删除。",
            self,
        )
        dialog.yesButton.setText("移除")
        dialog.cancelButton.setText("取消")
        if dialog.exec():
            try:
                self.store.remove(self.selected_id)
                self.selected_id = None
                self.refresh()
            except VaultError as exc:
                self.toast(str(exc), True)

    def selection_changed(self):
        row = self.account_table.currentRow()
        if 0 <= row < len(self.account_rows):
            self.selected_id = self.account_rows[row]["id"]
        self.update_selection()

    def update_selection(self):
        account = next(
            (a for a in self.account_rows if a["id"] == self.selected_id), None
        )
        self.selected_label.setText(account["label"] if account else "选择一个账号")
        for key, check in self.site_checks.items():
            check.blockSignals(True)
            row = (
                next(
                    (r for r in self.store.sites(self.selected_id) if r["site"] == key),
                    None,
                )
                if account
                else None
            )
            check.setChecked(bool(row and row["enabled"]))
            check.setEnabled(bool(account) and self.worker is None)
            check.blockSignals(False)
            self.site_labels[key].setText(
                STATUS.get(row["code"], "需检查") if row else "尚未连接"
            )
        idle = self.worker is None
        self.github_button.setEnabled(bool(account) and idle)
        self.remove_button.setEnabled(bool(account) and idle)
        self.checkin_button.setEnabled(
            bool(account and account["github_login"])
            and idle
            and any(c.isChecked() for c in self.site_checks.values())
        )
        self.run_all_button.setEnabled(idle and bool(self.run_jobs()))

    def toggle_site(self, key, state):
        if self.selected_id:
            self.store.enable(self.selected_id, key, self.site_checks[key].isChecked())
            self.refresh_tasks()
            self.update_selection()

    def refresh(self):
        self.account_rows = self.store.accounts()
        self.account_table.blockSignals(True)
        rows = []
        for account in self.account_rows:
            vault = Vault(self.store, account["id"])
            states = [vault.path(key).exists() for key in PROVIDERS]
            rows.append(
                [
                    account["github_login"] or "等待 GitHub 登录",
                    *("会话已保存" if s else "未连接" for s in states),
                ]
            )
        fill(self.account_table, rows)
        if not self.selected_id and self.account_rows:
            self.selected_id = self.account_rows[0]["id"]
        for i, account in enumerate(self.account_rows):
            if account["id"] == self.selected_id:
                self.account_table.selectRow(i)
        self.account_table.blockSignals(False)
        self.empty_hint.setVisible(not self.account_rows)
        current = self.account_filter.currentData()
        self.account_filter.blockSignals(True)
        self.account_filter.clear()
        self.account_filter.addItem("全部账号", userData=None)
        for account in self.account_rows:
            self.account_filter.addItem(account["label"], userData=account["id"])
        self.account_filter.setCurrentIndex(
            max(0, self.account_filter.findData(current))
        )
        self.account_filter.blockSignals(False)
        self.refresh_tasks()
        fill(
            self.history_table,
            [
                [
                    display_time(r["created"]),
                    r["label"],
                    PROVIDERS[r["site"]].name if r["site"] in PROVIDERS else "GitHub",
                    STATUS.get(r["code"], "需检查"),
                    r["message"],
                ]
                for r in self.store.history()
            ],
        )
        self.update_selection()

    def refresh_tasks(self):
        selected = self.account_filter.currentData()
        old_row = self.task_table.currentRow()
        old_key = (
            self.task_rows[old_row]
            if hasattr(self, "task_rows") and 0 <= old_row < len(self.task_rows)
            else None
        )
        sites = self.store.sites(selected)
        self.task_rows = [(r["account_id"], r["site"]) for r in sites]
        rows = []
        for r in sites:
            state = self.job_states.get((r["account_id"], r["site"]))
            rows.append(
                [
                    r["label"],
                    PROVIDERS[r["site"]].name,
                    state[0] if state else STATUS.get(r["code"], "需检查"),
                    "—"
                    if state and state[0] in {"等待执行", "正在执行", "已取消"}
                    else display_time(r["updated"]),
                    state[1]
                    if state
                    else r["message"]
                    if r["enabled"]
                    else "未选择此站点",
                ]
            )
        self.task_table.blockSignals(True)
        fill(
            self.task_table,
            rows,
        )
        if old_key in self.task_rows:
            self.task_table.selectRow(self.task_rows.index(old_key))
        self.task_table.blockSignals(False)
        self.update_task_action()

    def update_task_action(self):
        if hasattr(self, "task_action"):
            self.task_action.setEnabled(
                self.worker is None and self.task_table.currentRow() >= 0
            )

    def open_task(self):
        row = self.task_table.currentRow()
        if 0 <= row < len(self.task_rows):
            account, site = self.task_rows[row]
            self.start_jobs([(account, "open", site)])

    def run_current_account(self):
        if self.selected_id:
            self.account_filter.setCurrentIndex(
                max(0, self.account_filter.findData(self.selected_id))
            )
            self.start_jobs(self.run_jobs(self.selected_id))

    def run_all_accounts(self):
        self.account_filter.setCurrentIndex(0)
        self.start_jobs(self.run_jobs())

    def start_selected(self, action, site=None):
        if not self.selected_id:
            return self.toast("请先添加并选择账号。")
        if (
            action != "github"
            and not self.store.account(self.selected_id)["github_login"]
        ):
            return self.toast("请先完成此账号的 GitHub 登录。")
        self.start_jobs([(self.selected_id, action, site)])

    def run_jobs(self, selected=None):
        return [
            (r["account_id"], "run", r["site"])
            for r in self.store.sites(selected)
            if r["enabled"] and r["github_login"]
        ]

    def run_selected_accounts(self):
        self.start_jobs(self.run_jobs(self.account_filter.currentData()))

    def start_jobs(self, jobs):
        if self.worker:
            return self.toast("已有任务运行，请等待完成或取消。")
        if not jobs:
            return self.toast("没有可执行任务，请先登录 GitHub 并勾选站点。")
        self.login_work = all(action == "github" for _, action, _ in jobs)
        self.job_states = {
            (account, site): ("等待执行", "等待前面的任务完成")
            for account, _, site in jobs
            if site
        }
        self.active_job = None
        self.job_total = len(jobs)
        self.job_done = 0
        self.progress_bar.setRange(0, len(jobs))
        self.progress_bar.setValue(0)
        self.worker = Worker(self.store.root, jobs)
        self.worker.job_started.connect(self.job_started)
        self.worker.progress.connect(self.work_progress)
        self.worker.result.connect(self.job_result)
        self.worker.failed.connect(lambda text: self.toast(text, True))
        self.worker.finished.connect(self.work_finished)
        for button in self.mutation_buttons:
            button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.login_cancel.setVisible(self.login_work)
        self.login_progress.setVisible(self.login_work)
        self.login_cancel.setEnabled(True)
        self.account_filter.setEnabled(False)
        self.work_progress("正在打开 GitHub 登录…" if self.login_work else "准备签到…")
        self.update_selection()
        self.refresh_tasks()
        self.switchTo(self.accounts_page if self.login_work else self.tasks_page)
        self.worker.start()

    def job_started(self, account, site):
        self.active_job = (account, site)
        if site != "github":
            self.job_states[self.active_job] = ("正在执行", "正在检查登录和签到状态…")
            self.refresh_tasks()

    def work_progress(self, text):
        if self.login_work:
            self.login_progress.setText(text)
        else:
            self.progress_label.setText(
                f"已完成 {self.job_done}/{self.job_total} · {text}"
            )
            if self.active_job in self.job_states:
                self.job_states[self.active_job] = ("正在执行", text)
                self.refresh_tasks()

    def job_result(self, account, site, result):
        self.job_done += 1
        self.progress_bar.setValue(self.job_done)
        if site == "github":
            self.login_progress.setText(result.message)
        else:
            self.job_states[(account, site)] = (
                STATUS.get(result.code, "需检查"),
                result.message,
            )
        self.active_job = None
        self.refresh()

    def cancel_work(self):
        if self.worker:
            self.progress_label.setText("正在取消并保存最新会话…")
            self.login_progress.setText("正在取消登录…" if self.login_work else "")
            self.cancel_button.setEnabled(False)
            self.login_cancel.setEnabled(False)
            self.worker.cancel()

    def work_finished(self):
        cancelled = self.worker.cancel_requested
        self.worker.deleteLater()
        self.worker = None
        if self.pending_account_id:
            account_id = self.pending_account_id
            self.pending_account_id = None
            account = self.store.account(account_id)
            if (
                not account["github_login"]
                or not (self.store.folder(account_id) / "github.json").exists()
            ):
                self.store.remove(account_id)
                self.selected_id = None
            else:
                self.selected_id = account_id
                self.login_progress.setText(
                    f"已添加 {account['github_login']}，选择站点即可签到。"
                )
        for key, state in self.job_states.items():
            if state[0] in {"等待执行", "正在执行"}:
                self.job_states[key] = (
                    "已取消" if cancelled else "未完成",
                    "任务已结束，未确认完成",
                )
        for button in self.mutation_buttons:
            button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.login_cancel.hide()
        self.account_filter.setEnabled(True)
        self.progress_label.setText(
            f"本次任务已结束 · 已返回 {self.job_done}/{self.job_total} 项结果"
        )
        self.refresh()
        if self.login_work:
            self.switchTo(self.accounts_page)

    def select_browser(self, index):
        self.store.set_setting("browser", list(BROWSERS)[index])
        self.refresh_browser_location()

    def refresh_browser_location(self):
        name = list(BROWSERS)[self.browser_combo.currentIndex()]
        custom = self.store.setting("browser_paths", {}).get(name, "")
        try:
            path = browser_path(name, custom)
            self.browser_location.setText(
                str(path) if path else "由 Playwright 查找已安装的 Chromium。"
            )
        except BrowserUnavailable as exc:
            self.browser_location.setText(str(exc))

    def choose_browser_path(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择浏览器程序", "", "浏览器程序 (*.exe)"
        )
        if path:
            name = list(BROWSERS)[self.browser_combo.currentIndex()]
            paths = self.store.setting("browser_paths", {})
            paths[name] = path
            self.store.set_setting("browser_paths", paths)
            self.refresh_browser_location()

    def reset_browser_path(self):
        name = list(BROWSERS)[self.browser_combo.currentIndex()]
        paths = self.store.setting("browser_paths", {})
        paths.pop(name, None)
        self.store.set_setting("browser_paths", paths)
        self.refresh_browser_location()

    def schedule_tick(self):
        if self.worker or not self.store.setting("schedule_enabled", False):
            return
        now = datetime.now(timezone(timedelta(hours=8)))
        date = now.date().isoformat()
        if (
            now.strftime("%H:%M") < self.store.setting("schedule_time", "09:00")
            or self.store.setting("schedule_last_date") == date
        ):
            return
        jobs = self.run_jobs()
        if jobs:
            self.store.set_setting("schedule_last_date", date)
            self.start_jobs(jobs)

    def apply_theme(self, name):
        setTheme(
            {"跟随系统": Theme.AUTO, "浅色": Theme.LIGHT, "深色": Theme.DARK}[name]
        )
        self.store.set_setting("theme", name)

    def closeEvent(self, event):
        if self.worker:
            event.ignore()
            self.toast("请先取消或等待当前任务结束，再关闭软件。")
            return
        super().closeEvent(event)


def launch(root=None):
    from filelock import FileLock, Timeout

    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "FluentCheckin.Desktop"
        )
    app = QApplication.instance() or QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "app.ico")))
    app.setApplicationName("Fluent Check-in")
    store = Store(root)
    lock = FileLock(store.root / "desktop.lock", timeout=0)
    try:
        lock.acquire()
    except Timeout:
        print("Fluent Check-in 已在运行。")
        return 2
    try:
        window = MainWindow(store)
        window.show()
        return app.exec()
    finally:
        lock.release()
