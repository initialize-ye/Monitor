"""使用 Pillow 将文本渲染为样式化 PNG 图片，用于 QQ 图片消息。"""

import base64
import io
import os
import re

from PIL import Image, ImageDraw, ImageFont

CARD_WIDTH = 920
PADDING = 42
TITLE_HEIGHT = 76
LINE_HEIGHT = 42
HEADER_HEIGHT = 48
SECTION_SPACING = 18
FONT_SIZE = 19
TITLE_FONT_SIZE = 24
HEADER_FONT_SIZE = 20
COL2_X = 490
CORNER_RADIUS = 18
SHADOW_OFFSET = 3

COLOR_CANVAS = (246, 248, 252)
COLOR_CARD = (255, 255, 255)
COLOR_TITLE_BG = (37, 99, 235)
COLOR_TITLE_TEXT = (255, 255, 255)
COLOR_BODY = (31, 41, 55)
COLOR_MUTED = (107, 114, 128)
COLOR_ACCENT = (37, 99, 235)
COLOR_HEADER_BG = (239, 246, 255)
COLOR_FIELD_BG = (249, 250, 251)
COLOR_BORDER = (226, 232, 240)
COLOR_SEPARATOR = (226, 232, 240)
COLOR_SHADOW = (15, 23, 42, 24)

_TWO_COL_RE = re.compile(r"^(\S.+?)  {3,}(\S.+)$")
_FIELD_RE = re.compile(r"^[\w一-鿿 #()（）]+[:：]")
_NUMBERED_RE = re.compile(r"^\s*\d+[.)、]")
_HEADER_HINTS = (
    "关键词监控", "定时提醒", "高级管理", "提示", "今日统计", "群组信息", "关键词列表",
    "快捷操作菜单", "提醒", "提醒列表", "规则列表", "帮助", "用法",
)

_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
]

_font_cache: dict[int, ImageFont.FreeTypeFont] = {}


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    if size in _font_cache:
        return _font_cache[size]
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size)
                _font_cache[size] = font
                return font
            except Exception:
                continue
    font = ImageFont.load_default()
    _font_cache[size] = font
    return font


