import os
import re
from nonebot import logger, get_driver
from nonebot.adapters.onebot.v11 import Bot

from reminders import load_reminders, save_reminders, normalize_reminder, next_reminder_id

TZ = os.getenv("TZ", "Asia/Shanghai")
driver = get_driver()

_scheduled_jobs: dict[int, str] = {}


def _render_reminder(rem: dict) -> str:
    return (
        f"编号: {rem['id']}\n"
        f"时间: {rem['hour']:02d}:{rem['minute']:02d} 每天\n"
        f"内容: {rem['message']}"
    )


async def _reply(bot: Bot, user_id: int, message: str) -> None:
    await bot.call_api("send_msg", message_type="private", user_id=user_id, message=message)


def _schedule(rem: dict) -> None:
    if not rem["enabled"]:
        return
    try:
        from nonebot_plugin_apscheduler import scheduler
        job_id = f"remind_{rem['id']}"
        scheduler.add_job(
            _fire,
            "cron",
            hour=rem["hour"],
            minute=rem["minute"],
            id=job_id,
            replace_existing=True,
            args=[rem["id"]],
            timezone=TZ,
        )
        _scheduled_jobs[rem["id"]] = job_id
        logger.info("Scheduled reminder %s at %02d:%02d %s", rem["id"], rem["hour"], rem["minute"], TZ)
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


async def _fire(rem_id: int) -> None:
    try:
        reminders = load_reminders()
    except (RuntimeError, ValueError):
        logger.error("Failed to load reminders for firing")
        return

    rem = next((r for r in reminders if r["id"] == rem_id), None)
    if not rem or not rem["enabled"]:
        return

    bots = list(get_driver().bots.values())
    if not bots:
        logger.warning("No bot available to send reminder %s", rem_id)
        return

    bot = bots[0]
    text = f"[提醒] {rem['message']}"
    targets = rem.get("targets", [])
    if not targets:
        targets = [rem["creator_qq"]] if rem.get("creator_qq") else []

    for target_qq in targets:
        try:
            await bot.call_api("send_msg", message_type="private", user_id=target_qq, message=text)
        except Exception as exc:
            logger.error("Failed to send reminder %s to %s: %s", rem_id, target_qq, exc)

    logger.info("Fired reminder %s: %s", rem_id, rem["message"])


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
    parts = text.split(maxsplit=3)
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
        await _reply(bot, user_id, "\n\n".join(_render_reminder(r) for r in reminders))
        return

    if sub == "add":
        if len(parts) < 4 or not parts[2].strip() or not parts[3].strip():
            await _reply(bot, user_id, "用法: remind add <HH:MM> <内容>\n例如: remind add 10:00 背单词")
            return
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
        await _reply(bot, user_id, f"已删除提醒 {rem_id}")
        return

    await _reply(bot, user_id, "用法: remind add/remove/list")


@driver.on_startup
async def _on_startup() -> None:
    restore_all()
