"""端到端 API runner:Patchright 登录拿 cookies → 走 Flora 内部 API → 下载视频。

用法(在 run.py 里加 --api-mode 调用):
    runner = ApiRunner(account, tasks, output_dir=Path("output"))
    runner.run()
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from .accounts import Account
from .api_client import (
    FloraClient,
    GenerationProgress,
    SEEDANCE_REFERENCE_ENDPOINT,
    cookies_from_playwright,
    save_cookies,
)
from .flora_bot import FloraBot
from .materials import Task

log = logging.getLogger(__name__)


class ApiRunner:
    def __init__(
        self,
        bot: FloraBot,
        account: Account,
        tasks: list[Task],
        *,
        output_dir: Path = Path("output"),
        keys_dir: Path = Path("keys"),
    ) -> None:
        self.bot = bot
        self.account = account
        self.tasks = tasks
        self.output_dir = output_dir
        self.keys_dir = keys_dir

    def run(self, *, keep_open_s: int = 0) -> None:
        """全 UI 模式:Patchright 走完整流程(添加节点 + 上传图 + 填 prompt + 生成 + 下载)。

        Flora 的 Liveblocks 状态需要 Video 节点有 parent image 节点 + 边连接,
        纯 API 调 /api/workflow/generate 会 validate fail。所以全程走 UI 触发,
        让 Flora 前端帮我们建好节点 + 连线,我们只在最后抓视频 URL 下载。
        """
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw, self.bot._context_for(pw, self.account) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            self.bot._wire_network_logging(page, self.account)
            had_error = False
            try:
                self.bot._ensure_logged_in(page, self.account)
                self.bot._open_project(page)

                cookies = cookies_from_playwright(ctx)
                log.info("已导出 %d 个 flora.ai cookies", len(cookies))
                cookie_file = self.keys_dir / f"{self.account.slug}.cookies.json"
                save_cookies(cookies, cookie_file)

                for idx, task in enumerate(self.tasks, 1):
                    log.info("=" * 60)
                    log.info("[%s] 任务 %d/%d: %s", self.account.email, idx, len(self.tasks), task.name)
                    try:
                        video_url = self._full_ui_generate(page, task)
                        if video_url:
                            target = self._task_output_path(task)
                            self._download_video_url(page, video_url, target)
                            log.info("✅ 视频已下载: %s", target)
                    except Exception as e:
                        log.exception("任务 %s 失败: %s", task.name, e)
                        had_error = True
            except Exception as e:
                log.exception("致命错误: %s", e)
                had_error = True

            # 出错或显式要求时,浏览器保持打开,便于人工接管/调试
            wait = keep_open_s if keep_open_s > 0 else (1800 if had_error else 0)
            if wait > 0:
                log.warning(">>> 浏览器保持 %d 秒(失败 %s 或 --keep-open)<<<",
                            wait, "✓" if had_error else "✗")
                log.warning(">>> 你可以手动操作浏览器,或 Ctrl+C 提前退出 <<<")
                try:
                    page.wait_for_timeout(wait * 1000)
                except KeyboardInterrupt:
                    log.info("收到 Ctrl+C,退出")

    def _full_ui_generate(self, page, task) -> str | None:
        """半自动流程 —— 脚本做无聊的部分,人画连线 + 点 Generate。

        脚本:
        1. 加 Video 节点(V 键)
        2. set_input_files 上传图片(自动创建 Image 节点)
        3. 把 prompt 粘贴到 Video 节点的 textarea
        4. 暂停等人工:画连线 + 点 Generate
        5. 等 <video> 元素出现 → 抓 src
        6. 下载视频(由 runner 的 _download_video_url 负责)
        """
        import re as _re

        # === 1. 加 Video 节点 ===
        log.info("[1/5] 打开 + 面板加 Video 节点...")
        before_nodes = self._snapshot_rf_nodes(page)
        self.bot._open_node_palette(page)
        page.wait_for_timeout(1200)
        page.keyboard.press("v")
        page.wait_for_timeout(2500)
        # 关掉可能的面板,让画布有焦点
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        self.bot._dump_screenshot(page, "ui-step1-video-node")

        # === 2. 上传图片(会自动建 Image 节点) ===
        log.info("[2/5] 上传 %d 张参考图...", len(task.reference_images))
        try:
            page.locator("input[type='file']").last.set_input_files(
                [str(p) for p in task.reference_images]
            )
        except Exception as e:
            log.warning("批量上传失败 (%s),改逐张", e)
            for img in task.reference_images:
                try:
                    page.locator("input[type='file']").last.set_input_files(str(img))
                    page.wait_for_timeout(1500)
                except Exception as e2:
                    log.warning("单张上传失败 %s: %s", img.name, e2)
        page.wait_for_timeout(4500)
        self.bot._dump_screenshot(page, "ui-step2-images-uploaded")

        # === 3. 加独立 Text 节点 ===
        # Flora 在画布有焦点时,T 是全局快捷键直接加 Text 节点(不用先开面板)
        log.info("[3/5] 加 Text 节点...")
        before_text = self._snapshot_rf_nodes(page)
        before_text_ids = {n["id"] for n in before_text}

        # 先点画布空白区给焦点(避开节点位置)
        try:
            page.locator("body").click(position={"x": 200, "y": 700}, timeout=2000)
            page.wait_for_timeout(400)
        except Exception:
            pass

        # 全局快捷键 T(不打开面板)
        page.keyboard.press("t")
        page.wait_for_timeout(2500)

        after_text = self._snapshot_rf_nodes(page)
        new_text_nodes = [n for n in after_text if n["id"] not in before_text_ids]
        log.info("新增节点(应为 Text): %d 个", len(new_text_nodes))

        # 若没创建成功,再用面板试一次
        if not new_text_nodes:
            log.warning("T 快捷键没起作用,改打开面板再试")
            self.bot._open_node_palette(page)
            page.wait_for_timeout(1200)
            # 在面板里点 Text 选项
            for sel in [
                "[role='option']:has-text('Text')",
                "[role='menuitem']:has-text('Text')",
                "button:has-text('Text')",
                "div[role]:has-text('Text')",
            ]:
                try:
                    loc = page.locator(sel).first
                    if loc.is_visible(timeout=1500):
                        loc.click(timeout=2500)
                        log.info("已在面板里点 Text: %s", sel)
                        page.wait_for_timeout(2500)
                        break
                except Exception:
                    continue
            after_text2 = self._snapshot_rf_nodes(page)
            new_text_nodes = [n for n in after_text2 if n["id"] not in before_text_ids]
            log.info("再次尝试后新增节点: %d 个", len(new_text_nodes))

        self.bot._dump_screenshot(page, "ui-step3-text-node-added")

        # === 4. 点击 Text 节点 textarea + 用 OS 剪贴板 + Ctrl+V 真粘贴 ===
        log.info("[4/5] 填提示词到 Text 节点(%d 字)...", len(task.prompt))
        page.wait_for_timeout(2000)

        # 步骤 a:JS 找 Text 节点 textarea + 滚动到可见,返回 ElementHandle
        text_node_id = new_text_nodes[0]["id"] if new_text_nodes else None
        ta_handle = page.evaluate_handle(
            """(textNodeId) => {
                let ta = null;
                if (textNodeId) {
                    for (const sel of [
                        `[data-id="${textNodeId}"]`,
                        `[data-node-id="${textNodeId}"]`,
                    ]) {
                        const node = document.querySelector(sel);
                        if (node) {
                            ta = node.querySelector('textarea') ||
                                 node.querySelector('[contenteditable="true"]');
                            if (ta) break;
                        }
                    }
                }
                if (!ta) {
                    const allTas = [...document.querySelectorAll('textarea')];
                    if (allTas.length > 0) ta = allTas[allTas.length - 1];
                }
                if (!ta) {
                    const eds = [...document.querySelectorAll('[contenteditable="true"]')];
                    if (eds.length > 0) ta = eds[eds.length - 1];
                }
                if (ta) ta.scrollIntoView({block: 'center'});
                return ta;
            }""",
            text_node_id,
        )
        element = ta_handle.as_element()
        if not element:
            self.bot._dump_screenshot(page, "ui-step4-no-text-node-textarea")
            raise RuntimeError("找不到 Text 节点的 textarea")
        log.info("找到 Text textarea/contenteditable")

        # 步骤 b:真实 click(像真人,触发 focus)
        element.click()
        page.wait_for_timeout(500)

        # 步骤 c:把 prompt 放进 OS Windows 剪贴板(UTF-16LE,支持中文)
        import subprocess
        try:
            proc = subprocess.Popen(
                ["clip"], stdin=subprocess.PIPE, shell=True,
            )
            proc.communicate(input=task.prompt.encode("utf-16-le"))
            log.info("已写入 OS 剪贴板(%d 字符)", len(task.prompt))
        except Exception as e:
            log.warning("OS clip 写入失败: %s,fallback PowerShell", e)
            try:
                subprocess.run(
                    ["powershell", "-Command", "Set-Clipboard", "-Value", task.prompt],
                    check=True, timeout=5,
                )
                log.info("已用 PowerShell 写入剪贴板")
            except Exception as e2:
                log.error("剪贴板写入两种都失败: %s", e2)
                raise

        # 步骤 d:真键盘 Ctrl+V 粘贴(系统级,React 100% 收到)
        page.keyboard.press("Control+v")
        page.wait_for_timeout(800)
        log.info("已按 Ctrl+V")

        # 验证
        check = page.evaluate(
            """() => {
                const allTas = [...document.querySelectorAll('textarea')];
                const last = allTas[allTas.length - 1];
                return last ? {value_len: last.value.length,
                               preview: last.value.slice(0, 80)} : null;
            }"""
        )
        log.info("最终 textarea 状态: %s", check)
        self.bot._dump_screenshot(page, "ui-step4-prompt-filled")

        # === 5. 暂停等人工连线 + 点 Generate,然后扫视频 URL ===
        log.info("=" * 70)
        log.info(">>> 脚本已经准备好:")
        log.info(">>>   - Video 节点(空白,待连接)")
        log.info(">>>   - 2 个 Image 节点(参考图已上传)")
        log.info(">>>   - 1 个 Text 节点(提示词已填好)")
        log.info(">>> 请人工:")
        log.info(">>>   1. 画连线:Text 节点 → Video 节点 的 T Prompt 输入")
        log.info(">>>   2. 画连线:Image 节点 1 → Video 节点 的 Image 0/2 输入")
        log.info(">>>   3. 画连线:Image 节点 2 → Video 节点 的 Image 0/2 输入")
        log.info(">>>   4. 点 Video 节点的 Generate 按钮")
        log.info(">>> 脚本持续 polling,看到 <video> 就自动下载视频")
        log.info("=" * 70)
        log.info("[5/5] 等视频出现(最多 30 分钟)...")
        deadline = time.time() + 30 * 60
        video_url = None
        while time.time() < deadline:
            try:
                src = page.evaluate("""
                    () => {
                        const v = document.querySelector('video[data-id="video-block-result"]')
                              || document.querySelector('video');
                        if (!v) return null;
                        return v.src || v.currentSrc
                            || v.querySelector('source')?.src
                            || null;
                    }
                """)
                if src and src.startswith("http"):
                    log.info("✓ 视频 URL: %s", src)
                    video_url = src
                    break
            except Exception:
                pass
            page.wait_for_timeout(5000)

        self.bot._dump_screenshot(page, "ui-step6-final")
        if not video_url:
            log.error("视频元素 6 分钟没出现")
            return None
        return video_url

    def _snapshot_rf_nodes(self, page) -> list[dict]:
        """快照画布上所有节点 + bounding box。

        Flora 不一定是 React Flow,试多种选择器。识别"节点"的策略:
        - 任何包含 <img> 或 <video> 或 <textarea> 的固定大小 div(canvas 上漂浮)
        - 用 data-id / id 含 UUID 作为节点 ID
        - role=button / data-* 也尝试
        """
        return page.evaluate(r"""
            () => {
                const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
                const out = [];
                // 试多种节点容器选择器
                const sels = [
                    '.react-flow__node',
                    '[data-id]',
                    '[class*="node-" i]',
                    '[class*="Node" i]',
                ];
                const seen = new Set();
                for (const sel of sels) {
                    document.querySelectorAll(sel).forEach(el => {
                        const id = el.getAttribute('data-id') || el.id || '';
                        if (!UUID.test(id)) return;
                        if (seen.has(id)) return;
                        seen.add(id);
                        const r = el.getBoundingClientRect();
                        if (r.width < 30 || r.height < 30) return;  // 不是节点
                        // 节点种类:有 img 算 image,有 textarea+video header 算 video
                        const hasImg = !!el.querySelector('img');
                        const hasVideo = !!el.querySelector('video');
                        const hasTextarea = !!el.querySelector('textarea');
                        const text = (el.innerText || '').slice(0, 60);
                        out.push({
                            id: id,
                            box: {x: r.x, y: r.y, w: r.width, h: r.height,
                                  left: r.left, right: r.right,
                                  top: r.top, bottom: r.bottom,
                                  cx: r.x + r.width/2, cy: r.y + r.height/2},
                            hasImg, hasVideo, hasTextarea, text,
                        });
                    });
                }
                return out;
            }
        """)

    def _draw_wire(self, page, from_node: dict, to_node: dict):
        """从 from_node 的右边缘中部拖到 to_node 的左边缘上部(Image 输入 socket 大致位置)。

        Flora 画布的 + socket 看起来在节点左边缘外面一点。
        """
        fb = from_node["box"]
        tb = to_node["box"]
        # 起点:from 节点右侧中部(image 节点的 output)
        sx = fb["right"] + 5
        sy = fb["cy"]
        # 终点:to 节点左侧上部(Video 节点的 Image 输入 socket "+" 大致位置)
        # 看用户截图,Image socket 是节点左外 ~5px,垂直靠上 ~30-50px
        tx = tb["left"] - 5
        ty = tb["top"] + 40

        log.info("画线 (%d,%d) → (%d,%d)", int(sx), int(sy), int(tx), int(ty))

        # 拟人化分段拖拽
        page.mouse.move(sx, sy)
        page.wait_for_timeout(200)
        page.mouse.down()
        page.wait_for_timeout(100)
        steps = 20
        for i in range(1, steps + 1):
            mx = sx + (tx - sx) * i / steps
            my = sy + (ty - sy) * i / steps
            page.mouse.move(mx, my, steps=2)
        # 微抖一下确保 hover 检测到 target
        page.mouse.move(tx + 2, ty + 1, steps=3)
        page.mouse.move(tx, ty, steps=3)
        page.wait_for_timeout(150)
        page.mouse.up()
        page.wait_for_timeout(600)

    def _download_video_url(self, page, url: str, dest):
        """优先用 page.evaluate fetch(带 cookie),fallback urllib。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = page.evaluate(
                """async (u) => {
                    const r = await fetch(u, {credentials: 'include'});
                    const buf = await r.arrayBuffer();
                    return Array.from(new Uint8Array(buf));
                }""",
                url,
            )
            dest.write_bytes(bytes(data))
            log.info("下载完成 (page fetch): %d bytes", dest.stat().st_size)
            return
        except Exception as e:
            log.warning("page fetch 失败: %s,fallback urllib", e)
        import urllib.request
        with urllib.request.urlopen(url, timeout=180) as r:
            dest.write_bytes(r.read())
        log.info("下载完成 (urllib): %d bytes", dest.stat().st_size)

    def _create_seedance_node(self, page) -> str:
        """在画布上加 Video 节点(API 会把它设置成 Seedance 2.0 Reference),返回 nodeId。

        策略 (Liveblocks 走 WebSocket,HTTP 拦截看不到 nodeId,所以用 DOM 快照 diff):
        1. 创建前 DOM 快照所有 UUID 形 data-id/id
        2. 点 + 按钮打开 Add Node 面板
        3. 点 "Video"(或按 V 键 fallback)
        4. 等 1-2 秒让节点渲染
        5. DOM 再快照,diff 出新增的 UUID
        """
        import re as _re
        UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

        def snapshot_node_ids():
            """收集画布上所有看起来像 nodeId 的值。"""
            return set(page.evaluate(f"""
                () => {{
                    const re = /{UUID_RE}/i;
                    const out = new Set();
                    document.querySelectorAll('*').forEach(el => {{
                        for (const a of el.attributes) {{
                            if (a.name.startsWith('data-') || a.name === 'id') {{
                                if (re.test(a.value)) {{
                                    const m = a.value.match(re);
                                    if (m) out.add(m[0]);
                                }}
                            }}
                        }}
                    }});
                    return [...out];
                }}
            """))

        # 网络拦截 nodeId 也保留(双保险)
        captured_node_ids: list[str] = []
        def on_req_capture_node(req):
            if "/api/" not in req.url:
                return
            try:
                body = req.post_data
                if body and "nodeId" in body:
                    for m in _re.finditer(r'"nodeId"\s*:\s*"([0-9a-f-]{20,})"', body):
                        nid = m.group(1)
                        if nid not in captured_node_ids:
                            captured_node_ids.append(nid)
                            log.info("拦截到 nodeId(网络): %s", nid)
            except Exception:
                pass
        page.on("request", on_req_capture_node)

        # 创建前 DOM 快照
        before_ids = snapshot_node_ids()
        log.info("创建前 DOM 上的 UUID 数: %d", len(before_ids))

        # 打开节点面板("Add Node"面板)
        self.bot._open_node_palette(page)
        page.wait_for_timeout(1200)
        self.bot._dump_screenshot(page, "step5-palette-open")

        # 直接点 "Video" 分类创建通用 Generate Video 节点
        # (Seedance 2.0 不是单独节点,是 Video 节点里的 model 选项,API 会覆盖设置)
        # 见 memory: project-flora-video-node
        clicked = False
        click_candidates = [
            "[role='option']:has-text('Video')",
            "[role='menuitem']:has-text('Video')",
            "button:has-text('Video')",
            "div[role]:has-text('Video')",
        ]
        for selector in click_candidates:
            try:
                loc = page.locator(selector).first
                if loc.is_visible(timeout=1500):
                    # 验证元素文本以 "Video" 开头(避免误点 "Video Player" 等)
                    txt = loc.inner_text(timeout=500).strip()
                    if txt.startswith("Video"):
                        loc.click(timeout=3000)
                        log.info("已点 Video 节点: %s  text=%r", selector, txt[:50])
                        clicked = True
                        break
            except Exception:
                continue

        # Fallback:按键盘 V(Video 节点的快捷键)
        if not clicked:
            log.warning("没点到 Video,fallback 按 V 键")
            try:
                page.keyboard.press("v")
                page.wait_for_timeout(800)
                clicked = True
            except Exception as e:
                log.warning("按 V 也失败: %s", e)

        if not clicked:
            self.bot._dump_screenshot(page, "no-video-node")
            raise RuntimeError("找不到 Video 节点选项")

        # 等节点渲染 + Liveblocks 同步
        log.info("等节点出现在 DOM 上(2 秒)...")
        page.wait_for_timeout(2500)

        page.remove_listener("request", on_req_capture_node)

        # 创建后 DOM 快照
        after_ids = snapshot_node_ids()
        new_ids = after_ids - before_ids
        log.info("创建后 DOM UUID 数: %d  新增: %d", len(after_ids), len(new_ids))

        # 优先用网络拦截到的(若有)
        if captured_node_ids:
            node_id = captured_node_ids[-1]
            log.info("最终 nodeId(网络拦截): %s", node_id)
            return node_id

        if not new_ids:
            self.bot._dump_screenshot(page, "no-node-id-captured")
            raise RuntimeError("创建节点后 DOM 没有新增 UUID")

        # 多个新 ID,取看起来最像 nodeId 的(通常是第一个,或最长的)
        candidates = sorted(new_ids, key=len, reverse=True)
        node_id = candidates[0]
        log.info("最终 nodeId(DOM diff): %s (备选 %d)", node_id, len(candidates))
        return node_id

    def _run_one(self, client: FloraClient, task: Task, *, node_id: str | None = None) -> None:
        # 1. 上传所有参考图
        log.info("[1/4] 上传 %d 张参考图...", len(task.reference_images))
        image_urls: list[str] = []
        labels: list[str] = []
        for p in task.reference_images:
            res = client.upload_image(p)
            image_urls.append(res.media_url)
            # 给图片起个 Flora 风格的英文标签(可选)
            label = client.get_image_title(res.media_url) or p.stem.title()
            labels.append(label)
            log.info("  ✓ %s → %s  [%s]", p.name, res.media_url, label)

        node_labels = ", ".join(labels)
        log.info("[2/4] 节点标签: %s", node_labels)

        # 2. 提交生成并流式监听
        log.info("[3/4] POST /api/workflow/generate (endpointId=%s)", SEEDANCE_REFERENCE_ENDPOINT)
        completed = False
        video_url: str | None = None
        last_status = ""

        for ev in client.generate(
            prompt=task.prompt,
            image_urls=image_urls,
            endpoint_id=SEEDANCE_REFERENCE_ENDPOINT,
            aspect_ratio="9:16",
            resolution="1080p",
            node_labels=node_labels,
            node_id=node_id,
        ):
            status = ev.data.get("status", "")
            if status != last_status or ev.local_progress is not None:
                log.info("  [%s] status=%s data=%s",
                         ev.step_name, status or "...",
                         {k: v for k, v in ev.data.items() if k != "status"})
                last_status = status

            # 检测完成
            if status.startswith("COMPLETED"):
                completed = True
                from .api_client import _extract_video_url
                video_url = _extract_video_url(ev.data) or _extract_video_url(ev.data.get("output") or {})
                log.info(">>> 找到视频 URL: %s", video_url)
                # 持续读后续事件以免连接挂着
                continue
            if status.startswith("FAILED") or status.startswith("CANCELLED"):
                raise RuntimeError(f"生成失败/取消: {status}, data={ev.data}")

        if not completed or not video_url:
            log.warning("流结束但没看到 COMPLETED 事件。video_url=%s", video_url)
            # 不返回,让下面继续尝试 fallback

        # 3. 下载
        if video_url:
            log.info("[4/4] 下载视频...")
            target = self._task_output_path(task)
            client.download_video(video_url, target)
            log.info("✅ 完成: %s", target)
        else:
            log.error("没有视频 URL,跳过下载")

    def _task_output_path(self, task: Task) -> Path:
        ep_name = task.folder.parent.name if task.folder and task.folder.parent else "unknown-ep"
        output_dir = self.output_dir / ep_name
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / f"{task.name}.mp4"
        if not target.exists():
            return target
        return output_dir / f"{task.name}-{int(time.time())}.mp4"
