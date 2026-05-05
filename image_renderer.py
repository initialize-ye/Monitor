"""Render text to styled PNG images using Pillow, for use as QQ image messages."""

import base64
import io
import os
import re

from PIL import Image, ImageDraw, ImageFont

# --- Design constants ---
CARD_WIDTH = 900
PADDING = 32
TITLE_HEIGHT = 64
LINE_HEIGHT = 40
HEADER_LINE_HEIGHT = 38
SECTION_SPACING = 16  # Extra spacing between sections
FONT_SIZE = 18
TITLE_FONT_SIZE = 22
HEADER_FONT_SIZE = 19
COL2_X = 500
CORNER_RADIUS = 16

# Modern color palette with better contrast
COLOR_BG = (255, 255, 255)
COLOR_TITLE_BG = (59, 130, 246)  # Modern blue
COLOR_TITLE_TEXT = (255, 255, 255)
COLOR_BODY = (31, 41, 55)  # Darker for better readability
COLOR_ACCENT = (59, 130, 246)
COLOR_MUTED = (107, 114, 128)  # Better contrast
COLOR_SEPARATOR = (229, 231, 235)
COLOR_BORDER = (209, 213, 219)
COLOR_LIGHT_BG = (249, 250, 251)  # Light background for sections

_HEADER_PREFIXES = ("—", "─", "━")
_TWO_COL_RE = re.compile(r"^(\S.+?)  {3,}(\S.+)$")


# Common Chinese font paths across platforms
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
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


