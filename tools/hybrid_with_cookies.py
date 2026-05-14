"""用保存的 cookies 启动 Patchright,跳过 MS 登录,直接测试混合模式的画布部分。"""
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from patchright.sync_api import sync_playwright
from src.accounts import Account
from src.api_client import FloraClient, SEEDANCE_REFERENCE_ENDPOINT, load_cookies
from src.flora_bot import FloraBot
from src.materials import load_tasks
from src.runner_api import ApiRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def main():
    cookies_path = Path(sys.argv[1])
    cookies = load_cookies(cookies_path)
    log.info("加载 %d 个 cookies(从 %s)", len(cookies), cookies_path)

    # 创建一个假账号对象(slug 用来命名输出)
    slug = cookies_path.stem.replace(".cookies", "")
    fake_acc = Account(
        email=slug.replace("_outlook.com", "@outlook.com"),
        password="",  # 不用
        client_id="",
        refresh_token="",
    )

    # 加载素材
    tasks = load_tasks(ROOT / "素材")
    if not tasks:
        log.error("没素材")
        return

    bot = FloraBot(
        project_url="https://app.flora.ai/projects",
        user_data_dir_prefix=ROOT / ".browser_profile",
        headless=False,
        slow_mo_ms=150,
        generation_timeout_s=600,
        proxy_server="http://127.0.0.1:7897",
    )

    with sync_playwright() as pw, bot._context_for(pw, fake_acc) as ctx:
        # 把 cookies 注入 context(注意 Patchright/Playwright 要求每个 cookie 有 domain)
        ctx_cookies = []
        for name, value in cookies.items():
            ctx_cookies.append({
                "name": name,
                "value": value,
                "domain": ".flora.ai",
                "path": "/",
            })
        ctx.add_cookies(ctx_cookies)
        log.info("已注入 %d 个 cookies 到 context", len(ctx_cookies))

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        bot._wire_network_logging(page, fake_acc)

        # 直接进 /projects,跳过登录
        log.info("跳过登录,直接进 /projects")
        page.goto("https://app.flora.ai/projects", wait_until="domcontentloaded")
        page.wait_for_timeout(3500)
        log.info("当前 URL: %s", page.url)

        # 已登录就跑混合流程
        bot._open_project(page)

        runner = ApiRunner(bot, fake_acc, [tasks[0]])
        node_id = runner._create_seedance_node(page)
        log.info("✅ 拿到 nodeId: %s", node_id)

        # 拿当前实际 cookies(可能比注入的多一些)更新到 client
        actual_cookies = {}
        for c in ctx.cookies():
            if "flora.ai" in c.get("domain", ""):
                actual_cookies[c["name"]] = c["value"]
        client = FloraClient(cookies=actual_cookies)
        log.info("client cookies 数: %d", len(actual_cookies))

        runner._run_one(client, tasks[0], node_id=node_id)


if __name__ == "__main__":
    main()
