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

# Message buffer for merging forwards (group_id -> list of pending messages)
_message_buffer: dict[int, list[dict]] = {}
_message_buffer_tasks: set[int] = set()
_buffer_timeout_seconds = 3  # Merge messages within 3 seconds


def _check_keyword_cooldown(group_id: int, word: str) -> bool:
    if group_id in _message_buffer_tasks:
        return False
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


async def _flush_message_buffer(bot: Bot, group_id: int) -> None:
    """Flush buffered messages for a group as merged forward message."""
    if group_id not in _message_buffer or not _message_buffer[group_id]:
        return

    messages = _message_buffer.pop(group_id)
    if not messages:
        return

    # Get targets from the first message's rule
    targets = messages[0]["targets"]

    try:
        nodes = []
        for msg in messages:
            nodes.append({
                "type": "node",
                "data": {
                    "name": msg["sender_name"],
                    "uin": msg["sender_id"],
                    "content": msg["content"]
                }
            })

        for target_qq in targets:
            await bot.call_api(
                "send_private_forward_msg",
                user_id=target_qq,
                messages=nodes
            )

        logger.info("Forwarded %d messages as merged forward from group %s to %s", len(messages), group_id, targets)
    except Exception as e:
        logger.error("Failed to send merged forward: %s, falling back to individual messages", e)
        for msg in messages:
            for target_qq in targets:
                await bot.call_api("send_msg", message_type="private", user_id=target_qq, message=msg["text"])


async def _buffer_message(bot: Bot, group_id: int, message_data: dict) -> None:
    """Buffer a message and schedule flush after timeout."""
    import asyncio

    if group_id not in _message_buffer:
        _message_buffer[group_id] = []

    _message_buffer[group_id].append(message_data)
    if group_id in _message_buffer_tasks:
        return

    _message_buffer_tasks.add(group_id)

    async def _delayed_flush():
        try:
            await asyncio.sleep(_buffer_timeout_seconds)
            await _flush_message_buffer(bot, group_id)
        finally:
            _message_buffer_tasks.discard(group_id)

    asyncio.create_task(_delayed_flush())

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


def _save_rules_checked(rules: list[dict]) -> list[dict]:
    global _rules_cache, _rules_mtime
    normalized = [normalize_rule(rule) for rule in rules]
    normalized.sort(key=lambda item: item["group_id"])
    save_rules(normalized)
    _rules_cache = normalized
    _rules_mtime = RULES_FILE.stat().st_mtime
    logger.info("Saved rules to %s", RULES_FILE)
    return normalized


def _save_rules_file(rules: list[dict]) -> list[dict]:
    try:
        return _save_rules_checked(rules)
    except OSError as exc:
        logger.error("Failed to save rules: %s", exc)
        return _rules_cache


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


async def _save_rules_or_reply(bot: Bot, user_id: int, rules: list[dict]) -> list[dict] | None:
    try:
        return _save_rules_checked(rules)
    except OSError as exc:
        logger.error("Failed to save rules: %s", exc)
        await _reply_private(bot, user_id, f"错误: 保存失败: {exc}")
        return None


async def _set_session_or_reply(bot: Bot, user_id: int, state: str, **kwargs) -> bool:
    if await _session_manager.set_state(user_id, state, **kwargs):
        return True
    await _reply_private(bot, user_id, "注意: 当前操作人数较多，请稍后重试")
    return False


def _parse_csv_items(raw: str) -> list[str]:
    return [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()]


def _parse_indices(raw: str) -> tuple[list[int], str | None]:
    try:
        indices = [int(item) for item in _parse_csv_items(raw)]
    except ValueError:
        return [], "编号必须是数字，多个编号用逗号分隔"
    if not indices:
        return [], "请输入编号"
    return sorted(set(indices)), None


