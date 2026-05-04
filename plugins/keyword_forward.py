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
from reminders import load_reminders, save_reminders, normalize_reminder, next_reminder_id


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

_recent_keys: deque[tuple[float, str]] = deque(maxlen=1000)
_recent_seen: set[str] = set()
_dedupe_seconds = 30

_rules_mtime: float | None = None
_rules_cache: list[dict] = []

matcher = on_message(rule=is_type(GroupMessageEvent), priority=10, block=False)
admin_matcher = on_message(rule=is_type(PrivateMessageEvent), priority=5, block=True)


# --- keyword matching ---

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


# --- rules persistence ---

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
        _rules_mtime = stat.st_mtime
        return _rules_cache or []

    _rules_cache = [normalize_rule(item) for item in raw_rules]
    _rules_cache.sort(key=lambda item: item["group_id"])
    _rules_mtime = stat.st_mtime
    logger.info("Reloaded rules from %s", RULES_FILE)
    return _rules_cache


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


# --- dedup ---

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


# --- helpers ---

def _build_admin_help() -> str:
    return (
        "命令:\n"
        "status [群号]         — 查看规则\n"
        "add [群号] <关键词>    — 添加关键词\n"
        "remove [群号] <关键词> — 删除关键词\n"
        "set [群号] <词1,词2>  — 替换全部关键词\n"
        "on [群号]             — 启用监听\n"
        "off [群号]            — 禁用监听\n"
        "help                  — 显示帮助\n\n"
        "提醒:\n"
        "remind add <HH:MM> <内容>  — 添加每日提醒\n"
        "remind remove <编号>       — 删除提醒\n"
        "remind list                — 查看所有提醒\n\n"
        "高级命令:\n"
        "rule addgroup <群号>\n"
        "rule delgroup <群号>\n"
        "rule addtarget <群号> <QQ号>\n"
        "rule deltarget <群号> <QQ号>"
    )


def _render_rule(rule: dict) -> str:
    return (
        f"群号: {rule['group_id']}\n"
        f"状态: {'启用' if rule['enabled'] else '禁用'}\n"
        f"目标QQ: {', '.join(map(str, rule['targets'])) or '无'}\n"
        f"关键词: {', '.join(rule['keywords']) or '无'}\n"
        f"正则: {'是' if rule['use_regex'] else '否'}"
    )


def _render_reminder(rem: dict) -> str:
    return (
        f"编号: {rem['id']}\n"
        f"时间: {rem['hour']:02d}:{rem['minute']:02d} 每天\n"
        f"内容: {rem['message']}\n"
        f"目标QQ: {', '.join(map(str, rem['targets'])) or '无'}\n"
        f"状态: {'启用' if rem['enabled'] else '禁用'}"
    )


async def _reply_private(bot: Bot, user_id: int, message: str) -> None:
    logger.info("Reply private message to %s: %s", user_id, message.replace("\n", " | "))
    await bot.call_api("send_msg", message_type="private", user_id=user_id, message=message)


def _resolve_group_id(text: str) -> tuple[int | None, str | None]:
    """Detect if the first arg after command is a group_id (pure digits).

    Returns (group_id_or_None, keyword_or_None).
    """
    parts = text.split(maxsplit=2)
    if len(parts) < 2:
        return (None, None)

    first = parts[1]
    if first.isdigit():
        group_id = int(first)
        keyword = parts[2].strip() if len(parts) > 2 else None
        return (group_id, keyword)

    keyword = first.strip()
    return (None, keyword)


# --- group message handler ---

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


# --- unified keyword/rule commands ---

async def _handle_command(bot: Bot, user_id: int, command: str, text: str) -> None:
    rules = _load_rules_from_file()

    if command in {"help", "h"}:
        await _reply_private(bot, user_id, _build_admin_help())
        return

    if command == "status":
        if not rules:
            await _reply_private(bot, user_id, "当前没有任何群规则")
            return
        group_id_arg, _, _ = _resolve_group_id(text)
        if group_id_arg is not None:
            rule = find_rule(rules, group_id_arg)
            if not rule:
                await _reply_private(bot, user_id, f"群 {group_id_arg} 不存在")
                return
            await _reply_private(bot, user_id, _render_rule(rule))
        else:
            await _reply_private(bot, user_id, "\n\n".join(_render_rule(r) for r in rules))
        return

    if command in {"on", "off"}:
        group_id_arg, _, _ = _resolve_group_id(text)
        if group_id_arg is None and len(rules) != 1:
            await _reply_private(bot, user_id, "存在多个群规则，请指定群号: on <群号>")
            return
        group_id = group_id_arg if group_id_arg is not None else rules[0]["group_id"]
        rule = find_rule(rules, group_id)
        if not rule:
            await _reply_private(bot, user_id, f"群 {group_id} 不存在")
            return
        rule["enabled"] = command == "on"
        _save_rules_file(upsert_rule(rules, rule))
        await _reply_private(bot, user_id, _render_rule(rule))
        return

    if command in {"add", "remove", "set"}:
        group_id_arg, keyword, _ = _resolve_group_id(text)
        if not keyword:
            await _reply_private(bot, user_id, f"用法: {command} [群号] <关键词>")
            return
        if group_id_arg is None and len(rules) != 1:
            await _reply_private(bot, user_id, f"存在多个群规则，请指定群号: {command} <群号> <关键词>")
            return
        group_id = group_id_arg if group_id_arg is not None else rules[0]["group_id"]
        rule = find_rule(rules, group_id)
        if not rule:
            await _reply_private(bot, user_id, f"群 {group_id} 不存在")
            return

        if command == "add":
            if keyword not in rule["keywords"]:
                rule["keywords"].append(keyword)
        elif command == "remove":
            rule["keywords"] = [item for item in rule["keywords"] if item != keyword]
        else:
            rule["keywords"] = [item.strip() for item in keyword.replace("，", ",").split(",") if item.strip()]

        _save_rules_file(upsert_rule(rules, rule))
        await _reply_private(bot, user_id, _render_rule(rule))
        return

    await _reply_private(bot, user_id, _build_admin_help())


