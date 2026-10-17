"""Interactive GitHub login. No password/OTP collection or PAT creation."""

import asyncio
import re
import time
from urllib.parse import urlsplit
from .core import Result
from .vault import VaultError


async def github_identity(page):
    if (
        urlsplit(page.url).hostname != "github.com"
        or urlsplit(page.url).scheme != "https"
    ):
        return None
    try:
        login = await page.evaluate(
            "() => document.querySelector('meta[name=\"user-login\"]')?.content || null"
        )
    except Exception:
        return None  # Navigation may replace the document while OAuth redirects.
    if login and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login):
        return login
    return None


async def validate_github(context, store, account_id):
    """Verify restored identity on an authenticated GitHub page before OAuth."""
    page = await context.new_page()
    try:
        await page.goto(
            "https://github.com/settings/profile",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        login = await github_identity(page)
        if not login or urlsplit(page.url).path != "/settings/profile":
            return None
        expected = store.account(account_id)["github_login"]
        if not expected or login.lower() != expected.lower():
            raise VaultError("GitHub 会话身份不匹配，已停止恢复。")
        store.save_github(account_id, login, await context.storage_state())
        return login
    finally:
        await page.close()


async def login_github(
    browser, store, account_id, notify=lambda text: None, timeout=300
):
    context = await browser.new_context(
        storage_state=store.load_github(account_id), service_workers="block"
    )
    try:
        page = await context.new_page()
        await page.goto(
            "https://github.com/login", wait_until="domcontentloaded", timeout=45000
        )
        notify("请在 GitHub 官方窗口登录并完成二次验证，完成后会自动保存。")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if page.is_closed():
                return Result("cancelled", "登录窗口已关闭。")
            login = await github_identity(page)
            if login:
                # Do not rely only on a cookie name or arbitrary localStorage value.
                await page.goto(
                    "https://github.com/settings/profile", wait_until="domcontentloaded"
                )
                confirmed = await github_identity(page)
                if (
                    confirmed == login
                    and urlsplit(page.url).path == "/settings/profile"
                ):
                    store.save_github(account_id, login, await context.storage_state())
                    return Result("saved", "GitHub 登录状态已保存在本机。")
            await asyncio.sleep(1)
        return Result("timeout", "GitHub 登录等待超时，请重新打开登录窗口。")
    finally:
        await context.close()
