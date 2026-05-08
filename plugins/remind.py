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
    """Render reminder with modern formatting."""
    rem_type = rem.get("type", "daily")
    type_label = REMIND_TYPE_LABELS.get(rem_type, "每天")

    # Build time/schedule label
    if rem_type == "once":
        schedule = f"{rem.get('date', '')} {rem['hour']:02d}:{rem['minute']:02d}"
    elif rem_type == "interval":
        schedule = f"每 {rem['interval_minutes']} 分钟"
    elif rem_type == "period":
        schedule = f"{rem['hour']:02d}:{rem['minute']:02d} 起，每 {rem['repeat_interval']} 分钟"
    else:
        schedule = f"{rem['hour']:02d}:{rem['minute']:02d}"

    # Build content
    if rem.get("auto_generate") == "quote":
        content = "每日一言（自动生成）"
    else:
        content = f"{rem['message']}"

    # Build status
    status = ""
    if rem_type == "period" and rem.get("last_done_date") == date.today().isoformat():
        status = "\n成功: 今日已完成"

    return (
        f"提醒 #{rem['id']}\n"
        f"类型: {type_label}\n"
        f"时间: {schedule}\n"
        f"{content}"
        f"{status}"
    )


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


def _parse_time(time_str: str) -> tuple[int | None, int | None, str | None]:
    match = re.match(r"^(\d{1,2}):(\d{2})$", time_str.strip())
    if not match:
        return None, None, "时间格式错误，请使用 HH:MM（例如 09:00、18:30）"
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None, None, "时间范围错误：小时 0-23，分钟 0-59"
    return hour, minute, None


def _parse_id_list(raw: str) -> tuple[list[int], str | None]:
    parts = [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()]
    if not parts:
        return [], "请输入提醒编号"
    try:
        ids = [int(item) for item in parts]
    except ValueError:
        return [], "编号必须是数字，多个编号用逗号分隔"
    return sorted(set(ids)), None


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


def _should_resume_period_interval(rem: dict) -> bool:
    if rem.get("type") != "period" or not rem.get("enabled", True):
        return False
    if rem.get("last_done_date") == date.today().isoformat():
        return False
    now = datetime.now()
    start = now.replace(hour=int(rem.get("hour", 9)), minute=int(rem.get("minute", 0)), second=0, microsecond=0)
    return now >= start


def _format_schedule_result(new_rem: dict, error: str | None) -> str:
    if error:
        return f"注意: 提醒已保存，但调度失败: {error}\n\n{_render_reminder(new_rem)}"
    return _render_reminder(new_rem)


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

    targets = rem.get("targets", [])
    if not targets:
        targets = [rem["creator_qq"]] if rem.get("creator_qq") else []

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
    text = f"提醒 #{rem_id} - 周期催促\n{rem['message']}\n\n完成后回复: remind done {rem_id}"

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
    restored = 0
    interval_restored = 0
    for rem in reminders:
        rem = normalize_reminder(rem)
        ok, error = _schedule(rem)
        if ok:
            restored += 1
        else:
            logger.error("Failed to restore reminder %s: %s", rem.get("id"), error)
        if _should_resume_period_interval(rem):
            ok, error = _schedule_period_interval(rem["id"], rem["repeat_interval"])
            if ok:
                interval_restored += 1
            else:
                logger.error("Failed to restore period interval %s: %s", rem.get("id"), error)
    if reminders:
        logger.info("Restored %d reminders and %d period intervals", restored, interval_restored)


