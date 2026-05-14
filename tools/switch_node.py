"""列 / 切 Clash 节点。"""
import json
import sys
import urllib.parse
import urllib.request

API = "http://127.0.0.1:9097"
SECRET = "set-your-secret"
GROUP = "🔰 选择节点"


def call(method, path, body=None):
    url = API + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8")


def list_filtered():
    d = json.loads(call("GET", "/proxies"))
    g = d["proxies"][GROUP]
    print(f"=== 当前: {g['now']} ===")
    nodes = g["all"]
    # 优先美/日/新加坡
    priority = ["US", "美", "🇺🇸", "JP", "日", "🇯🇵", "SG", "新", "🇸🇬"]
    for prefix in priority:
        matched = [n for n in nodes if prefix in n]
        if matched:
            print(f"\n[{prefix}]")
            for n in matched:
                print(f"  {n}")
    return g["all"]


def switch_to_us_jp():
    """自动挑一个美国或日本节点切过去。"""
    d = json.loads(call("GET", "/proxies"))
    nodes = d["proxies"][GROUP]["all"]
    candidates = []
    for prio in ("US", "美", "🇺🇸", "JP", "日", "🇯🇵", "SG", "新", "🇸🇬"):
        for n in nodes:
            if prio in n and n not in candidates:
                candidates.append(n)
    if not candidates:
        print("没有美/日/新节点,只能用 HK")
        return None
    target = candidates[0]
    enc = urllib.parse.quote(GROUP, safe="")
    call("PUT", f"/proxies/{enc}", {"name": target})
    print(f"切到: {target}")
    return target


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        list_filtered()
    elif cmd == "switch-us-jp":
        switch_to_us_jp()
    elif cmd == "switch":
        target = sys.argv[2]
        enc = urllib.parse.quote(GROUP, safe="")
        call("PUT", f"/proxies/{enc}", {"name": target})
        print(f"切到: {target}")
