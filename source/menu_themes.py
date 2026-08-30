from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import atomic_files
import runtime_paths

HERE = Path(__file__).resolve().parent


CONFIG_PATH = runtime_paths.config_path(__file__, "menu_theme_config.json")


@dataclass(frozen=True)
class MenuTheme:
    name: str
    panel_dark: tuple[int, int, int]
    panel_light: tuple[int, int, int]
    accent: tuple[int, int, int]
    highlight: tuple[int, int, int]
    text_core: tuple[int, int, int]
    key_text: tuple[int, int, int] = (20, 17, 8)


PRESETS: dict[str, MenuTheme] = {
    "sapphire": MenuTheme(
        "sapphire",
        (10, 24, 45),
        (145, 194, 247),
        (58, 139, 220),
        (151, 211, 255),
        (248, 253, 255),
    ),
    "gold": MenuTheme(
        "gold",
        (43, 34, 6),
        (216, 190, 72),
        (225, 194, 83),
        (255, 236, 157),
        (255, 255, 242),
    ),
    "emerald": MenuTheme(
        "emerald",
        (0, 31, 19),
        (42, 190, 116),
        (34, 181, 105),
        (139, 255, 195),
        (245, 255, 250),
    ),
    "amethyst": MenuTheme(
        "amethyst",
        (25, 6, 43),
        (156, 80, 197),
        (153, 69, 207),
        (229, 155, 255),
        (254, 248, 255),
    ),
    "pink": MenuTheme(
        "pink",
        (50, 12, 36),
        (206, 98, 164),
        (218, 77, 164),
        (255, 185, 230),
        (255, 248, 253),
    ),
    "ruby": MenuTheme(
        "ruby",
        (40, 0, 0),
        (218, 58, 48),
        (219, 52, 61),
        (255, 143, 137),
        (255, 249, 247),
    ),
    "bronze": MenuTheme(
        "bronze",
        (25, 12, 7),
        (112, 67, 42),
        (99, 56, 35),
        (201, 135, 89),
        (255, 248, 240),
        key_text=(255, 250, 243),
    ),
}

ALIASES = {
    "blue": "sapphire",
    "safira": "sapphire",
    "dourado": "gold",
    "esmeralda": "emerald",
    "purple": "amethyst",
    "roxo": "amethyst",
    "ametista": "amethyst",
    "rosa": "pink",
    "black": "ruby",
    "preto": "ruby",
    "onyx": "ruby",
    "onix": "ruby",
    "ônix": "ruby",
    "rubi": "ruby",
    "fire": "emerald",
    "fogo": "emerald",
    "light_brown": "bronze",
    "dark_brown": "bronze",
    "marrom_claro": "bronze",
    "marrom_escuro": "bronze",
    "marrom claro": "bronze",
    "marrom escuro": "bronze",
    "white": "bronze",
    "pearl": "bronze",
    "branco": "bronze",
}

PARTY_MEMBER_IDS = {
    "vaan": 0,
    "ashe": 1,
    "fran": 2,
    "balthier": 3,
    "basch": 4,
    "penelo": 5,
    "reks": 6,
    "lamont": 10,
    "vossler": 12,
    "larsa": 13,
    "reddas": 14,
}


CANONICAL_MEMBER_ID_BY_MEMBER_ID = {
    0: 0,
    1: 1,
    2: 2,
    3: 3,
    4: 4,
    5: 5,
    6: 6,
    7: 1,
    8: 4,
    9: 4,
    10: 13,
    11: 12,
    12: 12,
    13: 13,
    14: 14,
}


@dataclass(frozen=True)
class CharacterThemeProfile:
    member_id: int
    name: str
    portrait_filename: str
    default_theme: str
    is_guest: bool = False


MAIN_CHARACTER_PROFILES = (
    CharacterThemeProfile(0, "Vaan", "0-Vaan.png", "sapphire"),
    CharacterThemeProfile(3, "Balthier", "3-Balthier.png", "bronze"),
    CharacterThemeProfile(2, "Fran", "2-Fran.png", "amethyst"),
    CharacterThemeProfile(4, "Basch", "4-Basch.png", "emerald"),
    CharacterThemeProfile(1, "Ashe", "1-Ashe.png", "pink"),
    CharacterThemeProfile(5, "Penelo", "5-Penelo.png", "gold"),
)

