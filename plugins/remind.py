import os
import re
from datetime import date, datetime
from nonebot import logger, get_driver
from nonebot.adapters.onebot.v11 import Bot

from reminders import load_reminders, save_reminders, normalize_reminder, next_reminder_id
from image_renderer import render_text_to_image
from quotes import random_quote

TZ = os.getenv("TZ", "Asia/Shanghai")
driver = get_driver()

_scheduled_jobs: dict[int, str] = {}


REMIND_TYPE_LABELS = {
    "daily": "每天",
    "workday": "工作日(周一至周五)",
    "once": "单次",
    "interval": "间隔",
    "period": "周期催促",
}


def _render_reminder(rem: dict) -> str:
    lines = [f"编号: {rem['id']}"]
    rem_type = rem.get("type", "daily")
    if rem_type == "once":
        lines.append(f"时间: {rem.get('date', '')} {rem['hour']:02d}:{rem['minute']:02d}")
    elif rem_type == "interval":
        lines.append(f"间隔: 每{rem['interval_minutes']}分钟")
    elif rem_type == "period":
        lines.append(f"时间: {rem['hour']:02d}:{rem['minute']:02d} 每{rem['repeat_interval']}分钟催促 每天")
    else:
        lines.append(f"时间: {rem['hour']:02d}:{rem['minute']:02d} {REMIND_TYPE_LABELS.get(rem_type, '')}")
    if rem.get("auto_generate") == "quote":
        lines.append("内容: [每日一言] 每次触发随机生成")
    else:
        lines.append(f"内容: {rem['message']}")
    if rem_type == "period" and rem.get("last_done_date") == date.today().isoformat():
        lines.append("状态: 今日已完成")
    return "\n".join(lines)


async def _reply(bot: Bot, user_id: int, message: str) -> None:
    await bot.call_api("send_msg", message_type="private", user_id=user_id, message=message)


async def _reply_image(bot: Bot, user_id: int, text: str, title: str = "提醒") -> None:
    """Render text to a styled image and send it, fall back to plain text on error."""
    try:
        b64 = render_text_to_image(text, title=title)
        await bot.call_api("send_msg", message_type="private", user_id=user_id, message=[
            {"type": "image", "data": {"file": b64}},
        ])
    except Exception:
        await _reply(bot, user_id, text)


def _schedule(rem: dict) -> None:
    if not rem["enabled"]:
        return
    try:
        from nonebot_plugin_apscheduler import scheduler
        job_id = f"remind_{rem['id']}"
        rem_type = rem.get("type", "daily")

        if rem_type == "once":
            if rem.get("fired"):
                return
            run_date = datetime.strptime(f"{rem['date']} {rem['hour']}:{rem['minute']}", "%Y-%m-%d %H:%M")
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
    except Exception as exc:
        logger.error("Failed to schedule reminder %s: %s", rem["id"], exc)


def _unschedule(rem_id: int) -> None:
    job_id = _scheduled_jobs.pop(rem_id, None)
    if job_id is None:
        return
    try:
        from nonebot_plugin_apscheduler import scheduler
        scheduler.remove_job(job_id)
        logger.info("Unscheduled reminder %s", rem_id)
    except Exception:
        pass


