"""核心插件：群消息监听、关键词匹配、转发、管理命令。"""

import asyncio
import json
import os
import time
from collections import deque
from pathlib import Path

from nonebot import logger, on_message, get_driver
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment, PrivateMessageEvent
from nonebot.exception import FinishedException
from nonebot.rule import is_type

from rules import find_rule, load_rules, normalize_rule, save_rules, upsert_rule
from .remind import handle_command as _handle_remind_command
from .common import reply_image as _reply_image, reply_private as _reply_private
from session_manager import SessionManager
from stats import load_stats, save_stats
from forward_utils import (
    parse_int_set, parse_str_list, normalize_text, match_keywords,
    is_duplicate, check_keyword_cooldown, track_keyword_hit,
    parse_csv_items, parse_indices, resolve_group_id,
    rule_by_index, resolve_rule_reference, rule_index, clean_display_text,
    render_rule, render_rule_with_menu, render_keyword_stats,
    menu_options_text, build_admin_help,
)


TARGET_QQS = parse_int_set(os.getenv("TARGET_QQS", ""))
ADMIN_QQS = parse_int_set(os.getenv("ADMIN_QQS", "")) or TARGET_QQS
KEYWORDS = parse_str_list(os.getenv("KEYWORDS", ""))
CASE_SENSITIVE = os.getenv("CASE_SENSITIVE", "false").lower() == "true"
USE_REGEX = os.getenv("USE_REGEX", "false").lower() == "true"
RULES_FILE = Path(os.getenv("RULES_FILE", "rules.json"))
LEGACY_KEYWORDS_FILE = Path(os.getenv("KEYWORDS_FILE", "keywords.json"))
DEFAULT_ALLOWED_GROUPS = sorted(parse_int_set(os.getenv("ALLOWED_GROUPS", "")))

_recent_keys: deque[tuple[float, str]] = deque(maxlen=1000)
_recent_seen: set[str] = set()
_dedupe_seconds = 30

# Per-keyword cooldown (prevent same keyword from triggering repeatedly)
_keyword_cooldown: dict[str, float] = {}
_keyword_cooldown_seconds = 15

# Keyword hit stats (persisted to stats.json)
_keyword_stats: dict[str, int] = load_stats()
_stats_dirty: bool = False

# Session manager for interactive menus (max 1000 concurrent sessions)
_session_manager = SessionManager(timeout_seconds=300, max_sessions=1000)

# Message buffer for merging forwards (group_id -> list of pending messages)
_message_buffer: dict[int, list[dict]] = {}
_message_buffer_tasks: set[int] = set()
_buffer_timeout_seconds = 8  # Merge messages within 8 seconds



async def _flush_message_buffer(bot: Bot, group_id: int) -> None:
    """将缓冲消息作为合并转发发送给目标。"""
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

        failed_targets = []
        for target_qq in targets:
            try:
                await bot.call_api(
                    "send_private_forward_msg",
                    user_id=target_qq,
                    messages=nodes
                )
            except Exception as e:
                logger.error("Failed to send merged forward to %s: %s", target_qq, e)
                failed_targets.append(target_qq)

        if failed_targets:
            logger.warning("Retrying %d failed targets with individual messages", len(failed_targets))
            for msg in messages:
                for target_qq in failed_targets:
                    try:
                        await bot.call_api("send_msg", message_type="private", user_id=target_qq, message=msg["text"])
                    except Exception as e:
                        logger.error("Failed to send individual message to %s: %s", target_qq, e)

        logger.info("Forwarded %d messages from group %s to %s", len(messages), group_id, targets)
    except Exception as e:
        logger.error("Failed to send merged forward: %s, falling back to individual messages", e)
        for msg in messages:
            for target_qq in targets:
                try:
                    await bot.call_api("send_msg", message_type="private", user_id=target_qq, message=msg["text"])
                except Exception as send_err:
                    logger.error("Failed to send fallback message to %s: %s", target_qq, send_err)


async def _buffer_message(bot: Bot, group_id: int, message_data: dict) -> None:
    """缓冲消息并在超时后触发合并转发。"""
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
            # Re-schedule if new messages arrived during the flush
            if group_id in _message_buffer and _message_buffer[group_id]:
                _message_buffer_tasks.add(group_id)
                asyncio.create_task(_delayed_flush())

    asyncio.create_task(_delayed_flush())

_rules_mtime: float | None = None
_rules_cache: list[dict] = []

