"""读取 ./素材 文件夹,把每个子目录视为一个视频生成任务。

模式: Seedance 2.0 **多图参考 + 文本提示词**

子目录约定:
  素材/任务01/
      ref1.png         # 参考图(可多张,按文件名排序上传)
      ref2.jpg
      ref3.webp
      prompt.txt       # 提示词(UTF-8),也接受 prompt.md 或目录内唯一的 .md 文件

至少 1 张参考图 + 一份提示词文件才会视为有效任务。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


@dataclass(frozen=True)
class Task:
    name: str
    reference_images: list[Path]  # 多图参考,按文件名排序
    prompt: str
    folder: Path


def _sorted_images(folder: Path) -> list[Path]:
    imgs = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    # 自然排序:数字按数值大小,而非字典序
    def key(p: Path) -> tuple:
        import re
        return tuple(
            int(s) if s.isdigit() else s.lower()
            for s in re.split(r"(\d+)", p.stem)
        )
    return sorted(imgs, key=key)


def _find_prompt_file(folder: Path) -> Path | None:
    """优先 prompt.txt → prompt.md → 目录内任一 .md/.txt 文件。"""
    for name in ("prompt.txt", "prompt.md"):
        p = folder / name
        if p.exists():
            return p
    candidates = [
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in (".md", ".txt")
    ]
    return candidates[0] if candidates else None


def load_tasks(materials_dir: str | Path) -> list[Task]:
    root = Path(materials_dir)
    if not root.exists():
        raise FileNotFoundError(f"素材目录不存在: {root}")

    tasks: list[Task] = []
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue

        prompt_file = _find_prompt_file(sub)
        if prompt_file is None:
            print(f"[skip] {sub.name}: 缺少 prompt 文件 (.txt 或 .md)")
            continue

        images = _sorted_images(sub)
        if not images:
            print(f"[skip] {sub.name}: 没有可用图片")
            continue

        prompt_text = prompt_file.read_text(encoding="utf-8").strip()
        if not prompt_text:
            print(f"[skip] {sub.name}: prompt.txt 为空")
            continue

        tasks.append(
            Task(
                name=sub.name,
                reference_images=images,
                prompt=prompt_text,
                folder=sub,
            )
        )
    return tasks
