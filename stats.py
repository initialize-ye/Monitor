"""关键词命中统计共享模块。"""

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

STATS_FILE = Path(os.getenv("STATS_FILE", "stats.json"))


def load_stats() -> dict[str, int]:
    """从 STATS_FILE 加载统计，文件缺失返回空字典。"""
    if not STATS_FILE.exists():
        return {}
    try:
        raw = STATS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load stats from %s: %s", STATS_FILE, exc)
        return {}
    stats = data.get("stats", {})
    if not isinstance(stats, dict):
        logger.warning("Invalid stats format in %s, expected dict", STATS_FILE)
        return {}
    result: dict[str, int] = {}
    for k, v in stats.items():
        try:
            result[str(k)] = int(v)
        except (ValueError, TypeError):
            logger.warning("Skipping invalid stats entry: %s=%s", k, v)
    return result


def save_stats(stats: dict[str, int]) -> None:
    """原子写入统计到 STATS_FILE（临时文件 + rename）。"""
    content = json.dumps({"stats": stats}, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        dir=str(STATS_FILE.parent),
        prefix=f".{STATS_FILE.name}.tmp.",
    )
    try:
        try:
            os.write(fd, content.encode("utf-8"))
        except OSError:
            os.close(fd)
            raise
        else:
            os.close(fd)
        os.replace(tmp_path, STATS_FILE)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
