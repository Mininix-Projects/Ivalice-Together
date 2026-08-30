from __future__ import annotations

import ctypes
import math
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

import runtime_entry

# The same executable also starts the mod's internal processes. Dispatch must
# happen before pygame is initialized so those processes do not open a window.
if runtime_entry.is_runtime_invocation(sys.argv[1:]):
    raise SystemExit(runtime_entry.safe_dispatch(sys.argv[1:]))


os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import controller_bindings as controls
import coop_roster
import launcher_core as core
import launcher_i18n
import launcher_tray
import menu_themes
import pygame

LOGICAL_SIZE = (1440, 810)
MINIMUM_WINDOW_SIZE = (1280, 720)
# Match the window to the authored artwork size to keep text and edges crisp.
WINDOW_SIZE = LOGICAL_SIZE
VISIBLE_FPS = 60
TRAY_POLL_FPS = 10
TEXT_CACHE_LIMIT = 768

INK = (3, 8, 14)
WHITE = (247, 244, 231)
TEXT = (221, 229, 232)
MUTED = (139, 157, 173)
FAINT = (78, 108, 132)
BLUE_LIGHT = (96, 196, 232)
GOLD = (201, 157, 65)
GOLD_LIGHT = (248, 215, 128)
GREEN = (105, 218, 171)
RED = (235, 111, 94)
PANEL_TOP = (17, 48, 76, 242)
PANEL_BOTTOM = (5, 18, 33, 247)

CONTENT_X = 292
CONTENT_W = 650
PAGES = (
    ("home", "GERAL"),
    ("colors", "CORES"),
    ("controls", "CONTROLES"),
    ("settings", "AJUSTES"),
)

PANEL_AA_SCALE = 4
PORTRAIT_AA_SCALE = 4

ACTION_LABELS = {
    "toggle_control": "Alternar controle",
    "move_up": "Avançar",
    "move_down": "Recuar",
    "move_left": "Mover à esquerda",
    "move_right": "Mover à direita",
    "run_toggle": "Andar / correr",
    "flee": "Fuga",
    "battle_menu": "Abrir menu de batalha",
    "menu_up": "Menu acima",
    "menu_down": "Menu abaixo",
    "menu_left": "Menu anterior",
    "menu_right": "Menu seguinte",
    "target_previous": "Alvo anterior",
    "target_next": "Próximo alvo",
    "quickening": "Ativar Quickening",
    "confirm": "Confirmar / interagir",
    "cancel": "Cancelar / fechar",
    "native_party_menu": "Party / Licenças",
    "native_pause": "Pause",
}

THEME_LABELS = {
    "sapphire": "Safira",
    "gold": "Ouro",
    "emerald": "Esmeralda",
    "amethyst": "Ametista",
    "pink": "Rosa",
    "bronze": "Bronze",
    "ruby": "Rubi",
}

# The source portraits have very different crops (Fran includes the full ears,
# while Penelo and Basch are much wider shoulder portraits).  These optical
# scale factors make faces and silhouettes read at one consistent icon size.
PORTRAIT_ZOOM = {
    0: 1.00,
    1: 1.02,
    2: 1.24,
    3: 1.04,
    4: 1.14,
    5: 1.25,
    6: 1.08,
    7: 1.02,
    8: 1.10,
    9: 1.10,
    10: 1.08,
    11: 1.08,
    12: 1.10,
    13: 1.08,
    14: 1.06,
}


@dataclass
class Item:
    key: str
    rect: pygame.Rect
    action: Callable[[], None] | None = None
    adjust: Callable[[int], None] | None = None
    clear: Callable[[], None] | None = None


@dataclass(frozen=True)
class TextCommand:
    font: pygame.font.Font
    text: str
    position: tuple[int, int]
    color: tuple[int, int, int]
    anchor: str
    shadow: bool
    rect: pygame.Rect


class Fonts:
    def __init__(self) -> None:
        palatino = pygame.font.match_font("palatinolinotype") or pygame.font.match_font(
            "georgia"
        )
        segoe = pygame.font.match_font("segoeui")
        segoe_semibold = (
            pygame.font.match_font("segoeuisemibold")
            or pygame.font.match_font("segoeuibold")
            or segoe
        )
        self._specs: dict[int, tuple[str | None, int]] = {}
        self._scaled: dict[tuple[int, int], pygame.font.Font] = {}
        self.hero = self._font(palatino, 47)
        self.title = self._font(palatino, 36)
        self.subtitle = self._font(palatino, 22)
        self.body = self._font(segoe, 18)
        self.body_small = self._font(segoe, 16)
        self.label = self._font(segoe_semibold, 15)
        self.micro = self._font(segoe_semibold, 12)
        self.button = self._font(palatino, 22)
        self.binding = self._font(segoe_semibold, 13)
        self.card_name = self._font(palatino, 21)

    def _font(self, path: str | None, size: int) -> pygame.font.Font:
        font = pygame.font.Font(path, size)
        self._specs[id(font)] = (path, size)
        return font

    def scaled(self, font: pygame.font.Font, scale: float) -> pygame.font.Font:
        path, size = self._specs[id(font)]
        scaled_size = max(1, round(size * scale))
        key = (id(font), scaled_size)
        cached = self._scaled.get(key)
        if cached is None:
            cached = pygame.font.Font(path, scaled_size)
            self._scaled[key] = cached
        return cached


class MenuAudio:
    FILES = {
        "move": "move.wav",
        "confirm": "confirm.wav",
        "cancel": "cancel.wav",
        "open": "open.wav",
        "error": "error.wav",
    }

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.sounds: dict[str, pygame.mixer.Sound] = {}
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=384)
            for name, filename in self.FILES.items():
                path = core.bundled_asset(filename)
                if path.is_file():
                    self.sounds[name] = pygame.mixer.Sound(str(path))
            self.set_volume(float(config.get("sound_volume", 1.0)))
        except pygame.error:
            self.sounds.clear()

    def set_volume(self, volume: float) -> None:
        for sound in self.sounds.values():
            sound.set_volume(max(0.0, min(1.0, volume)))

    def play(self, name: str) -> None:
        if not self.config.get("menu_sounds", True):
            return
        sound = self.sounds.get(name)
        if sound is not None:
            sound.play()


def pygame_key_to_vk(key: int, unicode_text: str = "") -> int | None:
    if pygame.K_a <= key <= pygame.K_z:
        return ord(chr(key).upper())
    if pygame.K_0 <= key <= pygame.K_9:
        return ord(chr(key))
    fixed = {
        pygame.K_BACKSPACE: 0x08,
        pygame.K_TAB: 0x09,
        pygame.K_RETURN: 0x0D,
        pygame.K_KP_ENTER: 0x0D,
        pygame.K_LSHIFT: 0x10,
        pygame.K_RSHIFT: 0x10,
        pygame.K_LCTRL: 0x11,
        pygame.K_RCTRL: 0x11,
        pygame.K_LALT: 0x12,
        pygame.K_RALT: 0x12,
        pygame.K_CAPSLOCK: 0x14,
        pygame.K_ESCAPE: 0x1B,
        pygame.K_SPACE: 0x20,
        pygame.K_PAGEUP: 0x21,
        pygame.K_PAGEDOWN: 0x22,
        pygame.K_END: 0x23,
        pygame.K_HOME: 0x24,
        pygame.K_LEFT: 0x25,
        pygame.K_UP: 0x26,
        pygame.K_RIGHT: 0x27,
        pygame.K_DOWN: 0x28,
        pygame.K_INSERT: 0x2D,
        pygame.K_DELETE: 0x2E,
    }
    if key in fixed:
        return fixed[key]
    keypads = tuple(getattr(pygame, f"K_KP{index}") for index in range(10))
    if key in keypads:
        return 0x60 + keypads.index(key)
    functions = tuple(getattr(pygame, f"K_F{index}") for index in range(1, 13))
    if key in functions:
        return 0x70 + functions.index(key)
    if unicode_text and len(unicode_text) == 1:
        code = ord(unicode_text.upper())
        if 0 < code < 256:
            return code
    return None


