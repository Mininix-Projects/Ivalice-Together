from __future__ import annotations

import colorsys
import math
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping

from PIL import Image, ImageChops, ImageDraw, ImageFilter
import numpy as np

import menu_themes
import native_ui_assets as native


HERE = Path(__file__).resolve().parent


CANVAS_SIZE = (520, 560)
PANEL_LEFT = 35
PANEL_TOP = 75


PANEL_WIDTH = 465


RIGHT_LABEL_ANCHOR = 448
ROW_PITCH = 46
CURSOR_X_OFFSET = 10
CURSOR_Y_OFFSET = 13
PANEL_VERTICAL_PADDING = 48
PORTRAIT_X = 29
ROW_CONTENT_Y = 16
COMMAND_TEXT_SCALE = 0.99


LABEL_X_NUDGE = {"Gambits": -2}
TITLE_TEXT_SCALE = 0.86
TITLE_SHEAR = 0.16
PORTRAIT_SCALE = 1.05
# Fran's portrait is taller because of her ears, so it uses a dedicated scale.
PORTRAIT_SCALE_BY_MEMBER_ID = {
    2: 0.74,
}
PORTRAIT_Y_OFFSET = 8
TARGET_PANEL_WIDTH = 458
TARGET_PANEL_HEIGHT = 127


TARGET_TITLE_ONLY_HEIGHT = 87
MAX_TARGET_VISIBLE_ROWS = 6
TRANSITION_SECONDS = 0.12
TRANSITION_SLIDE_PX = 34


# 38 px in 0.18 s produced roughly 211 px/s: too much travel for such a short
# interval, making the jump visible. Shorter travel over a longer interval gives
# roughly 92 px/s and keeps page transitions continuous without feeling slow.
PAGE_TRANSITION_SECONDS = 0.24
PAGE_TRANSITION_SLIDE_PX = 22
TARGET_GROUP_TRANSITION_SECONDS = 0.16
_STATIC_LIST_BASES: OrderedDict[tuple, Image.Image] = OrderedDict()
_STATIC_LAYERED_LIST_BASES: OrderedDict[tuple, Image.Image] = OrderedDict()
_STATIC_TARGET_BASES: OrderedDict[tuple, Image.Image] = OrderedDict()
_LIST_CHROME_BASES: OrderedDict[tuple, Image.Image] = OrderedDict()
_STATIC_BASE_LIMIT = 24
_LAYERED_LIST_BASE_LIMIT = 48


def _remember_static_base(
    cache: OrderedDict[tuple, Image.Image],
    key: tuple,
    candidate: Image.Image,
    limit: int = _STATIC_BASE_LIMIT,
) -> Image.Image:
    cached = cache.pop(key, None)
    if cached is None:
        cached = candidate
    cache[key] = cached
    while len(cache) > limit:
        cache.popitem(last=False)
    return cached


def _list_chrome_base(
    height: int,
    title: str,
    right_header: str,
    player: int,
    party_member_id: int | None,
    theme_name: str,
) -> Image.Image:
    key = (height, title, right_header, player, party_member_id, theme_name)
    cached = _LIST_CHROME_BASES.pop(key, None)
    if cached is not None:
        _LIST_CHROME_BASES[key] = cached
        return cached

    canvas = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
    panel = native.native_panel((PANEL_WIDTH, height), palette=theme_name)
    canvas.alpha_composite(panel, (PANEL_LEFT - 3, PANEL_TOP - 2))
    canvas.alpha_composite(
        _portrait(player, party_member_id),
        _portrait_position(player, PORTRAIT_Y_OFFSET, party_member_id),
    )
    _paste_leader_flag(canvas, player, PORTRAIT_Y_OFFSET, party_member_id)
    if title:
        canvas.alpha_composite(
            _title_text(title, player, theme_name),
            (PANEL_LEFT + 20, PANEL_TOP + 11),
        )
    if right_header:
        header = _column_header_text(right_header, player, theme_name)
        canvas.alpha_composite(
            header,
            (
                PANEL_LEFT + RIGHT_LABEL_ANCHOR - header.width - 22,
                PANEL_TOP + 14,
            ),
        )

    _LIST_CHROME_BASES[key] = canvas
    while len(_LIST_CHROME_BASES) > 24:
        _LIST_CHROME_BASES.popitem(last=False)
    return canvas


@dataclass(frozen=True)
class ListView:
    entries: tuple[str, ...]
    selected_index: int = 0
    title: str = ""
    right_labels: Mapping[int, str] | None = None
    right_header: str = ""
    enabled: tuple[bool, ...] | None = None
    first_visible: int = 0

    scroll_position: float | None = None
    max_visible_rows: int = 5
    cursor_position: float | None = None
    cursor_trail: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class TargetView:
    action_name: str
    target_name: str = ""
    has_target: bool = True
    group: str = "foes"
    target_names: tuple[str, ...] = ()
    selected_index: int = 0
    left_prompt: str = "Q"
    right_prompt: str = "E"

    available_groups: tuple[str, ...] = ("foes", "neutrals", "party")

    show_party_names: bool = False

    cursor_position: float | None = None
    cursor_trail: tuple[tuple[float, float], ...] = ()

    # Indices other than selected_index which the pending area action will
    # also affect.  They remain presentation-only: submission still uses the
    # single selected target.
    secondary_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class TransitionFrame:
    image: Image.Image
    offset_x: float
    opacity: float


@dataclass(frozen=True)
class ScrollbarGeometry:
    track_top: int
    track_bottom: int
    thumb_top: int
    thumb_bottom: int


FONT = native.BitmapFont()
HELP_BADGE_SIZE = (56, 57)
HELP_BADGE_Y = 0
HELP_TEXT_X = 56
HELP_TEXT_Y = 2
COMMAND_EDGE_RADIUS = native.BLUE_TEXT.edge_radius
LETTER_GLOW_GAIN = 1.25
COMMAND_CORE_INSET = 0.45
THREE_PLAYER_PANEL_SCALE = 0.75
NATIVE_STATUS_ALPHA_FLOOR = 24


THREE_PLAYER_FRINGE_COMPENSATION = 0.10
_THREE_PLAYER_BASE_CACHE: OrderedDict[
    int, tuple[weakref.ReferenceType[Image.Image], Image.Image]
] = OrderedDict()
_THREE_PLAYER_BASE_CACHE_LIMIT = 16
_THREE_PLAYER_LAYER_CACHE: OrderedDict[
    int, tuple[weakref.ReferenceType[Image.Image], Image.Image]
] = OrderedDict()
_THREE_PLAYER_LAYER_CACHE_LIMIT = 192


