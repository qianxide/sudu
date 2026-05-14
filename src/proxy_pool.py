"""动态 IP 代理池(ipipdo 等供应商格式)。

文件格式(每行):
    host:port:username:password

加载 `ip/` 目录下所有 .txt,每行一条,组合成 `http://user:pass@host:port`。
按账号 email 哈希做**确定性**分配 —— 同一账号每次拿到同一 IP,
MS OAuth 整个登录期间 IP 不变,避免触发地理/风控变化。

如果池里 N < accounts:多个账号共享同一 IP(取模)。
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def _parse_line(line: str) -> str | None:
    """`host:port:user:pass` → `http://user:pass@host:port`,失败返回 None。"""
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    parts = s.split(":")
    if len(parts) < 4:
        log.warning("代理行格式不符(需 host:port:user:pass): %r", s[:60])
        return None
    host, port = parts[0], parts[1]
    # user 可能含 `:`,所以从右往左切 password 最稳;ipipdo 格式 user 不含冒号,简化处理
    user, password = parts[2], ":".join(parts[3:])
    return f"http://{user}:{password}@{host}:{port}"


def load_proxies(folder: str | Path) -> list[str]:
    """读取目录下所有 .txt,合并去重。"""
    root = Path(folder)
    if not root.exists():
        log.warning("代理池目录不存在: %s", root)
        return []
    proxies: list[str] = []
    seen: set[str] = set()
    for f in sorted(root.glob("*.txt")):
        n_before = len(proxies)
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            url = _parse_line(line)
            if url and url not in seen:
                seen.add(url)
                proxies.append(url)
        log.info("代理池 %s: 新增 %d 条", f.name, len(proxies) - n_before)
    log.info("代理池合计 %d 个可用 IP", len(proxies))
    return proxies


def pick_for(key: str, proxies: list[str]) -> str | None:
    """按 key 哈希确定性挑一个 IP。空池返回 None。"""
    if not proxies:
        return None
    h = hashlib.sha256(key.encode("utf-8")).digest()
    idx = int.from_bytes(h[:8], "big") % len(proxies)
    return proxies[idx]


def short(proxy_url: str | None) -> str:
    """脱敏 + 简短显示,用于日志。保留 user 尾部(差异区段)便于区分多账号。"""
    if not proxy_url:
        return "(none)"
    try:
        _, rest = proxy_url.split("://", 1)
        if "@" not in rest:
            return proxy_url
        cred, hostport = rest.rsplit("@", 1)
        user = cred.split(":", 1)[0]
        # 显示 user 的前 6 + 末 8 个字符,中间用 ... 隐去
        if len(user) > 18:
            user_show = f"{user[:6]}...{user[-8:]}"
        else:
            user_show = user
        return f"{user_show}@{hostport}"
    except Exception:
        return proxy_url
