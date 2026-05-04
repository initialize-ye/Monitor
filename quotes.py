"""Fetch a random quote from the hitokoto API, with a local fallback."""

import random

import httpx

_FALLBACK = [
    "千里之行，始于足下。",
    "不积跬步，无以至千里。",
    "天行健，君子以自强不息。",
    "有志者，事竟成。",
    "业精于勤，荒于嬉。",
    "宝剑锋从磨砺出，梅花香自苦寒来。",
    "书山有路勤为径，学海无涯苦作舟。",
    "世上无难事，只怕有心人。",
    "一分耕耘，一分收获。",
    "功夫不负有心人。",
    "不经历风雨，怎么见彩虹。",
    "坚持就是胜利。",
    "机会永远留给有准备的人。",
    "今天的努力是明天的基石。",
    "不要等待机会，而要创造机会。",
    "成功是99%的汗水加1%的灵感。",
    "山重水复疑无路，柳暗花明又一村。",
    "长风破浪会有时，直挂云帆济沧海。",
    "会当凌绝顶，一览众山小。",
    "人生没有白走的路，每一步都算数。",
    "时间是最公平的，给每个人都是24小时。",
    "不要因为走得慢而放弃，慢点没关系，就怕停下。",
    "每一个优秀的人都有一段沉默的时光。",
    "星光不问赶路人，时光不负有心人。",
    "人生能有几回搏，此时不搏何时搏。",
    "态度决定高度，努力决定成就。",
    "只要路是对的，就不怕路远。",
    "天将降大任于斯人也，必先苦其心志。",
    "不经一番寒彻骨，怎得梅花扑鼻香。",
    "路漫漫其修远兮，吾将上下而求索。",
    "知不足而奋进，望远山而前行。",
    "纸上得来终觉浅，绝知此事要躬行。",
    "少壮不努力，老大徒伤悲。",
    "明日复明日，明日何其多。",
    "我生待明日，万事成蹉跎。",
    "盛年不重来，一日难再晨。",
    "及时当勉励，岁月不待人。",
    "莫等闲，白了少年头，空悲切。",
    "老当益壮，宁移白首之心？",
    "穷且益坚，不坠青云之志。",
    "博观而约取，厚积而薄发。",
    "学而不思则罔，思而不学则殆。",
    "温故而知新，可以为师矣。",
    "三人行，必有我师焉。",
    "择其善者而从之，其不善者而改之。",
    "知己知彼，百战不殆。",
    "人非生而知之者，孰能无惑？",
    "千淘万漉虽辛苦，吹尽狂沙始到金。",
    "沉舟侧畔千帆过，病树前头万木春。",
    "谁道人生无再少？门前流水尚能西。",
]


async def random_quote() -> str:
    """Fetch a random quote from hitokoto API.

    Returns a formatted string like: 内容 —— 来源
    Falls back to a local list on failure.
    """
    try:
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

    return random.choice(_FALLBACK)