def _unschedule_period_interval(rem_id: int) -> None:
    try:
        from nonebot_plugin_apscheduler import scheduler
        scheduler.remove_job(f"period_interval_{rem_id}")
    except Exception:
        pass


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
        except OSError:
            pass
        _unschedule(rem_id)
        logger.info("Auto-deleted once reminder %s after firing", rem_id)

    # Period reminder: skip if done today, schedule interval for repeats
    if rem.get("type") == "period":
        today = date.today().isoformat()
        if rem.get("last_done_date") == today:
            logger.info("Period reminder %s already done today, skipping", rem_id)
            return
        from nonebot_plugin_apscheduler import scheduler
        interval_id = f"period_interval_{rem_id}"
        if not scheduler.get_job(interval_id):
            scheduler.add_job(
                _fire_period_interval, "interval",
                minutes=rem["repeat_interval"],
                id=interval_id, replace_existing=True,
                args=[rem_id], timezone=TZ,
            )
            logger.info("Scheduled period interval for reminder %s", rem_id)

    bots = list(driver.bots.values())
    if not bots:
        logger.warning("No bot available to send reminder %s", rem_id)
        return

    bot = bots[0]
    rem_type = rem.get("type", "daily")
    auto = rem.get("auto_generate", "")

    if auto == "quote":
        text = f"[每日一言] {random_quote()}"
    else:
        label = REMIND_TYPE_LABELS.get(rem_type, "")
        text = f"[提醒] {rem['message']}"
        if label:
            text = f"[{label}提醒] {rem['message']}"

    targets = rem.get("targets", [])
    if not targets:
        targets = [rem["creator_qq"]] if rem.get("creator_qq") else []

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
    """Fired by the interval job of a period reminder (repeating nag)."""
    try:
        reminders = load_reminders()
    except (RuntimeError, ValueError):
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
    text = f"[周期催促提醒] {rem['message']}"

    targets = rem.get("targets", [])
    if not targets:
        targets = [rem["creator_qq"]] if rem.get("creator_qq") else []

    for target_qq in targets:
        try:
            await bot.call_api("send_msg", message_type="private", user_id=target_qq, message=text)
        except Exception as exc:
            logger.error("Failed to send period interval reminder %s to %s: %s", rem_id, target_qq, exc)

    logger.info("Period interval reminder %s: %s", rem_id, rem["message"])


def restore_all() -> None:
    try:
        reminders = load_reminders()
    except (RuntimeError, ValueError):
        logger.warning("No reminders to restore")
        return
    for rem in reminders:
        rem = normalize_reminder(rem)
        _schedule(rem)
    if reminders:
        logger.info("Restored %d reminders", len(reminders))


