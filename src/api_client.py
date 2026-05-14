"""Flora 内部 API 客户端(逆向自 HAR)。

完整流程:
    1. Patchright 登录一次,导出 cookies(Clerk session + __session 等)
    2. upload_image(path) → 走两步上传 → 返回 media.flora.ai URL
    3. generate_seedance_reference(prompt, image_urls) → 流式 POST → 等待 COMPLETED
    4. 拿视频 URL → 直接 curl 下载

鉴权:Flora 内部 API 用 cookie-based session(Clerk),不是 sk_live_。
       ImageKit 上传只用每次新签的 token,不需要 cookie。

见 memory: project-api-pivot
所有字段名来自 HAR 实测(bnijiublj@outlook.com 2026-05-13 一次成功的多图参考生成)。
"""
from __future__ import annotations

import base64
import io
import json
import logging
import mimetypes
import random
import string
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)


# 上传 ImageKit 的端点(从 HAR 抓的常量)
IMAGEKIT_UPLOAD_URL = "https://upload.imagekit.io/api/v1/files/upload"
# Flora ImageKit account 的 public key(固定常量,从 HAR 抓)
IMAGEKIT_PUBLIC_KEY = "public_2s1pl1OIZnpu8wUIKZCBinUa+2o="
FLORA_BASE = "https://app.flora.ai"

# Seedance 2.0 endpointId(从 HAR 抓的)
SEEDANCE_REFERENCE_ENDPOINT = "f2v-seedance-2.0"  # 多图参考
SEEDANCE_TEXT_ENDPOINT = "t2v-seedance-2.0"       # 纯文字


# ---------------------------------------------------------------- low-level

