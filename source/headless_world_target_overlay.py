from __future__ import annotations

import math
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFilter

import menu_themes
import native_ui_assets as native


REFERENCE_WIDTH = 1920
REFERENCE_HEIGHT = 1080
RING_SAMPLES = 64
TARGET_CENTER_HEIGHT_FACTOR = 0.54


TARGET_RADIUS_FACTOR = 1.34
TARGET_GOLD_RGB = (255, 230, 0)
# The HUD outline is dark but vivid. Lowering every channel preserves hue while
# draining colour into a muddy blue. Saturating first, by removing the common
# grey component and restoring the peak channel, keeps colour strong as luma falls.
TARGET_OUTLINE_GREY_REMOVAL = 0.85
TARGET_OUTLINE_PEAK = 0.64


TARGET_BAR_WHITEN = 0.88

BAR_FRAME_WIDTH = 2


def _dark_luminous(
    color: tuple[int, int, int], peak: float = TARGET_OUTLINE_PEAK
) -> tuple[int, int, int]:
    floor = min(color)
    saturated = [
        max(0.0, channel - floor * TARGET_OUTLINE_GREY_REMOVAL) for channel in color
    ]
    highest = max(saturated)
    if highest <= 0.0:
        return (0, 0, 0)
    scale = (max(color) * peak) / highest
    return tuple(min(255, max(0, int(round(channel * scale)))) for channel in saturated)


@dataclass(frozen=True)
class LiveTargetInfo:
    name: str
    actor: int
    handle: int
    position: tuple[float, float, float]
    level: int
    current_hp: int
    max_hp: int
    collision_height: float = 1.79
    collision_radius: float = 0.5
    reveal_stats: bool = True

    flying: bool = False

    anti_libra: bool = False
    weak_mask: int = 0

    status_mask: int = 0

    viewer_has_libra: bool = False

    # Level of the player who opened the card and whether the target is hostile.
    # These only make the name red when the enemy greatly outlevels the player;
    # the numeric level remains hidden without Libra.
    viewer_level: int = 0
    hostile: bool = False

    @property
    def is_ko(self) -> bool:
        return self.current_hp == 0 or bool(self.status_mask & 1)


@dataclass
class TargetHpAnimator:
    duration: float = 0.42
    handle: int = 0
    actor: int = 0
    maximum: int = 0
    source: float = 0.0
    destination: int = 0
    started: float = 0.0

    def clear(self) -> None:
        self.handle = 0
        self.actor = 0
        self.maximum = 0
        self.source = 0.0
        self.destination = 0
        self.started = 0.0

    def _sample_value(self, now: float) -> float:
        if self.started <= 0.0 or self.duration <= 0.0:
            return float(self.destination)
        progress = min(1.0, max(0.0, now - self.started) / self.duration)

        eased = progress * progress * (3.0 - 2.0 * progress)
        value = self.source + (self.destination - self.source) * eased
        if progress >= 1.0:
            self.source = float(self.destination)
            self.started = 0.0
        return value

    def present(self, info: LiveTargetInfo, now: float) -> LiveTargetInfo:
        actual = max(0, int(info.current_hp))
        maximum = max(1, int(info.max_hp))
        identity_changed = (
            int(info.handle) != self.handle
            or int(info.actor) != self.actor
            or maximum != self.maximum
        )
        if identity_changed:
            self.handle = int(info.handle)
            self.actor = int(info.actor)
            self.maximum = maximum
            self.source = float(actual)
            self.destination = actual
            self.started = 0.0
            return info

        displayed = self._sample_value(float(now))
        if actual != self.destination:
            self.source = displayed
            self.destination = actual
            self.started = float(now)
            displayed = self.source

        displayed = self._sample_value(float(now))
        visual_hp = max(0, int(round(displayed)))
        return replace(info, current_hp=visual_hp)


WEAK_ELEMENTS = (
    (0x01, "fire"),
    (0x02, "lightning"),
    (0x04, "ice"),
    (0x08, "earth"),
    (0x10, "water"),
    (0x20, "wind"),
    (0x40, "holy"),
    (0x80, "dark"),
)

# Bits from the word at bkbi+0x3c, in the game's status order. Libra occupies
# index 30 and reads the corresponding bit.
#
# Every status icon was identified manually against the atlas rather than
# derived: the game stores no status-to-rectangle table, the executable contains
# none of the measured rectangles in any encoding, and the packed atlas has no
# readable ordering. KO, Petrify, HP Critical and X-Zone have no icon, so 32
# statuses occupy 28 of the 29 sprites.
STATUS_EFFECTS = (
    (0, "ko", None, False),
    (1, "Stone", "stone", False),
    (2, "Petrify", None, False),
    (3, "Stop", "stop", False),
    (4, "Sleep", "sleep", False),
    (5, "Confuse", "confuse", False),
    (6, "Doom", "doom", False),
    (7, "Blind", "blind", False),
    (8, "Poison", "poison", False),
    (9, "Silence", "silence", False),
    (10, "Sap", "sap", False),
    (11, "Oil", "oil", False),
    (12, "Reverse", "reverse", False),
    (13, "Disable", "disable", False),
    (14, "Immobilize", "immobilize", False),
    (15, "Slow", "slow", False),
    (16, "Disease", "disease", False),
    (17, "Lure", "lure", True),
    (18, "Protect", "protect", True),
    (19, "Shell", "shell", True),
    (20, "Haste", "haste", True),
    (21, "Bravery", "bravery", True),
    (22, "Faith", "faith", True),
    (23, "Reflect", "reflect", True),
    (24, "Invisible", "invisible", True),
    (25, "Regen", "regen", True),
    (26, "Float", "float", True),
    (27, "Berserk", "berserk", True),
    (28, "Bubble", "bubble", True),
    (29, "HP Critical", None, False),
    (30, "Libra", "libra", True),
    (31, "X-Zone", None, False),
)