class LauncherApp:
    def __init__(self, *, preview: bool = False) -> None:
        self._enable_windows_dpi_awareness()
        if not preview:
            self._set_windows_identity()
        pygame.init()
        self.preview = preview
        self._window_icon: pygame.Surface | None = None
        self._native_icon_handles: tuple[int, ...] = ()
        self.tray: launcher_tray.WindowsTrayIcon | None = None
        self.in_tray = False

        self._set_icon()
        if not pygame.joystick.get_init():
            pygame.joystick.init()
        self.canvas = pygame.Surface(LOGICAL_SIZE, pygame.SRCALPHA)
        if preview:
            self.window = pygame.display.set_mode((1, 1))
        else:
            self.window = pygame.display.set_mode(
                WINDOW_SIZE, pygame.RESIZABLE | pygame.DOUBLEBUF
            )
            pygame.display.set_caption("Ivalice Together")

            self._set_native_window_icons()
            self.tray = launcher_tray.WindowsTrayIcon(
                core.bundled_asset("icon.ico"),
                "Ivalice Together",
            )
            self.tray.start()
        self.clock = pygame.time.Clock()
        self.fonts = Fonts()
        self.config = core.load_launcher_config()
        self.update_tray_labels()
        self.audio = MenuAudio(self.config)
        self.editor = core.ControlEditor(
            core.coop_directory() / "controller_config.json"
        )
        control_normalization_report = self.editor.normalization_report
        self.roster = core.load_roster()
        self.theme_selection = self._load_theme_selection()
        self.theme_mode = self.theme_selection.assignment_mode
        self.theme_group = "main"
        self.player_theme_draft = {
            "p1": self.theme_selection.player_1,
            "p2": self.theme_selection.player_2,
            "p3": self.theme_selection.player_3,
        }
        self.character_theme_draft = {
            profile.member_id: self.theme_selection.for_character(profile.member_id)
            for profile in menu_themes.PLAYABLE_CHARACTER_PROFILES
        }
        self.theme_dirty = False
        self.theme_picker_target: str | int | None = None
        self.theme_picker_return_key = ""
        self.launcher = core.CoopLaunchSupervisor()
        self.page = "home"
        self.window_focused = True
        self.control_player = "p1"
        self.items: list[Item] = []
        self.selected_key = "home:start_all"
        self.status_message = (
            "Configurações prontas. O co-op inicia automaticamente com o jogo."
        )
        self.status_kind = "normal"
        self.status_until = 0.0
        self.status_expired = True
        self.running = True
        self.game_open = bool(core.process_ids())
        if not preview:
            if control_normalization_report.incompatible_future:
                self.show_status(
                    self.control_future_version_message(control_normalization_report),
                    "warning",
                    12.0,
                )
            elif control_normalization_report.needs_persist:
                self.persist_control_normalization(control_normalization_report)
        self.controller_discovery_suspended = self.page != "controls"
        self.states = controls.read_xinput_states()
        self.sdl_states = (
            ()
            if self.controller_discovery_suspended
            else controls.read_sdl_controller_states()
        )
        if (
            not self.editor.read_only
            and controls.upgrade_sdl_identities(self.editor.config, self.sdl_states)
        ):
            self.editor.dirty = True
            try:
                self.editor.save()
            except OSError:
                self.editor.dirty = True
        self.last_pad_poll = 0.0
        self.last_controller_state_poll = 0.0
        self.controller_recovery_at = 0.0
        self.default_confirm_until = 0.0
        self.damage_reset_confirm_until = 0.0
        self.last_process_poll = time.monotonic()
        self.pointer_y: float | None = None
        self.pointer_trail: list[tuple[float, float]] = []
        self.pointer_damage: pygame.Rect | None = None
        self.frame_damage: pygame.Rect | None = None
        self.full_redraw = True
        self.scene: pygame.Surface | None = None
        self.scene_dirty = True
        self.background = self._load_background()
        (
            self.background_veil,
            self.background_wash,
            self.capture_shade,
        ) = self._make_static_layers()
        self.cursor = self._load_cursor()
        self.cursor_ghosts = self._make_cursor_ghosts()
        self.motes = self._make_motes()
        self.portraits = self._load_portraits()
        self.portrait_cache: dict[tuple[int, int, int], pygame.Surface] = {}
        self.portrait_frame_cache: dict[tuple[object, ...], pygame.Surface] = {}
        self.panel_cache: dict[tuple[object, ...], pygame.Surface] = {}
        self.box_cache: dict[tuple[object, ...], pygame.Surface] = {}
        self.text_cache: dict[
            tuple[int, str, tuple[int, int, int], bool],
            tuple[pygame.Surface, pygame.Surface | None],
        ] = {}
        self.text_commands: list[TextCommand] = []
        self.joysticks: list[pygame.joystick.JoystickType] = []
        self.joystick_names: tuple[str, ...] = ()
        self.refresh_joysticks()

    @staticmethod
    def _enable_windows_dpi_awareness() -> None:
        if os.name != "nt":
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            try:
                user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            except (AttributeError, OSError):
                user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass

    @staticmethod
    def _set_windows_identity() -> None:
        if os.name != "nt":
            return
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "FF12.ZodiacCoop.Launcher"
            )
        except (AttributeError, OSError):
            pass

    def _set_icon(self) -> None:
        try:
            self._window_icon = pygame.image.load(str(core.bundled_asset("icon.ico")))
            pygame.display.set_icon(self._window_icon)
        except (FileNotFoundError, pygame.error):
            self._window_icon = None

    def _set_native_window_icons(self) -> None:
        if os.name != "nt" or self.preview:
            return
        handles: list[int] = []
        try:
            import ctypes
            from ctypes import wintypes

            hwnd = int(pygame.display.get_wm_info().get("window") or 0)
            if not hwnd:
                return

            user32 = ctypes.windll.user32
            load_image = user32.LoadImageW
            load_image.argtypes = (
                wintypes.HINSTANCE,
                wintypes.LPCWSTR,
                wintypes.UINT,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            )
            load_image.restype = wintypes.HANDLE

            send_message = user32.SendMessageW
            send_message.argtypes = (
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            send_message.restype = ctypes.c_ssize_t

            get_system_metrics = user32.GetSystemMetrics
            get_system_metrics.argtypes = (ctypes.c_int,)
            get_system_metrics.restype = ctypes.c_int

            image_icon = 1
            load_from_file = 0x0010
            wm_seticon = 0x0080
            icon_small = 0
            icon_big = 1
            sm_cxicon, sm_cyicon = 11, 12
            sm_cxsmicon, sm_cysmicon = 49, 50
            path = str(core.bundled_asset("icon.ico"))

            dimensions = (
                (
                    icon_small,
                    max(1, get_system_metrics(sm_cxsmicon)),
                    max(1, get_system_metrics(sm_cysmicon)),
                ),
                (
                    icon_big,
                    max(1, get_system_metrics(sm_cxicon)),
                    max(1, get_system_metrics(sm_cyicon)),
                ),
            )
            for icon_kind, width, height in dimensions:
                handle = int(
                    load_image(
                        None,
                        path,
                        image_icon,
                        width,
                        height,
                        load_from_file,
                    )
                    or 0
                )
                if handle:
                    send_message(hwnd, wm_seticon, icon_kind, handle)
                    handles.append(handle)
            self._native_icon_handles = tuple(handles)
        except (AttributeError, OSError, TypeError, ValueError):
            self._native_icon_handles = tuple(handles)

    def _release_native_window_icons(self) -> None:
        handles, self._native_icon_handles = self._native_icon_handles, ()
        if os.name != "nt" or not handles:
            return
        try:
            import ctypes
            from ctypes import wintypes

            destroy_icon = ctypes.windll.user32.DestroyIcon
            destroy_icon.argtypes = (wintypes.HANDLE,)
            destroy_icon.restype = wintypes.BOOL
            for handle in handles:
                destroy_icon(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            pass

    def close(self) -> None:
        try:
            if self.tray is not None:
                self.tray.close()
            controls.release_sdl_controllers()
            pygame.quit()
        finally:
            self._release_native_window_icons()

    def native_window_handle(self) -> int:
        if os.name != "nt" or self.preview:
            return 0
        try:
            return int(pygame.display.get_wm_info().get("window") or 0)
        except (AttributeError, pygame.error, TypeError, ValueError):
            return 0

    def hide_to_tray(self) -> bool:
        if self.in_tray:
            return True
        if self.tray is None or not self.tray.show():
            pygame.display.iconify()
            return False
        hwnd = self.native_window_handle()
        if not hwnd:
            self.tray.hide()
            pygame.display.iconify()
            return False
        ctypes.windll.user32.ShowWindow(hwnd, 0)
        self.in_tray = True
        self.window_focused = False
        self.suspend_controller_discovery()
        return True

    def restore_from_tray(self) -> None:
        if not self.in_tray:
            return
        if self.tray is not None:
            self.tray.hide()
        hwnd = self.native_window_handle()
        if hwnd:
            user32 = ctypes.windll.user32
            user32.ShowWindowAsync(hwnd, 9)
            user32.ShowWindowAsync(hwnd, 5)
            user32.SetForegroundWindow(hwnd)
        self.in_tray = False
        self.window_focused = True
        self.resume_controller_discovery()
        self.scene_dirty = True

    def request_close(self) -> None:
        if self.launcher.session_active:
            self.hide_to_tray()
            return
        self.running = False

    def exit_completely(self) -> None:
        self.running = False

    def update_tray_labels(self) -> None:
        tray = getattr(self, "tray", None)
        if tray is None:
            return
        if self.language == "pt":
            tray.set_menu_labels("Abrir configurador", "Fechar completamente")
        else:
            tray.set_menu_labels("Open configurator", "Exit completely")

    @staticmethod
    def _load_theme_selection() -> menu_themes.ThemeSelection:
        try:
            return menu_themes.load_selection()
        except (OSError, TypeError, ValueError):
            return menu_themes.ThemeSelection()

    def _load_background(self) -> pygame.Surface:
        source = pygame.image.load(str(core.bundled_asset("background.png"))).convert()
        return pygame.transform.smoothscale(source, LOGICAL_SIZE)

    @staticmethod
    def _make_static_layers() -> tuple[
        pygame.Surface,
        pygame.Surface,
        pygame.Surface,
    ]:
        veil = pygame.Surface(LOGICAL_SIZE, pygame.SRCALPHA)
        veil.fill((0, 3, 8, 48))

        wash = pygame.Surface(LOGICAL_SIZE, pygame.SRCALPHA)
        width = 1160
        for x in range(width):
            alpha = max(0, round(166 * (1.0 - (x / width) ** 2)))
            pygame.draw.line(
                wash,
                (2, 13, 25, alpha),
                (x, 0),
                (x, LOGICAL_SIZE[1] - 1),
            )

        # A complete frame keeps the interface grounded while leaving the
        # Amano artwork visible as an intentional third visual column.
        frame_points = (
            (31, 18),
            (1405, 18),
            (1421, 34),
            (1421, 776),
            (1405, 792),
            (31, 792),
            (18, 779),
            (18, 31),
        )
        pygame.draw.polygon(wash, (1, 8, 16, 31), frame_points)
        pygame.draw.lines(wash, (65, 124, 160, 72), True, frame_points, 1)
        pygame.draw.line(wash, (231, 190, 91, 118), (48, 19), (552, 19), 1)
        pygame.draw.line(wash, (76, 165, 210, 80), (1078, 791), (1398, 791), 1)

        rail = pygame.Surface((226, 766), pygame.SRCALPHA)
        for y in range(rail.get_height()):
            t = y / max(1, rail.get_height() - 1)
            pygame.draw.line(
                rail,
                (4, 18 + round(9 * (1.0 - t)), 33 + round(13 * (1.0 - t)), 204),
                (0, y),
                (rail.get_width() - 1, y),
            )
        pygame.draw.rect(rail, (58, 126, 167, 128), rail.get_rect(), 1)
        pygame.draw.line(rail, (235, 196, 105, 150), (12, 1), (112, 1), 2)
        pygame.draw.line(
            rail,
            (88, 171, 211, 62),
            (rail.get_width() - 2, 20),
            (rail.get_width() - 2, rail.get_height() - 20),
            1,
        )
        wash.blit(rail, (31, 22))

        # Fine cartographic lines echo the game's menu ornament without adding
        # another bitmap asset or competing with the Yoshitaka Amano artwork.
        for radius, alpha in ((72, 22), (92, 17), (116, 13)):
            pygame.draw.arc(
                wash,
                (79, 164, 205, alpha),
                pygame.Rect(119 - radius, 572 - radius, radius * 2, radius * 2),
                0.24,
                5.36,
                1,
            )
        for y in range(93, 731, 74):
            pygame.draw.line(wash, (68, 132, 171, 18), (276, y), (1037, y), 1)

        for inset in range(30):
            alpha = round((1.0 - inset / 30.0) * 5)
            pygame.draw.rect(
                wash,
                (0, 0, 0, alpha),
                pygame.Rect(inset, inset, 1440 - inset * 2, 810 - inset * 2),
                1,
            )

        capture_shade = pygame.Surface(LOGICAL_SIZE, pygame.SRCALPHA)
        capture_shade.fill((0, 4, 10, 205))
        return veil, wash, capture_shade

    def _load_cursor(self) -> pygame.Surface:
        atlas = pygame.image.load(
            str(core.bundled_asset("cursol_4_p.png"))
        ).convert_alpha()
        hand = atlas.subsurface(pygame.Rect(0, 128, 64, 54)).copy()
        return pygame.transform.smoothscale(hand, (58, 49))

    def _load_portraits(self) -> dict[int, pygame.Surface]:
        result: dict[int, pygame.Surface] = {}
        for profile in menu_themes.PLAYABLE_CHARACTER_PROFILES:
            try:
                path = core.bundled_portrait(profile.portrait_filename)
                result[profile.member_id] = pygame.image.load(str(path)).convert_alpha()
            except (FileNotFoundError, pygame.error):
                continue
        return result

    def _make_cursor_ghosts(self) -> tuple[pygame.Surface, ...]:
        ghosts = []
        for alpha in (85, 42):
            ghost = self.cursor.copy()
            ghost.fill((125, 185, 255, alpha), special_flags=pygame.BLEND_RGBA_MULT)
            ghosts.append(ghost)
        return tuple(ghosts)

    @staticmethod
    def _make_motes() -> tuple[tuple[int, int, float, float], ...]:
        return tuple(
            (
                278 + (index * 73) % 660,
                78 + (index * 97) % 650,
                0.3 + (index % 5) * 0.11,
                0.55 + (index % 7) * 0.09,
            )
            for index in range(26)
        )

    def refresh_joysticks(self) -> None:
        # Only devices supported by SDL GameController can be assigned. Raw
        # DirectInput order has no relationship with XInput slot order and must
        # never be used to guess a controller's name.
        self.joysticks = []
        self.joystick_names = tuple(
            str(state.assignment.get("name", "SDL controller"))
            for state in getattr(self, "sdl_states", ())
        )

    def controller_name(self, slot: int) -> str:
        aliases = controls.aliased_sdl_assignments(self.editor.config, slot)
        alias = next(
            (
                candidate
                for candidate in aliases
                if controls.resolve_sdl_state(
                    getattr(self, "sdl_states", ()),
                    candidate,
                )
                is not None
            ),
            aliases[0] if aliases else None,
        )
        if alias is not None:
            return str(alias["name"])
        return f"Controle XInput {slot + 1}"

    @staticmethod
    def _controller_state_signature(
        states: list[Any],
        sdl_states: tuple[controls.SDLControllerState, ...],
    ) -> tuple[Any, ...]:
        return (
            tuple(state is not None for state in states),
            tuple(
                (
                    controls.device_assignment_key(state.assignment),
                    str(state.assignment.get("name", "")),
                )
                for state in sdl_states
            ),
        )

    def refresh_controller_states(self) -> bool:
        if getattr(self, "controller_discovery_suspended", False):
            return False
        previous_sdl = getattr(self, "sdl_states", ())
        before = self._controller_state_signature(self.states, previous_sdl)
        self.states = controls.read_xinput_states()
        self.sdl_states = controls.read_sdl_controller_states()
        editor = getattr(self, "editor", None)
        if editor is not None and controls.upgrade_sdl_identities(
            editor.config,
            self.sdl_states,
        ):
            was_dirty = bool(editor.dirty)
            editor.dirty = True
            if not was_dirty and not self.capture_active:
                try:
                    editor.save()
                except OSError:
                    editor.dirty = True
        self.refresh_joysticks()
        after = self._controller_state_signature(self.states, self.sdl_states)
        return before != after

    def suspend_controller_discovery(self) -> bool:
        """Release SDL devices while the configurator cannot use them."""
        if getattr(self, "controller_discovery_suspended", False):
            return False
        had_sdl_devices = bool(getattr(self, "sdl_states", ()))
        controls.release_sdl_controllers()
        self.controller_discovery_suspended = True
        self.sdl_states = ()
        self.joysticks = []
        self.joystick_names = ()
        return had_sdl_devices

    def resume_controller_discovery(self) -> bool:
        """Resume SDL discovery while the Controls page is visible and focused.

        SDL state polling is non-consuming, so the live runtime and configurator
        can observe the same controller.  Configuration writes remain atomic and
        runtime pollers absorb held controls when they reload the new snapshot.
        """
        if (
            self.in_tray
            or self.page != "controls"
            or not getattr(self, "window_focused", True)
        ):
            return False
        was_suspended = bool(
            getattr(self, "controller_discovery_suspended", False)
        )
        self.controller_discovery_suspended = False
        return self.refresh_controller_states() or was_suspended

    def read_window_events(self, now: float) -> tuple[pygame.event.Event, ...]:
        """Keep one transient SDL removal from terminating the configurator."""
        try:
            return tuple(pygame.event.get())
        except (pygame.error, SystemError):
            self.suspend_controller_discovery()
            self.controller_recovery_at = now + 0.75
            return ()

    def show_status(
        self, message: str, kind: str = "normal", duration: float = 4.5
    ) -> None:
        self.status_message = message
        self.status_kind = kind
        self.status_until = time.monotonic() + duration

    def reset_pointer(self) -> None:
        self.pointer_y = None
        self.pointer_trail.clear()

    def register(
        self,
        key: str,
        rect: pygame.Rect,
        action: Callable[[], None] | None = None,
        adjust: Callable[[int], None] | None = None,
        clear: Callable[[], None] | None = None,
    ) -> bool:
        self.items.append(Item(key, rect.copy(), action, adjust, clear))
        return self.selected_key == key

    def item(self, key: str | None = None) -> Item | None:
        target = self.selected_key if key is None else key
        return next((item for item in self.items if item.key == target), None)

    @property
    def capture_active(self) -> bool:
        return (
            self.editor.capture_target is not None
            or self.editor.assign_target is not None
        )

    def set_page(self, page: str) -> None:
        if page == self.page:
            return
        self.page = page
        if hasattr(self, "controller_discovery_suspended"):
            if page == "controls":
                self.resume_controller_discovery()
            else:
                self.suspend_controller_discovery()
        defaults = {
            "home": "home:start_all",
            "colors": f"colors:mode:{self.theme_mode}",
            "controls": f"controls:tab:{self.control_player}",
            "settings": f"settings:language:{self.language}",
        }
        self.selected_key = defaults[page]
        self.reset_pointer()
        self.audio.play("open")

    def draw_text(
        self,
        surface: pygame.Surface,
        font: pygame.font.Font,
        text: str,
        position: tuple[int, int],
        color: tuple[int, int, int] = TEXT,
        *,
        anchor: str = "topleft",
        shadow: bool = True,
    ) -> pygame.Rect:
        text = self.localize(text)
        key = (id(font), text, tuple(color), bool(shadow))
        cached = self.text_cache.get(key)
        if cached is None:
            rendered = font.render(text, True, color)
            shade = font.render(text, True, (0, 0, 0)) if shadow else None
            if len(self.text_cache) >= TEXT_CACHE_LIMIT:
                self.text_cache.clear()
            self.text_cache[key] = (rendered, shade)
        else:
            rendered, shade = cached
        rect = rendered.get_rect()
        setattr(rect, anchor, position)
        if (
            surface is self.canvas
            and not self.preview
            and abs(self.output_scale() - 1.0) > 0.001
        ):
            self.text_commands.append(
                TextCommand(
                    font,
                    text,
                    tuple(position),
                    tuple(color),
                    anchor,
                    bool(shadow),
                    rect.copy(),
                )
            )
            return rect
        if shade is not None:
            surface.blit(shade, rect.move(2, 2))
        surface.blit(rendered, rect)
        return rect

    def discard_deferred_text(self, rect: pygame.Rect) -> None:
        self.text_commands = [
            command for command in self.text_commands if not command.rect.colliderect(rect)
        ]

    @staticmethod
    def draw_aa_line(
        surface: pygame.Surface,
        color: tuple[int, ...],
        start: tuple[int, int],
        end: tuple[int, int],
        width: int = 1,
    ) -> None:
        if width <= 1:
            pygame.draw.aaline(surface, color, start, end)
            return
        scale = PANEL_AA_SCALE
        padding = width + 2
        left = min(start[0], end[0]) - padding
        top = min(start[1], end[1]) - padding
        right = max(start[0], end[0]) + padding
        bottom = max(start[1], end[1]) + padding
        logical = pygame.Rect(left, top, right - left + 1, bottom - top + 1)
        high = pygame.Surface(
            (logical.width * scale, logical.height * scale), pygame.SRCALPHA
        )
        local_start = (
            (start[0] - logical.x) * scale,
            (start[1] - logical.y) * scale,
        )
        local_end = (
            (end[0] - logical.x) * scale,
            (end[1] - logical.y) * scale,
        )
        pygame.draw.line(high, color, local_start, local_end, width * scale)
        surface.blit(pygame.transform.smoothscale(high, logical.size), logical)

    @staticmethod
    def draw_aa_polygon(
        surface: pygame.Surface,
        color: tuple[int, ...],
        points: tuple[tuple[int, int], ...] | list[tuple[int, int]],
    ) -> None:
        scale = PANEL_AA_SCALE
        left = min(point[0] for point in points) - 1
        top = min(point[1] for point in points) - 1
        right = max(point[0] for point in points) + 1
        bottom = max(point[1] for point in points) + 1
        logical = pygame.Rect(left, top, right - left + 1, bottom - top + 1)
        high = pygame.Surface(
            (logical.width * scale, logical.height * scale), pygame.SRCALPHA
        )
        local = tuple(
            ((x - logical.x) * scale, (y - logical.y) * scale) for x, y in points
        )
        pygame.draw.polygon(high, color, local)
        surface.blit(pygame.transform.smoothscale(high, logical.size), logical)

    @staticmethod
    def draw_aa_circle(
        surface: pygame.Surface,
        color: tuple[int, ...],
        center: tuple[int, int],
        radius: int,
    ) -> None:
        scale = PANEL_AA_SCALE
        diameter = radius * 2 + 4
        logical = pygame.Rect(
            center[0] - radius - 2,
            center[1] - radius - 2,
            diameter,
            diameter,
        )
        high = pygame.Surface(
            (logical.width * scale, logical.height * scale), pygame.SRCALPHA
        )
        pygame.draw.circle(
            high,
            color,
            (logical.width * scale // 2, logical.height * scale // 2),
            radius * scale,
        )
        surface.blit(pygame.transform.smoothscale(high, logical.size), logical)

    def fit_text(self, font: pygame.font.Font, text: str, width: int) -> str:
        text = self.localize(text)
        if font.size(text)[0] <= width:
            return text
        suffix = "…"
        value = text
        while value and font.size(value + suffix)[0] > width:
            value = value[:-1]
        return value.rstrip() + suffix

    @property
    def language(self) -> str:
        value = self.config.get("language", "en")
        return value if value in ("en", "pt") else "en"

    def localize(self, text: str) -> str:
        return launcher_i18n.translate(text, self.language)

    @staticmethod
    def _panel_points(rect: pygame.Rect, cut: int = 10) -> list[tuple[int, int]]:
        return [
            (rect.x + cut, rect.y),
            (rect.right - 1, rect.y),
            (rect.right - 1, rect.bottom - cut - 1),
            (rect.right - cut - 1, rect.bottom - 1),
            (rect.x, rect.bottom - 1),
            (rect.x, rect.y + cut),
        ]

    def draw_panel(
        self,
        surface: pygame.Surface,
        rect: pygame.Rect,
        *,
        selected: bool = False,
        strong: bool = False,
        accent: tuple[int, int, int] | None = None,
    ) -> None:
        cache_key = (
            rect.width,
            rect.height,
            bool(selected),
            bool(strong),
            tuple(accent) if accent is not None else None,
        )
        shadow_key = ("panel_shadow", rect.width, rect.height, bool(strong))
        shadow = self.panel_cache.get(shadow_key)
        if shadow is None:
            shadow = pygame.Surface(rect.size, pygame.SRCALPHA)
            local_shadow = pygame.Rect(0, 0, rect.width, rect.height)
            pygame.draw.polygon(
                shadow,
                (0, 0, 0, 142 if strong else 105),
                self._panel_points(local_shadow, cut=10),
            )
            self.panel_cache[shadow_key] = shadow
        surface.blit(shadow, rect.move(4, 5))

        cached = self.panel_cache.get(cache_key)
        if cached is not None:
            surface.blit(cached, rect)
            return

        scale = PANEL_AA_SCALE
        high_size = (rect.width * scale, rect.height * scale)
        panel = pygame.Surface(high_size, pygame.SRCALPHA)
        top = PANEL_TOP if strong else (11, 34, 57, 228)
        bottom = PANEL_BOTTOM if strong else (3, 15, 29, 239)
        if selected:
            top = (28, 67, 101, 249)
            bottom = (7, 25, 45, 251)
        for y in range(high_size[1]):
            t = y / max(1, high_size[1] - 1)
            color = tuple(round(top[i] * (1.0 - t) + bottom[i] * t) for i in range(4))
            pygame.draw.line(panel, color, (0, y), (high_size[0], y))
        local = pygame.Rect(0, 0, high_size[0], high_size[1])
        points = self._panel_points(local, cut=10 * scale)
        mask = pygame.Surface(high_size, pygame.SRCALPHA)
        pygame.draw.polygon(mask, (255, 255, 255, 255), points)
        panel.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        line = accent or (GOLD_LIGHT if selected else (83, 121, 151))
        pygame.draw.lines(
            panel,
            (*line, 238),
            True,
            points,
            max(scale, (2 if selected else 1) * scale),
        )
        top_line = accent or (GOLD if selected else (40, 104, 151))
        pygame.draw.line(
            panel,
            (*top_line, 250),
            (11 * scale, 2 * scale),
            (high_size[0] - 3 * scale, 2 * scale),
            (2 if selected else 1) * scale,
        )
        pygame.draw.line(
            panel,
            (148, 210, 236, 88 if strong or selected else 44),
            (12 * scale, 6 * scale),
            (high_size[0] - 10 * scale, 6 * scale),
            scale,
        )
        if selected:
            pygame.draw.polygon(
                panel,
                (*GOLD_LIGHT, 235),
                (
                    (3 * scale, 13 * scale),
                    (7 * scale, 9 * scale),
                    (11 * scale, 13 * scale),
                    (7 * scale, 17 * scale),
                ),
            )
            pygame.draw.line(
                panel,
                (*BLUE_LIGHT, 92),
                (12 * scale, high_size[1] - 8 * scale),
                (high_size[0] - 18 * scale, high_size[1] - 8 * scale),
                scale,
            )
        if strong:
            pygame.draw.line(
                panel,
                (38, 93, 136, 210),
                (4 * scale, high_size[1] - 4 * scale),
                (high_size[0] - 12 * scale, high_size[1] - 4 * scale),
                scale,
            )
        cached = pygame.transform.smoothscale(panel, rect.size)
        self.panel_cache[cache_key] = cached
        surface.blit(cached, rect)

    def draw_aa_box(
        self,
        rect: pygame.Rect,
        fill: tuple[int, ...],
        border: tuple[int, ...],
        *,
        width: int = 1,
        radius: int = 3,
    ) -> None:
        cache_key = (
            rect.width,
            rect.height,
            tuple(fill),
            tuple(border),
            int(width),
            int(radius),
        )
        cached = self.box_cache.get(cache_key)
        if cached is None:
            scale = PANEL_AA_SCALE
            size = (rect.width * scale, rect.height * scale)
            high = pygame.Surface(size, pygame.SRCALPHA)
            full = pygame.Rect(0, 0, *size)
            pygame.draw.rect(
                high,
                fill,
                full,
                border_radius=max(0, radius * scale),
            )
            pygame.draw.rect(
                high,
                border,
                full,
                max(scale, width * scale),
                border_radius=max(0, radius * scale),
            )
            cached = pygame.transform.smoothscale(high, rect.size)
            self.box_cache[cache_key] = cached
        self.canvas.blit(cached, rect)

    def draw_checkmark(self, rect: pygame.Rect, color: tuple[int, int, int]) -> None:
        scale = PANEL_AA_SCALE
        high = pygame.Surface(
            (rect.width * scale, rect.height * scale), pygame.SRCALPHA
        )
        points = (
            (round(rect.width * 0.20) * scale, round(rect.height * 0.51) * scale),
            (round(rect.width * 0.41) * scale, round(rect.height * 0.72) * scale),
            (round(rect.width * 0.80) * scale, round(rect.height * 0.24) * scale),
        )
        pygame.draw.lines(
            high,
            (*color, 255),
            False,
            points,
            max(scale, round(rect.width * 0.085) * scale),
        )
        self.canvas.blit(pygame.transform.smoothscale(high, rect.size), rect)

    def draw_badge(
        self,
        rect: pygame.Rect,
        label: str,
        *,
        active: bool = False,
        selected: bool = False,
        color: tuple[int, int, int] = GOLD_LIGHT,
    ) -> None:
        fill = (45, 77, 97) if active else (18, 31, 44)
        self.draw_aa_box(
            rect,
            fill,
            color if active or selected else (75, 93, 108),
            width=2 if selected else 1,
            radius=3,
        )
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            label,
            rect.center,
            color if active or selected else MUTED,
            anchor="center",
            shadow=False,
        )

    def scaled_portrait(
        self, member_id: int, max_width: int, max_height: int
    ) -> pygame.Surface | None:
        original = self.portraits.get(member_id)
        if original is None:
            return None
        key = (member_id, max_width, max_height)
        cached = self.portrait_cache.get(key)
        if cached is not None:
            return cached
        scale = min(
            max_width / original.get_width(), max_height / original.get_height()
        )
        size = (
            max(1, round(original.get_width() * scale)),
            max(1, round(original.get_height() * scale)),
        )
        cached = pygame.transform.smoothscale(original, size)
        self.portrait_cache[key] = cached
        return cached

    def draw_portrait(
        self,
        member_id: int | None,
        rect: pygame.Rect,
        *,
        glow: tuple[int, int, int] = BLUE_LIGHT,
    ) -> None:
        frame_key = (rect.width, rect.height, glow)
        frame = self.portrait_frame_cache.get(frame_key)
        if frame is None:
            scale = PORTRAIT_AA_SCALE
            frame = pygame.Surface(
                (rect.width * scale, rect.height * scale),
                pygame.SRCALPHA,
            )
            ellipse = pygame.Rect(
                2 * scale,
                4 * scale,
                max(scale, (rect.width - 4) * scale),
                max(scale, (rect.height - 8) * scale),
            )
            pygame.draw.ellipse(frame, (7, 15, 24, 175), ellipse)
            pygame.draw.arc(
                frame,
                (*glow, 160),
                ellipse,
                0.15,
                2.96,
                2 * scale,
            )
            frame = pygame.transform.smoothscale(frame, rect.size)
            self.portrait_frame_cache[frame_key] = frame
        self.canvas.blit(frame, rect)
        if member_id is None:
            self.draw_text(
                self.canvas,
                self.fonts.subtitle,
                "—",
                rect.center,
                FAINT,
                anchor="center",
            )
            return
        zoom = PORTRAIT_ZOOM.get(member_id, 1.08)
        portrait = self.scaled_portrait(
            member_id,
            max(1, round((rect.width - 8) * zoom)),
            max(1, round((rect.height - 4) * zoom)),
        )
        if portrait is None:
            self.draw_text(
                self.canvas,
                self.fonts.subtitle,
                "?",
                rect.center,
                MUTED,
                anchor="center",
            )
            return
        portrait_layer = pygame.Surface(rect.size, pygame.SRCALPHA)
        if member_id == 2 and portrait.get_height() > rect.height - 2:
            # Fran's source includes her full ears; keep those visible and let
            # the icon crop the less important shoulder area instead.
            target = portrait.get_rect(midtop=(rect.width // 2, 0))
        else:
            target = portrait.get_rect(midbottom=(rect.width // 2, rect.height - 2))
        shadow = portrait.copy()
        shadow.fill((0, 0, 0, 115), special_flags=pygame.BLEND_RGBA_MULT)
        portrait_layer.blit(shadow, target.move(2, 2))
        portrait_layer.blit(portrait, target)
        self.canvas.blit(portrait_layer, rect)

    def draw_header(
        self,
        title: str,
        kicker: str,
        *,
        width: int = CONTENT_W,
    ) -> None:
        page_number = {
            "home": "01",
            "colors": "02",
            "controls": "03",
            "settings": "04",
        }.get(getattr(self, "page", ""), "--")
        localized_kicker = self.localize(kicker)
        kicker_width = min(
            280,
            max(184, self.fonts.micro.size(localized_kicker)[0] + 28),
        )
        kicker_rect = pygame.Rect(CONTENT_X, 78, kicker_width, 23)
        self.draw_aa_box(
            kicker_rect,
            (10, 32, 48, 236),
            (91, 153, 184),
            radius=3,
        )
        self.draw_aa_polygon(
            self.canvas,
            GOLD_LIGHT,
            (
                (kicker_rect.x + 8, kicker_rect.centery),
                (kicker_rect.x + 12, kicker_rect.centery - 4),
                (kicker_rect.x + 16, kicker_rect.centery),
                (kicker_rect.x + 12, kicker_rect.centery + 4),
            ),
        )
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            kicker,
            (kicker_rect.x + 23, kicker_rect.centery),
            GOLD_LIGHT,
            anchor="midleft",
            shadow=False,
        )
        has_title = bool(title.strip())
        if has_title:
            self.draw_text(
                self.canvas,
                self.fonts.title,
                title,
                (CONTENT_X - 2, 106),
                WHITE,
            )
        right = CONTENT_X + width
        page_rect = pygame.Rect(right - 112, 78, 112, 23)
        self.draw_aa_box(
            page_rect,
            (8, 24, 39, 218),
            (61, 112, 143),
            radius=3,
        )
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            f"{'SECTION' if getattr(self, 'language', 'pt') == 'en' else 'SEÇÃO'}  "
            f"{page_number}  /  04",
            page_rect.center,
            BLUE_LIGHT,
            anchor="center",
            shadow=False,
        )
        line_y = 153 if has_title else 114
        self.draw_aa_line(self.canvas, GOLD, (CONTENT_X, line_y), (right, line_y))
        self.draw_aa_polygon(
            self.canvas,
            GOLD_LIGHT,
            (
                (CONTENT_X - 5, line_y),
                (CONTENT_X, line_y - 5),
                (CONTENT_X + 5, line_y),
                (CONTENT_X, line_y + 5),
            ),
        )
        self.draw_aa_line(
            self.canvas,
            (57, 134, 180),
            (right - 82, line_y + 4),
            (right, line_y + 4),
        )

    def draw_nav(self) -> None:
        self.draw_aa_polygon(
            self.canvas,
            GOLD_LIGHT,
            ((59, 44), (64, 39), (69, 44), (64, 49)),
        )
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            "FINAL FANTASY XII",
            (77, 38),
            (116, 150, 171),
            shadow=False,
        )
        self.draw_text(self.canvas, self.fonts.title, "IVALICE", (57, 57), WHITE)
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            "T O G E T H E R",
            (60, 101),
            GOLD_LIGHT,
            shadow=False,
        )
        self.draw_aa_line(self.canvas, GOLD, (60, 123), (222, 123))
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            "LOCAL CO-OP  ·  2–3P",
            (61, 136),
            (93, 132, 158),
            shadow=False,
        )
        self.draw_aa_line(self.canvas, (57, 115, 153), (82, 176), (82, 415))

        subtitles = {
            "home": "SESSÃO",
            "colors": "IDENTIDADE",
            "controls": "ENTRADAS",
            "settings": "SISTEMA",
        }
        for index, (page, label) in enumerate(PAGES):
            rect = pygame.Rect(62, 177 + index * 59, 181, 47)
            selected = self.register(
                f"nav:{page}", rect, lambda value=page: self.set_page(value)
            )
            active = page == self.page
            if selected or active:
                self.draw_aa_box(
                    rect,
                    (16, 54, 82, 224 if selected else 185),
                    GOLD_LIGHT if selected else (61, 131, 174),
                    width=2 if selected else 1,
                    radius=4,
                )
            if active:
                self.draw_aa_line(
                    self.canvas,
                    GOLD_LIGHT,
                    (rect.x + 1, rect.y + 5),
                    (rect.x + 1, rect.bottom - 5),
                    3,
                )
            node = pygame.Rect(rect.x + 10, rect.centery - 11, 22, 22)
            self.draw_badge(
                node,
                f"{index + 1:02d}",
                active=active,
                selected=selected,
                color=GOLD_LIGHT if active or selected else BLUE_LIGHT,
            )
            self.draw_text(
                self.canvas,
                self.fonts.label,
                label,
                (rect.x + 43, rect.y + 8),
                WHITE if active or selected else MUTED,
                shadow=False,
            )
            self.draw_text(
                self.canvas,
                self.fonts.micro,
                subtitles[page],
                (rect.x + 43, rect.y + 27),
                GOLD_LIGHT if active else (98, 130, 151),
                shadow=False,
            )

        system = pygame.Rect(55, 678, 178, 82)
        self.draw_aa_box(
            system,
            (5, 21, 35, 226),
            (46, 100, 133),
            radius=4,
        )
        launcher_state = getattr(getattr(self, "launcher", None), "state", "idle")
        if launcher_state == "ready":
            system_color = GREEN
            system_label = "CO-OP ATIVO"
        elif getattr(self, "game_open", False):
            system_color = GOLD_LIGHT
            system_label = "CO-OP AUTOMÁTICO"
        else:
            system_color = (94, 132, 158)
            system_label = "AGUARDANDO JOGO"
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            "STATUS DO SISTEMA",
            (system.x + 12, system.y + 11),
            (91, 132, 158),
            shadow=False,
        )
        self.draw_aa_circle(
            self.canvas, system_color, (system.x + 13, system.y + 40), 4
        )
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            system_label,
            (system.x + 25, system.y + 34),
            system_color,
            shadow=False,
        )
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            f"LOCAL  ·  2–3P  ·  v{core.LAUNCHER_VERSION}",
            (system.x + 12, system.y + 59),
            (74, 108, 133),
            shadow=False,
        )

    def role_member(self, player: str) -> int | None:
        if player == "p1":
            return coop_roster.VAAN_ID
        return getattr(self.roster, player)

    def role_name(self, player: str) -> str:
        member_id = self.role_member(player)
        if member_id is None:
            return "Desativado"
        if member_id == coop_roster.VAAN_ID:
            return "Vaan"
        return coop_roster.MEMBER_NAMES.get(member_id, f"Membro {member_id}")

    def draw_home(self) -> None:
        self.draw_header("", "CONFIGURAÇÃO")
        status = pygame.Rect(CONTENT_X, 137, CONTENT_W, 64)
        self.draw_panel(self.canvas, status, strong=True)
        dot = GREEN if self.game_open else GOLD_LIGHT
        self.draw_aa_circle(self.canvas, dot, (status.x + 25, status.centery), 7)
        self.draw_text(
            self.canvas,
            self.fonts.label,
            "JOGO ABERTO" if self.game_open else "JOGO FECHADO",
            (status.x + 45, status.y + 15),
            WHITE,
        )
        detail = (
            "A sessão pode ser aplicada agora."
            if self.game_open
            else "Abra pela Steam ou pelo perfil modificado do MO2."
        )
        self.draw_text(
            self.canvas,
            self.fonts.body_small,
            detail,
            (status.x + 45, status.y + 36),
            MUTED,
        )
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            "AUTOSTART VIA MODULE LOADER",
            (status.right - 20, status.centery),
            GREEN if self.game_open else FAINT,
            anchor="midright",
        )

        formation = pygame.Rect(CONTENT_X, 218, CONTENT_W, 216)
        self.draw_panel(self.canvas, formation, strong=True)
        start_all = self.roster.start_with_all
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            "FORMAÇÃO NO JOGO",
            (formation.x + 20, formation.y + 15),
            GOLD_LIGHT,
        )
        roster_toggle = pygame.Rect(
            formation.x + 14,
            formation.y + 36,
            formation.width - 28,
            44,
        )
        roster_selected = self.register(
            "home:start_all",
            roster_toggle,
            self.toggle_start_with_all,
        )
        self.draw_aa_box(
            roster_toggle,
            (13, 37, 45, 245) if start_all else (10, 24, 37, 235),
            GREEN
            if start_all
            else ((126, 150, 166) if roster_selected else (72, 94, 111)),
            width=2 if roster_selected else 1,
            radius=5,
        )
        check = pygame.Rect(roster_toggle.x + 10, roster_toggle.y + 9, 26, 26)
        self.draw_aa_box(
            check,
            (13, 37, 45, 245) if start_all else (7, 18, 29, 245),
            GREEN if start_all else (91, 112, 128),
            width=2,
            radius=4,
        )
        if start_all:
            self.draw_checkmark(check, GREEN)
        self.draw_text(
            self.canvas,
            self.fonts.label,
            "COMEÇAR COM TODOS OS PERSONAGENS",
            (roster_toggle.x + 47, roster_toggle.y + 5),
            WHITE,
        )
        roster_detail = (
            "Vaan solo; Party, Gambits e os cinco aliados liberados."
            if start_all
            else "Desmarcado: personagens seguem os eventos da história."
        )
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            roster_detail,
            (roster_toggle.x + 47, roster_toggle.y + 25),
            GREEN if start_all else MUTED,
        )
        members = (0, 3, 2, 4, 1, 5)
        for index, member_id in enumerate(members):
            tile = pygame.Rect(
                formation.x + 17 + index * 103,
                formation.y + 88,
                88,
                112,
            )
            active = member_id == coop_roster.VAAN_ID
            available = active or start_all
            accent = BLUE_LIGHT if active else (GREEN if available else (65, 83, 98))
            self.draw_aa_box(
                tile,
                (12, 29, 47, 228) if available else (7, 16, 27, 205),
                accent,
                width=2 if active else 1,
                radius=5,
            )
            self.draw_portrait(
                member_id,
                pygame.Rect(tile.centerx - 27, tile.y + 5, 54, 75),
                glow=accent,
            )
            name = "Vaan" if active else coop_roster.MEMBER_NAMES[member_id]
            self.draw_text(
                self.canvas,
                self.fonts.micro,
                name,
                (tile.centerx, tile.bottom - 23),
                WHITE if available else MUTED,
                anchor="center",
                shadow=False,
            )
            self.draw_text(
                self.canvas,
                self.fonts.micro,
                "ATIVO" if active else ("RESERVA" if start_all else "HISTÓRIA"),
                (tile.centerx, tile.bottom - 8),
                accent,
                anchor="center",
                shadow=False,
            )

        start = pygame.Rect(CONTENT_X, 450, CONTENT_W, 72)
        self.draw_panel(self.canvas, start, strong=True, accent=GREEN)
        self.draw_text(
            self.canvas,
            self.fonts.button,
            "CONFIGURAÇÃO SALVA",
            (start.centerx, start.centery - 6),
            GREEN if self.launcher.state == "ready" else WHITE,
            anchor="center",
        )
        subtitle = (
            "CO-OP ATIVO"
            if self.launcher.state == "ready"
            else "INICIE O JOGO PELA STEAM OU PELO FFXII (MODDED) DO MO2"
        )
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            subtitle,
            (start.centerx, start.bottom - 16),
            GREEN if self.launcher.state == "ready" else MUTED,
            anchor="center",
        )

        info = pygame.Rect(CONTENT_X, 540, CONTENT_W, 146)
        self.draw_panel(self.canvas, info, strong=True)
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            "CONFIGURE UMA VEZ, INICIE COMO PREFERIR",
            (info.x + 18, info.y + 14),
            GOLD_LIGHT,
        )
        steps = (
            ("01", "ELENCO", "Party e Gambits"),
            ("02", "IDENTIDADE", "HUD, menus e alvos"),
            ("03", "CONTROLES", "XInput e DirectInput"),
        )
        for index, (number, label, detail) in enumerate(steps):
            card = pygame.Rect(info.x + 17 + index * 207, info.y + 38, 196, 69)
            self.draw_aa_box(
                card,
                (7, 24, 39, 218),
                (48, 101, 134),
                radius=4,
            )
            self.draw_badge(
                pygame.Rect(card.x + 10, card.y + 11, 28, 28),
                number,
                active=True,
                color=BLUE_LIGHT,
            )
            self.draw_text(
                self.canvas,
                self.fonts.micro,
                label,
                (card.x + 49, card.y + 11),
                WHITE,
                shadow=False,
            )
            self.draw_text(
                self.canvas,
                self.fonts.micro,
                detail,
                (card.x + 49, card.y + 32),
                MUTED,
                shadow=False,
            )
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            "CONFIGURAÇÃO SALVA NO MOD · PRONTA PARA STEAM OU MO2",
            (info.centerx, info.bottom - 18),
            (91, 132, 158),
            anchor="center",
            shadow=False,
        )

    def toggle_start_with_all(self) -> None:
        enabled = not self.roster.start_with_all
        previous = self.roster
        try:
            self.roster = core.save_roster(self.roster.with_start_all(enabled))
            self.show_status(
                "Elenco completo será liberado com Vaan solo."
                if enabled
                else "Progressão nativa da história restaurada.",
                "ok",
            )
            self.audio.play("confirm")
        except OSError as error:
            self.roster = previous
            self.show_status(
                f"Não foi possível salvar a opção de elenco: {error}",
                "error",
            )
            self.audio.play("error")

    @property
    def theme_order(self) -> tuple[str, ...]:
        return tuple(name for name in THEME_LABELS if name in menu_themes.PRESETS)

    def theme_value(self, target: str | int) -> str:
        if isinstance(target, str):
            return self.player_theme_draft[target]
        return self.character_theme_draft[target]

    def open_theme_picker(self, target: str | int) -> None:
        self.theme_picker_target = target
        self.theme_picker_return_key = (
            f"colors:player:{target}"
            if isinstance(target, str)
            else f"colors:character:{target}"
        )
        self.selected_key = f"colors:theme:{self.theme_value(target)}"
        self.reset_pointer()
        self.audio.play("open")

    def close_theme_picker(self, *, sound: bool = True) -> None:
        if self.theme_picker_target is None:
            return
        self.theme_picker_target = None
        self.selected_key = self.theme_picker_return_key
        self.theme_picker_return_key = ""
        self.reset_pointer()
        if sound:
            self.audio.play("cancel")

    def select_theme(self, value: str) -> None:
        target = self.theme_picker_target
        if target is None or value not in self.theme_order:
            return
        previous = self.theme_value(target)
        if isinstance(target, str):
            self.player_theme_draft[target] = value
        else:
            self.character_theme_draft[target] = value
        self.theme_dirty = True
        if not self.save_themes():
            if isinstance(target, str):
                self.player_theme_draft[target] = previous
            else:
                self.character_theme_draft[target] = previous
            self.theme_dirty = False
            return
        self.close_theme_picker(sound=False)
        self.audio.play("confirm")

    def set_theme_mode(self, mode: str) -> None:
        if mode == self.theme_mode:
            return
        previous = self.theme_mode
        self.theme_mode = mode
        self.theme_dirty = True
        if not self.save_themes():
            self.theme_mode = previous
            self.theme_dirty = False
            return
        self.selected_key = f"colors:mode:{mode}"
        self.audio.play("confirm")

    def set_theme_group(self, group: str) -> None:
        self.theme_group = group
        self.selected_key = f"colors:group:{group}"
        self.audio.play("move")

    def save_themes(self) -> bool:
        selection = menu_themes.ThemeSelection(
            self.player_theme_draft["p1"],
            self.player_theme_draft["p2"],
            tuple(sorted(self.character_theme_draft.items())),
            player_3=self.player_theme_draft["p3"],
            assignment_mode=self.theme_mode,
        )
        try:
            self.theme_selection = menu_themes.save_selection(selection)
            self.theme_dirty = False
            self.show_status(
                "Cores aplicadas. O co-op ativo atualiza sem reiniciar.", "ok"
            )
            return True
        except OSError as error:
            self.show_status(f"Não foi possível salvar as cores: {error}", "error")
            self.audio.play("error")
            return False

    def theme_color(self, name: str) -> tuple[int, int, int]:
        return menu_themes.get_theme(name).accent

    def draw_theme_card(
        self, profile: menu_themes.CharacterThemeProfile, rect: pygame.Rect
    ) -> None:
        value = self.character_theme_draft[profile.member_id]
        selected = self.register(
            f"colors:character:{profile.member_id}",
            rect,
            lambda member=profile.member_id: self.open_theme_picker(member),
        )
        self.draw_panel(
            self.canvas,
            rect,
            selected=selected,
            strong=True,
            accent=self.theme_color(value),
        )
        self.draw_portrait(
            profile.member_id,
            pygame.Rect(rect.x + 7, rect.y + 9, 69, rect.height - 17),
            glow=self.theme_color(value),
        )
        self.draw_text(
            self.canvas,
            self.fonts.card_name,
            profile.name,
            (rect.x + 81, rect.y + 18),
            WHITE,
        )
        swatch = pygame.Rect(rect.x + 82, rect.y + 55, rect.width - 98, 26)
        self.draw_aa_box(
            swatch,
            menu_themes.get_theme(value).panel_dark,
            self.theme_color(value),
            width=2,
            radius=2,
        )
        self.draw_aa_line(
            self.canvas,
            menu_themes.get_theme(value).highlight,
            (swatch.x + 2, swatch.y + 3),
            (swatch.right - 3, swatch.y + 3),
            2,
        )
        self.draw_text(
            self.canvas,
            self.fonts.label,
            self.fit_text(
                self.fonts.label, THEME_LABELS.get(value, value), rect.width - 104
            ),
            (rect.x + 82, rect.bottom - 34),
            GOLD_LIGHT if selected else TEXT,
        )
        self.draw_text(
            self.canvas,
            self.fonts.card_name,
            "›",
            (rect.right - 11, rect.bottom - 33),
            GOLD_LIGHT,
            anchor="topright",
        )

    def draw_player_theme_card(self, player: str, rect: pygame.Rect) -> None:
        value = self.player_theme_draft[player]
        selected = self.register(
            f"colors:player:{player}",
            rect,
            lambda role=player: self.open_theme_picker(role),
        )
        color = self.theme_color(value)
        self.draw_panel(self.canvas, rect, selected=selected, strong=True, accent=color)
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            f"JOGADOR {int(player[1])}",
            (rect.centerx, rect.y + 17),
            GOLD_LIGHT,
            anchor="midtop",
        )
        self.draw_portrait(
            self.role_member(player),
            pygame.Rect(rect.centerx - 48, rect.y + 42, 96, 132),
            glow=color,
        )
        self.draw_text(
            self.canvas,
            self.fonts.card_name,
            self.role_name(player),
            (rect.centerx, rect.y + 181),
            WHITE,
            anchor="midtop",
        )
        swatch = pygame.Rect(rect.x + 24, rect.bottom - 64, rect.width - 48, 27)
        self.draw_aa_box(
            swatch,
            menu_themes.get_theme(value).panel_dark,
            color,
            width=2,
            radius=2,
        )
        theme_label = self.localize(THEME_LABELS.get(value, value))
        self.draw_text(
            self.canvas,
            self.fonts.label,
            theme_label,
            (rect.centerx, rect.bottom - 19),
            GOLD_LIGHT if selected else TEXT,
            anchor="midbottom",
        )

    def theme_picker_title(self, target: str | int) -> str:
        if isinstance(target, str):
            return f"JOGADOR {int(target[1])} · {self.role_name(target)}"
        profile = next(
            (
                profile
                for profile in menu_themes.PLAYABLE_CHARACTER_PROFILES
                if profile.member_id == target
            ),
            None,
        )
        return profile.name if profile is not None else str(target)

    def draw_theme_picker(self) -> None:
        target = self.theme_picker_target
        if target is None:
            return

        self.items = []
        self.text_commands.clear()
        self.canvas.blit(self.capture_shade, (0, 0))
        current = self.theme_value(target)
        modal = pygame.Rect(354, 102, 732, 606)
        self.draw_panel(
            self.canvas,
            modal,
            strong=True,
            accent=self.theme_color(current),
        )
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            "ESCOLHA UMA COR",
            (modal.x + 30, modal.y + 25),
            GOLD_LIGHT,
        )
        self.draw_text(
            self.canvas,
            self.fonts.title,
            self.theme_picker_title(target),
            (modal.x + 28, modal.y + 47),
            WHITE,
        )

        for index, value in enumerate(self.theme_order):
            column = index % 2
            row = index // 2
            rect = pygame.Rect(
                modal.x + 28 + column * 345,
                modal.y + 102 + row * 76,
                330,
                62,
            )
            selected = self.register(
                f"colors:theme:{value}",
                rect,
                lambda theme=value: self.select_theme(theme),
            )
            color = self.theme_color(value)
            active = value == current
            self.draw_panel(
                self.canvas,
                rect,
                selected=selected or active,
                strong=active,
                accent=color,
            )
            swatch = pygame.Rect(rect.x + 13, rect.y + 12, 38, 38)
            theme = menu_themes.get_theme(value)
            self.draw_aa_box(
                swatch,
                theme.panel_dark,
                color,
                width=2,
                radius=4,
            )
            self.draw_text(
                self.canvas,
                self.fonts.body,
                THEME_LABELS.get(value, value),
                (rect.x + 66, rect.centery),
                GOLD_LIGHT if selected or active else TEXT,
                anchor="midleft",
            )
            if active:
                self.draw_text(
                    self.canvas,
                    self.fonts.micro,
                    "ATUAL",
                    (rect.right - 16, rect.centery),
                    color,
                    anchor="midright",
                )

        cancel = pygame.Rect(modal.x + 28, modal.bottom - 66, modal.width - 56, 39)
        selected = self.register("colors:theme:cancel", cancel, self.close_theme_picker)
        self.draw_panel(self.canvas, cancel, selected=selected)
        self.draw_text(
            self.canvas,
            self.fonts.label,
            "CANCELAR",
            cancel.center,
            GOLD_LIGHT if selected else MUTED,
            anchor="center",
        )

    def draw_colors(self) -> None:
        self.draw_header("Cores da party", "IDENTIDADE VISUAL NO JOGO")
        modes = (("character", "POR PERSONAGEM"), ("player", "POR JOGADOR"))
        for index, (mode, label) in enumerate(modes):
            rect = pygame.Rect(CONTENT_X + index * 215, 172, 202, 42)
            selected = self.register(
                f"colors:mode:{mode}",
                rect,
                lambda value=mode: self.set_theme_mode(value),
            )
            active = self.theme_mode == mode
            self.draw_panel(
                self.canvas, rect, selected=selected or active, strong=active
            )
            self.draw_text(
                self.canvas,
                self.fonts.label,
                label,
                rect.center,
                GOLD_LIGHT if active else (WHITE if selected else MUTED),
                anchor="center",
            )
        explanation = (
            "A cor pertence ao personagem, mesmo quando ele muda de jogador."
            if self.theme_mode == "character"
            else "A cor pertence a P1, P2 ou P3 e acompanha o jogador nas trocas."
        )
        self.draw_text(
            self.canvas, self.fonts.body_small, explanation, (CONTENT_X, 226), MUTED
        )

        if self.theme_mode == "character":
            groups = (("main", "PRINCIPAIS"), ("guests", "CONVIDADOS"))
            for index, (group, label) in enumerate(groups):
                rect = pygame.Rect(CONTENT_X + index * 135, 254, 124, 31)
                selected = self.register(
                    f"colors:group:{group}",
                    rect,
                    lambda value=group: self.set_theme_group(value),
                )
                active = self.theme_group == group
                self.draw_badge(
                    rect, label, active=active, selected=selected, color=GOLD_LIGHT
                )
            profiles = (
                menu_themes.MAIN_CHARACTER_PROFILES
                if self.theme_group == "main"
                else menu_themes.GUEST_CHARACTER_PROFILES
            )
            if self.theme_group == "main":
                for index, profile in enumerate(profiles):
                    rect = pygame.Rect(
                        CONTENT_X + (index % 3) * 223,
                        296 + (index // 3) * 129,
                        204,
                        118,
                    )
                    self.draw_theme_card(profile, rect)
            else:
                for index, profile in enumerate(profiles):
                    rect = pygame.Rect(
                        CONTENT_X + (index % 2) * 334,
                        296 + (index // 2) * 129,
                        316,
                        118,
                    )
                    self.draw_theme_card(profile, rect)
        else:
            for index, player in enumerate(("p1", "p2", "p3")):
                rect = pygame.Rect(CONTENT_X + index * 223, 266, 204, 278)
                self.draw_player_theme_card(player, rect)

        applied = pygame.Rect(CONTENT_X, 568, CONTENT_W, 58)
        self.draw_aa_line(
            self.canvas,
            GREEN,
            (applied.x + 2, applied.centery),
            (applied.x + 28, applied.centery),
            2,
        )
        self.draw_text(
            self.canvas,
            self.fonts.label,
            "CORES APLICADAS AUTOMATICAMENTE",
            (applied.x + 42, applied.centery),
            GREEN,
            anchor="midleft",
            shadow=False,
        )

        application = pygame.Rect(CONTENT_X, 644, CONTENT_W, 89)
        self.draw_panel(self.canvas, application, strong=True)
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            "A PALETA ACOMPANHA TODA A INTERFACE DO JOGADOR",
            (application.x + 18, application.y + 13),
            GOLD_LIGHT,
        )
        for index, label in enumerate(
            ("BATTLE MENU", "HUD", "LINHA DE ALVO", "TARGET INFO")
        ):
            chip = pygame.Rect(
                application.x + 17 + index * 154,
                application.y + 39,
                146,
                34,
            )
            self.draw_aa_box(
                chip,
                (7, 25, 41, 222),
                (49, 109, 145),
                radius=3,
            )
            self.draw_aa_line(
                self.canvas,
                BLUE_LIGHT if index % 2 == 0 else GOLD_LIGHT,
                (chip.x + 9, chip.y + 5),
                (chip.x + 30, chip.y + 5),
            )
            self.draw_text(
                self.canvas,
                self.fonts.micro,
                label,
                chip.center,
                TEXT,
                anchor="center",
                shadow=False,
            )

    def set_control_player(self, player: str) -> None:
        self.control_player = player
        self.selected_key = f"controls:tab:{player}"
        self.reset_pointer()
        self.audio.play("move")

    def assignment_label(self, player: str) -> str:
        assignment = self.editor.assigned(player)
        if assignment is None:
            return "NENHUM CONTROLE"
        unified = len(
            controls.device_identity_keys(self.editor.config, assignment)
        ) > 1
        backend = "XINPUT + SDL" if unified else None
        if isinstance(assignment, int):
            effective = controls.effective_gamepad_assignment(
                self.editor.config,
                player,
                self.states,
                getattr(self, "sdl_states", ()),
            )
            display_slot = effective if isinstance(effective, int) else assignment
            source = backend or f"XINPUT {assignment + 1}"
            return f"{self.controller_name(display_slot)}  ·  {source}"
        source = backend or "DIRECTINPUT / SDL"
        return f"{assignment['name']}  ·  {source}"

    def begin_assignment(self) -> None:
        if not self.control_config_editable():
            return
        self.editor.begin_assignment(
            self.control_player,
            self.states,
            self.sdl_states,
        )
        self.reset_pointer()
        self.show_status(
            f"Pressione START no controle de {self.control_player.upper()}.",
            "capture",
            30.0,
        )
        self.audio.play("open")

    def clear_controller_assignment(self) -> None:
        if not self.control_config_editable():
            return
        self.editor.assign_controller(self.control_player, None)
        self.persist_controls(
            f"{self.control_player.upper()} agora está sem controle. Aplicado ao co-op.",
            sound="cancel",
        )

    def assign_desktop_to_current(self) -> None:
        if not self.control_config_editable():
            return
        player = self.control_player
        previous = self.editor.desktop_owner()
        if previous == player:
            self.show_status(
                f"Teclado e mouse já pertencem a {player.upper()}.",
                "ok",
            )
            self.audio.play("confirm")
            return
        self.editor.assign_desktop(player)
        if previous is None:
            message = f"Teclado e mouse atribuídos a {player.upper()}. Aplicado ao co-op."
        else:
            message = (
                f"Teclado e mouse transferidos de {previous.upper()} para "
                f"{player.upper()}. Aplicado ao co-op."
            )
        self.persist_controls(message)

    def clear_desktop_assignment(self) -> None:
        if not self.control_config_editable():
            return
        player = self.control_player
        if not self.editor.clear_desktop(player):
            owner = self.editor.desktop_owner()
            message = (
                f"Teclado e mouse pertencem a {owner.upper()}."
                if owner is not None
                else "Teclado e mouse já estão sem jogador."
            )
            self.show_status(message, "warning")
            self.audio.play("error")
            return
        self.persist_controls(
            "Teclado e mouse ficaram sem jogador. Aplicado ao co-op.",
            sound="cancel",
        )

    @staticmethod
    def control_normalization_message(
        report: controls.ConfigNormalizationReport,
    ) -> str:
        owner = (
            report.kept_desktop_owner.upper()
            if report.kept_desktop_owner is not None
            else "nenhum jogador"
        )
        if report.removed_desktop_owners:
            removed = ", ".join(
                player.upper() for player in report.removed_desktop_owners
            )
            return (
                "Teclado e mouse estavam atribuídos a mais de um jogador. "
                f"Mantido em {owner}; removido de {removed}. Correção salva."
            )
        return (
            "Configuração de controles atualizada: teclado e mouse agora "
            f"pertencem a {owner}. Migração salva."
        )

    @staticmethod
    def control_future_version_message(
        report: controls.ConfigNormalizationReport,
    ) -> str:
        return (
            "O arquivo de controles usa a versão "
            f"{report.source_version}, mas este configurador suporta até a versão "
            f"{controls.CONFIG_VERSION}. O arquivo foi preservado sem alterações; "
            "atualize o mod para editá-lo."
        )

    def control_config_editable(self) -> bool:
        if getattr(self.editor, "read_only", False) is not True:
            return True
        self.show_status(
            self.control_future_version_message(self.editor.normalization_report),
            "warning",
            12.0,
        )
        self.audio.play("error")
        return False

    def persist_control_normalization(
        self,
        report: controls.ConfigNormalizationReport,
    ) -> bool:
        if report.incompatible_future:
            return self.control_config_editable()
        return self.persist_controls(self.control_normalization_message(report))

    def begin_binding(self, action: str, slot: int) -> None:
        if not self.control_config_editable():
            return
        self.editor.begin_capture(
            self.control_player,
            action,
            slot,
            self.states,
            self.sdl_states,
        )
        self.reset_pointer()
        self.show_status(
            "Pressione uma tecla ou botão. Botão direito cancela.",
            "capture",
            30.0,
        )
        self.audio.play("open")

    def clear_binding(self, action: str, slot: int) -> None:
        if not self.control_config_editable():
            return
        if self.editor.clear_binding(self.control_player, action, slot):
            self.persist_controls("Atalho removido e aplicado ao co-op.", sound="cancel")

    def persist_controls(self, message: str, *, sound: str = "confirm") -> bool:
        try:
            self.editor.save()
        except controls.UnsupportedControllerConfigVersion:
            self.editor.reload()
            self.control_config_editable()
            return False
        except OSError as error:
            self.editor.reload()
            self.show_status(f"Não foi possível salvar: {error}", "error")
            self.audio.play("error")
            return False
        self.show_status(message, "ok")
        self.audio.play(sound)
        return True

    def save_controls(self) -> bool:
        return self.persist_controls(
            "Controles aplicados. O co-op ativo atualiza sem reiniciar."
        )

    def persist_control_capture(
        self,
        result: core.CaptureResult,
    ) -> core.CaptureResult:
        if result.kind not in ("assigned", "bound") or not self.editor.dirty:
            return result
        try:
            self.editor.save()
        except controls.UnsupportedControllerConfigVersion:
            self.editor.reload()
            return core.CaptureResult(
                "invalid",
                self.control_future_version_message(self.editor.normalization_report),
            )
        except OSError as error:
            self.editor.reload()
            return core.CaptureResult(
                "invalid",
                f"A alteração não pôde ser aplicada: {error}",
            )
        return core.CaptureResult(result.kind, result.message + " Aplicado ao co-op.")

    def restore_controls(self) -> None:
        if not self.control_config_editable():
            return
        now = time.monotonic()
        if now > self.default_confirm_until:
            self.default_confirm_until = now + 3.5
            self.show_status(
                "Pressione Restaurar novamente para confirmar.", "warning", 3.5
            )
            self.audio.play("error")
            return
        self.editor.restore_defaults()
        self.default_confirm_until = 0.0
        self.persist_controls("Padrões restaurados e aplicados ao co-op.")

    def draw_control_tab(self, player: str, index: int) -> None:
        rect = pygame.Rect(CONTENT_X + index * 246, 170, 232, 48)
        selected = self.register(
            f"controls:tab:{player}",
            rect,
            lambda value=player: self.set_control_player(value),
        )
        active = player == self.control_player
        self.draw_panel(self.canvas, rect, selected=selected or active, strong=active)
        self.draw_portrait(
            self.role_member(player),
            pygame.Rect(rect.x + 8, rect.y + 3, 40, 43),
            glow=BLUE_LIGHT if player == "p1" else GOLD_LIGHT,
        )
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            f"JOGADOR {index + 1}",
            (rect.x + 56, rect.y + 8),
            GOLD_LIGHT if active else MUTED,
        )
        self.draw_text(
            self.canvas,
            self.fonts.label,
            self.fit_text(self.fonts.label, self.role_name(player), 155),
            (rect.x + 56, rect.y + 25),
            WHITE if active else TEXT,
        )

    def draw_binding_card(self, action: str, rect: pygame.Rect) -> None:
        values = self.editor.bindings(self.control_player, action)
        assigned = self.editor.assigned(self.control_player)
        owns_desktop = self.editor.owns_desktop(self.control_player)
        selected_any = self.selected_key.startswith(f"controls:bind:{action}:")
        self.draw_panel(self.canvas, rect, selected=selected_any)
        label = self.fit_text(
            self.fonts.body_small, ACTION_LABELS.get(action, action), 88
        )
        self.draw_text(
            self.canvas,
            self.fonts.body_small,
            label,
            (rect.x + 11, rect.centery),
            TEXT,
            anchor="midleft",
            shadow=False,
        )
        for slot in range(controls.MAX_BINDINGS_PER_ACTION):
            cell = pygame.Rect(
                rect.x + 104 + slot * 60, rect.y + 8, 56, rect.height - 16
            )
            selected = self.register(
                f"controls:bind:{action}:{slot}",
                cell,
                lambda value=action, number=slot: self.begin_binding(value, number),
                clear=lambda value=action, number=slot: self.clear_binding(
                    value, number
                ),
            )
            binding = values[slot] if slot < len(values) else None
            inactive_keyboard = bool(
                binding
                and binding.get("device") == "keyboard"
                and not owns_desktop
            )
            self.draw_aa_box(
                cell,
                (30, 61, 86)
                if selected
                else ((8, 20, 31) if inactive_keyboard else (11, 26, 42)),
                GOLD_LIGHT
                if selected
                else ((42, 57, 68) if inactive_keyboard else (67, 98, 122)),
                width=2 if selected else 1,
                radius=4,
            )
            self.draw_text(
                self.canvas,
                self.fonts.binding,
                self.fit_text(
                    self.fonts.binding,
                    core.compact_binding_label(binding, assigned),
                    cell.width - 6,
                ),
                cell.center,
                GOLD_LIGHT
                if selected
                else (MUTED if inactive_keyboard or not binding else WHITE),
                anchor="center",
                shadow=False,
            )

    def draw_controls(self) -> None:
        width = 728
        self.draw_header(
            "Controles de cada jogador", "MAPEAMENTO INTEGRADO", width=width
        )
        for index, player in enumerate(("p1", "p2", "p3")):
            self.draw_control_tab(player, index)

        assigned = self.editor.assigned(self.control_player)
        device = pygame.Rect(CONTENT_X, 230, 356, 64)
        assign_button = pygame.Rect(device.right - 144, device.y + 15, 64, 34)
        selected = self.register(
            "controls:assign",
            assign_button,
            self.begin_assignment,
        )
        no_controller = pygame.Rect(device.right - 72, device.y + 15, 62, 34)
        no_controller_selected = self.register(
            "controls:unassign",
            no_controller,
            self.clear_controller_assignment,
        )
        self.draw_panel(
            self.canvas,
            device,
            selected=selected or no_controller_selected,
            strong=True,
        )
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            f"1 · CONTROLE DO {self.control_player.upper()}",
            (device.x + 17, device.y + 11),
            MUTED,
        )
        label = self.assignment_label(self.control_player)
        self.draw_text(
            self.canvas,
            self.fonts.label,
            self.fit_text(self.fonts.body_small, label, 190),
            (device.x + 17, device.y + 38),
            WHITE,
            anchor="midleft",
            shadow=False,
        )
        self.draw_badge(
            assign_button,
            "USAR" if assigned is None else "TROCAR",
            selected=selected,
            color=BLUE_LIGHT,
        )
        self.draw_badge(
            no_controller,
            "NENHUM" if assigned is None else "REMOVER",
            active=assigned is None,
            selected=no_controller_selected,
            color=GREEN if assigned is None else GOLD_LIGHT,
        )
        if assigned is not None:
            connected = controls.gamepad_assignment_connected(
                self.editor.config,
                self.control_player,
                self.states,
                getattr(self, "sdl_states", ()),
            )
            dot = GREEN if connected else RED
            self.draw_aa_circle(
                self.canvas, dot, (assign_button.x - 10, device.centery), 4
            )

        desktop_owner = self.editor.desktop_owner()
        owns_desktop = desktop_owner == self.control_player
        desktop = pygame.Rect(CONTENT_X + 372, 230, 356, 64)
        use_desktop = pygame.Rect(desktop.right - 144, desktop.y + 15, 64, 34)
        use_desktop_selected = self.register(
            "controls:desktop",
            use_desktop,
            self.assign_desktop_to_current,
        )
        clear_desktop = pygame.Rect(desktop.right - 72, desktop.y + 15, 62, 34)
        clear_desktop_selected = False
        if owns_desktop:
            clear_desktop_selected = self.register(
                "controls:desktop_clear",
                clear_desktop,
                self.clear_desktop_assignment,
            )
        self.draw_panel(
            self.canvas,
            desktop,
            selected=use_desktop_selected or clear_desktop_selected,
            strong=True,
        )
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            "2 · TECLADO + MOUSE",
            (desktop.x + 13, desktop.y + 8),
            MUTED,
        )
        desktop_label = (
            f"DONO ÚNICO: {desktop_owner.upper()}"
            if desktop_owner is not None
            else "SEM DONO"
        )
        self.draw_text(
            self.canvas,
            self.fonts.label,
            desktop_label,
            (desktop.x + 13, desktop.y + 30),
            WHITE if owns_desktop else TEXT,
            anchor="midleft",
            shadow=False,
        )
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            "MOVER TRANSFERE AMBOS",
            (desktop.x + 13, desktop.y + 49),
            MUTED,
            shadow=False,
        )
        self.draw_badge(
            use_desktop,
            "EM USO" if owns_desktop else ("USAR" if desktop_owner is None else "MOVER"),
            active=owns_desktop,
            selected=use_desktop_selected,
            color=GREEN if owns_desktop else BLUE_LIGHT,
        )
        self.draw_badge(
            clear_desktop,
            "REMOVER" if owns_desktop else ("NENHUM" if desktop_owner is None else "—"),
            active=desktop_owner is None,
            selected=clear_desktop_selected,
            color=(
                GOLD_LIGHT
                if owns_desktop
                else (GREEN if desktop_owner is None else MUTED)
            ),
        )

        actions = controls.PLAYER_ACTIONS[self.control_player]
        half = (len(actions) + 1) // 2
        very_dense = half > 9
        dense = half > 8
        row_height = 34 if very_dense else (39 if dense else 42)
        row_stride = 37 if very_dense else (42 if dense else 47)
        column_x = (CONTENT_X, CONTENT_X + 372)
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            f"3 · AÇÕES E ATALHOS · {controls.MAX_BINDINGS_PER_ACTION} SLOTS",
            (CONTENT_X + 10, 306),
            MUTED,
        )
        for column in range(2):
            base = column_x[column] + 104
            for slot in range(controls.MAX_BINDINGS_PER_ACTION):
                self.draw_text(
                    self.canvas,
                    self.fonts.micro,
                    str(slot + 1),
                    (base + slot * 60 + 28, 306),
                    MUTED,
                    anchor="midtop",
                )
        for index, (action, _english) in enumerate(actions):
            column = 0 if index < half else 1
            row = index if column == 0 else index - half
            rect = pygame.Rect(
                column_x[column],
                323 + row * row_stride,
                356,
                row_height,
            )
            self.draw_binding_card(action, rect)

        if self.control_player == "p1":
            guide = pygame.Rect(CONTENT_X, 521, width, 162)
            self.draw_panel(self.canvas, guide, strong=True)
            self.draw_text(
                self.canvas,
                self.fonts.micro,
                "P1 · CAMINHOS DE ENTRADA TRADUZIDOS PARA O JOGO",
                (guide.x + 18, guide.y + 15),
                GOLD_LIGHT,
            )
            routes = (
                ("NAVEGAÇÃO", "Direcional e ombros", "Menu de batalha"),
                ("AÇÕES", "Confirmar e cancelar", "Interações e portas"),
                ("MENU NATIVO", "Party e Licenças", "Atalho dedicado"),
            )
            for index, (label, detail, destination) in enumerate(routes):
                card = pygame.Rect(
                    guide.x + 17 + index * 232,
                    guide.y + 43,
                    218,
                    91,
                )
                self.draw_aa_box(
                    card,
                    (6, 23, 38, 218),
                    (47, 99, 132),
                    radius=4,
                )
                self.draw_text(
                    self.canvas,
                    self.fonts.micro,
                    f"0{index + 1}  {self.localize(label)}",
                    (card.x + 13, card.y + 12),
                    BLUE_LIGHT,
                    shadow=False,
                )
                self.draw_text(
                    self.canvas,
                    self.fonts.body_small,
                    detail,
                    (card.x + 13, card.y + 35),
                    WHITE,
                    shadow=False,
                )
                self.draw_text(
                    self.canvas,
                    self.fonts.micro,
                    destination,
                    (card.x + 13, card.y + 63),
                    MUTED,
                    shadow=False,
                )

        autosave = pygame.Rect(CONTENT_X, 704, 448, 46)
        self.draw_aa_line(
            self.canvas,
            GREEN,
            (autosave.x + 2, autosave.centery),
            (autosave.x + 28, autosave.centery),
            2,
        )
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            "ALTERAÇÕES APLICADAS AO TERMINAR CADA CAPTURA",
            (autosave.x + 42, autosave.centery),
            GREEN,
            anchor="midleft",
            shadow=False,
        )
        defaults = pygame.Rect(CONTENT_X + 464, 704, 264, 46)
        selected = self.register("controls:defaults", defaults, self.restore_controls)
        self.draw_panel(self.canvas, defaults, selected=selected, strong=True)
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            "RESTAURAR PADRÕES",
            defaults.center,
            GOLD_LIGHT if selected else TEXT,
            anchor="center",
        )

    def toggle_setting(self, key: str) -> None:
        self.config[key] = not bool(self.config.get(key, False))
        self.config = core.save_launcher_config(self.config)
        self.audio.config = self.config
        self.audio.play("confirm")
        self.show_status("Ajuste salvo.", "ok")

    def toggle_battle_menu_sound(self, player: str) -> None:
        settings = dict(self.config.get("battle_menu_sounds", {}))
        settings[player] = not bool(settings.get(player, True))
        self.config["battle_menu_sounds"] = settings
        self.config = core.save_launcher_config(self.config)
        self.audio.config = self.config
        self.audio.play("confirm")
        self.show_status("Som do Battle Menu salvo.", "ok")

    def set_damage_meter_mode(self, mode: str) -> None:
        if mode not in ("area", "indefinite"):
            return
        self.config["damage_meter_mode"] = mode
        self.config = core.save_launcher_config(self.config)
        self.audio.config = self.config
        self.audio.play("confirm")
        self.show_status("Modo do contador de dano salvo.", "ok")

    def reset_damage_meter_session(self) -> None:
        now = time.monotonic()
        if now > self.damage_reset_confirm_until:
            self.damage_reset_confirm_until = now + 3.5
            self.show_status(
                "Pressione Zerar novamente para iniciar uma nova sessão de dano.",
                "warning",
                3.5,
            )
            self.audio.play("open")
            return
        updated = dict(self.config)
        current = int(updated.get("damage_meter_session_id", 1))
        updated["damage_meter_session_id"] = 1 if current >= 0x7FFFFFFF else current + 1
        try:
            self.config = core.save_launcher_config(updated)
        except OSError as error:
            self.show_status(
                f"Não foi possível zerar a sessão de dano: {error}",
                "error",
            )
            self.audio.play("error")
            return
        self.damage_reset_confirm_until = 0.0
        self.audio.config = self.config
        self.audio.play("confirm")
        self.show_status(
            "Nova sessão de dano iniciada. O jogo ativo atualiza sem reiniciar.",
            "ok",
        )

    def adjust_volume(self, direction: int) -> None:
        current = float(self.config.get("sound_volume", 1.0))
        self.config["sound_volume"] = min(
            1.0, max(0.0, round(current + (0.1 if direction >= 0 else -0.1), 2))
        )
        self.config = core.save_launcher_config(self.config)
        self.audio.config = self.config
        self.audio.set_volume(float(self.config["sound_volume"]))
        self.audio.play("move")

    def set_language(self, language: str) -> None:
        if language not in ("en", "pt") or language == self.language:
            return
        self.config["language"] = language
        self.config = core.save_launcher_config(self.config)
        self.audio.config = self.config
        self.update_tray_labels()
        self.selected_key = f"settings:language:{language}"
        self.reset_pointer()
        self.audio.play("confirm")
        self.show_status("Idioma alterado e salvo.", "ok")

    def cycle_language(self, direction: int) -> None:
        order = ("en", "pt")
        index = order.index(self.language)
        self.set_language(order[(index + (1 if direction >= 0 else -1)) % 2])

    def draw_language_row(self, y: int) -> None:
        rect = pygame.Rect(CONTENT_X, y, CONTENT_W, 78)
        self.draw_panel(self.canvas, rect, strong=True)
        self.draw_text(
            self.canvas,
            self.fonts.label,
            "Idioma da ferramenta",
            (rect.x + 20, rect.y + 16),
            WHITE,
        )
        self.draw_text(
            self.canvas,
            self.fonts.body_small,
            "Escolha o idioma usado em toda a interface.",
            (rect.x + 20, rect.y + 44),
            MUTED,
        )
        options = (("en", "ENGLISH"), ("pt", "PORTUGUÊS"))
        for index, (language, label) in enumerate(options):
            badge = pygame.Rect(rect.right - 228 + index * 142, rect.y + 22, 84, 34)
            selected = self.register(
                f"settings:language:{language}",
                badge,
                lambda value=language: self.set_language(value),
                self.cycle_language,
            )
            self.draw_badge(
                badge,
                label,
                active=self.language == language,
                selected=selected,
                color=GOLD_LIGHT,
            )

    def draw_switch_row(
        self, key: str, y: int, title: str, detail: str, value: bool, setting_key: str
    ) -> None:
        rect = pygame.Rect(CONTENT_X, y, CONTENT_W, 78)
        selected = self.register(
            f"settings:{key}",
            rect,
            lambda value=setting_key: self.toggle_setting(value),
        )
        self.draw_panel(self.canvas, rect, selected=selected, strong=True)
        self.draw_text(
            self.canvas, self.fonts.label, title, (rect.x + 20, rect.y + 17), WHITE
        )
        self.draw_text(
            self.canvas,
            self.fonts.body_small,
            detail,
            (rect.x + 20, rect.y + 44),
            MUTED,
        )
        badge = pygame.Rect(rect.right - 102, rect.y + 23, 76, 32)
        self.draw_badge(
            badge,
            "ATIVO" if value else "DESL.",
            active=value,
            color=GREEN if value else MUTED,
        )

    def draw_damage_meter_row(self, y: int) -> None:
        rect = pygame.Rect(CONTENT_X, y, CONTENT_W, 78)
        enabled = bool(self.config.get("damage_meter_enabled", False))
        mode = str(self.config.get("damage_meter_mode", "area"))
        effective = bool(self.config.get("damage_meter_effective", False))
        enabled_badge = pygame.Rect(rect.right - 438, rect.y + 22, 66, 34)
        area_badge = pygame.Rect(rect.right - 364, rect.y + 22, 62, 34)
        session_badge = pygame.Rect(rect.right - 294, rect.y + 22, 76, 34)
        effective_badge = pygame.Rect(rect.right - 210, rect.y + 22, 84, 34)
        reset_badge = pygame.Rect(rect.right - 118, rect.y + 22, 100, 34)
        enabled_selected = self.register(
            "settings:damage_meter:enabled",
            enabled_badge,
            lambda: self.toggle_setting("damage_meter_enabled"),
        )
        area_selected = self.register(
            "settings:damage_meter:area",
            area_badge,
            lambda: self.set_damage_meter_mode("area"),
        )
        session_selected = self.register(
            "settings:damage_meter:indefinite",
            session_badge,
            lambda: self.set_damage_meter_mode("indefinite"),
        )
        effective_selected = self.register(
            "settings:damage_meter:effective",
            effective_badge,
            lambda: self.toggle_setting("damage_meter_effective"),
        )
        reset_selected = self.register(
            "settings:damage_meter:reset",
            reset_badge,
            self.reset_damage_meter_session,
        )
        self.draw_panel(
            self.canvas,
            rect,
            selected=(
                enabled_selected
                or area_selected
                or session_selected
                or effective_selected
                or reset_selected
            ),
            strong=True,
        )
        self.draw_text(
            self.canvas,
            self.fonts.label,
            "Contador de dano",
            (rect.x + 20, rect.y + 16),
            WHITE,
        )
        self.draw_text(
            self.canvas,
            self.fonts.body_small,
            "Por personagem até Zerar.",
            (rect.x + 20, rect.y + 44),
            MUTED,
        )
        self.draw_badge(
            enabled_badge,
            "ATIVO" if enabled else "DESL.",
            active=enabled,
            selected=enabled_selected,
            color=GREEN if enabled else MUTED,
        )
        self.draw_badge(
            area_badge,
            "\u00c1REA",
            active=mode == "area",
            selected=area_selected,
            color=GOLD_LIGHT,
        )
        self.draw_badge(
            session_badge,
            "SESS\u00c3O",
            active=mode == "indefinite",
            selected=session_selected,
            color=GOLD_LIGHT,
        )
        self.draw_badge(
            effective_badge,
            "EFETIVO" if effective else "BRUTO",
            active=effective,
            selected=effective_selected,
            color=GREEN if effective else MUTED,
        )
        reset_pending = time.monotonic() <= self.damage_reset_confirm_until
        self.draw_badge(
            reset_badge,
            "CONFIRMAR" if reset_pending else "ZERAR",
            active=reset_pending,
            selected=reset_selected,
            color=RED if reset_pending else GOLD_LIGHT,
        )

    def draw_settings(self) -> None:
        self.draw_header("Ajustes do configurador", "COMPORTAMENTO E SOM")
        self.draw_language_row(154)
        self.draw_switch_row(
            "sounds",
            238,
            "Sons do menu",
            "Reproduz sons ao navegar ou confirmar opções do configurador.",
            bool(self.config.get("menu_sounds", True)),
            "menu_sounds",
        )

        volume = float(self.config.get("sound_volume", 1.0))
        rect = pygame.Rect(CONTENT_X, 322, CONTENT_W, 78)
        volume_down = pygame.Rect(rect.right - 184, rect.y + 22, 42, 38)
        volume_up = pygame.Rect(rect.right - 52, rect.y + 22, 42, 38)
        down_selected = self.register(
            "settings:volume:down",
            volume_down,
            lambda: self.adjust_volume(-1),
        )
        up_selected = self.register(
            "settings:volume:up",
            volume_up,
            lambda: self.adjust_volume(1),
        )
        self.draw_panel(
            self.canvas,
            rect,
            selected=down_selected or up_selected,
            strong=True,
        )
        self.draw_text(
            self.canvas,
            self.fonts.label,
            "Volume da interface",
            (rect.x + 20, rect.y + 16),
            WHITE,
        )
        bar = pygame.Rect(rect.x + 20, rect.y + 51, 430, 6)
        pygame.draw.rect(self.canvas, (34, 48, 61), bar)
        fill = bar.copy()
        fill.width = round(bar.width * volume)
        pygame.draw.rect(self.canvas, GOLD_LIGHT, fill)
        self.draw_badge(
            volume_down,
            "−",
            selected=down_selected,
            color=GOLD_LIGHT,
        )
        self.draw_text(
            self.canvas,
            self.fonts.label,
            f"{round(volume * 100)}%",
            (rect.right - 96, rect.centery),
            WHITE,
            anchor="center",
        )
        self.draw_badge(
            volume_up,
            "+",
            selected=up_selected,
            color=GOLD_LIGHT,
        )

        battle_rect = pygame.Rect(CONTENT_X, 406, CONTENT_W, 78)
        self.draw_panel(self.canvas, battle_rect, strong=True)
        self.draw_text(
            self.canvas,
            self.fonts.label,
            "Som do Battle Menu",
            (battle_rect.x + 20, battle_rect.y + 16),
            WHITE,
        )
        self.draw_text(
            self.canvas,
            self.fonts.body_small,
            "Ative ou silencie o menu de cada jogador.",
            (battle_rect.x + 20, battle_rect.y + 46),
            MUTED,
        )
        battle_sounds = self.config.get("battle_menu_sounds", {})
        for index, player in enumerate(("p1", "p2", "p3")):
            enabled = bool(battle_sounds.get(player, True))
            badge = pygame.Rect(
                battle_rect.right - 246 + index * 76,
                battle_rect.y + 22,
                66,
                34,
            )
            selected = self.register(
                f"settings:battle_sound:{player}",
                badge,
                lambda value=player: self.toggle_battle_menu_sound(value),
            )
            self.draw_badge(
                badge,
                player.upper(),
                active=enabled,
                selected=selected,
                color=GREEN if enabled else MUTED,
            )

        self.draw_damage_meter_row(490)

        self.draw_switch_row(
            "minimize",
            574,
            "Minimizar quando estiver pronto",
            "O configurador é minimizado quando a sessão fica ativa.",
            bool(self.config.get("minimize_when_ready", True)),
            "minimize_when_ready",
        )

        help_panel = pygame.Rect(CONTENT_X, 658, CONTENT_W, 84)
        self.draw_panel(self.canvas, help_panel, strong=True)
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            "COMO USAR",
            (help_panel.x + 20, help_panel.y + 17),
            GOLD_LIGHT,
        )
        rows = (
            ("1", "Defina cores e controles; tudo é salvo no próprio mod."),
            ("2", "Abra pela Steam ou pelo FFXII (Modded) do MO2."),
        )
        for index, (number, text) in enumerate(rows):
            y = help_panel.y + 38 + index * 25
            badge = pygame.Rect(help_panel.x + 20, y - 4, 23, 23)
            self.draw_badge(badge, number, active=True, color=BLUE_LIGHT)
            self.draw_text(
                self.canvas, self.fonts.body_small, text, (help_panel.x + 56, y), TEXT
            )

    def draw_background(self, now: float) -> None:
        self.canvas.blit(self.background, (0, 0))
        self.canvas.blit(self.background_veil, (0, 0))
        self.canvas.blit(self.background_wash, (0, 0))
        for x, y, speed, phase in self.motes:
            pulse = 0.5 + 0.5 * math.sin(now * speed * 2.0 + phase * 9.0)
            pygame.draw.circle(
                self.canvas, (83, 150, 191, round(18 + pulse * 35)), (x, y), 1
            )

    def draw_status_bar(self) -> None:
        if time.monotonic() > self.status_until and self.launcher.state not in (
            "preparing",
            "ready",
            "error",
        ):
            message = (
                "MOUSE: selecionar e configurar · BOTÃO DIREITO: cancelar ou limpar"
            )
            color = (102, 127, 149)
        else:
            message = (
                self.launcher.last_message
                if self.launcher.state
                in ("preparing", "ready", "error")
                else self.status_message
            )
            color = {
                "ok": GREEN,
                "error": RED,
                "warning": GOLD_LIGHT,
                "capture": BLUE_LIGHT,
            }.get(
                "error" if self.launcher.state == "error" else self.status_kind, MUTED
            )
        bar = pygame.Rect(CONTENT_X, 758, CONTENT_W, 32)
        self.draw_aa_box(
            bar,
            (5, 18, 30, 220),
            (37, 76, 101),
            radius=4,
        )
        self.draw_aa_circle(self.canvas, color, (bar.x + 16, bar.centery), 3)
        self.draw_text(
            self.canvas,
            self.fonts.body_small,
            self.fit_text(self.fonts.body_small, message, bar.width - 46),
            (bar.x + 29, bar.centery),
            color,
            anchor="midleft",
            shadow=False,
        )

    def draw_pointer(self, now: float) -> pygame.Rect | None:
        selected = self.item()
        if selected is None:
            return None
        target_y = float(selected.rect.centery - self.cursor.get_height() / 2)
        if self.pointer_y is None:
            self.pointer_y = target_y
        delta = target_y - self.pointer_y
        if abs(delta) > 0.2:
            self.pointer_trail.append((self.pointer_y, 1.0))
            self.pointer_y += delta * 0.31
        else:
            self.pointer_y = target_y
        self.pointer_trail = [
            (position, alpha * 0.72)
            for position, alpha in self.pointer_trail[-5:]
            if alpha * 0.72 > 0.13
        ]
        jab_phase = (now % 0.78) / 0.78
        jab = math.sin(math.pi * jab_phase / 0.18) * 4.5 if jab_phase < 0.18 else 0.0
        x = selected.rect.x - 55 + round(jab)
        damage = None
        for index, (position, alpha) in enumerate(self.pointer_trail[-2:]):
            ghost = self.cursor_ghosts[min(index, len(self.cursor_ghosts) - 1)].copy()
            ghost.set_alpha(round(alpha * 105))
            rect = self.canvas.blit(ghost, (x - 4 - index * 4, round(position)))
            damage = rect if damage is None else damage.union(rect)
        rect = self.canvas.blit(self.cursor, (x, round(self.pointer_y)))
        return rect if damage is None else damage.union(rect)

    def draw_capture_overlay(self) -> None:
        if not self.capture_active:
            return

        self.items = []
        self.text_commands.clear()
        self.canvas.blit(self.capture_shade, (0, 0))
        rect = pygame.Rect(340, 224, 760, 330)
        self.draw_panel(self.canvas, rect, selected=True, strong=True)
        assignment = self.editor.assign_target is not None
        player = self.editor.assign_target or (
            self.editor.capture_target[0]
            if self.editor.capture_target
            else self.control_player
        )
        action = self.editor.capture_target[1] if self.editor.capture_target else None
        slot = self.editor.capture_target[2] if self.editor.capture_target else None
        self.draw_portrait(
            self.role_member(player),
            pygame.Rect(rect.x + 38, rect.y + 61, 116, 160),
            glow=GOLD_LIGHT,
        )
        self.draw_text(
            self.canvas,
            self.fonts.micro,
            "DETECTANDO CONTROLE" if assignment else "CAPTURANDO ATALHO",
            (rect.x + 190, rect.y + 45),
            GOLD_LIGHT,
        )
        self.draw_text(
            self.canvas,
            self.fonts.subtitle,
            f"JOGADOR {int(player[1])} · {self.role_name(player)}",
            (rect.x + 188, rect.y + 75),
            WHITE,
        )
        if action is not None:
            self.draw_text(
                self.canvas,
                self.fonts.label,
                f"{ACTION_LABELS.get(action, action)}  ·  ATALHO {int(slot) + 1}",
                (rect.x + 190, rect.y + 116),
                BLUE_LIGHT,
            )
        prompt = (
            "Pressione START apenas no controle físico desejado"
            if assignment
            else "Pressione uma tecla"
        )
        self.draw_text(
            self.canvas, self.fonts.body, prompt, (rect.x + 190, rect.y + 157), TEXT
        )
        self.draw_text(
            self.canvas,
            self.fonts.body_small,
            (
                "XInput e SDL duplicados serão unificados automaticamente."
                if assignment
                else "Solte entradas que já estavam pressionadas antes de tentar."
            ),
            (rect.x + 190, rect.y + 190),
            MUTED,
        )
        pulse = 0.5 + 0.5 * math.sin(time.monotonic() * 5.0)
        self.draw_aa_circle(
            self.canvas,
            (*BLUE_LIGHT, round(100 + pulse * 155)),
            (rect.x + 194, rect.y + 236),
            7,
        )
        self.draw_text(
            self.canvas,
            self.fonts.label,
            "AGUARDANDO ENTRADA…",
            (rect.x + 213, rect.y + 236),
            BLUE_LIGHT,
            anchor="midleft",
        )
        self.draw_aa_line(
            self.canvas,
            (57, 104, 136),
            (rect.x + 36, rect.bottom - 53),
            (rect.right - 36, rect.bottom - 53),
        )
        self.draw_text(
            self.canvas,
            self.fonts.body_small,
            "BOTÃO DIREITO DO MOUSE: CANCELAR",
            (rect.centerx, rect.bottom - 29),
            MUTED,
            anchor="center",
        )

    def cancel_capture(self) -> None:
        self.editor.cancel_capture()
        self.selected_key = f"controls:tab:{self.control_player}"
        self.reset_pointer()
        self.show_status("Captura cancelada.")
        self.audio.play("cancel")

    def render(self, now: float | None = None) -> pygame.Surface:
        now = time.monotonic() if now is None else now
        rebuild = self.scene is None or self.scene_dirty
        previous_damage = self.pointer_damage
        if rebuild:
            self.text_commands.clear()
            self.items = []
            self.draw_background(now)
            self.draw_nav()
            if self.page == "home":
                self.draw_home()
            elif self.page == "colors":
                self.draw_colors()
            elif self.page == "controls":
                self.draw_controls()
            else:
                self.draw_settings()
            if (
                not self.capture_active
                and self.theme_picker_target is None
                and not any(item.key == self.selected_key for item in self.items)
            ):
                self.selected_key = self.items[0].key if self.items else ""
            self.draw_status_bar()
            if self.capture_active:
                self.draw_capture_overlay()
            elif self.theme_picker_target is not None:
                self.draw_theme_picker()
            self.scene = self.canvas.copy()
            self.scene_dirty = False
            self.full_redraw = True
            previous_damage = None
        else:
            self.full_redraw = False
            if previous_damage is not None:
                self.canvas.blit(self.scene, previous_damage, previous_damage)
        pointer_damage = None
        if not self.capture_active:
            pointer_damage = self.draw_pointer(now)
        self.pointer_damage = pointer_damage
        if previous_damage is None:
            self.frame_damage = pointer_damage
        elif pointer_damage is None:
            self.frame_damage = previous_damage
        else:
            self.frame_damage = previous_damage.union(pointer_damage)
        return self.canvas

    def logical_mouse(self, position: tuple[int, int]) -> tuple[int, int] | None:
        width, height = self.window.get_size()
        scale = min(width / LOGICAL_SIZE[0], height / LOGICAL_SIZE[1])
        draw_width = round(LOGICAL_SIZE[0] * scale)
        draw_height = round(LOGICAL_SIZE[1] * scale)
        offset_x = (width - draw_width) // 2
        offset_y = (height - draw_height) // 2
        x, y = position
        if not (
            offset_x <= x < offset_x + draw_width
            and offset_y <= y < offset_y + draw_height
        ):
            return None
        return (round((x - offset_x) / scale), round((y - offset_y) / scale))

    def output_scale(self) -> float:
        width, height = self.window.get_size()
        return min(width / LOGICAL_SIZE[0], height / LOGICAL_SIZE[1])

    def enforce_minimum_window_size(self) -> bool:
        if self.preview:
            return False
        width, height = self.window.get_size()
        size = (
            max(MINIMUM_WINDOW_SIZE[0], width),
            max(MINIMUM_WINDOW_SIZE[1], height),
        )
        if size == (width, height):
            return False
        self.window = pygame.display.set_mode(
            size,
            pygame.RESIZABLE | pygame.DOUBLEBUF,
        )
        pygame.display.set_caption("Ivalice Together")
        self._set_native_window_icons()
        return True

    def present_native_text(
        self,
        scale: float,
        offset: tuple[int, int],
    ) -> None:
        for command in self.text_commands:
            font = self.fonts.scaled(command.font, scale)
            rendered = font.render(command.text, True, command.color)
            rect = rendered.get_rect()
            position = (
                offset[0] + round(command.position[0] * scale),
                offset[1] + round(command.position[1] * scale),
            )
            setattr(rect, command.anchor, position)
            if command.shadow:
                shade = font.render(command.text, True, (0, 0, 0))
                shadow_offset = max(1, round(2 * scale))
                self.window.blit(shade, rect.move(shadow_offset, shadow_offset))
            self.window.blit(rendered, rect)

    def present(self) -> None:
        width, height = self.window.get_size()
        if (width, height) == LOGICAL_SIZE:
            if not self.full_redraw:
                if self.frame_damage is not None:
                    damage = self.frame_damage.clip(self.canvas.get_rect())
                    if damage:
                        self.window.blit(self.canvas, damage, damage)
                        pygame.display.update(damage)
                return
            self.window.blit(self.canvas, (0, 0))
            pygame.display.flip()
            return

        self.window.fill(INK)
        scale = min(width / LOGICAL_SIZE[0], height / LOGICAL_SIZE[1])
        size = (
            max(1, round(LOGICAL_SIZE[0] * scale)),
            max(1, round(LOGICAL_SIZE[1] * scale)),
        )
        frame = pygame.transform.smoothscale(self.canvas, size)
        offset = ((width - size[0]) // 2, (height - size[1]) // 2)
        self.window.blit(frame, offset)
        self.present_native_text(scale, offset)
        pygame.display.flip()

    def navigate(self, direction: str) -> None:
        current = self.item()
        if current is None and self.items:
            self.selected_key = self.items[0].key
            return
        if current is None:
            return
        cx, cy = current.rect.center
        candidates: list[tuple[float, Item]] = []
        for candidate in self.items:
            if candidate.key == current.key:
                continue
            x, y = candidate.rect.center
            dx, dy = x - cx, y - cy
            if direction == "up" and dy < -2:
                score = abs(dy) + abs(dx) * 0.43
            elif direction == "down" and dy > 2:
                score = abs(dy) + abs(dx) * 0.43
            elif direction == "left" and dx < -2:
                score = abs(dx) + abs(dy) * 0.55
            elif direction == "right" and dx > 2:
                score = abs(dx) + abs(dy) * 0.55
            else:
                continue
            candidates.append((score, candidate))
        if not candidates:
            return
        self.selected_key = min(candidates, key=lambda value: value[0])[1].key
        self.audio.play("move")

    def handle_key(self, event: pygame.event.Event) -> None:
        if self.editor.capture_target is not None:
            vk = pygame_key_to_vk(event.key, getattr(event, "unicode", ""))
            if vk is None:
                self.show_status("Essa tecla não pode ser usada.", "error")
                self.audio.play("error")
                return
            result = self.editor.feed_keyboard_vk(vk)
            result = self.persist_control_capture(result)
            self.show_status(
                result.message,
                "error" if result.kind in ("invalid", "wrong_pad") else "ok",
            )
            self.audio.play(
                "error" if result.kind in ("invalid", "wrong_pad") else "confirm"
            )
            if self.editor.capture_target is None:
                self.selected_key = f"controls:tab:{self.control_player}"
                self.reset_pointer()
            return

    def handle_mouse(self, event: pygame.event.Event) -> None:
        position = self.logical_mouse(event.pos)
        if position is None:
            return
        hovered = next(
            (item for item in reversed(self.items) if item.rect.collidepoint(position)),
            None,
        )
        if event.type == pygame.MOUSEMOTION:
            if hovered and hovered.key != self.selected_key:
                self.selected_key = hovered.key
                self.audio.play("move")
            return
        if event.button == 3:
            if self.capture_active:
                self.cancel_capture()
            elif self.theme_picker_target is not None:
                self.close_theme_picker()
            elif hovered and hovered.clear:
                hovered.clear()
            return
        if event.button == 1 and hovered:
            self.selected_key = hovered.key
            if hovered.action:
                hovered.action()

    def poll_pad(self, now: float) -> bool:
        if (
            not self.capture_active
            or getattr(self, "controller_discovery_suspended", False)
        ):
            return False
        if now - self.last_pad_poll < 0.028:
            return False
        self.last_pad_poll = now
        self.refresh_controller_states()
        if self.editor.assign_target is not None:
            result = self.editor.poll_assignment(
                self.states,
                self.sdl_states,
                now=now,
            )
        else:
            result = self.editor.poll_xinput(self.states)
            if result is None and self.capture_active:
                result = self.editor.poll_sdl(self.sdl_states)
        if result:
            result = self.persist_control_capture(result)
            bad = result.kind in ("invalid", "wrong_pad")
            self.show_status(result.message, "error" if bad else "ok")
            self.audio.play("error" if bad else "confirm")
            if not self.capture_active:
                self.selected_key = f"controls:tab:{self.control_player}"
                self.reset_pointer()
            return True
        return False

    def update_launcher(self) -> bool:
        now = time.monotonic()
        changed = False
        if now - self.last_process_poll >= 1.0:
            game_open = bool(core.process_ids())
            was_open = self.game_open
            self.game_open = game_open
            if was_open and not game_open:
                if self.in_tray:
                    self.restore_from_tray()
                else:
                    changed = self.resume_controller_discovery() or changed
            changed = game_open != was_open or changed
            self.last_process_poll = now
            if self.launcher.attach_if_running():
                changed = True
        if (
            not self.in_tray
            and self.page == "controls"
            and getattr(self, "window_focused", True)
            and now - self.last_controller_state_poll >= 0.5
        ):
            changed = self.refresh_controller_states() or changed
            self.last_controller_state_poll = now
        recovery_at = getattr(self, "controller_recovery_at", 0.0)
        if recovery_at and now >= recovery_at:
            self.controller_recovery_at = 0.0
            changed = self.resume_controller_discovery() or changed
        for event in self.launcher.drain():
            changed = True
            if event.kind == "error":
                self.audio.play("error")
                self.restore_from_tray()
            elif event.kind == "ready":
                auto_minimize = (
                    bool(self.config.get("minimize_when_ready", True))
                    and not self.preview
                )
                if auto_minimize:
                    self.hide_to_tray()
                else:
                    self.audio.play("confirm")
            elif event.kind == "idle":
                self.restore_from_tray()
        return changed

    def prepare_navigation_visuals(self, now: float) -> None:
        """Optional one-time cache preparation after the first visible frame."""

    def run(self) -> int:
        minimized_event = getattr(pygame, "WINDOWMINIMIZED", -1)
        focus_lost_event = getattr(pygame, "WINDOWFOCUSLOST", -1)
        focus_gained_event = getattr(pygame, "WINDOWFOCUSGAINED", -1)
        controller_added_event = getattr(pygame, "CONTROLLERDEVICEADDED", -2)
        controller_removed_event = getattr(pygame, "CONTROLLERDEVICEREMOVED", -3)
        controller_events = {
            pygame.JOYDEVICEADDED,
            pygame.JOYDEVICEREMOVED,
            controller_added_event,
            controller_removed_event,
        }
        redraw_events = {
            getattr(pygame, name, -1)
            for name in (
                "WINDOWEXPOSED",
                "WINDOWRESIZED",
                "WINDOWSIZECHANGED",
                "WINDOWRESTORED",
            )
        }
        resize_events = {
            getattr(pygame, "WINDOWRESIZED", -1),
            getattr(pygame, "WINDOWSIZECHANGED", -1),
        }
        navigation_visuals_prepared = False
        while self.running:
            now = time.monotonic()
            redraw = False
            for event in self.read_window_events(now):
                if event.type == pygame.QUIT:
                    self.request_close()
                elif event.type == pygame.KEYDOWN:
                    self.handle_key(event)
                    redraw = True
                elif event.type in (pygame.MOUSEMOTION, pygame.MOUSEBUTTONDOWN):
                    self.handle_mouse(event)
                    redraw = True
                elif event.type in controller_events:
                    self.refresh_controller_states()
                    self.last_controller_state_poll = now
                    redraw = True
                elif event.type == minimized_event:
                    self.hide_to_tray()
                elif event.type == focus_lost_event:
                    self.window_focused = False
                    self.suspend_controller_discovery()
                elif event.type == focus_gained_event:
                    self.window_focused = True
                    if self.resume_controller_discovery():
                        redraw = True
                elif event.type in redraw_events:
                    if event.type in resize_events:
                        self.enforce_minimum_window_size()
                    redraw = True

            if self.tray is not None and self.tray.consume_restore_request():
                self.restore_from_tray()
                redraw = True
            if self.tray is not None and self.tray.consume_exit_request():
                self.exit_completely()
                break
            if not self.in_tray and self.poll_pad(now):
                redraw = True
            if self.update_launcher():
                redraw = True

            status_expired = now > self.status_until
            if status_expired != self.status_expired:
                self.status_expired = status_expired
                redraw = True
            if redraw:
                self.scene_dirty = True
            if self.in_tray:
                self.clock.tick(TRAY_POLL_FPS)
                continue
            self.render(now)
            self.present()
            if not navigation_visuals_prepared:
                self.prepare_navigation_visuals(now)
                navigation_visuals_prepared = True
            self.clock.tick(VISIBLE_FPS)
        self.close()
        return 0


ALREADY_OPEN_MESSAGE = (
    "O configurador já está aberto. Procure o ícone do co-op perto do relógio."
)


def already_open_message() -> str:
    language = core.load_launcher_config().get("language", "en")
    return launcher_i18n.translate(ALREADY_OPEN_MESSAGE, language)


def main() -> int:
    instance = core.acquire_launcher_instance()
    if instance is None:
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(
                None,
                already_open_message(),
                "Ivalice Together",
                0x40,
            )
        return 0
    try:
        return LauncherApp().run()
    finally:
        core.release_launcher_instance(instance)


if __name__ == "__main__":
    raise SystemExit(main())
