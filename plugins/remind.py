"""提醒插件：定时提醒、周期催促、管理命令。"""

import os
import re
from datetime import date, datetime
from nonebot import logger, get_driver
from nonebot.adapters.onebot.v11 import Bot

from reminders import load_reminders, save_reminders, normalize_reminder, next_reminder_id
from .common import reply_image as _reply_image, reply_private as _reply
from quotes import random_quote
from remind_utils import (
    render_reminder, parse_time, parse_id_list,
    should_resume_period_interval, format_schedule_result, REMIND_TYPE_LABELS,
)

TZ = os.getenv("TZ", "Asia/Shanghai")
driver = get_driver()

_scheduled_jobs: dict[int, str] = {}


def _schedule_period_interval(rem_id: int, interval_minutes: int) -> tuple[bool, str | None]:
    try:
        from nonebot_plugin_apscheduler import scheduler
        scheduler.add_job(
            _fire_period_interval, "interval",
            minutes=interval_minutes,
            id=f"period_interval_{rem_id}", replace_existing=True,
            args=[rem_id], timezone=TZ,
        )
        logger.info("Scheduled period interval for reminder %s", rem_id)
        return True, None
    except Exception as exc:
        logger.error("Failed to schedule period interval %s: %s", rem_id, exc)
        return False, str(exc)


def _schedule(rem: dict) -> tuple[bool, str | None]:
    if not rem["enabled"]:
        return True, None
    try:
        from nonebot_plugin_apscheduler import scheduler
        job_id = f"remind_{rem['id']}"
        rem_type = rem.get("type", "daily")

        if rem_type == "once":
            if rem.get("fired"):
                return True, None
            run_date = datetime.strptime(f"{rem['date']} {int(rem['hour']):02d}:{int(rem['minute']):02d}", "%Y-%m-%d %H:%M")
            scheduler.add_job(
                _fire, "date", run_date=run_date,
                id=job_id, replace_existing=True, args=[rem["id"]], timezone=TZ,
            )
        elif rem_type == "interval":
            scheduler.add_job(
                _fire, "interval", minutes=rem["interval_minutes"],
                id=job_id, replace_existing=True, args=[rem["id"]], timezone=TZ,
            )
        elif rem_type == "period":
            scheduler.add_job(
                _fire, "cron", hour=rem["hour"], minute=rem["minute"],
                id=job_id, replace_existing=True, args=[rem["id"]], timezone=TZ,
            )
        elif rem_type == "workday":
            scheduler.add_job(
                _fire, "cron", hour=rem["hour"], minute=rem["minute"], day_of_week="mon-fri",
                id=job_id, replace_existing=True, args=[rem["id"]], timezone=TZ,
            )
        else:  # daily
            scheduler.add_job(
                _fire, "cron", hour=rem["hour"], minute=rem["minute"],
                id=job_id, replace_existing=True, args=[rem["id"]], timezone=TZ,
            )

        _scheduled_jobs[rem["id"]] = job_id
        logger.info("Scheduled reminder %s (%s)", rem["id"], rem_type)
        return True, None
    except Exception as exc:
        logger.error("Failed to schedule reminder %s: %s", rem["id"], exc)
        return False, str(exc)


def _unschedule(rem_id: int) -> None:
    job_id = _scheduled_jobs.pop(rem_id, None)
    if job_id is None:
        return
    try:
        from nonebot_plugin_apscheduler import scheduler
        scheduler.remove_job(job_id)
        logger.info("Unscheduled reminder %s", rem_id)
    except Exception:
        logger.debug("Job %s for reminder %s not found or already removed", job_id, rem_id)


def _unschedule_period_interval(rem_id: int) -> None:
    try:
        from nonebot_plugin_apscheduler import scheduler
        scheduler.remove_job(f"period_interval_{rem_id}")
    except Exception:
        logger.debug("Period interval job %s not found or already removed", rem_id)


def _resolve_targets(rem: dict) -> list[int]:
    """解析提醒目标，未设置时回退到创建者 QQ。"""
    targets = rem.get("targets", [])
    if not targets:
        targets = [rem["creator_qq"]] if rem.get("creator_qq") else []
    return targets