STATUS_REVIEW_EFFECTS = ()


def status_review_mask() -> int:
    bits = {name: bit for bit, name, _icon, _good in STATUS_EFFECTS}
    return sum(1 << bits[name] for name in STATUS_REVIEW_EFFECTS)


def status_icon_names(mask: int, beneficial: bool) -> list[str]:
    if STATUS_REVIEW_EFFECTS:
        mask = status_review_mask()
    return [
        icon
        for bit, _name, icon, good in STATUS_EFFECTS
        if icon and good is beneficial and mask & (1 << bit)
    ]


def status_icon_sequence(mask: int) -> list[str]:
    return status_icon_names(mask, beneficial=True) + status_icon_names(
        mask, beneficial=False
    )


def status_icon_scale() -> float:
    return TRAIT_ICON_SCALE * panel_scale() * FRAMED_EFFECT_ICON_SCALE


def status_line_width() -> int:
    return _px(CARD_BOX_WIDTH) - 2 * _px(FRAMED_PAD_X)


def _status_icon_width(name: str) -> int | None:
    sprite = trait_sprite(f"status/{name}")
    if sprite is None:
        return None
    return max(1, int(sprite.width * status_icon_scale()))


def _wrap_status_icons(names: Sequence[str]) -> list[list[str]]:
    gap = _px(WEAK_ICON_GAP)
    limit = status_line_width()
    lines: list[list[str]] = []
    line: list[str] = []
    used = 0
    for name in names:
        width = _status_icon_width(name)
        if width is None:
            continue
        needed = used + gap + width if line else width
        if line and needed > limit:
            lines.append(line)
            line, used = [name], width
        else:
            line.append(name)
            used = needed
    if line:
        lines.append(line)
    return lines


def status_effect_lines(mask: int) -> list[list[str]]:
    return _wrap_status_icons(status_icon_sequence(mask))


def max_status_lines() -> int:
    icons = [icon for _bit, _name, icon, good in STATUS_EFFECTS if icon and good]
    icons += [icon for _bit, _name, icon, good in STATUS_EFFECTS if icon and not good]
    return len(_wrap_status_icons(icons))


SHOW_ALL_WEAK_ELEMENTS = False


CARD_OWNED_WIDTH = 328
WEAK_ROW_HEIGHT = 38

# The framed layout uses a dark background and a player-coloured border.
TARGET_PANEL_STYLE = "framed"

# Overall card size, with 1.0 as the authored proportion. Every measurement
# below is multiplied by this value during glyph and sprite composition.

FRAMED_SCALE = 0.866

# The slab must end within the card width or it lies under the next card; the
# remainder becomes the inter-card gap. This unscaled authored width is also
# the ceiling that FRAMED_SCALE = 1.0 must respect.
CARD_BOX_WIDTH = 318
TARGET_BACKDROP_FADE_WIDTH = 112
TARGET_BACKDROP_FADE_LEAD = 34
TARGET_BACKDROP_PEAK_ALPHA = 98
TARGET_BACKDROP_RGB = (7, 9, 11)
TARGET_BACKDROP_TOP_FEATHER = 4
TARGET_BACKDROP_BOTTOM_FEATHER = 14
FRAMED_CORNER = 12
FRAMED_PAD_X = 16
FRAMED_NAME_TOP = 13
FRAMED_HP_TOP = 55
FRAMED_BAR_HEIGHT = 7


FRAMED_BAR_BOTTOM_MARGIN = 30

FRAMED_HP_BLOCK = 45


FRAMED_EFFECT_ROW_HEIGHT = 34
FRAMED_EFFECT_BAND_EMPTY = 8
# Status icons were much smaller when an entire polarity had to fit one row.
# Wrapping lets them read at the Weak-icon size again; the source sprites are
# slightly larger, hence a scale below 1.0.
FRAMED_EFFECT_ICON_SCALE = 0.88
FRAMED_BORDER_WIDTH = 2


FRAMED_NAME_SCALE = 0.62
FRAMED_LEVEL_SCALE = 0.60

# On the 318 px strip, a 0.69 scale uses one third of the title row.

FRAMED_FLYING_SCALE = 0.45
FRAMED_CURRENT_HP_SCALE = 1.00
FRAMED_MAXIMUM_HP_SCALE = 0.74


FRAMED_BAR_WHITEN = 0.42

FRAMED_TITLE_GAP = 10


FRAMED_DRAW_SLAB = False


FRAMED_BODY_ALPHA = 177
# The "TARGET INFO" label sits above the slab so the box retains four content
# rows. It is smaller than classic's 0.70 because that size and wide letter
# spacing left only 26 px of slab for the decorative lines.
FRAMED_HEADER_SCALE = 0.55
FRAMED_HEADER_GAP = 3


FRAMED_HEADER_RULE_GAP = 7
FRAMED_HEADER_RULE_INSET = 3
FRAMED_HEADER_RULE_WIDTH = 2


FRAMED_LIBRA_DROP = 0

TRAIT_SPRITE_DIR = (
    Path(__file__).resolve().parent / "runtime_ui_assets_png" / "target_traits"
)
TRAIT_ICON_SCALE = 0.63


WEAK_ICON_DROP = 5

WEAK_ICON_GAP = 3
FLYING_ROW_Y = 88


FLYING_RIGHT_EDGE = 348
_TRAIT_SPRITES: dict[str, Image.Image | None] = {}


