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

from rules import find_rule, load_rules, normalize_rule, save_rules, upsert_rule


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
        if use_regex:
            flags = 0 if CASE_SENSITIVE else re.IGNORECASE
            if re.search(keyword, text, flags=flags):
                matched.append(keyword)
        else:
            candidate = keyword if CASE_SENSITIVE else keyword.lower()
            if candidate in normalized:
                matched.append(keyword)
    return matched


def _build_default_rules() -> list[dict]:
    if not DEFAULT_ALLOWED_GROUPS:
        return []
    return [
        normalize_rule(
            {
                "group_id": group_id,
                "targets": sorted(TARGET_QQS),
                "keywords": KEYWORDS,
                "enabled": True,
                "use_regex": USE_REGEX,
            }
        )
        for group_id in DEFAULT_ALLOWED_GROUPS
    ]


def _load_legacy_keywords() -> list[str]:
    if not LEGACY_KEYWORDS_FILE.exists():
        return KEYWORDS.copy()
    try:
        data = json.loads(LEGACY_KEYWORDS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to parse %s, falling back to KEYWORDS env", LEGACY_KEYWORDS_FILE)
        return KEYWORDS.copy()
    file_keywords = data.get("keywords", [])
    return [str(item).strip() for item in file_keywords if str(item).strip()]


def _migrate_legacy_rules() -> list[dict]:
    keywords = _load_legacy_keywords()
    rules = []
    for group_id in DEFAULT_ALLOWED_GROUPS:
        rules.append(
            normalize_rule(
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


def _save_rules_file(rules: list[dict]) -> list[dict]:
    global _rules_cache, _rules_mtime
    normalized = [normalize_rule(rule) for rule in rules]
    normalized.sort(key=lambda item: item["group_id"])
    try:
        save_rules(normalized)
    except OSError as exc:
        logger.error("Failed to save rules: %s", exc)
        return _rules_cache
    _rules_cache = normalized
    _rules_mtime = RULES_FILE.stat().st_mtime
    logger.info("Saved rules to %s", RULES_FILE)
    return normalized


def _load_rules_from_file() -> list[dict]:
    global _rules_cache, _rules_mtime

    if not RULES_FILE.exists():
        migrated = _migrate_legacy_rules() or _build_default_rules()
        if migrated:
            return _save_rules_file(migrated)
        _rules_cache = []
        return _rules_cache

    stat = RULES_FILE.stat()
    if _rules_mtime is not None and stat.st_mtime == _rules_mtime:
        return _rules_cache

    try:
        raw_rules = load_rules()
    except (RuntimeError, ValueError) as exc:
        logger.error("Failed to load rules: %s", exc)
        _rules_cache = []
        _rules_mtime = stat.st_mtime
        return _rules_cache

    _rules_cache = [normalize_rule(item) for item in raw_rules]
    _rules_cache.sort(key=lambda item: item["group_id"])
    _rules_mtime = stat.st_mtime
    logger.info("Reloaded rules from %s", RULES_FILE)
    return _rules_cache


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
    rules = _load_rules_from_file()
    single = len(rules) == 1

    if single:
        return (
            "简化命令（单群模式）:\n"
            "status         — 查看当前规则\n"
            "add <关键词>    — 添加关键词\n"
            "remove <关键词> — 删除关键词\n"
            "set <词1,词2>   — 替换全部关键词\n"
            "on              — 启用监听\n"
            "off             — 禁用监听\n"
            "help            — 显示帮助\n\n"
            "完整命令（多群模式）:\n"
            "rule list / addgroup <群号> / delgroup <群号>\n"
            "rule addtarget <群号> <QQ号> / deltarget <群号> <QQ号>\n"
            "rule enable <群号> / disable <群号>\n"
            "kw list <群号> / kw add <群号> <关键词>\n"
            "kw remove <群号> <关键词> / kw set <群号> <词1,词2>"
        )

    return (
        "管理命令:\n"
        "rule list\n"
        "rule addgroup <群号>\n"
        "rule delgroup <群号>\n"
        "rule addtarget <群号> <QQ号>\n"
        "rule deltarget <群号> <QQ号>\n"
        "rule enable <群号>\n"
        "rule disable <群号>\n"
        "kw list <群号>\n"
        "kw add <群号> <关键词>\n"
        "kw remove <群号> <关键词>\n"
        "kw set <群号> <词1,词2,...>\n"
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
    rule = find_rule(rules, event.group_id)
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


async def _handle_rule_command(bot: Bot, user_id: int, parts: list[str]) -> None:
    command = parts[1].lower() if len(parts) > 1 else "help"
    rules = _load_rules_from_file()

    if command in {"help", "h"}:
        await _reply_private(bot, user_id, _build_admin_help())
        return

    if command == "list":
        if not rules:
            await _reply_private(bot, user_id, "当前没有任何群规则")
            return
        await _reply_private(bot, user_id, "\n\n".join(_render_rule(rule) for rule in rules))
        return

    if len(parts) < 3:
        await _reply_private(bot, user_id, _build_admin_help())
        return

    try:
        group_id = int(parts[2].strip())
    except ValueError:
        await _reply_private(bot, user_id, f"无效的群号: {parts[2]}")
        return

    rule = find_rule(rules, group_id)

    if command == "addgroup":
        if rule:
            await _reply_private(bot, user_id, f"群 {group_id} 已存在")
            return
        new_rule = {
            "group_id": group_id,
            "targets": sorted(TARGET_QQS),
            "keywords": [],
            "enabled": True,
            "use_regex": USE_REGEX,
        }
        _save_rules_file(upsert_rule(rules, new_rule))
        await _reply_private(bot, user_id, f"已添加群规则\n{_render_rule(new_rule)}")
        return

    if not rule:
        await _reply_private(bot, user_id, f"群 {group_id} 不存在")
        return

    if command == "delgroup":
        updated = [item for item in rules if item["group_id"] != group_id]
        _save_rules_file(updated)
        await _reply_private(bot, user_id, f"已删除群规则 {group_id}")
        return

    if command in {"enable", "disable"}:
        rule["enabled"] = command == "enable"
        _save_rules_file(upsert_rule(rules, rule))
        await _reply_private(bot, user_id, _render_rule(rule))
        return

    if command in {"addtarget", "deltarget"}:
        if len(parts) < 4:
            await _reply_private(bot, user_id, f"用法: rule {command} <群号> <QQ号>")
            return
        try:
            target = int(parts[3].strip())
        except ValueError:
            await _reply_private(bot, user_id, f"无效的 QQ 号: {parts[3]}")
            return
        targets = set(rule["targets"])
        if command == "addtarget":
            targets.add(target)
        else:
            targets.discard(target)
        rule["targets"] = sorted(targets)
        _save_rules_file(upsert_rule(rules, rule))
        await _reply_private(bot, user_id, _render_rule(rule))
        return

    await _reply_private(bot, user_id, _build_admin_help())


async def _handle_kw_command(bot: Bot, user_id: int, parts: list[str]) -> None:
    command = parts[1].lower() if len(parts) > 1 else "list"
    rules = _load_rules_from_file()

    if command in {"help", "h"}:
        await _reply_private(bot, user_id, _build_admin_help())
        return

    if len(parts) < 3:
        await _reply_private(bot, user_id, "用法: kw list <群号> / kw add <群号> <关键词>")
        return

    try:
        group_id = int(parts[2].strip())
    except ValueError:
        await _reply_private(bot, user_id, f"无效的群号: {parts[2]}")
        return

    rule = find_rule(rules, group_id)
    if not rule:
        await _reply_private(bot, user_id, f"群 {group_id} 不存在，请先执行 rule addgroup {group_id}")
        return

    if command == "list":
        await _reply_private(bot, user_id, _render_rule(rule))
        return

    if len(parts) < 4 or not parts[3].strip():
        await _reply_private(bot, user_id, f"用法: kw {command} <群号> <关键词>")
        return

    if command == "add":
        keyword = parts[3].strip()
        if keyword not in rule["keywords"]:
            rule["keywords"].append(keyword)
        _save_rules_file(upsert_rule(rules, rule))
        await _reply_private(bot, user_id, _render_rule(rule))
        return

    if command == "remove":
        keyword = parts[3].strip()
        rule["keywords"] = [item for item in rule["keywords"] if item != keyword]
        _save_rules_file(upsert_rule(rules, rule))
        await _reply_private(bot, user_id, _render_rule(rule))
        return

    if command == "set":
        keywords = [item.strip() for item in parts[3].replace("，", ",").split(",") if item.strip()]
        rule["keywords"] = keywords
        _save_rules_file(upsert_rule(rules, rule))
        await _reply_private(bot, user_id, _render_rule(rule))
        return

    await _reply_private(bot, user_id, _build_admin_help())


SIMPLE_COMMANDS = {"status", "add", "remove", "set", "on", "off", "help"}


async def _handle_simple_command(bot: Bot, user_id: int, text: str) -> None:
    parts = text.split(maxsplit=2)
    command = parts[0].lower()

    if command == "help":
        await _reply_private(bot, user_id, _build_admin_help())
        return

    rules = _load_rules_from_file()
    if len(rules) != 1:
        await _reply_private(bot, user_id, "存在多个群规则，请使用完整命令（输入 help 查看）")
        return

    group_id = rules[0]["group_id"]

    if command == "status":
        await _reply_private(bot, user_id, _render_rule(rules[0]))
        return

    if command == "on":
        rule = find_rule(rules, group_id)
        rule["enabled"] = True
        _save_rules_file(upsert_rule(rules, rule))
        await _reply_private(bot, user_id, _render_rule(rule))
        return

    if command == "off":
        rule = find_rule(rules, group_id)
        rule["enabled"] = False
        _save_rules_file(upsert_rule(rules, rule))
        await _reply_private(bot, user_id, _render_rule(rule))
        return

    if len(parts) < 2 or not parts[1].strip():
        await _reply_private(bot, user_id, f"用法: {command} <关键词>")
        return

    keyword = parts[1].strip()
    rule = find_rule(rules, group_id)

    if command == "add":
        if keyword not in rule["keywords"]:
            rule["keywords"].append(keyword)
        _save_rules_file(upsert_rule(rules, rule))
        await _reply_private(bot, user_id, _render_rule(rule))
        return

    if command == "remove":
        rule["keywords"] = [item for item in rule["keywords"] if item != keyword]
        _save_rules_file(upsert_rule(rules, rule))
        await _reply_private(bot, user_id, _render_rule(rule))
        return

    if command == "set":
        keywords = [item.strip() for item in keyword.replace("，", ",").split(",") if item.strip()]
        rule["keywords"] = keywords
        _save_rules_file(upsert_rule(rules, rule))
        await _reply_private(bot, user_id, _render_rule(rule))
        return


@admin_matcher.handle()
async def handle_admin_command(bot: Bot, event: PrivateMessageEvent) -> None:
    if ADMIN_QQS and event.user_id not in ADMIN_QQS:
        raise FinishedException

    text = event.get_plaintext().strip()
    if not text:
        raise FinishedException

    lower = text.lower()
    first_word = lower.split(maxsplit=1)[0]

    if first_word in SIMPLE_COMMANDS:
        await _handle_simple_command(bot, event.user_id, text)
        raise FinishedException

    if not (first_word == "kw" or first_word == "rule"):
        raise FinishedException

    parts = text.split(maxsplit=3)

    namespace = parts[0].lower()
    if namespace == "rule":
        await _handle_rule_command(bot, event.user_id, parts)
    elif namespace == "kw":
        await _handle_kw_command(bot, event.user_id, parts)
    else:
        await _reply_private(bot, event.user_id, _build_admin_help())

    raise FinishedException