def _scale_three_player_pixels(image: Image.Image) -> Image.Image:
    source = np.asarray(image.convert("RGBA"), dtype=np.uint8).copy()
    alpha = source[..., 3].astype(np.float32)
    brightness = source[..., :3].max(axis=2).astype(np.float32)
    bright_weight = np.clip((brightness - 72.0) / 150.0, 0.0, 1.0)
    fringe_weight = bright_weight * (alpha > 0.0) * (alpha < 252.0)
    alpha += (255.0 - alpha) * fringe_weight * THREE_PLAYER_FRINGE_COMPENSATION
    source[..., 3] = np.clip(alpha, 0.0, 255.0).astype(np.uint8)
    prepared = Image.fromarray(source, "RGBA")
    return prepared.resize(
        (
            max(1, int(round(prepared.width * THREE_PLAYER_PANEL_SCALE))),
            max(1, int(round(prepared.height * THREE_PLAYER_PANEL_SCALE))),
        ),
        Image.Resampling.LANCZOS,
    )


def _scale_three_player_layer(image: Image.Image) -> Image.Image:
    return image.resize(
        (
            max(1, int(round(image.width * THREE_PLAYER_PANEL_SCALE))),
            max(1, int(round(image.height * THREE_PLAYER_PANEL_SCALE))),
        ),
        Image.Resampling.LANCZOS,
    )


def _cached_three_player_image(
    cache: OrderedDict[int, tuple[weakref.ReferenceType[Image.Image], Image.Image]],
    image: Image.Image,
    scaler,
    limit: int,
) -> Image.Image:
    key = id(image)
    entry = cache.pop(key, None)
    if entry is not None and entry[0]() is image:
        cache[key] = entry
        return entry[1]

    compact = scaler(image)

    def discard(reference, *, cached_key=key, target=cache) -> None:
        current = target.get(cached_key)
        if current is not None and current[0] is reference:
            target.pop(cached_key, None)

    cache[key] = (weakref.ref(image, discard), compact)
    while len(cache) > limit:
        cache.popitem(last=False)
    return compact


def _cached_three_player_base(image: Image.Image) -> Image.Image:
    return _cached_three_player_image(
        _THREE_PLAYER_BASE_CACHE,
        image,
        _scale_three_player_pixels,
        _THREE_PLAYER_BASE_CACHE_LIMIT,
    )


def _cached_three_player_layer(image: Image.Image) -> Image.Image:
    return _cached_three_player_image(
        _THREE_PLAYER_LAYER_CACHE,
        image,
        _scale_three_player_layer,
        _THREE_PLAYER_LAYER_CACHE_LIMIT,
    )


def scale_three_player_panel(image: Image.Image) -> Image.Image:
    dynamic = image.info.get("ff12_dynamic_layers")
    if not dynamic:
        return _scale_three_player_pixels(image)

    base, layers = dynamic
    compact_base = _cached_three_player_base(base)
    compact = compact_base.copy()
    compact_layers: list[tuple[Image.Image, int, int]] = []
    for layer, x, y in layers:
        compact_layer = _cached_three_player_layer(layer)
        compact_x = int(round(x * THREE_PLAYER_PANEL_SCALE))
        compact_y = int(round(y * THREE_PLAYER_PANEL_SCALE))
        compact.alpha_composite(compact_layer, (compact_x, compact_y))
        compact_layers.append((compact_layer, compact_x, compact_y))
    compact.info["ff12_dynamic_layers"] = (
        compact_base,
        tuple(compact_layers),
    )
    return compact


MENU_EDGE_TARGET_LUMA = native.NATIVE_SPREAD_LUMA
MENU_CORE_TINT_RATIO = 0.08


