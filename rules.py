"""规则管理共享模块。"""

import json
import os
import tempfile
from pathlib import Path


RULES_FILE = Path(os.getenv("RULES_FILE", "rules.json"))


def load_rules() -> list[dict]:
    """从 RULES_FILE 加载规则列表，文件缺失或损坏时抛出异常。"""
    if not RULES_FILE.exists():
        return []
    try:
        raw = RULES_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Failed to read {RULES_FILE}: {exc}") from exc

    rules = data.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError("rules.json field 'rules' must be a list")
    return rules


def save_rules(rules: list[dict]) -> None:
    """原子写入规则到 RULES_FILE（临时文件 + rename）。"""
    content = json.dumps({"rules": rules}, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        dir=str(RULES_FILE.parent),
        prefix=f".{RULES_FILE.name}.tmp.",
    )
    try:
        try:
            os.write(fd, content.encode("utf-8"))
        except OSError:
            os.close(fd)
            raise
        else:
            os.close(fd)
        os.replace(tmp_path, RULES_FILE)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def find_rule(rules: list[dict], group_id: int) -> dict | None:
    """按 group_id 查找规则，未找到返回 None。"""
    for rule in rules:
        if rule["group_id"] == group_id:
            return rule
    return None


def normalize_rule(rule: dict) -> dict:
    """规范化规则字典：类型转换、目标去重、过滤空白关键词。"""
    keywords: list[dict] = []
    for item in rule.get("keywords", []):
        if isinstance(item, dict):
            word = str(item.get("word", "")).strip()
            if word:
                kw_dict: dict = {
                    "word": word,
                    "enabled": bool(item.get("enabled", True)),
                }
                if "targets" in item and item["targets"]:
                    kw_dict["targets"] = sorted({int(t) for t in item["targets"]})
                keywords.append(kw_dict)
        else:
            word = str(item).strip()
            if word:
                keywords.append({"word": word, "enabled": True})
    return {
        "group_id": int(rule["group_id"]),
        "targets": sorted({int(item) for item in rule.get("targets", [])}),
        "keywords": keywords,
        "enabled": bool(rule.get("enabled", True)),
        "use_regex": bool(rule.get("use_regex", False)),
    }


def upsert_rule(rules: list[dict], new_rule: dict) -> list[dict]:
    """按 group_id 插入或替换规则，返回排序后的列表。"""
    updated = [normalize_rule(rule) for rule in rules if rule["group_id"] != new_rule["group_id"]]
    updated.append(normalize_rule(new_rule))
    updated.sort(key=lambda item: item["group_id"])
    return updated