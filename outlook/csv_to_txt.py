"""
将 outlook*.csv 转成同名 .txt。
CSV 每行格式(被引号包裹):
  "email----password----clientID----refreshToken"
TXT 输出每行去掉引号,保持原始 ---- 分隔符。
"""
from pathlib import Path

ROOT = Path(__file__).parent


def convert(src: Path) -> int:
    dst = src.with_suffix(".txt")
    # 原文件是 ISO-8859 / UTF-8 带 BOM 混合;用 utf-8-sig 容错读
    raw = src.read_text(encoding="utf-8-sig", errors="replace")
    out_lines: list[str] = []
    for line in raw.splitlines():
        line = line.strip().lstrip("﻿")
        if not line:
            continue
        # CSV 字段两侧带 "" ,统一脱掉
        if line.startswith('"') and line.endswith('"'):
            line = line[1:-1]
        # 跳过纯乱码/标题行(没有 ---- 的)
        if "----" not in line:
            continue
        out_lines.append(line)

    dst.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"OK: {len(out_lines)} 条账号已写入 {dst}")
    return len(out_lines)


def main() -> None:
    csvs = sorted(ROOT.glob("outlook*.csv"))
    if not csvs:
        raise SystemExit("没有找到 outlook*.csv")
    total = 0
    for src in csvs:
        total += convert(src)
    print(f"完成: 共 {total} 条账号")


if __name__ == "__main__":
    main()