def _maximum_chroma_menu_edge(
    color: tuple[int, int, int] | tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    """Return the most saturated edge that keeps the native perceived luma.

    Equal HSV values do not look equally bright: yellow carries far more
    luminance than blue or pink.  Solve in HSV for maximum saturation first,
    then maximum value, while holding the edge at the native spread luma.
    This gives every theme the same bright-core/coloured-edge separation.
    """

    alpha = color[3] if len(color) > 3 else 255
    hue, _saturation, _value = colorsys.rgb_to_hsv(
        *(channel / 255 for channel in color[:3])
    )
    pure_rgb = tuple(
        int(round(channel * 255)) for channel in colorsys.hsv_to_rgb(hue, 1.0, 1.0)
    )
    pure_luma = native.relative_luminance((*pure_rgb, 255))
    if pure_luma >= MENU_EDGE_TARGET_LUMA:
        saturation = 1.0
        value = MENU_EDGE_TARGET_LUMA / pure_luma
    else:
        saturation = (255.0 - MENU_EDGE_TARGET_LUMA) / (255.0 - pure_luma)
        value = 1.0
    edge = colorsys.hsv_to_rgb(hue, saturation, value)
    return (*(int(round(channel * 255)) for channel in edge), alpha)


def _light_theme_core(
    color: tuple[int, int, int] | tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    return (
        *(
            int(round(255 + (channel - 255) * MENU_CORE_TINT_RATIO))
            for channel in color[:3]
        ),
        255,
    )


def _boost_letter_glow(
    color: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    return (
        color[0],
        color[1],
        color[2],
        min(255, int(round(color[3] * LETTER_GLOW_GAIN))),
    )


MENU_GOLD_TEXT = native.TextStyle(
    # Yellow has much more perceived luminance than pink/blue at the same HSV
    # value.  The former 255/214/18 edge measured ~209 luma against a ~253
    # core, so both layers merged into one fuzzy yellow silhouette.  This
    # saturated amber measures ~121 luma, matching the coloured edge of the
    # other themes while retaining a clearly warm, near-white centre.
    core=(255, 252, 232, 255),
    edge=(190, 113, 0, 255),
    glow=_boost_letter_glow((190, 113, 0, 160)),
    shadow=native.GOLD_TEXT.shadow,
    edge_radius=COMMAND_EDGE_RADIUS,
    glow_radius=native.GOLD_TEXT.glow_radius,
    shadow_radius=native.GOLD_TEXT.shadow_radius,
    shadow_offset=(1.0, 2.0),
    core_inset=COMMAND_CORE_INSET,
)
MENU_BLUE_TEXT = native.TextStyle(
    core=_light_theme_core(menu_themes.get_theme("sapphire").accent),
    edge=_maximum_chroma_menu_edge(menu_themes.get_theme("sapphire").accent),
    glow=_boost_letter_glow(native.BLUE_TEXT.glow),
    shadow=native.BLUE_TEXT.shadow,
    edge_radius=COMMAND_EDGE_RADIUS,
    glow_radius=native.BLUE_TEXT.glow_radius,
    shadow_radius=native.BLUE_TEXT.shadow_radius,
    shadow_offset=(1.0, 2.0),
    core_inset=COMMAND_CORE_INSET,
)
HELP_INFO_TEXT = native.TextStyle(
    core=(255, 255, 252, 255),
    edge=(14, 17, 20, 252),
    glow=(0, 0, 0, 150),
    shadow=(0, 0, 0, 205),
    edge_radius=1.08,
    glow_radius=1.55,
    shadow_radius=1.25,
    shadow_offset=(1.0, 2.0),
    spread_dilate=1.0,
    spread_sigma=1.05,
    spread_gain=2.4,
)


def _resolved_theme(player: int, theme_name: str | None) -> str:
    return menu_themes.normalize_name(
        theme_name,
        menu_themes.default_name(player),
    )


@lru_cache(maxsize=16)
def _menu_text_style(theme_name: str) -> native.TextStyle:
    if theme_name == "gold":
        return MENU_GOLD_TEXT
    if theme_name == "sapphire":
        return MENU_BLUE_TEXT
    theme = menu_themes.get_theme(theme_name)
    return native.TextStyle(
        core=_light_theme_core(theme.accent),
        edge=_maximum_chroma_menu_edge(theme.accent),
        glow=_boost_letter_glow((*theme.accent, 180)),
        shadow=(0, 0, 0, 180),
        edge_radius=COMMAND_EDGE_RADIUS,
        glow_radius=native.BLUE_TEXT.glow_radius,
        shadow_radius=native.BLUE_TEXT.shadow_radius,
        shadow_offset=(1.0, 2.0),
        core_inset=COMMAND_CORE_INSET,
    )


PORTRAIT_ROOT = HERE / "assets" / "portraits"
VAAN_PORTRAIT_PATH = PORTRAIT_ROOT / "0-Vaan.png"
PENELO_PORTRAIT_PATH = PORTRAIT_ROOT / "5-Penelo.png"
PORTRAIT_PATH_BY_MEMBER_ID = {
    0: PORTRAIT_ROOT / "0-Vaan.png",
    1: PORTRAIT_ROOT / "1-Ashe.png",
    2: PORTRAIT_ROOT / "2-Fran.png",
    3: PORTRAIT_ROOT / "3-Balthier.png",
    4: PORTRAIT_ROOT / "4-Basch.png",
    5: PORTRAIT_ROOT / "5-Penelo.png",
    6: PORTRAIT_ROOT / "6-Reks.png",
    7: PORTRAIT_ROOT / "7-Ashe.png",
    8: PORTRAIT_ROOT / "8-Basch-(Prisoner)-(Variant-1).png",
    9: PORTRAIT_ROOT / "9-Basch-(Fugitive).png",
    10: PORTRAIT_ROOT / "10-Lamont.png",
    11: PORTRAIT_ROOT / "11-Vossler-(Judge).png",
    12: PORTRAIT_ROOT / "12-Vossler-(Knight).png",
    13: PORTRAIT_ROOT / "13-Larsa.png",
    14: PORTRAIT_ROOT / "14-Reddas.png",
}
RUNTIME_UI_PNG_ROOT = HERE / "runtime_ui_assets_png"
CURSOR_ATLAS_PATH = RUNTIME_UI_PNG_ROOT / "cursol_4_p__pic00__pal00.png"
PAUSE_ON_OFF_PATH = RUNTIME_UI_PNG_ROOT / "pause_on_off_gold.png"
LEADER_COMMAND_FLAG_PATH = RUNTIME_UI_PNG_ROOT / "vaan_command_flag.png"
INFO_ATLAS_PATH = RUNTIME_UI_PNG_ROOT / "plik_003.png"


@lru_cache(maxsize=1)
def _cursor() -> Image.Image:
    with Image.open(CURSOR_ATLAS_PATH) as source:
        return source.convert("RGBA").crop((0, 128, 64, 182))


PARTY_CURSOR_OUTLINE_BY_THEME = {
    "sapphire": (28, 151, 255),
    "gold": (255, 205, 24),
    "emerald": (28, 218, 118),
    "amethyst": (184, 75, 255),
    "pink": (255, 47, 155),
    "ruby": (255, 48, 55),
    "bronze": (222, 116, 52),
}

AREA_CURSOR_PULSE_HZ = 5.0
AREA_CURSOR_MIN_OPACITY = 0.20
AREA_CURSOR_MAX_OPACITY = 0.60


def area_cursor_opacity(now: float) -> float:
    """Native-like five-hertz pulse for secondary area-target hands."""

    midpoint = (AREA_CURSOR_MIN_OPACITY + AREA_CURSOR_MAX_OPACITY) * 0.5
    amplitude = (AREA_CURSOR_MAX_OPACITY - AREA_CURSOR_MIN_OPACITY) * 0.5
    return midpoint + amplitude * math.cos(math.tau * AREA_CURSOR_PULSE_HZ * float(now))


@lru_cache(maxsize=16)
def party_target_cursor(
    reduced: bool = False,
    theme_name: str = "pink",
) -> Image.Image:
    """Return the native hand with an outline matching the selected theme."""

    theme_name = menu_themes.normalize_name(theme_name)
    source = _cursor().copy()
    rgba = np.asarray(source, dtype=np.uint8).copy()
    rgb = rgba[..., :3]
    alpha = rgba[..., 3]
    maximum = rgb.max(axis=2)
    chroma = maximum - rgb.min(axis=2)

    # The atlas includes a wide translucent drop shadow below the hand. Seed
    # from the solid neutral outline and expand only into its antialiasing so
    # the authored shadow and the white hand remain untouched.
    rows = np.arange(source.height, dtype=np.int16)[:, None]
    seed = (maximum <= 55) & (chroma <= 7) & (alpha >= 180) & (rows <= 46)
    expanded = (
        np.asarray(
            Image.fromarray(seed.astype(np.uint8) * 255, mode="L").filter(
                ImageFilter.MaxFilter(5)
            )
        )
        > 0
    )
    outline = expanded & (alpha > 0) & (maximum <= 80) & (chroma <= 7) & (rows <= 46)
    rgba[outline, :3] = PARTY_CURSOR_OUTLINE_BY_THEME[theme_name]
    result = Image.fromarray(rgba, mode="RGBA")
    if reduced:
        result = result.resize(
            (
                int(round(result.width * 0.75)),
                int(round(result.height * 0.75)),
            ),
            Image.Resampling.LANCZOS,
        )
    return result


@lru_cache(maxsize=256)
def party_target_cursor_ghost(
    theme_name: str,
    reduced: bool,
    level: int,
) -> Image.Image | None:
    level = min(15, max(0, int(level)))
    if not level:
        return None
    ghost = party_target_cursor(reduced=reduced, theme_name=theme_name).copy()
    opacity = 0.34 * (level / 15.0)
    ghost.putalpha(ghost.getchannel("A").point(lambda value: int(value * opacity)))
    return ghost


@lru_cache(maxsize=256)
def party_area_target_cursor(
    theme_name: str,
    reduced: bool,
    opacity_level: int,
) -> Image.Image | None:
    """Return one themed secondary hand at a quantized opacity."""

    opacity_level = min(20, max(0, int(opacity_level)))
    if not opacity_level:
        return None
    cursor = party_target_cursor(reduced=reduced, theme_name=theme_name).copy()
    opacity = opacity_level / 20.0
    cursor.putalpha(
        cursor.getchannel("A").point(lambda value: int(round(value * opacity)))
    )
    return cursor


def render_party_target_cursor(
    theme_name: str,
    *,
    reduced: bool,
    position: float,
    trail: tuple[tuple[float, float], ...] = (),
    secondary_positions: tuple[float, ...] = (),
    secondary_opacity: float = AREA_CURSOR_MIN_OPACITY,
) -> Image.Image:
    """Render the HUD cursor and the same short afterimage used by menu lists."""

    cursor = party_target_cursor(reduced=reduced, theme_name=theme_name)
    row_pitch = 60.0 * (0.75 if reduced else 1.0)
    canvas = Image.new(
        "RGBA",
        (cursor.width, int(round(row_pitch * 3.0)) + cursor.height),
        (0, 0, 0, 0),
    )

    def row_y(value: float) -> int:
        return int(round(min(3.0, max(0.0, float(value))) * row_pitch))

    secondary = party_area_target_cursor(
        theme_name,
        reduced,
        int(round(min(1.0, max(0.0, float(secondary_opacity))) * 20.0)),
    )
    if secondary is not None:
        for secondary_position in dict.fromkeys(secondary_positions):
            if abs(float(secondary_position) - float(position)) < 0.001:
                continue
            canvas.alpha_composite(secondary, (0, row_y(secondary_position)))

    for trail_position, trail_opacity in trail:
        ghost = party_target_cursor_ghost(
            theme_name,
            reduced,
            int(round(float(trail_opacity) * 15.0)),
        )
        if ghost is not None:
            canvas.alpha_composite(ghost, (0, row_y(trail_position)))
    canvas.alpha_composite(cursor, (0, row_y(position)))
    return canvas


@lru_cache(maxsize=32)
def _area_target_list_cursor(opacity_level: int) -> Image.Image | None:
    opacity_level = min(20, max(0, int(opacity_level)))
    if not opacity_level:
        return None
    cursor = _cursor().copy()
    opacity = opacity_level / 20.0
    cursor.putalpha(
        cursor.getchannel("A").point(lambda value: int(round(value * opacity)))
    )
    return cursor


@lru_cache(maxsize=32)
def _portrait(
    player: int = 2,
    party_member_id: int | None = None,
) -> Image.Image:
    if party_member_id is None:
        party_member_id = 5 if player == 2 else 0
    supplied = PORTRAIT_PATH_BY_MEMBER_ID.get(
        int(party_member_id),
        PENELO_PORTRAIT_PATH if player == 2 else VAAN_PORTRAIT_PATH,
    )
    with Image.open(supplied) as image:
        source = image.convert("RGBA")
    scale = PORTRAIT_SCALE_BY_MEMBER_ID.get(int(party_member_id), PORTRAIT_SCALE)
    return source.resize(
        (
            int(round(source.width * scale)),
            int(round(source.height * scale)),
        ),
        Image.Resampling.LANCZOS,
    )


def _portrait_position(
    player: int,
    anchor_y: int,
    party_member_id: int | None = None,
) -> tuple[int, int]:
    portrait = _portrait(player, party_member_id)
    penelo_width = int(round(69 * PORTRAIT_SCALE))
    penelo_height = int(round(83 * PORTRAIT_SCALE))
    x = PORTRAIT_X - 2 + (penelo_width - portrait.width) // 2
    y = anchor_y + penelo_height - portrait.height
    return x, y


@lru_cache(maxsize=1)
def _leader_command_flag() -> Image.Image | None:
    if not LEADER_COMMAND_FLAG_PATH.is_file():
        return None
    with Image.open(LEADER_COMMAND_FLAG_PATH) as source:
        return source.convert("RGBA")


def _paste_leader_flag(
    canvas: Image.Image,
    player: int,
    anchor_y: int,
    party_member_id: int | None = None,
) -> None:
    if player != 1:
        return
    flag = _leader_command_flag()
    if flag is None:
        return
    portrait = _portrait(player, party_member_id)
    x, y = _portrait_position(player, anchor_y, party_member_id)
    canvas.alpha_composite(flag, (x - 7, y + portrait.height - 25))


@lru_cache(maxsize=2)
def _native_status(label: str) -> Image.Image | None:
    boxes = {
        "ON": (459, 281, 529, 322),
        "OFF": (545, 281, 621, 322),
    }
    box = boxes.get(label.upper())
    if box is None or not PAUSE_ON_OFF_PATH.is_file():
        return None

    with Image.open(PAUSE_ON_OFF_PATH) as source:
        atlas = source.convert("RGBA")
    status = atlas.crop(box)
    # The extracted atlas carries a low compression-alpha floor across almost
    # the entire rectangular crop. It is visually absorbed at native size, but
    # scaling the status as an independent three-player layer resamples that
    # floor into a visible box. Remove only the floor and retain the real glow.
    alpha = status.getchannel("A").point(
        lambda value: (
            0
            if value <= NATIVE_STATUS_ALPHA_FLOOR
            else (value - NATIVE_STATUS_ALPHA_FLOOR)
            * 255
            // (255 - NATIVE_STATUS_ALPHA_FLOOR)
        )
    )
    status.putalpha(alpha)
    return status


@lru_cache(maxsize=1)
def _native_question_badge() -> Image.Image | None:
    if not INFO_ATLAS_PATH.is_file():
        return None
    with Image.open(INFO_ATLAS_PATH) as source:
        atlas = source.convert("RGBA")
    badge = atlas.crop((184, 2, 242, 61))
    return badge.resize(HELP_BADGE_SIZE, Image.Resampling.LANCZOS)


def _gold_material_to_theme(
    source: Image.Image,
    theme_name: str,
) -> Image.Image:
    if theme_name == "gold":
        return source.copy()
    theme = menu_themes.get_theme(theme_name)

    pixels = []
    for red, green, blue, alpha in source.get_flattened_data():
        if red > green * 1.05 and green > blue * 1.08 and red >= 70:
            value = max(red, green, blue) / 255.0
            amount = min(1.0, max(0.0, (value - 0.10) / 0.90))
            pixels.append(
                tuple(
                    int(round(low + (high - low) * amount))
                    for low, high in zip(theme.panel_dark, theme.highlight)
                )
                + (alpha,)
            )
        else:
            pixels.append((red, green, blue, alpha))
    result = Image.new("RGBA", source.size)
    result.putdata(pixels)
    return result


@lru_cache(maxsize=16)
def _native_question_badge_theme(theme_name: str) -> Image.Image | None:
    badge = _native_question_badge()
    return None if badge is None else _gold_material_to_theme(badge, theme_name)


def cursor_jab(now: float) -> float:
    phase = (now % 0.78) / 0.78
    return math.sin(math.pi * phase / 0.18) * 4.5 if phase < 0.18 else 0.0


@lru_cache(maxsize=32)
def _cursor_ghost(level: int) -> Image.Image | None:
    level = min(15, max(0, int(level)))
    if not level:
        return None
    ghost = _cursor().copy()

    opacity = 0.34 * (level / 15.0)
    ghost.putalpha(ghost.getchannel("A").point(lambda value: int(value * opacity)))
    return ghost


def panel_height(row_count: int) -> int:
    return max(77, PANEL_VERTICAL_PADDING + max(1, row_count) * ROW_PITCH)


def target_panel_height(content_rows: int) -> int:
    rows = max(0, min(MAX_TARGET_VISIBLE_ROWS, int(content_rows)))
    if rows == 0:
        return TARGET_TITLE_ONLY_HEIGHT
    return TARGET_PANEL_HEIGHT + (rows - 1) * ROW_PITCH


def target_content_rows(
    group: str,
    candidate_count: int,
    *,
    show_party_names: bool = False,
) -> int:
    count = max(0, int(candidate_count))
    # An empty list still draws "No Available Targets". It occupies a complete
    # visual row despite not representing a selectable entry; reserving that row
    # keeps the group-switch footer out of the warning, especially in the compact
    # three-player layout.
    if not count:
        return 1
    if str(group) == "party" and not show_party_names:
        return 0
    return min(MAX_TARGET_VISIBLE_ROWS, count)


def bottom_anchor_offset(*, row_count: int | None = None, target: bool = False) -> int:
    baseline_bottom = PANEL_TOP + panel_height(5)
    if target:
        target_rows = max(0, min(MAX_TARGET_VISIBLE_ROWS, int(row_count or 0)))
        panel_bottom = PANEL_TOP + target_panel_height(target_rows)
    else:
        panel_bottom = PANEL_TOP + panel_height(max(1, int(row_count or 1)))
    return baseline_bottom - panel_bottom


@lru_cache(maxsize=1024)
def _font_text(
    label: str,
    scale: float,
    style: native.TextStyle,
) -> Image.Image:
    return FONT.render(label, scale=scale, style=style)


@lru_cache(maxsize=512)
def _text(
    label: str,
    *,
    selected: bool = False,
    scale: float = COMMAND_TEXT_SCALE,
    player: int = 2,
    theme_name: str | None = None,
) -> Image.Image:
    del selected
    return _font_text(
        label, scale, _menu_text_style(_resolved_theme(player, theme_name))
    )


@lru_cache(maxsize=1024)
def _list_text(
    label: str,
    enabled: bool,
    player: int,
    theme_name: str,
) -> Image.Image:
    sprite = _text(label, player=player, theme_name=theme_name)
    if enabled:
        return sprite
    disabled = sprite.copy()
    disabled.putalpha(disabled.getchannel("A").point(lambda value: value * 110 // 255))
    return disabled


@lru_cache(maxsize=256)
def _title_text(
    label: str,
    player: int = 2,
    theme_name: str | None = None,
) -> Image.Image:
    source = _font_text(
        label,
        TITLE_TEXT_SCALE,
        _menu_text_style(_resolved_theme(player, theme_name)),
    )
    extra = int(math.ceil(source.height * TITLE_SHEAR))
    return source.transform(
        (source.width + extra, source.height),
        Image.Transform.AFFINE,
        (1.0, TITLE_SHEAR, -TITLE_SHEAR * source.height, 0.0, 1.0, 0.0),
        resample=Image.Resampling.BICUBIC,
    )


@lru_cache(maxsize=64)
def _column_header_text(
    label: str,
    player: int = 2,
    theme_name: str | None = None,
) -> Image.Image:
    source = _font_text(
        label,
        0.76,
        _menu_text_style(_resolved_theme(player, theme_name)),
    )
    extra = int(math.ceil(source.height * TITLE_SHEAR))
    return source.transform(
        (source.width + extra, source.height),
        Image.Transform.AFFINE,
        (1.0, TITLE_SHEAR, -TITLE_SHEAR * source.height, 0.0, 1.0, 0.0),
        resample=Image.Resampling.BICUBIC,
    )


def scrollbar_geometry(
    total_rows: int,
    visible_rows: int,
    first_visible: float,
    *,
    track_top: int,
    track_bottom: int,
) -> ScrollbarGeometry | None:
    total = max(0, int(total_rows))
    visible = max(0, min(total, int(visible_rows)))
    top = int(track_top)
    bottom = max(top + 1, int(track_bottom))
    if total <= visible or visible <= 0:
        return None
    maximum_first = total - visible
    first = min(max(0.0, float(first_visible)), float(maximum_first))
    track_height = bottom - top
    thumb_height = max(18, int(round(track_height * visible / total)))
    thumb_height = min(track_height, thumb_height)
    travel = track_height - thumb_height
    progress = first / maximum_first if maximum_first else 0.0
    thumb_top = top + int(round(travel * progress))
    return ScrollbarGeometry(
        track_top=top,
        track_bottom=bottom,
        thumb_top=thumb_top,
        thumb_bottom=thumb_top + thumb_height,
    )


def _fade_overflow_sprite(sprite: Image.Image) -> Image.Image:
    if sprite.height <= 1:
        return sprite
    result = sprite.copy()
    start = max(1, int(round(sprite.height * 0.48)))
    span = max(1, sprite.height - start)
    mask = Image.new("L", (1, sprite.height), 255)
    for y in range(start, sprite.height):
        progress = (y - start) / span

        mask.putpixel((0, y), int(round(255 - 190 * progress)))
    alpha = ImageChops.multiply(
        result.getchannel("A"),
        mask.resize(result.size, Image.Resampling.BILINEAR),
    )
    result.putalpha(alpha)
    return result


@lru_cache(maxsize=256)
def _list_scrollbar_layer(
    track_top: int,
    track_bottom: int,
    thumb_top: int,
    thumb_bottom: int,
    theme_name: str,
) -> tuple[Image.Image, int, int]:
    theme = menu_themes.get_theme(theme_name)
    # The panel body ends at the bright edge at PANEL_LEFT + PANEL_WIDTH - 22;
    # everything after that is only the outer glow. With its centre at -29, the
    # 7 px bar spans -32..-26 and stays inside the frame like the native menu
    # instead of lying over the rounded edge.
    center_x = PANEL_LEFT + PANEL_WIDTH - 29
    left = center_x - 3
    top = int(track_top)
    image = Image.new(
        "RGBA",
        (7, max(1, int(track_bottom) - top + 1)),
        (0, 0, 0, 0),
    )
    draw = ImageDraw.Draw(image, "RGBA")
    center = 3
    local_bottom = int(track_bottom) - top
    local_thumb_top = int(thumb_top) - top
    local_thumb_bottom = int(thumb_bottom) - top
    draw.rounded_rectangle(
        (center - 1, 0, center + 1, local_bottom),
        radius=2,
        fill=(*theme.panel_dark, 150),
        outline=(*theme.panel_light, 105),
        width=1,
    )
    draw.rounded_rectangle(
        (center - 2, local_thumb_top, center + 2, local_thumb_bottom),
        radius=3,
        fill=(*theme.accent, 235),
        outline=(*theme.highlight, 255),
        width=1,
    )
    if local_thumb_bottom - local_thumb_top >= 8:
        draw.line(
            (
                center - 1,
                local_thumb_top + 3,
                center - 1,
                local_thumb_bottom - 3,
            ),
            fill=(*theme.highlight, 165),
            width=1,
        )
    return image, left, top


def _vertically_clipped_layer(
    sprite: Image.Image,
    x: int,
    y: int,
    top: int,
    bottom: int,
) -> tuple[Image.Image, int, int] | None:
    source_top = max(0, top - y)
    source_bottom = min(sprite.height, bottom - y)
    if source_top >= source_bottom:
        return None
    if source_top == 0 and source_bottom == sprite.height:
        return sprite, x, y
    return (
        sprite.crop((0, source_top, sprite.width, source_bottom)),
        x,
        y + source_top,
    )


def render_list(
    view: ListView,
    *,
    now: float = 0.0,
    player: int = 2,
    party_member_id: int | None = None,
    theme_name: str | None = None,
    compose_layers: bool = True,
) -> Image.Image:
    theme_name = _resolved_theme(player, theme_name)
    entries = view.entries or ("No actions available",)
    selected = min(max(0, view.selected_index), len(entries) - 1)
    max_rows = max(1, int(view.max_visible_rows))
    visible_count = min(len(entries), max_rows)
    maximum_first = max(0, len(entries) - visible_count)
    first = min(max(0, int(view.first_visible)), maximum_first)

    if selected < first:
        first = selected
    elif selected >= first + visible_count:
        first = selected - visible_count + 1
    scroll_position = (
        float(first)
        if view.scroll_position is None
        else min(
            max(0.0, float(view.scroll_position)),
            float(maximum_first),
        )
    )
    scroll_position = round(scroll_position * ROW_PITCH) / ROW_PITCH
    title_rows = 1 if view.title else 0
    height = panel_height(visible_count + title_rows)
    enabled = view.enabled or tuple(True for _ in entries)
    right = view.right_labels or {}
    base_key = (
        entries,
        view.title,
        tuple(sorted(right.items())),
        view.right_header,
        tuple(enabled),
        round(scroll_position, 4),
        max_rows,
        player,
        party_member_id,
        theme_name,
        bool(compose_layers),
    )
    base_cache = _STATIC_LIST_BASES if compose_layers else _STATIC_LAYERED_LIST_BASES
    base = base_cache.get(base_key)
    if base is None:
        chrome = _list_chrome_base(
            height,
            view.title,
            view.right_header,
            player,
            party_member_id,
            theme_name,
        )
        canvas = chrome.copy() if compose_layers else chrome._new(chrome.im)
        content_layers: list[tuple[Image.Image, int, int]] = []

        more_below = scroll_position + visible_count < len(entries) - 0.001
        draw_first = max(0, int(math.floor(scroll_position)))
        draw_last = min(
            len(entries),
            int(math.ceil(scroll_position)) + visible_count,
        )
        fade_row = min(
            len(entries) - 1,
            int(math.floor(scroll_position)) + visible_count - 1,
        )
        content_top = PANEL_TOP + ROW_CONTENT_Y + title_rows * ROW_PITCH - 4
        content_bottom = min(
            CANVAS_SIZE[1],
            PANEL_TOP + ROW_CONTENT_Y + (title_rows + visible_count) * ROW_PITCH - 2,
        )
        for absolute_row in range(draw_first, draw_last):
            label = entries[absolute_row]
            sprite = _list_text(
                label,
                absolute_row >= len(enabled) or bool(enabled[absolute_row]),
                player,
                theme_name,
            )
            fade_overflow = bool(more_below and absolute_row == fade_row)
            if fade_overflow:
                sprite = _fade_overflow_sprite(sprite)
            row_y = int(
                round(
                    PANEL_TOP
                    + ROW_CONTENT_Y
                    + (absolute_row - scroll_position + title_rows) * ROW_PITCH
                )
            )
            layer = _vertically_clipped_layer(
                sprite,
                PANEL_LEFT + 35 + LABEL_X_NUDGE.get(label, 0),
                row_y,
                content_top,
                content_bottom,
            )
            if layer is not None:
                if compose_layers:
                    canvas.alpha_composite(layer[0], (layer[1], layer[2]))
                content_layers.append(layer)
            if absolute_row in right:
                right_label = right[absolute_row]
                status = (
                    _native_status(right_label)
                    if right_label.upper() in {"ON", "OFF"}
                    else None
                )
                right_margin = 32 if status is not None else 20
                if status is None:
                    status = _font_text(
                        right_label,
                        COMMAND_TEXT_SCALE,
                        _menu_text_style(theme_name),
                    )
                if fade_overflow:
                    status = _fade_overflow_sprite(status)
                layer = _vertically_clipped_layer(
                    status,
                    PANEL_LEFT + RIGHT_LABEL_ANCHOR - status.width - right_margin,
                    row_y,
                    content_top,
                    content_bottom,
                )
                if layer is not None:
                    if compose_layers:
                        canvas.alpha_composite(layer[0], (layer[1], layer[2]))
                    content_layers.append(layer)

        scrollbar = scrollbar_geometry(
            len(entries),
            visible_count,
            scroll_position,
            track_top=(PANEL_TOP + ROW_CONTENT_Y + title_rows * ROW_PITCH + 2),
            track_bottom=(
                PANEL_TOP + ROW_CONTENT_Y + (title_rows + visible_count) * ROW_PITCH - 9
            ),
        )
        if scrollbar is not None:
            scrollbar_layer = _list_scrollbar_layer(
                scrollbar.track_top,
                scrollbar.track_bottom,
                scrollbar.thumb_top,
                scrollbar.thumb_bottom,
                theme_name,
            )
            if compose_layers:
                canvas.alpha_composite(
                    scrollbar_layer[0],
                    (scrollbar_layer[1], scrollbar_layer[2]),
                )
            content_layers.append(scrollbar_layer)
        base = canvas
        base.info["ff12_dynamic_layers"] = (chrome, tuple(content_layers))
    base = _remember_static_base(
        base_cache,
        base_key,
        base,
        _LAYERED_LIST_BASE_LIMIT if not compose_layers else _STATIC_BASE_LIMIT,
    )
    canvas = base.copy() if compose_layers else base._new(base.im)
    base_dynamic = base.info.get("ff12_dynamic_layers")
    if base_dynamic:
        transport_base, base_layers = base_dynamic
        dynamic_layers = list(base_layers)
    else:
        transport_base = base
        dynamic_layers = []
    cursor_position = (
        float(selected) - scroll_position
        if view.cursor_position is None
        else view.cursor_position - scroll_position
    )
    for trail_position, trail_opacity in view.cursor_trail:
        trail_y = (
            int(
                round(
                    PANEL_TOP
                    + 4
                    + (trail_position - scroll_position + title_rows) * ROW_PITCH
                )
            )
            + CURSOR_Y_OFFSET
        )
        ghost = _cursor_ghost(int(round(trail_opacity * 15.0)))
        if ghost is not None:
            ghost_position = (
                CURSOR_X_OFFSET + int(round(cursor_jab(now))),
                trail_y,
            )
            if compose_layers:
                canvas.alpha_composite(
                    ghost,
                    ghost_position,
                )
            dynamic_layers.append((ghost, *ghost_position))
    hand_y = (
        int(round(PANEL_TOP + 4 + (cursor_position + title_rows) * ROW_PITCH))
        + CURSOR_Y_OFFSET
    )
    cursor = _cursor()
    cursor_position_xy = (
        CURSOR_X_OFFSET + int(round(cursor_jab(now))),
        hand_y,
    )
    if compose_layers:
        canvas.alpha_composite(
            cursor,
            cursor_position_xy,
        )
    dynamic_layers.append((cursor, *cursor_position_xy))
    canvas.info["ff12_dynamic_layers"] = (
        transport_base,
        tuple(dynamic_layers),
    )
    return canvas


def render_target(
    view: TargetView,
    *,
    now: float = 0.0,
    player: int = 2,
    party_member_id: int | None = None,
    theme_name: str | None = None,
) -> Image.Image:
    theme_name = _resolved_theme(player, theme_name)
    canvas = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
    top = PANEL_TOP
    party_world_only = (
        view.group == "party" and view.has_target and not view.show_party_names
    )
    names = () if party_world_only else view.target_names
    if not names and view.has_target and view.target_name:
        names = (view.target_name,)
    selected = view.selected_index % len(names) if names else 0
    content_rows = target_content_rows(
        view.group,
        len(names),
        show_party_names=view.show_party_names,
    )
    visible_rows = max(1, content_rows)
    maximum_first = max(0, len(names) - visible_rows)
    first = min(max(0, selected - visible_rows + 1), maximum_first)
    if selected < first:
        first = selected
    visible_names = names[first : first + visible_rows]
    height = target_panel_height(content_rows)
    panel = native.native_panel(
        (TARGET_PANEL_WIDTH, height),
        palette=theme_name,
    )
    canvas.alpha_composite(panel, (PANEL_LEFT - 3, top - 2))
    canvas.alpha_composite(
        _portrait(player, party_member_id),
        _portrait_position(player, top - 73 + PORTRAIT_Y_OFFSET, party_member_id),
    )
    _paste_leader_flag(
        canvas,
        player,
        top - 73 + PORTRAIT_Y_OFFSET,
        party_member_id,
    )

    action = _title_text(view.action_name, player, theme_name)
    canvas.alpha_composite(action, (PANEL_LEFT + 20, top + 11))
    dynamic_layers: list[tuple[Image.Image, int, int]] = []
    if not party_world_only and view.has_target:
        for row, name in enumerate(visible_names):
            target = _text(name, player=player, theme_name=theme_name)
            canvas.alpha_composite(
                target, (PANEL_LEFT + 37, top + 58 + row * ROW_PITCH)
            )
    elif not party_world_only:
        pulse = 0.28 + 0.72 * (0.5 + 0.5 * math.sin(now * 1.55))
        warning = _font_text("No Available Targets", 0.72, native.RED_TEXT).copy()
        warning.putalpha(
            warning.getchannel("A").point(lambda value: int(value * pulse))
        )
        dynamic_layers.append((warning, PANEL_LEFT + 37, top + 58))
    if not party_world_only and view.has_target and bool(visible_names):
        secondary_cursor = _area_target_list_cursor(
            int(round(area_cursor_opacity(now) * 20.0))
        )
        if secondary_cursor is not None:
            visible_secondary = {
                int(index)
                for index in view.secondary_indices
                if first <= int(index) < first + len(visible_names)
                and int(index) != selected
            }
            for index in sorted(visible_secondary):
                cursor_y = top + 50 + (index - first) * ROW_PITCH + CURSOR_Y_OFFSET
                dynamic_layers.append(
                    (
                        secondary_cursor,
                        CURSOR_X_OFFSET + int(round(cursor_jab(now))),
                        cursor_y,
                    )
                )
        for trail_position, trail_opacity in view.cursor_trail:
            ghost = _cursor_ghost(int(round(float(trail_opacity) * 15.0)))
            if ghost is None:
                continue
            cursor_y = int(
                round(
                    top
                    + 50
                    + (float(trail_position) - first) * ROW_PITCH
                    + CURSOR_Y_OFFSET
                )
            )
            dynamic_layers.append(
                (
                    ghost,
                    CURSOR_X_OFFSET + int(round(cursor_jab(now))),
                    cursor_y,
                )
            )
        cursor_position = (
            float(selected)
            if view.cursor_position is None
            else float(view.cursor_position)
        )
        cursor_y = int(
            round(
                top
                + 50
                + (cursor_position - first) * ROW_PITCH
                + CURSOR_Y_OFFSET
            )
        )
        dynamic_layers.append(
            (
                _cursor(),
                CURSOR_X_OFFSET + int(round(cursor_jab(now))),
                cursor_y,
            )
        )

    available_groups = tuple(dict.fromkeys(view.available_groups))

    if len(available_groups) > 1:
        draw = ImageDraw.Draw(canvas)
        footer_y = top + height - 19
        left_active = view.group == "foes"
        neutral_active = view.group == "neutrals"
        party_active = view.group == "party"
        reserve_active = view.group == "reserve"
        draw_keycap(
            draw,
            (PANEL_LEFT + 95, footer_y),
            view.left_prompt,
            left_active,
            player=player,
            theme_name=theme_name,
        )
        active_style = _menu_text_style(theme_name)
        if "reserve" in available_groups:
            compact_groups = (
                ("foes", "Foes", PANEL_LEFT + 131, left_active),
                ("neutrals", "Allies", PANEL_LEFT + 184, neutral_active),
                ("party", "Party", PANEL_LEFT + 244, party_active),
                ("reserve", "Reserve", PANEL_LEFT + 294, reserve_active),
            )
            for key, label, x, active in compact_groups:
                if key not in available_groups:
                    continue
                sprite = _font_text(
                    label,
                    0.50,
                    active_style if active else native.WHITE_TEXT,
                )
                canvas.alpha_composite(sprite, (x, footer_y - 1))
        else:
            if "foes" in available_groups:
                foes = _font_text(
                    "Foes", 0.60, active_style if left_active else native.WHITE_TEXT
                )
                canvas.alpha_composite(foes, (PANEL_LEFT + 131, footer_y - 3))
            if "neutrals" in available_groups:
                neutrals = _font_text(
                    "Allies",
                    0.60,
                    active_style if neutral_active else native.WHITE_TEXT,
                )
                canvas.alpha_composite(neutrals, (PANEL_LEFT + 202, footer_y - 3))
            if "party" in available_groups:
                party = _font_text(
                    "Party", 0.60, active_style if party_active else native.WHITE_TEXT
                )
                canvas.alpha_composite(party, (PANEL_LEFT + 282, footer_y - 3))
        draw_keycap(
            draw,
            (PANEL_LEFT + 354, footer_y),
            view.right_prompt,
            party_active or reserve_active,
            player=player,
            theme_name=theme_name,
        )
    base_key = (
        view.action_name,
        view.target_name,
        bool(view.has_target),
        view.group,
        tuple(names),
        selected,
        first,
        view.left_prompt,
        view.right_prompt,
        available_groups,
        bool(view.show_party_names),
        player,
        party_member_id,
        theme_name,
    )
    base = _remember_static_base(_STATIC_TARGET_BASES, base_key, canvas)
    canvas = base.copy()
    for layer, x, y in dynamic_layers:
        canvas.alpha_composite(layer, (x, y))
    if dynamic_layers:
        canvas.info["ff12_dynamic_layers"] = (
            base,
            tuple(dynamic_layers),
        )
    return canvas


def draw_keycap(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    label: str,
    active: bool,
    *,
    player: int = 2,
    theme_name: str | None = None,
) -> None:
    x, y = xy
    width = max(28, 12 + len(label) * 8)
    theme = menu_themes.get_theme(_resolved_theme(player, theme_name))
    active_fill = (*theme.accent, 238)
    active_outline = (*theme.highlight, 248)
    fill = active_fill if active else (37, 34, 29, 235)
    outline = active_outline if active else (180, 177, 165, 225)
    draw.rounded_rectangle(
        (x, y, x + width, y + 25), radius=4, fill=fill, outline=outline, width=2
    )

    draw.text(
        (x + width / 2, y + 12),
        label,
        fill=(*theme.key_text, 255) if active else (245, 245, 240, 255),
        anchor="mm",
    )


@lru_cache(maxsize=512)
def render_help(
    description: str,
    *,
    player: int = 2,
    theme_name: str | None = None,
) -> Image.Image:
    theme_name = _resolved_theme(player, theme_name)
    theme = menu_themes.get_theme(theme_name)
    help_scale = 0.98
    width = max(720, FONT.text_width(description, help_scale) + 80)
    image = Image.new("RGBA", (width, 58), (0, 0, 0, 0))
    badge = _native_question_badge_theme(theme_name)
    if badge is not None:
        image.alpha_composite(badge, (0, HELP_BADGE_Y))
    else:
        draw = ImageDraw.Draw(image)
        badge_fill = (*theme.highlight, 255)
        badge_edge = (*theme.panel_dark, 255)
        draw.rounded_rectangle((4, 8, 45, 49), radius=9, fill=(0, 0, 0, 170))
        draw.rounded_rectangle(
            (1, 4, 42, 45),
            radius=9,
            fill=badge_fill,
            outline=badge_edge,
            width=3,
        )
        question = _font_text("?", 0.78, native.WHITE_TEXT)

        dark = Image.new("RGBA", question.size, (24, 19, 11, 0))
        dark.putalpha(question.getchannel("A"))
        image.alpha_composite(dark, (12, 4))
    help_text = _font_text(description, help_scale, HELP_INFO_TEXT)
    image.alpha_composite(help_text, (HELP_TEXT_X, HELP_TEXT_Y))
    return image


def transition(
    image: Image.Image,
    progress: float,
    *,
    opening: bool,
    p2_mirrored: bool = True,
) -> TransitionFrame:
    progress = min(1.0, max(0.0, float(progress)))
    eased = 1.0 - (1.0 - progress) ** 3
    visible = eased if opening else 1.0 - eased
    direction = 1.0 if p2_mirrored else -1.0
    slide = direction * TRANSITION_SLIDE_PX * ((1.0 - eased) if opening else eased)

    return TransitionFrame(image, slide, visible)


def page_transition(
    old_image: Image.Image,
    new_image: Image.Image,
    progress: float,
    *,
    forward: bool,
) -> Image.Image:
    progress = min(1.0, max(0.0, float(progress)))
    eased = progress * progress * (3.0 - 2.0 * progress)
    direction = 1 if forward else -1
    del old_image
    result = new_image.copy()
    result.info.update(new_image.info)
    result.info["ff12_local_offset_y"] = (
        direction * PAGE_TRANSITION_SLIDE_PX * (1.0 - eased)
    )
    result.info["ff12_local_opacity"] = 0.62 + 0.38 * eased
    return result


def target_group_transition(
    image: Image.Image,
    progress: float,
    *,
    initial_offset_y: float,
) -> Image.Image:
    progress = min(1.0, max(0.0, float(progress)))
    eased = progress * progress * (3.0 - 2.0 * progress)
    result = image.copy()
    result.info.update(image.info)
    result.info["ff12_local_offset_y"] = initial_offset_y * (1.0 - eased)

    result.info["ff12_local_opacity"] = 0.72 + 0.28 * eased
    return result


__all__ = [
    "CANVAS_SIZE",
    "CURSOR_Y_OFFSET",
    "ListView",
    "PANEL_LEFT",
    "PANEL_TOP",
    "PANEL_WIDTH",
    "PAGE_TRANSITION_SECONDS",
    "TARGET_GROUP_TRANSITION_SECONDS",
    "ROW_PITCH",
    "bottom_anchor_offset",
    "TRANSITION_SECONDS",
    "TRANSITION_SLIDE_PX",
    "TargetView",
    "TransitionFrame",
    "cursor_jab",
    "party_target_cursor",
    "party_target_cursor_ghost",
    "party_area_target_cursor",
    "render_party_target_cursor",
    "area_cursor_opacity",
    "panel_height",
    "render_help",
    "render_list",
    "render_target",
    "page_transition",
    "target_group_transition",
    "transition",
]
