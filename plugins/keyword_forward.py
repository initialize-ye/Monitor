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
from .remind import handle_command as _handle_remind_command
from image_renderer import render_text_to_image
from quotes import random_quote
from session_manager import SessionManager


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

# Per-keyword cooldown (prevent same keyword from triggering repeatedly)
_keyword_cooldown: dict[str, float] = {}
_keyword_cooldown_seconds = 15

# Keyword hit stats (in-memory, resets on restart)
_keyword_stats: dict[str, int] = {}

# Session manager for interactive menus (max 1000 concurrent sessions)
_session_manager = SessionManager(timeout_seconds=300, max_sessions=1000)


def _check_keyword_cooldown(group_id: int, word: str) -> bool:
    key = f"{group_id}:{word}"
    now = time.time()
    last = _keyword_cooldown.get(key)
    if last and now - last < _keyword_cooldown_seconds:
        return True
    _keyword_cooldown[key] = now
    return False


def _track_keyword_hit(group_id: int, word: str) -> None:
    today = time.strftime("%Y-%m-%d")
    key = f"{today}:{group_id}:{word}"
    _keyword_stats[key] = _keyword_stats.get(key, 0) + 1

_rules_mtime: float | None = None
_rules_cache: list[dict] = []

matcher = on_message(rule=is_type(GroupMessageEvent), priority=10, block=False)
admin_matcher = on_message(rule=is_type(PrivateMessageEvent), priority=5, block=True)


# --- keyword matching ---

def _normalize(text: str) -> str:
    return text if CASE_SENSITIVE else text.lower()


def _match_keywords(text: str, keywords: list[dict], use_regex: bool) -> list[str]:
    matched: list[str] = []
    normalized = _normalize(text)
    for kw in keywords:
        if not kw.get("enabled", True):
            continue
        word = kw["word"]
        if use_regex:
            flags = 0 if CASE_SENSITIVE else re.IGNORECASE
            if re.search(word, text, flags=flags):
                matched.append(word)
        else:
            candidate = word if CASE_SENSITIVE else word.lower()
            if candidate in normalized:
                matched.append(word)
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


async def _reply_image(bot: Bot, user_id: int, text: str, title: str = "Bot") -> None:
    """Render text to a styled image and send it, fall back to plain text on error."""
    try:
        b64 = render_text_to_image(text, title=title)
        await bot.call_api("send_msg", message_type="private", user_id=user_id, message=[
            {"type": "image", "data": {"file": b64}},
        ])
    except Exception:
        await _reply_private(bot, user_id, text)


def _build_admin_help() -> str:
    return (
        "— 关键词监控 —\n"
        "status                      查看规则\n"
        "add <关键词>                添加关键词\n"
        "remove <编号>               删除关键词\n"
        "set <词1,词2>               替换全部关键词\n"
        "disable <编号>              临时禁用关键词\n"
        "enable <编号>               恢复禁用关键词\n"
        "stats                       今日命中统计\n"
        "quote                       随机励志名言\n"
        "on / off                    启用/禁用监听\n"
        "\n"
        "— 定时提醒 —\n"
        "remind <HH:MM> <内容>                   每天提醒\n"
        "remind add <HH:MM> <内容>               每天提醒\n"
        "remind once <日期> <时间> <内容>        单次提醒\n"
        "remind workday <HH:MM> <内容>           工作日提醒\n"
        "remind interval <分钟> <内容>           间隔提醒\n"
        "remind period <时间> <分钟> <内容>      周期催促\n"
        "remind quote <HH:MM>                    每日一言\n"
        "remind done <编号>                      标记完成\n"
        "remind remove <编号>                    删除提醒\n"
        "remind list                             查看提醒\n"
        "\n"
        "— 高级管理 (多群) —\n"
        "rule addgroup <群号>                添加群规则\n"
        "rule delgroup <群号>                删除群规则\n"
        "rule addtarget <群号> <QQ>          添加转发目标\n"
        "rule deltarget <群号> <QQ>          删除转发目标\n"
        "\n"
        "— 其他 —\n"
        "cancel                              取消当前操作\n"
        "\n"
        "💡 单群模式下关键词命令无需群号"
    )