def _render_keyword_stats() -> tuple[str, bool]:
    today = time.strftime("%Y-%m-%d")
    today_hits = [(k.split(":", 2)[-1], v) for k, v in _keyword_stats.items() if k.startswith(today)]
    if not today_hits:
        return "今日暂无关键词命中记录", False

    today_hits.sort(key=lambda x: -x[1])
    total_hits = sum(count for _, count in today_hits)
    lines = [
        f"今日统计 ({today})",
        f"总命中: {total_hits} 次 - 关键词: {len(today_hits)} 个",
        ""
    ]

    for i, (word, count) in enumerate(today_hits[:20], 1):
        if i <= 3:
            medal = ["1.", "2.", "3."][i - 1]
            lines.append(f"{medal} {word}   {count} 次")
        else:
            lines.append(f"  {i}. {word}   {count} 次")

    if len(today_hits) > 20:
        lines.append(f"\n... 还有 {len(today_hits) - 20} 个关键词")

    return "\n".join(lines), True


def _menu_options_text() -> str:
    return (
        "可选操作:\n"
        "1. 添加关键词\n"
        "2. 删除关键词\n"
        "3. 切换启用状态\n"
        "4. 查看今日统计\n"
        "5. 设置每日一言\n"
        "回复 cancel 取消"
    )


def _build_admin_help() -> str:
    return (
        "常用操作\n"
        "status                              查看规则列表和快捷菜单\n"
        "status <编号|群号>                  查看指定群规则\n"
        "quote [HH:MM]                       设置每日一言，默认 09:00\n"
        "stats                               查看今日命中统计\n"
        "cancel                              取消当前操作\n"
        "\n"
        "关键词管理\n"
        "add [编号|群号] <关键词>             添加关键词\n"
        "add [编号|群号] <词1,词2>            批量添加关键词\n"
        "remove [编号|群号] <关键词编号>      删除关键词\n"
        "remove [编号|群号] <1,2,3>           批量删除关键词\n"
        "disable [编号|群号] <关键词编号>     临时禁用关键词\n"
        "enable [编号|群号] <关键词编号>      恢复禁用关键词\n"
        "set [编号|群号] <词1,词2>            替换全部关键词，需要确认\n"
        "on [编号|群号]                       启用监听\n"
        "off [编号|群号]                      禁用监听\n"
        "\n"
        "定时提醒\n"
        "remind <HH:MM> <内容>                添加每日提醒\n"
        "remind once <日期> <时间> <内容>     添加单次提醒\n"
        "remind workday <HH:MM> <内容>        添加工作日提醒\n"
        "remind interval <分钟> <内容>        添加间隔提醒\n"
        "remind period <时间> <分钟> <内容>   添加周期催促\n"
        "remind quote <HH:MM>                 添加每日一言\n"
        "remind done <编号>                   标记周期催促完成\n"
        "remind remove <编号[,编号]>          删除提醒\n"
        "remind list                          查看提醒列表\n"
        "\n"
        "高级管理\n"
        "rule addgroup <群号>                 添加群规则\n"
        "rule delgroup <编号|群号>            删除群规则，需要确认\n"
        "rule addtarget <编号|群号> <QQ>      添加转发目标\n"
        "rule deltarget <编号|群号> <QQ>      删除转发目标\n"
        "\n"
        "说明\n"
        "单群模式下可以省略编号或群号。\n"
        "多个关键词或编号支持中英文逗号分隔。\n"
        "规则编号请先发送 status 查看。"
    )