matcher = on_message(rule=is_type(GroupMessageEvent), priority=10, block=False)
admin_matcher = on_message(rule=is_type(PrivateMessageEvent), priority=5, block=True)


# --- keyword matching ---

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


# --- helpers ---


async def _save_rules_or_reply(bot: Bot, user_id: int, rules: list[dict]) -> list[dict] | None:
    try:
        return _save_rules_checked(rules)
    except OSError as exc:
        logger.error("Failed to save rules: %s", exc)
        await _reply_private(bot, user_id, "保存失败，请稍后重试")
        return None


async def _set_session_or_reply(bot: Bot, user_id: int, state: str, **kwargs) -> bool:
    if await _session_manager.set_state(user_id, state, **kwargs):
        return True
    await _reply_private(bot, user_id, "系统繁忙，请稍后重试")
    return False


async def _render_rule_annotated(bot: Bot, rule: dict, rules: list[dict]) -> str:
    """标注群名后渲染规则。"""
    rule_copy = dict(rule)
    rule_copy["group_name"] = await _get_group_name(bot, rule["group_id"])
    return render_rule(rule_copy, index=rule_index(rules, rule["group_id"]))


async def _render_rule_annotated_with_menu(bot: Bot, rule: dict, rules: list[dict]) -> str:
    """标注群名后渲染规则（带菜单）。"""
    rule_copy = dict(rule)
    rule_copy["group_name"] = await _get_group_name(bot, rule["group_id"])
    return render_rule_with_menu(rule_copy, index=rule_index(rules, rule["group_id"]))


async def _get_group_name(bot: Bot, group_id: int) -> str:
    try:
        info = await bot.call_api("get_group_info", group_id=group_id, no_cache=False)
        raw_name = info.get("group_name") or info.get("group_remark") or "未知"
        return clean_display_text(str(raw_name))
    except Exception as exc:
        logger.warning("Failed to get group name for %s: %s", group_id, exc)
        return "未知"


async def _annotate_group_names(bot: Bot, rules: list[dict]) -> list[dict]:
    names = await asyncio.gather(*[_get_group_name(bot, r["group_id"]) for r in rules])
    annotated = []
    for rule, name in zip(rules, names):
        item = dict(rule)
        item["group_name"] = name
        annotated.append(item)
    return annotated


# --- group message handler ---

@matcher.handle()
async def handle_group_message(bot: Bot, event: GroupMessageEvent) -> None:
    """匹配群消息关键词并缓冲转发。"""
    rules = _load_rules_from_file()
    rule = find_rule(rules, event.group_id)
    if not rule or not rule["enabled"]:
        raise FinishedException

    text = event.get_plaintext().strip()
    if not text:
        raise FinishedException

    matched = match_keywords(text, rule["keywords"], rule["use_regex"], CASE_SENSITIVE)
    if not matched:
        raise FinishedException

    # Filter keywords on cooldown
    matched = [kw for kw in matched if not check_keyword_cooldown(event.group_id, kw, _keyword_cooldown, _keyword_cooldown_seconds)]
    if not matched:
        raise FinishedException

    # Track stats
    for kw in matched:
        track_keyword_hit(event.group_id, kw, _keyword_stats)
    _stats_dirty = True

    message_key = f"{event.group_id}:{event.message_id}"
    if is_duplicate(message_key, _recent_keys, _recent_seen, _dedupe_seconds):
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

    # Build keyword match annotation
    display_matched = matched[:3]
    suffix = f" (+{len(matched) - 3})" if len(matched) > 3 else ""
    match_label = "命中: " + ", ".join(display_matched) + suffix

    forward_text = text
    if extra:
        forward_text += f"\n{extra}"
    forward_text += f"\n{match_label}"

    # Merged forward content should stay the same as direct forwarding content.
    raw_text = forward_text

    # Annotate content for merged forwards
    annotated_content = event.message + Message(MessageSegment.text(f"\n{match_label}"))

    # Compute effective targets: per-keyword overrides + rule-level fallback
    kw_targets_map = {kw["word"]: kw.get("targets") for kw in rule["keywords"] if "targets" in kw}
    effective_targets: set[int] = set()
    for kw in matched:
        kt = kw_targets_map.get(kw)
        if kt:
            effective_targets.update(kt)
        else:
            effective_targets.update(rule["targets"])
    effective_targets = sorted(effective_targets) or rule["targets"]

    # Buffer message for merging
    message_data = {
        "text": forward_text,
        "raw_text": raw_text,
        "content": annotated_content,
        "sender_name": sender_name,
        "sender_id": event.user_id,
        "targets": effective_targets,
        "time": event.time
    }

    await _buffer_message(bot, event.group_id, message_data)
    logger.info("Buffered message %s from group %s", event.message_id, event.group_id)