def _render_rule(rule: dict) -> str:
    kw_lines = "\n".join(
        f"  {i}. {kw['word']}{'' if kw.get('enabled', True) else ' ✗'}"
        for i, kw in enumerate(rule["keywords"], 1)
    ) if rule["keywords"] else "无"
    return (
        f"群号: {rule['group_id']}\n"
        f"状态: {'✅ 启用' if rule['enabled'] else '⛔ 禁用'}\n"
        f"目标: {', '.join(map(str, rule['targets'])) or '无'}\n"
        f"正则: {'是' if rule['use_regex'] else '否'}\n"
        f"─ 关键词 ─\n"
        f"{kw_lines}"
    )


def _render_rule_with_menu(rule: dict) -> str:
    """Render rule with interactive menu options."""
    base = _render_rule(rule)
    menu = (
        "\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📋 快捷操作\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "1️⃣  添加关键词\n"
        "2️⃣  删除关键词\n"
        "3️⃣  禁用/启用关键词\n"
        "4️⃣  查看统计\n"
        "5️⃣  获取随机名言\n"
        "\n"
        "💡 回复数字选择操作\n"
        "💡 输入 cancel 取消操作"
    )
    return base + menu




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

    # Filter keywords on cooldown
    matched = [kw for kw in matched if not _check_keyword_cooldown(event.group_id, kw)]
    if not matched:
        raise FinishedException

    # Track stats
    for kw in matched:
        _track_keyword_hit(event.group_id, kw)

    message_key = f"{event.group_id}:{event.message_id}"
    if _is_duplicate(message_key):
        logger.warning("Skip duplicate forwarded message: %s", message_key)
        raise FinishedException

    sender_name = event.sender.card or event.sender.nickname or str(event.user_id)
    msg_time = time.strftime("%m-%d %H:%M", time.localtime(event.time))

    # Check for images and @mentions
    extra_parts: list[str] = []
    image_count = sum(1 for seg in event.message if seg.type == "image")
    if image_count:
        extra_parts.append(f"[图片×{image_count}]")
    for seg in event.message:
        if seg.type == "at":
            qq = seg.data.get("qq", "")
            extra_parts.append(f"@{qq if qq != 'all' else '全体成员'}")
    extra = " ".join(extra_parts)

    forward_text = (
        f"🔍 关键词命中 · {msg_time}\n"
        f"─────────────────────\n"
        f"👤 {sender_name} ({event.user_id})\n"
        f"🎯 命中: {', '.join(matched)}\n"
        f"─────────────────────\n"
        f"{text}"
    )
    if extra:
        forward_text += f"\n📎 {extra}"

    for target_qq in rule["targets"]:
        await bot.call_api("send_msg", message_type="private", user_id=target_qq, message=forward_text)

    logger.info("Forwarded group message %s from %s to %s", event.message_id, event.group_id, rule["targets"])


# --- unified keyword/rule commands ---