GUEST_CHARACTER_PROFILES = (
    CharacterThemeProfile(6, "Reks", "6-Reks.png", "sapphire", True),
    CharacterThemeProfile(12, "Vossler", "12-Vossler-(Knight).png", "sapphire", True),
    CharacterThemeProfile(13, "Larsa", "13-Larsa.png", "sapphire", True),
    CharacterThemeProfile(14, "Reddas", "14-Reddas.png", "sapphire", True),
)

PLAYABLE_CHARACTER_PROFILES = (
    *MAIN_CHARACTER_PROFILES,
    *GUEST_CHARACTER_PROFILES,
)

DEFAULT_CHARACTER_THEMES = {
    profile.member_id: profile.default_theme for profile in PLAYABLE_CHARACTER_PROFILES
}

TARGET_CIRCLE_PALETTE_BY_THEME = {
    "sapphire": 0,
    "ruby": 1,
    "pink": 2,
    "emerald": 3,
    "gold": 4,
    "amethyst": 5,
    "bronze": 6,
}

# The bundled battle_4_c override bakes seven hue rotations of the exact
# live-approved pink ramp into palettes 108..114.  Every palette preserves its
# 96-level alpha curve.  Emerald alone keeps a neutral white centre and applies
# a darker saturated curve to its coloured edge, separating Basch's damage
# from pale-green healing; no menu/HUD colour is involved.
DAMAGE_NUMBER_PALETTE = {
    "sapphire": (127, 127, 127),
    "gold": (127, 127, 127),
    "emerald": (127, 127, 127),
    "amethyst": (127, 127, 127),
    "pink": (127, 127, 127),
    "ruby": (127, 127, 127),
    "bronze": (127, 127, 127),
}

# Palette indexes from the appended custom CLUTs in battle_4_c.  Native slots
# 0..107 are untouched; these seven are new resources, not indexes from the
# unrelated global fontclut table.
DAMAGE_NUMBER_CLUT_BY_THEME = {
    "sapphire": 108,
    "gold": 109,
    "emerald": 110,
    "amethyst": 111,
    "pink": 112,
    "ruby": 113,
    "bronze": 114,
}

ASSIGNMENT_MODE_CHARACTER = "character"
ASSIGNMENT_MODE_PLAYER = "player"
ASSIGNMENT_MODES = (
    ASSIGNMENT_MODE_CHARACTER,
    ASSIGNMENT_MODE_PLAYER,
)


def canonical_member_id(value: int) -> int:
    member_id = int(value)
    return CANONICAL_MEMBER_ID_BY_MEMBER_ID.get(member_id, member_id)


def normalize_name(value: object, fallback: str = "gold") -> str:
    name = str(value or "").strip().lower().replace("-", "_")
    name = ALIASES.get(name, name)
    return name if name in PRESETS else fallback


def normalize_assignment_mode(value: object) -> str:
    name = str(value or "").strip().lower().replace("-", "_")
    if name in ASSIGNMENT_MODES:
        return name
    raise ValueError(f"unsupported theme assignment mode: {value!r}")


def default_name(player: int) -> str:
    return {1: "sapphire", 2: "emerald", 3: "bronze"}.get(int(player), "gold")


def get_theme(name: object, *, fallback: str = "gold") -> MenuTheme:
    return PRESETS[normalize_name(name, fallback)]


def gauge_palette(name: object) -> tuple[int, int, int, int, int, int, int, int]:
    theme_name = normalize_name(name)

    if theme_name == "gold":
        return (127, 127, 58, 127, 127, 108, 18, 127)
    if theme_name == "ruby":
        # This is FF12's authored red gauge ramp. Besides matching the native
        # Ruby elements, its softer channel transition preserves the gauge's
        # antialiased edge instead of turning it into a saturated red fringe.
        return (92, 44, 60, 127, 127, 75, 45, 127)
    theme = get_theme(theme_name)

    def half_channel(value: int) -> int:
        return min(127, max(0, int(round(value * 127.0 / 255.0))))

    bright = tuple(half_channel(value) for value in theme.highlight)
    body = tuple(half_channel(value) for value in theme.accent)
    return (*bright, 127, *body, 127)


def damage_number_color(name: object) -> tuple[int, int, int]:
    theme_name = normalize_name(name)
    return DAMAGE_NUMBER_PALETTE[theme_name]