def _render_rule(rule: dict, index: int | None = None) -> str:
    """Render rule with modern formatting."""
    status_badge = "成功: 已启用" if rule['enabled'] else "禁用 已禁用"
    group_name = str(rule.get("group_name") or "未知")

    # Keywords section
    if rule["keywords"]:
        enabled_kws = [kw for kw in rule["keywords"] if kw.get('enabled', True)]
        disabled_kws = [kw for kw in rule["keywords"] if not kw.get('enabled', True)]

        kw_lines = []
        for i, kw in enumerate(rule["keywords"], 1):
            suffix = " 停用" if not kw.get('enabled', True) else ""
            kw_lines.append(f"  {i}. {kw['word']}{suffix}")

        kw_summary = f"{len(enabled_kws)} 个启用"
        if disabled_kws:
            kw_summary += f" - {len(disabled_kws)} 个禁用"

        kw_section = "\n".join(kw_lines)
    else:
        kw_summary = "暂无关键词"
        kw_section = "  (空)"

    rule_no = f"规则编号: {index}\n" if index is not None else ""
    return (
        f"群组信息\n"
        f"{rule_no}"
        f"群名: {group_name}\n"
        f"群号: {rule['group_id']}\n"
        f"状态: {status_badge}\n"
        f"转发目标: {', '.join(map(str, rule['targets'])) or '未设置'}\n"
        f"正则匹配: {'开启' if rule['use_regex'] else '关闭'}\n"
        f"\n"
        f"关键词列表 ({kw_summary})\n"
        f"{kw_section}"
    )