# --- remind commands ---

async def _handle_remind_command(bot: Bot, user_id: int, parts: list[str]) -> None:
    if len(parts) < 2:
        await _reply_private(bot, user_id, "用法: remind add/remove/list")
        return

    sub = parts[1].lower()

    if sub == "list":
        try:
            reminders = load_reminders()
        except (RuntimeError, ValueError) as exc:
            await _reply_private(bot, user_id, f"加载提醒失败: {exc}")
            return
        if not reminders:
            await _reply_private(bot, user_id, "当前没有任何提醒")
            return
        await _reply_private(bot, user_id, "\n\n".join(_render_reminder(r) for r in reminders))
        return

    if sub == "add":
        # remind add 10:00 背单词
        if len(parts) < 4 or not parts[2].strip() or not parts[3].strip():
            await _reply_private(bot, user_id, "用法: remind add <HH:MM> <内容>")
            return
        time_str = parts[2].strip()
        message = parts[3].strip()
        match = re.match(r"^(\d{1,2}):(\d{2})$", time_str)
        if not match:
            await _reply_private(bot, user_id, "时间格式错误，请使用 HH:MM，例如 10:00")
            return
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            await _reply_private(bot, user_id, "时间范围错误，小时 0-23，分钟 0-59")
            return

        try:
            reminders = load_reminders()
        except (RuntimeError, ValueError):
            reminders = []

        new_rem = normalize_reminder({
            "id": next_reminder_id(reminders),
            "hour": hour,
            "minute": minute,
            "message": message,
            "targets": sorted(TARGET_QQS),
            "enabled": True,
        })
        reminders.append(new_rem)
        try:
            save_reminders(reminders)
        except OSError as exc:
            await _reply_private(bot, user_id, f"保存失败: {exc}")
            return

        _schedule_reminder(new_rem)
        await _reply_private(bot, user_id, f"已添加提醒\n{_render_reminder(new_rem)}")
        return

    if sub in {"remove", "del"}:
        if len(parts) < 3 or not parts[2].strip():
            await _reply_private(bot, user_id, "用法: remind remove <编号>")
            return
        try:
            rem_id = int(parts[2].strip())
        except ValueError:
            await _reply_private(bot, user_id, "编号必须是数字")
            return

        try:
            reminders = load_reminders()
        except (RuntimeError, ValueError) as exc:
            await _reply_private(bot, user_id, f"加载提醒失败: {exc}")
            return

        target = next((r for r in reminders if r["id"] == rem_id), None)
        if not target:
            await _reply_private(bot, user_id, f"编号 {rem_id} 不存在")
            return

        reminders = [r for r in reminders if r["id"] != rem_id]
        try:
            save_reminders(reminders)
        except OSError as exc:
            await _reply_private(bot, user_id, f"保存失败: {exc}")
            return

        _unschedule_reminder(rem_id)
        await _reply_private(bot, user_id, f"已删除提醒 {rem_id}")
        return

    await _reply_private(bot, user_id, "用法: remind add/remove/list")


# --- reminder scheduling ---

_scheduled_jobs: dict[int, object] = {}


def _schedule_reminder(rem: dict) -> None:
    if not rem["enabled"]:
        return
    try:
        from nonebot_plugin_apscheduler import scheduler
        job_id = f"remind_{rem['id']}"
        scheduler.add_job(
            _fire_reminder,
            "cron",
            hour=rem["hour"],
            minute=rem["minute"],
            id=job_id,
            replace_existing=True,
            args=[rem["id"]],
        )
        _scheduled_jobs[rem["id"]] = job_id
        logger.info("Scheduled reminder %s at %02d:%02d", rem["id"], rem["hour"], rem["minute"])
    except Exception as exc:
        logger.error("Failed to schedule reminder %s: %s", rem["id"], exc)


