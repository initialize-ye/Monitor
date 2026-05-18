"""提醒插件的纯工具函数。

无 NoneBot 依赖，可直接测试。
"""

import re
from datetime import date, datetime


REMIND_TYPE_LABELS = {
    "daily": "每天",
    "workday": "工作日(周一至周五)",
    "once": "单次",
    "interval": "间隔",
    "period": "周期催促",
}


def render_reminder(rem: dict) -> str:
    """将提醒渲染为显示文本。"""
    rem_type = rem.get("type", "daily")
    type_label = REMIND_TYPE_LABELS.get(rem_type, "每天")

    if rem_type == "once":
        schedule = f"{rem.get('date', '')} {rem['hour']:02d}:{rem['minute']:02d}"
    elif rem_type == "interval":
        schedule = f"每 {rem['interval_minutes']} 分钟"
    elif rem_type == "period":
        schedule = f"{rem['hour']:02d}:{rem['minute']:02d} 起，每 {rem['repeat_interval']} 分钟"
    else:
        schedule = f"{rem['hour']:02d}:{rem['minute']:02d}"

    if rem.get("auto_generate") == "quote":
        content = "每日一言（自动生成）"
    else:
        content = f"{rem['message']}"

    status = ""
    if rem_type == "period" and rem.get("last_done_date") == date.today().isoformat():
        status = "\n状态: 今日已完成"

    return (
        f"提醒 #{rem['id']}\n"
        f"类型: {type_label}\n"
        f"时间: {schedule}\n"
        f"{content}"
        f"{status}"
    )


def parse_time(time_str: str) -> tuple[int | None, int | None, str | None]:
    """解析时间字符串 (HH:MM)，返回 (时, 分, 错误信息)。"""
    match = re.match(r"^(\d{1,2}):(\d{2})$", time_str.strip())
    if not match:
        return None, None, "时间格式错误，请使用 HH:MM（例如 09:00、18:30）"
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None, None, "时间范围错误：小时 0-23，分钟 0-59"
    return hour, minute, None


def parse_id_list(raw: str) -> tuple[list[int], str | None]:
    """解析逗号分隔的提醒 ID 列表，返回 (ID列表, 错误信息)。"""
    parts = [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()]
    if not parts:
        return [], "请输入提醒编号"
    try:
        ids = [int(item) for item in parts]
    except ValueError:
        return [], "编号必须是数字，多个编号用逗号分隔"
    return sorted(set(ids)), None


def should_resume_period_interval(rem: dict) -> bool:
    """检查周期催促是否应恢复（已过开始时间且今日未完成）。"""
    if rem.get("type") != "period" or not rem.get("enabled", True):
        return False
    if rem.get("last_done_date") == date.today().isoformat():
        return False
    now = datetime.now()
    start = now.replace(hour=int(rem.get("hour", 9)), minute=int(rem.get("minute", 0)), second=0, microsecond=0)
    return now >= start


def format_schedule_result(new_rem: dict, error: str | None) -> str:
    """格式化调度结果，可选附带错误信息。"""
    if error:
        return f"注意: 提醒已保存，但调度失败: {error}\n\n{render_reminder(new_rem)}"
    return render_reminder(new_rem)