def _multipart_body(fields: dict[str, str | tuple[str, bytes, str]]) -> tuple[bytes, str]:
    """构造 multipart/form-data body。

    fields 值可以是:
      str  → 普通文本字段
      (filename, bytes, mime) → 文件字段
    返回 (body, content_type_with_boundary)
    """
    boundary = "----floraapi-" + "".join(random.choices(string.ascii_letters + string.digits, k=16))
    out = io.BytesIO()
    for name, value in fields.items():
        out.write(f"--{boundary}\r\n".encode())
        if isinstance(value, tuple):
            filename, data, mime = value
            out.write(
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
            )
            out.write(f"Content-Type: {mime}\r\n\r\n".encode())
            out.write(data)
            out.write(b"\r\n")
        else:
            out.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            out.write(str(value).encode())
            out.write(b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def _http(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 120,
    stream: bool = False,
) -> tuple[int, dict[str, str], bytes | urllib.request.addinfourl]:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        h = dict(resp.headers)
        if stream:
            return resp.status, h, resp
        return resp.status, h, resp.read()
    except urllib.error.HTTPError as e:
        err = e.read()
        log.error("HTTP %d %s %s: %s", e.code, method, url, err[:500])
        raise


# ---------------------------------------------------------------- API client


@dataclass
class UploadResult:
    file_id: str
    name: str          # 例如 "1b8d2864-..._TUZPCzKJVa.png"
    url: str           # ImageKit URL
    media_url: str     # media.flora.ai CNAME alias(传给 generate 用这个)
    width: int
    height: int
    raw: dict


@dataclass
class GenerationProgress:
    step_name: str
    local_progress: float | None = None
    data: dict = field(default_factory=dict)


@dataclass
class GenerationResult:
    completed: bool
    video_url: str | None
    progress_events: list[GenerationProgress]
    raw_last_event: dict


class FloraClient:
    """需要先用 Patchright 登一次拿 cookies,然后传 cookies dict 给本类。

    cookies dict 形如 {"__session": "...", "__clerk_db_jwt": "...", ...}
    可以通过 BrowserContext.cookies() 拿到再转成 dict。
    """

    def __init__(self, cookies: dict[str, str], *, base: str = FLORA_BASE) -> None:
        self.cookies = cookies
        self.base = base.rstrip("/")

    # ---------- helpers

    def _cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def _json_post(self, path: str, body: dict | list, *, stream: bool = False) -> Any:
        url = self.base + path
        data = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": self.base,
            "Cookie": self._cookie_header(),
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        status, h, resp = _http("POST", url, headers=headers, body=data, stream=stream)
        if stream:
            return resp
        if not resp:
            return {}
        text = resp.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    # ---------- upload

    def upload_image(self, image_path: Path) -> UploadResult:
        """两步上传:create-signed-upload-url → POST ImageKit。"""
        p = Path(image_path)
        log.info("上传图片: %s (%d bytes)", p.name, p.stat().st_size)

        # Step 1: 拿签名
        sign = self._json_post(
            "/api/nodes/create-signed-upload-url",
            [p.name],
        )
        log.debug("签名响应: %s", sign)
        token = sign["token"]
        signature = sign["signature"]
        expire = sign["expire"]
        file_path = sign["filePath"]
        file_name = sign["fileName"]  # ImageKit 用这个名字存盘

        # Step 2: POST 到 ImageKit (multipart/form-data,所有字段必填)
        mime = mimetypes.guess_type(p.name)[0] or "image/png"
        body, content_type = _multipart_body({
            "token": token,
            "signature": signature,
            "expire": str(expire),
            "filePath": file_path,
            "fileName": file_name,
            "file": (p.name, p.read_bytes(), mime),
            "isPrivateFile": "false",
            "publicKey": IMAGEKIT_PUBLIC_KEY,
        })
        headers = {
            "Content-Type": content_type,
            "Origin": self.base,
        }
        status, _, raw = _http("POST", IMAGEKIT_UPLOAD_URL, headers=headers, body=body, timeout=300)
        resp = json.loads(raw.decode("utf-8"))
        ik_url = resp["url"]  # https://ik.imagekit.io/ff5bkg98p/<name>
        # media.flora.ai 的 CNAME 别名(generate 体里用这个,虽然 ik.imagekit.io 也能用)
        media_url = ik_url.replace("https://ik.imagekit.io/ff5bkg98p/", "https://media.flora.ai/")

        log.info("上传完成: %s", media_url)
        return UploadResult(
            file_id=resp["fileId"],
            name=resp["name"],
            url=ik_url,
            media_url=media_url,
            width=resp["width"],
            height=resp["height"],
            raw=resp,
        )

    def get_image_title(self, image_url: str) -> str:
        """让 Flora 用 LLM 给图片起个英文标题(给 combinedParentVisualNodeLabels 用)。"""
        try:
            resp = self._json_post("/api/llm/title-for-image", [image_url])
            return resp.get("title", {}).get("response") or ""
        except Exception as e:
            log.warning("title-for-image 失败: %s", e)
            return ""

    # ---------- generate

    def generate(
        self,
        *,
        prompt: str,
        image_urls: list[str],
        endpoint_id: str = SEEDANCE_REFERENCE_ENDPOINT,
        aspect_ratio: str = "9:16",
        resolution: str = "1080p",
        node_labels: str = "",
        node_id: str | None = None,
        generation_id: str | None = None,
    ) -> Iterator[GenerationProgress]:
        """提交 Seedance 生成,流式 yield 进度事件。

        流响应是 base64 编码的 NDJSON(每行一个 event JSON)。
        每个 event 结构形如:
          {"stepName": "Check Generation Status", "data": {"status": "IN_PROGRESS"}}
          {"stepName": "Check Generation Status", "data": {"status": "COMPLETED", "output": {...}}}
        """
        body = {
            "params": {
                "generationId": generation_id or _rand_id(),
                "nodeId": node_id or _uuid4(),
                "endpointId": endpoint_id,
                "modelParameters": {
                    "aspect_ratio": aspect_ratio,
                    "resolution": resolution,
                    "prompt": prompt,
                    "combinedParentVisualNodeLabels": node_labels,
                    "image_urls": image_urls,
                },
            }
        }
        log.info(
            "POST /api/workflow/generate (endpointId=%s, image_urls=%d)",
            endpoint_id, len(image_urls),
        )
        resp = self._json_post("/api/workflow/generate", body, stream=True)

        # 流式读响应,响应体可能是 base64 或者直接 NDJSON
        # 把所有原始事件 dump 到 debug 文件,方便排错
        debug_path = Path("logs") / f"generate-raw-{int(time.time())}.ndjson"
        debug_path.parent.mkdir(exist_ok=True)
        debug_fp = debug_path.open("w", encoding="utf-8")
        log.info("raw events → %s", debug_path)

        buffer = b""
        for chunk in iter(lambda: resp.read(4096), b""):
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    decoded = base64.b64decode(line).decode("utf-8")
                    event = json.loads(decoded)
                except Exception:
                    try:
                        event = json.loads(line.decode("utf-8"))
                    except Exception as e:
                        log.debug("无法解析事件行: %s (%s)", line[:80], e)
                        continue
                # dump 原始事件 JSON 到 debug 文件
                debug_fp.write(json.dumps(event, ensure_ascii=False) + "\n")
                debug_fp.flush()
                # 重要事件全字段打到 log
                if "Error" in event.get("stepName", "") or event.get("data"):
                    log.info("RAW event: %s", json.dumps(event, ensure_ascii=False)[:500])
                yield GenerationProgress(
                    step_name=event.get("stepName", ""),
                    local_progress=event.get("localProgress"),
                    data=event.get("data", {}),
                )
        debug_fp.close()

    def generate_and_wait(
        self,
        *,
        prompt: str,
        image_urls: list[str],
        endpoint_id: str = SEEDANCE_REFERENCE_ENDPOINT,
        aspect_ratio: str = "9:16",
        resolution: str = "1080p",
        node_labels: str = "",
        on_event: callable = None,
        timeout_s: int = 600,
    ) -> GenerationResult:
        """同步等待生成完成,返回视频 URL。"""
        events: list[GenerationProgress] = []
        deadline = time.time() + timeout_s
        last_raw: dict = {}
        video_url: str | None = None
        completed = False

        for ev in self.generate(
            prompt=prompt, image_urls=image_urls,
            endpoint_id=endpoint_id, aspect_ratio=aspect_ratio,
            resolution=resolution, node_labels=node_labels,
        ):
            if time.time() > deadline:
                raise TimeoutError("生成超时")
            events.append(ev)
            last_raw = ev.data
            if on_event:
                try:
                    on_event(ev)
                except Exception:
                    pass
            log.info("[%s] %s", ev.step_name, ev.data or f"progress={ev.local_progress}")
            # 检查 COMPLETED — 视频 URL 的字段名待子代理确认,先放几个候选
            status = (ev.data or {}).get("status", "")
            if status in ("COMPLETED", "SUCCESS", "DONE"):
                completed = True
                video_url = _extract_video_url(ev.data)
                break
            if status in ("FAILED", "ERROR", "CANCELLED"):
                raise RuntimeError(f"生成失败: {ev.data}")

        return GenerationResult(
            completed=completed,
            video_url=video_url,
            progress_events=events,
            raw_last_event=last_raw,
        )

    @staticmethod
    def download_video(url: str, dest: Path) -> Path:
        log.info("下载视频: %s → %s", url[:80], dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=300) as r:
            dest.write_bytes(r.read())
        log.info("下载完成: %d bytes", dest.stat().st_size)
        return dest


# ---------------------------------------------------------------- helpers

def _uuid4() -> str:
    import uuid
    return str(uuid.uuid4())


def _rand_id(n: int = 32) -> str:
    """Flora 的 generationId 看起来是 base36-ish 32 字符随机串。"""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choices(alphabet, k=n))