# --- unified keyword/rule commands ---

async def _cmd_status(bot: Bot, user_id: int, text: str, rules: list[dict]) -> None:
    if not rules:
        await _reply_private(bot, user_id, "暂无群规则，发送 help 查看帮助")
        return
    display_rules = await _annotate_group_names(bot, rules)
    group_id_arg, _ = resolve_group_id(text)
    if group_id_arg is not None:
        rule = resolve_rule_reference(display_rules, group_id_arg)
        if not rule:
            await _reply_private(bot, user_id, f"编号 {group_id_arg} 不存在，发送 status 查看列表")
            return
        index = next((i for i, item in enumerate(display_rules, 1) if item["group_id"] == rule["group_id"]), None)
        if not await _set_session_or_reply(bot, user_id, "menu_status", group_id=rule["group_id"]):
            return
        await _reply_image(bot, user_id, render_rule_with_menu(rule, index=index), title=f"群 {rule['group_id']}")
    else:
        if len(display_rules) == 1:
            rule = display_rules[0]
            if not await _set_session_or_reply(bot, user_id, "menu_status", group_id=rule["group_id"]):
                return
            await _reply_image(bot, user_id, render_rule_with_menu(rule, index=1), title=f"群 {rule['group_id']}")
        else:
            await _reply_image(
                bot, user_id,
                "\n\n".join(render_rule(r, index=i) for i, r in enumerate(display_rules, 1)),
                title="规则列表"
            )


async def _cmd_on_off(bot: Bot, user_id: int, command: str, text: str, rules: list[dict]) -> None:
    group_id_arg, _ = resolve_group_id(text)
    if group_id_arg is None and len(rules) != 1:
        await _reply_private(bot, user_id, "存在多个群规则，请指定: on <群号|编号>")
        return
    group_id = group_id_arg if group_id_arg is not None else rules[0]["group_id"]
    rule = resolve_rule_reference(rules, group_id)
    if not rule:
        await _reply_private(bot, user_id, f"编号 {group_id} 不存在，发送 status 查看列表")
        return
    rule["enabled"] = command == "on"
    if await _save_rules_or_reply(bot, user_id, upsert_rule(rules, rule)) is None:
        return
    await _reply_image(bot, user_id, await _render_rule_annotated(bot, rule, rules), title=f"群 {rule['group_id']}")


async def _cmd_remove(bot: Bot, user_id: int, text: str, rules: list[dict]) -> None:
    parts = text.split(maxsplit=2)
    if len(parts) < 2:
        await _reply_private(bot, user_id, "用法: remove [群号] <编号>\n支持批量: remove 1,3,5\n发送 status 查看编号")
        return

    if len(parts) > 2 and parts[1].strip().isdigit():
        group_id = int(parts[1])
        indices_str = parts[2]
    else:
        if len(rules) != 1:
            await _reply_private(bot, user_id, "存在多个群规则，请指定: remove <群号|编号> <关键词编号>")
            return
        group_id = rules[0]["group_id"]
        indices_str = parts[1]

    rule = resolve_rule_reference(rules, group_id)
    if not rule:
        await _reply_private(bot, user_id, f"编号 {group_id} 不存在，发送 status 查看列表")
        return
    group_id = rule["group_id"]

    indices, error = parse_indices(indices_str)
    if error:
        await _reply_private(bot, user_id, f"错误: {error}")
        return

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
        rendered = await _render_rule_annotated(bot, rule, rules)
        msg = f"已删除 {len(removed)} 个关键词: {removed_names}\n\n{rendered}"
        if invalid:
            msg = f"编号 {', '.join(map(str, invalid))} 不存在\n\n" + msg
        await _reply_image(bot, user_id, msg, title=f"群 {group_id}")
    else:
        await _reply_private(bot, user_id, f"编号 {', '.join(map(str, invalid))} 不存在，当前共 {len(rule['keywords'])} 个关键词")