def damage_number_clut(name: object) -> int:
    theme_name = normalize_name(name)
    return DAMAGE_NUMBER_CLUT_BY_THEME[theme_name]


def damage_number_style(name: object) -> tuple[int, int, int, int]:
    theme_name = normalize_name(name)
    return (*DAMAGE_NUMBER_PALETTE[theme_name], DAMAGE_NUMBER_CLUT_BY_THEME[theme_name])


def hud_text_color(name: object) -> tuple[int, int, int]:
    theme_name = normalize_name(name)

    if theme_name == "gold":
        return (127, 108, 18)
    if theme_name == "sapphire":
        return (74, 112, 127)
    if theme_name == "ruby":
        return (127, 75, 45)
    theme = get_theme(theme_name)
    source = theme.accent

    if max(source) < 140:
        source = theme.highlight
    return tuple(
        min(127, max(0, int(round(value * 127.0 / 255.0)))) for value in source
    )


@dataclass(frozen=True)
class ThemeSelection:
    player_1: str = "sapphire"
    player_2: str = "gold"
    character_overrides: tuple[tuple[int, str], ...] = ()

    player_3: str | None = None
    assignment_mode: str = ASSIGNMENT_MODE_CHARACTER

    def __post_init__(self) -> None:
        player_1 = normalize_name(self.player_1, "sapphire")
        player_2 = normalize_name(self.player_2, "gold")
        player_3 = (
            player_2
            if self.player_3 is None
            else normalize_name(self.player_3, player_2)
        )
        overrides = tuple(
            sorted(
                (
                    int(member_id),
                    normalize_name(theme_name),
                )
                for member_id, theme_name in self.character_overrides
            )
        )
        object.__setattr__(self, "player_1", player_1)
        object.__setattr__(self, "player_2", player_2)
        object.__setattr__(self, "player_3", player_3)
        object.__setattr__(self, "character_overrides", overrides)
        object.__setattr__(
            self,
            "assignment_mode",
            normalize_assignment_mode(self.assignment_mode),
        )

    def player_theme(self, player: int) -> str:
        player = int(player)
        if player == 1:
            return self.player_1
        if player == 3:
            return str(self.player_3)
        return self.player_2

    def _character_theme(
        self,
        party_member_id: int,
        *,
        fallback: str,
    ) -> str:
        member_id = int(party_member_id)
        canonical_id = canonical_member_id(member_id)
        overrides = dict(self.character_overrides)
        if member_id in overrides:
            return overrides[member_id]
        if canonical_id in overrides:
            return overrides[canonical_id]
        if canonical_id in DEFAULT_CHARACTER_THEMES:
            return DEFAULT_CHARACTER_THEMES[canonical_id]
        return fallback

    def for_player(
        self,
        player: int,
        party_member_id: int | None = None,
    ) -> str:
        role_theme = self.player_theme(player)
        if self.assignment_mode == ASSIGNMENT_MODE_PLAYER or party_member_id is None:
            return role_theme
        return self._character_theme(
            party_member_id,
            fallback=role_theme,
        )

    def for_character(self, party_member_id: int) -> str:
        return self._character_theme(
            party_member_id,
            fallback=self.player_1,
        )


def default_selection() -> ThemeSelection:
    return ThemeSelection(
        "sapphire",
        "emerald",
        character_overrides=tuple(sorted(DEFAULT_CHARACTER_THEMES.items())),
        player_3="bronze",
    )


def _party_member_id(value: object) -> int | None:
    label = str(value).strip().lower()
    if label in PARTY_MEMBER_IDS:
        return PARTY_MEMBER_IDS[label]
    try:
        member_id = int(label, 0)
    except ValueError:
        return None
    return member_id if 0 <= member_id <= 255 else None


