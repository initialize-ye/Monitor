"""Fetch a random quote from the hitokoto API, with a local fallback."""

import random

_FALLBACK = [
    "千里之行，始于足下。",
    "不积跬步，无以至千里。",
    "天行健，君子以自强不息。",
    "有志者，事竟成。",
    "宝剑锋从磨砺出，梅花香自苦寒来。",
    "世上无难事，只怕有心人。",
    "功夫不负有心人。",
    "坚持就是胜利。",
    "机会永远留给有准备的人。",
    "长风破浪会有时，直挂云帆济沧海。",
    "星光不问赶路人，时光不负有心人。",
    "路漫漫其修远兮，吾将上下而求索。",
    "知不足而奋进，望远山而前行。",
    "少壮不努力，老大徒伤悲。",
    "盛年不重来，一日难再晨。",
    "及时当勉励，岁月不待人。",
    "博观而约取，厚积而薄发。",
    "天将降大任于斯人也，必先苦其心志。",
    "不经一番寒彻骨，怎得梅花扑鼻香。",
    "沉舟侧畔千帆过，病树前头万木春。",
]


def _random_fallback() -> str:
    return random.choice(_FALLBACK)


async def random_quote() -> str:
    """Fetch a random quote from hitokoto API.

    Returns a formatted string like: 内容 —— 来源
    Falls back to a local list on failure.
    """
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get("https://v1.hitokoto.cn/")
            resp.raise_for_status()
            data = resp.json()
            text = data.get("hitokoto", "")
            source = data.get("from", "") or data.get("from_who", "")
            if text:
                return f"{text} —— {source}" if source else text
    except Exception:
        pass

    return _random_fallback()