async def _cmd_add_set(bot: Bot, user_id: int, command: str, text: str, rules: list[dict]) -> None:
    group_id_arg, keyword = resolve_group_id(text)
    if not keyword:
        await _reply_private(bot, user_id, f"用法: {command} [群号] <关键词>\n支持批量: {command} 鞋子,裤子,衣服")
        return
    if group_id_arg is None and len(rules) != 1:
        await _reply_private(bot, user_id, f"存在多个群规则，请指定: {command} <群号|编号> <关键词>")
        return
    group_id = group_id_arg if group_id_arg is not None else rules[0]["group_id"]
    rule = resolve_rule_reference(rules, group_id)
    if not rule:
        await _reply_private(bot, user_id, f"编号 {group_id} 不存在，发送 status 查看列表")
        return

    keywords = parse_csv_items(keyword)
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
            rendered = await _render_rule_annotated(bot, rule, rules)
            msg = f"已添加 {len(added)} 个关键词: {', '.join(added)}\n\n{rendered}"
            if skipped:
                msg = f"已存在: {', '.join(skipped)}\n\n" + msg
            await _reply_image(bot, user_id, msg, title=f"群 {group_id}")
        else:
            await _reply_private(bot, user_id, f"关键词已存在: {', '.join(skipped)}")
    else:
        seen: set[str] = set()
        unique_keywords: list[str] = []
        for kw in keywords:
            if kw not in seen:
                seen.add(kw)
                unique_keywords.append(kw)
        current_count = len(rule["keywords"])
        if not await _set_session_or_reply(
            bot, user_id, "confirm_keyword_set", group_id=group_id, keywords=unique_keywords
        ):
            return
        await _reply_private(
            bot, user_id,
            f"将替换群 {group_id} 的 {current_count} 个关键词为 {len(unique_keywords)} 个:\n"
            f"{', '.join(unique_keywords)}\n\n"
            "回复 yes 确认，cancel 取消"
        )


async def _cmd_stats(bot: Bot, user_id: int) -> None:
    stats_text, has_data = render_keyword_stats(_keyword_stats)
    if has_data:
        await _reply_image(bot, user_id, stats_text, title="今日统计")
    else:
        await _reply_private(bot, user_id, stats_text)


async def _cmd_disable_enable(bot: Bot, user_id: int, command: str, text: str, rules: list[dict]) -> None:
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
            await _reply_private(bot, user_id, f"存在多个群规则，请指定: {command} <群号|编号> <关键词编号>")
            return
        group_id = rules[0]["group_id"]

    rule = resolve_rule_reference(rules, group_id)
    if not rule:
        await _reply_private(bot, user_id, f"编号 {group_id} 不存在，发送 status 查看列表")
        return
    group_id = rule["group_id"]
    if 1 <= idx <= len(rule["keywords"]):
        rule["keywords"][idx - 1]["enabled"] = command == "enable"
        if await _save_rules_or_reply(bot, user_id, upsert_rule(rules, rule)) is None:
            return
        status = "启用" if command == "enable" else "禁用"
        kw_name = rule['keywords'][idx - 1]['word']
        rendered = await _render_rule_annotated(bot, rule, rules)
        await _reply_image(bot, user_id, f"已{status}关键词: {kw_name}\n\n{rendered}", title=f"群 {group_id}")
    else:
        await _reply_private(bot, user_id, f"编号 {idx} 不存在，当前共 {len(rule['keywords'])} 个关键词")


async def _handle_command(bot: Bot, user_id: int, command: str, text: str) -> None:
    rules = _load_rules_from_file()

    if command in {"help", "h"}:
        await _reply_image(bot, user_id, build_admin_help(), title="帮助")
    elif command == "cancel":
        session = await _session_manager.get_state(user_id)
        if session:
            await _session_manager.clear_state(user_id)
            await _reply_private(bot, user_id, "已退出")
        else:
            await _reply_private(bot, user_id, "当前没有进行中的操作")
    elif command == "status":
        await _cmd_status(bot, user_id, text, rules)
    elif command in {"on", "off"}:
        await _cmd_on_off(bot, user_id, command, text, rules)
    elif command == "remove":
        await _cmd_remove(bot, user_id, text, rules)
    elif command in {"add", "set"}:
        await _cmd_add_set(bot, user_id, command, text, rules)
    elif command == "stats":
        await _cmd_stats(bot, user_id)
    elif command == "quote":
        parts = text.split(maxsplit=1)
        quote_time = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "09:00"
        await _handle_remind_command(bot, user_id, f"remind quote {quote_time}")
    elif command in {"disable", "enable"}:
        await _cmd_disable_enable(bot, user_id, command, text, rules)
    else:
        await _reply_image(bot, user_id, build_admin_help(), title="帮助")