async def _create_and_schedule_reminder(bot: Bot, user_id: int, rem_dict: dict, success_msg: str) -> None:
    """通用模式：加载提醒、创建新提醒、保存、调度、回复。"""
    try:
        reminders = load_reminders()
    except (RuntimeError, ValueError):
        reminders = []
    new_rem = normalize_reminder(rem_dict)
    reminders.append(new_rem)
    try:
        save_reminders(reminders)
    except OSError:
        await _reply(bot, user_id, "保存失败，请稍后重试")
        return
    ok, error = _schedule(new_rem)
    await _reply_image(
        bot, user_id,
        f"{success_msg}\n{format_schedule_result(new_rem, error if not ok else None)}",
        title=f"提醒 {new_rem['id']}"
    )


async def _fire(rem_id: int) -> None:
    try:
        reminders = load_reminders()
    except (RuntimeError, ValueError):
        logger.error("Failed to load reminders for firing")
        return

    rem = next((r for r in reminders if r["id"] == rem_id), None)
    if not rem or not rem["enabled"]:
        return

    # Auto-delete once reminders after firing
    if rem.get("type") == "once":
        reminders = [r for r in reminders if r["id"] != rem_id]
        try:
            save_reminders(reminders)
            _unschedule(rem_id)
            logger.info("Auto-deleted once reminder %s after firing", rem_id)
        except OSError as exc:
            logger.error("Failed to delete once reminder %s: %s, still sending", rem_id, exc)

    # Period reminder: skip if done today, schedule interval for repeats
    if rem.get("type") == "period":
        today = date.today().isoformat()
        if rem.get("last_done_date") == today:
            logger.info("Period reminder %s already done today, skipping", rem_id)
            return
        ok, error = _schedule_period_interval(rem_id, rem["repeat_interval"])
        if not ok:
            logger.error("Failed to start period interval for reminder %s: %s", rem_id, error)

    bots = list(driver.bots.values())
    if not bots:
        logger.warning("No bot available to send reminder %s", rem_id)
        return

    bot = bots[0]
    rem_type = rem.get("type", "daily")
    auto = rem.get("auto_generate", "")

    if auto == "quote":
        text = await random_quote()
    else:
        label = REMIND_TYPE_LABELS.get(rem_type, "")
        if rem_type == "period":
            text = f"提醒 #{rem_id} - 周期催促\n{rem['message']}\n\n完成后回复: remind done {rem_id}"
        elif label:
            text = f"提醒 #{rem_id} - {label}\n{rem['message']}"
        else:
            text = f"提醒 #{rem_id}\n{rem['message']}"

    targets = _resolve_targets(rem)

    if auto == "quote":
        for target_qq in targets:
            try:
                await _reply_image(bot, target_qq, text, title="每日一言")
            except Exception as exc:
                logger.error("Failed to send quote reminder %s to %s: %s", rem_id, target_qq, exc)
        logger.info("Fired quote reminder %s", rem_id)
        return

    # Period cron trigger: send as plain text
    if rem_type == "period":
        for target_qq in targets:
            try:
                await bot.call_api("send_msg", message_type="private", user_id=target_qq, message=text)
            except Exception as exc:
                logger.error("Failed to send period reminder %s to %s: %s", rem_id, target_qq, exc)
        logger.info("Fired period reminder %s: %s", rem_id, rem["message"])
        return

    for target_qq in targets:
        try:
            await bot.call_api("send_msg", message_type="private", user_id=target_qq, message=text)
        except Exception as exc:
            logger.error("Failed to send reminder %s to %s: %s", rem_id, target_qq, exc)

    logger.info("Fired reminder %s: %s", rem_id, rem["message"])


