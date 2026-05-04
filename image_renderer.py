"""Render text to styled PNG images using Pillow, for use as QQ image messages."""

import base64
import io
import os

from PIL import Image, ImageDraw, ImageFont

# --- Design constants ---
CARD_WIDTH = 780
PADDING = 24
TITLE_HEIGHT = 52
LINE_HEIGHT = 30
PARA_GAP = 10
FONT_SIZE = 16
TITLE_FONT_SIZE = 19

COLOR_BG = (255, 255, 255)
COLOR_TITLE_BG = (81, 133, 249)
COLOR_TITLE_TEXT = (255, 255, 255)
COLOR_BODY = (48, 49, 51)
COLOR_ACCENT = (81, 133, 249)
COLOR_MUTED = (153, 153, 153)
COLOR_BORDER = (230, 230, 230)

# Common Chinese font paths across platforms
_FONT_CANDIDATES = [
    # Linux
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    # Windows
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
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


def _measure_text(draw: ImageDraw, text: str, font: ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _wrap_text(draw: ImageDraw, text: str, font: ImageFont, max_width: int) -> list[str]:
    """Wrap text to fit within max_width pixels, preserving original line breaks."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        # If the whole paragraph fits in one line
        w, _ = _measure_text(draw, paragraph, font)
        if w <= max_width:
            lines.append(paragraph)
            continue
        # Character-by-character wrapping for CJK text
        current = ""
        for char in paragraph:
            test = current + char
            w, _ = _measure_text(draw, test, font)
            if w <= max_width:
                current = test
            else:
                lines.append(current)
                current = char
        if current:
            lines.append(current)
    return lines


def render_text_to_image(text: str, title: str = "Bot") -> str:
    """Render text to a styled PNG image, return a 'base64://...' data URI for use as
    a OneBot v11 image message segment.

    The image is a clean card with a colored title bar and wrapped body text.
    Lines starting with '—' are displayed as section headers in accent color.
    """
    font = _get_font(FONT_SIZE)
    title_font = _get_font(TITLE_FONT_SIZE)
    content_width = CARD_WIDTH - PADDING * 2

    # Create a temporary draw context for measurement
    temp_img = Image.new("RGB", (CARD_WIDTH, 1))
    temp_draw = ImageDraw.Draw(temp_img)

    # Wrap body text
    wrapped_lines: list[str] = []
    for line in text.split("\n"):
        if not line:
            wrapped_lines.append("")
        elif line.startswith("—"):
            wrapped_lines.append(line)
        else:
            sub = _wrap_text(temp_draw, line, font, content_width)
            wrapped_lines.extend(sub)

    # Calculate image height
    body_height = PADDING * 2
    for line in wrapped_lines:
        if line == "":
            body_height += LINE_HEIGHT // 2
        else:
            body_height += LINE_HEIGHT
    img_height = TITLE_HEIGHT + body_height

    # Create actual image
    img = Image.new("RGB", (CARD_WIDTH, img_height), COLOR_BG)
    draw = ImageDraw.Draw(img)

    # --- Title bar ---
    draw.rectangle(
        [(0, 0), (CARD_WIDTH, TITLE_HEIGHT)],
        fill=COLOR_TITLE_BG,
    )
    tw, _ = _measure_text(draw, title, title_font)
    draw.text(
        ((CARD_WIDTH - tw) // 2, (TITLE_HEIGHT - LINE_HEIGHT) // 2 - 2),
        title,
        fill=COLOR_TITLE_TEXT,
        font=title_font,
    )

    # --- Body text ---
    y = TITLE_HEIGHT + PADDING
    for line in wrapped_lines:
        if line == "":
            y += LINE_HEIGHT // 2
            continue
        if line.startswith("—"):
            # Section header
            draw.text((PADDING, y), line, fill=COLOR_ACCENT, font=font)
        else:
            draw.text((PADDING, y), line, fill=COLOR_BODY, font=font)
        y += LINE_HEIGHT

    # Convert to PNG base64
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"base64://{b64}"
