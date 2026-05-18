"""关键词转发插件的纯工具函数。

无 NoneBot 依赖，可直接测试。
"""

import re
import time
from collections import deque


def parse_int_set(raw: str) -> set[int]:
    """解析逗号分隔的整数集合。"""
    values: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            values.add(int(part))
    return values


def parse_str_list(raw: str) -> list[str]:
    """解析逗号分隔的字符串列表。"""
    return [part.strip() for part in raw.split(",") if part.strip()]


def normalize_text(text: str, case_sensitive: bool = False) -> str:
    """规范化文本用于匹配。"""
    return text if case_sensitive else text.lower()


def match_keywords(text: str, keywords: list[dict], use_regex: bool, case_sensitive: bool = False) -> list[str]:
    """匹配文本中的关键词，返回命中的关键词列表。"""
    matched: list[str] = []
    normalized = normalize_text(text, case_sensitive)
    for kw in keywords:
        if not kw.get("enabled", True):
            continue
        word = kw["word"]
        if use_regex:
            flags = 0 if case_sensitive else re.IGNORECASE
            try:
                if re.search(word, text, flags=flags):
                    matched.append(word)
            except re.error:
                continue
        else:
            candidate = word if case_sensitive else word.lower()
            if candidate in normalized:
                matched.append(word)
    return matched


def is_duplicate(message_key: str, recent_keys: deque, recent_seen: set, dedupe_seconds: int = 30) -> bool:
    """检查并记录消息去重，重复消息返回 True。"""
    now = time.time()
    while recent_keys and now - recent_keys[0][0] > dedupe_seconds:
        _, expired = recent_keys.popleft()
        recent_seen.discard(expired)
    if message_key in recent_seen:
        return True
    recent_keys.append((now, message_key))
    recent_seen.add(message_key)
    return False


def check_keyword_cooldown(group_id: int, word: str, cooldown_dict: dict, cooldown_seconds: int = 15) -> bool:
    """检查关键词是否在冷却中，冷却中返回 True。"""
    key = f"{group_id}:{word}"
    now = time.time()
    last = cooldown_dict.get(key)
    if last and now - last < cooldown_seconds:
        return True
    cooldown_dict[key] = now
    return False


def track_keyword_hit(group_id: int, word: str, stats_dict: dict) -> None:
    """记录关键词命中统计。"""
    today = time.strftime("%Y-%m-%d")
    key = f"{today}:{group_id}:{word}"
    stats_dict[key] = stats_dict.get(key, 0) + 1


def parse_csv_items(raw: str) -> list[str]:
    """解析逗号分隔的项目（支持中文逗号）。"""
    return [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()]


def parse_indices(raw: str) -> tuple[list[int], str | None]:
    """解析逗号分隔的编号列表，返回 (编号列表, 错误信息)。"""
    try:
        indices = [int(item) for item in parse_csv_items(raw)]
    except ValueError:
        return [], "编号必须是数字，多个编号用逗号分隔"
    if not indices:
        return [], "请输入编号"
    return sorted(set(indices)), None


def resolve_group_id(text: str) -> tuple[int | None, str | None]:
    """从命令文本解析群号和关键词，返回 (群号或None, 关键词或None)。"""
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


def rule_by_index(rules: list[dict], index: int) -> dict | None:
    """按 1 起始的索引获取规则。"""
    if 1 <= index <= len(rules):
        return rules[index - 1]
    return None


def resolve_rule_reference(rules: list[dict], value: int) -> dict | None:
    """按群号或 1 起始的索引查找规则。"""
    for rule in rules:
        if rule["group_id"] == value:
            return rule
    return rule_by_index(rules, value)


def rule_index(rules: list[dict], group_id: int) -> int | None:
    """按群号获取规则的 1 起始索引。"""
    return next((i for i, rule in enumerate(rules, 1) if rule["group_id"] == group_id), None)


def clean_display_text(text: str) -> str:
    """移除 emoji 并规范化空白字符。"""
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