async def _cmd_rule_addgroup(bot: Bot, user_id: int, parts: list[str], rules: list[dict]) -> None:
    if len(parts) < 3:
        await _reply_private(bot, user_id, "用法: rule addgroup <群号>")
        return
    try:
        group_id = int(parts[2].strip())
    except ValueError:
        await _reply_private(bot, user_id, f"无效的群号: {parts[2]}")
        return
    if find_rule(rules, group_id):
        await _reply_private(bot, user_id, f"编号 {group_id} 已存在")
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
    await _reply_image(bot, user_id, f"已添加群规则\n{render_rule(new_rule, index=rule_index(updated_rules, group_id))}", title=f"群 {group_id}")


async def _cmd_rule_delgroup(bot: Bot, user_id: int, parts: list[str], rules: list[dict]) -> None:
    if len(parts) < 3:
        await _reply_private(bot, user_id, "用法: rule delgroup <群号>")
        return
    try:
        group_id = int(parts[2].strip())
    except ValueError:
        await _reply_private(bot, user_id, f"无效的群号: {parts[2]}")
        return
    rule = resolve_rule_reference(rules, group_id)
    if not rule:
        await _reply_private(bot, user_id, f"编号 {group_id} 不存在，发送 status 查看列表")
        return
    group_id = rule["group_id"]
    if not await _set_session_or_reply(bot, user_id, "confirm_rule_delgroup", group_id=group_id):
        return
    await _reply_private(
        bot, user_id,
        f"将删除群 {group_id} 的规则\n"
        f"关键词: {len(rule['keywords'])} 个\n"
        f"转发目标: {', '.join(map(str, rule['targets'])) or '未设置'}\n\n"
        "回复 yes 确认删除，cancel 取消"
    )


async def _cmd_rule_addtarget(bot: Bot, user_id: int, parts: list[str], rules: list[dict]) -> None:
    if len(parts) < 4:
        await _reply_private(bot, user_id, "用法: rule addtarget <群号> <QQ号>")
        return
    try:
        group_id = int(parts[2].strip())
        target = int(parts[3].strip())
    except ValueError:
        await _reply_private(bot, user_id, "无效的群号或QQ号")
        return
    rule = resolve_rule_reference(rules, group_id)
    if not rule:
        await _reply_private(bot, user_id, f"编号 {group_id} 不存在，发送 status 查看列表")
        return
    group_id = rule["group_id"]
    targets = set(rule["targets"])
    targets.add(target)
    rule["targets"] = sorted(targets)
    if await _save_rules_or_reply(bot, user_id, upsert_rule(rules, rule)) is None:
        return
    await _reply_image(bot, user_id, await _render_rule_annotated(bot, rule, rules), title=f"群 {group_id}")


async def _cmd_rule_deltarget(bot: Bot, user_id: int, parts: list[str], rules: list[dict]) -> None:
    if len(parts) < 4:
        await _reply_private(bot, user_id, "用法: rule deltarget <群号> <QQ号>")
        return
    try:
        group_id = int(parts[2].strip())
        target = int(parts[3].strip())
    except ValueError:
        await _reply_private(bot, user_id, "无效的群号或QQ号")
        return
    rule = resolve_rule_reference(rules, group_id)
    if not rule:
        await _reply_private(bot, user_id, f"编号 {group_id} 不存在，发送 status 查看列表")
        return
    group_id = rule["group_id"]
    targets = set(rule["targets"])
    if target not in targets:
        await _reply_private(bot, user_id, f"QQ {target} 不在转发目标中")
        return
    targets.remove(target)
    rule["targets"] = sorted(targets)
    if await _save_rules_or_reply(bot, user_id, upsert_rule(rules, rule)) is None:
        return
    await _reply_image(bot, user_id, await _render_rule_annotated(bot, rule, rules), title=f"群 {group_id}")


_RULE_HANDLERS = {
    "addgroup": _cmd_rule_addgroup,
    "delgroup": _cmd_rule_delgroup,
    "addtarget": _cmd_rule_addtarget,
    "deltarget": _cmd_rule_deltarget,
}


async def _handle_rule_advanced(bot: Bot, user_id: int, parts: list[str]) -> None:
    if len(parts) < 2:
        await _reply_image(bot, user_id, build_admin_help(), title="帮助")
        return

    sub = parts[1].lower()
    handler = _RULE_HANDLERS.get(sub)
    if handler:
        rules = _load_rules_from_file()
        await handler(bot, user_id, parts, rules)
    else:
        await _reply_image(bot, user_id, build_admin_help(), title="帮助")