def _measure(draw: ImageDraw, text: str, font: ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _wrap_text(draw: ImageDraw, text: str, font: ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        w, _ = _measure(draw, paragraph, font)
        if w <= max_width:
            lines.append(paragraph)
            continue
        current = ""
        for char in paragraph:
            test = current + char
            w, _ = _measure(draw, test, font)
            if w <= max_width:
                current = test
            else:
                lines.append(current)
                current = char
        if current:
            lines.append(current)
    return lines


def _is_header(line: str) -> bool:
    return any(line.startswith(p) for p in _HEADER_PREFIXES)


def _is_separator(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return all(c in "─━—－-" for c in stripped)


def render_text_to_image(text: str, title: str = "Bot") -> str:
    """Render text to a styled PNG image, return a 'base64://...' data URI.

    Features:
    - Rounded card with colored title bar
    - Two-column detection: lines with 3+ spaces between words are split into
      command (left) and description (right) columns for proportional fonts
    - Section headers (starting with —/─/━) in accent color with background
    - Separator lines (all dashes) as thin horizontal rules
    - Improved spacing and visual hierarchy
    """
    font = _get_font(FONT_SIZE)
    header_font = _get_font(HEADER_FONT_SIZE)
    title_font = _get_font(TITLE_FONT_SIZE)
    content_width = CARD_WIDTH - PADDING * 2

    # Temporary draw for measurement
    temp_img = Image.new("RGB", (CARD_WIDTH, 1))
    tmp = ImageDraw.Draw(temp_img)

    # Parse lines with their rendering style
    entries: list[tuple[str, str, str | None]] = []
    # (text, style, col2_text) where style is "header"/"sep"/"body"/"col2"

    for line in text.split("\n"):
        if not line:
            entries.append(("", "body", None))
        elif _is_separator(line):
            entries.append(("─" * 50, "sep", None))
        elif _is_header(line):
            entries.append((line, "header", None))
        elif m := _TWO_COL_RE.match(line):
            col1, col2 = m.group(1), m.group(2)
            w1, _ = _measure(tmp, col1, font)
            # Check if col1 fits before COL2_X with margin
            if PADDING + w1 + 40 <= COL2_X:
                entries.append((col1, "col2", col2))
            else:
                sub = _wrap_text(tmp, line, font, content_width)
                for s in sub:
                    entries.append((s, "body", None))
        else:
            sub = _wrap_text(tmp, line, font, content_width)
            for s in sub:
                entries.append((s, "body", None))

    # Calculate image height with improved spacing
    body_height = PADDING * 2
    prev_style = None
    for ent_text, style, _ in entries:
        # Add extra spacing between sections
        if prev_style == "header" and style != "header":
            body_height += SECTION_SPACING // 2
        elif style == "header" and prev_style not in (None, "header"):
            body_height += SECTION_SPACING

        if not ent_text:
            body_height += LINE_HEIGHT // 2
        elif style == "sep":
            body_height += 32
        elif style == "header":
            body_height += HEADER_LINE_HEIGHT + 8  # Extra padding for header
        else:
            body_height += LINE_HEIGHT

        prev_style = style

    img_height = TITLE_HEIGHT + body_height + 8

    # Create image
    img = Image.new("RGBA", (CARD_WIDTH, img_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Card background with shadow effect
    draw.rounded_rectangle(
        (0, 0, CARD_WIDTH - 1, img_height - 1),
        radius=CORNER_RADIUS, fill=COLOR_BG,
    )
    draw.rounded_rectangle(
        (0, 0, CARD_WIDTH - 1, img_height - 1),
        radius=CORNER_RADIUS, outline=COLOR_BORDER, width=2,
    )

    # --- Title bar (rounded top) ---
    draw.rounded_rectangle(
        (0, 0, CARD_WIDTH - 1, TITLE_HEIGHT + CORNER_RADIUS),
        radius=CORNER_RADIUS, fill=COLOR_TITLE_BG,
    )

    # Accent dot + title text (left-aligned)
    dot_size = 10
    dot_y = TITLE_HEIGHT // 2 - dot_size // 2
    draw.ellipse(
        (PADDING, dot_y, PADDING + dot_size, dot_y + dot_size),
        fill=COLOR_TITLE_TEXT,
    )
    draw.text(
        (PADDING + 22, (TITLE_HEIGHT - TITLE_FONT_SIZE) // 2 - 2),
        title, fill=COLOR_TITLE_TEXT, font=title_font,
    )

    # Subtle separator line under title bar
    draw.line(
        [(PADDING, TITLE_HEIGHT), (CARD_WIDTH - PADDING, TITLE_HEIGHT)],
        fill=(255, 255, 255, 80), width=1,
    )

    # --- Body ---
    y = TITLE_HEIGHT + PADDING
    prev_style = None

    for ent_text, style, col2 in entries:
        # Add extra spacing between sections
        if prev_style == "header" and style != "header":
            y += SECTION_SPACING // 2
        elif style == "header" and prev_style not in (None, "header"):
            y += SECTION_SPACING

        if not ent_text:
            y += LINE_HEIGHT // 2
            prev_style = style
            continue

        if style == "sep":
            draw.line(
                [(PADDING, y + 16), (CARD_WIDTH - PADDING, y + 16)],
                fill=COLOR_SEPARATOR, width=2,
            )
            y += 32
            prev_style = style
            continue

        if style == "header":
            # Header with light background
            header_bg_height = HEADER_LINE_HEIGHT + 8
            draw.rounded_rectangle(
                [(PADDING - 8, y), (CARD_WIDTH - PADDING + 8, y + header_bg_height)],
                radius=8, fill=COLOR_LIGHT_BG,
            )

            # Colored left bar
            draw.rounded_rectangle(
                [(PADDING, y + 4), (PADDING + 4, y + header_bg_height - 4)],
                radius=2, fill=COLOR_ACCENT,
            )

            # Header text
            draw.text(
                (PADDING + 16, y + 4), ent_text,
                fill=COLOR_ACCENT, font=header_font,
            )
            y += header_bg_height
            prev_style = style
            continue

        if style == "col2":
            # Two-column layout
            draw.text((PADDING, y), ent_text, fill=COLOR_BODY, font=font)
            if col2:
                draw.text((COL2_X, y), col2, fill=COLOR_MUTED, font=font)
            y += LINE_HEIGHT
        else:
            # Regular body text
            draw.text((PADDING, y), ent_text, fill=COLOR_BODY, font=font)
            y += LINE_HEIGHT

        prev_style = style

    # Bottom accent bar with rounded corners
    bar_y = img_height - 6
    draw.rounded_rectangle(
        [(PADDING, bar_y), (CARD_WIDTH - PADDING, bar_y + 3)],
        radius=2, fill=COLOR_TITLE_BG,
    )

    out = img.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG", quality=95, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"base64://{b64}"