def _render_rule_with_menu(rule: dict, index: int | None = None) -> str:
    """Render rule with interactive menu options."""
    base = _render_rule(rule, index=index)
    menu = (
        "\n\n"
        "快捷操作菜单\n"
        "1.  添加关键词\n"
        "2.  删除关键词\n"
        "3.  切换启用状态\n"
        "4.  查看今日统计\n"
        "5.  设置每日一言\n"
        "\n"
        "提示: 回复数字选择操作\n"
        "提示: 输入 cancel 取消操作"
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


def _rule_by_index(rules: list[dict], index: int) -> dict | None:
    if 1 <= index <= len(rules):
        return rules[index - 1]
    return None


def _resolve_rule_reference(rules: list[dict], value: int) -> dict | None:
    return find_rule(rules, value) or _rule_by_index(rules, value)


def _rule_index(rules: list[dict], group_id: int) -> int | None:
    return next((i for i, rule in enumerate(rules, 1) if rule["group_id"] == group_id), None)


def _clean_display_text(text: str) -> str:
    cleaned = []
    for char in text:
        code = ord(char)
        if (
            0x1F1E6 <= code <= 0x1F1FF  # regional indicator flags
            or 0x1F300 <= code <= 0x1FAFF
            or 0x2600 <= code <= 0x27BF
            or code in {0x200D, 0xFE0F, 0x20E3}
        ):
            continue
        cleaned.append(char)
    text = re.sub(r"\s+", " ", "".join(cleaned)).strip()
    return text or "未知"


async def _get_group_name(bot: Bot, group_id: int) -> str:
    try:
        info = await bot.call_api("get_group_info", group_id=group_id, no_cache=False)
        raw_name = info.get("group_name") or info.get("group_remark") or "未知"
        return _clean_display_text(str(raw_name))
    except Exception as exc:
        logger.warning("Failed to get group name for %s: %s", group_id, exc)
        return "未知"


async def _annotate_group_names(bot: Bot, rules: list[dict]) -> list[dict]:
    annotated = []
    for rule in rules:
        item = dict(rule)
        item["group_name"] = await _get_group_name(bot, rule["group_id"])
        annotated.append(item)
    return annotated


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

    # Check for images and @mentions
    extra_parts: list[str] = []
    image_count = sum(1 for seg in event.message if seg.type == "image")
    if image_count:
        extra_parts.append(f"图片×{image_count}")
    for seg in event.message:
        if seg.type == "at":
            qq = seg.data.get("qq", "")
            extra_parts.append(f"@{qq if qq != 'all' else '全体成员'}")
    extra = " ".join(extra_parts)

    forward_text = text
    if extra:
        forward_text += f"\n{extra}"

    # Merged forward content should stay the same as direct forwarding content.
    raw_text = forward_text

    # Buffer message for merging
    message_data = {
        "text": forward_text,
        "raw_text": raw_text,
        "content": event.message,
        "sender_name": sender_name,
        "sender_id": event.user_id,
        "targets": rule["targets"],
        "time": event.time
    }

    await _buffer_message(bot, event.group_id, message_data)
    logger.info("Buffered message %s from group %s", event.message_id, event.group_id)


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
            await _reply_private(bot, user_id, "成功: 已取消当前操作")
        else:
            await _reply_private(bot, user_id, "提示: 当前没有进行中的操作")
        return

    if command == "status":
        if not rules:
            await _reply_private(bot, user_id, "当前没有任何群规则")
            return
        display_rules = await _annotate_group_names(bot, rules)
        group_id_arg, _ = _resolve_group_id(text)
        if group_id_arg is not None:
            rule = _resolve_rule_reference(display_rules, group_id_arg)
            if not rule:
                await _reply_private(bot, user_id, f"群或规则编号 {group_id_arg} 不存在")
                return
            index = next((i for i, item in enumerate(display_rules, 1) if item["group_id"] == rule["group_id"]), None)
            if not await _set_session_or_reply(bot, user_id, "menu_status", group_id=rule["group_id"]):
                return
            await _reply_image(bot, user_id, _render_rule_with_menu(rule, index=index), title=f"群 {rule['group_id']}")
        else:
            if len(display_rules) == 1:
                rule = display_rules[0]
                if not await _set_session_or_reply(bot, user_id, "menu_status", group_id=rule["group_id"]):
                    return
                await _reply_image(bot, user_id, _render_rule_with_menu(rule, index=1), title=f"群 {rule['group_id']}")
            else:
                await _reply_image(
                    bot, user_id,
                    "\n\n".join(_render_rule(r, index=i) for i, r in enumerate(display_rules, 1)),
                    title="规则列表"
                )
        return

    if command in {"on", "off"}:
        group_id_arg, _ = _resolve_group_id(text)
        if group_id_arg is None and len(rules) != 1:
            await _reply_private(bot, user_id, "存在多个群规则，请指定群号或规则编号: on <群号|编号>")
            return
        group_id = group_id_arg if group_id_arg is not None else rules[0]["group_id"]
        rule = _resolve_rule_reference(rules, group_id)
        if not rule:
            await _reply_private(bot, user_id, f"群或规则编号 {group_id} 不存在")
            return
        rule["enabled"] = command == "on"
        if await _save_rules_or_reply(bot, user_id, upsert_rule(rules, rule)) is None:
            return
        await _reply_image(bot, user_id, _render_rule(rule, index=_rule_index(rules, rule["group_id"])), title=f"群 {rule['group_id']}")
        return

    if command == "remove":
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            await _reply_private(bot, user_id, f"用法: remove [群号] <编号>\n支持批量: remove 1,3,5\n使用 status 查看编号")
            return

        # Parse group_id and indices
        if len(parts) > 2 and parts[1].strip().isdigit():
            group_id = int(parts[1])
            indices_str = parts[2]
        else:
            if len(rules) != 1:
                await _reply_private(bot, user_id, "存在多个群规则，请指定群号或规则编号: remove <群号|编号> <关键词编号>")
                return
            group_id = rules[0]["group_id"]
            indices_str = parts[1]

        rule = _resolve_rule_reference(rules, group_id)
        if not rule:
            await _reply_private(bot, user_id, f"群或规则编号 {group_id} 不存在")
            return
        group_id = rule["group_id"]

        # Parse indices (support comma-separated)
        indices, error = _parse_indices(indices_str)
        if error:
            await _reply_private(bot, user_id, f"错误: {error}")
            return

        # Validate and remove (sort descending to avoid index shift)
        removed = []
        invalid = []

        for idx in sorted(indices, reverse=True):
            if 1 <= idx <= len(rule["keywords"]):
                removed.append(rule["keywords"].pop(idx - 1))
            else:
                invalid.append(idx)

        if removed:
            if await _save_rules_or_reply(bot, user_id, upsert_rule(rules, rule)) is None:
                return
            removed_names = ", ".join(f"{kw['word']}" for kw in removed)
            msg = f"成功: 已删除 {len(removed)} 个关键词: {removed_names}\n\n{_render_rule(rule, index=_rule_index(rules, rule['group_id']))}"
            if invalid:
                msg = f"注意: 编号 {', '.join(map(str, invalid))} 不存在\n\n" + msg
            await _reply_image(bot, user_id, msg, title=f"群 {group_id}")
        else:
            await _reply_private(bot, user_id, f"错误: 编号 {', '.join(map(str, invalid))} 不存在，当前共 {len(rule['keywords'])} 个关键词")
        return

    if command in {"add", "set"}:
        group_id_arg, keyword = _resolve_group_id(text)
        if not keyword:
            await _reply_private(bot, user_id, f"用法: {command} [群号] <关键词>\n支持批量: {command} 鞋子,裤子,衣服")
            return
        if group_id_arg is None and len(rules) != 1:
            await _reply_private(bot, user_id, f"存在多个群规则，请指定群号或规则编号: {command} <群号|编号> <关键词>")
            return
        group_id = group_id_arg if group_id_arg is not None else rules[0]["group_id"]
        rule = _resolve_rule_reference(rules, group_id)
        if not rule:
            await _reply_private(bot, user_id, f"群或规则编号 {group_id} 不存在")
            return

        # Parse keywords (support comma-separated)
        keywords = _parse_csv_items(keyword)
        if not keywords:
            await _reply_private(bot, user_id, f"用法: {command} [群号] <关键词>\n支持批量: {command} 鞋子,裤子,衣服")
            return

        if command == "add":
            added = []
            skipped = []
            for kw in keywords:
                if not any(k["word"] == kw for k in rule["keywords"]):
                    rule["keywords"].append({"word": kw, "enabled": True})
                    added.append(kw)
                else:
                    skipped.append(kw)

            if added:
                if await _save_rules_or_reply(bot, user_id, upsert_rule(rules, rule)) is None:
                    return
                msg = f"成功: 已添加 {len(added)} 个关键词: {', '.join(added)}\n\n{_render_rule(rule, index=_rule_index(rules, rule['group_id']))}"
                if skipped:
                    msg = f"注意: 已存在: {', '.join(skipped)}\n\n" + msg
                await _reply_image(bot, user_id, msg, title=f"群 {group_id}")
            else:
                await _reply_private(bot, user_id, f"错误: 关键词已存在: {', '.join(skipped)}")
        else:
            current_count = len(rule["keywords"])
            if not await _set_session_or_reply(
                bot, user_id, "confirm_keyword_set", group_id=group_id, keywords=keywords
            ):
                return
            await _reply_private(
                bot, user_id,
                f"注意: 将把群 {group_id} 的 {current_count} 个关键词替换为 {len(keywords)} 个关键词。\n"
                f"新关键词: {', '.join(keywords)}\n\n"
                "回复 yes 确认，回复 cancel 取消。"
            )
        return

    if command == "stats":
        stats_text, has_data = _render_keyword_stats()
        if has_data:
            await _reply_image(bot, user_id, stats_text, title="今日统计")
        else:
            await _reply_private(bot, user_id, stats_text)
        return

    if command == "quote":
        parts = text.split(maxsplit=1)
        quote_time = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "09:00"
        await _handle_remind_command(bot, user_id, f"remind quote {quote_time}")
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
                await _reply_private(bot, user_id, f"存在多个群规则，请指定群号或规则编号: {command} <群号|编号> <关键词编号>")
                return
            group_id = rules[0]["group_id"]

        rule = _resolve_rule_reference(rules, group_id)
        if not rule:
            await _reply_private(bot, user_id, f"群或规则编号 {group_id} 不存在")
            return
        group_id = rule["group_id"]
        if 1 <= idx <= len(rule["keywords"]):
            rule["keywords"][idx - 1]["enabled"] = command == "enable"
            if await _save_rules_or_reply(bot, user_id, upsert_rule(rules, rule)) is None:
                return
            status = "已启用" if command == "enable" else "已禁用"
            kw_name = rule['keywords'][idx - 1]['word']
            await _reply_image(bot, user_id, f"成功: {status}关键词: {kw_name}\n\n{_render_rule(rule, index=_rule_index(rules, rule['group_id']))}", title=f"群 {group_id}")
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
        updated_rules = upsert_rule(rules, new_rule)
        if await _save_rules_or_reply(bot, user_id, updated_rules) is None:
            return
        new_rule["group_name"] = await _get_group_name(bot, group_id)
        await _reply_image(bot, user_id, f"已添加群规则\n{_render_rule(new_rule, index=_rule_index(updated_rules, group_id))}", title=f"群 {group_id}")

    elif sub == "delgroup":
        if len(parts) < 3:
            await _reply_private(bot, user_id, "用法: rule delgroup <群号>")
            return
        try:
            group_id = int(parts[2].strip())
        except ValueError:
            await _reply_private(bot, user_id, f"无效的群号: {parts[2]}")
            return
        rule = _resolve_rule_reference(rules, group_id)
        if not rule:
            await _reply_private(bot, user_id, f"群或规则编号 {group_id} 不存在")
            return
        group_id = rule["group_id"]
        if not await _set_session_or_reply(bot, user_id, "confirm_rule_delgroup", group_id=group_id):
            return
        await _reply_private(
            bot, user_id,
            f"注意: 将删除群 {group_id} 的规则。\n"
            f"关键词: {len(rule['keywords'])} 个\n"
            f"转发目标: {', '.join(map(str, rule['targets'])) or '未设置'}\n\n"
            "回复 yes 确认删除，回复 cancel 取消。"
        )

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
        rule = _resolve_rule_reference(rules, group_id)
        if not rule:
            await _reply_private(bot, user_id, f"群或规则编号 {group_id} 不存在")
            return
        group_id = rule["group_id"]
        targets = set(rule["targets"])
        targets.add(target)
        rule["targets"] = sorted(targets)
        if await _save_rules_or_reply(bot, user_id, upsert_rule(rules, rule)) is None:
            return
        await _reply_image(bot, user_id, _render_rule(rule, index=_rule_index(rules, rule["group_id"])), title=f"群 {group_id}")

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
        rule = _resolve_rule_reference(rules, group_id)
        if not rule:
            await _reply_private(bot, user_id, f"群或规则编号 {group_id} 不存在")
            return
        group_id = rule["group_id"]
        targets = set(rule["targets"])
        if target not in targets:
            await _reply_private(bot, user_id, f"QQ {target} 不在群 {group_id} 的转发目标中")
            return
        targets.remove(target)
        rule["targets"] = sorted(targets)
        if await _save_rules_or_reply(bot, user_id, upsert_rule(rules, rule)) is None:
            return
        await _reply_image(bot, user_id, _render_rule(rule, index=_rule_index(rules, rule["group_id"])), title=f"群 {group_id}")

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
        if text.strip().lower() in {"cancel", "取消"}:
            await _session_manager.clear_state(user_id)
            await _reply_private(bot, user_id, "成功: 已取消当前操作")
            return True

        rules = _load_rules_from_file()

        # Handle menu selection
        if state == "menu_status":
            group_id = session["group_id"]
            rule = await _get_rule_or_fail(group_id)
            if not rule:
                return True

            if text == "1":
                # Add keyword
                if not await _set_session_or_reply(bot, user_id, "awaiting_keyword_add", group_id=group_id):
                    return True
                await _reply_private(bot, user_id, "请输入要添加的关键词，多个用逗号分隔：")
                return True
            elif text == "2":
                # Delete keyword
                if not rule["keywords"]:
                    await _reply_private(bot, user_id, "当前没有关键词")
                    await _session_manager.clear_state(user_id)
                    return True
                if not await _set_session_or_reply(bot, user_id, "awaiting_keyword_remove", group_id=group_id):
                    return True
                kw_list = "\n".join(f"{i}. {kw['word']}" for i, kw in enumerate(rule["keywords"], 1))
                await _reply_private(bot, user_id, f"请输入要删除的关键词编号，多个用逗号分隔：\n{kw_list}")
                return True
            elif text == "3":
                # Toggle keyword enable/disable
                if not rule["keywords"]:
                    await _reply_private(bot, user_id, "当前没有关键词")
                    await _session_manager.clear_state(user_id)
                    return True
                if not await _set_session_or_reply(bot, user_id, "awaiting_keyword_toggle", group_id=group_id):
                    return True
                kw_list = "\n".join(
                    f"{i}. {kw['word']} {'启用' if kw.get('enabled', True) else '禁用'}"
                    for i, kw in enumerate(rule["keywords"], 1)
                )
                await _reply_private(bot, user_id, f"请输入要切换状态的关键词编号，多个用逗号分隔：\n{kw_list}")
                return True
            elif text == "4":
                # Show stats
                await _session_manager.clear_state(user_id)
                stats_text, has_data = _render_keyword_stats()
                if has_data:
                    await _reply_image(bot, user_id, stats_text, title="今日统计")
                else:
                    await _reply_private(bot, user_id, stats_text)
                return True
            elif text == "5":
                # Schedule daily quote
                await _session_manager.clear_state(user_id)
                await _handle_remind_command(bot, user_id, "remind quote 09:00")
                return True
            else:
                # Invalid option
                await _reply_private(bot, user_id, f"错误: 无效选项，请输入 1-5\n\n{_menu_options_text()}")
                return True

        # Handle keyword add
        elif state == "awaiting_keyword_add":
            group_id = session["group_id"]
            rule = await _get_rule_or_fail(group_id)
            if not rule:
                return True

            keywords = _parse_csv_items(text)
            if not keywords:
                await _reply_private(bot, user_id, "错误: 关键词不能为空")
                return True

            added = []
            skipped = []
            for keyword in keywords:
                if any(kw["word"] == keyword for kw in rule["keywords"]):
                    skipped.append(keyword)
                else:
                    rule["keywords"].append({"word": keyword, "enabled": True})
                    added.append(keyword)

            if not added:
                await _reply_private(bot, user_id, f"错误: 关键词已存在: {', '.join(skipped)}")
                await _session_manager.clear_state(user_id)
                return True

            if await _save_rules_or_reply(bot, user_id, upsert_rule(rules, rule)) is None:
                return True

            msg = f"成功: 已添加 {len(added)} 个关键词: {', '.join(added)}\n\n{_render_rule(rule)}"
            if skipped:
                msg = f"注意: 已存在: {', '.join(skipped)}\n\n" + msg
            await _session_manager.clear_state(user_id)
            await _reply_image(bot, user_id, msg, title=f"群 {group_id}")
            return True

        # Handle keyword remove
        elif state == "awaiting_keyword_remove":
            group_id = session["group_id"]
            rule = await _get_rule_or_fail(group_id)
            if not rule:
                return True

            indices, error = _parse_indices(text)
            if error:
                await _reply_private(bot, user_id, f"错误: {error}")
                return True

            removed = []
            invalid = []
            for idx in sorted(indices, reverse=True):
                if 1 <= idx <= len(rule["keywords"]):
                    removed.append(rule["keywords"].pop(idx - 1))
                else:
                    invalid.append(idx)

            if not removed:
                await _reply_private(bot, user_id, f"错误: 编号 {', '.join(map(str, invalid))} 不存在，当前共 {len(rule['keywords'])} 个关键词")
                return True

            if await _save_rules_or_reply(bot, user_id, upsert_rule(rules, rule)) is None:
                return True

            removed_names = ", ".join(kw["word"] for kw in removed)
            msg = f"成功: 已删除 {len(removed)} 个关键词: {removed_names}\n\n{_render_rule(rule, index=_rule_index(rules, rule['group_id']))}"
            if invalid:
                msg = f"注意: 编号 {', '.join(map(str, invalid))} 不存在\n\n" + msg
            await _session_manager.clear_state(user_id)
            await _reply_image(bot, user_id, msg, title=f"群 {group_id}")
            return True

        # Handle keyword toggle
        elif state == "awaiting_keyword_toggle":
            group_id = session["group_id"]
            rule = await _get_rule_or_fail(group_id)
            if not rule:
                return True

            indices, error = _parse_indices(text)
            if error:
                await _reply_private(bot, user_id, f"错误: {error}")
                return True

            changed = []
            invalid = []
            for idx in indices:
                if 1 <= idx <= len(rule["keywords"]):
                    kw = rule["keywords"][idx - 1]
                    kw["enabled"] = not kw.get("enabled", True)
                    status = "已启用" if kw["enabled"] else "已禁用"
                    changed.append(f"{idx}. {kw['word']} - {status}")
                else:
                    invalid.append(idx)

            if not changed:
                await _reply_private(bot, user_id, f"错误: 编号 {', '.join(map(str, invalid))} 不存在，当前共 {len(rule['keywords'])} 个关键词")
                return True

            if await _save_rules_or_reply(bot, user_id, upsert_rule(rules, rule)) is None:
                return True

            msg = f"成功: 已切换 {len(changed)} 个关键词\n" + "\n".join(changed) + f"\n\n{_render_rule(rule, index=_rule_index(rules, rule['group_id']))}"
            if invalid:
                msg = f"注意: 编号 {', '.join(map(str, invalid))} 不存在\n\n" + msg
            await _session_manager.clear_state(user_id)
            await _reply_image(bot, user_id, msg, title=f"群 {group_id}")
            return True

        elif state == "confirm_keyword_set":
            choice = text.strip().lower()
            if choice in {"no", "n", "取消", "cancel"}:
                await _session_manager.clear_state(user_id)
                await _reply_private(bot, user_id, "成功: 已取消替换关键词")
                return True
            if choice not in {"yes", "y", "确认"}:
                await _reply_private(bot, user_id, "请回复 yes 确认替换，或回复 cancel 取消。")
                return True

            group_id = session["group_id"]
            keywords = session["keywords"]
            rule = await _get_rule_or_fail(group_id)
            if not rule:
                return True
            rule["keywords"] = [{"word": kw, "enabled": True} for kw in keywords]
            if await _save_rules_or_reply(bot, user_id, upsert_rule(rules, rule)) is None:
                return True
            await _session_manager.clear_state(user_id)
            await _reply_image(bot, user_id, f"成功: 已替换为 {len(keywords)} 个关键词\n\n{_render_rule(rule, index=_rule_index(rules, rule['group_id']))}", title=f"群 {group_id}")
            return True

        elif state == "confirm_rule_delgroup":
            choice = text.strip().lower()
            if choice in {"no", "n", "取消", "cancel"}:
                await _session_manager.clear_state(user_id)
                await _reply_private(bot, user_id, "成功: 已取消删除群规则")
                return True
            if choice not in {"yes", "y", "确认"}:
                await _reply_private(bot, user_id, "请回复 yes 确认删除，或回复 cancel 取消。")
                return True

            group_id = session["group_id"]
            if not find_rule(rules, group_id):
                await _session_manager.clear_state(user_id)
                await _reply_private(bot, user_id, f"群 {group_id} 不存在")
                return True
            updated = [item for item in rules if item["group_id"] != group_id]
            if await _save_rules_or_reply(bot, user_id, updated) is None:
                return True
            await _session_manager.clear_state(user_id)
            await _reply_private(bot, user_id, f"成功: 已删除群规则 {group_id}")
            return True

        return False

    except Exception as e:
        logger.error("Session input handling failed for user %s: %s", user_id, e, exc_info=True)
        await _session_manager.clear_state(user_id)
        await _reply_private(bot, user_id, "错误: 操作失败，会话已重置。请重新开始。")
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

    if first_word == "cancel":
        pass
    elif first_word in COMMANDS:
        await _session_manager.clear_state(event.user_id)
    else:
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