async def _handle_kwtarget(bot: Bot, user_id: int, parts: list[str]) -> None:
    if len(parts) < 2:
        await _reply_image(bot, user_id, build_admin_help(), title="帮助")
        return

    sub = parts[1].lower()
    if sub not in {"add", "del"}:
        await _reply_private(bot, user_id, "用法: kwtarget add|del <群号> <关键词编号> <QQ>")
        return

    if len(parts) < 5:
        await _reply_private(bot, user_id, f"用法: kwtarget {sub} <群号> <关键词编号> <QQ>")
        return

    try:
        group_id = int(parts[2].strip())
        kw_idx = int(parts[3].strip())
        target_qq = int(parts[4].strip())
    except ValueError:
        await _reply_private(bot, user_id, "群号、编号和QQ必须是数字")
        return

    rules = _load_rules_from_file()
    rule = resolve_rule_reference(rules, group_id)
    if not rule:
        await _reply_private(bot, user_id, f"编号 {group_id} 不存在，发送 status 查看列表")
        return
    group_id = rule["group_id"]

    if kw_idx < 1 or kw_idx > len(rule["keywords"]):
        await _reply_private(bot, user_id, f"编号 {kw_idx} 不存在，当前共 {len(rule['keywords'])} 个关键词")
        return

    kw = rule["keywords"][kw_idx - 1]

    if sub == "add":
        targets = kw.get("targets", [])
        if target_qq in targets:
            await _reply_private(bot, user_id, f"QQ {target_qq} 已是关键词 '{kw['word']}' 的目标")
            return
        targets.append(target_qq)
        kw["targets"] = sorted(targets)
    else:
        targets = kw.get("targets", [])
        if target_qq not in targets:
            await _reply_private(bot, user_id, f"QQ {target_qq} 不是关键词 '{kw['word']}' 的目标")
            return
        targets.remove(target_qq)
        if targets:
            kw["targets"] = targets
        else:
            kw.pop("targets", None)

    if await _save_rules_or_reply(bot, user_id, upsert_rule(rules, rule)) is None:
        return
    await _reply_image(bot, user_id, await _render_rule_annotated(bot, rule, rules), title=f"群 {group_id}")


async def _session_menu_status(bot: Bot, user_id: int, text: str, session: dict, rules: list[dict]) -> bool:
    group_id = session["group_id"]
    rule = find_rule(rules, group_id)
    if not rule:
        await _session_manager.clear_state(user_id)
        await _reply_private(bot, user_id, f"编号 {group_id} 不存在")
        return True

    if text == "1":
        if not await _set_session_or_reply(bot, user_id, "awaiting_keyword_add", group_id=group_id):
            return True
        await _reply_private(bot, user_id, "输入关键词，多个用逗号分隔")
    elif text == "2":
        if not rule["keywords"]:
            await _reply_private(bot, user_id, "暂无关键词")
            await _session_manager.clear_state(user_id)
            return True
        if not await _set_session_or_reply(bot, user_id, "awaiting_keyword_remove", group_id=group_id):
            return True
        kw_list = "\n".join(f"{i}. {kw['word']}" for i, kw in enumerate(rule["keywords"], 1))
        await _reply_private(bot, user_id, f"输入要删除的编号，多个用逗号分隔：\n{kw_list}")
    elif text == "3":
        if not rule["keywords"]:
            await _reply_private(bot, user_id, "暂无关键词")
            await _session_manager.clear_state(user_id)
            return True
        if not await _set_session_or_reply(bot, user_id, "awaiting_keyword_toggle", group_id=group_id):
            return True
        kw_list = "\n".join(
            f"{i}. {kw['word']} {'启用' if kw.get('enabled', True) else '禁用'}"
            for i, kw in enumerate(rule["keywords"], 1)
        )
        await _reply_private(bot, user_id, f"输入要切换的编号，多个用逗号分隔：\n{kw_list}")
    elif text == "4":
        await _session_manager.clear_state(user_id)
        stats_text, has_data = render_keyword_stats(_keyword_stats)
        if has_data:
            await _reply_image(bot, user_id, stats_text, title="今日统计")
        else:
            await _reply_private(bot, user_id, stats_text)
    elif text == "5":
        await _session_manager.clear_state(user_id)
        await _handle_remind_command(bot, user_id, "remind quote 09:00")
    else:
        await _reply_private(bot, user_id, f"无效选项，请输入 1-5\n\n{menu_options_text()}")
    return True