def _measure(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        if _measure(draw, paragraph, font)[0] <= max_width:
            lines.append(paragraph)
            continue
        # Binary search for break point instead of per-character measurement
        remaining = paragraph
        while remaining:
            if _measure(draw, remaining, font)[0] <= max_width:
                lines.append(remaining)
                break
            lo, hi = 1, len(remaining)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if _measure(draw, remaining[:mid], font)[0] <= max_width:
                    lo = mid
                else:
                    hi = mid - 1
            lines.append(remaining[:lo])
            remaining = remaining[lo:]
    return lines


def _is_separator(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and all(c in "-_=─━—－" for c in stripped)


def _is_header(line: str) -> bool:
    stripped = line.strip()
    if not stripped or _FIELD_RE.match(stripped) or _NUMBERED_RE.match(stripped):
        return False
    if stripped in _HEADER_HINTS:
        return True
    return any(stripped.startswith(hint) for hint in _HEADER_HINTS)


def _row_style(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return "blank"
    if _is_separator(stripped):
        return "sep"
    if _is_header(stripped):
        return "header"
    if _NUMBERED_RE.match(stripped):
        return "item"
    if _FIELD_RE.match(stripped):
        return "field"
    return "body"


def _split_field(text: str) -> tuple[str, str] | None:
    for sep in (":", "："):
        if sep in text:
            key, value = text.split(sep, 1)
            if key.strip() and value.strip():
                return key.strip() + sep, value.strip()
    return None


def _text_y(y: int, row_height: int, font_size: int) -> int:
    return y + (row_height - font_size) // 2 - 2


def render_text_to_image(text: str, title: str = "Bot") -> str:
    """将样式文本渲染为 PNG 图片，返回 base64 data URI。"""
    font = _get_font(FONT_SIZE)
    header_font = _get_font(HEADER_FONT_SIZE)
    title_font = _get_font(TITLE_FONT_SIZE)
    content_width = CARD_WIDTH - PADDING * 2

    temp_img = Image.new("RGB", (CARD_WIDTH, 1))
    tmp = ImageDraw.Draw(temp_img)

    entries: list[tuple[str, str, str | None]] = []
    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        style = _row_style(line)
        if style in {"blank", "sep", "header", "field", "item"}:
            entries.append((line, style, None))
            continue
        if m := _TWO_COL_RE.match(line):
            col1, col2 = m.group(1), m.group(2)
            if PADDING + _measure(tmp, col1, font)[0] + 56 <= COL2_X:
                entries.append((col1, "col2", col2))
                continue
        for wrapped in _wrap_text(tmp, line, font, content_width):
            entries.append((wrapped, "body", None))

    body_height = PADDING * 2
    prev_style = None
    for ent_text, style, _ in entries:
        if style == "blank":
            body_height += 18
        elif style == "sep":
            body_height += 24
        elif style == "header":
            if prev_style not in (None, "blank", "sep"):
                body_height += SECTION_SPACING
            body_height += HEADER_HEIGHT
        elif style == "field":
            body_height += 40
        else:
            body_height += LINE_HEIGHT
        prev_style = style

    img_height = TITLE_HEIGHT + body_height
    canvas = Image.new("RGBA", (CARD_WIDTH + SHADOW_OFFSET * 2, img_height + SHADOW_OFFSET * 2), COLOR_CANVAS)
    img = Image.new("RGBA", (CARD_WIDTH, img_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle((SHADOW_OFFSET, SHADOW_OFFSET, CARD_WIDTH - 1, img_height - 1), radius=CORNER_RADIUS, fill=COLOR_SHADOW)
    draw.rounded_rectangle((0, 0, CARD_WIDTH - SHADOW_OFFSET - 1, img_height - SHADOW_OFFSET - 1), radius=CORNER_RADIUS, fill=COLOR_CARD)

    title_bottom = TITLE_HEIGHT
    draw.rounded_rectangle((0, 0, CARD_WIDTH - SHADOW_OFFSET - 1, title_bottom + CORNER_RADIUS), radius=CORNER_RADIUS, fill=COLOR_TITLE_BG)
    draw.rectangle((0, title_bottom - CORNER_RADIUS, CARD_WIDTH - SHADOW_OFFSET - 1, title_bottom), fill=COLOR_TITLE_BG)
    draw.text((PADDING, _text_y(0, TITLE_HEIGHT, TITLE_FONT_SIZE)), title, fill=COLOR_TITLE_TEXT, font=title_font)

    y = TITLE_HEIGHT + PADDING
    prev_style = None
    for ent_text, style, col2 in entries:
        if style == "blank":
            y += 18
            prev_style = style
            continue

        if style == "sep":
            draw.line((PADDING, y + 12, CARD_WIDTH - PADDING, y + 12), fill=COLOR_SEPARATOR, width=1)
            y += 24
            prev_style = style
            continue

        if style == "header":
            if prev_style not in (None, "blank", "sep"):
                y += SECTION_SPACING
            draw.rounded_rectangle((PADDING - 8, y, CARD_WIDTH - PADDING + 8, y + HEADER_HEIGHT), radius=12, fill=COLOR_HEADER_BG)
            draw.rectangle((PADDING - 8, y + 10, PADDING - 4, y + HEADER_HEIGHT - 10), fill=COLOR_ACCENT)
            draw.text((PADDING + 10, _text_y(y, HEADER_HEIGHT, HEADER_FONT_SIZE)), ent_text.strip(), fill=COLOR_ACCENT, font=header_font)
            y += HEADER_HEIGHT
            prev_style = style
            continue

        if style == "field":
            field = _split_field(ent_text.strip())
            if field:
                key, value = field
                draw.rounded_rectangle((PADDING - 6, y + 2, CARD_WIDTH - PADDING + 6, y + 38), radius=8, fill=COLOR_FIELD_BG)
                draw.text((PADDING + 4, _text_y(y, 40, FONT_SIZE)), key, fill=COLOR_MUTED, font=font)
                key_width = _measure(draw, key, font)[0]
                draw.text((PADDING + key_width + 16, _text_y(y, 40, FONT_SIZE)), value, fill=COLOR_BODY, font=font)
            else:
                draw.text((PADDING, y), ent_text, fill=COLOR_BODY, font=font)
            y += 40
            prev_style = style
            continue

        if style == "item":
            draw.text((PADDING + 8, y), ent_text.strip(), fill=COLOR_BODY, font=font)
            y += LINE_HEIGHT
            prev_style = style
            continue

        if style == "col2":
            draw.text((PADDING, y), ent_text, fill=COLOR_BODY, font=font)
            if col2:
                draw.text((COL2_X, y), col2, fill=COLOR_MUTED, font=font)
        else:
            draw.text((PADDING, y), ent_text, fill=COLOR_BODY, font=font)
        y += LINE_HEIGHT
        prev_style = style

    canvas.alpha_composite(img, (0, 0))
    out = canvas.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG", quality=95, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"base64://{b64}"
