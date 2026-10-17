"""Render synthetic previews in an isolated directory; no accounts or network."""

import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from pathlib import Path
import tempfile
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase, QFont
from PySide6.QtTest import QTest
from checkin.ui import MainWindow
from checkin.vault import Store, Vault
from checkin.core import Result

app = QApplication([])
for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "segoeuib.ttf"):
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/" + name)
app.setFont(QFont("Microsoft YaHei", 10))
target = Path("artifacts/ui")
target.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory() as root:
    store = Store(root)
    first = store.add("个人账号 · 预览数据")
    second = store.add("工作账号 · 预览数据")
    store.bind_github(first, "preview-personal")
    store.bind_github(second, "preview-work")
    Vault(store, first).save("agentrouter", 42, [])
    Vault(store, first).save("justdowork", 42, [])
    store.record(
        first,
        "agentrouter",
        Result("triggered_unconfirmed", "已触发，服务端未返回到账标记。"),
    )
    store.record(
        first, "justdowork", Result("verification_required", "请打开官网完成验证。")
    )
    w = MainWindow(store)
    w.setMicaEffectEnabled(False)
    w.setMinimumSize(860, 620)
    for theme in ("浅色", "深色"):
        w.apply_theme(theme)
        w.resize(1180, 790)
        w.show()
        app.processEvents()
        for page in (w.accounts_page, w.tasks_page, w.history_page, w.settings_page):
            w.switchTo(page)
            QTest.qWait(500)
            w.grab().save(
                str(
                    target
                    / f"{page.objectName()}-{'dark' if theme == '深色' else 'light'}.png"
                )
            )
        w.resize(860, 620)
        w.switchTo(w.accounts_page)
        QTest.qWait(500)
        w.grab().save(
            str(target / f"accounts-small-{'dark' if theme == '深色' else 'light'}.png")
        )
    w.close()
print("Synthetic previews saved to", target.resolve())