def trait_sprite(name: str) -> Image.Image | None:
    if name not in _TRAIT_SPRITES:
        if name == "anti_libra":
            libra = trait_sprite("status/libra")
            if libra is None:
                _TRAIT_SPRITES[name] = None
            else:
                badge = libra.copy()
                draw = ImageDraw.Draw(badge)
                width = max(2, round(min(badge.size) * 0.12))
                margin = max(1, round(min(badge.size) * 0.08))
                endpoints = (
                    margin,
                    badge.height - margin,
                    badge.width - margin,
                    margin,
                )
                draw.line(endpoints, fill=(48, 8, 8, 255), width=width + 2)
                draw.line(endpoints, fill=(239, 64, 52, 255), width=width)
                _TRAIT_SPRITES[name] = badge
            return _TRAIT_SPRITES[name]
        path = TRAIT_SPRITE_DIR / f"{name}.png"
        try:
            with Image.open(path) as source:
                _TRAIT_SPRITES[name] = source.convert("RGBA")
        except OSError:
            _TRAIT_SPRITES[name] = None
    return _TRAIT_SPRITES[name]


def weak_element_names(mask: int) -> list[str]:
    if SHOW_ALL_WEAK_ELEMENTS and mask:
        return [name for _bit, name in WEAK_ELEMENTS]
    return [name for bit, name in WEAK_ELEMENTS if mask & bit]


@dataclass(frozen=True)
class PositionedImage:
    image: Image.Image
    x: float
    y: float


def projected_ring_center(
    points: Sequence[tuple[float, float]],
) -> tuple[float, float] | None:
    if not points:
        return None
    return (
        (min(point[0] for point in points) + max(point[0] for point in points)) * 0.5,
        (min(point[1] for point in points) + max(point[1] for point in points)) * 0.5,
    )


def interpolate_projected_ring(
    source: Sequence[tuple[float, float]],
    destination: Sequence[tuple[float, float]],
    progress: float,
) -> tuple[tuple[float, float], ...]:
    if not destination:
        return ()
    if len(source) != len(destination):
        return tuple(destination)
    amount = min(1.0, max(0.0, float(progress)))
    return tuple(
        (
            start[0] + (finish[0] - start[0]) * amount,
            start[1] + (finish[1] - start[1]) * amount,
        )
        for start, finish in zip(source, destination)
    )


def translate_positioned(
    frame: PositionedImage, delta_x: float, delta_y: float
) -> PositionedImage:
    return PositionedImage(frame.image, frame.x + delta_x, frame.y + delta_y)


def is_probable_minimap_view(view: Sequence[float]) -> bool:
    if len(view) != 16:
        return False
    return abs(view[9]) > 0.82 and abs(view[10]) < 0.60 and abs(view[6]) > 0.82


def transform_row_vector(
    vector: Sequence[float], matrix: Sequence[float]
) -> tuple[float, float, float, float]:
    if len(vector) != 4 or len(matrix) != 16:
        raise ValueError("expected a vec4 and a 4x4 matrix")
    return tuple(
        sum(float(vector[row]) * float(matrix[row * 4 + column]) for row in range(4))
        for column in range(4)
    )  # type: ignore[return-value]


def project_point(
    point: Sequence[float],
    view: Sequence[float],
    projection: Sequence[float],
    *,
    width: int = REFERENCE_WIDTH,
    height: int = REFERENCE_HEIGHT,
) -> tuple[float, float, float] | None:
    camera = transform_row_vector((*point, 1.0), view)
    clip = transform_row_vector(camera, projection)
    if not all(math.isfinite(value) for value in clip) or clip[3] <= 0.01:
        return None
    ndc_x = clip[0] / clip[3]
    ndc_y = clip[1] / clip[3]
    ndc_z = clip[2] / clip[3]
    return (
        (ndc_x + 1.0) * 0.5 * width,
        (1.0 - ndc_y) * 0.5 * height,
        ndc_z,
    )


def project_actor_point(
    point: Sequence[float],
    view: Sequence[float],
    projection: Sequence[float],
    *,
    width: int = REFERENCE_WIDTH,
    height: int = REFERENCE_HEIGHT,
):
    if len(point) != 3:
        raise ValueError("expected an actor XYZ position")
    return project_point(
        (-float(point[0]), -float(point[1]), float(point[2])),
        view,
        projection,
        width=width,
        height=height,
    )


def projected_actor_ring(
    info: LiveTargetInfo,
    view: Sequence[float],
    projection: Sequence[float],
) -> tuple[tuple[float, float], ...]:
    capsule_height = float(info.collision_height)
    capsule_radius = float(info.collision_radius)
    if not math.isfinite(capsule_height) or not 0.1 <= capsule_height <= 100.0:
        capsule_height = 1.79
    if not math.isfinite(capsule_radius) or not 0.05 <= capsule_radius <= 50.0:
        capsule_radius = 0.5

    center_world = (
        info.position[0],
        info.position[1] + capsule_height * TARGET_CENTER_HEIGHT_FACTOR,
        info.position[2],
    )

    # Points remain fixed during a frame. Rebuilding the DIB for every sample
    # made the target appear slower than the native marker; the glow adds motion.
    radius = capsule_radius * TARGET_RADIUS_FACTOR
    points: list[tuple[float, float]] = []
    for index in range(RING_SAMPLES):
        angle = math.tau * index / RING_SAMPLES
        point = (
            center_world[0] + math.cos(angle) * radius,
            center_world[1],
            center_world[2] + math.sin(angle) * radius,
        )
        projected = project_actor_point(point, view, projection)
        if projected is None:
            return ()
        points.append(projected[:2])
    return tuple(points)