async def _session_awaiting_keyword_add(bot: Bot, user_id: int, text: str, session: dict, rules: list[dict]) -> bool:
    group_id = session["group_id"]
    rule = find_rule(rules, group_id)
    if not rule:
        await _session_manager.clear_state(user_id)
        await _reply_private(bot, user_id, f"编号 {group_id} 不存在")
        return True

    keywords = parse_csv_items(text)
    if not keywords:
        await _reply_private(bot, user_id, "关键词不能为空")
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
        await _reply_private(bot, user_id, f"关键词已存在: {', '.join(skipped)}")
        await _session_manager.clear_state(user_id)
        return True

    if await _save_rules_or_reply(bot, user_id, upsert_rule(rules, rule)) is None:
        return True

    rendered = await _render_rule_annotated_with_menu(bot, rule, rules)
    msg = f"已添加 {len(added)} 个关键词: {', '.join(added)}\n\n{rendered}"
    if skipped:
        msg = f"已存在: {', '.join(skipped)}\n\n" + msg
    await _reply_image(bot, user_id, msg, title=f"群 {group_id}")
    return True


async def _session_awaiting_keyword_remove(bot: Bot, user_id: int, text: str, session: dict, rules: list[dict]) -> bool:
    group_id = session["group_id"]
    rule = find_rule(rules, group_id)
    if not rule:
        await _session_manager.clear_state(user_id)
        await _reply_private(bot, user_id, f"编号 {group_id} 不存在")
        return True

    indices, error = parse_indices(text)
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
        await _reply_private(bot, user_id, f"编号 {', '.join(map(str, invalid))} 不存在，当前共 {len(rule['keywords'])} 个关键词")
        return True

    if await _save_rules_or_reply(bot, user_id, upsert_rule(rules, rule)) is None:
        return True

    removed_names = ", ".join(kw["word"] for kw in removed)
    rendered = await _render_rule_annotated_with_menu(bot, rule, rules)
    msg = f"已删除 {len(removed)} 个关键词: {removed_names}\n\n{rendered}"
    if invalid:
        msg = f"编号 {', '.join(map(str, invalid))} 不存在\n\n" + msg
    await _reply_image(bot, user_id, msg, title=f"群 {group_id}")
    return True


async def _session_awaiting_keyword_toggle(bot: Bot, user_id: int, text: str, session: dict, rules: list[dict]) -> bool:
    group_id = session["group_id"]
    rule = find_rule(rules, group_id)
    if not rule:
        await _session_manager.clear_state(user_id)
        await _reply_private(bot, user_id, f"编号 {group_id} 不存在")
        return True

    indices, error = parse_indices(text)
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
        await _reply_private(bot, user_id, f"编号 {', '.join(map(str, invalid))} 不存在，当前共 {len(rule['keywords'])} 个关键词")
        return True

    if await _save_rules_or_reply(bot, user_id, upsert_rule(rules, rule)) is None:
        return True

    rendered = await _render_rule_annotated_with_menu(bot, rule, rules)
    msg = f"已切换 {len(changed)} 个关键词\n" + "\n".join(changed) + f"\n\n{rendered}"
    if invalid:
        msg = f"编号 {', '.join(map(str, invalid))} 不存在\n\n" + msg
    await _reply_image(bot, user_id, msg, title=f"群 {group_id}")
    return True


async def _session_confirm_keyword_set(bot: Bot, user_id: int, text: str, session: dict, rules: list[dict]) -> bool:
    choice = text.strip().lower()
    if choice in {"no", "n", "取消", "cancel"}:
        await _session_manager.clear_state(user_id)
        await _reply_private(bot, user_id, "已取消")
        return True
    if choice not in {"yes", "y", "确认"}:
        await _reply_private(bot, user_id, "回复 yes 确认，cancel 取消")
        return True

    group_id = session["group_id"]
    keywords = session["keywords"]
    rule = find_rule(rules, group_id)
    if not rule:
        await _session_manager.clear_state(user_id)
        await _reply_private(bot, user_id, f"编号 {group_id} 不存在")
        return True
    rule["keywords"] = [{"word": kw, "enabled": True} for kw in keywords]
    if await _save_rules_or_reply(bot, user_id, upsert_rule(rules, rule)) is None:
        return True
    await _session_manager.clear_state(user_id)
    rendered = await _render_rule_annotated(bot, rule, rules)
    await _reply_image(bot, user_id, f"已替换为 {len(keywords)} 个关键词\n\n{rendered}", title=f"群 {group_id}")
    return True


