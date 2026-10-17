import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .core import (
    Result,
    agent_result,
    agent_receipt_confirmed,
    checkin_status,
    response_problem,
    user_id,
)


# No redirects: never forward an Authorization header to an unexpected origin.
FETCH = """async ({path, method, headers}) => {
  try {
    const r = await fetch(path, {method, headers, credentials: 'same-origin',
      redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(25000)});
    const type = r.headers.get('content-type') || '';
    const retry_after = r.headers.get('retry-after');
    if (r.status === 403 || r.status === 429) return {status: r.status, retry_after};
    if (!type.toLowerCase().includes('application/json'))
      return {status: r.status, retry_after, html: type.toLowerCase().includes('text/html')};
    return {status: r.status, retry_after, body: await r.json()};
  } catch { return {status: 0}; }
}"""


async def api(page, path, method="GET", headers=None):
    if not path.startswith("/api/") or "//" in path or "\\" in path:
        raise ValueError("Invalid API path")
    return await page.evaluate(
        FETCH, {"path": path, "method": method, "headers": headers or {}}
    )


async def blank_site_page(context, provider):
    # Local response at the site origin: avoid loading its SPA, analytics and
    # automatic refresh concurrently with our single controlled refresh request.
    url = provider.origin + "/__local_checkin__"
    await context.route(
        url,
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="<!doctype html><title>Local check-in</title>",
        ),
    )
    page = await context.new_page()
    await page.goto(url)
    return page


async def agent_confirmation(page, uid, headers, body):
    explicit = agent_result(body)
    if explicit.code == "checked_in":
        return explicit
    now = datetime.now(timezone.utc)
    start = now.astimezone(timezone(timedelta(hours=8))).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # No trailing slash: this route redirects when a slash is appended.
    receipt = await api(
        page,
        f"/api/log/self?p=0&page_size=100&type=4&start_timestamp={int(start.timestamp())}&end_timestamp={int(now.timestamp())}",
        headers=headers,
    )
    if problem := response_problem(receipt):
        # Even an expired receipt query must not trigger a new reward attempt.
        if problem.code == "login_required":
            return Result(
                "request_failed", "到账查询会话失效，本次停止，请在官网核实。"
            )
        return problem
    if not isinstance(receipt["body"].get("data"), dict) or not isinstance(
        receipt["body"]["data"].get("items"), list
    ):
        return Result("schema_changed", "到账日志格式变化，本次不重新登录。")
    if agent_receipt_confirmed(receipt.get("body"), uid, now):
        return Result("already_checked_in", "站点签到成功日志确认今天已到账。")
    return explicit