async def _fire_period_interval(rem_id: int) -> None:
    """周期提醒的间隔任务触发函数。"""
    try:
        reminders = load_reminders()
    except (RuntimeError, ValueError):
        logger.error("Failed to load reminders for period interval %s", rem_id)
        return

    rem = next((r for r in reminders if r["id"] == rem_id), None)
    if not rem or not rem["enabled"]:
        return

    today = date.today().isoformat()
    if rem.get("last_done_date") == today:
        logger.info("Period reminder %s done today, stopping interval", rem_id)
        _unschedule_period_interval(rem_id)
        return

    bots = list(driver.bots.values())
    if not bots:
        return

    bot = bots[0]
    text = f"提醒 #{rem_id} - 周期催促\n{rem['message']}\n\n完成后回复: remind done {rem_id}"

    targets = _resolve_targets(rem)

    for target_qq in targets:
        try:
            await bot.call_api("send_msg", message_type="private", user_id=target_qq, message=text)
        except Exception as exc:
            logger.error("Failed to send period interval reminder %s to %s: %s", rem_id, target_qq, exc)

    logger.info("Period interval reminder %s: %s", rem_id, rem["message"])


def restore_all() -> None:
    """启动时从文件恢复所有提醒调度。"""
    try:
        reminders = load_reminders()
    except (RuntimeError, ValueError):
        logger.warning("No reminders to restore")
        return
    restored = 0
    interval_restored = 0
    for rem in reminders:
        rem = normalize_reminder(rem)
        ok, error = _schedule(rem)
        if ok:
            restored += 1
        else:
            logger.error("Failed to restore reminder %s: %s", rem.get("id"), error)
        if should_resume_period_interval(rem):
            ok, error = _schedule_period_interval(rem["id"], rem["repeat_interval"])
            if ok:
                interval_restored += 1
            else:
                logger.error("Failed to restore period interval %s: %s", rem.get("id"), error)
    if reminders:
        logger.info("Restored %d reminders and %d period intervals", restored, interval_restored)


async def _cmd_remind_list(bot: Bot, user_id: int) -> None:
    try:
        reminders = load_reminders()
    except (RuntimeError, ValueError):
        await _reply(bot, user_id, "加载提醒失败，请稍后重试")
        return
    if not reminders:
        await _reply(bot, user_id, "暂无提醒")
        return
    text_list = "\n\n".join(render_reminder(normalize_reminder(r)) for r in reminders)
    await _reply_image(bot, user_id, text_list, title="提醒列表")


async def _cmd_remind_once(bot: Bot, user_id: int, text: str) -> None:
    parts = text.split(maxsplit=4)
    if len(parts) < 5 or not parts[2].strip() or not parts[3].strip() or not parts[4].strip():
        await _reply(bot, user_id, "用法: remind once <YYYY-MM-DD> <HH:MM> <内容>\n例如: remind once 2025-12-25 10:00 圣诞节快乐")
        return
    date_str = parts[2].strip()
    time_str = parts[3].strip()
    message = parts[4].strip()
    try:
        date.fromisoformat(date_str)
    except ValueError:
        await _reply(bot, user_id, "日期格式错误，使用 YYYY-MM-DD")
        return
    hour, minute, error = parse_time(time_str)
    if error:
        await _reply(bot, user_id, error)
        return
    run_at = datetime.strptime(f"{date_str} {hour:02d}:{minute:02d}", "%Y-%m-%d %H:%M")
    if run_at <= datetime.now():
        await _reply(bot, user_id, "提醒时间不能早于当前时间")
        return
    try:
        reminders = load_reminders()
    except (RuntimeError, ValueError):
        reminders = []
    await _create_and_schedule_reminder(bot, user_id, {
        "id": next_reminder_id(reminders),
        "type": "once",
        "date": date_str,
        "hour": hour,
        "minute": minute,
        "message": message,
        "targets": [],
        "enabled": True,
        "creator_qq": user_id,
    }, "已添加单次提醒")


async def _cmd_remind_interval(bot: Bot, user_id: int, text: str) -> None:
    parts = text.split(maxsplit=3)
    if len(parts) < 4 or not parts[2].strip() or not parts[3].strip():
        await _reply(bot, user_id, "用法: remind interval <分钟> <内容>\n例如: remind interval 30 起来活动一下")
        return
    try:
        interval = int(parts[2].strip())
    except ValueError:
        await _reply(bot, user_id, "间隔必须是数字")
        return
    if interval < 1:
        await _reply(bot, user_id, "间隔至少为 1 分钟")
        return
    message = parts[3].strip()
    try:
        reminders = load_reminders()
    except (RuntimeError, ValueError):
        reminders = []
    await _create_and_schedule_reminder(bot, user_id, {
        "id": next_reminder_id(reminders),
        "type": "interval",
        "interval_minutes": interval,
        "message": message,
        "targets": [],
        "enabled": True,
        "creator_qq": user_id,
    }, "已添加间隔提醒")