async def _handle_command(bot: Bot, user_id: int, command: str, text: str) -> None:
    rules = _load_rules_from_file()

    if command in {"help", "h"}:
        await _reply_image(bot, user_id, _build_admin_help(), title="帮助")
        return

    if command == "cancel":
        session = await _session_manager.get_state(user_id)
        if session:
            await _session_manager.clear_state(user_id)
            await _reply_private(bot, user_id, "✅ 已取消当前操作")
        else:
            await _reply_private(bot, user_id, "💡 当前没有进行中的操作")
        return

    if command == "status":
        if not rules:
            await _reply_private(bot, user_id, "当前没有任何群规则")
            return
        group_id_arg, _ = _resolve_group_id(text)
        if group_id_arg is not None:
            rule = find_rule(rules, group_id_arg)
            if not rule:
                await _reply_private(bot, user_id, f"群 {group_id_arg} 不存在")
                return
            # Set session state for interactive menu
            await _session_manager.set_state(user_id, "menu_status", group_id=rule["group_id"])
            await _reply_image(bot, user_id, _render_rule_with_menu(rule), title=f"群 {rule['group_id']}")
        else:
            if len(rules) == 1:
                # Single rule: show with menu
                await _session_manager.set_state(user_id, "menu_status", group_id=rules[0]["group_id"])
                await _reply_image(bot, user_id, _render_rule_with_menu(rules[0]), title=f"群 {rules[0]['group_id']}")
            else:
                # Multiple rules: show list without menu
                await _reply_image(bot, user_id, "\n\n".join(_render_rule(r) for r in rules), title="规则列表")
        return

    if command in {"on", "off"}:
        group_id_arg, _ = _resolve_group_id(text)
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
        await _reply_image(bot, user_id, _render_rule(rule), title=f"群 {rule['group_id']}")
        return

    if command == "remove":
        parts = text.split(maxsplit=2)
        if len(parts) < 2 or not parts[1].strip().isdigit():
            await _reply_private(bot, user_id, f"用法: remove [群号] <编号>\n使用 status 查看编号")
            return
        if len(parts) > 2 and parts[2].strip().isdigit():
            group_id = int(parts[1])
            idx = int(parts[2])
        elif len(parts) > 2:
            await _reply_private(bot, user_id, "编号必须是数字")
            return
        else:
            idx = int(parts[1])
            if len(rules) != 1:
                await _reply_private(bot, user_id, "存在多个群规则，请指定群号: remove <群号> <编号>")
                return
            group_id = rules[0]["group_id"]

        rule = find_rule(rules, group_id)
        if not rule:
            await _reply_private(bot, user_id, f"群 {group_id} 不存在")
            return
        if 1 <= idx <= len(rule["keywords"]):
            removed = rule["keywords"].pop(idx - 1)
            _save_rules_file(upsert_rule(rules, rule))
            await _reply_image(bot, user_id, f"已删除关键词 [{idx}] {removed['word']}\n{_render_rule(rule)}", title=f"群 {group_id}")
        else:
            await _reply_private(bot, user_id, f"编号 {idx} 不存在，当前共 {len(rule['keywords'])} 个关键词")
        return

    if command in {"add", "set"}:
        group_id_arg, keyword = _resolve_group_id(text)
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
            if not any(kw["word"] == keyword for kw in rule["keywords"]):
                rule["keywords"].append({"word": keyword, "enabled": True})
                _save_rules_file(upsert_rule(rules, rule))
        else:
            rule["keywords"] = [
                {"word": item.strip(), "enabled": True}
                for item in keyword.replace("，", ",").split(",") if item.strip()
            ]
            _save_rules_file(upsert_rule(rules, rule))

        await _reply_image(bot, user_id, _render_rule(rule), title=f"群 {group_id}")
        return

    if command == "stats":
        today = time.strftime("%Y-%m-%d")
        today_hits = [(k.split(":", 2)[-1], v) for k, v in _keyword_stats.items() if k.startswith(today)]
        if not today_hits:
            await _reply_private(bot, user_id, "今日暂无命中统计")
            return
        today_hits.sort(key=lambda x: -x[1])
        lines = [f"📊 今日关键词统计 · {today}", ""]
        lines.extend(f"  ▸ {word}  {count}次" for word, count in today_hits)
        await _reply_private(bot, user_id, "\n".join(lines))
        return

    if command == "quote":
        await _reply_private(bot, user_id, f"📖 {await random_quote()}")
        return

    if command in {"disable", "enable"}:
        parts = text.split(maxsplit=2)
        if len(parts) < 2 or not parts[1].strip().isdigit():
            await _reply_private(bot, user_id, f"用法: {command} <编号>\n使用 status 查看编号")
            return
        if len(parts) > 2 and parts[2].strip().isdigit():
            group_id = int(parts[1])
            idx = int(parts[2])
        elif len(parts) > 2:
            await _reply_private(bot, user_id, "编号必须是数字")
            return
        else:
            idx = int(parts[1])
            if len(rules) != 1:
                await _reply_private(bot, user_id, f"存在多个群规则，请指定群号: {command} <群号> <编号>")
                return
            group_id = rules[0]["group_id"]

        rule = find_rule(rules, group_id)
        if not rule:
            await _reply_private(bot, user_id, f"群 {group_id} 不存在")
            return
        if 1 <= idx <= len(rule["keywords"]):
            rule["keywords"][idx - 1]["enabled"] = command == "enable"
            _save_rules_file(upsert_rule(rules, rule))
            status = "已启用" if command == "enable" else "已禁用"
            await _reply_image(bot, user_id, f"{status}关键词 [{idx}] {rule['keywords'][idx - 1]['word']}\n{_render_rule(rule)}", title=f"群 {group_id}")
        else:
            await _reply_private(bot, user_id, f"编号 {idx} 不存在，当前共 {len(rule['keywords'])} 个关键词")
        return

    await _reply_image(bot, user_id, _build_admin_help(), title="帮助")

