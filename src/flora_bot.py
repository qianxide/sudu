"""Flora.ai 自动化主流程。

策略:
  1. 用 Playwright 持久化上下文(每账号一个 user-data-dir)启动 Chromium
  2. 打开 Flora 项目链接
  3. 若未登录,走 "Continue with Microsoft" OAuth
       - 优先:用 refresh_token 直接换 ID/Access Token,把 cookie 注入到上下文
       - Fallback:在 Microsoft 登录页里走表单(邮箱→下一步→密码→登录→Stay signed in?Yes)
  4. 进入项目画布,对每个素材任务:
       - 添加 Seedance 2.0 节点(或图生视频节点)
       - 上传首帧 / 尾帧
       - 填入 prompt
       - 触发生成,等待完成
  5. 单账号配额耗尽后,关闭上下文,换下一账号

注意:Flora 画布的真实 DOM 选择器在第一次跑时可能需要按实际 UI 微调。
本模块用语义化定位(role/text)而非脆弱的 CSS,大多数文案变更后仍能命中。
"""
from __future__ import annotations

import logging
import random
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# 切换回原生 playwright(2026-05-14 参照 dreamina_register_playwright_usa 的成功路径):
# patchright 在新的 Flora/Clerk 流程下反而引发 ERR_CONNECTION_CLOSED 类 quirks,
# 而对方实现用的是 plain playwright + browser.launch + new_context,工作良好。
from playwright.sync_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    sync_playwright,
)

from .accounts import Account
from .materials import Task

log = logging.getLogger(__name__)

MICROSOFT_OAUTH_HOSTS = ("login.microsoftonline.com", "login.live.com")
MS_RELATED_HOST_RE = re.compile(
    r"(login\.microsoft(online)?\.com|login\.live\.com|account\.live\.com|account\.microsoft\.com)"
)
GOOGLE_OAUTH_HOSTS = ("accounts.google.com",)
GOOGLE_RELATED_HOST_RE = re.compile(
    r"(accounts\.google\.com|myaccount\.google\.com|signin\.google\.com)"
)


