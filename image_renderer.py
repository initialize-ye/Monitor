"""Render text to styled PNG images using Pillow, for use as QQ image messages."""

import base64
import io
import os
import re

from PIL import Image, ImageDraw, ImageFont

# --- Design constants ---
CARD_WIDTH = 920
PADDING = 40
TITLE_HEIGHT = 72
LINE_HEIGHT = 44
HEADER_LINE_HEIGHT = 42
SECTION_SPACING = 20
FONT_SIZE = 19
TITLE_FONT_SIZE = 24
HEADER_FONT_SIZE = 20
SMALL_FONT_SIZE = 16
COL2_X = 480
CORNER_RADIUS = 18
SHADOW_OFFSET = 4

# Modern color palette - inspired by Tailwind CSS
COLOR_BG = (255, 255, 255)
COLOR_TITLE_BG = (99, 102, 241)  # Indigo-500
COLOR_TITLE_TEXT = (255, 255, 255)
COLOR_BODY = (17, 24, 39)  # Gray-900
COLOR_ACCENT = (99, 102, 241)  # Indigo-500
COLOR_SUCCESS = (34, 197, 94)  # Green-500
COLOR_WARNING = (251, 146, 60)  # Orange-400
COLOR_MUTED = (107, 114, 128)  # Gray-500
COLOR_SEPARATOR = (229, 231, 235)  # Gray-200
COLOR_BORDER = (229, 231, 235)  # Gray-200
COLOR_LIGHT_BG = (249, 250, 251)  # Gray-50
COLOR_CARD_BG = (248, 250, 252)  # Slate-50
COLOR_SHADOW = (0, 0, 0, 15)  # Subtle shadow

_HEADER_PREFIXES = ("—", "─", "━", "▸", "►", "●")
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
    """Render text to a modern styled PNG image, return a 'base64://...' data URI.

    Features:
    - Modern card design with subtle shadow
    - Gradient title bar with icon
    - Two-column detection for command lists
    - Section headers with colored indicators
    - Improved spacing and visual hierarchy
    - Status badges and icons
    """
    font = _get_font(FONT_SIZE)
    header_font = _get_font(HEADER_FONT_SIZE)
    title_font = _get_font(TITLE_FONT_SIZE)
    small_font = _get_font(SMALL_FONT_SIZE)
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
            if PADDING + w1 + 60 <= COL2_X:
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
            body_height += 36
        elif style == "header":
            body_height += HEADER_LINE_HEIGHT + 12
        else:
            body_height += LINE_HEIGHT

        prev_style = style

    img_height = TITLE_HEIGHT + body_height + 12

    # Create image with shadow space
    canvas = Image.new("RGBA", (CARD_WIDTH + SHADOW_OFFSET * 2, img_height + SHADOW_OFFSET * 2), (0, 0, 0, 0))

    # Draw subtle shadow
    shadow = Image.new("RGBA", (CARD_WIDTH, img_height), COLOR_SHADOW)
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle(
        (0, 0, CARD_WIDTH - 1, img_height - 1),
        radius=CORNER_RADIUS, fill=COLOR_SHADOW
    )
    canvas.paste(shadow, (SHADOW_OFFSET, SHADOW_OFFSET), shadow)

    # Create main card
    img = Image.new("RGBA", (CARD_WIDTH, img_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Card background
    draw.rounded_rectangle(
        (0, 0, CARD_WIDTH - 1, img_height - 1),
        radius=CORNER_RADIUS, fill=COLOR_BG,
    )

    # --- Title bar with gradient effect ---
    # Main title background
    draw.rounded_rectangle(
        (0, 0, CARD_WIDTH - 1, TITLE_HEIGHT + CORNER_RADIUS),
        radius=CORNER_RADIUS, fill=COLOR_TITLE_BG,
    )

    # Subtle gradient overlay (lighter at top)
    for i in range(TITLE_HEIGHT // 2):
        alpha = int(30 * (1 - i / (TITLE_HEIGHT // 2)))
        overlay_color = (255, 255, 255, alpha)
        draw.line([(0, i), (CARD_WIDTH, i)], fill=overlay_color, width=1)

    # Icon circle + title text
    icon_size = 32
    icon_x = PADDING
    icon_y = (TITLE_HEIGHT - icon_size) // 2

    # Icon background circle
    draw.ellipse(
        (icon_x, icon_y, icon_x + icon_size, icon_y + icon_size),
        fill=(255, 255, 255, 40)
    )

    # Icon inner circle
    inner_size = 16
    inner_x = icon_x + (icon_size - inner_size) // 2
    inner_y = icon_y + (icon_size - inner_size) // 2
    draw.ellipse(
        (inner_x, inner_y, inner_x + inner_size, inner_y + inner_size),
        fill=COLOR_TITLE_TEXT
    )

    # Title text
    title_x = icon_x + icon_size + 16
    title_y = (TITLE_HEIGHT - TITLE_FONT_SIZE) // 2 - 2
    draw.text(
        (title_x, title_y),
        title, fill=COLOR_TITLE_TEXT, font=title_font,
    )

    # Subtle separator line under title
    draw.line(
        [(0, TITLE_HEIGHT), (CARD_WIDTH, TITLE_HEIGHT)],
        fill=(255, 255, 255, 60), width=2,
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
            # Modern separator with gradient
            sep_y = y + 18
            draw.line(
                [(PADDING, sep_y), (CARD_WIDTH - PADDING, sep_y)],
                fill=COLOR_SEPARATOR, width=1,
            )
            y += 36
            prev_style = style
            continue

        if style == "header":
            # Modern header with colored left border
            header_bg_height = HEADER_LINE_HEIGHT + 12

            # Header background
            draw.rounded_rectangle(
                [(PADDING - 12, y), (CARD_WIDTH - PADDING + 12, y + header_bg_height)],
                radius=10, fill=COLOR_LIGHT_BG,
            )

            # Colored left accent bar
            draw.rounded_rectangle(
                [(PADDING - 8, y + 6), (PADDING - 2, y + header_bg_height - 6)],
                radius=3, fill=COLOR_ACCENT,
            )

            # Header text
            draw.text(
                (PADDING + 8, y + 6), ent_text,
                fill=COLOR_ACCENT, font=header_font,
            )
            y += header_bg_height
            prev_style = style
            continue

        if style == "col2":
            # Two-column layout with better spacing
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
    bar_y = img_height - 8
    draw.rounded_rectangle(
        [(PADDING, bar_y), (CARD_WIDTH - PADDING, bar_y + 4)],
        radius=2, fill=COLOR_TITLE_BG,
    )

    # Paste card onto canvas
    canvas.paste(img, (SHADOW_OFFSET, SHADOW_OFFSET), img)

    # Convert to RGB and save
    out = canvas.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG", quality=95, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"base64://{b64}"