def projected_ring_stroke_widths(
    points: Sequence[tuple[float, float]],
) -> tuple[int, int, int, int]:
    if len(points) < 3:
        return 12, 7, 5, 1
    width = max(point[0] for point in points) - min(point[0] for point in points)
    height = max(point[1] for point in points) - min(point[1] for point in points)
    minor = max(1.0, min(width, height))
    # The native lock is a broad world-space ribbon. The old 14.5% band still
    # looked like a thin vector outline beside the game's ring, especially after
    # perspective flattened the ellipse. A generous floor scaled by the projected
    # minor axis gives large or nearby actors the same apparent thickness.
    body = max(12, min(34, int(round(minor * 0.22))))
    glow = max(body + 7, int(round(body * 1.80)))
    gold = max(8, int(round(body * 0.76)))
    hot = max(2, int(round(body * 0.22)))
    return glow, body, gold, hot


def transform_positioned(
    frame: PositionedImage, *, scale: float = 1.0, opacity: float = 1.0
) -> PositionedImage:
    scale = max(0.01, float(scale))
    opacity = min(1.0, max(0.0, float(opacity)))
    width = max(1, int(round(frame.image.width * scale)))
    height = max(1, int(round(frame.image.height * scale)))
    image = frame.image
    if (width, height) != image.size:
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    else:
        image = image.copy()
    if opacity < 0.999:
        image.putalpha(image.getchannel("A").point(lambda value: int(value * opacity)))
    center_x = frame.x + frame.image.width * 0.5
    center_y = frame.y + frame.image.height * 0.5
    return PositionedImage(image, center_x - width * 0.5, center_y - height * 0.5)


def render_projected_ring(
    points: Sequence[tuple[float, float]], *, now: float = 0.0
) -> PositionedImage | None:
    if len(points) < 3:
        return None

    glow_width, body_width, gold_width, hot_width = projected_ring_stroke_widths(points)
    margin = max(24, glow_width + 12)
    left = math.floor(min(point[0] for point in points)) - margin
    top = math.floor(min(point[1] for point in points)) - margin
    right = math.ceil(max(point[0] for point in points)) + margin
    bottom = math.ceil(max(point[1] for point in points)) + margin
    if right < 0 or bottom < 0 or left > REFERENCE_WIDTH or top > REFERENCE_HEIGHT:
        return None
    if right - left > REFERENCE_WIDTH * 2 or bottom - top > REFERENCE_HEIGHT * 2:
        return None

    local = [(x - left, y - top) for x, y in points]
    local.append(local[0])
    size = (max(1, right - left), max(1, bottom - top))

    image = Image.new("RGBA", size, (0, 0, 0, 0))

    ImageDraw.Draw(image).polygon(local[:-1], fill=(*TARGET_GOLD_RGB, 10))

    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    pulse = 0.84 + 0.16 * (0.5 + 0.5 * math.sin(now * 5.4))
    glow_draw.line(
        local,
        fill=(*TARGET_GOLD_RGB, int(142 * pulse)),
        width=glow_width,
        joint="curve",
    )
    glow = glow.filter(ImageFilter.GaussianBlur(max(2.5, glow_width * 0.28)))

    image.alpha_composite(glow)
    draw = ImageDraw.Draw(image)
    draw.line(local, fill=(67, 48, 0, 242), width=body_width, joint="curve")
    draw.line(local, fill=(*TARGET_GOLD_RGB, 255), width=gold_width, joint="curve")
    draw.line(local, fill=(255, 250, 158, 248), width=hot_width, joint="curve")
    return PositionedImage(image, float(left), float(top))


FONT = native.BitmapFont()

HEADER_TEXT = native.TextStyle(
    core=(201, 202, 198, 245),
    edge=(80, 82, 80, 220),
    glow=(0, 0, 0, 125),
    shadow=(0, 0, 0, 185),
    edge_radius=0.8,
    glow_radius=1.3,
    shadow_radius=1.2,
    shadow_offset=(1.0, 1.0),
)

BLUE_TEXT = native.TextStyle(
    core=(248, 253, 255, 255),
    edge=(92, 183, 255, 250),
    glow=(20, 91, 226, 190),
    shadow=(0, 0, 0, 175),
    edge_radius=0.82,
    glow_radius=1.75,
    shadow_radius=1.45,
    shadow_offset=(1.0, 2.0),
)


@lru_cache(maxsize=16)
def target_theme_style(theme_name: str) -> native.TextStyle:
    theme = menu_themes.get_theme(theme_name)

    outline = _dark_luminous(theme.accent)
    return native.TextStyle(
        core=native.NATIVE_TEXT_CORE,
        edge=(*outline, 255),
        glow=(*outline, 180),
        shadow=(0, 0, 0, 180),
        # The menu's broad soft spread looks blurred on this smaller HUD text. A
        # dense tight outline restores native sharpness: almost no blur, with gain
        # lifting the remainder to full opacity so it reads as a line, not a halo.
        spread_dilate=1.0,
        spread_sigma=0.55,
        spread_gain=2.8,
    )


# Compatibility for development prototypes made before this became public.
_target_theme_style = target_theme_style


# Use a red name when the enemy greatly outlevels the player.
#
# This applies with or without Libra: both levels are always read, and only the
# displayed NUMBER depends on revelation. Party members never turn red, hence
# the `hostile` guard.
#
# This threshold belongs to the mod. All 84 executable reads of
# `battle_keep + 0x1C2` were inspected and none enters the UI region (0x3E-0x40),
# which means the game decides the colour earlier and stores the result. Tracing
# that flag further requires a live game. Until then, ten levels is the mod's
# rule, and changing this value changes that rule globally.
DANGEROUS_LEVEL_GAP = 10