def render_rule(rule: dict, index: int | None = None) -> str:
    """将规则渲染为显示文本。"""
    status_badge = "已启用" if rule['enabled'] else "已禁用"
    group_name = str(rule.get("group_name") or "未知")

    if rule["keywords"]:
        enabled_kws = [kw for kw in rule["keywords"] if kw.get('enabled', True)]
        disabled_kws = [kw for kw in rule["keywords"] if not kw.get('enabled', True)]

        kw_lines = []
        for i, kw in enumerate(rule["keywords"], 1):
            suffix = " 停用" if not kw.get('enabled', True) else ""
            kw_targets = kw.get("targets")
            target_suffix = f" → {','.join(map(str, kw_targets))}" if kw_targets else ""
            kw_lines.append(f"  {i}. {kw['word']}{target_suffix}{suffix}")

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


def render_rule_with_menu(rule: dict, index: int | None = None) -> str:
    """将规则渲染为带交互菜单的显示文本。"""
    base = render_rule(rule, index=index)
    menu = (
        "\n\n"
        "快捷操作\n"
        "1.  添加关键词\n"
        "2.  删除关键词\n"
        "3.  切换启用状态\n"
        "4.  查看今日统计\n"
        "5.  设置每日一言\n"
        "\n"
        "回复数字选择，cancel 退出"
    )
    return base + menu


def render_keyword_stats(stats: dict[str, int]) -> tuple[str, bool]:
    """渲染关键词命中统计，返回 (文本, 是否有数据)。"""
    today = time.strftime("%Y-%m-%d")
    today_hits = [(k.split(":", 2)[-1], v) for k, v in stats.items() if k.startswith(today)]
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


def menu_options_text() -> str:
    """获取菜单选项文本。"""
    return (
        "快捷操作\n"
        "1. 添加关键词\n"
        "2. 删除关键词\n"
        "3. 切换启用状态\n"
        "4. 查看今日统计\n"
        "5. 设置每日一言\n"
        "回复数字选择，cancel 退出"
    )


def build_admin_help() -> str:
    """构建管理帮助文本。"""
    return (
        "常用命令\n"
        "status                              查看规则和快捷菜单\n"
        "stats                               今日命中统计\n"
        "quote [HH:MM]                       每日一言，默认 09:00\n"
        "cancel                              退出当前操作\n"
        "\n"
        "关键词管理\n"
        "add [编号|群号] <词1,词2>            添加关键词\n"
        "remove [编号|群号] <编号>            删除关键词\n"
        "set [编号|群号] <词1,词2>            替换全部关键词\n"
        "disable/enable [编号|群号] <编号>    禁用/启用关键词\n"
        "on/off [编号|群号]                   启用/禁用监听\n"
        "\n"
        "定时提醒\n"
        "remind <HH:MM> <内容>                每日提醒\n"
        "remind once <日期> <时间> <内容>     单次提醒\n"
        "remind workday <HH:MM> <内容>        工作日提醒\n"
        "remind interval <分钟> <内容>        间隔提醒\n"
        "remind period <时间> <分钟> <内容>   周期催促\n"
        "remind quote <HH:MM>                 每日一言\n"
        "remind edit <编号> <字段> <值>       编辑提醒\n"
        "remind done <编号>                   标记完成\n"
        "remind remove <编号>                 删除提醒\n"
        "remind list                          查看提醒\n"
        "\n"
        "高级管理\n"
        "rule addgroup <群号>                 添加群规则\n"
        "rule delgroup <编号|群号>            删除群规则\n"
        "rule addtarget <编号|群号> <QQ>      添加转发目标\n"
        "rule deltarget <编号|群号> <QQ>      删除转发目标\n"
        "kwtarget add <群号> <编号> <QQ>      添加关键词目标\n"
        "kwtarget del <群号> <编号> <QQ>      删除关键词目标\n"
        "\n"
        "多个关键词或编号支持逗号分隔\n"
        "单群模式下可省略编号或群号"
    )