async def _cmd_remind_done(bot: Bot, user_id: int, text: str) -> None:
    parts = text.split(maxsplit=2)
    if len(parts) < 3 or not parts[2].strip():
        await _reply(bot, user_id, "用法: remind done <编号>\n例如: remind done 1")
        return
    try:
        rem_id = int(parts[2].strip())
    except ValueError:
        await _reply(bot, user_id, "编号必须是数字")
        return

    try:
        reminders = load_reminders()
    except (RuntimeError, ValueError):
        await _reply(bot, user_id, "加载提醒失败，请稍后重试")
        return

    target = next((r for r in reminders if r["id"] == rem_id), None)
    if not target:
        await _reply(bot, user_id, f"提醒 {rem_id} 不存在")
        return
    if target.get("type") != "period":
        await _reply(bot, user_id, "该提醒不是周期催促类型")
        return

    today = date.today().isoformat()
    if target.get("last_done_date") == today:
        await _reply(bot, user_id, f"提醒 {rem_id} 今日已完成")
        return

    target["last_done_date"] = today
    try:
        save_reminders(reminders)
    except OSError:
        await _reply(bot, user_id, "保存失败，请稍后重试")
        return

    _unschedule_period_interval(rem_id)
    await _reply(bot, user_id, f"已标记提醒 {rem_id} 完成，明天继续")


async def _cmd_remind_edit(bot: Bot, user_id: int, text: str) -> None:
    parts = text.split(maxsplit=4)
    if len(parts) < 4:
        await _reply(bot, user_id, "用法: remind edit <编号> <字段> <值>\n字段: time / message / interval\n例如: remind edit 1 time 10:30")
        return
    try:
        rem_id = int(parts[2].strip())
    except ValueError:
        await _reply(bot, user_id, "编号必须是数字")
        return
    field = parts[3].strip().lower()
    value = parts[4].strip() if len(parts) > 4 else ""

    try:
        reminders = load_reminders()
    except (RuntimeError, ValueError):
        await _reply(bot, user_id, "加载提醒失败，请稍后重试")
        return

    target = next((r for r in reminders if r["id"] == rem_id), None)
    if not target:
        await _reply(bot, user_id, f"提醒 {rem_id} 不存在")
        return

    rem_type = target.get("type", "daily")

    if field == "time":
        if rem_type == "interval":
            await _reply(bot, user_id, "间隔提醒没有固定时间，使用 interval 修改间隔")
            return
        hour, minute, error = parse_time(value)
        if error:
            await _reply(bot, user_id, error)
            return
        if rem_type == "once":
            date_str = target.get("date", "")
            if date_str:
                run_at = datetime.strptime(f"{date_str} {hour:02d}:{minute:02d}", "%Y-%m-%d %H:%M")
                if run_at <= datetime.now():
                    await _reply(bot, user_id, "提醒时间不能早于当前时间")
                    return
        target["hour"] = hour
        target["minute"] = minute
    elif field == "message":
        if target.get("auto_generate") == "quote":
            await _reply(bot, user_id, "每日一言不支持修改内容")
            return
        if not value:
            await _reply(bot, user_id, "内容不能为空")
            return
        target["message"] = value
    elif field in {"interval", "间隔"}:
        if rem_type not in ("interval", "period"):
            await _reply(bot, user_id, "仅间隔提醒和周期催促支持修改间隔")
            return
        try:
            interval = int(value)
        except ValueError:
            await _reply(bot, user_id, "间隔必须是数字（分钟）")
            return
        if interval < 1:
            await _reply(bot, user_id, "间隔至少为 1 分钟")
            return
        if rem_type == "interval":
            target["interval_minutes"] = interval
        else:
            target["repeat_interval"] = interval
    else:
        await _reply(bot, user_id, f"不支持的字段: {field}\n支持: time / message / interval")
        return

    try:
        save_reminders(reminders)
    except OSError:
        await _reply(bot, user_id, "保存失败，请稍后重试")
        return

    _unschedule(rem_id)
    _unschedule_period_interval(rem_id)
    ok, error = _schedule(target)
    if not ok:
        logger.error("Failed to reschedule reminder %s after edit: %s", rem_id, error)
    await _reply_image(
        bot, user_id,
        f"已更新提醒\n{format_schedule_result(target, error if not ok else None)}",
        title=f"提醒 {rem_id}"
    )


