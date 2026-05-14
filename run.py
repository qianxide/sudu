"""入口脚本。

用法:
  python run.py                       # 用 .env 默认值跑全部素材任务
  python run.py --account 0           # 只用第 0 个账号
  python run.py --account zopgskeuuj  # 用邮箱前缀匹配账号
  python run.py --list-accounts       # 列出所有账号
  python run.py --list-tasks          # 列出所有素材任务
  python run.py --dry-run             # 只打开浏览器登录,不真正生成
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from src.accounts import Account, load_accounts  # noqa: E402
from src.flora_bot import FloraBot  # noqa: E402
from src.materials import Task, load_tasks  # noqa: E402
from src import proxy_pool  # noqa: E402


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    v = _env(name, str(default)).lower()
    return v in ("1", "true", "yes", "y", "on")


def _select_account(accounts: list[Account], key: str | None) -> list[Account]:
    if not key:
        return accounts
    if key.isdigit():
        i = int(key)
        if i < 0 or i >= len(accounts):
            raise SystemExit(f"账号下标越界: {i} (共 {len(accounts)} 个)")
        return [accounts[i]]
    matched = [a for a in accounts if key.lower() in a.email.lower()]
    if not matched:
        raise SystemExit(f"没有账号匹配 '{key}'")
    return matched


def main() -> None:
    load_dotenv(ROOT / ".env")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    ap = argparse.ArgumentParser()
    ap.add_argument("--account", help="账号下标或邮箱前缀")
    ap.add_argument("--task", help="只跑名字包含该子串的任务(如 segment-02)")
    ap.add_argument("--list-accounts", action="store_true")
    ap.add_argument("--list-tasks", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只登录,不生成视频")
    ap.add_argument("--grab-key", action="store_true",
                    help="登录后跳 Settings/API Keys 抓 key,并网络拦截 technique slug")
    ap.add_argument("--record", action="store_true",
                    help="录制模式:登录完成后用户手动操作,所有 API 流量录到 HAR + jsonl")
    ap.add_argument("--download-existing", action="store_true",
                    help="登录后打开项目,把画布上所有现存的视频下载到 output/")
    ap.add_argument("--api", action="store_true",
                    help="走 API 模式:登录拿 cookies → HTTP 调用 Flora 内部 API 上传+生成+下载")
    ap.add_argument("--keep-open", type=int, default=0, metavar="SEC",
                    help="登录/任务完成后浏览器保持打开 N 秒,等待人工操作")
    args = ap.parse_args()

    project_url = _env("FLORA_PROJECT_URL")
    accounts_file = _env("ACCOUNTS_FILE", "./outlook/outlook1.txt")
    materials_dir = _env("MATERIALS_DIR", "./素材")
    user_data_dir_prefix = _env("USER_DATA_DIR_PREFIX", "./.browser_profile")
    headless = _env_bool("HEADLESS", False)
    slow_mo_ms = int(_env("SLOW_MO_MS", "150") or "150")
    gen_timeout = int(_env("GENERATION_TIMEOUT_S", "600") or "600")
    per_account_quota = int(_env("PER_ACCOUNT_QUOTA", "3") or "3")
    proxy_server = _env("PROXY_SERVER", "") or None
    proxy_pool_dir = _env("PROXY_POOL_DIR", "") or None
    auth_provider = _env("AUTH_PROVIDER", "microsoft").lower()

    # 动态 IP 池:如果配置了,优先用池里的 IP(按账号哈希挑),覆盖 PROXY_SERVER
    proxies: list[str] = []
    if proxy_pool_dir:
        proxies = proxy_pool.load_proxies(ROOT / proxy_pool_dir)

    if not project_url:
        raise SystemExit("FLORA_PROJECT_URL 未配置(请复制 .env.example 为 .env)")

    accounts = load_accounts(ROOT / accounts_file)
    if args.list_accounts:
        for i, a in enumerate(accounts):
            print(f"{i:>3}  {a.email}")
        return

    materials_path = ROOT / materials_dir
    tasks: list[Task] = load_tasks(materials_path) if materials_path.exists() else []
    if args.task:
        before = len(tasks)
        tasks = [t for t in tasks if args.task.lower() in t.name.lower()]
        logging.info("--task '%s' 过滤:%d → %d", args.task, before, len(tasks))
    if args.list_tasks:
        if not tasks:
            print(f"(无任务) 素材目录: {materials_path}")
        for t in tasks:
            imgs = ", ".join(p.name for p in t.reference_images)
            print(f"  {t.name}: refs=[{imgs}]  prompt[{len(t.prompt)}字]")
        return

    selected = _select_account(accounts, args.account)
    logging.info("已加载账号 %d 个,本次将用 %d 个", len(accounts), len(selected))
    logging.info("已加载素材任务 %d 个", len(tasks))

    bot = FloraBot(
        project_url=project_url,
        user_data_dir_prefix=ROOT / user_data_dir_prefix,
        headless=headless,
        slow_mo_ms=slow_mo_ms,
        generation_timeout_s=gen_timeout,
        proxy_server=proxy_server,
        auth_provider=auth_provider,
    )
    logging.info("Auth provider: %s", auth_provider)
    if proxy_server:
        logging.info("浏览器代理: %s", proxy_server)

    # 把任务按 per_account_quota 切片分给各账号
    task_iter = iter(tasks)
    for account in selected:
        batch = [t for t, _ in zip(task_iter, range(per_account_quota))]
        if args.dry_run:
            batch = []  # dry-run 只触发登录

        # 动态 IP 池:按账号 email 确定性挑一个出口 IP
        if proxies:
            chosen = proxy_pool.pick_for(account.email, proxies)
            bot.proxy_server = chosen
            logging.info("账号 %s 使用 IP: %s", account.email, proxy_pool.short(chosen))

        if args.api:
            # API 模式:Patchright 登录拿 cookies,然后走 HTTP API
            from src.runner_api import ApiRunner
            logging.info(">>> [API 模式] 使用账号 %s 处理 %d 个任务", account.email, len(batch))
            ApiRunner(bot, account, batch).run(keep_open_s=args.keep_open)
            continue

        logging.info(">>> 使用账号 %s 处理 %d 个任务", account.email, len(batch))
        bot.run_batch(account, batch, keep_open_s=args.keep_open,
                      grab_key=args.grab_key, record_mode=args.record,
                      download_existing=args.download_existing)
        if (not args.grab_key and not args.record and not args.dry_run
                and not args.download_existing and not batch):
            logging.info("素材已用完,提前结束")
            break


if __name__ == "__main__":
    main()
