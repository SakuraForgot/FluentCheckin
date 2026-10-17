"""Per-account local JSON sessions and SQLite metadata. No DPAPI."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import uuid
from filelock import FileLock, Timeout
from .core import PROVIDERS, site_cookies, user_id


class VaultError(Exception):
    pass


def default_root():
    # Frozen onefile apps extract modules to a temporary directory; user data
    # must follow the actual executable, never __file__, _MEIPASS or cwd.
    base = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent.parent
    )
    return base / "data"


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="state-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def github_state(state):
    cookies = []
    for cookie in state.get("cookies", []):
        if cookie.get("domain") in {"github.com", ".github.com"}:
            cookies.append(
                {
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
            )
    return {
        "cookies": cookies,
        "origins": [
            o
            for o in state.get("origins", [])
            if o.get("origin") == "https://github.com"
        ],
    }


class Store:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else default_root()
        self.root.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, label TEXT NOT NULL,
                    github_login TEXT UNIQUE COLLATE NOCASE, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sites (account_id TEXT NOT NULL, site TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, code TEXT NOT NULL DEFAULT 'not_connected',
                    message TEXT NOT NULL DEFAULT '尚未连接', updated TEXT, PRIMARY KEY(account_id,site));
                CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, account_id TEXT NOT NULL,
                    label TEXT NOT NULL, site TEXT NOT NULL, code TEXT NOT NULL, message TEXT NOT NULL, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            # Keep legacy schema compatible, but names now come only from GitHub.
            db.execute(
                "UPDATE accounts SET label=github_login WHERE github_login IS NOT NULL AND label<>github_login"
            )
            db.execute(
                "UPDATE history SET label=(SELECT a.github_login FROM accounts a WHERE a.id=history.account_id) WHERE account_id IN (SELECT id FROM accounts WHERE github_login IS NOT NULL) AND label<>(SELECT a.github_login FROM accounts a WHERE a.id=history.account_id)"
            )

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / "app.sqlite3", timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def accounts(self):
        with self.db() as db:
            return [
                dict(r)
                for r in db.execute("SELECT * FROM accounts ORDER BY created,rowid")
            ]

    def account(self, account_id):
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
        if row is None:
            raise VaultError("账号不存在。")
        return dict(row)

    def add(self, label="正在登录 GitHub…"):
        label = label.strip()[:80]
        if not label:
            raise VaultError("账号名称不能为空。")
        key = uuid.uuid4().hex
        with self.db() as db:
            db.execute(
                "INSERT INTO accounts VALUES (?,?,NULL,?)", (key, label, self.now())
            )
            db.executemany(
                "INSERT INTO sites(account_id,site) VALUES (?,?)",
                [(key, s) for s in PROVIDERS],
            )
        return key

    def bind_github(self, account_id, login):
        old = self.account(account_id)["github_login"]
        if old and old.lower() != login.lower():
            raise VaultError("登录的 GitHub 账号与此记录不一致，请添加独立账号。")
        try:
            with self.db() as db:
                db.execute(
                    "UPDATE accounts SET github_login=?,label=? WHERE id=?",
                    (login, login, account_id),
                )
                db.execute(
                    "UPDATE history SET label=? WHERE account_id=?", (login, account_id)
                )
        except sqlite3.IntegrityError as exc:
            raise VaultError("此 GitHub 账号已添加，请使用原账号记录。") from exc

    def folder(self, account_id):
        if len(account_id) != 32 or any(
            c not in "0123456789abcdef" for c in account_id
        ):
            raise VaultError("无效账号编号。")
        return self.root / "accounts" / account_id

    @contextmanager
    def lock(self, account_id):
        self.account(account_id)
        lock_dir = self.root / "locks"
        lock_dir.mkdir(exist_ok=True)
        try:
            with FileLock(lock_dir / (account_id + ".lock"), timeout=0):
                yield
        except Timeout as exc:
            raise VaultError("此账号已有任务运行，请稍后再试。") from exc

    def save_github(self, account_id, login, state):
        for cookie in state.get("cookies", []):
            if (
                cookie.get("domain") in {"github.com", ".github.com"}
                and cookie.get("name") == "dotcom_user"
            ):
                if cookie.get("value", "").lower() != login.lower():
                    raise VaultError("GitHub 会话身份发生变化，拒绝覆盖原账号。")
        self.bind_github(account_id, login)
        atomic_json(self.folder(account_id) / "github.json", github_state(state))

    @contextmanager
    def site_lock(self, site):
        if site not in PROVIDERS:
            raise VaultError("未知站点。")
        lock_dir = self.root / "locks"
        lock_dir.mkdir(exist_ok=True)
        try:
            with FileLock(lock_dir / ("site-" + site + ".lock"), timeout=0):
                yield
        except Timeout as exc:
            raise VaultError("该站点已有任务运行或等待，请稍后再试。") from exc

    def load_github(self, account_id):
        self.account(account_id)
        path = self.folder(account_id) / "github.json"
        if not path.exists():
            return {"cookies": [], "origins": []}
        try:
            return github_state(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, TypeError, AttributeError, OSError) as exc:
            raise VaultError("GitHub 会话文件无法读取，请重新登录。") from exc

    def sites(self, account_id=None):
        with self.db() as db:
            rows = db.execute(
                "SELECT s.*,a.label,a.github_login FROM sites s JOIN accounts a ON a.id=s.account_id "
                + ("WHERE account_id=? " if account_id else "")
                + "ORDER BY a.created,a.rowid,s.site",
                (account_id,) if account_id else (),
            ).fetchall()
        return [dict(r) for r in rows]

    def enable(self, account_id, site, enabled):
        with self.db() as db:
            db.execute(
                "UPDATE sites SET enabled=? WHERE account_id=? AND site=?",
                (int(enabled), account_id, site),
            )

    def record(self, account_id, site, result):
        account = self.account(account_id)
        now = self.now()
        with self.db() as db:
            db.execute(
                "INSERT INTO history(account_id,label,site,code,message,created) VALUES (?,?,?,?,?,?)",
                (account_id, account["label"], site, result.code, result.message, now),
            )
            db.execute(
                "UPDATE sites SET code=?,message=?,updated=? WHERE account_id=? AND site=?",
                (result.code, result.message, now, account_id, site),
            )

    def history(self, limit=300):
        with self.db() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)
                )
            ]

    def confirmed_today(self, account_id, site):
        """Return original confirmation time, never renew it when a run is skipped."""
        with self.db() as db:
            row = db.execute(
                "SELECT created FROM history WHERE account_id=? AND site=? "
                "AND code IN ('checked_in','already_checked_in') ORDER BY id DESC LIMIT 1",
                (account_id, site),
            ).fetchone()
        if not row:
            return None
        try:
            confirmed = datetime.fromisoformat(row["created"])
            now = datetime.fromisoformat(self.now())
            if confirmed.tzinfo is None or now.tzinfo is None or confirmed > now:
                return None
            day_zone = timezone(timedelta(hours=8))
            if confirmed.astimezone(day_zone).date() == now.astimezone(day_zone).date():
                return confirmed.astimezone(day_zone)
        except (ValueError, TypeError):
            pass
        return None

    def setting(self, key, default=None):
        with self.db() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value))
            )

    def remove(self, account_id):
        with self.lock(account_id):
            folder = self.folder(account_id)
            for name in ("github.json", "agentrouter.json", "justdowork.json"):
                (folder / name).unlink(missing_ok=True)
            if folder.exists() and not any(folder.iterdir()):
                folder.rmdir()
            with self.db() as db:
                for table in ("sites", "history"):
                    db.execute(f"DELETE FROM {table} WHERE account_id=?", (account_id,))
                db.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Vault:
    def __init__(self, store, account_id):
        self.store, self.account_id = store, account_id
        store.account(account_id)
        self.root = store.folder(account_id)

    def path(self, key):
        if key not in PROVIDERS:
            raise VaultError("未知站点。")
        return self.root / (key + ".json")

    def load(self, key):
        path = self.path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("origin") != PROVIDERS[key].origin or not user_id(
                data.get("user_id")
            ):
                raise ValueError()
            data["cookies"] = site_cookies(data["cookies"], PROVIDERS[key])
            return data
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
            raise VaultError("站点会话文件损坏或不匹配，请重新连接。") from exc

    def save(self, key, uid, cookies):
        if not user_id(uid):
            raise VaultError("未确认用户身份，拒绝保存。")
        old = self.load(key)
        if old and old["user_id"] != int(uid):
            raise VaultError("网站用户身份与已连接账号不同，已阻止覆盖。")
        atomic_json(
            self.path(key),
            {
                "version": 2,
                "origin": PROVIDERS[key].origin,
                "user_id": int(uid),
                "cookies": site_cookies(cookies, PROVIDERS[key]),
            },
        )

    def lock(self, key):
        self.path(key)
        return self.store.lock(self.account_id)
