import json
import os
import re
import time
from collections import deque
from pathlib import Path

from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, PrivateMessageEvent
from nonebot.exception import FinishedException
from nonebot.rule import is_type


def _parse_int_set(raw: str) -> set[int]:
    values: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            values.add(int(part))
    return values


def _parse_str_list(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


TARGET_QQS = _parse_int_set(os.getenv("TARGET_QQS", ""))
ADMIN_QQS = _parse_int_set(os.getenv("ADMIN_QQS", "")) or TARGET_QQS
KEYWORDS = _parse_str_list(os.getenv("KEYWORDS", ""))
CASE_SENSITIVE = os.getenv("CASE_SENSITIVE", "false").lower() == "true"
USE_REGEX = os.getenv("USE_REGEX", "false").lower() == "true"
RULES_FILE = Path(os.getenv("RULES_FILE", "rules.json"))
LEGACY_KEYWORDS_FILE = Path(os.getenv("KEYWORDS_FILE", "keywords.json"))
DEFAULT_ALLOWED_GROUPS = sorted(_parse_int_set(os.getenv("ALLOWED_GROUPS", "")))

_recent_keys: deque[tuple[float, str]] = deque()
_recent_seen: set[str] = set()
_dedupe_seconds = 30

_rules_mtime: float | None = None
_rules_cache: list[dict] = []

matcher = on_message(rule=is_type(GroupMessageEvent), priority=10, block=False)
admin_matcher = on_message(rule=is_type(PrivateMessageEvent), priority=5, block=True)


def _normalize(text: str) -> str:
    return text if CASE_SENSITIVE else text.lower()


def _match_keywords(text: str, keywords: list[str], use_regex: bool) -> list[str]:
    matched: list[str] = []
    normalized = _normalize(text)
    for keyword in keywords:
        candidate = keyword if CASE_SENSITIVE else keyword.lower()
        if use_regex:
            flags = 0 if CASE_SENSITIVE else re.IGNORECASE
            if re.search(keyword, text, flags=flags):
                matched.append(keyword)
        elif candidate in normalized:
            matched.append(keyword)
    return matched


def _normalize_rule(rule: dict) -> dict:
    group_id = int(rule["group_id"])
    targets = sorted({int(item) for item in rule.get("targets", [])})
    keywords = [str(item).strip() for item in rule.get("keywords", []) if str(item).strip()]
    enabled = bool(rule.get("enabled", True))
    use_regex = bool(rule.get("use_regex", USE_REGEX))
    return {
        "group_id": group_id,
        "targets": targets,
        "keywords": keywords,
        "enabled": enabled,
        "use_regex": use_regex,
    }


def _build_default_rules() -> list[dict]:
    default_groups = DEFAULT_ALLOWED_GROUPS or []
    if not default_groups:
        return []
    return [
        _normalize_rule(
            {
                "group_id": group_id,
                "targets": sorted(TARGET_QQS),
                "keywords": KEYWORDS,
                "enabled": True,
                "use_regex": USE_REGEX,
            }
        )
        for group_id in default_groups
    ]


def _load_legacy_keywords() -> list[str]:
    if not LEGACY_KEYWORDS_FILE.exists():
        return KEYWORDS.copy()
    data = json.loads(LEGACY_KEYWORDS_FILE.read_text(encoding="utf-8"))
    file_keywords = data.get("keywords", [])
    return [str(item).strip() for item in file_keywords if str(item).strip()]


def _migrate_legacy_rules() -> list[dict]:
    keywords = _load_legacy_keywords()
    rules = []
    for group_id in DEFAULT_ALLOWED_GROUPS:
        rules.append(
            _normalize_rule(
                {
                    "group_id": group_id,
                    "targets": sorted(TARGET_QQS),
                    "keywords": keywords,
                    "enabled": True,
                    "use_regex": USE_REGEX,
                }
            )
        )
    return rules


def _save_rules_to_file(rules: list[dict]) -> list[dict]:
    global _rules_cache, _rules_mtime
    normalized = [_normalize_rule(rule) for rule in rules]
    normalized.sort(key=lambda item: item["group_id"])
    RULES_FILE.write_text(
        json.dumps({"rules": normalized}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _rules_cache = normalized
    _rules_mtime = RULES_FILE.stat().st_mtime
    logger.info("Saved rules to %s", RULES_FILE)
    return normalized


def _load_rules_from_file() -> list[dict]:
    global _rules_cache, _rules_mtime

    if not RULES_FILE.exists():
        migrated = _migrate_legacy_rules() or _build_default_rules()
        if migrated:
            return _save_rules_to_file(migrated)
        _rules_cache = []
        return _rules_cache

    stat = RULES_FILE.stat()
    if _rules_mtime is not None and stat.st_mtime == _rules_mtime:
        return _rules_cache

    data = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    file_rules = data.get("rules", [])
    if not isinstance(file_rules, list):
        raise ValueError("rules.json field 'rules' must be a list")

    _rules_cache = [_normalize_rule(item) for item in file_rules]
    _rules_cache.sort(key=lambda item: item["group_id"])
    _rules_mtime = stat.st_mtime
    logger.info("Reloaded rules from %s", RULES_FILE)
    return _rules_cache


def _find_rule(rules: list[dict], group_id: int) -> dict | None:
    for rule in rules:
        if rule["group_id"] == group_id:
            return rule
    return None


def _upsert_rule(rules: list[dict], new_rule: dict) -> list[dict]:
    updated = [rule for rule in rules if rule["group_id"] != new_rule["group_id"]]
    updated.append(_normalize_rule(new_rule))
    return sorted(updated, key=lambda item: item["group_id"])


def _is_duplicate(message_key: str) -> bool:
    now = time.time()
    while _recent_keys and now - _recent_keys[0][0] > _dedupe_seconds:
        _, expired = _recent_keys.popleft()
        _recent_seen.discard(expired)

    if message_key in _recent_seen:
        return True

    _recent_keys.append((now, message_key))
    _recent_seen.add(message_key)
    return False


def _build_admin_help() -> str:
    return (
        "管理命令:\n"
        "kw list <群号>\n"
        "kw add <群号> <关键词>\n"
        "kw remove <群号> <关键词>\n"
        "kw set <群号> <词1,词2,...>\n"
        "rule list\n"
        "rule addgroup <群号>\n"
        "rule delgroup <群号>\n"
        "rule addtarget <群号> <QQ号>\n"
        "rule deltarget <群号> <QQ号>\n"
        "rule enable <群号>\n"
        "rule disable <群号>\n"
        "rule help"
    )


def _render_rule(rule: dict) -> str:
    return (
        f"群号: {rule['group_id']}\n"
        f"状态: {'启用' if rule['enabled'] else '禁用'}\n"
        f"目标QQ: {', '.join(map(str, rule['targets'])) or '无'}\n"
        f"关键词: {', '.join(rule['keywords']) or '无'}\n"
        f"正则: {'是' if rule['use_regex'] else '否'}"
    )


async def _reply_private(bot: Bot, user_id: int, message: str) -> None:
    logger.info("Reply private message to %s: %s", user_id, message.replace("\n", " | "))
    await bot.call_api("send_msg", message_type="private", user_id=user_id, message=message)


@matcher.handle()
async def handle_group_message(bot: Bot, event: GroupMessageEvent) -> None:
    rules = _load_rules_from_file()
    rule = _find_rule(rules, event.group_id)
    if not rule or not rule["enabled"]:
        raise FinishedException

    text = event.get_plaintext().strip()
    if not text:
        raise FinishedException

    matched = _match_keywords(text, rule["keywords"], rule["use_regex"])
    if not matched:
        raise FinishedException

    message_key = f"{event.group_id}:{event.message_id}"
    if _is_duplicate(message_key):
        logger.warning("Skip duplicate forwarded message: %s", message_key)
        raise FinishedException

    sender_name = event.sender.card or event.sender.nickname or str(event.user_id)
    forward_text = (
        "[关键词命中]\n"
        f"群号: {event.group_id}\n"
        f"发送者: {sender_name} ({event.user_id})\n"
        f"命中词: {', '.join(matched)}\n"
        f"内容: {text}"
    )

    for target_qq in rule["targets"]:
        await bot.call_api("send_msg", message_type="private", user_id=target_qq, message=forward_text)

    logger.info("Forwarded group message %s from %s to %s", event.message_id, event.group_id, rule["targets"])


@admin_matcher.handle()
async def handle_admin_command(bot: Bot, event: PrivateMessageEvent) -> None:
    if ADMIN_QQS and event.user_id not in ADMIN_QQS:
        raise FinishedException

    text = event.get_plaintext().strip()
    if not text:
        raise FinishedException

    lower = text.lower()
    if not (lower.startswith("kw") or lower.startswith("rule")):
        raise FinishedException

    parts = text.split(maxsplit=3)
    if len(parts) == 1:
        await _reply_private(bot, event.user_id, _build_admin_help())
        raise FinishedException

    namespace = parts[0].lower()
    command = parts[1].lower() if len(parts) > 1 else "help"
    rules = _load_rules_from_file()

    if namespace == "rule":
        if command in {"help", "h"}:
            await _reply_private(bot, event.user_id, _build_admin_help())
            raise FinishedException

        if command == "list":
            if not rules:
                await _reply_private(bot, event.user_id, "当前没有任何群规则")
                raise FinishedException
            message = "\n\n".join(_render_rule(rule) for rule in rules)
            await _reply_private(bot, event.user_id, message)
            raise FinishedException

        if len(parts) < 3:
            await _reply_private(bot, event.user_id, _build_admin_help())
            raise FinishedException

        group_id = int(parts[2].strip())
        rule = _find_rule(rules, group_id)

        if command == "addgroup":
            if rule:
                await _reply_private(bot, event.user_id, f"群 {group_id} 已存在")
                raise FinishedException
            new_rule = {
                "group_id": group_id,
                "targets": sorted(TARGET_QQS),
                "keywords": [],
                "enabled": True,
                "use_regex": USE_REGEX,
            }
            _save_rules_to_file(_upsert_rule(rules, new_rule))
            await _reply_private(bot, event.user_id, f"已添加群规则\n{_render_rule(new_rule)}")
            raise FinishedException

        if not rule:
            await _reply_private(bot, event.user_id, f"群 {group_id} 不存在")
            raise FinishedException

        if command == "delgroup":
            updated = [item for item in rules if item["group_id"] != group_id]
            _save_rules_to_file(updated)
            await _reply_private(bot, event.user_id, f"已删除群规则 {group_id}")
            raise FinishedException

        if command in {"enable", "disable"}:
            rule["enabled"] = command == "enable"
            _save_rules_to_file(_upsert_rule(rules, rule))
            await _reply_private(bot, event.user_id, _render_rule(rule))
            raise FinishedException

        if command in {"addtarget", "deltarget"}:
            if len(parts) < 4:
                await _reply_private(bot, event.user_id, f"用法: rule {command} <群号> <QQ号>")
                raise FinishedException
            target = int(parts[3].strip())
            targets = set(rule["targets"])
            if command == "addtarget":
                targets.add(target)
            else:
                targets.discard(target)
            rule["targets"] = sorted(targets)
            _save_rules_to_file(_upsert_rule(rules, rule))
            await _reply_private(bot, event.user_id, _render_rule(rule))
            raise FinishedException

        await _reply_private(bot, event.user_id, _build_admin_help())
        raise FinishedException

    if namespace == "kw":
        if command in {"help", "h"}:
            await _reply_private(bot, event.user_id, _build_admin_help())
            raise FinishedException

        if len(parts) < 3:
            await _reply_private(bot, event.user_id, "用法: kw list <群号> / kw add <群号> <关键词>")
            raise FinishedException

        group_id = int(parts[2].strip())
        rule = _find_rule(rules, group_id)
        if not rule:
            await _reply_private(bot, event.user_id, f"群 {group_id} 不存在，请先执行 rule addgroup {group_id}")
            raise FinishedException

        if command == "list":
            await _reply_private(bot, event.user_id, _render_rule(rule))
            raise FinishedException

        if len(parts) < 4 or not parts[3].strip():
            await _reply_private(bot, event.user_id, f"用法: kw {command} <群号> <关键词>")
            raise FinishedException

        if command == "add":
            keyword = parts[3].strip()
            if keyword not in rule["keywords"]:
                rule["keywords"].append(keyword)
            _save_rules_to_file(_upsert_rule(rules, rule))
            await _reply_private(bot, event.user_id, _render_rule(rule))
            raise FinishedException

        if command == "remove":
            keyword = parts[3].strip()
            rule["keywords"] = [item for item in rule["keywords"] if item != keyword]
            _save_rules_to_file(_upsert_rule(rules, rule))
            await _reply_private(bot, event.user_id, _render_rule(rule))
            raise FinishedException

        if command == "set":
            keywords = [item.strip() for item in parts[3].replace("，", ",").split(",") if item.strip()]
            rule["keywords"] = keywords
            _save_rules_to_file(_upsert_rule(rules, rule))
            await _reply_private(bot, event.user_id, _render_rule(rule))
            raise FinishedException

    await _reply_private(bot, event.user_id, _build_admin_help())
    raise FinishedException
