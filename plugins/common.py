"""插件共享工具函数。"""

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot
from image_renderer import render_text_to_image


async def reply_image(bot: Bot, user_id: int, text: str, title: str = "Bot") -> None:
    """将文本渲染为样式图片并发送，失败时回退为纯文本。"""
    try:
        b64 = render_text_to_image(text, title=title)
        await bot.call_api("send_msg", message_type="private", user_id=user_id, message=[
            {"type": "image", "data": {"file": b64}},
        ])
    except Exception:
        logger.warning("Failed to render image, falling back to plain text")
        await reply_private(bot, user_id, text)


async def reply_private(bot: Bot, user_id: int, message: str) -> None:
    """发送纯文本私聊消息。"""
    logger.info("Reply private message to %s: %s", user_id, message.replace("\n", " | "))
    await bot.call_api("send_msg", message_type="private", user_id=user_id, message=message)
