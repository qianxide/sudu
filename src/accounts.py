"""读取批量账号文件,兼容两种格式:

  Outlook 4 段: email----password----clientID----refreshToken
  Google  3 段: email----password----recoveryEmail
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Account:
    email: str
    password: str
    client_id: str = ""
    refresh_token: str = ""
    recovery_email: str = ""

    @property
    def slug(self) -> str:
        # 用于做 user-data-dir 子目录名,避免文件系统非法字符
        return re.sub(r"[^a-zA-Z0-9_.-]", "_", self.email)


def load_accounts(path: str | Path) -> list[Account]:
    paths = [p.strip() for p in re.split(r"[;,]", str(path)) if p.strip()]
    if len(paths) > 1:
        accounts: list[Account] = []
        seen: set[str] = set()
        for item in paths:
            for account in load_accounts(item):
                key = account.email.lower()
                if key in seen:
                    continue
                seen.add(key)
                accounts.append(account)
        return accounts

    p = Path(paths[0] if paths else path)
    if not p.exists():
        raise FileNotFoundError(f"账号文件不存在: {p}")

    accounts: list[Account] = []
    for raw in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or "----" not in line:
            continue
        parts = [s.strip() for s in line.split("----")]
        if len(parts) < 2:
            continue
        email, password = parts[0], parts[1]
        # 4 段 = Outlook (email, pw, clientID, refreshToken)
        # 3 段 = Google  (email, pw, recoveryEmail)
        client_id = parts[2] if len(parts) >= 4 else ""
        refresh_token = parts[3] if len(parts) >= 4 else ""
        recovery_email = parts[2] if len(parts) == 3 else (parts[4] if len(parts) >= 5 else "")
        accounts.append(
            Account(
                email=email,
                password=password,
                client_id=client_id,
                refresh_token=refresh_token,
                recovery_email=recovery_email,
            )
        )
    return accounts