async def _cmd_remind_remove(bot: Bot, user_id: int, text: str) -> None:
    parts = text.split(maxsplit=2)
    if len(parts) < 3 or not parts[2].strip():
        await _reply(bot, user_id, "用法: remind remove <编号[,编号]>\n例如: remind remove 1,3")
        return
    rem_ids, error = parse_id_list(parts[2].strip())
    if error:
        await _reply(bot, user_id, error)
        return

    try:
        reminders = load_reminders()
    except (RuntimeError, ValueError):
        await _reply(bot, user_id, "加载提醒失败，请稍后重试")
        return

    existing_ids = {r["id"] for r in reminders}
    removed_ids = [rem_id for rem_id in rem_ids if rem_id in existing_ids]
    missing_ids = [rem_id for rem_id in rem_ids if rem_id not in existing_ids]

    if not removed_ids:
        await _reply(bot, user_id, f"提醒 {', '.join(map(str, missing_ids))} 不存在")
        return

    reminders = [r for r in reminders if r["id"] not in set(removed_ids)]
    try:
        save_reminders(reminders)
    except OSError:
        await _reply(bot, user_id, "保存失败，请稍后重试")
        return

    for rem_id in removed_ids:
        _unschedule(rem_id)
        _unschedule_period_interval(rem_id)

    msg = f"已删除 {len(removed_ids)} 个提醒: {', '.join(map(str, removed_ids))}"
    if missing_ids:
        msg += f"\n注意: 编号不存在: {', '.join(map(str, missing_ids))}"
    await _reply(bot, user_id, msg)


_REMIND_HELP_TEXT = (
    "remind <HH:MM> <内容>                   每天提醒\n"
    "remind once <日期> <时间> <内容>        单次提醒\n"
    "remind workday <HH:MM> <内容>           工作日提醒\n"
    "remind interval <分钟> <内容>           间隔提醒\n"
    "remind period <时间> <分钟> <内容>      周期催促\n"
    "remind quote <HH:MM>                    每日一言\n"
    "remind edit <编号> <字段> <值>          编辑提醒\n"
    "remind done <编号>                      标记完成\n"
    "remind remove <编号>                    删除提醒\n"
    "remind list                             查看提醒"
)


async def handle_command(bot: Bot, user_id: int, text: str) -> None:
    """将 remind 子命令分发到对应处理器。"""
    head_parts = text.split(maxsplit=2)
    sub = head_parts[1].lower() if len(head_parts) > 1 else ""

    if sub == "list":
        await _cmd_remind_list(bot, user_id)
    elif sub == "add":
        await _cmd_remind_daily(bot, user_id, text.split(maxsplit=3))
    elif sub == "quote":
        parts = text.split(maxsplit=3)
        if len(parts) < 3 or not parts[2].strip():
            await _reply(bot, user_id, "用法: remind quote <HH:MM>\n例如: remind quote 09:00")
        elif len(parts) > 3 and parts[3].strip():
            await _reply(bot, user_id, "用法: remind quote <HH:MM>\n每日一言不需要输入内容，每次自动随机生成")
        else:
            await _cmd_remind_quote(bot, user_id, parts)
    elif sub == "once":
        await _cmd_remind_once(bot, user_id, text)
    elif sub == "workday":
        parts = text.split(maxsplit=3)
        if len(parts) < 4 or not parts[2].strip() or not parts[3].strip():
            await _reply(bot, user_id, "用法: remind workday <HH:MM> <内容>\n例如: remind workday 09:00 上班打卡")
        else:
            await _cmd_remind_timed(bot, user_id, parts, "workday")
    elif sub == "interval":
        await _cmd_remind_interval(bot, user_id, text)
    elif sub == "period":
        parts = text.split(maxsplit=4)
        if len(parts) < 5 or not parts[2].strip() or not parts[3].strip() or not parts[4].strip():
            await _reply(bot, user_id, "用法: remind period <HH:MM> <间隔分钟> <内容>\n例如: remind period 18:00 10 背单词")
        else:
            await _cmd_remind_period(bot, user_id, parts)
    elif sub == "done":
        await _cmd_remind_done(bot, user_id, text)
    elif sub == "edit":
        await _cmd_remind_edit(bot, user_id, text)
    elif sub in {"remove", "del", "delete"}:
        await _cmd_remind_remove(bot, user_id, text)
    elif re.match(r"^\d{1,2}:\d{2}$", sub):
        parts = text.split(maxsplit=2)
        if len(parts) < 3 or not parts[2].strip():
            await _reply(bot, user_id, "用法: remind <HH:MM> <内容>\n例如: remind 10:00 背单词")
        else:
            await _cmd_remind_timed(bot, user_id, ["remind", "add", parts[1], parts[2]], "daily")
    else:
        await _reply_image(bot, user_id, _REMIND_HELP_TEXT, title="提醒命令")