def _unschedule_reminder(rem_id: int) -> None:
    job_id = _scheduled_jobs.pop(rem_id, None)
    if job_id is None:
        return
    try:
        from nonebot_plugin_apscheduler import scheduler
        scheduler.remove_job(job_id)
        logger.info("Unscheduled reminder %s", rem_id)
    except Exception:
        pass


async def _fire_reminder(rem_id: int) -> None:
    try:
        reminders = load_reminders()
    except (RuntimeError, ValueError):
        logger.error("Failed to load reminders for firing")
        return

    rem = next((r for r in reminders if r["id"] == rem_id), None)
    if not rem or not rem["enabled"]:
        return

    bots = list(nonebot.get_bots().values())
    if not bots:
        logger.warning("No bot available to send reminder %s", rem_id)
        return

    bot = bots[0]
    text = f"[提醒] {rem['message']}"
    for target_qq in rem["targets"]:
        try:
            await bot.call_api("send_msg", message_type="private", user_id=target_qq, message=text)
        except Exception as exc:
            logger.error("Failed to send reminder %s to %s: %s", rem_id, target_qq, exc)

    logger.info("Fired reminder %s: %s", rem_id, rem["message"])


def _restore_reminders() -> None:
    try:
        reminders = load_reminders()
    except (RuntimeError, ValueError):
        logger.warning("No reminders to restore")
        return
    for rem in reminders:
        rem = normalize_reminder(rem)
        _schedule_reminder(rem)
    if reminders:
        logger.info("Restored %d reminders", len(reminders))


# --- advanced rule commands ---

async def _handle_rule_advanced(bot: Bot, user_id: int, parts: list[str]) -> None:
    if len(parts) < 2:
        await _reply_private(bot, user_id, _build_admin_help())
        return

    sub = parts[1].lower()
    rules = _load_rules_from_file()

    if sub == "addgroup":
        if len(parts) < 3:
            await _reply_private(bot, user_id, "用法: rule addgroup <群号>")
            return
        try:
            group_id = int(parts[2].strip())
        except ValueError:
            await _reply_private(bot, user_id, f"无效的群号: {parts[2]}")
            return
        if find_rule(rules, group_id):
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

    elif sub == "delgroup":
        if len(parts) < 3:
            await _reply_private(bot, user_id, "用法: rule delgroup <群号>")
            return
        try:
            group_id = int(parts[2].strip())
        except ValueError:
            await _reply_private(bot, user_id, f"无效的群号: {parts[2]}")
            return
        updated = [item for item in rules if item["group_id"] != group_id]
        _save_rules_file(updated)
        await _reply_private(bot, user_id, f"已删除群规则 {group_id}")

    elif sub == "addtarget":
        if len(parts) < 4:
            await _reply_private(bot, user_id, "用法: rule addtarget <群号> <QQ号>")
            return
        try:
            group_id = int(parts[2].strip())
            target = int(parts[3].strip())
        except ValueError:
            await _reply_private(bot, user_id, "无效的群号或QQ号")
            return
        rule = find_rule(rules, group_id)
        if not rule:
            await _reply_private(bot, user_id, f"群 {group_id} 不存在")
            return
        targets = set(rule["targets"])
        targets.add(target)
        rule["targets"] = sorted(targets)
        _save_rules_file(upsert_rule(rules, rule))
        await _reply_private(bot, user_id, _render_rule(rule))

    elif sub == "deltarget":
        if len(parts) < 4:
            await _reply_private(bot, user_id, "用法: rule deltarget <群号> <QQ号>")
            return
        try:
            group_id = int(parts[2].strip())
            target = int(parts[3].strip())
        except ValueError:
            await _reply_private(bot, user_id, "无效的群号或QQ号")
            return
        rule = find_rule(rules, group_id)
        if not rule:
            await _reply_private(bot, user_id, f"群 {group_id} 不存在")
            return
        targets = set(rule["targets"])
        targets.discard(target)
        rule["targets"] = sorted(targets)
        _save_rules_file(upsert_rule(rules, rule))
        await _reply_private(bot, user_id, _render_rule(rule))

    else:
        await _reply_private(bot, user_id, _build_admin_help())


# --- main dispatch ---

COMMANDS = {"status", "add", "remove", "set", "on", "off", "help", "h", "rule", "remind"}


@admin_matcher.handle()
async def handle_admin_command(bot: Bot, event: PrivateMessageEvent) -> None:
    if ADMIN_QQS and event.user_id not in ADMIN_QQS:
        raise FinishedException

    text = event.get_plaintext().strip()
    if not text:
        raise FinishedException

    first_word = text.split(maxsplit=1)[0].lower()

    if first_word not in COMMANDS:
        raise FinishedException

    if first_word == "remind":
        await _handle_remind_command(bot, event.user_id, text.split(maxsplit=3))
    elif first_word == "rule":
        await _handle_rule_advanced(bot, event.user_id, text.split(maxsplit=3))
    else:
        await _handle_command(bot, event.user_id, first_word, text)

    raise FinishedException


# --- restore reminders on bot startup ---

driver = nonebot.get_driver()


@driver.on_startup
async def _on_startup() -> None:
    _restore_reminders()
