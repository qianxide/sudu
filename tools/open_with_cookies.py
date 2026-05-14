"""用保存的 cookies 开一个 Chrome 窗口,什么都不做,保持打开。

用法:
    python tools/open_with_cookies.py keys/imfpyzuhd_outlook.com.cookies.json
"""
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from patchright.sync_api import sync_playwright
from src.api_client import load_cookies
from src.flora_bot import FloraBot
from src.accounts import Account

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger()


def main():
    cookies_path = Path(sys.argv[1])
    cookies = load_cookies(cookies_path)
    log.info("加载 %d 个 cookies(%s)", len(cookies), cookies_path)

    slug = cookies_path.stem.replace(".cookies", "")
    acc = Account(email=slug.replace("_outlook.com", "@outlook.com"),
                  password="", client_id="", refresh_token="")

    bot = FloraBot(
        project_url="https://app.flora.ai/projects",
        user_data_dir_prefix=ROOT / ".browser_profile",
        headless=False,
        slow_mo_ms=50,
        generation_timeout_s=600,
        proxy_server="http://127.0.0.1:7897",
    )

    with sync_playwright() as pw, bot._context_for(pw, acc) as ctx:
        ctx_cookies = [
            {"name": k, "value": v, "domain": ".flora.ai", "path": "/"}
            for k, v in cookies.items()
        ]
        ctx.add_cookies(ctx_cookies)
        log.info("已注入 %d cookies", len(ctx_cookies))

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://app.flora.ai/projects", wait_until="domcontentloaded")
        log.info(">>> 浏览器已开,直接用就行。Ctrl+C 退出 <<<")

        # 一直保持,直到 Ctrl+C
        try:
            while True:
                time.sleep(60)
                log.info("(浏览器仍在,URL: %s)", page.url)
        except KeyboardInterrupt:
            log.info("收到 Ctrl+C,退出")


if __name__ == "__main__":
    main()