async def _cmd_remind_daily(bot: Bot, user_id: int, parts: list[str]) -> None:
    if len(parts) < 4 or not parts[2].strip() or not parts[3].strip():
        await _reply(bot, user_id, "用法: remind <HH:MM> <内容>\n例如: remind 10:00 背单词")
        return
    await _cmd_remind_timed(bot, user_id, parts, "daily")


async def _cmd_remind_timed(bot: Bot, user_id: int, parts: list[str], rem_type: str) -> None:
    time_str = parts[2].strip()
    message = parts[3].strip()
    hour, minute, error = parse_time(time_str)
    if error:
        await _reply(bot, user_id, error)
        return

    try:
        reminders = load_reminders()
    except (RuntimeError, ValueError):
        reminders = []

    type_label = {"daily": "每日", "workday": "工作日"}.get(rem_type, "提醒")
    await _create_and_schedule_reminder(bot, user_id, {
        "id": next_reminder_id(reminders),
        "type": rem_type,
        "hour": hour,
        "minute": minute,
        "message": message,
        "targets": [],
        "enabled": True,
        "creator_qq": user_id,
    }, f"已添加{type_label}提醒")


async def _cmd_remind_quote(bot: Bot, user_id: int, parts: list[str]) -> None:
    time_str = parts[2].strip()
    hour, minute, error = parse_time(time_str)
    if error:
        await _reply(bot, user_id, error)
        return

    try:
        reminders = load_reminders()
    except (RuntimeError, ValueError):
        reminders = []

    await _create_and_schedule_reminder(bot, user_id, {
        "id": next_reminder_id(reminders),
        "type": "daily",
        "hour": hour,
        "minute": minute,
        "targets": [],
        "enabled": True,
        "creator_qq": user_id,
        "auto_generate": "quote",
    }, "已添加每日一言")


async def _cmd_remind_period(bot: Bot, user_id: int, parts: list[str]) -> None:
    time_str = parts[2].strip()
    try:
        interval = int(parts[3].strip())
    except ValueError:
        await _reply(bot, user_id, "间隔必须是数字")
        return
    if interval < 1:
        await _reply(bot, user_id, "间隔至少为 1 分钟")
        return
    message = parts[4].strip()

    hour, minute, error = parse_time(time_str)
    if error:
        await _reply(bot, user_id, error)
        return

    try:
        reminders = load_reminders()
    except (RuntimeError, ValueError):
        reminders = []

    await _create_and_schedule_reminder(bot, user_id, {
        "id": next_reminder_id(reminders),
        "type": "period",
        "hour": hour,
        "minute": minute,
        "repeat_interval": interval,
        "message": message,
        "targets": [],
        "enabled": True,
        "creator_qq": user_id,
    }, "已添加周期催促提醒")


@driver.on_startup
async def _on_startup() -> None:
    restore_all()
