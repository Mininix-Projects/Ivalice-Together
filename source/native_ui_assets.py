from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import struct
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

import menu_themes


HERE = Path(__file__).resolve().parent
ASSET_ROOT = HERE / "extracted_ui_assets"
PNG_ROOT = HERE / "extracted_ui_assets_png"
SKIN_MENU_ROOT = HERE / "imgui_skin_menu"
NATIVE_WINDOW_ATLAS = PNG_ROOT / "w_win_c__pic00__pal00.png"

FONT_DAT = ASSET_ROOT / "gamedata/d3d11/artdata/font/us/font00.dat"
FONT_KER = ASSET_ROOT / "gamedata/d3d11/artdata/font/us/font00.ker"
FONT_ATLAS = PNG_ROOT / "font00.png"


_SKIN_PIECES: dict[tuple[str, str], Image.Image] = {}
_NATIVE_WINDOW_PIECES: dict[str, tuple[Image.Image, Image.Image]] = {}


def _tint_native_window(source: Image.Image, palette: str) -> Image.Image:
    alpha = source.getchannel("A")
    value = source.convert("RGB").convert("L")
    if palette == "gold":
        tinted = ImageOps.colorize(
            value, black=(43, 34, 6), white=(216, 190, 72)
        ).convert("RGBA")
    elif palette == "sapphire":
        tinted = ImageOps.colorize(
            value, black=(10, 24, 45), white=(145, 194, 247)
        ).convert("RGBA")
    else:
        theme = menu_themes.get_theme(palette)
        tinted = ImageOps.colorize(
            value,
            black=theme.panel_dark,
            white=theme.panel_light,
        ).convert("RGBA")
    tinted.putalpha(alpha)
    return tinted


def _native_window_pieces(palette: str) -> tuple[Image.Image, Image.Image]:
    cached = _NATIVE_WINDOW_PIECES.get(palette)
    if cached is not None:
        return cached[0].copy(), cached[1].copy()

    with Image.open(NATIVE_WINDOW_ATLAS) as source:
        atlas = source.convert("RGBA")

    left = atlas.crop((3, 2, 113, 58))
    right = atlas.crop((0, 66, 123, 122))
    result = (_tint_native_window(left, palette), _tint_native_window(right, palette))
    _NATIVE_WINDOW_PIECES[palette] = result
    return result[0].copy(), result[1].copy()


def _stretch_region(
    destination: Image.Image,
    source: Image.Image,
    source_box: tuple[int, int, int, int],
    destination_box: tuple[int, int, int, int],
) -> None:
    width = destination_box[2] - destination_box[0]
    height = destination_box[3] - destination_box[1]
    if width <= 0 or height <= 0:
        return
    piece = source.crop(source_box)
    if piece.size != (width, height):
        piece = piece.resize((width, height), Image.Resampling.LANCZOS)
    destination.alpha_composite(piece, destination_box[:2])


