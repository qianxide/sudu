"""列出 / 切换 Clash 节点。"""
import json
import sys
import urllib.request
import urllib.parse

API = "http://127.0.0.1:9097"
SECRET = "set-your-secret"


def _req(method, path, body=None):
    url = API + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {SECRET}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8")


def list_nodes(group="🔰 选择节点"):
    d = json.loads(_req("GET", "/proxies"))
    g = d["proxies"].get(group)
    if not g:
        # 列出所有 Selector groups,猜
        print("=== Available groups ===")
        for k, v in d["proxies"].items():
            if v.get("type") in ("Selector", "URLTest", "Fallback"):
                print(f"  {k}  type={v['type']}  now={v.get('now')}  ({len(v.get('all', []))} nodes)")
        return
    print(f"=== Current selection: {g.get('now')} ===")
    print(f"=== {len(g['all'])} nodes in '{group}' ===")
    # filter
    keys = ["US", "美", "🇺🇸", "JP", "日", "🇯🇵", "SG", "新", "🇸🇬"]
    filtered = [n for n in g["all"] if any(k in n for k in keys)]
    if filtered:
        print("--- 美/日/新加坡 ---")
        for n in filtered:
            print(" ", n)
    print("--- 全部前 30 ---")
    for n in g["all"][:30]:
        print(" ", n)


def switch_node(group, node):
    print(f"切换 {group} → {node}")
    enc_group = urllib.parse.quote(group, safe="")
    _req("PUT", f"/proxies/{enc_group}", {"name": node})
    d = json.loads(_req("GET", f"/proxies/{enc_group}"))
    print(f"现在: {d.get('now')}")


def check_ip():
    import urllib.request
    proxy = urllib.request.ProxyHandler({"https": "http://127.0.0.1:7897"})
    opener = urllib.request.build_opener(proxy)
    with opener.open("https://ipinfo.io/json", timeout=15) as r:
        d = json.loads(r.read())
        print(f"出口 IP: {d.get('ip')}  地区: {d.get('country')}  城市: {d.get('city')}  ISP: {d.get('org')}")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] == "list":
        list_nodes(sys.argv[2] if len(sys.argv) > 2 else "🔰 选择节点")
    elif sys.argv[1] == "switch":
        switch_node(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "ip":
        check_ip()
    else:
        print(f"用法: {sys.argv[0]} [list|switch <group> <node>|ip]")
