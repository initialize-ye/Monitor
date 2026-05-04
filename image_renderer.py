"""Render text to styled PNG images using Pillow, for use as QQ image messages."""

import base64
import io
import os

from PIL import Image, ImageDraw, ImageFont

# --- Design constants ---
CARD_WIDTH = 780
PADDING = 28
TITLE_HEIGHT = 56
LINE_HEIGHT = 34
FONT_SIZE = 17
TITLE_FONT_SIZE = 21
CORNER_RADIUS = 12

COLOR_BG = (255, 255, 255)
COLOR_TITLE_BG = (81, 133, 249)
COLOR_TITLE_TEXT = (255, 255, 255)
COLOR_BODY = (48, 49, 51)
COLOR_ACCENT = (81, 133, 249)
COLOR_SEPARATOR = (230, 232, 235)
COLOR_BORDER = (200, 204, 209)

_HEADER_PREFIXES = ("—", "─", "━")


def _rounded_rectangle(draw: ImageDraw, xy, radius: int, fill=None, outline=None, width=1):
    """Draw a rounded rectangle."""
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


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


def _is_header(line: str) -> bool:
    return any(line.startswith(p) for p in _HEADER_PREFIXES)


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
        w, _ = _measure_text(draw, paragraph, font)
        if w <= max_width:
            lines.append(paragraph)
            continue
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


def _is_separator(line: str) -> bool:
    """Check if a line is purely a separator line (only dashes)."""
    stripped = line.strip()
    if not stripped:
        return False
    return all(c in "─━—－-" for c in stripped)


def render_text_to_image(text: str, title: str = "Bot") -> str:
    """Render text to a styled PNG image, return a 'base64://...' data URI for use as
    a OneBot v11 image message segment.

    The image is a clean rounded card with a colored title bar and wrapped body text.
    Section header lines (starting with —/─/━) are shown in accent color.
    Pure separator lines (all dashes) are rendered as thin horizontal rules.
    """
    font = _get_font(FONT_SIZE)
    title_font = _get_font(TITLE_FONT_SIZE)
    content_width = CARD_WIDTH - PADDING * 2

    # Temporary draw for measurement
    temp_img = Image.new("RGB", (CARD_WIDTH, 1))
    temp_draw = ImageDraw.Draw(temp_img)

    # Wrap body text, tracking which lines are headers/separators
    wrapped: list[tuple[str, str]] = []  # (line, style: "header"|"sep"|"body")
    for line in text.split("\n"):
        if not line:
            wrapped.append(("", "body"))
        elif _is_separator(line):
            wrapped.append(("─" * 50, "sep"))  # normalize to fixed separator
        elif _is_header(line):
            wrapped.append((line, "header"))
        else:
            sub = _wrap_text(temp_draw, line, font, content_width)
            for s in sub:
                wrapped.append((s, "body"))

    # Calculate image height
    body_height = PADDING * 2
    for line_text, style in wrapped:
        if not line_text:
            body_height += LINE_HEIGHT // 2
        elif style == "sep":
            body_height += 20
        else:
            body_height += LINE_HEIGHT
    img_height = TITLE_HEIGHT + body_height + 4  # +4 for bottom border accent

    # Create image with rounded corners via mask
    img = Image.new("RGBA", (CARD_WIDTH, img_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Card background (rounded white rect)
    _rounded_rectangle(draw, (0, 0, CARD_WIDTH - 1, img_height - 1), CORNER_RADIUS, fill=COLOR_BG)
    # Card border (subtle gray)
    _rounded_rectangle(draw, (0, 0, CARD_WIDTH - 1, img_height - 1), CORNER_RADIUS,
                       outline=COLOR_BORDER, width=1)

    # --- Title bar ---
    # Title background (rounded top corners only)
    title_clip = Image.new("RGBA", (CARD_WIDTH, TITLE_HEIGHT + CORNER_RADIUS), (0, 0, 0, 0))
    tc = ImageDraw.Draw(title_clip)
    _rounded_rectangle(tc, (0, 0, CARD_WIDTH - 1, TITLE_HEIGHT + CORNER_RADIUS),
                       CORNER_RADIUS, fill=COLOR_TITLE_BG)
    img.paste(title_clip, (0, 0), title_clip)

    # Title text
    tw, th = _measure_text(draw, title, title_font)
    draw.text(
        ((CARD_WIDTH - tw) // 2, (TITLE_HEIGHT - th) // 2 - 1),
        title,
        fill=COLOR_TITLE_TEXT,
        font=title_font,
    )

    # Thin separator line under title bar
    draw.line(
        [(PADDING, TITLE_HEIGHT), (CARD_WIDTH - PADDING, TITLE_HEIGHT)],
        fill=(255, 255, 255, 80), width=1,
    )

    # --- Body text ---
    y = TITLE_HEIGHT + PADDING
    for line_text, style in wrapped:
        if not line_text:
            y += LINE_HEIGHT // 2
            continue
        if style == "sep":
            # Horizontal rule
            draw.line(
                [(PADDING, y + 10), (CARD_WIDTH - PADDING, y + 10)],
                fill=COLOR_SEPARATOR, width=2,
            )
            y += 20
            continue
        if style == "header":
            draw.text((PADDING, y), line_text, fill=COLOR_ACCENT, font=font)
        else:
            draw.text((PADDING, y), line_text, fill=COLOR_BODY, font=font)
        y += LINE_HEIGHT

    # Bottom accent bar
    draw.line(
        [(PADDING, img_height - 3), (CARD_WIDTH - PADDING, img_height - 3)],
        fill=COLOR_TITLE_BG, width=2,
    )

    # Convert to RGB and save as PNG base64
    out = img.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"base64://{b64}"