async def _handle_rule_advanced(bot: Bot, user_id: int, parts: list[str]) -> None:
    if len(parts) < 2:
        await _reply_image(bot, user_id, _build_admin_help(), title="帮助")
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
        await _reply_image(bot, user_id, f"已添加群规则\n{_render_rule(new_rule)}", title=f"群 {group_id}")

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
        await _reply_image(bot, user_id, _render_rule(rule), title=f"群 {group_id}")

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
        await _reply_image(bot, user_id, _render_rule(rule), title=f"群 {group_id}")

    else:
        await _reply_image(bot, user_id, _build_admin_help(), title="帮助")


async def _handle_session_input(bot: Bot, user_id: int, text: str) -> bool:
    """Handle user input based on session state.

    Returns True if input was handled by session, False otherwise.
    """
    async def _get_rule_or_fail(group_id: int) -> dict | None:
        """Get rule by group_id, send error and clear session if not found."""
        rule = find_rule(rules, group_id)
        if not rule:
            await _session_manager.clear_state(user_id)
            await _reply_private(bot, user_id, f"群 {group_id} 不存在")
        return rule

    try:
        # Check if user's previous session expired
        if await _session_manager.check_and_clear_expired_flag(user_id):
            await _reply_private(bot, user_id, "⏱️ 上次操作已超时，请重新开始")
            return False

        session = await _session_manager.get_state(user_id)
        if not session:
            return False

        state = session["state"]
        rules = _load_rules_from_file()

        # Handle menu selection
        if state == "menu_status":
            group_id = session["group_id"]
            rule = await _get_rule_or_fail(group_id)
            if not rule:
                return True

            if text == "1":
                # Add keyword
                await _session_manager.set_state(user_id, "awaiting_keyword_add", group_id=group_id)
                await _reply_private(bot, user_id, "请输入要添加的关键词：")
                return True
            elif text == "2":
                # Delete keyword
                if not rule["keywords"]:
                    await _reply_private(bot, user_id, "当前没有关键词")
                    await _session_manager.clear_state(user_id)
                    return True
                await _session_manager.set_state(user_id, "awaiting_keyword_remove", group_id=group_id)
                kw_list = "\n".join(f"{i}. {kw['word']}" for i, kw in enumerate(rule["keywords"], 1))
                await _reply_private(bot, user_id, f"请输入要删除的关键词编号：\n{kw_list}")
                return True
            elif text == "3":
                # Toggle keyword enable/disable
                if not rule["keywords"]:
                    await _reply_private(bot, user_id, "当前没有关键词")
                    await _session_manager.clear_state(user_id)
                    return True
                await _session_manager.set_state(user_id, "awaiting_keyword_toggle", group_id=group_id)
                kw_list = "\n".join(
                    f"{i}. {kw['word']} {'✅' if kw.get('enabled', True) else '⛔'}"
                    for i, kw in enumerate(rule["keywords"], 1)
                )
                await _reply_private(bot, user_id, f"请输入要切换状态的关键词编号：\n{kw_list}")
                return True
            elif text == "4":
                # Show stats
                await _session_manager.clear_state(user_id)
                today = time.strftime("%Y-%m-%d")
                today_hits = [(k.split(":", 2)[-1], v) for k, v in _keyword_stats.items() if k.startswith(today)]
                if not today_hits:
                    await _reply_private(bot, user_id, "今日暂无命中统计")
                    return True
                today_hits.sort(key=lambda x: -x[1])
                lines = [f"📊 今日关键词统计 · {today}", ""]
                lines.extend(f"  ▸ {word}  {count}次" for word, count in today_hits)
                await _reply_private(bot, user_id, "\n".join(lines))
                return True
            elif text == "5":
                # Random quote
                await _session_manager.clear_state(user_id)
                await _reply_private(bot, user_id, f"📖 {await random_quote()}")
                return True
            else:
                # Invalid option
                await _reply_private(bot, user_id, "❌ 无效选项，请输入 1-5")
                return True

        # Handle keyword add
        elif state == "awaiting_keyword_add":
            group_id = session["group_id"]
            rule = await _get_rule_or_fail(group_id)
            if not rule:
                return True

            keyword = text.strip()
            if not keyword:
                await _reply_private(bot, user_id, "❌ 关键词不能为空")
                return True

            if not any(kw["word"] == keyword for kw in rule["keywords"]):
                rule["keywords"].append({"word": keyword, "enabled": True})
                _save_rules_file(upsert_rule(rules, rule))

            await _session_manager.clear_state(user_id)
            await _reply_image(bot, user_id, f"✅ 已添加关键词 \"{keyword}\"\n\n{_render_rule(rule)}", title=f"群 {group_id}")
            return True

        # Handle keyword remove
        elif state == "awaiting_keyword_remove":
            group_id = session["group_id"]
            rule = await _get_rule_or_fail(group_id)
            if not rule:
                return True

            try:
                idx = int(text.strip())
            except ValueError:
                await _reply_private(bot, user_id, "❌ 请输入有效的数字")
                return True

            if 1 <= idx <= len(rule["keywords"]):
                removed = rule["keywords"].pop(idx - 1)
                _save_rules_file(upsert_rule(rules, rule))
                await _session_manager.clear_state(user_id)
                await _reply_image(bot, user_id, f"✅ 已删除关键词 \"{removed['word']}\"\n\n{_render_rule(rule)}", title=f"群 {group_id}")
            else:
                await _reply_private(bot, user_id, f"❌ 编号 {idx} 不存在，当前共 {len(rule['keywords'])} 个关键词")
            return True

        # Handle keyword toggle
        elif state == "awaiting_keyword_toggle":
            group_id = session["group_id"]
            rule = await _get_rule_or_fail(group_id)
            if not rule:
                return True

            try:
                idx = int(text.strip())
            except ValueError:
                await _reply_private(bot, user_id, "❌ 请输入有效的数字")
                return True

            if 1 <= idx <= len(rule["keywords"]):
                kw = rule["keywords"][idx - 1]
                kw["enabled"] = not kw.get("enabled", True)
                _save_rules_file(upsert_rule(rules, rule))
                status = "已启用" if kw["enabled"] else "已禁用"
                await _session_manager.clear_state(user_id)
                await _reply_image(bot, user_id, f"✅ {status}关键词 \"{kw['word']}\"\n\n{_render_rule(rule)}", title=f"群 {group_id}")
            else:
                await _reply_private(bot, user_id, f"❌ 编号 {idx} 不存在，当前共 {len(rule['keywords'])} 个关键词")
            return True

        return False

    except Exception as e:
        logger.error("Session input handling failed for user %s: %s", user_id, e, exc_info=True)
        await _session_manager.clear_state(user_id)
        await _reply_private(bot, user_id, "❌ 操作失败，会话已重置。请重新开始。")
        return True