async def handle_command(bot: Bot, user_id: int, text: str) -> None:
    head_parts = text.split(maxsplit=2)
    sub = head_parts[1].lower() if len(head_parts) > 1 else ""

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
        await _add_daily(bot, user_id, text.split(maxsplit=3))
        return

    if sub == "quote":
        parts = text.split(maxsplit=3)
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
            await _reply(bot, user_id, "日期格式错误，请使用 YYYY-MM-DD")
            return
        hour, minute, error = _parse_time(time_str)
        if error:
            await _reply(bot, user_id, error)
            return
        run_at = datetime.strptime(f"{date_str} {hour:02d}:{minute:02d}", "%Y-%m-%d %H:%M")
        if run_at <= datetime.now():
            await _reply(bot, user_id, "单次提醒时间不能早于当前时间")
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
        ok, error = _schedule(new_rem)
        await _reply_image(
            bot, user_id,
            f"已添加单次提醒\n{_format_schedule_result(new_rem, error if not ok else None)}",
            title=f"提醒 {new_rem['id']}"
        )
        return

    if sub == "workday":
        # remind workday <HH:MM> <内容>
        parts = text.split(maxsplit=3)
        if len(parts) < 4 or not parts[2].strip() or not parts[3].strip():
            await _reply(bot, user_id, "用法: remind workday <HH:MM> <内容>\n例如: remind workday 09:00 上班打卡")
            return
        await _add_timed(bot, user_id, parts, "workday")
        return

    if sub == "interval":
        # remind interval <分钟> <内容>
        parts = text.split(maxsplit=3)
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
        ok, error = _schedule(new_rem)
        await _reply_image(
            bot, user_id,
            f"已添加间隔提醒\n{_format_schedule_result(new_rem, error if not ok else None)}",
            title=f"提醒 {new_rem['id']}"
        )
        return

    if sub == "period":
        # remind period <HH:MM> <间隔分钟> <内容>
        parts = text.split(maxsplit=4)
        if len(parts) < 5 or not parts[2].strip() or not parts[3].strip() or not parts[4].strip():
            await _reply(bot, user_id, "用法: remind period <HH:MM> <间隔分钟> <内容>\n例如: remind period 18:00 10 背单词")
            return
        await _add_period(bot, user_id, parts)
        return

    if sub == "done":
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
        except (RuntimeError, ValueError) as exc:
            await _reply(bot, user_id, f"加载提醒失败: {exc}")
            return

        target = next((r for r in reminders if r["id"] == rem_id), None)
        if not target:
            await _reply(bot, user_id, f"编号 {rem_id} 不存在")
            return
        if target.get("type") != "period":
            await _reply(bot, user_id, "注意: 该提醒不是周期催促类型")
            return

        today = date.today().isoformat()
        if target.get("last_done_date") == today:
            _unschedule_period_interval(rem_id)
            await _reply(bot, user_id, f"成功: 提醒 {rem_id} 今日已完成，无需重复标记")
            return

        target["last_done_date"] = today
        try:
            save_reminders(reminders)
        except OSError as exc:
            await _reply(bot, user_id, f"错误: 保存失败: {exc}")
            return

        _unschedule_period_interval(rem_id)
        await _reply(bot, user_id, f"成功: 已标记提醒 {rem_id} 今日完成，明天继续")
        return

    if sub in {"remove", "del", "delete"}:
        parts = text.split(maxsplit=2)
        if len(parts) < 3 or not parts[2].strip():
            await _reply(bot, user_id, "用法: remind remove <编号[,编号]>\n例如: remind remove 1,3")
            return
        rem_ids, error = _parse_id_list(parts[2].strip())
        if error:
            await _reply(bot, user_id, error)
            return

        try:
            reminders = load_reminders()
        except (RuntimeError, ValueError) as exc:
            await _reply(bot, user_id, f"加载提醒失败: {exc}")
            return

        existing_ids = {r["id"] for r in reminders}
        removed_ids = [rem_id for rem_id in rem_ids if rem_id in existing_ids]
        missing_ids = [rem_id for rem_id in rem_ids if rem_id not in existing_ids]

        if not removed_ids:
            await _reply(bot, user_id, f"编号 {', '.join(map(str, missing_ids))} 不存在")
            return

        reminders = [r for r in reminders if r["id"] not in set(removed_ids)]
        try:
            save_reminders(reminders)
        except OSError as exc:
            await _reply(bot, user_id, f"保存失败: {exc}")
            return

        for rem_id in removed_ids:
            _unschedule(rem_id)
            _unschedule_period_interval(rem_id)

        msg = f"已删除: 已删除 {len(removed_ids)} 个提醒: {', '.join(map(str, removed_ids))}"
        if missing_ids:
            msg += f"\n注意: 编号不存在: {', '.join(map(str, missing_ids))}"
        await _reply(bot, user_id, msg)
        return

    # Shortcut: "remind HH:MM <内容>" → daily reminder without "add"
    if re.match(r"^\d{1,2}:\d{2}$", sub):
        parts = text.split(maxsplit=2)
        if len(parts) < 3 or not parts[2].strip():
            await _reply(bot, user_id, "用法: remind <HH:MM> <内容>\n例如: remind 10:00 背单词")
            return
        await _add_timed(bot, user_id, ["remind", "add", parts[1], parts[2]], "daily")
        return

    await _reply_image(
        bot, user_id,
        "用法:\n"
        "remind <HH:MM> <内容>                            每天提醒\n"
        "remind add HH:MM <内容>                          每天提醒\n"
        "remind once YYYY-MM-DD HH:MM …                  单次提醒\n"
        "remind workday HH:MM <内容>                     工作日提醒\n"
        "remind interval <分钟> <内容>                   间隔提醒\n"
        "remind period HH:MM <分钟> <内容>               周期催促(不做一直催)\n"
        "remind done <编号>                              标记周期催促今日完成\n"
        "remind quote HH:MM                              每日一言(随机名言)\n"
        "remind remove <编号[,编号]>                     删除提醒\n"
        "remind list                                     查看提醒",
        title="提醒命令",
    )


async def _add_daily(bot: Bot, user_id: int, parts: list[str]) -> None:
    if len(parts) < 4 or not parts[2].strip() or not parts[3].strip():
        await _reply(bot, user_id, "用法: remind <HH:MM> <内容>\n例如: remind 10:00 背单词")
        return
    await _add_timed(bot, user_id, parts, "daily")


async def _add_timed(bot: Bot, user_id: int, parts: list[str], rem_type: str) -> None:
    time_str = parts[2].strip()
    message = parts[3].strip()
    hour, minute, error = _parse_time(time_str)
    if error:
        await _reply(bot, user_id, error)
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

    ok, error = _schedule(new_rem)
    await _reply_image(
        bot, user_id,
        f"已添加提醒\n{_format_schedule_result(new_rem, error if not ok else None)}",
        title=f"提醒 {new_rem['id']}"
    )


async def _add_quote(bot: Bot, user_id: int, parts: list[str]) -> None:
    time_str = parts[2].strip()
    hour, minute, error = _parse_time(time_str)
    if error:
        await _reply(bot, user_id, error)
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

    ok, error = _schedule(new_rem)
    await _reply_image(
        bot, user_id,
        f"已添加每日一言\n{_format_schedule_result(new_rem, error if not ok else None)}",
        title=f"提醒 {new_rem['id']}"
    )


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

    hour, minute, error = _parse_time(time_str)
    if error:
        await _reply(bot, user_id, error)
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

    ok, error = _schedule(new_rem)
    await _reply_image(
        bot, user_id,
        f"已添加周期催促提醒\n{_format_schedule_result(new_rem, error if not ok else None)}",
        title=f"提醒 {new_rem['id']}"
    )


@driver.on_startup
async def _on_startup() -> None:
    restore_all()
