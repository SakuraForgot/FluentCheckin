"""UI-independent workflows. One account lock spans OAuth and refreshed sessions."""

import asyncio
import math
import os
import random
import time
from .core import PROVIDERS, Result
from .vault import Vault, VaultError
from .runtime import BROWSERS, BrowserUnavailable, launch_options


async def perform(store, account_id, action, site=None, notify=lambda text: None):
    if action == "github":
        return await _perform(store, account_id, action, site, notify)
    # Successful daily receipts bypass both the browser and any waiting period.
    if action == "run" and (confirmed := store.confirmed_today(account_id, site)):
        return Result(
            "skipped_today",
            f"今天 {confirmed:%H:%M} 已获服务端签到确认，本次跳过（北京时间）。",
        )
    # Cover UI, CLI and simultaneous processes sharing the same local store.
    with store.site_lock(site):
        key = "site_control_" + site
        state = store.setting(key, {})
        remaining = state.get("retry_until", 0) - time.time()
        if remaining > 0:
            return Result(
                "cooldown",
                f"该站点限流冷却中，至少还需 {math.ceil(remaining)} 秒；本次未发送请求。",
            )
        if state.get("verification") and action != "open":
            return Result(
                "site_paused",
                "该站点因验证或访问限制暂停，请通过“打开官网处理”确认签到后再继续。",
            )
        # Each site keeps its own interval, including manual processing.
        await wait_interval(
            state.get("next_run", 0) - time.time(), notify, PROVIDERS[site].name
        )
        result = None
        try:
            result = await _perform(store, account_id, action, site, notify)
            if result.code == "rate_limited":
                seconds = (
                    max(1, result.retry_after)
                    if result.retry_after is not None
                    else 900
                )
                state["retry_until"] = max(
                    state.get("retry_until", 0), time.time() + seconds
                )
                result.message = (
                    f"站点限流，本轮该站点后续任务已停止，至少冷却 {seconds} 秒。"
                )
            elif result.code == "verification_required":
                state["verification"] = True
            elif action == "open" and result.code in {
                "checked_in",
                "already_checked_in",
            }:
                state["verification"] = False
            return result
        finally:
            # Persist after failures/cancellation too; clicking again or restarting
            # must not remove the minimum gap between real attempts.
            if result is None or result.code != "skipped_today":
                interval = 60 if site == "justdowork" else random.randint(30, 60)
                state["next_run"] = time.time() + interval
            store.set_setting(key, state)


async def wait_interval(seconds, notify, site_name):
    deadline = time.monotonic() + max(0, seconds)
    while (remaining := deadline - time.monotonic()) > 0:
        notify(f"{site_name} · 账号间隔等待 {math.ceil(remaining)} 秒，可取消任务。")
        await asyncio.sleep(min(1, remaining))


async def _perform(store, account_id, action, site=None, notify=lambda text: None):
    from playwright.async_api import Error as PlaywrightError, async_playwright
    from .browser import interactive_site, run_site
    from .github import login_github

    os.environ.pop("DEBUG", None)
    os.environ.pop("PWDEBUG", None)
    with store.lock(account_id):
        if action == "run" and (confirmed := store.confirmed_today(account_id, site)):
            return Result(
                "skipped_today",
                f"今天 {confirmed:%H:%M} 已获服务端签到确认，本次跳过（北京时间）。",
            )
        async with async_playwright() as pw:
            browser_name = store.setting("browser", "msedge")
            try:
                options = launch_options(store)
                notify(f"正在启动 {BROWSERS[browser_name]}…")
                browser = await pw.chromium.launch(headless=action == "run", **options)
            except BrowserUnavailable as exc:
                return Result("browser_error", str(exc))
            except PlaywrightError:
                return Result(
                    "browser_error",
                    "浏览器启动失败，请检查安装、设置中的程序路径及系统策略。",
                )
            try:
                vault = Vault(store, account_id)
                if action == "github":
                    return await login_github(browser, store, account_id, notify)
                provider = PROVIDERS[site]
                if action in {"connect", "open"}:
                    return await interactive_site(
                        browser,
                        provider,
                        vault,
                        login=action == "connect",
                        notify=notify,
                        use_github=True,
                    )
                result = await run_site(browser, provider, vault)
                if (
                    provider.key == "agentrouter"
                    and result.code in {"triggered_unconfirmed", "login_required"}
                    and store.account(account_id)["github_login"]
                ):
                    notify(
                        "未找到今日签到确认，使用 GitHub 重新登录 AgentRouter 触发签到。"
                    )
                    return await interactive_site(
                        browser,
                        provider,
                        vault,
                        timeout=60,
                        background=True,
                        notify=notify,
                        use_github=True,
                        force_oauth=True,
                    )
                if (
                    result.code == "login_required"
                    and store.account(account_id)["github_login"]
                ):
                    notify(
                        "尝试使用保存的 GitHub 状态"
                        + ("恢复网站登录。" if vault.load(site) else "连接网站。")
                    )
                    recovered = await interactive_site(
                        browser,
                        provider,
                        vault,
                        login=True,
                        timeout=35,
                        background=True,
                        notify=notify,
                        use_github=True,
                    )
                    if recovered.code == "saved":
                        return await run_site(browser, provider, vault)
                    return recovered
                return result
            finally:
                await browser.close()


async def batch(
    store,
    jobs,
    emit=lambda *args: None,
    notify=lambda text: None,
    started=lambda *args: None,
):
    stopped = set()
    for account_id, action, site in jobs:
        started(account_id, site or "github")
        notify(
            f"{store.account(account_id)['label']} · "
            + (PROVIDERS[site].name if site else "GitHub")
        )
        try:
            if (
                action == "run"
                and site in stopped
                and not store.confirmed_today(account_id, site)
            ):
                result = Result(
                    "site_paused", "本轮该站点已因限流或验证停止，未处理此账号。"
                )
            else:
                result = await perform(store, account_id, action, site, notify)
            if result.code in {
                "rate_limited",
                "cooldown",
                "verification_required",
                "site_paused",
            }:
                stopped.add(site)
        except VaultError as exc:
            result = Result("storage_error", str(exc))
        except asyncio.CancelledError:
            result = Result("cancelled", "用户已取消。")
            store.record(account_id, site or "github", result)
            emit(account_id, site or "github", result)
            raise
        except Exception:
            result = Result(
                "request_failed", "浏览器或网络操作失败，请检查浏览器安装和网络后重试。"
            )
        store.record(account_id, site or "github", result)
        emit(account_id, site or "github", result)
