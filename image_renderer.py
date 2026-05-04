"""Render text to styled PNG images using Pillow, for use as QQ image messages."""

import base64
import io
import os
import re

from PIL import Image, ImageDraw, ImageFont

# --- Design constants ---
CARD_WIDTH = 860
PADDING = 28
TITLE_HEIGHT = 56
LINE_HEIGHT = 34
HEADER_LINE_HEIGHT = 30
FONT_SIZE = 17
TITLE_FONT_SIZE = 20
HEADER_FONT_SIZE = 18
COL2_X = 320  # X position for second column (command descriptions)
CORNER_RADIUS = 14

COLOR_BG = (255, 255, 255)
COLOR_TITLE_BG = (81, 133, 249)
COLOR_TITLE_TEXT = (255, 255, 255)
COLOR_BODY = (48, 49, 51)
COLOR_ACCENT = (81, 133, 249)
COLOR_MUTED = (140, 145, 150)
COLOR_SEPARATOR = (230, 232, 235)
COLOR_BORDER = (200, 204, 209)

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
    - Section headers (starting with —/─/━) in accent color
    - Separator lines (all dashes) as thin horizontal rules
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
            if w1 < COL2_X - PADDING - 20:
                entries.append((col1, "col2", col2))
            else:
                sub = _wrap_text(tmp, line, font, content_width)
                for s in sub:
                    entries.append((s, "body", None))
        else:
            sub = _wrap_text(tmp, line, font, content_width)
            for s in sub:
                entries.append((s, "body", None))

    # Calculate image height
    body_height = PADDING * 2
    for ent_text, style, _ in entries:
        if not ent_text:
            body_height += LINE_HEIGHT // 2
        elif style == "sep":
            body_height += 24
        elif style == "header":
            body_height += HEADER_LINE_HEIGHT
        else:
            body_height += LINE_HEIGHT
    img_height = TITLE_HEIGHT + body_height + 4

    # Create image
    img = Image.new("RGBA", (CARD_WIDTH, img_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Card background
    draw.rounded_rectangle(
        (0, 0, CARD_WIDTH - 1, img_height - 1),
        radius=CORNER_RADIUS, fill=COLOR_BG,
    )
    draw.rounded_rectangle(
        (0, 0, CARD_WIDTH - 1, img_height - 1),
        radius=CORNER_RADIUS, outline=COLOR_BORDER, width=1,
    )

    # --- Title bar (rounded top) ---
    draw.rounded_rectangle(
        (0, 0, CARD_WIDTH - 1, TITLE_HEIGHT + CORNER_RADIUS),
        radius=CORNER_RADIUS, fill=COLOR_TITLE_BG,
    )

    # Accent dot + title text (left-aligned)
    draw.ellipse(
        (PADDING, TITLE_HEIGHT // 2 - 4, PADDING + 8, TITLE_HEIGHT // 2 + 4),
        fill=COLOR_TITLE_TEXT,
    )
    draw.text(
        (PADDING + 18, (TITLE_HEIGHT - FONT_SIZE) // 2 - 1),
        title, fill=COLOR_TITLE_TEXT, font=title_font,
    )

    # Separator line under title bar
    draw.line(
        [(PADDING, TITLE_HEIGHT), (CARD_WIDTH - PADDING, TITLE_HEIGHT)],
        fill=(255, 255, 255, 100), width=1,
    )

    # --- Body ---
    y = TITLE_HEIGHT + PADDING
    for ent_text, style, col2 in entries:
        if not ent_text:
            y += LINE_HEIGHT // 2
            continue
        if style == "sep":
            draw.line(
                [(PADDING, y + 12), (CARD_WIDTH - PADDING, y + 12)],
                fill=COLOR_SEPARATOR, width=2,
            )
            y += 24
            continue
        if style == "header":
            # Colored left bar + header text
            draw.rectangle(
                [(PADDING, y + 3), (PADDING + 3, y + HEADER_LINE_HEIGHT - 3)],
                fill=COLOR_ACCENT,
            )
            draw.text(
                (PADDING + 12, y + 1), ent_text,
                fill=COLOR_ACCENT, font=header_font,
            )
            y += HEADER_LINE_HEIGHT
        elif style == "col2":
            draw.text((PADDING, y), ent_text, fill=COLOR_BODY, font=font)
            if col2:
                cw, _ = _measure(draw, col2, font)
                draw.text((COL2_X - cw, y), col2, fill=COLOR_MUTED, font=font)
            y += LINE_HEIGHT
        else:
            draw.text((PADDING, y), ent_text, fill=COLOR_BODY, font=font)
            y += LINE_HEIGHT

    # Bottom accent bar
    draw.line(
        [(PADDING, img_height - 3), (CARD_WIDTH - PADDING, img_height - 3)],
        fill=COLOR_TITLE_BG, width=2,
    )

    out = img.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"base64://{b64}"
