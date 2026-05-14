"""分析 HAR 文件,提取所有 /api/ 调用 + Seedance 相关 URL,输出关键发现。"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from collections import Counter, defaultdict

HAR_PATH = Path(sys.argv[1] if len(sys.argv) > 1 else "logs/har")
if HAR_PATH.is_dir():
    files = sorted(HAR_PATH.glob("*.har"), key=lambda p: p.stat().st_size, reverse=True)
    HAR_PATH = files[0]
print(f"分析: {HAR_PATH}")

har = json.loads(HAR_PATH.read_text(encoding="utf-8"))
entries = har["log"]["entries"]
print(f"总请求数: {len(entries)}\n")

api_hits: list[dict] = []
seedance_hits: list[dict] = []
upload_hits: list[dict] = []
endpoint_count: Counter[str] = Counter()

for e in entries:
    url = e["request"]["url"]
    method = e["request"]["method"]
    status = e["response"]["status"]
    # 只关心 flora.ai 域
    if "flora.ai" not in url:
        continue
    if "/api/" not in url:
        continue
    # 抽出端点路径(去 query)
    path = url.split("?", 1)[0].split("flora.ai", 1)[-1]
    # 把 ID 替换成 {id}
    path_norm = re.sub(r"/[a-z0-9]{12,}\b", "/{id}", path)
    endpoint_count[f"{method} {path_norm}"] += 1
    api_hits.append({"method": method, "url": url, "status": status, "entry": e})
    low = url.lower()
    if "seedance" in low:
        seedance_hits.append({"method": method, "url": url, "status": status, "entry": e})
    if "upload" in low or "media" in path or "/files" in path or "/blob" in path:
        upload_hits.append({"method": method, "url": url, "status": status, "entry": e})

print("=== 端点频次 Top 40 ===")
for k, v in endpoint_count.most_common(40):
    print(f"  {v:4d}  {k}")

print(f"\n=== Seedance 相关请求 ({len(seedance_hits)} 条) ===")
for h in seedance_hits[:20]:
    print(f"  {h['method']} {h['status']} {h['url'][:200]}")

print(f"\n=== 上传/媒体相关请求 ({len(upload_hits)} 条) ===")
for h in upload_hits[:30]:
    print(f"  {h['method']} {h['status']} {h['url'][:200]}")

# 找 POST 请求 (有 body 的) 含 IMAGE_URL/prompt/reference 字样的
print("\n=== 含 'inputs' / 'IMAGE_URL' / 'prompt' 的 POST body ===")
for h in api_hits:
    if h["method"] != "POST":
        continue
    e = h["entry"]
    body_text = (e["request"].get("postData") or {}).get("text", "")
    if not body_text:
        continue
    if any(k in body_text for k in ("IMAGE_URL", '"inputs"', "reference_image", "seedance")):
        print(f"\n  POST {h['url'][:200]}  ({h['status']})")
        print(f"  body[:1500]: {body_text[:1500]}")
