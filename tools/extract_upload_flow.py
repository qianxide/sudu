"""从 HAR 抽出 /api/nodes/create-signed-upload-url 的请求/响应 + 后续 PUT 上传。"""
import json
import sys
from pathlib import Path

HAR = sys.argv[1]
har = json.loads(Path(HAR).read_text(encoding="utf-8"))
entries = har["log"]["entries"]

print(f"=== /api/nodes/create-signed-upload-url 全流程 ===\n")
for i, e in enumerate(entries):
    url = e["request"]["url"]
    if "create-signed-upload-url" in url or "media.flora.ai" in url:
        method = e["request"]["method"]
        status = e["response"]["status"]
        if method == "POST":
            req_body = (e["request"].get("postData") or {}).get("text", "")
            resp_body = (e["response"]["content"].get("text") or "")[:2000]
            print(f"\n[{i}] POST {url}")
            print(f"  status={status}")
            print(f"  REQ body: {req_body[:1000]}")
            print(f"  RESP body: {resp_body}")
        elif method == "PUT":
            req_size = (e["request"].get("bodySize", -1))
            print(f"\n[{i}] PUT {url[:200]}")
            print(f"  status={status}")
            print(f"  body size: {req_size} bytes")
        # 跳过 GET image,信息太杂

print("\n=== /api/llm/title-for-image 调用 ===\n")
for i, e in enumerate(entries):
    if "/api/llm/title-for-image" in e["request"]["url"]:
        req_body = (e["request"].get("postData") or {}).get("text", "")
        resp_body = (e["response"]["content"].get("text") or "")[:500]
        print(f"\n[{i}] {e['request']['method']} {e['request']['url']}")
        print(f"  REQ body: {req_body[:500]}")
        print(f"  RESP body: {resp_body}")

print("\n=== /api/workflow/generate 响应 ===\n")
for i, e in enumerate(entries):
    if "/api/workflow/generate" in e["request"]["url"]:
        resp_body = (e["response"]["content"].get("text") or "")[:2000]
        req_body = (e["request"].get("postData") or {}).get("text", "")
        print(f"\n[{i}] POST {e['request']['url']}")
        print(f"  REQ body: {req_body[:2000]}")
        print(f"  RESP body: {resp_body}")
