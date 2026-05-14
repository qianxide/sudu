"""找到 HAR 里 PUT/POST 图片到 ImageKit 的请求,获取上传 URL + headers。"""
import json
import sys
from pathlib import Path

HAR = sys.argv[1]
print(f"扫描: {HAR}")

har = json.loads(Path(HAR).read_text(encoding="utf-8"))
entries = har["log"]["entries"]

for i, e in enumerate(entries):
    req = e["request"]
    url = req["url"]
    method = req["method"]
    # 找上传请求
    if method in ("POST", "PUT") and (
        "imagekit" in url.lower()
        or "upload.imagekit.io" in url
        or ("api/v1/files/upload" in url)
    ):
        print(f"\n[{i}] {method} {url}")
        print(f"  status={e['response']['status']}")
        # headers
        headers = {h["name"]: h["value"] for h in req.get("headers", [])}
        for k in ("content-type", "Content-Type", "authorization", "Authorization", "origin", "Origin"):
            if k in headers:
                v = headers[k]
                if "Bearer" in v or "Basic" in v:
                    v = v[:30] + "...(truncated)"
                print(f"  header {k}: {v}")
        # body 前 600 字符
        body = (req.get("postData") or {}).get("text", "")
        if body:
            print(f"  body[:600]: {body[:600]}")
        # response
        resp_body = (e["response"].get("content", {}).get("text") or "")[:800]
        print(f"  resp body[:800]: {resp_body}")