DANGEROUS_NAME_COLOR = (228, 42, 46)


@lru_cache(maxsize=1)
def ko_badge() -> Image.Image:
    # Reuse the native skull already shipped with the Pure status sprites.
    skull = trait_sprite('status/doom')
    if skull is None:
        raise FileNotFoundError('native skull asset status/doom.png is missing')
    scale = status_icon_scale()
    return skull.resize(
        (max(1, round(skull.width * scale)), max(1, round(skull.height * scale))),
        Image.Resampling.LANCZOS,
    )


def outclasses_viewer(info: "LiveTargetInfo") -> bool:
    if not info.hostile or not info.viewer_level:
        return False
    return int(info.level) - int(info.viewer_level) >= DANGEROUS_LEVEL_GAP


@lru_cache(maxsize=1)
def _dangerous_name_style() -> native.TextStyle:
    outline = _dark_luminous(DANGEROUS_NAME_COLOR)
    return native.TextStyle(
        core=native.NATIVE_TEXT_CORE,
        edge=(*outline, 255),
        glow=(*outline, 180),
        shadow=(0, 0, 0, 180),
        spread_dilate=1.0,
        spread_sigma=0.55,
        spread_gain=2.8,
    )


def panel_scale() -> float:
    return FRAMED_SCALE if TARGET_PANEL_STYLE == "framed" else 1.0


def _px(value: float) -> int:
    return int(round(value * panel_scale()))


def panel_pitch() -> int:
    if TARGET_PANEL_STYLE != "framed":
        return CARD_OWNED_WIDTH
    return _px(CARD_BOX_WIDTH) + _px(CARD_OWNED_WIDTH - CARD_BOX_WIDTH)


def target_card_visual_width() -> int:
    if TARGET_PANEL_STYLE == "framed":
        return _px(CARD_BOX_WIDTH)
    return CARD_OWNED_WIDTH


def target_backdrop_top_inset() -> int:
    return header_band()