def _selection_from_payload(payload: object) -> ThemeSelection:
    if not isinstance(payload, dict):
        raise ValueError("theme config must contain one JSON object")
    defaults = payload.get("defaults")
    if not isinstance(defaults, dict):
        defaults = {}
    raw_characters = payload.get("characters")
    overrides: dict[int, str] = {}
    if isinstance(raw_characters, dict):
        for label, theme_name in raw_characters.items():
            member_id = _party_member_id(label)
            if member_id is not None:
                overrides[member_id] = normalize_name(theme_name)
    player_1 = normalize_name(
        defaults.get("player_1", payload.get("player_1")),
        "sapphire",
    )
    player_2 = normalize_name(
        defaults.get("player_2", payload.get("player_2")),
        "gold",
    )
    raw_player_3 = defaults.get(
        "player_3",
        payload.get("player_3", player_2),
    )
    return ThemeSelection(
        player_1=player_1,
        player_2=player_2,
        character_overrides=tuple(sorted(overrides.items())),
        player_3=normalize_name(raw_player_3, player_2),
        assignment_mode=normalize_assignment_mode(
            payload.get("assignment_mode", ASSIGNMENT_MODE_CHARACTER)
        ),
    )


def _load_selection_file(path: Path) -> ThemeSelection:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return _selection_from_payload(payload)


def load_selection(path: Path = CONFIG_PATH) -> ThemeSelection:
    try:
        return _load_selection_file(path)
    except (OSError, TypeError, ValueError):
        return default_selection()


def target_circle_palette(name: object) -> int:
    return TARGET_CIRCLE_PALETTE_BY_THEME[normalize_name(name)]


def save_selection(
    selection: ThemeSelection,
    path: Path = CONFIG_PATH,
) -> ThemeSelection:
    normalized = ThemeSelection(
        player_1=selection.player_1,
        player_2=selection.player_2,
        character_overrides=selection.character_overrides,
        player_3=selection.player_3,
        assignment_mode=selection.assignment_mode,
    )
    profile_ids = {profile.member_id for profile in PLAYABLE_CHARACTER_PROFILES}
    characters = {
        profile.name: normalized.for_character(profile.member_id)
        for profile in PLAYABLE_CHARACTER_PROFILES
    }

    characters.update(
        {
            str(member_id): theme_name
            for member_id, theme_name in normalized.character_overrides
            if member_id not in profile_ids
        }
    )
    payload = {
        "version": 3,
        "assignment_mode": normalized.assignment_mode,
        "defaults": {
            "player_1": normalized.player_1,
            "player_2": normalized.player_2,
            "player_3": normalized.player_theme(3),
        },
        "characters": characters,
        "available_presets": list(PRESETS),
    }
    atomic_files.write_text(
        path,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    )
    return load_selection(path)


class ThemeTracker:
    def __init__(self, path: Path = CONFIG_PATH, poll_seconds: float = 0.25) -> None:
        self.path = path
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.selection = default_selection()
        self._stamp: tuple[int, int] | None = None
        self._next_poll = 0.0
        self.reload(force=True)

    def reload(self, *, force: bool = False) -> ThemeSelection:
        now = time.monotonic()
        if not force and now < self._next_poll:
            return self.selection
        self._next_poll = now + self.poll_seconds
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if not force and stamp == self._stamp:
                return self.selection
            # A mod-manager VFS can briefly expose an absent or incomplete file
            # while replacing it.  A live renderer must keep the last complete
            # selection instead of turning that transient read into defaults.
            candidate = _load_selection_file(self.path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return self.selection
        self._stamp = stamp
        self.selection = candidate
        return self.selection


__all__ = [
    "ASSIGNMENT_MODE_CHARACTER",
    "ASSIGNMENT_MODE_PLAYER",
    "ASSIGNMENT_MODES",
    "CONFIG_PATH",
    "CANONICAL_MEMBER_ID_BY_MEMBER_ID",
    "CharacterThemeProfile",
    "DEFAULT_CHARACTER_THEMES",
    "default_selection",
    "DAMAGE_NUMBER_CLUT_BY_THEME",
    "DAMAGE_NUMBER_PALETTE",
    "GUEST_CHARACTER_PROFILES",
    "MAIN_CHARACTER_PROFILES",
    "MenuTheme",
    "PARTY_MEMBER_IDS",
    "PLAYABLE_CHARACTER_PROFILES",
    "PRESETS",
    "TARGET_CIRCLE_PALETTE_BY_THEME",
    "ThemeSelection",
    "ThemeTracker",
    "canonical_member_id",
    "default_name",
    "damage_number_clut",
    "damage_number_color",
    "damage_number_style",
    "get_theme",
    "gauge_palette",
    "hud_text_color",
    "load_selection",
    "normalize_assignment_mode",
    "normalize_name",
    "save_selection",
    "target_circle_palette",
]
