"""Shared rule management for QQ keyword forward bot."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any


RULES_FILE = Path(os.getenv("RULES_FILE", "rules.json"))


def load_rules() -> list[dict]:
    """Load rules from RULES_FILE. Returns empty list if file missing or corrupt."""
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
    """Write rules to RULES_FILE atomically via temp file + rename."""
    content = json.dumps({"rules": rules}, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        dir=str(RULES_FILE.parent),
        prefix=f".{RULES_FILE.name}.tmp.",
    )
    try:
        os.write(fd, content.encode("utf-8"))
    except OSError:
        os.close(fd)
        os.unlink(tmp_path)
        raise
    else:
        os.close(fd)
    os.replace(tmp_path, RULES_FILE)


def find_rule(rules: list[dict], group_id: int) -> dict | None:
    for rule in rules:
        if rule["group_id"] == group_id:
            return rule
    return None


def normalize_rule(rule: dict) -> dict:
    return {
        "group_id": int(rule["group_id"]),
        "targets": sorted({int(item) for item in rule.get("targets", [])}),
        "keywords": [str(item).strip() for item in rule.get("keywords", []) if str(item).strip()],
        "enabled": bool(rule.get("enabled", True)),
        "use_regex": bool(rule.get("use_regex", False)),
    }


def upsert_rule(rules: list[dict], new_rule: dict) -> list[dict]:
    updated = [rule for rule in rules if rule["group_id"] != new_rule["group_id"]]
    updated.append(normalize_rule(new_rule))
    updated.sort(key=lambda item: item["group_id"])
    return updated