class FloraBot:
    def __init__(
        self,
        *,
        project_url: str,
        user_data_dir_prefix: Path,
        headless: bool = False,
        slow_mo_ms: int = 150,
        generation_timeout_s: int = 600,
        proxy_server: str | None = None,
        auth_provider: str = "microsoft",
    ) -> None:
        self.project_url = project_url
        self.user_data_dir_prefix = Path(user_data_dir_prefix)
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self.generation_timeout_s = generation_timeout_s
        self.proxy_server = proxy_server
        self.auth_provider = auth_provider.lower()  # "microsoft" | "google"

    # ------------------------------------------------------------------ context

    @contextmanager
    def _context_for(self, pw: Playwright, account: Account) -> Iterator[BrowserContext]:
        """参照 dreamina_register_playwright_usa 的成功路径:
        - playwright.chromium.launch + browser.new_context(非 persistent)
        - 用本地真实 Chrome(executable_path)
        - 基础 args + UA + 1920x1080 viewport + clipboard 权限
        - 不挂 locale / timezone / geolocation,避免与 IP 冲突
        """
        log.info("启动 Chrome (playwright)")
        har_dir = Path("logs/har")
        har_dir.mkdir(parents=True, exist_ok=True)
        har_path = har_dir / f"{account.slug}-{int(time.time())}.har"

        # 本地真实 Chrome 路径(参照 dreamina_register 的 find_chrome_browser)
        chrome_path = self._find_chrome_executable()

        launch_args = [
            "--incognito",
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
        ]
        if self.headless:
            launch_args.append("--headless=new")

        if chrome_path:
            log.info("使用本地 Chrome: %s", chrome_path)
            browser = pw.chromium.launch(
                executable_path=chrome_path,
                headless=self.headless,
                slow_mo=self.slow_mo_ms,
                args=launch_args,
            )
        else:
            log.info("未找到本地 Chrome,fallback Playwright 内置 Chromium")
            browser = pw.chromium.launch(
                headless=self.headless,
                slow_mo=self.slow_mo_ms,
                args=launch_args,
            )

        ctx_kwargs = dict(
            viewport={"width": 1280, "height": 800},
            permissions=["clipboard-read", "clipboard-write"],
            record_har_path=str(har_path),
            record_har_content="embed",
        )
        if self.proxy_server:
            ctx_kwargs["proxy"] = {"server": self.proxy_server}
            log.info("使用代理: %s", self.proxy_server)
        ctx = browser.new_context(**ctx_kwargs)
        log.info("HAR 录制中 → %s", har_path)
        self._har_path = har_path
        # 给上下文留个 page,后续逻辑 ctx.pages[0] 不会越界
        ctx.new_page()

        try:
            yield ctx
        finally:
            try:
                ctx.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

    def _find_chrome_executable(self) -> str | None:
        """查本地 Chrome 路径,找不到返回 None。"""
        import os as _os
        candidates = [
            _os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            _os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            _os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        for p in candidates:
            try:
                if _os.path.isfile(p) and _os.path.getsize(p) > 1024 * 1024:
                    return p
            except Exception:
                continue
        return None

    # --------------------------------------------------------------- public API

    def run_batch(self, account: Account, tasks: list[Task], *, keep_open_s: int = 0,
                  grab_key: bool = False, record_mode: bool = False,
                  download_existing: bool = False) -> None:
        with sync_playwright() as pw, self._context_for(pw, account) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # 任何模式都开实时网络日志,把 /api/v1/ 调用打到控制台 + jsonl 文件
            self._wire_network_logging(page, account)

            if grab_key:
                seen_slugs: set[str] = set()
                slug_re = re.compile(r"/api/v1/techniques/([^/?]+)")

                def on_request_slug(req):
                    m = slug_re.search(req.url)
                    if m and m.group(1) not in seen_slugs:
                        seen_slugs.add(m.group(1))
                        log.info("【拦截】technique slug: %s", m.group(1))

                page.on("request", on_request_slug)

            self._ensure_logged_in(page, account)

            if download_existing:
                self._open_project(page)
                self._download_all_videos_on_canvas(page, account)
            elif record_mode:
                log.info(">>> 录制模式:登录已完成,现在请手动操作浏览器。<<<")
                log.info(">>> 所有 /api/v1/ 调用都会实时显示在这里,并录到 HAR <<<")
                log.info(">>> 完成后 Ctrl+C 即可,HAR 文件自动落盘 <<<")
            elif grab_key:
                self._grab_api_key(page, account)
                self._write_slugs(account, seen_slugs)
            else:
                self._open_project(page)
                if not tasks:
                    log.info("没有任务可执行(dry-run / 只登录模式)")
                else:
                    for idx, task in enumerate(tasks, 1):
                        log.info("[%s] 任务 %d/%d: %s", account.email, idx, len(tasks), task.name)
                        try:
                            self._generate_one(page, task)
                        except Exception as e:
                            log.exception("任务 %s 失败: %s", task.name, e)

            if keep_open_s > 0:
                log.info(">>> 浏览器保持打开 %d 秒,等待人工操作。Ctrl+C 可提前结束 <<<", keep_open_s)
                try:
                    page.wait_for_timeout(keep_open_s * 1000)
                except KeyboardInterrupt:
                    log.info("收到 Ctrl+C,关闭浏览器")

    def _wire_network_logging(self, page: Page, account: Account) -> None:
        """订阅 page.on('request'/'response'),把 /api/v1/ 调用实时打印 + 写 jsonl。"""
        import json as _json
        log_path = Path("logs") / f"{account.slug}-api-trace.jsonl"
        log_path.parent.mkdir(exist_ok=True)
        log.info("API trace → %s", log_path)
        trace_fp = log_path.open("a", encoding="utf-8")
        self._trace_fp = trace_fp

        def _short(s: str | None, n: int = 800) -> str:
            if s is None:
                return ""
            return s if len(s) <= n else s[:n] + f"...[+{len(s)-n}]"

        def on_request(req):
            try:
                if "/api/v1/" not in req.url and "flora.ai/api" not in req.url:
                    return
                body = ""
                try:
                    body = req.post_data or ""
                except Exception:
                    pass
                log.info("→ %s %s\n   body=%s", req.method, req.url, _short(body, 400))
                trace_fp.write(_json.dumps({
                    "ts": time.time(), "kind": "request",
                    "method": req.method, "url": req.url,
                    "headers": dict(req.headers),
                    "body": body[:8000] if body else "",
                }, ensure_ascii=False) + "\n")
                trace_fp.flush()
            except Exception as e:
                log.debug("on_request err: %s", e)

        def on_response(resp):
            try:
                if "/api/v1/" not in resp.url and "flora.ai/api" not in resp.url:
                    return
                body = ""
                try:
                    body = resp.text()
                except Exception:
                    pass
                log.info("← %d %s\n   body=%s", resp.status, resp.url, _short(body, 600))
                trace_fp.write(_json.dumps({
                    "ts": time.time(), "kind": "response",
                    "status": resp.status, "url": resp.url,
                    "headers": dict(resp.headers),
                    "body": body[:16000] if body else "",
                }, ensure_ascii=False) + "\n")
                trace_fp.flush()
            except Exception as e:
                log.debug("on_response err: %s", e)

        page.on("request", on_request)
        page.on("response", on_response)

    # ---------------------------------------------------------- API key 抓取

    def _download_all_videos_on_canvas(self, page: Page, account: Account) -> None:
        """扫描画布上所有 <video> 元素,把视频 src 抓出来下载到 output/。
        如果视频还在生成,等最多 15 分钟。
        """
        output_dir = Path("output")
        output_dir.mkdir(exist_ok=True)
        deadline = time.time() + 15 * 60
        downloaded: set[str] = set()
        loop = 0

        while time.time() < deadline:
            loop += 1
            # 拉所有 video src(包括 <source>),优先 data-id=video-block-result(用户指定)
            try:
                srcs = page.evaluate("""
                    () => {
                        const out = [];
                        // 优先抓 data-id=video-block-result(Flora 的结果视频元素)
                        document.querySelectorAll('video[data-id="video-block-result"]').forEach(v => {
                            const s = v.src || v.currentSrc;
                            if (s && !out.includes(s)) out.push(s);
                        });
                        // 兜底:其它所有 video 元素
                        document.querySelectorAll('video').forEach(v => {
                            const s = v.src || v.currentSrc;
                            if (s && !out.includes(s)) out.push(s);
                            v.querySelectorAll('source').forEach(srcEl => {
                                if (srcEl.src && !out.includes(srcEl.src)) out.push(srcEl.src);
                            });
                        });
                        return out;
                    }
                """)
            except Exception as e:
                log.warning("evaluate 失败: %s", e)
                srcs = []

            new_srcs = [s for s in srcs if s and s not in downloaded and (s.startswith("http") or s.startswith("blob:"))]
            log.info("[轮 %d] 扫到 %d 个 video,新增 %d 个", loop, len(srcs), len(new_srcs))

            for i, src in enumerate(new_srcs):
                downloaded.add(src)
                target = output_dir / f"{account.slug}-{int(time.time())}-{i}.mp4"
                log.info("下载: %s → %s", src[:120], target)
                try:
                    if src.startswith("blob:"):
                        # blob URL 需要在 page 里 fetch 后转 base64 再下载
                        data = page.evaluate(
                            """async (url) => {
                                const r = await fetch(url);
                                const buf = await r.arrayBuffer();
                                return Array.from(new Uint8Array(buf));
                            }""",
                            src,
                        )
                        target.write_bytes(bytes(data))
                    else:
                        data = page.evaluate(
                            """async (url) => {
                                const r = await fetch(url, {credentials: 'include'});
                                const buf = await r.arrayBuffer();
                                return Array.from(new Uint8Array(buf));
                            }""",
                            src,
                        )
                        target.write_bytes(bytes(data))
                    log.info("✓ 已保存 %s (%d bytes)", target, target.stat().st_size)
                except Exception as e:
                    log.warning("下载失败: %s", e)

            if downloaded and loop > 3:
                # 至少跑 3 轮再退出,等可能后到的视频
                log.info("已下载 %d 个视频,继续等待 15 秒看有没有新的", len(downloaded))
                page.wait_for_timeout(15_000)
                # 再扫一次,没新增就退
                try:
                    final_srcs = page.evaluate("() => Array.from(document.querySelectorAll('video')).map(v => v.src || v.querySelector('source')?.src).filter(Boolean)")
                except Exception:
                    final_srcs = []
                if all(s in downloaded for s in final_srcs):
                    log.info("没有新视频,扫描结束。共下载 %d 个", len(downloaded))
                    return

            page.wait_for_timeout(5000)

        log.warning("15 分钟到,共下载 %d 个视频", len(downloaded))

    def _grab_api_key(self, page: Page, account: Account) -> None:
        """登录后跳到 Settings → API Keys,抓 key 落盘。"""
        keys_url = "https://app.flora.ai/projects?openSettings=true&initialTab=apiKeys"
        log.info("跳转到 API Keys 页: %s", keys_url)
        page.goto(keys_url, wait_until="domcontentloaded")
        self._human_wait(3000, 4500)
        self._dump_screenshot(page, "step-keys-page")

        # 看一下页面上有没有现成的 key
        key = self._extract_key_from_page(page)
        if key:
            log.info("发现现有 key,直接复用: %s...%s", key[:8], key[-4:])
            self._save_key(account, key)
            return

        # 没现成 key 就点 "Create API Key"
        log.info("没有现成 key,尝试创建...")
        create_re = re.compile(r"Create( new)?( API)? Key|Create API|新建|创建", re.I)
        for label, loc in [
            ("button create", page.get_by_role("button", name=create_re)),
            ("link create", page.get_by_role("link", name=create_re)),
        ]:
            try:
                loc.first.click(timeout=4000)
                log.info("点击: %s", label)
                self._human_wait(1500, 2500)
                break
            except Exception:
                continue

        # 创建后可能弹出输入名字的对话框 + 确认按钮
        try:
            name_input = page.locator(
                "input[placeholder*='name' i], input[placeholder*='key' i]"
            ).first
            if name_input.is_visible(timeout=1500):
                self._human_type(name_input, f"batch-{account.slug[:10]}")
                self._human_wait(500, 1000)
        except Exception:
            pass

        # 确认创建
        try:
            page.get_by_role("button", name=re.compile(r"^(Create|Confirm|Done|确定|创建)$", re.I)).first.click(timeout=4000)
            self._human_wait(2000, 3500)
        except Exception:
            pass

        self._dump_screenshot(page, "step-keys-after-create")
        key = self._extract_key_from_page(page)
        if key:
            log.info("创建成功,key: %s...%s", key[:8], key[-4:])
            self._save_key(account, key)
        else:
            log.error("没能抓到 key,请手动检查页面或检查截图")
            page.wait_for_timeout(60_000)

    def _extract_key_from_page(self, page: Page) -> str | None:
        """从页面里找 sk_live_ 开头的字符串。"""
        # 把整页文本拉下来做正则
        try:
            text = page.locator("body").inner_text(timeout=3000)
        except Exception:
            return None
        m = re.search(r"sk_live_[A-Za-z0-9_-]{20,}", text)
        if m:
            return m.group(0)
        # 也可能在 input value / readonly field 里
        for sel in ("input[readonly]", "code", "[class*='key' i]", "[data-key]"):
            try:
                for el in page.locator(sel).all():
                    v = el.get_attribute("value") or el.inner_text(timeout=500)
                    if v and "sk_live_" in v:
                        m = re.search(r"sk_live_[A-Za-z0-9_-]{20,}", v)
                        if m:
                            return m.group(0)
            except Exception:
                continue
        return None

    def _save_key(self, account: Account, key: str) -> None:
        keys_dir = Path("keys")
        keys_dir.mkdir(exist_ok=True)
        f = keys_dir / f"{account.slug}.txt"
        f.write_text(key + "\n", encoding="utf-8")
        log.info("key 已保存: %s", f)

    def _write_slugs(self, account: Account, slugs: set[str]) -> None:
        """把网络拦截到的所有 technique slug 落盘,方便人工挑出 Seedance Reference。"""
        if not slugs:
            return
        keys_dir = Path("keys")
        keys_dir.mkdir(exist_ok=True)
        f = keys_dir / f"{account.slug}.slugs.txt"
        f.write_text("\n".join(sorted(slugs)) + "\n", encoding="utf-8")
        log.info("已记录 %d 个 technique slug → %s", len(slugs), f)

    # ---------------------------------------------------------------- login

    def _ensure_logged_in(self, page: Page, account: Account) -> None:
        # 第一次进 sign-up 页(用户 2026-05-14 指定):
        # 这批是全新 Outlook 账号,在 Flora 上从未注册过,
        # 必须从 /sign-up?redirect_url=... 走,这样 OAuth 回调能直接进 /projects。
        signup_url = "https://app.flora.ai/sign-up?redirect_url=https%3A%2F%2Fapp.flora.ai%2Fprojects"
        page.goto(signup_url, wait_until="domcontentloaded")
        self._human_wait(2000, 3500)

        if self._is_on_flora_project(page):
            log.info("已登录: %s", account.email)
            return

        if self.auth_provider == "google":
            log.info("未登录,走 Google OAuth: %s", account.email)
            self._click_continue_with_google(page)
            self._google_signin(page, account)
        else:
            log.info("未登录,走 Microsoft OAuth: %s", account.email)
            self._click_continue_with_microsoft(page)
            self._microsoft_signin(page, account)

        # OAuth 完成后,等 URL 真正回到 Flora 项目页(不是 /sign-in)
        # 路上可能再次出现 MS 登录页(点"已登录"卡片即可)或 Cloudflare 人机验证。
        log.info("等待跳转回 Flora 项目页(同时处理二次 MS 登录 / Cloudflare 验证)...")
        if not self._wait_for_project_with_handlers(page, timeout_s=300):
            self._dump_screenshot(page, "after-signin-no-project-url")
            log.error("OAuth 完成但 URL 没回到 /projects/,当前: %s", page.url)
            log.error("浏览器保持 90 秒,你可手动完成剩余步骤(若 Cloudflare 没自动点掉)")
            page.wait_for_timeout(90_000)
            if not self._is_on_flora_project(page):
                raise RuntimeError(f"OAuth 完成但仍未进入项目页: {account.email}")
        log.info("已回到 Flora 项目页:%s", page.url)

    def _wait_for_project_with_handlers(self, page: Page, *, timeout_s: int) -> bool:
        """在 timeout_s 秒内等 URL 进入 /projects/,期间反复处理:
           1. Microsoft 二次登录页(点'已登录'账户卡片)
           2. Cloudflare Turnstile 复选框
           3. 卡在 sso-callback 黑屏超过 12 秒就自动刷新
           4. 其它通用"跳过/同意"按钮
           5. **/sign-in?redirect_url=... 卡 >20s 时停止自动 retry,等用户手动接手**
        """
        deadline = time.time() + timeout_s
        loop = 0
        stuck_since: float | None = None
        stuck_url = ""
        reloaded_count = 0
        clerk_404_redirected = False  # 只主动跳 1 次,避免循环
        ms_retry_count = 0           # 二次 OAuth 重试次数,>=1 就停手等用户
        signin_hand_off = False      # 已让位给手动操作的标志

        while time.time() < deadline:
            loop += 1
            if self._is_on_flora_project(page):
                return True

            url = page.url
            log.info("[等项目页] 第%d轮  URL: %s", loop, url[:120])

            # 0a) Clerk SDK bug:clerk.flora.ai/sso-callback 经常 404(它本该
            #     重定向到 app.flora.ai/sso-callback?redirect_url=... 但相对路径写挂)
            #     检测到就主动 goto app.flora.ai/projects;session 已建则进画布,
            #     没建则被弹回 /sign-in,反正不会再死循环
            if "clerk.flora.ai" in url and not clerk_404_redirected:
                clerk_404_redirected = True
                # clerk.flora.ai/sso-callback 实际是 404:Clerk SDK 跑在 app.flora.ai
                # 把 domain 换掉,用同样的 query string 跳到 app.flora.ai/sso-callback
                from urllib.parse import urlparse, urlunparse
                parsed = urlparse(url)
                app_sso = urlunparse(
                    parsed._replace(netloc="app.flora.ai")
                )
                log.warning("clerk.flora.ai/sso-callback 是 404,转跳 app.flora.ai: %s", app_sso[:120])
                try:
                    page.goto(app_sso, wait_until="domcontentloaded", timeout=20_000)
                except PWTimeout:
                    pass
                # 等 Flora Clerk SDK 建立会话并跳转 redirect_url(最多20s)
                self._human_wait(3000, 4000)
                try:
                    page.wait_for_url(
                        lambda u: "sso-callback" not in u and "sign-in" not in u
                                  and "sign-up" not in u,
                        timeout=20_000
                    )
                    log.info("Clerk session 建立完成,现在: %s", page.url[:80])
                except PWTimeout:
                    log.warning("等会话超时,当前 URL: %s", page.url[:80])
                stuck_since = None
                stuck_url = ""
                continue

            provider_name = "Google" if self.auth_provider == "google" else "Microsoft"
            click_provider = (self._click_continue_with_google
                              if self.auth_provider == "google"
                              else self._click_continue_with_microsoft)
            provider_button_visible = (self._google_button_visible
                                       if self.auth_provider == "google"
                                       else self._microsoft_button_visible)
            provider_host_re = (GOOGLE_RELATED_HOST_RE
                                if self.auth_provider == "google"
                                else MS_RELATED_HOST_RE)

            # 0c) 任何域下的 /sso-callback — Clerk 的 SSO 回调页
            #     等 Clerk SDK 建立 session 并自动跳转(最多 15s),超时直接跳 /projects
            if "sso-callback" in url:
                log.info("在 sso-callback,等 Clerk 完成 session (15s)...")
                try:
                    page.wait_for_url(
                        lambda u: "sso-callback" not in u,
                        timeout=15_000
                    )
                    log.info("Clerk session 完成,跳到: %s", page.url[:80])
                except PWTimeout:
                    log.warning("sso-callback 15s 未跳(黑屏?),强制跳转 /projects ...")
                    try:
                        page.goto("https://app.flora.ai/projects",
                                  wait_until="domcontentloaded", timeout=20_000)
                        self._human_wait(2000, 3000)
                    except Exception as e:
                        log.warning("强制跳转失败: %s", e)
                stuck_since = None
                stuck_url = ""
                continue

            # 0b) **被踢回 /sign-in?redirect_url=...** 说明 Clerk callback 拿了 code
            #     但没在 app.flora.ai 建 session(Clerk SDK / code 过期 bug)。
            #     策略:retry 最多 1 次;再卡就停手等用户手动点 provider 一次。
            if "/sign-in" in url and ("redirect_url" in url or "accounts.flora.ai" in url):
                if ms_retry_count >= 1 and not signin_hand_off:
                    signin_hand_off = True
                    log.warning("=" * 60)
                    log.warning(">>> Clerk 没建 session,自动 retry 失败 <<<")
                    log.warning(">>> 请在浏览器里【手动点一下 %s 按钮】 <<<", provider_name)
                    log.warning(">>> %s 已登录,几秒后应该自动跳到 /projects <<<", provider_name)
                    log.warning("=" * 60)
                if signin_hand_off:
                    page.wait_for_timeout(2000)
                    continue
                ms_retry_count += 1
                stuck_since = None
                log.info("发现 Flora sign-in 里的 %s 按钮,首次自动 retry OAuth", provider_name)
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                try:
                    click_provider(page)
                    self._human_wait(1500, 2500)
                except Exception as e:
                    log.warning("二次点 %s 失败: %s", provider_name, e)
                    page.wait_for_timeout(1500)
                continue

            # 0) flora.ai 子域里看到 provider 按钮就点(兜底其它非 sign-in 情况)
            if ("flora.ai" in url and not signin_hand_off
                and "/sign-in" not in url and "/sign-up" not in url
                and provider_button_visible(page)):
                stuck_since = None
                log.info("发现 Flora 页面里的 %s 按钮,重做 OAuth (URL: %s)", provider_name, url)
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                try:
                    click_provider(page)
                    self._human_wait(1500, 2500)
                except Exception as e:
                    log.warning("二次点 %s 失败: %s", provider_name, e)
                    page.wait_for_timeout(1500)
                continue

            # 1) Microsoft 第二次登录页 → 点'已登录'账户卡片
            if MS_RELATED_HOST_RE.search(url):
                stuck_since = None
                if self._click_signed_in_account_card(page):
                    self._human_wait(1500, 2500)
                    continue
                self._click_any_skip_or_yes(page)
                page.wait_for_timeout(1500)
                continue

            # 2) Cloudflare Turnstile
            cf_state = self._detect_cloudflare(page)
            if cf_state == "iframe":
                stuck_since = None
                if not self._try_click_cloudflare(page):
                    log.warning(">>> Cloudflare 自动点击未成功,请在浏览器里手动点击复选框 <<<")
                self._human_wait(3000, 5000)
                continue
            elif cf_state == "text-only":
                log.warning(">>> 检测到 Cloudflare 验证页面 但找不到 iframe,请在浏览器手动点击 <<<")
                stuck_since = None
                page.wait_for_timeout(3000)
                continue

            # 3) 卡在同一 URL(尤其是 sso-callback)超过 12 秒 → 刷新
            now = time.time()
            if url == stuck_url:
                if stuck_since and now - stuck_since > 12 and reloaded_count < 3:
                    reloaded_count += 1
                    log.warning("URL 卡住 >12s,刷新页面(第 %d 次)", reloaded_count)
                    self._dump_screenshot(page, f"stuck-before-reload-{reloaded_count}")
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=20_000)
                    except PWTimeout:
                        pass
                    stuck_since = time.time()
                    self._human_wait(1500, 2500)
                    continue
            else:
                stuck_url = url
                stuck_since = now

            page.wait_for_timeout(1200)

        return self._is_on_flora_project(page)

    def _microsoft_button_visible(self, page: Page) -> bool:
        """Flora 页面上是否能看到 'Microsoft' 登录按钮(用于二次 OAuth 检测)。"""
        try:
            ms_btn = page.get_by_role("button", name=re.compile(r"^\s*microsoft\s*$", re.I))
            return ms_btn.first.is_visible(timeout=400)
        except Exception:
            return False

    def _google_button_visible(self, page: Page) -> bool:
        """Flora 页面上是否能看到 'Google' 登录按钮。"""
        try:
            g_btn = page.get_by_role("button", name=re.compile(r"^\s*google\s*$", re.I))
            return g_btn.first.is_visible(timeout=400)
        except Exception:
            return False

    def _click_signed_in_account_card(self, page: Page) -> bool:
        """MS 第二次登录页一般会列出'我们发现了你可以在此处使用的帐户'卡片,点它即可。"""
        candidates = [
            page.get_by_text(re.compile(r"已登录", re.I)),
            page.locator("[data-test-id='signedIn-tile']"),
            page.locator("[aria-label*='已登录' i]"),
        ]
        for loc in candidates:
            try:
                if loc.first.is_visible(timeout=600):
                    loc.first.click(timeout=2500)
                    log.info("已点'已登录'账户卡片")
                    return True
            except Exception:
                continue
        return False

    def _click_any_skip_or_yes(self, page: Page) -> None:
        """通用兜底:点页面里的 跳过/Skip/Yes/是/Accept/Continue/同意 按钮。"""
        skip_re = re.compile(r"跳过|Skip for now|Skip|Not now|Maybe later|稍后|以后再说", re.I)
        yes_re = re.compile(r"^(Yes|是)$", re.I)
        accept_re = re.compile(r"^(Accept|Continue|Allow|接受|同意|继续)$", re.I)
        for label, loc in [
            ("link skip", page.get_by_role("link", name=skip_re)),
            ("button skip", page.get_by_role("button", name=skip_re)),
            ("button yes", page.get_by_role("button", name=yes_re)),
            ("button accept/continue", page.get_by_role("button", name=accept_re)),
            ("link accept/continue", page.get_by_role("link", name=accept_re)),
        ]:
            try:
                if loc.first.is_visible(timeout=400):
                    loc.first.click(timeout=2000)
                    log.info("通用点击: %s", label)
                    return
            except Exception:
                continue

    def _detect_cloudflare(self, page: Page) -> str:
        """检测 Cloudflare 状态:
           'iframe' - 找到了 Turnstile iframe(可以尝试自动点击)
           'text-only' - 只有"Verify you are human"文本(需手动)
           'none' - 没检测到
        """
        for sel in [
            "iframe[src*='challenges.cloudflare.com']",
            "iframe[src*='turnstile']",
            "iframe[title*='Widget containing' i]",
            "iframe[title*='challenge' i]",
            "iframe[title*='Cloudflare' i]",
        ]:
            try:
                if page.locator(sel).first.is_visible(timeout=200):
                    return "iframe"
            except Exception:
                continue
        try:
            if page.get_by_text(re.compile(r"Verify you are human|确认您是人类|确认你是人类", re.I)).first.is_visible(timeout=200):
                return "text-only"
        except Exception:
            pass
        return "none"

    def _try_click_cloudflare(self, page: Page) -> bool:
        """检测并点击 Cloudflare Turnstile 复选框('Verify you are human')。

        策略:Cloudflare 跨域 iframe 用 frame_locator 经常进不去,直接用真实鼠标
        轨迹按坐标点 iframe 左侧的 checkbox 区域,这是最稳的方式。
        """
        cf_iframe_sels = [
            "iframe[src*='challenges.cloudflare.com']",
            "iframe[src*='turnstile']",
            "iframe[title*='Widget containing' i]",
            "iframe[title*='challenge' i]",
            "iframe[title*='Cloudflare' i]",
        ]
        iframe = None
        matched_sel = ""
        for sel in cf_iframe_sels:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=300):
                    iframe = loc
                    matched_sel = sel
                    break
            except Exception:
                continue

        if iframe is None:
            # 没找到 iframe 但页面文本有"Verify you are human"
            try:
                if page.get_by_text(re.compile(r"Verify you are human|确认您是人类|确认你是人类", re.I)).first.is_visible(timeout=300):
                    log.warning("发现 Cloudflare 文本但未能定位 iframe,等用户手动点击复选框")
            except Exception:
                pass
            return False

        log.info("发现 Cloudflare iframe: %s", matched_sel)
        # 让 iframe 内部脚本充分初始化(Cloudflare 会观察 200~500ms 内的轨迹)
        self._human_wait(900, 1500)

        box = iframe.bounding_box()
        if not box:
            log.warning("Cloudflare iframe 没有 bounding box")
            return False

        # 复选框在 widget 左侧约 30px 处
        target_x = box["x"] + 30
        target_y = box["y"] + box["height"] / 2

        # 模拟真人鼠标:从屏幕外随机起点,弧线两段移到目标,微抖,再点击
        start_x = box["x"] - random.randint(150, 300)
        start_y = box["y"] - random.randint(50, 150)
        try:
            page.mouse.move(start_x, start_y)
            self._human_wait(120, 250)
            mid_x = box["x"] + random.randint(80, 140)
            mid_y = box["y"] + random.randint(-10, 30)
            page.mouse.move(mid_x, mid_y, steps=random.randint(20, 30))
            self._human_wait(100, 220)
            page.mouse.move(target_x, target_y, steps=random.randint(8, 14))
            self._human_wait(80, 180)
            # 微抖一下,再按下
            page.mouse.move(target_x + random.randint(-2, 2), target_y + random.randint(-2, 2), steps=3)
            page.mouse.down()
            self._human_wait(60, 130)
            page.mouse.up()
            log.info("Cloudflare checkbox 已按坐标点击 (%d,%d)", int(target_x), int(target_y))
            return True
        except Exception as e:
            log.warning("Cloudflare 坐标点击失败: %s", e)
            return False

    def _is_on_flora_project(self, page: Page) -> bool:
        """是否已经登录进 Flora(不在 auth 页面就算)。

        覆盖 /projects (列表)、/projects/<id> (画布)、/new/* (新建项目向导)等。
        """
        url = page.url
        if "/sign-in" in url or "/sign-up" in url or "/sso-callback" in url:
            return False
        if "/auth/" in url or "/oauth" in url:
            return False
        return "app.flora.ai/" in url and "/access-denied" not in url

    def _looks_logged_in(self, page: Page) -> bool:
        url = page.url
        if MICROSOFT_OAUTH_HOSTS[0] in url or MICROSOFT_OAUTH_HOSTS[1] in url:
            return False
        # Flora 登录页通常有 "Continue with" 按钮;登录后画布会出现
        try:
            return not page.get_by_text("Continue with", exact=False).first.is_visible(timeout=1500)
        except PWTimeout:
            return True

    def _click_continue_with_microsoft(self, page: Page) -> None:
        """点击 Flora sign-in 页的 Microsoft 按钮,验证跳转到 MS 域名才算成功。"""
        ms_re_loose = re.compile(r"microsoft", re.I)  # 子串匹配,更宽
        before = page.url
        ms_url_re = re.compile(r"^https?://login\.(microsoftonline|live)\.com/")
        candidates = [
            ("role=button has microsoft", page.get_by_role("button", name=ms_re_loose)),
            ("role=link has microsoft", page.get_by_role("link", name=ms_re_loose)),
            ("button:has-text Microsoft", page.locator("button:has-text('Microsoft')")),
            ("a:has-text Microsoft", page.locator("a:has-text('Microsoft')")),
            ("aria-label Microsoft", page.locator("[aria-label*='Microsoft' i]")),
        ]
        for desc, loc in candidates:
            try:
                loc.first.click(timeout=4000)
                log.info("点击 Microsoft 按钮: %s", desc)
                # 用 wait_for_url 替代 wait_for_function(避免 CSP 拦截 eval)
                try:
                    page.wait_for_url(ms_url_re, timeout=10_000)
                    log.info("已跳转到 Microsoft 登录页: %s", page.url[:80])
                    return
                except PWTimeout:
                    log.warning("点了 %s 但 URL 没跳 MS (现在 %s),换下一个", desc, page.url[:80])
                    if page.url != before:
                        try:
                            page.go_back()
                            page.wait_for_timeout(800)
                        except Exception:
                            pass
                    continue
            except PWTimeout:
                continue
            except Exception as e:
                log.debug("候选 %s 失败: %s", desc, e)
                continue
        self._dump_screenshot(page, "no-microsoft-button")
        raise RuntimeError("找不到能跳转到 Microsoft 的按钮")

    def _click_continue_with_google(self, page: Page) -> None:
        """点 Flora sign-in 页的 Google 按钮,验证跳转到 accounts.google.com 才算成功。"""
        g_re_loose = re.compile(r"google", re.I)
        before = page.url
        g_url_re = re.compile(r"^https?://accounts\.google\.com/")
        candidates = [
            ("role=button has google", page.get_by_role("button", name=g_re_loose)),
            ("role=link has google", page.get_by_role("link", name=g_re_loose)),
            ("button:has-text Google", page.locator("button:has-text('Google')")),
            ("a:has-text Google", page.locator("a:has-text('Google')")),
            ("aria-label Google", page.locator("[aria-label*='Google' i]")),
        ]
        for desc, loc in candidates:
            try:
                loc.first.click(timeout=4000)
                log.info("点击 Google 按钮: %s", desc)
                try:
                    page.wait_for_url(g_url_re, timeout=10_000)
                    log.info("已跳转到 Google 登录页: %s", page.url[:80])
                    return
                except PWTimeout:
                    log.warning("点了 %s 但 URL 没跳 Google (现在 %s),换下一个", desc, page.url[:80])
                    if page.url != before:
                        try:
                            page.go_back()
                            page.wait_for_timeout(800)
                        except Exception:
                            pass
                    continue
            except PWTimeout:
                continue
            except Exception as e:
                log.debug("候选 %s 失败: %s", desc, e)
                continue
        self._dump_screenshot(page, "no-google-button")
        raise RuntimeError("找不到能跳转到 Google 的按钮")

    def _google_signin(self, page: Page, account: Account) -> None:
        """Google OAuth 登录:邮箱 → Next → 密码 → Next → (验证身份/接受条款) → 回 Flora。"""
        page.wait_for_load_state("domcontentloaded", timeout=20_000)
        self._human_wait(800, 1500)

        # === 邮箱 ===
        log.info("等 Google 邮箱输入框出现...")
        email_input = page.locator(
            "input[type='email']:visible, "
            "input[name='identifier']:visible, "
            "#identifierId"
        ).first
        email_input.wait_for(state="visible", timeout=20_000)
        self._dump_screenshot(page, "g-step1-email-page")
        self._human_type(email_input, account.email)
        log.info("已填 Google 邮箱,Next")
        self._human_wait(400, 900)
        # Google 的 Next 按钮:#identifierNext 或 role=button name=Next
        self._google_click_next(page, "#identifierNext", "g-email-next")
        self._human_wait(1800, 2800)
        self._dump_screenshot(page, "g-step2-after-email")

        # === 密码 ===
        log.info("等 Google 密码输入框出现...")
        password_input = page.locator(
            "input[type='password']:visible, "
            "input[name='password']:visible, "
            "input[name='Passwd']:visible"
        ).first
        try:
            password_input.wait_for(state="visible", timeout=15_000)
        except PWTimeout:
            self._dump_screenshot(page, "g-step2b-no-password-yet")
            # 可能出现了图片验证码或其他人机验证,提示用户手动处理
            current_url = page.url
            if "identifier" in current_url or "signin" in current_url:
                log.warning("=" * 60)
                log.warning("⚠️  Google 出现验证码或人机验证!")
                log.warning("请在浏览器窗口中手动完成验证,然后等待继续...")
                log.warning("=" * 60)
                # 等待最多 120 秒让用户手动过验证码
                try:
                    password_input.wait_for(state="visible", timeout=120_000)
                    log.info("验证码已通过,继续填密码")
                except PWTimeout:
                    self._dump_screenshot(page, "g-step2b-captcha-timeout")
                    raise RuntimeError(
                        "Google 验证码等待超时(120s) —— 请重试"
                    )
            else:
                self._dump_screenshot(page, "g-step2b-no-password-yet")
                raise RuntimeError(
                    "Google 密码框未出现 —— 可能账号不存在/被风控/要求验证身份"
                )
        self._human_type(password_input, account.password)
        log.info("已填 Google 密码,Next")
        self._human_wait(400, 900)
        self._google_click_next(page, "#passwordNext", "g-password-next")
        self._human_wait(1800, 2800)
        self._dump_screenshot(page, "g-step3-after-password")

        # === 可能弹出的页面:验证身份/欢迎新账户/同意条款/选择账户 ===
        self._handle_google_post_login_prompts(page)

    def _google_click_next(self, page: Page, id_sel: str, label: str) -> None:
        """点 Google 的 Next。Enter 优先,再点 #identifierNext / #passwordNext,再点 role=button Next。"""
        before = page.url
        # 1) Enter 提交(对 input 最稳)
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass
        if self._wait_for_navigation_or_change(page, before, timeout_ms=4000):
            return
        # 2) 点 id 选择器(Google 的 Next 按钮通常是 <div id=identifierNext><button>...</button></div>)
        for desc, loc in [
            (id_sel, page.locator(id_sel)),
            (f"{id_sel} button", page.locator(f"{id_sel} button")),
            ("role=button name=Next", page.get_by_role("button", name=re.compile(r"^(Next|下一步)$", re.I))),
            ("text Next", page.get_by_text(re.compile(r"^(Next|下一步)$", re.I))),
        ]:
            try:
                loc.first.click(timeout=4000)
                log.info("[%s] 点击: %s", label, desc)
                if self._wait_for_navigation_or_change(page, before, timeout_ms=8000):
                    return
            except Exception as e:
                log.debug("[%s] %s 失败: %s", label, desc, e)
        self._dump_screenshot(page, label)
        raise RuntimeError(f"[{label}] 所有 Next 提交方式都失败")

    def _handle_google_post_login_prompts(self, page: Page) -> None:
        """Google 登录后可能弹:验证身份/欢迎新账户/接受条款/选择账户/同意 OAuth 权限。
        在 60 秒内反复尝试,直到离开 accounts.google.com。
        """
        log.info("处理 Google 登录后弹窗(欢迎/同意/验证身份...)")
        deadline = time.time() + 60
        # 去掉 ^ $ 锚定,让 "Continue as [name]" 之类也能匹配
        accept_re = re.compile(
            r"I agree|Agree|Continue|Allow|Accept|Confirm|I understand|Got it|"
            r"我同意|同意|继续|允许|接受|确认|我了解|知道了|好的",
            re.I,
        )
        not_now_re = re.compile(
            r"^(Not now|Skip|Maybe later|"
            r"暂不|跳过|稍后)$",
            re.I,
        )
        loop_count = 0
        while time.time() < deadline:
            url = page.url
            if not GOOGLE_RELATED_HOST_RE.search(url):
                log.info("已离开 Google 域(URL: %s),弹窗处理结束", url)
                return
            loop_count += 1
            if loop_count <= 5 or loop_count % 5 == 0:
                self._dump_screenshot(page, f"g-prompt-loop-{loop_count}")
            clicked = False
            click_order = [
                ("button 同意/继续", page.get_by_role("button", name=accept_re)),
                ("link 同意/继续", page.get_by_role("link", name=accept_re)),
                ("button 暂不/跳过", page.get_by_role("button", name=not_now_re)),
                ("link 暂不/跳过", page.get_by_role("link", name=not_now_re)),
            ]
            for label, loc in click_order:
                try:
                    if loc.first.is_visible(timeout=600):
                        loc.first.click(timeout=2500)
                        log.info("点击: %s (URL: %s)", label, page.url)
                        self._human_wait(1500, 2500)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                page.wait_for_timeout(1000)
        log.warning("60秒后还在 Google 域,放弃自动处理")
        self._dump_screenshot(page, "g-prompt-loop-timeout")

    def _microsoft_signin(self, page: Page, account: Account) -> None:
        """Microsoft 登录:邮箱 → 下一步 → 密码 → 登录 → 跳过安全 → 保持登录?是 → 同意。

        拟人化策略:输入字符逐个 type (50~150ms/字符),操作之间随机停顿。
        """
        page.wait_for_load_state("domcontentloaded", timeout=20_000)
        self._human_wait(800, 1500)

        # 账号选择器(如果出现)→ 用另一个账号
        try:
            page.get_by_role(
                "link", name=re.compile(r"Use another account|另一个|其他账户", re.I)
            ).first.click(timeout=2500)
            self._human_wait()
        except Exception:
            pass

        # === 邮箱 ===
        log.info("等邮箱输入框出现...")
        email_input = page.locator("input[type='email'], input[name='loginfmt']").first
        email_input.wait_for(state="visible", timeout=20_000)
        self._dump_screenshot(page, "step1-email-page")
        self._human_type(email_input, account.email)
        log.info("已填邮箱,提交...")
        self._human_wait(400, 900)
        self._submit_form(page, email_input, re.compile(r"Next|下一步", re.I), label="email-next")
        self._human_wait(1500, 2500)
        self._dump_screenshot(page, "step2-after-email")

        # 邮箱后也不再主动读 _read_ms_error,同样原因(DOM 残留)。
        # 真错误(账号不存在)会让密码输入框永远不出现,接下来的 wait_for 会超时,
        # 那时再 dump 截图人工判断。

        # === 可能出现 "验证你的电子邮件" 分支页,点 "使用密码" 跳回密码流程 ===
        try:
            use_pw = page.get_by_role(
                "link", name=re.compile(r"使用密码|Use your password", re.I)
            ).or_(
                page.get_by_text(re.compile(r"使用密码|Use your password", re.I))
            )
            if use_pw.first.is_visible(timeout=2000):
                log.info("出现'验证你的电子邮件'页,点'使用密码'跳过")
                use_pw.first.click(timeout=3000)
                self._human_wait(1500, 2500)
        except Exception:
            pass

        # === 密码 ===
        log.info("等密码输入框出现...")
        password_input = page.locator(
            "input[type='password']:visible, input[name='passwd']:visible"
        ).first
        try:
            password_input.wait_for(state="visible", timeout=15_000)
        except PWTimeout:
            # 密码框没出现,再扫一次"使用密码"链接(可能初次扫的时候页面还没渲染)
            log.warning("密码框 15s 未出现,再试一次找'使用密码'")
            self._dump_screenshot(page, "step2b-no-password-yet")
            try:
                use_pw = page.get_by_text(re.compile(r"使用密码|Use your password", re.I))
                if use_pw.first.is_visible(timeout=2000):
                    use_pw.first.click(timeout=3000)
                    self._human_wait(1500, 2500)
                    password_input.wait_for(state="visible", timeout=20_000)
                else:
                    raise PWTimeout("password input not visible and no 'Use password' link")
            except PWTimeout:
                raise RuntimeError(
                    "密码输入框未出现,且找不到'使用密码'链接。"
                    "可能 Microsoft 强制验证邮件,需要从 98faka.top 取验证码。"
                )
        self._human_type(password_input, account.password)
        log.info("已填密码,登录...")
        self._human_wait(400, 900)
        self._submit_form(page, password_input, re.compile(r"Sign in|登录", re.I), label="password-signin")
        self._human_wait(1500, 2500)
        self._dump_screenshot(page, "step3-after-password")

        # 密码后不再主动读 _read_ms_error —— Microsoft 页面 DOM 里常残留前一步的
        # 错误元素(隐藏但 aria-live 可见),会把残留文本误判为新错误。下游
        # _wait_for_project_with_handlers 会用 URL 跳转来判断登录是否真的成功。

        # === "让我们来保护你的帐户" / "保持登录状态?" 都可能出现 ===
        # 这俩可能其中之一出现也可能都出现,顺序也不固定。统一处理一段时间。
        self._handle_post_login_prompts(page)

        # === 同意权限页(如果出现)===
        try:
            page.get_by_role(
                "button", name=re.compile(r"Accept|Continue|接受|同意", re.I)
            ).first.click(timeout=5000)
            log.info("已同意权限")
            self._human_wait()
        except PWTimeout:
            pass

    def _handle_post_login_prompts(self, page: Page) -> None:
        """登录成功后 MS 可能弹一连串提示:保护账户/保持登录/通行密钥/通知/隐私....
        在 60 秒窗口内反复尝试匹配并点掉,直到 URL 离开 *.microsoft.com / *.live.com。
        """
        log.info("处理登录后弹窗(保护账户/保持登录/FIDO...)")
        deadline = time.time() + 60
        # role=button/link 用的较宽松,文本匹配单独用一个**严格锚定**的版本,
        # 避免误匹配 "Skip having to sign in every time. Learn more" 这种长描述
        skip_re = re.compile(
            r"跳过|Skip for now|Skip|Not now|Maybe later|稍后|以后再说"
            r"|スキップ"             # Japanese
            r"|건너뛰기|건너뜀"        # Korean
            r"|Saltar|Omitir"        # Spanish
            r"|Ignorer|Passer"       # French
            r"|Überspringen"         # German
            r"|Salta"                # Italian
            r"|Pular|Ignorar",       # Portuguese
            re.I,
        )
        skip_text_re = re.compile(
            r"^(跳过|Skip|Skip for now|Not now|Maybe later|稍后|以后再说)$",
            re.I,
        )
        yes_re = re.compile(r"^(Yes|是|はい|예|Sí|Oui|Ja|Sim)$", re.I)
        accept_re = re.compile(r"^(Accept|Continue|Allow|接受|同意|继续|Aceptar|Accepter|Akzeptieren)$", re.I)
        cancel_re = re.compile(r"^(取消|Cancel|キャンセル|취소|Cancelar|Annuler|Abbrechen)$", re.I)
        loop_count = 0

        while time.time() < deadline:
            url = page.url
            if not MS_RELATED_HOST_RE.search(url):
                log.info("已离开 Microsoft 域(URL: %s),弹窗处理结束", url)
                return

            loop_count += 1
            if loop_count <= 5 or loop_count % 5 == 0:
                self._dump_screenshot(page, f"prompt-loop-{loop_count}")

            clicked = False
            # FIDO 密钥设置页 (login.microsoft.com/consumers/fido/create) 优先点 "取消"
            is_fido = "fido" in url.lower() or "密钥" in self._page_text_preview(page)

            # 注意顺序:Yes / Accept 这种"主推按钮"必须在 text 跳过 之前,
            # 否则 KMSI 页 "Skip having to sign in every time" 的描述文字会被先点中
            click_order = [
                ("link 跳过", page.get_by_role("link", name=skip_re)),
                ("button 跳过", page.get_by_role("button", name=skip_re)),
                ("button 是(KMSI)", page.get_by_role("button", name=yes_re)),
                ("button Accept(Consent)", page.get_by_role("button", name=accept_re)),
                ("link Accept(Consent)", page.get_by_role("link", name=accept_re)),
                # text 兜底放最后,且用严格锚定(避免误中"Skip having to sign in every time"长描述)
                ("text 跳过", page.get_by_text(skip_text_re)),
            ]
            if is_fido:
                # 在 FIDO 页把"取消"放最前面
                click_order = [
                    ("button 取消(FIDO)", page.get_by_role("button", name=cancel_re)),
                    *click_order,
                ]

            for label, loc in click_order:
                try:
                    if loc.first.is_visible(timeout=600):
                        loc.first.click(timeout=2000)
                        log.info("点击: %s (URL: %s)", label, page.url)
                        self._human_wait(1200, 2000)
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                page.wait_for_timeout(1000)

        log.warning("60秒后还在 Microsoft 域,放弃自动处理")
        self._dump_screenshot(page, "prompt-loop-timeout")

    def _page_text_preview(self, page: Page, n: int = 300) -> str:
        try:
            return page.locator("body").inner_text(timeout=500)[:n]
        except Exception:
            return ""

    def _human_wait(self, lo: int = 250, hi: int = 700) -> None:
        time.sleep(random.uniform(lo, hi) / 1000.0)

    def _human_type(self, locator, text: str) -> None:
        """逐字符输入,模拟真人键盘速度。"""
        try:
            locator.click()
        except Exception:
            pass
        # 先清空
        try:
            locator.fill("")
        except Exception:
            pass
        locator.press_sequentially(text, delay=random.randint(60, 130))

    def _submit_form(self, page: Page, input_loc, button_re: re.Pattern, *, label: str) -> None:
        """提交 Microsoft 表单:优先按 Enter,失败再点按钮,再失败用 #idSIButton9。

        每次会等到 URL 改变或下一个元素出现,避免静默失败。
        """
        before_url = page.url
        # 1) 按 Enter:对 input[type=email/password] 最稳
        try:
            input_loc.press("Enter", timeout=3000)
        except Exception as e:
            log.warning("[%s] Enter 提交失败: %s", label, e)

        if self._wait_for_navigation_or_change(page, before_url, timeout_ms=4000):
            return

        log.info("[%s] Enter 没触发跳转,尝试点按钮", label)
        # 2) 点角色为 button 且文本匹配的元素
        for selector_desc, loc in [
            (f"role=button name={button_re.pattern}", page.get_by_role("button", name=button_re)),
            (f"text={button_re.pattern}", page.get_by_text(button_re)),
            ("#idSIButton9", page.locator("#idSIButton9")),
            ("input[type=submit]", page.locator("input[type='submit']")),
        ]:
            try:
                loc.first.click(timeout=5000)
                log.info("[%s] 点击成功: %s", label, selector_desc)
                if self._wait_for_navigation_or_change(page, before_url, timeout_ms=8000):
                    return
            except Exception as e:
                log.debug("[%s] %s 点击失败: %s", label, selector_desc, e)

        # 全部失败,截图便于排查
        self._dump_screenshot(page, label)
        raise RuntimeError(f"[{label}] 所有提交方式都失败")

    def _wait_for_navigation_or_change(self, page: Page, before_url: str, *, timeout_ms: int) -> bool:
        """等 URL 改变或密码框出现,任一发生即认为提交成功。"""
        try:
            page.wait_for_function(
                """([prev]) => location.href !== prev
                       || !!document.querySelector("input[type='password']:not([disabled])")""",
                arg=[before_url],
                timeout=timeout_ms,
            )
            return True
        except PWTimeout:
            return False

    def _maybe_skip_security_setup(self, page: Page) -> None:
        """跳过 'Help us protect your account' / '让我们来保护你的帐户' 安全信息设置页。

        页面有 '跳过(这是你最后一次可以跳过)' 链接,优先点它。
        """
        skip_re = re.compile(
            r"跳过|Skip for now|Looks good|Not now|Maybe later",
            re.I,
        )
        for selector_desc, loc in [
            ("link 跳过", page.get_by_role("link", name=skip_re)),
            ("button 跳过", page.get_by_role("button", name=skip_re)),
            ("text 跳过", page.get_by_text(skip_re)),
        ]:
            try:
                if loc.first.is_visible(timeout=1500):
                    loc.first.click(timeout=3000)
                    log.info("已点'跳过':%s", selector_desc)
                    page.wait_for_timeout(1500)
                    return
            except Exception:
                continue
        log.info("(没有出现安全设置页)")

    def _read_ms_error(self, page: Page) -> str | None:
        """检查 Microsoft 登录页面上是否有错误提示。"""
        for sel in ("#usernameError", "#passwordError", "[role='alert']", ".alert-error"):
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=500):
                    txt = loc.inner_text(timeout=500).strip()
                    if txt:
                        return txt
            except Exception:
                continue
        return None

    def _dump_screenshot(self, page: Page, label: str) -> None:
        try:
            logs_dir = Path("logs")
            logs_dir.mkdir(exist_ok=True)
            path = logs_dir / f"fail-{label}-{int(time.time())}.png"
            page.screenshot(path=str(path), full_page=True)
            log.warning("已保存失败截图: %s (URL: %s)", path, page.url)
        except Exception as e:
            log.warning("截图失败: %s", e)

    # --------------------------------------------------------------- project

    def _open_project(self, page: Page) -> None:
        """登录后落在 /projects,**点 "New project" 创建空白项目**,进入新画布。

        见 memory: project-per-account-workspace。
        不复用 Untitled 卡片(可能停留在 onboarding 引导页),每次都新建,环境干净。
        """
        # Flora 给新账号会自动跳进一个"onboarding 模板选择项目"(画面里有
        # "Hey, X. Pick one of these to get started" 三选模板,没有 + 加节点按钮),
        # 那个项目不能直接用。所以无论现在 URL 是什么,**强制回 /projects 然后新建一个干净的项目**。
        if "/access-denied" not in page.url:
            page.wait_for_timeout(2500)  # 让 Flora 的自动重定向尘埃落定

        log.info("当前 URL: %s,强制回 /projects 准备新建项目", page.url)
        try:
            page.goto("https://app.flora.ai/projects", wait_until="domcontentloaded", timeout=20_000)
        except PWTimeout:
            pass
        page.wait_for_timeout(2500)

        # 创建前快照所有项目链接 + 收集已知 projectId
        before_links = set(page.evaluate(
            "() => [...document.querySelectorAll(\"a[href*='/projects/']\")].map(a => a.getAttribute('href'))"
        ))
        seen_project_ids: set[str] = set()
        new_project_id_holder: list[str | None] = [None]
        pid_re = re.compile(r"projectId=([a-z0-9]+)")

        def on_req_capture_pid(req):
            m = pid_re.search(req.url)
            if m and "app.flora.ai/api" in req.url:
                pid = m.group(1)
                if pid not in seen_project_ids:
                    if seen_project_ids:
                        new_project_id_holder[0] = pid
                    seen_project_ids.add(pid)

        page.on("request", on_req_capture_pid)
        page.wait_for_timeout(500)
        log.info("创建前项目数: %d  已知 projectId: %d", len(before_links), len(seen_project_ids))

        log.info("创建新项目...")
        new_project_clicked = False
        new_project_re = re.compile(r"New project|新项目|创建项目|新建", re.I)

        # 策略 A:点 "New project" 按钮/链接
        for sel_desc, loc in [
            ("button New project", page.get_by_role("button", name=new_project_re)),
            ("link New project", page.get_by_role("link", name=new_project_re)),
            ("text New project", page.get_by_text(new_project_re)),
        ]:
            try:
                loc.first.click(timeout=3000)
                log.info("点击新建项目成功: %s", sel_desc)
                new_project_clicked = True
                break
            except Exception:
                continue

        # 策略 B:直接 goto /new (Flora 应该会自动建项目 + 跳到 /projects/<id>)
        if not new_project_clicked:
            log.warning("没找到 New project 按钮,改 goto /new")
            try:
                page.goto("https://app.flora.ai/new", wait_until="domcontentloaded", timeout=15_000)
                page.wait_for_timeout(2000)
                # 检查是否报错了
                if "error=" in page.url and "Failed" in page.url:
                    log.warning("/new 返回错误: %s", page.url)
                else:
                    new_project_clicked = True
                    log.info("/new 跳转成功,当前 URL: %s", page.url)
            except Exception as e:
                log.warning("goto /new 失败: %s", e)

        # 策略 D:点画布列表里已存在的 Untitled 卡片(进去再说)
        if not new_project_clicked:
            log.warning("没新建成功,尝试点现有项目卡片直接进...")
            try:
                page.goto("https://app.flora.ai/projects", wait_until="domcontentloaded", timeout=10_000)
                page.wait_for_timeout(2000)
                # 找含 Untitled 文本的卡片
                card = page.locator("text=Untitled").first
                card.click(timeout=4000)
                page.wait_for_timeout(2500)
                if re.search(r"/projects/[a-z0-9]{10,}", page.url):
                    new_project_clicked = True
                    log.info("已进入现有项目: %s", page.url)
            except Exception as e:
                log.warning("点现有卡片也失败: %s", e)

        # 策略 C:点侧边栏 Recent Projects / Workspace 旁边的 + 图标
        if not new_project_clicked:
            try:
                page.locator("text=Recent Projects").locator("xpath=following-sibling::*[1]").click(timeout=3000)
                new_project_clicked = True
                log.info("点了 Recent Projects 的 +")
            except Exception:
                pass

        if not new_project_clicked:
            self._dump_screenshot(page, "no-new-project-button")
            raise RuntimeError("找不到 'New project' 按钮也没法 goto /new")

        # 等新 projectId 在网络请求里出现
        log.info("等待新 projectId 从网络请求出现...")
        deadline = time.time() + 15
        while time.time() < deadline and not new_project_id_holder[0]:
            page.wait_for_timeout(500)

        if re.search(r"/projects/[a-z0-9]{10,}", page.url):
            log.info("New project 直接跳转到了画布: %s", page.url)
        elif new_project_id_holder[0]:
            target = f"https://app.flora.ai/projects/{new_project_id_holder[0]}"
            log.info("从网络拦截到新 projectId,goto: %s", target)
            page.goto(target, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
        else:
            after_links = page.evaluate(
                "() => [...document.querySelectorAll(\"a[href*='/projects/']\")].map(a => a.getAttribute('href'))"
            )
            new_links = [u for u in after_links if u not in before_links and re.search(r"/projects/[a-z0-9]{10,}", u)]
            if new_links:
                target = new_links[0]
                log.info("DOM 找到新链接,goto: %s", target)
                page.goto(f"https://app.flora.ai{target}" if target.startswith("/") else target,
                          wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
            else:
                log.warning("创建后没抓到新 projectId,刷新项目列表后取最新项目兜底")
                page.goto("https://app.flora.ai/projects", wait_until="domcontentloaded", timeout=20_000)
                page.wait_for_timeout(3000)
                fallback_links = page.evaluate(
                    "() => [...document.querySelectorAll(\"a[href*='/projects/']\")].map(a => a.getAttribute('href'))"
                )
                fallback_links = [u for u in fallback_links if u and re.search(r"/projects/[a-z0-9]{10,}", u)]
                if not fallback_links:
                    self._dump_screenshot(page, "no-new-project-id")
                    raise RuntimeError("创建后既没抓到新 projectId,也没找到项目链接")
                target = fallback_links[0]
                log.info("兜底进入项目: %s", target)
                page.goto(f"https://app.flora.ai{target}" if target.startswith("/") else target,
                          wait_until="domcontentloaded")
                page.wait_for_timeout(2500)

        page.remove_listener("request", on_req_capture_pid)

        if not re.search(r"/projects/[a-z0-9]{10,}", page.url):
            self._dump_screenshot(page, "not-in-project")
            raise RuntimeError(f"进项目后 URL 仍未变成 /projects/<id>: {page.url}")
        log.info("✅ 已进入新项目画布: %s", page.url)

        self._dump_screenshot(page, "step4-flora-canvas")
        log.info("Flora 画布 URL: %s", page.url)

        # 关掉可能的 onboarding 引导覆盖层(按 Esc + 点空白处)
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            page.mouse.click(700, 400)  # 点画布中央空白
            page.wait_for_timeout(800)
        except Exception:
            pass

        # 多种 Flora 画布痕迹,任一出现即认为加载完成
        selectors = [
            "canvas",
            ".react-flow",
            ".react-flow__renderer",
            "[data-testid='canvas']",
            "[class*='canvas' i]",
            "[class*='Canvas']",
            "[class*='editor' i]",
            "main",  # 兜底:有 main 元素也算页面已渲染
        ]
        try:
            page.wait_for_selector(
                ", ".join(selectors),
                timeout=20_000,
            )
            log.info("Flora 画布已加载 ✓")
        except PWTimeout:
            self._dump_screenshot(page, "step4-canvas-timeout")
            log.error("Flora 画布没出现,当前 URL: %s", page.url)
            log.error("浏览器保持 90 秒,你可手动检查是否要完成 Flora 首次引导/接受条款")
            page.wait_for_timeout(90_000)
            raise

    # ------------------------------------------------------------- generation

    def _generate_one(self, page: Page, task: Task) -> None:
        """单个素材任务:创建 3 个节点然后**停手等人工接线/生成**。

        新流程(2026-05-14 用户指定):
          1. Image 节点 + 上传参考图
          2. Text 节点 + OS 剪贴板粘贴 prompt(合成事件被 Flora 拒绝,见 memory)
          3. Video 节点(Seedance 2)
          后面接线 + Generate + 下载由用户手工完成。
        """
        # 解掉新项目 onboarding 的"What are you working on?"覆盖层,
        # 否则它会覆盖左侧 + 按钮的 click 命中区域
        self._dismiss_onboarding(page)

        # ---------- 1) Image 节点 ----------
        log.info("===== [1/3] 添加 Image 节点 =====")
        self._add_node(page, "Image")
        self._dump_screenshot(page, "step5a-image-node-added")

        # 上传参考图
        image_paths = [str(p) for p in task.reference_images]
        log.info("上传 %d 张参考图: %s", len(image_paths), [p.name for p in task.reference_images])
        file_inputs = page.locator("input[type='file']")
        try:
            file_inputs.last.wait_for(state="attached", timeout=10_000)
        except PWTimeout:
            log.warning("Image 节点没有 file input,跳过上传")
        else:
            try:
                file_inputs.last.set_input_files(image_paths)
            except Exception as e:
                log.warning("批量上传失败 (%s),改逐张", e)
                for img in image_paths:
                    page.locator("input[type='file']").last.set_input_files(img)
                    page.wait_for_timeout(700)
            page.wait_for_timeout(1500)
        self._dump_screenshot(page, "step5b-images-uploaded")

        # ---------- 2) Text 节点:按 T 快捷键创建,点进文字框粘贴 ----------
        log.info("===== [2/3] 添加 Text 节点 (按 T) =====")
        # 先点画布空白区域确保焦点在画布
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        self._human_wait(200, 400)
        page.mouse.click(300, 300)
        self._human_wait(400, 700)
        page.keyboard.press("t")
        self._human_wait(250, 450)
        page.mouse.click(320, 320)
        self._human_wait(1200, 1800)
        if not self._latest_text_node_rect(page):
            log.warning("T 快捷键未创建 Text 节点,改用节点面板添加 Text")
            self._add_node(page, "Text")
            self._human_wait(250, 450)
            page.mouse.click(320, 320)
            self._human_wait(1200, 1800)
        self._dump_screenshot(page, "step6a-text-node-added")

        # 点文字框并用 OS 剪贴板粘贴 prompt
        log.info("OS 剪贴板粘贴提示词(%d 字)...", len(task.prompt))
        try:
            self._paste_into_last_textarea(page, task.prompt)
        except Exception as e:
            log.warning("粘贴 prompt 失败: %s", e)
        self._dump_screenshot(page, "step6b-prompt-pasted")

        # 按 Escape 退出文字编辑状态,回到画布
        page.keyboard.press("Escape")
        self._human_wait(400, 700)

        # ---------- 3) Video 节点 ----------
        log.info("===== [3/3] 添加 Video (Seedance 2) 节点 =====")
        page.keyboard.press("Escape")
        self._human_wait(200, 400)
        page.mouse.click(1030, 420)
        self._human_wait(300, 600)
        page.keyboard.press("v")
        self._human_wait(250, 450)
        page.mouse.click(1040, 430)
        self._human_wait(1000, 1500)
        self._dump_screenshot(page, "step7-video-node-added")

        # 双击打开视频节点面板
        log.info("双击打开 Video 节点面板...")
        try:
            # 找最后添加的节点元素双击
            node_els = page.locator("[data-node-id], [data-id], .react-flow__node").all()
            if node_els:
                node_els[-1].dblclick(timeout=3000)
                self._human_wait(800, 1200)
                log.info("已双击打开 Video 节点")
        except Exception as e:
            log.warning("双击 Video 节点失败: %s", e)
        self._dump_screenshot(page, "step7b-video-node-opened")
        log.info("请手动连接: 图片->Text、图片->Video、Text->Video, 然后点击 Generate。脚本会等待生成完成并下载。")
        log.info("===== [4/6] 填写 Video 提示词 =====")

        log.info("===== [5/6] 自动连线 =====")

        log.info("===== [6/6] 触发生成并下载 =====")
        self._wait_for_generation(page)
        self._download_video(page, task)

        log.info("✅ 三个节点已创建。等待人工接线 + 点 Generate + 下载")

    def _canvas_nodes(self, page: Page) -> list[dict]:
        return page.evaluate(
            """() => [...document.querySelectorAll('.react-flow__node, [data-node-id], [data-id]')]
                .map((el, index) => {
                    const r = el.getBoundingClientRect();
                    const text = (el.innerText || el.textContent || '').trim();
                    return {
                        index,
                        text,
                        hasImg: !!el.querySelector('img'),
                        hasVideo: !!el.querySelector('video'),
                        hasTextarea: !!el.querySelector('textarea'),
                        box: {
                            left: r.left, right: r.right, top: r.top, bottom: r.bottom,
                            width: r.width, height: r.height,
                            cx: r.left + r.width / 2, cy: r.top + r.height / 2,
                        },
                    };
                })
                .filter(n => n.box.width > 40 && n.box.height > 40 && n.box.right > 0 && n.box.bottom > 0)"""
        )

    def _find_flow_node(self, page: Page, kind: str) -> dict:
        nodes = self._canvas_nodes(page)
        if kind == "image":
            candidates = [n for n in nodes if n["hasImg"] and "Video" not in n["text"]]
        elif kind == "text":
            candidates = [n for n in nodes if re.search(r"(^|\s)Text(\s|$)", n["text"], re.I)]
        elif kind == "video":
            candidates = [n for n in nodes if re.search(r"(^|\s)Video(\s|$)", n["text"], re.I)]
        else:
            candidates = []
        if not candidates:
            log.warning("当前节点快照: %s", nodes)
            raise RuntimeError(f"找不到 {kind} 节点")
        return candidates[-1]

    def _drag_wire_between(self, page: Page, source: dict, target: dict, *, target_y_ratio: float = 0.5) -> None:
        sb = source["box"]
        tb = target["box"]
        sx = sb["right"] - 6
        sy = sb["cy"]
        tx = tb["left"] + 8
        ty = tb["top"] + tb["height"] * target_y_ratio
        log.info("拖线: (%.0f, %.0f) -> (%.0f, %.0f)", sx, sy, tx, ty)
        page.mouse.move(sx, sy)
        page.wait_for_timeout(120)
        page.mouse.down()
        page.wait_for_timeout(120)
        page.mouse.move((sx + tx) / 2, sy, steps=8)
        page.mouse.move((sx + tx) / 2, ty, steps=8)
        page.mouse.move(tx, ty, steps=12)
        page.wait_for_timeout(160)
        page.mouse.up()
        page.wait_for_timeout(900)

    def _connect_image_text_video(self, page: Page) -> None:
        image = self._find_flow_node(page, "image")
        text = self._find_flow_node(page, "text")
        video = self._find_flow_node(page, "video")
        before = page.locator(".react-flow__edge").count()
        self._drag_wire_between(page, image, text, target_y_ratio=0.5)
        self._drag_wire_between(page, image, video, target_y_ratio=0.35)
        self._drag_wire_between(page, text, video, target_y_ratio=0.75)
        after = page.locator(".react-flow__edge").count()
        log.info("连线前后 edge 数: %d -> %d", before, after)

    def _click_generate(self, page: Page) -> None:
        generate_re = re.compile(r"Generate|生成", re.I)
        for desc, loc in [
            ("button Generate", page.get_by_role("button", name=generate_re)),
            ("text Generate", page.get_by_text(generate_re)),
        ]:
            try:
                if loc.last.is_visible(timeout=2500):
                    loc.last.click(timeout=5000)
                    log.info("已点击 Generate: %s", desc)
                    page.wait_for_timeout(2000)
                    return
            except Exception:
                continue
        self._dump_screenshot(page, "no-generate-button")
        raise RuntimeError("找不到 Generate 按钮")

    def _download_video(self, page: Page, task: Task) -> None:
        """生成完成后直接抓 <video> 直链下载并保存到 ./output/。"""
        output_dir = Path("output")
        output_dir.mkdir(exist_ok=True)
        target = output_dir / f"{task.name}-{int(time.time())}.mp4"

        log.info("直接抓视频直链下载到 %s", target)

        # Flora 的下载按钮经常被参数面板/播放遮罩挡住；直接读 video.currentSrc 最稳。
        src = None
        try:
            page.locator("video").last.wait_for(state="attached", timeout=10_000)
            src = page.evaluate(
                """() => {
                    const videos = [...document.querySelectorAll('video')];
                    for (const v of videos.reverse()) {
                        const src = v.currentSrc || v.src || v.getAttribute('src');
                        if (src && !src.startsWith('blob:')) return src;
                        const source = v.querySelector('source[src]');
                        if (source && source.src && !source.src.startsWith('blob:')) return source.src;
                    }
                    return null;
                }"""
            )
        except Exception as e:
            log.warning("读取 video src 失败: %s", e)

        if src:
            try:
                log.info("从 <video src> 抓直链: %s", src)
                blob = page.evaluate(
                    """async (url) => {
                        const r = await fetch(url);
                        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
                        const buf = await r.arrayBuffer();
                        return Array.from(new Uint8Array(buf));
                    }""",
                    src,
                )
                target.write_bytes(bytes(blob))
                if target.stat().st_size > 1024 * 100:
                    log.info("视频已下载(直链 fetch): %s, %d bytes", target, target.stat().st_size)
                    return
                log.warning("直链下载文件过小: %s, %d bytes", target, target.stat().st_size)
            except Exception as e:
                log.warning("直链 fetch 下载失败: %s", e)

        # 备用方式: 只有直链失败时才尝试 UI 下载按钮。
        try:
            with page.expect_download(timeout=15_000) as dl_info:
                for label, loc in [
                    ("Download button", page.get_by_role("button", name=re.compile(r"Download|下载", re.I))),
                    ("Download menuitem", page.get_by_role("menuitem", name=re.compile(r"Download|下载", re.I))),
                ]:
                    try:
                        loc.last.click(timeout=3000)
                        log.info("已点击 %s", label)
                        break
                    except Exception:
                        continue
                else:
                    raise RuntimeError("找不到 Download 按钮")
            download = dl_info.value
            download.save_as(str(target))
            log.info("视频已下载(UI 备用): %s", target)
            return
        except Exception as e:
            log.warning("UI 下载备用也失败: %s", e)

        log.error("视频下载失败,请手动下载; 素材已保留供下次重试")

    def _dismiss_onboarding(self, page: Page) -> None:
        """新项目画布会有 "What are you working on?" 覆盖层,挡住左侧 + 按钮的命中区域。
        策略:按 Escape + 在左下角空白处点一下,把焦点切到画布本身。
        """
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
        except Exception:
            pass
        # 点画布左下角空白(避开中央 onboarding 浮层)
        try:
            page.mouse.click(200, 800)
            page.wait_for_timeout(400)
        except Exception:
            pass

    def _open_node_palette(self, page: Page) -> None:
        """打开节点选择面板,**必须真的看到搜索输入框**才算成功。

        关键陷阱:中央 "Make anything..." 输入框旁边也有一个 +,aria-label 可能撞名。
        所以我们只点 **左侧栏顶部** 的 + 按钮(位置严格约束在 x<80 的左侧条)。
        """
        # 先确保画布有焦点(点左下角空白处,避开中央 onboarding)
        try:
            page.mouse.click(200, 800)
            page.wait_for_timeout(300)
        except Exception:
            pass

        search_re = re.compile(r"Search nodes|Search and add|Add Node|Search\b", re.I)

        def palette_visible() -> bool:
            try:
                return page.locator(
                    "input[placeholder*='Search nodes' i], "
                    "input[placeholder*='Search and add' i], "
                    "input[placeholder*='Search' i]"
                ).first.is_visible(timeout=600)
            except Exception:
                return False

        # 策略 1:左侧栏内 aria-label='Add node' 的按钮(约束在 x<80)
        for sel_desc, loc in [
            ("aria-label=Add node (left sidebar)",
             page.locator("[aria-label='Add node' i]")),
            ("aria-label*=Add node",
             page.locator("[aria-label*='Add node' i]")),
            ("button title=Add node",
             page.locator("button[title*='Add node' i]")),
        ]:
            try:
                candidates = loc.all()
            except Exception:
                candidates = []
            for cand in candidates:
                try:
                    bb = cand.bounding_box()
                    if not bb:
                        continue
                    # 左侧栏的 + 应该在 x<80。中央输入框的 + 会在 x>1000。
                    if bb["x"] > 100:
                        continue
                    cand.click(timeout=2500)
                    page.wait_for_timeout(700)
                    if palette_visible():
                        log.info("点开节点面板: %s @ (%d,%d)", sel_desc, int(bb["x"]), int(bb["y"]))
                        return
                except Exception:
                    continue

        # 策略 2:按坐标点左上 + 按钮(根据截图,左侧栏 + 在 (43, ~315))
        for x, y in [(43, 315), (45, 312), (40, 320), (43, 270), (45, 225)]:
            try:
                page.mouse.click(x, y)
                page.wait_for_timeout(700)
                if palette_visible():
                    log.info("按坐标 (%d, %d) 点开节点面板", x, y)
                    return
            except Exception:
                continue

        self._dump_screenshot(page, "palette-not-open")
        raise RuntimeError("打不开节点面板(搜索框未出现)")

    def _add_node(self, page: Page, query: str) -> None:
        """打开节点面板,搜 `query`,点第一个结果,关闭面板。"""
        self._open_node_palette(page)
        self._search_and_add_node(page, query)
        page.wait_for_timeout(1200)
        # 面板可能没关,按 Escape 关一下(避免下一次开节点时叠加)
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:
            pass

    def _search_and_add_node(self, page: Page, query: str) -> None:
        """在已打开的节点面板里搜并点第一个结果。"""
        search = page.locator(
            "input[placeholder*='Search nodes' i], "
            "input[placeholder*='Search and add' i], "
            "input[placeholder*='Search' i], "
            "input[type='search']"
        ).first
        search.wait_for(state="visible", timeout=5000)
        search.click()
        # 用真人节奏输入(fill 同步事件也可,这里搜索框接受合成事件)
        self._human_type(search, query)
        page.wait_for_timeout(700)
        # 优先点 role=option/menuitem;否则点文本
        for desc, loc in [
            ("role=option", page.get_by_role("option", name=re.compile(re.escape(query), re.I))),
            ("role=menuitem", page.get_by_role("menuitem", name=re.compile(re.escape(query), re.I))),
            ("role=button", page.get_by_role("button", name=re.compile(re.escape(query), re.I))),
            ("text", page.get_by_text(re.compile(re.escape(query), re.I))),
        ]:
            try:
                loc.first.click(timeout=3000)
                log.info("选中节点 (%s): %s", desc, query)
                return
            except Exception:
                continue
        # 全部失败:按 Enter 让面板选高亮项
        try:
            search.press("Enter", timeout=2000)
            log.info("Enter 选中节点: %s", query)
        except Exception:
            raise RuntimeError(f"搜到 '{query}' 但点不到结果")

    # ----------------------------------------------------- OS 剪贴板粘贴 prompt

    def _set_os_clipboard(self, text: str) -> None:
        """把文本写入 Windows OS 剪贴板(PowerShell Set-Clipboard,UTF-8 安全)。

        Flora 的 textarea 拒绝合成 paste/keydown 事件,只接受真实 OS 粘贴。
        见 memory: project_flora_textarea_paste。
        """
        import subprocess
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".txt", delete=False
        ) as f:
            f.write(text)
            tmp = f.name
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-Content -LiteralPath '{tmp}' -Raw -Encoding UTF8 | "
                 f"Set-Clipboard"],
                check=True,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _latest_text_node_rect(self, page: Page) -> dict | None:
        return page.evaluate(
            """() => {
                const nodes = [...document.querySelectorAll('.react-flow__node, [data-node-id], [data-id]')]
                    .filter(n => {
                        const r = n.getBoundingClientRect();
                        return r.width > 20 && r.height > 20 && r.bottom > 0 && r.right > 0;
                    });
                const textNodes = nodes.filter(n => {
                    const s = (n.innerText || n.textContent || '').trim();
                    return /(^|\\s)Text(\\s|$)/i.test(s);
                });
                const n = textNodes[textNodes.length - 1];
                if (!n) return null;
                const r = n.getBoundingClientRect();
                return {
                    x: r.left + Math.min(Math.max(r.width * 0.45, 35), r.width - 15),
                    y: r.top + Math.min(Math.max(r.height * 0.42, 35), r.height - 15),
                    w: r.width,
                    h: r.height,
                    text: (n.innerText || n.textContent || '').slice(0, 80),
                };
            }"""
        )

    def _paste_into_latest_text_node(self, page: Page, text: str) -> bool:
        """Click the newest Text node and paste into its editor."""
        rect = self._latest_text_node_rect(page)
        if not rect:
            log.warning("未定位到 Text 节点,走旧 textarea fallback")
            return False

        log.info("点击最新 Text 节点输入区: %s", rect)
        page.mouse.dblclick(rect["x"], rect["y"])
        self._human_wait(500, 900)

        editable = page.locator(
            "textarea:not([placeholder*='comment' i]):not([placeholder*='留言' i]):visible, "
            "[contenteditable='true']:visible"
        ).last
        try:
            editable.wait_for(state="visible", timeout=3000)
            editable.click(timeout=3000)
        except PWTimeout:
            log.info("没有显式输入框,直接对已选中的 Text 节点粘贴")

        page.keyboard.press("Control+a")
        self._human_wait(100, 250)
        page.keyboard.press("Control+v")
        self._human_wait(800, 1200)

        ok = page.evaluate(
            """(needle) => {
                const active = document.activeElement;
                const activeText = active ? (active.value || active.innerText || active.textContent || '') : '';
                const nodes = [...document.querySelectorAll('.react-flow__node, [data-node-id], [data-id]')];
                const lastText = nodes.reverse().find(n => /(^|\\s)Text(\\s|$)/i.test(n.innerText || n.textContent || ''));
                const nodeText = lastText ? (lastText.innerText || lastText.textContent || '') : '';
                return activeText.includes(needle) || nodeText.includes(needle);
            }""",
            text[:30],
        )
        log.info("Text 粘贴校验: %s", ok)
        return bool(ok)

    def _paste_into_last_textarea(self, page: Page, text: str) -> None:
        """在画布上最后一个 textarea(刚加的 Text 节点)粘贴文本。"""
        self._set_os_clipboard(text)
        self._human_wait(300, 600)
        if not self._paste_into_latest_text_node(page, text):
            raise RuntimeError("没有定位到可粘贴的 Text 节点")
        return
        # 排除 "Leave a comment" 之类的隐藏评论框,找可见的 textarea
        ta = page.locator(
            "textarea:not([placeholder*='comment' i]):not([placeholder*='留言' i]):visible"
        ).last
        try:
            ta.wait_for(state="visible", timeout=5000)
        except PWTimeout:
            # fallback: 点画布空白处让 Text 节点获焦,再找
            page.mouse.click(600, 400)
            self._human_wait(500, 800)
            ta = page.locator(
                "textarea:not([placeholder*='comment' i]):visible"
            ).last
            ta.wait_for(state="visible", timeout=5000)
        ta.click()
        self._human_wait(200, 500)
        # 全选(确保清空)+ 粘贴
        page.keyboard.press("Control+a")
        self._human_wait(100, 250)
        page.keyboard.press("Control+v")
        self._human_wait(800, 1200)

    def _wait_for_generation(self, page: Page) -> None:
        deadline = time.time() + self.generation_timeout_s
        log.info("等待视频生成完成(最多 %ds)...", self.generation_timeout_s)
        while time.time() < deadline:
            # 完成的标志:节点出现 <video> 元素 / "Download" 按钮 / 状态变为 done
            try:
                if page.locator("video").last.is_visible(timeout=1000):
                    log.info("生成完成 ✓")
                    return
            except PWTimeout:
                pass
            try:
                if page.get_by_role("button", name="Download").last.is_visible(timeout=1000):
                    log.info("生成完成 ✓ (Download 按钮已出现)")
                    return
            except PWTimeout:
                pass
            page.wait_for_timeout(2000)
        raise TimeoutError("视频生成超时")