def _atlas_native_panel(size: tuple[int, int], palette: str) -> Image.Image:
    width, height = size
    output = Image.new("RGBA", (width + 10, height + 12), (0, 0, 0, 0))
    left, right = _native_window_pieces(palette)
    source = Image.new(
        "RGBA", (left.width + right.width, max(left.height, right.height)), (0, 0, 0, 0)
    )
    source.alpha_composite(left, (0, 0))
    source.alpha_composite(right, (left.width, 0))
    panel = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    source_left = 14
    source_right = 14
    source_top = 12
    source_bottom = 12
    dest_left = min(20, width // 2)
    dest_right = min(20, width - dest_left)
    dest_top = min(20, height // 2)
    dest_bottom = min(20, height - dest_top)
    sx = (0, source_left, source.width - source_right, source.width)
    sy = (0, source_top, source.height - source_bottom, source.height)
    dx = (0, dest_left, width - dest_right, width)
    dy = (0, dest_top, height - dest_bottom, height)
    for row in range(3):
        for column in range(3):
            _stretch_region(
                panel,
                source,
                (sx[column], sy[row], sx[column + 1], sy[row + 1]),
                (dx[column], dy[row], dx[column + 1], dy[row + 1]),
            )
    output.alpha_composite(panel, (3, 2))
    return output


def _skin_piece(name: str, palette: str) -> Image.Image:
    key = (name, palette)
    cached = _SKIN_PIECES.get(key)
    if cached is not None:
        return cached.copy()

    with Image.open(SKIN_MENU_ROOT / name) as image:
        source = image.convert("RGBA")
    if palette == "gold":
        alpha = source.getchannel("A")
        hue, saturation, value = source.convert("RGB").convert("HSV").split()

        hue = hue.point(lambda channel: (channel + 133) & 0xFF)
        saturation = saturation.point(lambda channel: min(255, int(channel * 1.04)))
        source = Image.merge("HSV", (hue, saturation, value)).convert("RGBA")
        source.putalpha(alpha)
    elif palette != "sapphire":
        theme = menu_themes.get_theme(palette)
        alpha = source.getchannel("A")
        value = source.convert("RGB").convert("L")
        source = ImageOps.colorize(
            value,
            black=theme.panel_dark,
            white=theme.panel_light,
        ).convert("RGBA")
        source.putalpha(alpha)

    _SKIN_PIECES[key] = source
    return source.copy()


def _strengthen_skin_top_corner(
    piece: Image.Image,
    palette: str,
) -> Image.Image:
    alpha = piece.getchannel("A")
    luminance = piece.convert("RGB").convert("L")
    highlight_mask = luminance.point(lambda value: max(0, min(255, (value - 60) * 2)))
    highlight_mask = ImageChops.multiply(highlight_mask, alpha).point(
        lambda value: value * 96 // 255
    )
    highlight = menu_themes.get_theme(palette).highlight
    layer = Image.new("RGBA", piece.size, (*highlight, 0))
    layer.putalpha(highlight_mask)
    result = piece.copy()
    result.alpha_composite(layer)

    result.putalpha(alpha)
    return result


def _reinforce_directional_skin_edges(
    panel: Image.Image,
    palette: str,
) -> Image.Image:
    alpha = panel.getchannel("A")
    width, height = panel.size
    side_depth = min(45, max(1, width // 2))
    top_depth = min(30, max(1, height))
    bottom_depth = min(15, max(1, height))

    def falloff(distance: int, depth: int, maximum: int) -> int:
        if distance >= depth:
            return 0
        remaining = (depth - distance) / depth
        return int(round(maximum * remaining))

    values = bytearray(width * height)
    offset = 0
    for y in range(height):
        top = falloff(y, top_depth, 52)
        bottom = falloff(height - 1 - y, bottom_depth, 10)
        vertical = max(top, bottom)
        for x in range(width):
            side = falloff(min(x, width - 1 - x), side_depth, 68)
            values[offset] = max(side, vertical)
            offset += 1

    mask = Image.frombytes("L", panel.size, bytes(values))
    mask = ImageChops.multiply(mask, alpha)
    highlight = menu_themes.get_theme(palette).highlight
    layer = Image.new("RGBA", panel.size, (*highlight, 0))
    layer.putalpha(mask)
    result = panel.copy()
    result.alpha_composite(layer)
    result.putalpha(alpha)
    return result


# The nine-slice artwork is 72 px wide with 60/68 px bands. Drawing it at half
# size produced an approximately 8 px corner radius; lossless captures show a
# roughly 12 px native command-box radius, hence the three-quarter scale. One
# scale for all three slices keeps the artwork undistorted.
SKIN_SLICE_SCALE = 0.75
SKIN_CORNER_WIDTH = int(round(72 * SKIN_SLICE_SCALE))
SKIN_TOP_HEIGHT = int(round(60 * SKIN_SLICE_SCALE))
SKIN_BOTTOM_HEIGHT = int(round(68 * SKIN_SLICE_SCALE))


def _skin_native_panel(size: tuple[int, int], palette: str) -> Image.Image:
    width, height = size
    output = Image.new("RGBA", (width + 10, height + 12), (0, 0, 0, 0))
    panel = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    corner_width = min(SKIN_CORNER_WIDTH, max(1, width // 2))
    top_height = min(SKIN_TOP_HEIGHT, max(1, height // 2))
    bottom_height = min(SKIN_BOTTOM_HEIGHT, max(1, height - top_height))
    middle_width = max(1, width - corner_width * 2)
    middle_height = max(1, height - top_height - bottom_height)

    center = _skin_piece("panel.png", palette).resize(
        (middle_width, middle_height), Image.Resampling.BILINEAR
    )
    panel.alpha_composite(center, (corner_width, top_height))

    placements = (
        ("border_top.png", (middle_width, top_height), (corner_width, 0)),
        (
            "border_bottom.png",
            (middle_width, bottom_height),
            (corner_width, height - bottom_height),
        ),
        ("border_left.png", (corner_width, middle_height), (0, top_height)),
        (
            "border_right.png",
            (corner_width, middle_height),
            (width - corner_width, top_height),
        ),
        ("border_tl.png", (corner_width, top_height), (0, 0)),
        ("border_tr.png", (corner_width, top_height), (width - corner_width, 0)),
        (
            "border_bl.png",
            (corner_width, bottom_height),
            (0, height - bottom_height),
        ),
        (
            "border_br.png",
            (corner_width, bottom_height),
            (width - corner_width, height - bottom_height),
        ),
    )
    for filename, piece_size, position in placements:
        piece = _skin_piece(filename, palette).resize(
            piece_size, Image.Resampling.LANCZOS
        )
        if filename in ("border_tl.png", "border_tr.png"):
            piece = _strengthen_skin_top_corner(piece, palette)
        panel.alpha_composite(piece, position)

    panel = _reinforce_directional_skin_edges(panel, palette)
    output.alpha_composite(panel, (3, 2))
    return output


@dataclass(frozen=True)
class Glyph:
    index: int
    codepoint: int
    x: int
    y: int
    width: int
    height: int
    advance: int
    advance_duplicate: int
    sentinel: int


@dataclass(frozen=True)
class FontHeader:
    version: int
    glyph_count: int
    atlas_width: int
    atlas_height: int
    glyph_width: int
    glyph_height: int
    scale_bits: int
    reserved: int
    space_advance: int
    tab_advance: int


# The game composes text in two layers: a neutral glyph-coverage core and a
# spread colour. Colours and the blend operator came from lossless 1080p captures.

NATIVE_TEXT_EFFECT = True


NATIVE_TEXT_CORE = (228, 228, 228, 255)


NATIVE_SPREAD_LUMA = 121.5


def relative_luminance(color: tuple[int, int, int, int]) -> float:
    return 0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2]


@dataclass(frozen=True)
class TextStyle:
    core: tuple[int, int, int, int]
    edge: tuple[int, int, int, int]
    glow: tuple[int, int, int, int]
    shadow: tuple[int, int, int, int] = (0, 0, 0, 185)
    edge_radius: float = 1.35
    glow_radius: float = 2.8
    shadow_radius: float = 2.0
    shadow_offset: tuple[float, float] = (1.0, 2.0)
    core_inset: float = 0.0

    spread_dilate: float = 1.0
    spread_sigma: float = 1.95
    spread_gain: float = 1.75


BLUE_TEXT = TextStyle(
    core=NATIVE_TEXT_CORE,
    edge=(53, 129, 248, 255),
    glow=(22, 73, 255, 215),
)

GOLD_TEXT = TextStyle(
    core=(255, 253, 231, 255),
    edge=(255, 205, 68, 250),
    glow=(222, 132, 0, 205),
)

WHITE_TEXT = TextStyle(
    core=(255, 255, 252, 255),
    edge=(192, 201, 217, 245),
    glow=(52, 75, 113, 185),
)

RED_TEXT = TextStyle(
    core=(255, 206, 205, 255),
    edge=(246, 77, 85, 250),
    glow=(126, 18, 47, 215),
)


def _solid_layer(
    size: tuple[int, int], color: tuple[int, int, int, int], mask: Image.Image
) -> Image.Image:
    layer = Image.new("RGBA", size, color)
    if color[3] != 255:
        mask = mask.point(lambda value: value * color[3] // 255)
    layer.putalpha(mask)
    return layer


def _max_filter(mask: Image.Image, radius: float) -> Image.Image:
    radius = max(0.0, float(radius))
    lower = int(radius)
    fraction = radius - lower

    def filtered(pixels: int) -> Image.Image:
        if pixels <= 0:
            return mask.copy()
        return mask.filter(ImageFilter.MaxFilter(pixels * 2 + 1))

    low = filtered(lower)
    if fraction <= 0.001:
        return low
    high = filtered(lower + 1)
    return Image.blend(low, high, fraction)


def _min_filter(mask: Image.Image, radius: float) -> Image.Image:
    radius = max(0.0, float(radius))
    lower = int(radius)
    fraction = radius - lower

    def filtered(pixels: int) -> Image.Image:
        if pixels <= 0:
            return mask.copy()
        return mask.filter(ImageFilter.MinFilter(pixels * 2 + 1))

    low = filtered(lower)
    if fraction <= 0.001:
        return low
    high = filtered(lower + 1)
    return Image.blend(low, high, fraction)


class BitmapFont:
    HEADER_STRUCT = struct.Struct("<10I")
    GLYPH_STRUCT = struct.Struct("<9i")
    KERN_STRUCT = struct.Struct("<3i")

    def __init__(
        self,
        atlas_path: Path = FONT_ATLAS,
        data_path: Path = FONT_DAT,
        kerning_path: Path = FONT_KER,
    ) -> None:
        raw = data_path.read_bytes()
        if len(raw) < self.HEADER_STRUCT.size:
            raise ValueError(f"truncated native font table: {data_path}")
        self.header = FontHeader(*self.HEADER_STRUCT.unpack_from(raw, 0))
        expected = (
            self.HEADER_STRUCT.size + self.header.glyph_count * self.GLYPH_STRUCT.size
        )
        if len(raw) != expected:
            raise ValueError(
                f"unexpected font00.dat length {len(raw)}; expected {expected}"
            )
        self.glyphs: dict[int, Glyph] = {}
        offset = self.HEADER_STRUCT.size
        for _ in range(self.header.glyph_count):
            glyph = Glyph(*self.GLYPH_STRUCT.unpack_from(raw, offset))
            offset += self.GLYPH_STRUCT.size
            self.glyphs[glyph.codepoint] = glyph

        kerning_raw = kerning_path.read_bytes()
        if len(kerning_raw) % self.KERN_STRUCT.size:
            raise ValueError(f"misaligned native kerning table: {kerning_path}")
        self.kerning: dict[tuple[int, int], int] = {}
        for offset in range(0, len(kerning_raw), self.KERN_STRUCT.size):
            left, right, adjustment = self.KERN_STRUCT.unpack_from(kerning_raw, offset)
            self.kerning[(left, right)] = adjustment

        with Image.open(atlas_path) as source:
            self.atlas = source.convert("RGBA")
        if self.atlas.size != (self.header.atlas_width, self.header.atlas_height):
            raise ValueError(
                f"native atlas is {self.atlas.size}, table says "
                f"{self.header.atlas_width}x{self.header.atlas_height}"
            )
        self._alpha = self.atlas.getchannel("A")

    def _glyph_for(self, character: str) -> Glyph | None:
        codepoint = ord(character)
        glyph = self.glyphs.get(codepoint)
        if glyph is None and character not in (" ", "\t", "\n"):
            glyph = self.glyphs.get(ord("?"))
        return glyph

    def text_width(self, text: str, scale: float = 1.0) -> int:
        lines = text.split("\n")
        widest = 0
        for line in lines:
            cursor = 0
            previous: int | None = None
            for character in line:
                codepoint = ord(character)
                if previous is not None:
                    cursor += self.kerning.get((previous, codepoint), 0)
                if character == " ":
                    cursor += self.header.space_advance
                elif character == "\t":
                    cursor += self.header.tab_advance
                else:
                    glyph = self._glyph_for(character)
                    if glyph:
                        cursor += glyph.advance
                previous = codepoint
            widest = max(widest, cursor)
        return max(1, int(round(widest * scale)))

    def render_mask(
        self,
        text: str,
        scale: float = 1.0,
        line_spacing: float = 1.0,
    ) -> Image.Image:
        if scale <= 0:
            raise ValueError("scale must be positive")
        lines = text.split("\n")
        base_line_height = self.header.glyph_height
        width = max(self.text_width(line, scale) for line in lines)
        line_height = max(1, int(round(base_line_height * scale * line_spacing)))
        height = max(1, line_height * len(lines))
        canvas = Image.new("L", (width + int(8 * scale) + 2, height), 0)

        for line_number, line in enumerate(lines):
            cursor = 0.0
            previous: int | None = None
            for character in line:
                codepoint = ord(character)
                if previous is not None:
                    cursor += self.kerning.get((previous, codepoint), 0) * scale
                if character == " ":
                    cursor += self.header.space_advance * scale
                elif character == "\t":
                    cursor += self.header.tab_advance * scale
                else:
                    glyph = self._glyph_for(character)
                    if glyph:
                        source_y = self.header.atlas_height - glyph.y - glyph.height
                        source = self._alpha.crop(
                            (
                                glyph.x,
                                source_y,
                                glyph.x + glyph.width,
                                source_y + glyph.height,
                            )
                        )
                        source = ImageOps.flip(source)
                        if scale != 1.0:
                            source = source.resize(
                                (
                                    max(1, int(round(glyph.width * scale))),
                                    max(1, int(round(glyph.height * scale))),
                                ),
                                Image.Resampling.LANCZOS,
                            )
                        # Native kerning overlaps glyphs. A simple paste would
                        # erase the previous glyph, so combine their coverage.

                        position = (int(round(cursor)), line_number * line_height)
                        box = (
                            position[0],
                            position[1],
                            position[0] + source.width,
                            position[1] + source.height,
                        )
                        clipped = (
                            max(0, box[0]),
                            max(0, box[1]),
                            min(canvas.width, box[2]),
                            min(canvas.height, box[3]),
                        )
                        if clipped[2] > clipped[0] and clipped[3] > clipped[1]:
                            source_box = (
                                clipped[0] - box[0],
                                clipped[1] - box[1],
                                clipped[2] - box[0],
                                clipped[3] - box[1],
                            )
                            existing = canvas.crop(clipped)
                            combined = ImageChops.lighter(
                                existing, source.crop(source_box)
                            )
                            canvas.paste(combined, clipped[:2])
                        cursor += glyph.advance * scale
                previous = codepoint
        return canvas

    def render(
        self,
        text: str,
        scale: float = 1.0,
        style: TextStyle = GOLD_TEXT,
        line_spacing: float = 1.0,
        trim: bool = True,
    ) -> Image.Image:
        core = self.render_mask(text, scale=scale, line_spacing=line_spacing)

        if NATIVE_TEXT_EFFECT:
            reach = style.spread_dilate + 3.0 * style.spread_sigma
            pad = max(6, int(round(reach * scale)) + 1)
            padded = Image.new("L", (core.width + 2 * pad, core.height + 2 * pad), 0)
            padded.paste(core, (pad, pad))

            spread = _max_filter(padded, style.spread_dilate * scale)
            if style.spread_sigma > 0:
                spread = spread.filter(
                    ImageFilter.GaussianBlur(style.spread_sigma * scale)
                )
            if style.spread_gain != 1.0:
                gain = style.spread_gain
                spread = spread.point(lambda value: min(255, int(value * gain + 0.5)))

            output = Image.new("RGBA", padded.size, (0, 0, 0, 0))
            output.alpha_composite(_solid_layer(padded.size, style.edge, spread))
            output.alpha_composite(_solid_layer(padded.size, style.core, padded))

            if trim:
                bounds = output.getbbox()
                if bounds:
                    output = output.crop(bounds)
            return output

        pad = max(6, int(round((style.glow_radius + 3.0) * scale)))
        padded = Image.new("L", (core.width + 2 * pad, core.height + 2 * pad), 0)
        padded.paste(core, (pad, pad))

        core_mask = _min_filter(padded, style.core_inset * scale)
        edge_radius = style.edge_radius * scale
        edge_mask = _max_filter(padded, edge_radius)
        glow_mask = _max_filter(padded, edge_radius + 1.0).filter(
            ImageFilter.GaussianBlur(style.glow_radius * scale)
        )
        shadow_mask = _max_filter(padded, edge_radius + 1.0).filter(
            ImageFilter.GaussianBlur(style.shadow_radius * scale)
        )

        output = Image.new("RGBA", padded.size, (0, 0, 0, 0))
        shadow_x = int(round(style.shadow_offset[0] * scale))
        shadow_y = int(round(style.shadow_offset[1] * scale))
        shadow_layer = _solid_layer(padded.size, style.shadow, shadow_mask)
        output.alpha_composite(shadow_layer, (shadow_x, shadow_y))
        output.alpha_composite(_solid_layer(padded.size, style.glow, glow_mask))
        output.alpha_composite(_solid_layer(padded.size, style.edge, edge_mask))
        output.alpha_composite(_solid_layer(padded.size, style.core, core_mask))

        if trim:
            bounds = output.getbbox()
            if bounds:
                output = output.crop(bounds)
        return output


@lru_cache(maxsize=32)
def native_panel(
    size: tuple[int, int],
    palette: str = "gold",
    radius: int = 18,
) -> Image.Image:
    width, height = size
    if width <= 2 or height <= 2:
        raise ValueError("panel size is too small")

    if SKIN_MENU_ROOT.is_dir():
        return _skin_native_panel(size, palette)
    if NATIVE_WINDOW_ATLAS.is_file():
        return _atlas_native_panel(size, palette)
    styles = {
        "gold": {
            "top": (94, 77, 18, 231),
            "bottom": (50, 41, 12, 224),
            "border": (205, 181, 85, 244),
            "highlight": (255, 235, 149, 155),
            "stripe": (18, 13, 3, 0),
        },
        "sapphire": {
            "top": (27, 48, 83, 231),
            "bottom": (13, 28, 53, 224),
            "border": (113, 150, 212, 244),
            "highlight": (204, 226, 255, 150),
            "stripe": (2, 8, 22, 25),
        },
    }
    if palette in styles:
        colors = styles[palette]
    else:
        theme = menu_themes.get_theme(palette)
        colors = {
            "top": (*theme.panel_light, 233),
            "bottom": (*theme.panel_dark, 226),
            "border": (*theme.accent, 246),
            "highlight": (*theme.highlight, 160),
            "stripe": (*theme.panel_dark, 0),
        }

    output = Image.new("RGBA", (width + 10, height + 12), (0, 0, 0, 0))
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width - 1, height - 1), radius=radius, fill=255
    )

    gradient = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gd = ImageDraw.Draw(gradient)
    top = colors["top"]
    bottom = colors["bottom"]
    for y in range(height):
        t = y / max(1, height - 1)
        color = tuple(int(round(a + (b - a) * t)) for a, b in zip(top, bottom))
        gd.line((0, y, width, y), fill=color)
    gradient.putalpha(ImageChops.multiply(gradient.getchannel("A"), mask))

    texture = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    td = ImageDraw.Draw(texture)
    if colors["stripe"][3]:
        for x in range(-height, width + height, 86):
            td.line(
                (x, height + 28, x + height + 28, 0), fill=colors["stripe"], width=18
            )
            td.line(
                (x + 24, height + 28, x + height + 52, 0),
                fill=(255, 255, 255, 8),
                width=3,
            )
    texture.putalpha(ImageChops.multiply(texture.getchannel("A"), mask))
    gradient.alpha_composite(texture)

    shadow = Image.new("RGBA", output.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (5, 7, width + 4, height + 6), radius=radius, fill=(0, 0, 0, 175)
    )
    output.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(3.2)))
    output.alpha_composite(gradient, (3, 2))
    border = ImageDraw.Draw(output)
    border.rounded_rectangle(
        (3, 2, width + 2, height + 1),
        radius=radius,
        outline=colors["border"],
        width=2,
    )

    glint = Image.new("RGBA", output.size, (0, 0, 0, 0))
    ImageDraw.Draw(glint).line(
        (radius + 6, 3, width - radius + 1, 3),
        fill=colors["highlight"],
        width=1,
    )
    output.alpha_composite(glint.filter(ImageFilter.GaussianBlur(0.45)))
    return output


__all__ = [
    "ASSET_ROOT",
    "BLUE_TEXT",
    "BitmapFont",
    "FONT_ATLAS",
    "FONT_DAT",
    "FONT_KER",
    "GOLD_TEXT",
    "Glyph",
    "PNG_ROOT",
    "RED_TEXT",
    "TextStyle",
    "WHITE_TEXT",
    "native_panel",
]