def _extract_video_url(data: dict) -> str | None:
    """从 event.data 里抽视频 URL。字段名待子代理确认后精化。"""
    # 候选字段路径
    for key in ("videoUrl", "video_url", "outputUrl", "output_url", "url"):
        v = data.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
    output = data.get("output") or data.get("outputs")
    if isinstance(output, dict):
        for key in ("url", "videoUrl", "video_url", "value"):
            v = output.get(key)
            if isinstance(v, str) and v.startswith("http"):
                return v
    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict):
                u = _extract_video_url(item)
                if u:
                    return u
            elif isinstance(item, str) and item.startswith("http"):
                return item
    # 深度遍历兜底
    def walk(node):
        if isinstance(node, str) and node.startswith("http") and ".mp4" in node:
            return node
        if isinstance(node, dict):
            for v in node.values():
                r = walk(v)
                if r:
                    return r
        if isinstance(node, list):
            for x in node:
                r = walk(x)
                if r:
                    return r
        return None
    return walk(data)


# ---------------------------------------------------------------- session helper

def cookies_from_playwright(context) -> dict[str, str]:
    """从一个登录后的 Playwright BrowserContext 导出 cookies dict。"""
    out: dict[str, str] = {}
    for c in context.cookies():
        # 只取 flora.ai 域的
        if "flora.ai" in c.get("domain", ""):
            out[c["name"]] = c["value"]
    return out


def save_cookies(cookies: dict[str, str], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")


def load_cookies(src: Path) -> dict[str, str]:
    return json.loads(Path(src).read_text(encoding="utf-8"))