# --- main dispatch ---

COMMANDS = {"status", "add", "remove", "set", "on", "off", "disable", "enable", "stats", "quote", "help", "h", "rule", "remind", "cancel"}


@admin_matcher.handle()
async def handle_admin_command(bot: Bot, event: PrivateMessageEvent) -> None:
    if ADMIN_QQS and event.user_id not in ADMIN_QQS:
        raise FinishedException

    text = event.get_plaintext().strip()
    if not text:
        raise FinishedException

    first_word = text.split(maxsplit=1)[0].lower()

    # If user types a command while in session, clear session and process command
    if first_word in COMMANDS:
        await _session_manager.clear_state(event.user_id)
    else:
        # Check if user has active session state
        if await _handle_session_input(bot, event.user_id, text):
            raise FinishedException

    if first_word not in COMMANDS:
        raise FinishedException

    if first_word == "remind":
        await _handle_remind_command(bot, event.user_id, text)
    elif first_word == "rule":
        await _handle_rule_advanced(bot, event.user_id, text.split(maxsplit=3))
    else:
        await _handle_command(bot, event.user_id, first_word, text)

    raise FinishedException


# --- session cleanup task ---

try:
    from nonebot_plugin_apscheduler import scheduler

    @scheduler.scheduled_job("interval", minutes=10, id="cleanup_sessions")
    async def cleanup_expired_sessions() -> None:
        """Clean up expired user sessions every 10 minutes."""
        count = await _session_manager.cleanup_expired()
        if count > 0:
            logger.info("Cleaned up %d expired sessions", count)
except ImportError:
    logger.warning("nonebot-plugin-apscheduler not available, session cleanup disabled")