async def handle_command(bot: Bot, user_id: int, text: str) -> None:
    parts = text.split(maxsplit=4)
    sub = parts[1].lower() if len(parts) > 1 else ""

    if sub == "list":
        try:
            reminders = load_reminders()
        except (RuntimeError, ValueError) as exc:
            await _reply(bot, user_id, f"加载提醒失败: {exc}")
            return
        if not reminders:
            await _reply(bot, user_id, "当前没有任何提醒")
            return
        text_list = "\n\n".join(_render_reminder(r) for r in reminders)
        await _reply_image(bot, user_id, text_list, title="提醒列表")
        return

    if sub == "add":
        await _add_daily(bot, user_id, parts)
        return

    if sub == "quote":
        if len(parts) < 3 or not parts[2].strip():
            await _reply(bot, user_id, "用法: remind quote <HH:MM>\n例如: remind quote 09:00")
            return
        if len(parts) > 3 and parts[3].strip():
            await _reply(bot, user_id, "用法: remind quote <HH:MM>\n每日一言不需要输入内容，每次自动随机生成")
            return
        await _add_quote(bot, user_id, parts)
        return

    if sub == "once":
        # remind once <YYYY-MM-DD> <HH:MM> <内容>
        if len(parts) < 5 or not parts[2].strip() or not parts[3].strip() or not parts[4].strip():
            await _reply(bot, user_id, "用法: remind once <YYYY-MM-DD> <HH:MM> <内容>\n例如: remind once 2025-12-25 10:00 圣诞节快乐")
            return
        date_str = parts[2].strip()
        time_str = parts[3].strip()
        message = parts[4].strip()
        try:
            date.fromisoformat(date_str)
        except ValueError:
            await _reply(bot, user_id, "日期格式错误，请使用 YYYY-MM-DD")
            return
        match = re.match(r"^(\d{1,2}):(\d{2})$", time_str)
        if not match:
            await _reply(bot, user_id, "时间格式错误，请使用 HH:MM")
            return
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            await _reply(bot, user_id, "时间范围错误")
            return
        try:
            reminders = load_reminders()
        except (RuntimeError, ValueError):
            reminders = []
        new_rem = normalize_reminder({
            "id": next_reminder_id(reminders),
            "type": "once",
            "date": date_str,
            "hour": hour,
            "minute": minute,
            "message": message,
            "targets": [],
            "enabled": True,
            "creator_qq": user_id,
        })
        reminders.append(new_rem)
        try:
            save_reminders(reminders)
        except OSError as exc:
            await _reply(bot, user_id, f"保存失败: {exc}")
            return
        _schedule(new_rem)
        await _reply(bot, user_id, f"已添加单次提醒\n{_render_reminder(new_rem)}")
        return

    if sub == "workday":
        # remind workday <HH:MM> <内容>
        if len(parts) < 4 or not parts[2].strip() or not parts[3].strip():
            await _reply(bot, user_id, "用法: remind workday <HH:MM> <内容>\n例如: remind workday 09:00 上班打卡")
            return
        await _add_timed(bot, user_id, parts, "workday")
        return

    if sub == "interval":
        # remind interval <分钟> <内容>
        if len(parts) < 4 or not parts[2].strip() or not parts[3].strip():
            await _reply(bot, user_id, "用法: remind interval <分钟> <内容>\n例如: remind interval 30 起来活动一下")
            return
        try:
            interval = int(parts[2].strip())
        except ValueError:
            await _reply(bot, user_id, "间隔必须是数字（分钟）")
            return
        if interval < 1:
            await _reply(bot, user_id, "间隔至少为 1 分钟")
            return
        message = parts[3].strip()

        try:
            reminders = load_reminders()
        except (RuntimeError, ValueError):
            reminders = []
        new_rem = normalize_reminder({
            "id": next_reminder_id(reminders),
            "type": "interval",
            "interval_minutes": interval,
            "message": message,
            "targets": [],
            "enabled": True,
            "creator_qq": user_id,
        })
        reminders.append(new_rem)
        try:
            save_reminders(reminders)
        except OSError as exc:
            await _reply(bot, user_id, f"保存失败: {exc}")
            return
        _schedule(new_rem)
        await _reply(bot, user_id, f"已添加间隔提醒\n{_render_reminder(new_rem)}")
        return

    if sub == "period":
        # remind period <HH:MM> <间隔分钟> <内容>
        if len(parts) < 5 or not parts[2].strip() or not parts[3].strip() or not parts[4].strip():
            await _reply(bot, user_id, "用法: remind period <HH:MM> <间隔分钟> <内容>\n例如: remind period 18:00 10 背单词")
            return
        await _add_period(bot, user_id, parts)
        return

    if sub == "done":
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
        except (RuntimeError, ValueError) as exc:
            await _reply(bot, user_id, f"加载提醒失败: {exc}")
            return

        target = next((r for r in reminders if r["id"] == rem_id), None)
        if not target:
            await _reply(bot, user_id, f"编号 {rem_id} 不存在")
            return
        if target.get("type") != "period":
            await _reply(bot, user_id, "该提醒不是周期催促类型")
            return

        today = date.today().isoformat()
        target["last_done_date"] = today
        try:
            save_reminders(reminders)
        except OSError as exc:
            await _reply(bot, user_id, f"保存失败: {exc}")
            return

        _unschedule_period_interval(rem_id)
        await _reply(bot, user_id, f"已标记提醒 {rem_id} 今日完成，明天继续")
        return

    if sub in {"remove", "del", "delete"}:
        if len(parts) < 3 or not parts[2].strip():
            await _reply(bot, user_id, "用法: remind remove <编号>\n例如: remind remove 1")
            return
        try:
            rem_id = int(parts[2].strip())
        except ValueError:
            await _reply(bot, user_id, "编号必须是数字")
            return

        try:
            reminders = load_reminders()
        except (RuntimeError, ValueError) as exc:
            await _reply(bot, user_id, f"加载提醒失败: {exc}")
            return

        target = next((r for r in reminders if r["id"] == rem_id), None)
        if not target:
            await _reply(bot, user_id, f"编号 {rem_id} 不存在")
            return

        reminders = [r for r in reminders if r["id"] != rem_id]
        try:
            save_reminders(reminders)
        except OSError as exc:
            await _reply(bot, user_id, f"保存失败: {exc}")
            return

        _unschedule(rem_id)
        _unschedule_period_interval(rem_id)
        await _reply(bot, user_id, f"已删除提醒 {rem_id}")
        return

    await _reply(
        bot, user_id,
        "用法:\n"
        "remind add HH:MM <内容>                          — 每天提醒\n"
        "remind once YYYY-MM-DD HH:MM …                   — 单次提醒\n"
        "remind workday HH:MM <内容>                      — 工作日提醒\n"
        "remind interval <分钟> <内容>                    — 间隔提醒\n"
        "remind period <HH:MM> <分钟> <内容>              — 周期催促(不做一直催)\n"
        "remind done <编号>                               — 标记周期催促今日完成\n"
        "remind quote HH:MM                               — 每日一言(随机名言)\n"
        "remind remove <编号>                              — 删除提醒\n"
        "remind list                                       — 查看提醒"
    )