async def run_site(browser, provider, vault):
    saved = vault.load(provider.key)
    if saved is None:
        return Result("login_required", "尚未保存会话，请先授权登录。")
    context = await browser.new_context(service_workers="block")
    try:
        await context.add_cookies(saved["cookies"])
        page = await blank_site_page(context, provider)
        headers = {"new-api-user": str(saved["user_id"])}
        if provider.refresh:
            response = await api(page, "/api/user/auth/refresh", "POST")
            # A rotated HttpOnly cookie must survive even if subsequent work fails.
            vault.save(provider.key, saved["user_id"], await context.cookies())
            if problem := response_problem(response):
                return problem
            bundle = response["body"].get("data", {})
            if not isinstance(bundle, dict):
                return Result("schema_changed", "登录接口格式变化，请重新检查适配。")
            token = bundle.get("access_token")
            who = bundle.get("user", {})
            if (
                not isinstance(who, dict)
                or user_id(who.get("id")) != saved["user_id"]
                or not isinstance(token, str)
                or not token
                or bundle.get("token_type") != "Bearer"
            ):
                return Result("identity_mismatch", "会话身份或令牌格式不匹配，已停止。")
            headers = {"Authorization": "Bearer " + token}
        response = await api(page, "/api/user/self", headers=headers)
        if problem := response_problem(response):
            return problem
        info = response["body"].get("data")
        if not isinstance(info, dict) or user_id(info.get("id")) != saved["user_id"]:
            return Result("identity_mismatch", "服务端返回的用户身份不匹配，已停止。")
        if not provider.refresh:
            return await agent_confirmation(
                page, saved["user_id"], headers, response["body"]
            )

        status = await api(page, "/api/user/checkin", headers=headers)
        if problem := response_problem(status):
            return problem
        checked = checkin_status(status["body"])
        if checked is True:
            return Result("already_checked_in", "服务端确认今天已签到。")
        if checked is None:
            return Result(
                "schema_changed", "签到未启用或状态格式变化，未发送签到请求。"
            )
        settings = await api(page, "/api/status")
        if problem := response_problem(settings):
            return problem
        data = settings["body"].get("data", {})
        if not isinstance(data, dict) or not isinstance(
            data.get("turnstile_check"), bool
        ):
            return Result("schema_changed", "无法确认站点验证配置，未发送签到请求。")
        if data["turnstile_check"]:
            return Result(
                "verification_required",
                "官网启用了 Turnstile，请通过“打开官网处理”完成验证及签到。",
            )
        submitted = await api(page, "/api/user/checkin", "POST", headers)
        problem = response_problem(submitted)
        if problem and problem.code in {"verification_required", "rate_limited"}:
            return problem
        # POST is never blindly retried, including timeouts/unknown outcome.
        verified = await api(page, "/api/user/checkin", headers=headers)
        if problem := response_problem(verified):
            return problem
        if checkin_status(verified["body"]) is True:
            return Result("checked_in", "服务端确认今天已签到。")
        if problem := response_problem(submitted):
            return problem
        return Result(
            "unconfirmed", "签到请求已提交，但未能确认最终状态，请在官网核实。"
        )
    finally:
        # Save refreshed cookies without retaining access tokens or page storage.
        try:
            vault.save(provider.key, saved["user_id"], await context.cookies())
        finally:
            await context.close()


class Observer:
    """Observe only exact-site auth responses; never inspect GitHub credentials."""

    def __init__(self, provider, allow_recovery=False):
        self.provider = provider
        self.allow_recovery = allow_recovery
        self.uid = None
        self.token = None
        self.checked = False
        self.auth_revision = 0
        self.tasks = set()
        self.problem = None

    def on_response(self, response):
        url = urlsplit(response.url)
        if f"{url.scheme}://{url.netloc}" != self.provider.origin:
            return
        if (
            url.path
            not in {
                "/api/user/self",
                "/api/user/auth/refresh",
                "/api/oauth/github",
                "/api/user/checkin",
                "/api/oauth/state",
                "/api/status",
            }
            and response.request.resource_type != "document"
        ):
            return
        task = asyncio.create_task(self.consume(response, url.path))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def consume(self, response, path):
        try:
            if response.status in {403, 429}:
                problem = response_problem(
                    {
                        "status": response.status,
                        "retry_after": await response.header_value("retry-after"),
                    }
                )
                if self.problem is None or problem.code == "rate_limited":
                    self.problem = problem
                return
            if response.status != 200:
                return
            if response.request.resource_type == "document":
                if (
                    self.allow_recovery
                    and self.problem
                    and self.problem.code == "verification_required"
                ):
                    self.problem = None
                return
            body = await response.json()
            if not isinstance(body, dict) or body.get("success") is not True:
                return
            if (
                self.allow_recovery
                and self.problem
                and self.problem.code == "verification_required"
            ):
                self.problem = None
            data = body.get("data")
            if not isinstance(data, dict):
                return
            if path == "/api/user/checkin":
                if response.request.method == "GET" and checkin_status(body) is True:
                    self.checked = True
                if response.request.method == "POST" and isinstance(
                    data.get("checkin_date"), str
                ):
                    self.checked = True
                return
            who = data.get("user", data)
            if isinstance(who, dict) and (uid := user_id(who.get("id"))):
                self.uid = uid
                self.auth_revision += 1
            token = data.get("access_token")
            if isinstance(token, str) and token and data.get("token_type") == "Bearer":
                self.token = token
            if data.get("checked_in") is True or (
                isinstance(who, dict) and who.get("checked_in") is True
            ):
                self.checked = True
        except Exception:
            # Never print response bodies, OAuth callback URLs, or exception text.
            return