async def _session_confirm_rule_delgroup(bot: Bot, user_id: int, text: str, session: dict, rules: list[dict]) -> bool:
    choice = text.strip().lower()
    if choice in {"no", "n", "取消", "cancel"}:
        await _session_manager.clear_state(user_id)
        await _reply_private(bot, user_id, "已取消")
        return True
    if choice not in {"yes", "y", "确认"}:
        await _reply_private(bot, user_id, "回复 yes 确认删除，cancel 取消")
        return True

    group_id = session["group_id"]
    if not find_rule(rules, group_id):
        await _session_manager.clear_state(user_id)
        await _reply_private(bot, user_id, f"编号 {group_id} 不存在")
        return True
    updated = [item for item in rules if item["group_id"] != group_id]
    if await _save_rules_or_reply(bot, user_id, updated) is None:
        return True
    await _session_manager.clear_state(user_id)
    await _reply_private(bot, user_id, f"已删除群规则 {group_id}")
    return True


_SESSION_HANDLERS = {
    "menu_status": _session_menu_status,
    "awaiting_keyword_add": _session_awaiting_keyword_add,
    "awaiting_keyword_remove": _session_awaiting_keyword_remove,
    "awaiting_keyword_toggle": _session_awaiting_keyword_toggle,
    "confirm_keyword_set": _session_confirm_keyword_set,
    "confirm_rule_delgroup": _session_confirm_rule_delgroup,
}


async def _handle_session_input(bot: Bot, user_id: int, text: str) -> bool:
    """根据会话状态处理用户输入，已处理返回 True。"""
    try:
        session, was_expired = await _session_manager.get_state_or_check_expired(user_id)
        if was_expired:
            await _reply_private(bot, user_id, "操作已超时")
            if text.strip() in {"1", "2", "3", "4", "5"}:
                rules = _load_rules_from_file()
                if rules:
                    display_rules = await _annotate_group_names(bot, rules)
                    if len(display_rules) == 1:
                        rule = display_rules[0]
                        if await _session_manager.set_state(user_id, "menu_status", group_id=rule["group_id"]):
                            await _reply_image(bot, user_id, render_rule_with_menu(rule, index=1), title=f"群 {rule['group_id']}")
                            return True
            return False

        if not session:
            return False

        if text.strip().lower() in {"cancel", "取消", "done", "完成"}:
            await _session_manager.clear_state(user_id)
            await _reply_private(bot, user_id, "已退出")
            return True

        state = session["state"]
        handler = _SESSION_HANDLERS.get(state)
        if handler:
            rules = _load_rules_from_file()
            return await handler(bot, user_id, text, session, rules)

        return False

    except Exception as e:
        logger.error("Session input handling failed for user %s: %s", user_id, e, exc_info=True)
        await _session_manager.clear_state(user_id)
        await _reply_private(bot, user_id, "操作失败，请重新开始")
        return True


# --- main dispatch ---

COMMANDS = {"status", "add", "remove", "set", "on", "off", "disable", "enable", "stats", "quote", "help", "h", "rule", "remind", "cancel", "kwtarget"}


@admin_matcher.handle()
async def handle_admin_command(bot: Bot, event: PrivateMessageEvent) -> None:
    """将私聊管理命令分发到对应处理器。"""
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
    elif first_word == "kwtarget":
        await _handle_kwtarget(bot, event.user_id, text.split(maxsplit=4))
    else:
        await _handle_command(bot, event.user_id, first_word, text)

    raise FinishedException


# --- session cleanup task ---

try:
    from nonebot_plugin_apscheduler import scheduler

    @scheduler.scheduled_job("interval", minutes=10, id="cleanup_sessions")
    async def cleanup_expired_sessions() -> None:
        """每 10 分钟清理过期会话。"""
        count = await _session_manager.cleanup_expired()
        if count > 0:
            logger.info("Cleaned up %d expired sessions", count)

    @scheduler.scheduled_job("interval", minutes=1, id="flush_stats")
    async def flush_stats() -> None:
        """每分钟将关键词命中统计持久化到磁盘（仅在有变更时），并清理过期数据。"""
        global _stats_dirty
        if _stats_dirty:
            today = time.strftime("%Y-%m-%d")
            stale_keys = [k for k in _keyword_stats if not k.startswith(today)]
            for k in stale_keys:
                del _keyword_stats[k]
            save_stats(_keyword_stats)
            _stats_dirty = False
except ImportError:
    logger.warning("nonebot-plugin-apscheduler not available, scheduled tasks disabled")


_driver = get_driver()


@_driver.on_shutdown
async def _save_stats_on_shutdown() -> None:
    if _stats_dirty:
        save_stats(_keyword_stats)
