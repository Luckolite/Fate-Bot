"""Render Fate's owner dashboard as deterministic images."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from math import ceil
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

CANVAS_SIZE = (1600, 1000)
FONT_CANDIDATES = {
    "regular": (
        Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ),
    "bold": (
        Path("C:/Windows/Fonts/segoeuib.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ),
}


@dataclass(frozen=True)
class DashboardRow:
    """A label/value pair, optionally accompanied by a usage bar."""

    label: str
    value: str
    progress: float | None = None
    tone: str = "normal"


@dataclass(frozen=True)
class DashboardCard:
    """A titled group of related dashboard rows."""

    title: str
    rows: Sequence[DashboardRow]


@dataclass(frozen=True)
class DashboardPage:
    """The content and visual identity for one dashboard image."""

    slug: str
    label: str
    title: str
    subtitle: str
    status: str
    status_tone: str
    accent: tuple[int, int, int]
    icon_path: Path
    cards: Sequence[DashboardCard]
    footer: str


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    variant = "bold" if bold else "regular"
    for path in FONT_CANDIDATES[variant]:
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def _truncate(
    draw: ImageDraw.ImageDraw,
    value: object,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    text = " ".join(str(value).replace("\n", " ").split()) or "Unavailable"
    if draw.textlength(text, font=font) <= max_width:
        return text
    suffix = "…"
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if draw.textlength(text[:middle] + suffix, font=font) <= max_width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + suffix


def _pill(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    tone: str,
) -> None:
    colors = {
        "good": ((33, 214, 145, 42), (76, 244, 177), (183, 255, 227)),
        "warn": ((240, 166, 74, 42), (255, 184, 77), (255, 226, 184)),
        "normal": ((105, 92, 255, 42), (129, 118, 255), (224, 220, 255)),
    }
    fill, outline, foreground = colors.get(tone, colors["normal"])
    font = _font(23, bold=True)
    width = ceil(draw.textlength(text.upper(), font=font)) + 52
    x, y = xy
    draw.rounded_rectangle(
        (x, y, x + width, y + 46),
        radius=23,
        fill=fill,
        outline=outline,
        width=2,
    )
    draw.ellipse((x + 16, y + 18, x + 26, y + 28), fill=outline)
    draw.text((x + 36, y + 7), text.upper(), font=font, fill=foreground)


def _draw_icon(canvas: Image.Image, path: Path, accent: tuple[int, int, int]) -> None:
    icon_box = (1330, 44, 1536, 250)
    icon = Image.open(path).convert("RGB")
    icon = ImageOps.fit(icon, (190, 190), method=Image.Resampling.LANCZOS)
    mask = Image.new("L", icon.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, 189, 189), radius=40, fill=255)
    panel = Image.new("RGBA", (206, 206), (7, 10, 29, 224))
    panel_draw = ImageDraw.Draw(panel, "RGBA")
    panel_draw.rounded_rectangle(
        (0, 0, 205, 205), radius=46, outline=(*accent, 150), width=2
    )
    panel.paste(icon.convert("RGBA"), (8, 8), mask)
    canvas.alpha_composite(panel, (icon_box[0], icon_box[1]))


def _draw_card(
    canvas: Image.Image,
    card: DashboardCard,
    bounds: tuple[int, int, int, int],
    accent: tuple[int, int, int],
) -> None:
    x1, y1, x2, y2 = bounds
    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.rounded_rectangle(
        bounds,
        radius=28,
        fill=(6, 11, 34, 218),
        outline=(*accent, 92),
        width=2,
    )
    draw.rounded_rectangle(
        (x1 + 22, y1 + 22, x1 + 28, y1 + 60),
        radius=3,
        fill=(*accent, 255),
    )
    title_font = _font(26, bold=True)
    title = _truncate(draw, card.title.upper(), title_font, x2 - x1 - 78)
    draw.text(
        (x1 + 42, y1 + 22),
        title,
        font=title_font,
        fill=(231, 235, 255, 255),
    )

    rows = tuple(card.rows) or (DashboardRow("Status", "Unavailable"),)
    content_top = y1 + 78
    content_bottom = y2 - 24
    row_height = min(53, max(29, (content_bottom - content_top) // len(rows)))
    compact = row_height < 35
    label_font = _font(19 if compact else 22)
    value_font = _font(22 if compact else 26, bold=True)
    max_text_width = x2 - x1 - 56
    value_colors = {
        "good": (100, 245, 184, 255),
        "warn": (255, 193, 100, 255),
        "bad": (255, 115, 132, 255),
        "normal": (242, 244, 255, 255),
        "muted": (168, 179, 215, 255),
    }

    for index, row in enumerate(rows):
        top = content_top + index * row_height
        if top + 24 > content_bottom:
            break
        value_color = value_colors.get(row.tone, value_colors["normal"])
        value = _truncate(draw, row.value, value_font, int(max_text_width * 0.57))
        label_max = max_text_width - ceil(draw.textlength(value, font=value_font)) - 22
        label = _truncate(draw, row.label, label_font, max(72, label_max))
        draw.text((x1 + 28, top), label, font=label_font, fill=(153, 166, 207, 255))
        value_x = x2 - 28 - ceil(draw.textlength(value, font=value_font))
        draw.text((value_x, top - 2), value, font=value_font, fill=value_color)

        if row.progress is not None and row_height >= 43:
            progress = max(0.0, min(100.0, float(row.progress)))
            bar_y = top + 31
            bar_bounds = (x1 + 28, bar_y, x2 - 28, bar_y + 7)
            draw.rounded_rectangle(bar_bounds, radius=4, fill=(79, 91, 132, 76))
            if progress:
                fill_right = x1 + 28 + max(7, round((x2 - x1 - 56) * progress / 100))
                draw.rounded_rectangle(
                    (x1 + 28, bar_y, fill_right, bar_y + 7),
                    radius=4,
                    fill=(*accent, 230),
                )


def render_dashboard_page(page: DashboardPage, background_path: Path) -> bytes:
    """Render one dashboard page and return its PNG bytes."""
    background = Image.open(background_path).convert("RGB")
    background = ImageOps.fit(background, CANVAS_SIZE, method=Image.Resampling.LANCZOS)
    background = ImageEnhance.Brightness(background).enhance(0.72)
    canvas = background.convert("RGBA")
    shade = Image.new("RGBA", CANVAS_SIZE, (1, 4, 18, 70))
    canvas = Image.alpha_composite(canvas, shade)
    draw = ImageDraw.Draw(canvas, "RGBA")
    accent = page.accent

    draw.rounded_rectangle(
        (42, 32, 1558, 958),
        radius=40,
        fill=(1, 5, 22, 56),
        outline=(*accent, 70),
        width=2,
    )
    draw.text(
        (68, 52),
        "FATE  //  OWNER OPERATIONS",
        font=_font(22, bold=True),
        fill=(*accent, 255),
    )
    draw.text(
        (64, 85),
        page.title,
        font=_font(62, bold=True),
        fill=(248, 249, 255, 255),
    )
    subtitle = _truncate(draw, page.subtitle, _font(27), 1110)
    draw.text((67, 158), subtitle, font=_font(27), fill=(168, 179, 215, 255))
    _pill(draw, (67, 197), page.status, page.status_tone)
    _draw_icon(canvas, page.icon_path, accent)

    cards = tuple(page.cards)
    columns = 3 if len(cards) >= 5 or len(cards) == 3 else 2
    rows = max(1, ceil(len(cards) / columns))
    gap = 22
    left, right = 64, 1536
    top, bottom = 270, 885
    card_width = (right - left - gap * (columns - 1)) // columns
    card_height = (bottom - top - gap * (rows - 1)) // rows
    for index, card in enumerate(cards):
        column = index % columns
        row = index // columns
        x1 = left + column * (card_width + gap)
        y1 = top + row * (card_height + gap)
        _draw_card(
            canvas,
            card,
            (x1, y1, x1 + card_width, y1 + card_height),
            accent,
        )

    draw.line((68, 917, 1532, 917), fill=(*accent, 92), width=2)
    footer = _truncate(draw, page.footer, _font(20), 1430)
    draw.text((68, 926), footer, font=_font(20), fill=(139, 151, 192, 255))

    output = BytesIO()
    canvas.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()


def render_dashboard_pages(
    pages: Iterable[DashboardPage], background_path: Path
) -> dict[str, bytes]:
    """Render all dashboard pages while preserving their supplied order."""
    return {
        page.label: render_dashboard_page(page, background_path) for page in pages
    }