async def dismiss_site_notice(page, provider):
    """Only close the known informational modal, never consent or verification."""
    u = urlsplit(page.url)
    if provider.key != "justdowork" or f"{u.scheme}://{u.netloc}" != provider.origin:
        return False
    notice = page.get_by_role(
        "dialog", name=re.compile(r"^(公告|站点公告|Notice|Announcement)$", re.I)
    )
    if await notice.count() != 1 or not await notice.is_visible():
        return False
    close = notice.get_by_role("button", name=re.compile(r"^(关闭|Close)$", re.I))
    if await close.count() and await close.first.is_enabled():
        await close.first.click(timeout=3000)
        return True
    return False


async def interactive_site(
    browser,
    provider,
    vault,
    login=False,
    timeout=300,
    background=False,
    notify=lambda text: None,
    use_github=False,
    force_oauth=False,
):
    saved = vault.load(provider.key)
    state = (
        vault.store.load_github(vault.account_id)
        if use_github
        else {"cookies": [], "origins": []}
    )
    context = await browser.new_context(service_workers="block", storage_state=state)
    observer = Observer(provider, allow_recovery=not background)
    context.on("response", observer.on_response)
    confirmed_uid = None
    github_login = None
    try:
        if saved and not force_oauth:
            await context.add_cookies(saved["cookies"])
            if not provider.refresh:
                # Only the ID needed by the legacy frontend, never its full profile.
                await context.add_init_script(
                    "if (location.origin === " + repr(provider.origin) + ") "
                    "localStorage.setItem('user', JSON.stringify({id: "
                    + str(saved["user_id"])
                    + "}));"
                )
        for host in (
            "www.googletagmanager.com",
            "www.google-analytics.com",
            "cloud.umami.is",
        ):
            await context.route("https://" + host + "/**", lambda route: route.abort())
        page = await context.new_page()
        await page.goto(
            provider.origin
            + (
                provider.login
                if login or force_oauth or not saved
                else provider.profile
            ),
            wait_until="domcontentloaded",
            timeout=45000,
        )
        notify(
            "正在后台连接；如需首次授权或验证，会提示你打开官网处理。"
            if background
            else "请在官方窗口完成授权或验证；首次授权由你确认，完成后自动保存。"
        )
        deadline = time.monotonic() + timeout
        last_candidate = None
        saved_revision = -1
        oauth_started = False
        stage = "等待网站登录页加载"
        while time.monotonic() < deadline:
            if observer.problem:
                if background or observer.problem.code == "rate_limited":
                    return observer.problem
                # Keep a user-opened window available for manual verification.
                # No more helper API calls until the website itself recovers.
                stage = "等待在官网完成验证"
                await asyncio.sleep(1)
                continue
            pages = [p for p in context.pages if not p.is_closed()]
            if not pages:
                return Result("cancelled", "窗口已关闭。")
            if observer.uid and (not saved or observer.uid == saved["user_id"]):
                if observer.auth_revision != saved_revision:
                    vault.save(provider.key, observer.uid, await context.cookies())
                    saved_revision = observer.auth_revision
            for active in pages:
                u = urlsplit(active.url)
                if u.hostname == "github.com":
                    stage = "等待 GitHub 授权返回"
                    from .github import github_identity

                    current = await github_identity(active)
                    if (
                        current
                        and github_login
                        and current.lower() != github_login.lower()
                    ):
                        github_login = None
                        return Result(
                            "identity_mismatch", "GitHub 账号发生变化，已停止连接。"
                        )
                    if background and (
                        u.path.startswith("/login") or u.path.startswith("/sessions")
                    ):
                        return Result(
                            "authorization_required",
                            "需要确认 GitHub 授权或重新登录，请点击“打开官网处理”。",
                        )
                    continue
                if f"{u.scheme}://{u.netloc}" != provider.origin:
                    continue
                if background:
                    challenge = active.locator(
                        '#challenge-running, #challenge-stage, iframe[src*="challenges.cloudflare.com"]'
                    )
                    if await challenge.count() and await challenge.first.is_visible():
                        return Result(
                            "verification_required",
                            "官网需要验证，请通过“打开官网处理”完成。",
                        )
                if u.path.startswith("/oauth"):
                    stage = "等待网站完成 OAuth 回调"
                    continue
                if await dismiss_site_notice(active, provider):
                    notify("已关闭站点公告，继续连接 GitHub。")
                if (
                    use_github
                    and not oauth_started
                    and u.path.rstrip("/") in {"/login", "/sign-in"}
                ):
                    stage = "等待网站 GitHub 登录入口"
                    if not github_login:
                        from .github import validate_github

                        github_login = await validate_github(
                            context, vault.store, vault.account_id
                        )
                        if not github_login:
                            return Result(
                                "github_required",
                                "GitHub 登录已失效，请在账号管理点击“重新登录”。",
                            )
                    button = active.get_by_role(
                        "button", name=re.compile("GitHub", re.I)
                    )
                    link = active.get_by_role("link", name=re.compile("GitHub", re.I))
                    for target in (button, link):
                        if (
                            await target.count()
                            and await target.first.is_visible()
                            and await target.first.is_enabled()
                        ):
                            await target.first.click(timeout=5000)
                            oauth_started = True
                            stage = "已点击 GitHub 登录，等待跳转"
                            break
                    if oauth_started:
                        continue
                uid = observer.uid
                if not uid and not provider.refresh:
                    uid = user_id(
                        await active.evaluate(
                            "() => {try { return JSON.parse(localStorage.getItem('user') || '{}').id; } catch { return null; }}"
                        )
                    )
                if not uid or (provider.refresh and not observer.token):
                    continue
                stage = "验证网站登录身份"
                headers = (
                    {"Authorization": "Bearer " + observer.token}
                    if provider.refresh
                    else {"new-api-user": str(uid)}
                )
                if saved and uid != saved["user_id"]:
                    return Result(
                        "identity_mismatch",
                        "网站账号与已连接身份不同，请使用正确的独立账号记录。",
                    )
                candidate = (uid, observer.token)
                if candidate != last_candidate:
                    last_candidate = candidate
                    response = await api(active, "/api/user/self", headers=headers)
                    if problem := response_problem(response):
                        if not background and problem.code == "verification_required":
                            observer.problem = problem
                            last_candidate = None
                            break
                        return problem
                    info = (response.get("body") or {}).get("data")
                    if not isinstance(info, dict) or user_id(info.get("id")) != uid:
                        confirmed_uid = None
                        continue
                    confirmed_uid = uid
                    vault.save(provider.key, uid, await context.cookies())
                if confirmed_uid != uid:
                    continue
                if login:
                    return Result(
                        "saved",
                        "站点会话已保存在本机。"
                        + (" 服务端同时确认本次签到成功。" if observer.checked else ""),
                    )
                if not provider.refresh:
                    return (
                        Result("checked_in", "服务端确认签到成功。")
                        if observer.checked
                        else await agent_confirmation(
                            active, uid, headers, response["body"]
                        )
                    )
                if u.path.rstrip("/") != provider.profile:
                    await active.goto(
                        provider.origin + provider.profile,
                        wait_until="domcontentloaded",
                    )
                    continue
                if background and not observer.checked:
                    return Result("saved", "网站登录已恢复，可以继续检查签到。")
                # Observe the site's own check-in requests; no background refresh or CAPTCHA automation.
                stage = "等待在官网完成验证及签到"
                if observer.checked:
                    verified = await api(active, "/api/user/checkin", headers=headers)
                    if problem := response_problem(verified):
                        if not background and problem.code == "verification_required":
                            observer.problem = problem
                            break
                        return problem
                    if checkin_status(verified["body"]) is True:
                        return Result(
                            "checked_in", "服务端确认今天已签到，会话已更新。"
                        )
            await asyncio.sleep(1)
        return Result("timeout", f"{stage}超时；请点击该站点的“打开官网处理”。")
    finally:
        try:
            final_uid = confirmed_uid or observer.uid
            if final_uid and (not saved or final_uid == saved["user_id"]):
                vault.save(provider.key, final_uid, await context.cookies())
            if github_login:
                vault.store.save_github(
                    vault.account_id, github_login, await context.storage_state()
                )
        finally:
            for task in tuple(observer.tasks):
                task.cancel()
            if observer.tasks:
                await asyncio.gather(*observer.tasks, return_exceptions=True)
            await context.close()
