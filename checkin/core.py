from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
import math
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    origin: str
    login: str
    profile: str
    refresh: bool


PROVIDERS = {
    "agentrouter": Provider(
        "agentrouter",
        "AgentRouter",
        "https://agentrouter.org",
        "/login",
        "/console/personal",
        False,
    ),
    "justdowork": Provider(
        "justdowork",
        "JustDoWork",
        "https://api.justwoker.icu",
        "/sign-in",
        "/profile",
        True,
    ),
}


@dataclass
class Result:
    code: str
    message: str
    retry_after: int | None = None

    @property
    def exit_code(self):
        return (
            0
            if self.code
            in {"checked_in", "already_checked_in", "skipped_today", "saved"}
            else 2
        )


def user_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if (
        isinstance(value, str)
        and value.isascii()
        and value.isdigit()
        and int(value) > 0
    ):
        return int(value)
    return None


def site_cookies(cookies, provider):
    """Export only target-site cookies, narrowing domain cookies to this host."""
    host = urlsplit(provider.origin).hostname
    result = []
    for cookie in cookies:
        domain = cookie.get("domain", "").lower()
        if domain not in {host, "." + host}:
            # Accept the one known parent domain only, never public suffixes.
            if not (provider.key == "justdowork" and domain == ".justwoker.icu"):
                continue
        copy = {
            k: v
            for k, v in cookie.items()
            if k
            in {
                "name",
                "value",
                "domain",
                "path",
                "expires",
                "httpOnly",
                "secure",
                "sameSite",
            }
        }
        copy["domain"] = host
        copy["secure"] = True
        result.append(copy)
    return result


def retry_after_seconds(value, now=None):
    """Parse only Retry-After, never retain raw response headers."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return int(value) if len(value) <= 12 else None
    try:
        deadline = parsedate_to_datetime(value)
        if deadline.tzinfo is None:
            return None
        return max(
            0,
            math.ceil((deadline - (now or datetime.now(timezone.utc))).total_seconds()),
        )
    except (TypeError, ValueError, OverflowError):
        return None


def response_problem(response):
    status = response.get("status", 0)
    body = response.get("body")
    if status == 401:
        return Result("login_required", "登录会话已失效，请重新授权登录。")
    if status == 429:
        return Result(
            "rate_limited",
            "站点请求限流，已停止并进入冷却。",
            retry_after_seconds(response.get("retry_after")),
        )
    if status == 403 or response.get("html"):
        return Result(
            "verification_required", "站点验证或访问限制，请打开官网处理后再试。"
        )
    if status == 409:
        return Result(
            "session_conflict", "会话已轮换或存在冲突，请重新登录；本次不重复刷新。"
        )
    if not 200 <= status < 300 or not isinstance(body, dict):
        return Result("request_failed", "请求失败或返回格式不匹配，本次不自动重试。")
    if body.get("success") is not True:
        message = body.get("message", "")
        if isinstance(message, str) and any(
            text in message.lower()
            for text in (
                "未登录",
                "登录已过期",
                "登录已失效",
                "not logged in",
                "session expired",
                "unauthorized",
            )
        ):
            return Result("login_required", "登录会话已失效，请重新授权登录。")
        # Never relay raw server messages: they may contain credential material.
        return Result("site_rejected", "站点未接受请求，请打开官网查看具体提示。")
    return None


def checkin_status(body):
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or data.get("enabled") is not True:
        return None
    stats = data.get("stats")
    if isinstance(stats, dict) and isinstance(stats.get("checked_in_today"), bool):
        return stats["checked_in_today"]
    return None


def agent_result(body):
    data = body.get("data", {})
    if isinstance(data, dict) and data.get("checked_in") is True:
        return Result("checked_in", "服务端确认本次签到成功。")
    return Result(
        "triggered_unconfirmed",
        "尚未获得今日签到标记或签到成功日志，请在官网核实。",
    )


def agent_receipt_confirmed(body, uid, now):
    """AgentRouter type-4 system receipt, observed on its live official API."""
    if not isinstance(body, dict) or body.get("success") is not True:
        return False
    data = body.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return False
    zone = timezone(timedelta(hours=8))
    for item in data["items"]:
        if (
            not isinstance(item, dict)
            or item.get("type") != 4
            or user_id(item.get("user_id")) != uid
        ):
            continue
        content = item.get("content")
        stamp = item.get("created_at")
        if not isinstance(content, str) or type(stamp) is not int:
            continue
        receipt = re.fullmatch(
            r"每日签到成功，增加额度 [＄$]([0-9]{1,12}(?:\.[0-9]{1,8})?) 额度", content
        )
        if not receipt or Decimal(receipt[1]) <= 0:
            continue
        try:
            recorded = datetime.fromtimestamp(stamp, timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue
        if (
            recorded <= now
            and recorded.astimezone(zone).date() == now.astimezone(zone).date()
        ):
            return True
    return False