@lru_cache(maxsize=192)
def render_target_info_backdrop(width: int, height: int) -> Image.Image:
    """Render the continuous native-style shade behind the target cards."""

    width = max(1, int(width))
    height = max(1, int(height))
    fade = min(
        TARGET_BACKDROP_FADE_WIDTH + TARGET_BACKDROP_FADE_LEAD,
        max(1, width - 1),
    )
    fade_start = width - fade

    horizontal = bytearray(width)
    for x in range(width):
        if x < fade_start:
            factor = 1.0
        else:
            remaining = (width - 1 - x) / max(1, fade - 1)
            factor = remaining * remaining * (3.0 - 2.0 * remaining)
        horizontal[x] = int(round(TARGET_BACKDROP_PEAK_ALPHA * factor))

    vertical = bytearray(height)
    top_feather = min(TARGET_BACKDROP_TOP_FEATHER, max(0, height // 4))
    bottom_feather = min(TARGET_BACKDROP_BOTTOM_FEATHER, max(0, height // 3))
    for y in range(height):
        factor = 1.0
        if top_feather and y < top_feather:
            factor *= (y + 1) / top_feather
        if bottom_feather and y >= height - bottom_feather:
            factor *= (height - y) / bottom_feather
        vertical[y] = int(round(255.0 * factor))

    horizontal_mask = Image.frombytes("L", (width, 1), bytes(horizontal)).resize(
        (width, height)
    )
    vertical_mask = Image.frombytes("L", (1, height), bytes(vertical)).resize(
        (width, height)
    )
    alpha = ImageChops.multiply(horizontal_mask, vertical_mask)
    image = Image.new("RGBA", (width, height), (*TARGET_BACKDROP_RGB, 0))
    image.putalpha(alpha)
    return image


@lru_cache(maxsize=8)
def _header_sprite(scale: float) -> Image.Image:
    return FONT.render("T A R G E T   I N F O", scale=scale, style=HEADER_TEXT)


@lru_cache(maxsize=8)
def _header_rule_spans(scale: float, width: int) -> tuple[tuple[int, int], ...]:
    text = _header_sprite(scale)
    band_width = max(width, text.width)
    left = max(0, (band_width - text.width) // 2)
    gap = max(1, _px(FRAMED_HEADER_RULE_GAP))
    inset = max(0, _px(FRAMED_HEADER_RULE_INSET))
    right_of_text = left + text.width

    length = min(left - gap - inset, band_width - right_of_text - gap - inset)
    if length <= 1:
        return ()
    return ((left - gap - length, length), (right_of_text + gap, length))


def header_rule_end_center() -> int:
    spans = _header_rule_spans(FRAMED_HEADER_SCALE * panel_scale(), _px(CARD_BOX_WIDTH))
    if not spans:
        return _px(CARD_BOX_WIDTH) - _px(FRAMED_PAD_X)
    start, length = spans[-1]
    return start + length // 2


@lru_cache(maxsize=8)
def _header_band_sprite(scale: float, width: int) -> Image.Image:
    text = _header_sprite(scale)
    band = Image.new("RGBA", (max(width, text.width), text.height), (0, 0, 0, 0))
    left = max(0, (band.width - text.width) // 2)
    band.alpha_composite(text, (left, 0))

    thickness = max(1, _px(FRAMED_HEADER_RULE_WIDTH))

    box = text.getchannel("A").getbbox()
    middle = (box[1] + box[3]) // 2 if box else text.height // 2
    top = max(0, middle - thickness // 2)

    pixels = band.load()
    for (start, length), fades_left in zip(
        _header_rule_spans(scale, width), (True, False)
    ):
        for step in range(length):
            ratio = (step / (length - 1)) if fades_left else (1 - step / (length - 1))
            alpha = int(round(245 * ratio * ratio))
            if alpha <= 0:
                continue
            for row in range(top, min(band.height, top + thickness)):
                pixels[start + step, row] = (*HEADER_TEXT.core[:3], alpha)
    return band


def header_band() -> int:
    if TARGET_PANEL_STYLE != "framed":
        return 0
    sprite = _header_sprite(FRAMED_HEADER_SCALE * panel_scale())
    return sprite.height + _px(FRAMED_HEADER_GAP)


def header_sprite() -> Image.Image:
    return _header_band_sprite(FRAMED_HEADER_SCALE * panel_scale(), _px(CARD_BOX_WIDTH))


def _effect_band_units(effect_rows: int) -> int:
    if effect_rows <= 0:
        return FRAMED_EFFECT_BAND_EMPTY
    return effect_rows * FRAMED_EFFECT_ROW_HEIGHT


def gauge_top(effect_rows: int = 0) -> int:
    return header_band() + _px(
        FRAMED_HP_TOP + FRAMED_HP_BLOCK + _effect_band_units(effect_rows)
    )


def panel_height(has_weak_row: bool, effect_rows: int = 0) -> int:
    if TARGET_PANEL_STYLE != "framed":
        return 150 + (WEAK_ROW_HEIGHT if has_weak_row else 0)

    return (
        gauge_top(effect_rows)
        + _px(FRAMED_BAR_BOTTOM_MARGIN)
        + (_px(WEAK_ROW_HEIGHT) if has_weak_row else 0)
    )


def gauge_box(effect_rows: int = 0) -> tuple[int, int, int, int]:
    if TARGET_PANEL_STYLE == "framed":
        top = gauge_top(effect_rows)
        return (
            _px(FRAMED_PAD_X),
            top,
            _px(CARD_BOX_WIDTH) - 1 - _px(FRAMED_PAD_X),
            top + _px(FRAMED_BAR_HEIGHT),
        )
    return (
        20 - BAR_FRAME_WIDTH,
        134 - BAR_FRAME_WIDTH,
        236 + BAR_FRAME_WIDTH,
        141 + BAR_FRAME_WIDTH,
    )


def _draw_panel_frame(
    image: Image.Image, height: int, theme: "menu_themes.MenuTheme"
) -> None:
    if not FRAMED_DRAW_SLAB:
        return

    right = _px(CARD_BOX_WIDTH) - 1
    border = max(1, _px(FRAMED_BORDER_WIDTH))
    top = header_band()
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).rounded_rectangle(
        (0, top, right, height - 1),
        radius=_px(FRAMED_CORNER) + 2,
        fill=(*theme.accent, 70),
    )
    image.alpha_composite(
        glow.filter(ImageFilter.GaussianBlur(max(1.0, 3.0 * panel_scale())))
    )

    body = (
        *(min(255, int(round(channel * 0.16)) + 8) for channel in theme.panel_dark),
        FRAMED_BODY_ALPHA,
    )
    ImageDraw.Draw(image).rounded_rectangle(
        (border, top + border, right - border, height - 1 - border),
        radius=_px(FRAMED_CORNER),
        fill=body,
        outline=(*theme.accent, 255),
        width=border,
    )


@lru_cache(maxsize=512)
def render_target_info(
    info: LiveTargetInfo,
    player: int = 2,
    theme_name: str | None = None,
) -> Image.Image:
    theme_name = menu_themes.normalize_name(
        theme_name,
        menu_themes.default_name(player),
    )
    theme = menu_themes.get_theme(theme_name)
    identity_style = target_theme_style(theme_name)
    divider = (*theme.accent, 104)
    framed = TARGET_PANEL_STYLE == "framed"
    # The native bar reads nearly white with only a trace of identity hue. A
    # saturated accent made the mod's bar dominate the panel. Its empty portion
    # is also a weak window tint rather than the almost-black slab.
    whiten = FRAMED_BAR_WHITEN if framed else TARGET_BAR_WHITEN
    bar_fill = (
        (*theme.accent, 255)
        if theme_name == "ruby"
        else (
            *(
                min(255, int(round(channel + (255 - channel) * whiten)))
                for channel in theme.accent
            ),
            255,
        )
    )
    bar_empty = (
        *(max(0, int(round(channel * 0.30))) for channel in theme.panel_dark),
        170,
    )

    # Libra still reveals level, HP and weaknesses. Without it, the card body
    # shows "??" and the weakness row disappears. Party cards always reveal them.
    elements = weak_element_names(info.weak_mask) if info.reveal_stats else []

    # Status ailments are different. The game already draws their icons over the
    # enemy without Libra, so hiding them on the card only disagreed with the
    # world view. `live_target_panel_height` also always reserved these rows;
    # gating them left space for a strip that was never drawn.
    effects = status_effect_lines(info.status_mask) if framed else []

    width = 390
    height = panel_height(bool(elements), len(effects))
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    if framed:
        _draw_panel_frame(image, height, theme)

        image.alpha_composite(header_sprite(), (0, 0))
    else:
        image.alpha_composite(_header_sprite(0.70), (1, 0))
        draw.line((2, 34, width - 3, 34), fill=divider, width=1)

    # The Libra badge belongs to the player who opened the card. The title
    # reserves space for the icon instead of running underneath it.
    title_right = _px(CARD_BOX_WIDTH) - _px(FRAMED_PAD_X)
    if framed and info.viewer_has_libra:
        badge = trait_sprite("status/libra")
        if badge is not None:
            badge_scale = status_icon_scale()
            badge = badge.resize(
                (
                    max(1, int(badge.width * badge_scale)),
                    max(1, int(badge.height * badge_scale)),
                ),
                Image.LANCZOS,
            )
            badge_left = header_rule_end_center() - badge.width // 2
            image.alpha_composite(
                badge, (badge_left, header_band() + _px(FRAMED_LIBRA_DROP))
            )
            title_right = min(title_right, badge_left - _px(FRAMED_TITLE_GAP))

    # FLYING sits on the title row like native UI, aligned to the right edge of
    # the owned card width. Cards are placed every 328 px while each image is 390
    # px wide, so overflow is covered by the next card and would flicker with its
    # target. Only the framed layout divides this row; classic reserves nothing.
    flying_reserved = _px(CARD_BOX_WIDTH) - _px(FRAMED_PAD_X) - title_right
    if info.anti_libra:
        anti_libra = trait_sprite("anti_libra")
        if anti_libra is not None:
            scale = status_icon_scale() if framed else TRAIT_ICON_SCALE
            scaled = anti_libra.resize(
                (
                    max(1, int(anti_libra.width * scale)),
                    max(1, int(anti_libra.height * scale)),
                ),
                Image.LANCZOS,
            )
            if framed:
                flying_reserved += scaled.width + _px(FRAMED_TITLE_GAP)
                image.alpha_composite(
                    scaled,
                    (
                        title_right - scaled.width,
                        header_band() + _px(FRAMED_NAME_TOP),
                    ),
                )
                title_right -= scaled.width + _px(FRAMED_TITLE_GAP)
            else:
                image.alpha_composite(
                    scaled, (FLYING_RIGHT_EDGE - scaled.width, FLYING_ROW_Y)
                )
    if info.flying:
        flying = trait_sprite("flying")
        if flying is not None:
            scale = FRAMED_FLYING_SCALE * panel_scale() if framed else TRAIT_ICON_SCALE
            scaled = flying.resize(
                (
                    max(1, int(flying.width * scale)),
                    max(1, int(flying.height * scale)),
                ),
                Image.LANCZOS,
            )
            if framed:
                # Framed cards omit the header, leaving the title row free for
                # FLYING at the native position. If Libra already owns that
                # corner, place FLYING to its left.
                flying_reserved += scaled.width + _px(FRAMED_TITLE_GAP)
                image.alpha_composite(
                    scaled,
                    (title_right - scaled.width, header_band() + _px(FRAMED_NAME_TOP)),
                )
            else:
                image.alpha_composite(
                    scaled, (FLYING_RIGHT_EDGE - scaled.width, FLYING_ROW_Y)
                )

    if info.is_ko:
        badge = ko_badge()
        edge = title_right if framed else FLYING_RIGHT_EDGE
        top = header_band() + _px(FRAMED_NAME_TOP) if framed else 38
        image.alpha_composite(badge, (edge - badge.width, top))
        flying_reserved += badge.width + _px(FRAMED_TITLE_GAP)

    type_scale = panel_scale() if framed else 1.0
    LABEL_SCALE = 0.72 * type_scale
    VALUE_SCALE = (FRAMED_NAME_SCALE if framed else 0.94) * type_scale
    LEVEL_LABEL_SCALE = FRAMED_LEVEL_SCALE * type_scale if framed else LABEL_SCALE
    CURRENT_HP_SCALE = (FRAMED_CURRENT_HP_SCALE if framed else 1.16) * type_scale
    MAXIMUM_HP_SCALE = (FRAMED_MAXIMUM_HP_SCALE if framed else 0.82) * type_scale
    LABEL_GAP = max(1, _px(3)) if framed else 3
    GROUP_GAP = max(1, _px(6)) if framed else 6
    text_left = _px(FRAMED_PAD_X) if framed else 4

    slab_top = header_band()
    name_top = slab_top + _px(FRAMED_NAME_TOP) if framed else 38
    hp_top = slab_top + _px(FRAMED_HP_TOP) if framed else 80

    def baseline(sprite: Image.Image, tallest: int, top: int) -> int:
        return top + max(0, tallest - sprite.height)

    level_number = f"{max(0, info.level):02d}" if info.reveal_stats else "??"
    dangerous = outclasses_viewer(info)
    name_style = _dangerous_name_style() if dangerous else identity_style
    level_style = name_style if dangerous else identity_style
    level_label = FONT.render("Lv.", scale=LEVEL_LABEL_SCALE, style=level_style)
    level_value = FONT.render(level_number, scale=VALUE_SCALE, style=level_style)
    name = FONT.render(info.name, scale=VALUE_SCALE, style=name_style)
    name_left = (
        text_left + level_label.width + LABEL_GAP + level_value.width + GROUP_GAP
    )
    if framed:
        budget = _px(CARD_BOX_WIDTH) - _px(FRAMED_PAD_X) - flying_reserved - name_left
        scale = VALUE_SCALE
        floor = 0.42 * type_scale
        while name.width > budget and scale > floor:
            scale -= 0.04 * type_scale
            name = FONT.render(info.name, scale=scale, style=name_style)
    row_height = max(level_label.height, level_value.height, name.height)
    cursor = text_left
    image.alpha_composite(
        level_label, (cursor, baseline(level_label, row_height, name_top))
    )
    cursor += level_label.width + LABEL_GAP
    image.alpha_composite(
        level_value, (cursor, baseline(level_value, row_height, name_top))
    )
    cursor += level_value.width + GROUP_GAP
    image.alpha_composite(name, (cursor, baseline(name, row_height, name_top)))

    current = max(0, info.current_hp)
    maximum = max(1, info.max_hp)
    hp = FONT.render("HP", scale=LABEL_SCALE, style=identity_style)
    current_label = str(current) if info.reveal_stats else "????"
    maximum_label = str(maximum) if info.reveal_stats else "????"
    current_text = FONT.render(
        current_label, scale=CURRENT_HP_SCALE, style=identity_style
    )

    maximum_text = FONT.render(
        maximum_label, scale=MAXIMUM_HP_SCALE, style=identity_style
    )

    separator = (
        FONT.render("/", scale=MAXIMUM_HP_SCALE, style=identity_style)
        if framed
        else None
    )
    hp_height = max(hp.height, current_text.height, maximum_text.height)
    cursor = text_left if framed else 20
    image.alpha_composite(hp, (cursor, baseline(hp, hp_height, hp_top)))
    cursor += hp.width + LABEL_GAP
    image.alpha_composite(
        current_text, (cursor, baseline(current_text, hp_height, hp_top))
    )
    cursor += current_text.width + LABEL_GAP
    if separator is not None:
        image.alpha_composite(
            separator, (cursor, baseline(separator, hp_height, hp_top))
        )
        cursor += separator.width + LABEL_GAP
    image.alpha_composite(
        maximum_text, (cursor, baseline(maximum_text, hp_height, hp_top))
    )

    bar_left, bar_top, bar_right, bar_bottom = gauge_box(len(effects))
    radius = (bar_bottom - bar_top) // 2 if framed else 0

    def gauge(box, fill) -> None:
        if radius:
            draw.rounded_rectangle(box, radius=radius, fill=fill)
        else:
            draw.rectangle(box, fill=fill)

    gauge((bar_left, bar_top, bar_right, bar_bottom), bar_empty)

    ratio = min(1.0, current / maximum)
    filled = bar_left + int((bar_right - bar_left) * ratio)
    if filled > bar_left:
        gauge(
            (bar_left, bar_top, max(bar_left + 2 * radius + 1, filled), bar_bottom),
            bar_fill,
        )

    if not framed:
        draw.rectangle(
            (bar_left, bar_top, bar_right, bar_bottom),
            outline=(*_dark_luminous(theme.accent), 255),
            width=BAR_FRAME_WIDTH,
        )

    default_icon_scale = TRAIT_ICON_SCALE * (panel_scale() if framed else 1.0)
    row_left = text_left if framed else 20
    edge = _px(CARD_BOX_WIDTH) - _px(FRAMED_PAD_X) if framed else CARD_OWNED_WIDTH
    drop = _px(WEAK_ICON_DROP) if framed else WEAK_ICON_DROP
    preferred_gap = _px(WEAK_ICON_GAP) if framed else WEAK_ICON_GAP

    def icon_row(
        top: int,
        caption: str,
        names: Sequence[str],
        folder: str = "",
        cap_to_fit: bool = False,
        row_height: int = 0,
        base_scale: float | None = None,
    ) -> None:
        start = row_left
        if caption:
            label = FONT.render(caption, scale=LABEL_SCALE, style=identity_style)
            label_y = (
                top + max(0, (row_height - label.height) // 2)
                if row_height
                else top + (_px(4) if framed else 4)
            )
            image.alpha_composite(label, (row_left, label_y))
            start = row_left + label.width + GROUP_GAP

        sprites = [
            sprite
            for sprite in (trait_sprite(f"{folder}{name}") for name in names)
            if sprite is not None
        ]
        if not sprites:
            return

        scale = base_scale if base_scale is not None else default_icon_scale
        scaled = [
            sprite.resize(
                (
                    max(1, int(sprite.width * scale)),
                    max(1, int(sprite.height * scale)),
                ),
                Image.LANCZOS,
            )
            for sprite in sprites
        ]

        if cap_to_fit:
            natural = sum(sprite.width for sprite in scaled)
            available = edge - start - preferred_gap * max(0, len(scaled) - 1)
            if natural > available > 0:
                scale *= available / natural
                scaled = [
                    sprite.resize(
                        (
                            max(1, int(sprite.width * scale)),
                            max(1, int(sprite.height * scale)),
                        ),
                        Image.LANCZOS,
                    )
                    for sprite in sprites
                ]

        room = edge - start - sum(sprite.width for sprite in scaled)
        gap = max(0, min(preferred_gap, room // max(1, len(scaled) - 1)))
        tallest = max(sprite.height for sprite in scaled)
        base = top + max(0, (row_height - tallest) // 2) if row_height else top + drop
        cursor = start
        for sprite in scaled:
            image.alpha_composite(
                sprite, (cursor, base + (tallest - sprite.height) // 2)
            )
            cursor += sprite.width + gap

    effect_top = header_band() + _px(FRAMED_HP_TOP + FRAMED_HP_BLOCK)
    effect_pitch = _px(FRAMED_EFFECT_ROW_HEIGHT)
    for index, icons in enumerate(effects):
        icon_row(
            effect_top + index * effect_pitch,
            "",
            icons,
            folder="status/",
            cap_to_fit=True,
            row_height=effect_pitch,
            base_scale=status_icon_scale(),
        )

    if elements:
        icon_row(bar_bottom + (_px(6) if framed else 6), "Weak:", elements)
    return image


__all__ = [
    "LiveTargetInfo",
    "PositionedImage",
    "project_actor_point",
    "project_point",
    "projected_ring_center",
    "projected_actor_ring",
    "interpolate_projected_ring",
    "is_probable_minimap_view",
    "render_projected_ring",
    "render_target_info_backdrop",
    "render_target_info",
    "target_backdrop_top_inset",
    "target_card_visual_width",
    "target_theme_style",
    "translate_positioned",
    "transform_positioned",
    "transform_row_vector",
]
