import argparse
import asyncio
from .vault import Store
from .service import batch


def main():
    parser = argparse.ArgumentParser(description="Fluent Check-in · 本地多账号签到")
    parser.add_argument(
        "command", nargs="?", default="gui", choices=["gui", "run", "status"]
    )
    parser.add_argument("--account", help="账号 UUID；省略则使用全部账号")
    parser.add_argument(
        "--data-dir", help="可选覆盖数据目录；默认使用 EXE（源码为 main.py）旁的 data"
    )
    args = parser.parse_args()
    if args.command == "gui":
        from .ui import launch

        return launch(args.data_dir)
    store = Store(args.data_dir)
    rows = store.sites(args.account)
    if args.command == "status":
        for r in rows:
            print(f"{r['label']} / {r['site']}: {r['message']}")
        return 0
    jobs = [
        (r["account_id"], "run", r["site"])
        for r in rows
        if r["enabled"] and r["github_login"]
    ]
    if not jobs:
        print("没有可执行账号，请先打开界面完成 GitHub 登录。")
        return 2
    code = 0

    def output(account, site, result):
        nonlocal code
        code = max(code, result.exit_code)
        print(f"{store.account(account)['label']} / {site}: {result.message}")

    try:
        asyncio.run(batch(store, jobs, output, print))
    except KeyboardInterrupt:
        return 2
    return code