async def _add_daily(bot: Bot, user_id: int, parts: list[str]) -> None:
    if len(parts) < 4 or not parts[2].strip() or not parts[3].strip():
        await _reply(bot, user_id, "用法: remind add <HH:MM> <内容>\n例如: remind add 10:00 背单词")
        return
    await _add_timed(bot, user_id, parts, "daily")


async def _add_timed(bot: Bot, user_id: int, parts: list[str], rem_type: str) -> None:
    time_str = parts[2].strip()
    message = parts[3].strip()
    match = re.match(r"^(\d{1,2}):(\d{2})$", time_str)
    if not match:
        await _reply(bot, user_id, "时间格式错误，请使用 HH:MM（例如 10:00、08:30）")
        return
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        await _reply(bot, user_id, "时间范围错误：小时 0-23，分钟 0-59")
        return

    try:
        reminders = load_reminders()
    except (RuntimeError, ValueError):
        reminders = []

    new_rem = normalize_reminder({
        "id": next_reminder_id(reminders),
        "type": rem_type,
        "hour": hour,
        "minute": minute,
        "message": message,
        "targets": [],
        "enabled": True,
        "creator_qq": user_id,
    })
    reminders.append(new_rem)
    try:
        save_reminders(reminders)
    except OSError as exc:
        await _reply(bot, user_id, f"保存失败: {exc}")
        return

    _schedule(new_rem)
    await _reply(bot, user_id, f"已添加提醒\n{_render_reminder(new_rem)}")


async def _add_quote(bot: Bot, user_id: int, parts: list[str]) -> None:
    time_str = parts[2].strip()
    match = re.match(r"^(\d{1,2}):(\d{2})$", time_str)
    if not match:
        await _reply(bot, user_id, "时间格式错误，请使用 HH:MM（例如 09:00、08:30）")
        return
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        await _reply(bot, user_id, "时间范围错误：小时 0-23，分钟 0-59")
        return

    try:
        reminders = load_reminders()
    except (RuntimeError, ValueError):
        reminders = []

    new_rem = normalize_reminder({
        "id": next_reminder_id(reminders),
        "type": "daily",
        "hour": hour,
        "minute": minute,
        "targets": [],
        "enabled": True,
        "creator_qq": user_id,
        "auto_generate": "quote",
    })
    reminders.append(new_rem)
    try:
        save_reminders(reminders)
    except OSError as exc:
        await _reply(bot, user_id, f"保存失败: {exc}")
        return

    _schedule(new_rem)
    await _reply(bot, user_id, f"已添加每日一言\n{_render_reminder(new_rem)}")


async def _add_period(bot: Bot, user_id: int, parts: list[str]) -> None:
    time_str = parts[2].strip()
    try:
        interval = int(parts[3].strip())
    except ValueError:
        await _reply(bot, user_id, "间隔必须是数字（分钟）")
        return
    if interval < 1:
        await _reply(bot, user_id, "间隔至少为 1 分钟")
        return
    message = parts[4].strip()

    match = re.match(r"^(\d{1,2}):(\d{2})$", time_str)
    if not match:
        await _reply(bot, user_id, "时间格式错误，请使用 HH:MM（例如 18:00、08:30）")
        return
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        await _reply(bot, user_id, "时间范围错误：小时 0-23，分钟 0-59")
        return

    try:
        reminders = load_reminders()
    except (RuntimeError, ValueError):
        reminders = []

    new_rem = normalize_reminder({
        "id": next_reminder_id(reminders),
        "type": "period",
        "hour": hour,
        "minute": minute,
        "repeat_interval": interval,
        "message": message,
        "targets": [],
        "enabled": True,
        "creator_qq": user_id,
    })
    reminders.append(new_rem)
    try:
        save_reminders(reminders)
    except OSError as exc:
        await _reply(bot, user_id, f"保存失败: {exc}")
        return

    _schedule(new_rem)
    await _reply(bot, user_id, f"已添加周期催促提醒\n{_render_reminder(new_rem)}")


@driver.on_startup
async def _on_startup() -> None:
    restore_all()
