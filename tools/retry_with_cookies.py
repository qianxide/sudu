"""用保存的 cookies 直接重跑 API(跳过 Patchright 登录),便于快速调试 generation。

用法:
    python tools/retry_with_cookies.py keys/scvyajrulq_outlook.com.cookies.json 素材/segment-02
"""
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.api_client import FloraClient, SEEDANCE_REFERENCE_ENDPOINT, load_cookies
from src.materials import load_tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def main():
    cookies_path = Path(sys.argv[1])
    task_folder = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("素材/segment-02")

    cookies = load_cookies(cookies_path)
    log.info("加载 %d 个 cookies", len(cookies))
    client = FloraClient(cookies=cookies)

    # 取该子目录作为单个任务
    materials_root = task_folder.parent
    tasks = [t for t in load_tasks(materials_root) if t.folder == task_folder]
    if not tasks:
        log.error("没找到任务: %s", task_folder)
        return
    task = tasks[0]
    log.info("任务: %s (参考图 %d 张,prompt %d 字)",
             task.name, len(task.reference_images), len(task.prompt))

    # 上传
    image_urls = []
    labels = []
    for p in task.reference_images:
        res = client.upload_image(p)
        image_urls.append(res.media_url)
        label = client.get_image_title(res.media_url) or p.stem
        labels.append(label)
        log.info("✓ %s → %s [%s]", p.name, res.media_url, label)

    # 生成,流式读
    log.info("=" * 60)
    log.info("POST workflow/generate")
    for ev in client.generate(
        prompt=task.prompt,
        image_urls=image_urls,
        endpoint_id=SEEDANCE_REFERENCE_ENDPOINT,
        node_labels=", ".join(labels),
    ):
        log.info("ev: step=%s progress=%s data=%s",
                 ev.step_name, ev.local_progress, ev.data)
        # 检测最终事件
        status = (ev.data or {}).get("status", "")
        if status.startswith("COMPLETED"):
            log.info("✅ COMPLETED 事件完整 JSON: %s", ev.data)
            break


if __name__ == "__main__":
    main()
