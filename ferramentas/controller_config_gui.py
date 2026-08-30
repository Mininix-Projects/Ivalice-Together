"""Global controls and character-theme studio for the FF12 co-op mod."""

from __future__ import annotations

import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # co-op modules


import controller_bindings as controls
import coop_roster
import menu_themes
import tool_theme as ui
from PIL import Image, ImageTk

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
try:
    import pygame
except ImportError:  # The mapper remains usable without the optional names.
    pygame = None


BG = ui.COLORS["bg"]
PANEL = ui.COLORS["card"]
PANEL_2 = ui.COLORS["card_alt"]
TEXT = ui.COLORS["text"]
MUTED = ui.COLORS["muted"]
BLUE = ui.COLORS["blue"]
YELLOW = ui.COLORS["gold"]
ACTIVE = ui.COLORS["green"]

THEME_DISPLAY_NAMES = {
    "blue": "Sapphire",
    "gold": "Gold",
    "ruby": "Ruby",
    "emerald": "Emerald",
    "purple": "Amethyst",
    "fire": "Fire",
    "pink": "Rose",
    "black": "Onyx",
    "light_brown": "Sandstone",
    "dark_brown": "Dark bronze",
}


def theme_display_name(name: object) -> str:
    normalized = menu_themes.normalize_name(name)
    return THEME_DISPLAY_NAMES.get(normalized, normalized.replace("_", " ").title())


def save_character_themes(themes: dict[int, str]) -> menu_themes.ThemeSelection:
    try:
        existing = menu_themes.load_selection()
    except (OSError, TypeError, ValueError):
        existing = menu_themes.ThemeSelection()
    overrides = dict(existing.character_overrides)
    for profile in menu_themes.PLAYABLE_CHARACTER_PROFILES:
        overrides[profile.member_id] = menu_themes.normalize_name(
            themes.get(
                profile.member_id,
                existing.for_character(profile.member_id),
            ),
            profile.default_theme,
        )
    return menu_themes.save_selection(
        menu_themes.ThemeSelection(
            player_1=existing.player_1,
            player_2=existing.player_2,
            player_3=existing.player_3,
            character_overrides=tuple(sorted(overrides.items())),
            assignment_mode=existing.assignment_mode,
        )
    )


# Emulator-style callouts used by the per-player controller diagrams.
# Every configurable action appears exactly once for its player.
BINDING_DIAGRAM_LAYOUT = {
    "p1": (
        ("menu_up", "MENU UP", "left", "DPAD_UP"),
        ("menu_down", "MENU DOWN", "left", "DPAD_DOWN"),
        ("menu_left", "PREVIOUS / LEFT", "left", "DPAD_LEFT"),
        ("menu_right", "NEXT / RIGHT", "right", "DPAD_RIGHT"),
        ("native_party_menu", "PARTY / LICENSES", "right", "Y"),
        ("native_pause", "PAUSE", "right", "START"),
        ("confirm", "CONFIRM / OPEN", "right", "A"),
        ("cancel", "CANCEL / CLOSE", "right", "B"),
    ),
    "p2": (
        ("toggle_control", "ENABLE / DISABLE", "left", "HOTKEY"),
        ("move_up", "MOVE FORWARD", "left", "LS_UP"),
        ("move_left", "MOVE LEFT", "left", "LS_LEFT"),
        ("move_down", "MOVE BACK", "left", "LS_DOWN"),
        ("move_right", "MOVE RIGHT", "left", "LS_RIGHT"),
        ("run_toggle", "WALK / RUN", "left", "LEFT_THUMB"),
        ("flee", "FLEEING", "left", "RT"),
        ("battle_menu", "OPEN BATTLE MENU", "left", "X"),
        ("menu_left", "MENU LEFT", "left", "DPAD_LEFT"),
        ("menu_up", "MENU UP", "right", "DPAD_UP"),
        ("menu_down", "MENU DOWN", "right", "DPAD_DOWN"),
        ("menu_right", "MENU RIGHT", "right", "DPAD_RIGHT"),
        ("target_previous", "PREVIOUS TARGET", "left", "LB"),
        ("target_next", "NEXT TARGET", "right", "RB"),
        ("native_party_menu", "PARTY / LICENSES", "right", "Y"),
        ("native_pause", "PAUSE", "right", "START"),
        ("quickening", "ACTIVATE QUICKENING", "right", "X"),
        ("confirm", "CONFIRM / INTERACT", "right", "A"),
        ("cancel", "CANCEL / CLOSE", "right", "B"),
    ),
    "p3": (
        ("toggle_control", "ENABLE / DISABLE", "left", "HOTKEY"),
        ("move_up", "MOVE FORWARD", "left", "LS_UP"),
        ("move_left", "MOVE LEFT", "left", "LS_LEFT"),
        ("move_down", "MOVE BACK", "left", "LS_DOWN"),
        ("move_right", "MOVE RIGHT", "left", "LS_RIGHT"),
        ("run_toggle", "WALK / RUN", "left", "LEFT_THUMB"),
        ("flee", "FLEEING", "left", "RT"),
        ("battle_menu", "OPEN BATTLE MENU", "left", "X"),
        ("menu_left", "MENU LEFT", "left", "DPAD_LEFT"),
        ("menu_up", "MENU UP", "right", "DPAD_UP"),
        ("menu_down", "MENU DOWN", "right", "DPAD_DOWN"),
        ("menu_right", "MENU RIGHT", "right", "DPAD_RIGHT"),
        ("target_previous", "PREVIOUS TARGET", "left", "LB"),
        ("target_next", "NEXT TARGET", "right", "RB"),
        ("native_party_menu", "PARTY / LICENSES", "right", "Y"),
        ("native_pause", "PAUSE", "right", "START"),
        ("quickening", "ACTIVATE QUICKENING", "right", "A"),
        ("confirm", "CONFIRM / INTERACT", "right", "A"),
        ("cancel", "CANCEL / CLOSE", "right", "B"),
    ),
}


_COMPACT_PAD_NAMES = {
    "DPAD_UP": "D-UP",
    "DPAD_DOWN": "D-DN",
    "DPAD_LEFT": "D-LT",
    "DPAD_RIGHT": "D-RT",
    "LS_UP": "LS-UP",
    "LS_DOWN": "LS-DN",
    "LS_LEFT": "LS-LT",
    "LS_RIGHT": "LS-RT",
    "RS_UP": "RS-UP",
    "RS_DOWN": "RS-DN",
    "RS_LEFT": "RS-LT",
    "RS_RIGHT": "RS-RT",
    "LEFT_THUMB": "LS-CLK",
    "RIGHT_THUMB": "RS-CLK",
}


def compact_binding_label(
    binding: dict | None, assigned_index: int | None = None
) -> str:
    """Short labels that remain readable inside controller callout fields."""

    if not binding:
        return "+"
    if binding.get("device") == "keyboard":
        vk = int(binding.get("vk", 0))
        return controls.VK_NAMES.get(vk, f"VK{vk:02X}")
    if binding.get("device") == "xinput":
        index = (
            assigned_index
            if isinstance(assigned_index, int)
            else int(binding.get("index", 0))
        )
        control = str(binding.get("control", "?"))
        return f"P{index + 1} {_COMPACT_PAD_NAMES.get(control, control)}"
    return "+"


def full_binding_label(binding: dict | None, assigned_index: int | None = None) -> str:
    if not binding:
        return "—"
    if binding.get("device") == "keyboard":
        vk = int(binding.get("vk", 0))
        return f"Keyboard: {controls.VK_NAMES.get(vk, f'VK {vk:#04x}')}"
    if binding.get("device") == "xinput":
        index = (
            assigned_index
            if assigned_index is not None
            else int(binding.get("index", 0))
        )
        return f"Pad {index + 1}: {binding.get('control', '?')}"
    return "—"


class ControllerCanvas(tk.Canvas):
    def __init__(self, master, **kwargs):
        super().__init__(
            master,
            width=900,
            height=215,
            bg=PANEL,
            highlightthickness=0,
            **kwargs,
        )
        self.items: dict[str, list[int]] = {}
        self._draw()

    def tagged(self, control: str, item: int) -> None:
        self.items.setdefault(control, []).append(item)

    def _oval(self, control: str, coords, fill=PANEL_2, outline="#69717e", width=2):
        item = self.create_oval(*coords, fill=fill, outline=outline, width=width)
        self.tagged(control, item)
        return item

    def _draw(self) -> None:
        self.create_text(
            25,
            18,
            anchor="w",
            text="LIVE CONTROLLER",
            fill=MUTED,
            font=("Segoe UI", 9, "bold"),
        )
        self.create_polygon(
            275,
            58,
            350,
            36,
            450,
            52,
            550,
            36,
            625,
            58,
            690,
            177,
            618,
            197,
            545,
            139,
            355,
            139,
            282,
            197,
            210,
            177,
            fill="#303641",
            outline="#778190",
            width=3,
            smooth=True,
        )
        # D-pad.
        for name, coords, label in (
            ("DPAD_UP", (307, 79, 337, 109), "▲"),
            ("DPAD_DOWN", (307, 139, 337, 169), "▼"),
            ("DPAD_LEFT", (277, 109, 307, 139), "◀"),
            ("DPAD_RIGHT", (337, 109, 367, 139), "▶"),
        ):
            item = self.create_rectangle(
                *coords, fill=PANEL_2, outline="#69717e", width=2
            )
            self.tagged(name, item)
            self.create_text(
                (coords[0] + coords[2]) / 2,
                (coords[1] + coords[3]) / 2,
                text=label,
                fill=TEXT,
                font=("Segoe UI", 10, "bold"),
            )
        # Face buttons.
        for name, x, y, label in (
            ("Y", 590, 78, "Y"),
            ("X", 555, 113, "X"),
            ("B", 625, 113, "B"),
            ("A", 590, 148, "A"),
        ):
            self._oval(name, (x - 16, y - 16, x + 16, y + 16))
            self.create_text(x, y, text=label, fill=TEXT, font=("Segoe UI", 11, "bold"))
        # Sticks.
        for prefix, x, y in (("LS", 397, 128), ("RS", 510, 128)):
            self._oval(
                prefix + "_UP",
                (x - 25, y - 25, x + 25, y + 25),
                fill="#171a1f",
                width=3,
            )
            for suffix in ("DOWN", "LEFT", "RIGHT"):
                self.items[prefix + "_" + suffix] = self.items[prefix + "_UP"]
            self._oval(
                "LEFT_THUMB" if prefix == "LS" else "RIGHT_THUMB",
                (x - 11, y - 11, x + 11, y + 11),
                fill="#555e6c",
            )
        # Center and shoulders/triggers.
        for name, x, label in (("BACK", 425, "BACK"), ("START", 483, "START")):
            item = self.create_rectangle(
                x - 23, 72, x + 23, 94, fill=PANEL_2, outline="#69717e"
            )
            self.tagged(name, item)
            self.create_text(x, 83, text=label, fill=TEXT, font=("Segoe UI", 7, "bold"))
        for name, x1, x2, label in (
            ("LB", 285, 375, "LB"),
            ("RB", 525, 615, "RB"),
            ("LT", 303, 366, "LT"),
            ("RT", 534, 597, "RT"),
        ):
            y1, y2 = (45, 62) if name in ("LB", "RB") else (24, 41)
            item = (
                self.create_round_rectangle(
                    x1, y1, x2, y2, radius=8, fill=PANEL_2, outline="#69717e"
                )
                if hasattr(self, "create_round_rectangle")
                else self.create_rectangle(
                    x1, y1, x2, y2, fill=PANEL_2, outline="#69717e"
                )
            )
            self.tagged(name, item)
            self.create_text(
                (x1 + x2) / 2,
                (y1 + y2) / 2,
                text=label,
                fill=TEXT,
                font=("Segoe UI", 8, "bold"),
            )
        self.status = self.create_text(
            875,
            18,
            anchor="e",
            text="No controller input",
            fill=MUTED,
            font=("Segoe UI", 9),
        )

    def update_live(self, states, controller_names=None) -> None:
        strengths: dict[str, float] = {}
        active_index = None
        for index, state in enumerate(states):
            if state is None:
                continue
            if active_index is None:
                active_index = index
            for control in (
                tuple(controls.XINPUT_BUTTONS) + controls.XINPUT_AXIS_CONTROLS
            ):
                value = controls.xinput_control_strength(state, control)
                if value > strengths.get(control, 0.0):
                    strengths[control] = value
                if value > 0.55:
                    active_index = index
        touched = set()
        for control, items in self.items.items():
            value = strengths.get(control, 0.0)
            color = ACTIVE if value > 0.55 else ("#4d88c7" if value > 0.05 else PANEL_2)
            for item in items:
                if item not in touched:
                    self.itemconfigure(item, fill=color)
                    touched.add(item)
        self.itemconfigure(
            self.status,
            text=(
                (
                    controller_names.get(active_index, f"Controller {active_index + 1}")
                    if controller_names
                    else f"Controller {active_index + 1}"
                )
                + " connected"
                if active_index is not None
                else "No XInput controller detected"
            ),
            fill=(ACTIVE if active_index is not None else MUTED),
        )


class ModernControllerCanvas(tk.Canvas):
    """Compact, polished live XInput visualizer used by the configurator."""

    def __init__(self, master, **kwargs):
        super().__init__(
            master,
            width=1080,
            height=230,
            bg=PANEL,
            highlightbackground=ui.COLORS["border_soft"],
            highlightthickness=1,
            **kwargs,
        )
        self.items: dict[str, list[int]] = {}
        self._draw()

    def tagged(self, control: str, item: int) -> None:
        self.items.setdefault(control, []).append(item)

    def _shape(self, control: str, kind: str, coords, **kwargs) -> int:
        item = getattr(self, f"create_{kind}")(*coords, **kwargs)
        self.tagged(control, item)
        return item

    def _draw(self) -> None:
        border = ui.COLORS["border"]
        field = ui.COLORS["field"]
        self.create_text(
            24,
            22,
            anchor="w",
            text="LIVE INPUT MONITOR",
            fill=MUTED,
            font=(ui.FONT_DISPLAY, 9),
        )
        self.create_text(
            24,
            44,
            anchor="w",
            text="Press any connected controller input to preview it here.",
            fill=ui.COLORS["faint"],
            font=(ui.FONT, 9),
        )
        self.create_polygon(
            302,
            72,
            370,
            48,
            452,
            62,
            540,
            48,
            628,
            62,
            710,
            48,
            778,
            72,
            830,
            192,
            760,
            211,
            682,
            154,
            398,
            154,
            320,
            211,
            250,
            192,
            fill="#202C3A",
            outline="#52647A",
            width=3,
            smooth=True,
        )

        for name, coords, label in (
            ("DPAD_UP", (342, 89, 374, 121), "▲"),
            ("DPAD_DOWN", (342, 151, 374, 183), "▼"),
            ("DPAD_LEFT", (311, 120, 343, 152), "◀"),
            ("DPAD_RIGHT", (373, 120, 405, 152), "▶"),
        ):
            self._shape(
                name,
                "rectangle",
                coords,
                fill=field,
                outline=border,
                width=2,
            )
            self.create_text(
                (coords[0] + coords[2]) / 2,
                (coords[1] + coords[3]) / 2,
                text=label,
                fill=MUTED,
                font=(ui.FONT, 9, "bold"),
            )

        for name, x, y, color in (
            ("Y", 724, 90, "#F2C94C"),
            ("X", 688, 126, "#55A7F7"),
            ("B", 760, 126, "#F06B70"),
            ("A", 724, 162, "#55D196"),
        ):
            self._shape(
                name,
                "oval",
                (x - 16, y - 16, x + 16, y + 16),
                fill=PANEL_2,
                outline=border,
                width=2,
            )
            self.create_text(
                x,
                y,
                text=name,
                fill=color,
                font=(ui.FONT_DISPLAY, 11),
            )

        for prefix, x, y in (("LS", 455, 147), ("RS", 624, 147)):
            directional = self._shape(
                prefix + "_UP",
                "oval",
                (x - 27, y - 27, x + 27, y + 27),
                fill=field,
                outline=border,
                width=3,
            )
            for suffix in ("DOWN", "LEFT", "RIGHT"):
                self.items[prefix + "_" + suffix] = [directional]
            self._shape(
                "LEFT_THUMB" if prefix == "LS" else "RIGHT_THUMB",
                "oval",
                (x - 12, y - 12, x + 12, y + 12),
                fill="#536175",
                outline=border,
                width=2,
            )

        for name, x, label in (("BACK", 505, "BACK"), ("START", 575, "START")):
            self._shape(
                name,
                "rectangle",
                (x - 27, 84, x + 27, 108),
                fill=PANEL_2,
                outline=border,
            )
            self.create_text(x, 96, text=label, fill=MUTED, font=(ui.FONT_DISPLAY, 7))

        for name, x1, x2, y1, y2 in (
            ("LB", 335, 438, 56, 75),
            ("RB", 642, 745, 56, 75),
            ("LT", 353, 425, 33, 51),
            ("RT", 655, 727, 33, 51),
        ):
            self._shape(
                name,
                "rectangle",
                (x1, y1, x2, y2),
                fill=PANEL_2,
                outline=border,
            )
            self.create_text(
                (x1 + x2) / 2,
                (y1 + y2) / 2,
                text=name,
                fill=MUTED,
                font=(ui.FONT_DISPLAY, 8),
            )

        self.status = self.create_text(
            1054,
            22,
            anchor="e",
            text="No controller input",
            fill=MUTED,
            font=(ui.FONT_DISPLAY, 9),
        )

    def update_live(self, states, controller_names=None) -> None:
        strengths: dict[str, float] = {}
        active_index = None
        for index, state in enumerate(states):
            if state is None:
                continue
            if active_index is None:
                active_index = index
            for control in (
                tuple(controls.XINPUT_BUTTONS) + controls.XINPUT_AXIS_CONTROLS
            ):
                value = controls.xinput_control_strength(state, control)
                strengths[control] = max(strengths.get(control, 0.0), value)
                if value > 0.55:
                    active_index = index

        touched = set()
        for control, items in self.items.items():
            value = strengths.get(control, 0.0)
            color = ACTIVE if value > 0.55 else (BLUE if value > 0.05 else PANEL_2)
            for item in items:
                if item not in touched:
                    self.itemconfigure(item, fill=color)
                    touched.add(item)

        if active_index is None:
            status = "No XInput controller detected"
        elif controller_names:
            status = (
                controller_names.get(active_index, f"Controller {active_index + 1}")
                + " connected"
            )
        else:
            status = f"Controller {active_index + 1} connected"
        self.itemconfigure(
            self.status,
            text=status,
            fill=(ACTIVE if active_index is not None else MUTED),
        )


class CompactInputMonitor(tk.Frame):
    """Slim live monitor with device chips instead of a gamepad drawing."""

    def __init__(self, master, **kwargs):
        super().__init__(
            master,
            bg=PANEL,
            highlightbackground=ui.COLORS["border_soft"],
            highlightthickness=1,
            **kwargs,
        )
        identity = tk.Frame(self, bg=PANEL)
        identity.pack(side="left", fill="y", padx=(12, 16), pady=8)
        tk.Label(
            identity,
            text="LIVE INPUT",
            bg=PANEL,
            fg=TEXT,
            font=(ui.FONT_DISPLAY, 9),
            anchor="w",
        ).pack(anchor="w")
        self.activity_var = tk.StringVar(value="Waiting for input")
        tk.Label(
            identity,
            textvariable=self.activity_var,
            bg=PANEL,
            fg=MUTED,
            font=(ui.FONT, 8),
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))

        devices = tk.Frame(self, bg=PANEL)
        devices.pack(side="right", fill="y", padx=8, pady=7)
        self.device_labels: list[tk.Label] = []
        for index in range(4):
            label = tk.Label(
                devices,
                text=f"{index + 1}  OFFLINE",
                bg=ui.COLORS["field"],
                fg=ui.COLORS["faint"],
                font=(ui.FONT_DISPLAY, 8),
                padx=9,
                pady=5,
                width=18,
                anchor="w",
                highlightbackground=ui.COLORS["border_soft"],
                highlightthickness=1,
            )
            label.pack(side="left", padx=3)
            self.device_labels.append(label)

    def update_live(self, states, controller_names=None) -> None:
        active_index: int | None = None
        active_controls: list[tuple[float, str]] = []
        for index in range(4):
            state = states[index] if index < len(states) else None
            connected = state is not None
            if connected:
                strongest = 0.0
                for control in (
                    tuple(controls.XINPUT_BUTTONS) + controls.XINPUT_AXIS_CONTROLS
                ):
                    value = controls.xinput_control_strength(state, control)
                    strongest = max(strongest, value)
                    if value > 0.12:
                        active_controls.append((value, control))
                if strongest > 0.12:
                    active_index = index
            name = (
                controller_names.get(index, f"Controller {index + 1}")
                if controller_names
                else f"Controller {index + 1}"
            )
            short_name = name if len(name) <= 17 else name[:16] + "…"
            label = self.device_labels[index]
            label.configure(
                text=f"{index + 1}  {short_name}"
                if connected
                else f"{index + 1}  OFFLINE",
                bg=(
                    ui.COLORS["blue_deep"]
                    if index == active_index
                    else ui.COLORS["field"]
                ),
                fg=(
                    ACTIVE
                    if index == active_index
                    else (TEXT if connected else ui.COLORS["faint"])
                ),
                highlightbackground=(
                    BLUE if index == active_index else ui.COLORS["border_soft"]
                ),
            )

        if active_controls:
            strongest = sorted(active_controls, reverse=True)[:3]
            summary = "  ·  ".join(
                f"{control} {round(value * 100):d}%" for value, control in strongest
            )
            self.activity_var.set(summary)
        elif any(state is not None for state in states):
            self.activity_var.set("Connected · no active input")
        else:
            self.activity_var.set("No XInput controller detected")


class GamepadInputMonitor(tk.Frame):
    """Organized XInput preview with a clean, proportional gamepad diagram."""

    def __init__(self, master, **kwargs):
        super().__init__(
            master,
            bg=PANEL,
            highlightbackground=ui.COLORS["border_soft"],
            highlightthickness=1,
            **kwargs,
        )
        self.items: dict[str, list[int]] = {}
        self.idle_fill: dict[int, str] = {}
        self.canvas = tk.Canvas(
            self,
            width=600,
            height=168,
            bg=PANEL,
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.pack(side="left", fill="both", expand=True)
        self._draw_gamepad()

        devices = tk.Frame(
            self,
            bg=ui.COLORS["surface"],
            width=270,
            highlightbackground=ui.COLORS["border_soft"],
            highlightthickness=1,
        )
        devices.pack(side="right", fill="y", padx=7, pady=7)
        devices.pack_propagate(False)
        tk.Label(
            devices,
            text="XINPUT DEVICES",
            bg=ui.COLORS["surface"],
            fg=MUTED,
            font=(ui.FONT_DISPLAY, 8),
            anchor="w",
        ).pack(fill="x", padx=10, pady=(9, 5))
        device_grid = tk.Frame(devices, bg=ui.COLORS["surface"])
        device_grid.pack(fill="x", padx=7)
        for column in range(2):
            device_grid.columnconfigure(column, weight=1, uniform="device")
        self.device_labels: list[tk.Label] = []
        for index in range(4):
            label = tk.Label(
                device_grid,
                text=f"{index + 1}  OFFLINE",
                bg=ui.COLORS["field"],
                fg=ui.COLORS["faint"],
                font=(ui.FONT_DISPLAY, 7),
                padx=6,
                pady=6,
                anchor="w",
                highlightbackground=ui.COLORS["border_soft"],
                highlightthickness=1,
            )
            label.grid(
                row=index // 2,
                column=index % 2,
                sticky="ew",
                padx=3,
                pady=3,
            )
            self.device_labels.append(label)
        self.device_status_var = tk.StringVar(value="No controller detected")
        tk.Label(
            devices,
            textvariable=self.device_status_var,
            bg=ui.COLORS["surface"],
            fg=MUTED,
            font=(ui.FONT, 8),
            anchor="w",
            wraplength=245,
        ).pack(fill="x", padx=10, pady=(7, 0))

    def _register(self, control: str, item: int, idle: str) -> int:
        self.items.setdefault(control, []).append(item)
        self.idle_fill[item] = idle
        return item

    def _draw_gamepad(self) -> None:
        canvas = self.canvas
        field = ui.COLORS["field"]
        border = "#40536a"
        body = "#1a2633"
        canvas.create_text(
            16,
            16,
            anchor="w",
            text="INPUT PREVIEW",
            fill=MUTED,
            font=(ui.FONT_DISPLAY, 8),
        )
        self.activity_text = canvas.create_text(
            584,
            16,
            anchor="e",
            text="Waiting for input",
            fill=ui.COLORS["faint"],
            font=(ui.FONT, 8),
        )

        # One restrained silhouette with symmetric grips; controls sit inside
        # the body instead of floating around it.
        canvas.create_polygon(
            170,
            61,
            210,
            42,
            260,
            39,
            300,
            49,
            340,
            39,
            390,
            42,
            430,
            61,
            456,
            128,
            446,
            148,
            423,
            153,
            374,
            119,
            226,
            119,
            177,
            153,
            154,
            148,
            144,
            128,
            fill=body,
            outline=border,
            width=2,
            smooth=True,
            splinesteps=24,
        )
        canvas.create_line(
            225,
            119,
            375,
            119,
            fill=ui.COLORS["border_soft"],
            width=1,
        )

        for control, x1, x2, y1, y2 in (
            ("LT", 192, 246, 29, 40),
            ("RT", 354, 408, 29, 40),
            ("LB", 180, 251, 43, 56),
            ("RB", 349, 420, 43, 56),
        ):
            item = canvas.create_rectangle(
                x1,
                y1,
                x2,
                y2,
                fill=field,
                outline=border,
                width=1,
            )
            self._register(control, item, field)
            canvas.create_text(
                (x1 + x2) / 2,
                (y1 + y2) / 2,
                text=control,
                fill=MUTED,
                font=(ui.FONT_DISPLAY, 7),
            )

        # D-pad sectors remain individually readable when diagonals are used.
        for control, coords, label in (
            ("DPAD_UP", (203, 78, 221, 96), "▲"),
            ("DPAD_DOWN", (203, 104, 221, 122), "▼"),
            ("DPAD_LEFT", (185, 96, 203, 104), "◀"),
            ("DPAD_RIGHT", (221, 96, 239, 104), "▶"),
        ):
            item = canvas.create_rectangle(*coords, fill=field, outline=border, width=1)
            self._register(control, item, field)
            canvas.create_text(
                (coords[0] + coords[2]) / 2,
                (coords[1] + coords[3]) / 2,
                text=label,
                fill=MUTED,
                font=(ui.FONT, 7),
            )

        for prefix, x, y in (("LS", 266, 83), ("RS", 337, 108)):
            ring = canvas.create_oval(
                x - 18,
                y - 18,
                x + 18,
                y + 18,
                fill=field,
                outline=border,
                width=2,
            )
            self._register(prefix + "_UP", ring, field)
            for suffix in ("DOWN", "LEFT", "RIGHT"):
                self.items[prefix + "_" + suffix] = [ring]
            thumb_control = "LEFT_THUMB" if prefix == "LS" else "RIGHT_THUMB"
            thumb = canvas.create_oval(
                x - 8,
                y - 8,
                x + 8,
                y + 8,
                fill="#44566d",
                outline="#62758d",
                width=1,
            )
            self._register(thumb_control, thumb, "#44566d")

        for control, x, y, color in (
            ("Y", 402, 72, "#e5c34d"),
            ("X", 383, 91, "#58a9ef"),
            ("B", 421, 91, "#e66b72"),
            ("A", 402, 110, "#55ca91"),
        ):
            button = canvas.create_oval(
                x - 10,
                y - 10,
                x + 10,
                y + 10,
                fill=field,
                outline=color,
                width=2,
            )
            self._register(control, button, field)
            canvas.create_text(
                x,
                y,
                text=control,
                fill=color,
                font=(ui.FONT_DISPLAY, 8),
            )

        for control, x, glyph in (
            ("BACK", 302, "−"),
            ("START", 323, "+"),
        ):
            item = canvas.create_oval(
                x - 7,
                73,
                x + 7,
                87,
                fill=field,
                outline=border,
                width=1,
            )
            self._register(control, item, field)
            canvas.create_text(x, 80, text=glyph, fill=MUTED, font=(ui.FONT_DISPLAY, 8))

    def update_live(self, states, controller_names=None) -> None:
        strengths: dict[str, float] = {}
        device_strengths: list[float] = [0.0] * 4
        connected: list[bool] = [False] * 4
        controls_catalog = (
            tuple(controls.XINPUT_BUTTONS) + controls.XINPUT_AXIS_CONTROLS
        )
        for index in range(4):
            state = states[index] if index < len(states) else None
            if state is None:
                continue
            connected[index] = True
            for control in controls_catalog:
                value = controls.xinput_control_strength(state, control)
                strengths[control] = max(strengths.get(control, 0.0), value)
                device_strengths[index] = max(device_strengths[index], value)
        active_index = (
            max(range(4), key=device_strengths.__getitem__)
            if max(device_strengths, default=0.0) > 0.12
            else None
        )

        item_strengths: dict[int, float] = {}
        for control, items in self.items.items():
            strength = strengths.get(control, 0.0)
            for item in items:
                item_strengths[item] = max(item_strengths.get(item, 0.0), strength)
        for item, strength in item_strengths.items():
            self.canvas.itemconfigure(
                item,
                fill=(
                    ACTIVE
                    if strength > 0.55
                    else (
                        ui.COLORS["blue_deep"]
                        if strength > 0.05
                        else self.idle_fill.get(item, ui.COLORS["field"])
                    )
                ),
            )

        active_controls = sorted(
            ((value, control) for control, value in strengths.items() if value > 0.12),
            reverse=True,
        )
        activity = (
            "  ·  ".join(
                f"{control} {round(value * 100):d}%"
                for value, control in active_controls[:3]
            )
            if active_controls
            else "Waiting for input"
        )
        self.canvas.itemconfigure(
            self.activity_text,
            text=activity,
            fill=ACTIVE if active_controls else ui.COLORS["faint"],
        )

        for index, label in enumerate(self.device_labels):
            name = (
                controller_names.get(index, f"Pad {index + 1}")
                if controller_names
                else f"Pad {index + 1}"
            )
            short_name = name if len(name) <= 12 else name[:11] + "…"
            label.configure(
                text=f"{index + 1}  {short_name}"
                if connected[index]
                else f"{index + 1}  OFFLINE",
                bg=(
                    ui.COLORS["blue_deep"]
                    if index == active_index
                    else ui.COLORS["field"]
                ),
                fg=(
                    ACTIVE
                    if index == active_index
                    else (TEXT if connected[index] else ui.COLORS["faint"])
                ),
                highlightbackground=(
                    BLUE if index == active_index else ui.COLORS["border_soft"]
                ),
            )
        connected_count = sum(connected)
        self.device_status_var.set(
            f"{connected_count} connected · active pad {active_index + 1}"
            if active_index is not None
            else (
                f"{connected_count} connected · idle"
                if connected_count
                else "No controller detected"
            )
        )


class PlayerBindingDiagram(tk.Frame):
    """Per-player emulator-style gamepad map with editable callouts."""

    def __init__(self, master, owner, player: str, **kwargs):
        super().__init__(
            master,
            bg=PANEL,
            highlightbackground=ui.COLORS["border_soft"],
            highlightthickness=1,
            **kwargs,
        )
        self.owner = owner
        self.player = player
        self.accent = BLUE if player == "p1" else YELLOW
        self.control_items: dict[str, list[int]] = {}
        self.idle_fill: dict[int, str] = {}
        self.cards: dict[str, tk.Frame] = {}
        self._last_states = []
        self._last_names: dict[int, str] = {}
        self.canvas = tk.Canvas(
            self,
            bg=PANEL,
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._build_callouts()
        self.bind("<Configure>", self._redraw)

    def _build_callouts(self) -> None:
        for action, label, _side, _control in BINDING_DIAGRAM_LAYOUT[self.player]:
            card = tk.Frame(
                self,
                bg=ui.COLORS["surface"],
                highlightbackground=ui.COLORS["border"],
                highlightthickness=1,
            )
            for column in range(controls.MAX_BINDINGS_PER_ACTION):
                card.columnconfigure(column, weight=1, uniform=f"{action}_slots")
            tk.Label(
                card,
                text=label,
                bg=ui.COLORS["surface"],
                fg=self.accent,
                font=(ui.FONT_DISPLAY, 7),
                anchor="w",
            ).grid(
                row=0,
                column=0,
                columnspan=controls.MAX_BINDINGS_PER_ACTION,
                sticky="ew",
                padx=5,
                pady=(2, 0),
            )
            for slot in range(controls.MAX_BINDINGS_PER_ACTION):
                button = ttk.Button(
                    card,
                    width=7,
                    style="BindMini.TButton",
                    command=lambda p=self.player, a=action, s=slot: (
                        self.owner.start_capture(p, a, s)
                    ),
                )
                button.grid(
                    row=1,
                    column=slot,
                    sticky="ew",
                    padx=(
                        3 if slot == 0 else 1,
                        3
                        if slot == controls.MAX_BINDINGS_PER_ACTION - 1
                        else 1,
                    ),
                    pady=(1, 3),
                )
                button.bind(
                    "<Button-3>",
                    lambda _event, p=self.player, a=action, s=slot: (
                        self.owner.clear_binding(p, a, s)
                    ),
                )
                setattr(button, "_diagram_binding", True)
                self.owner.binding_buttons[(self.player, action, slot)] = button
            self.cards[action] = card

    def _register(self, control: str, item: int, idle: str) -> int:
        self.control_items.setdefault(control, []).append(item)
        self.idle_fill[item] = idle
        return item

    def _place_cards(self, width: int, height: int) -> dict[str, tuple[int, int, int]]:
        card_width = min(220, max(184, ((width - 314) // 2) - 22))
        side_actions: dict[str, list[str]] = {"left": [], "right": []}
        for action, _label, side, _control in BINDING_DIAGRAM_LAYOUT[self.player]:
            side_actions[side].append(action)
        positions: dict[str, tuple[int, int, int]] = {}
        for side, actions in side_actions.items():
            count = max(1, len(actions))
            usable = max(180, height - 12)
            card_height = min(43, max(36, (usable - (count - 1) * 4) // count))
            gap = (usable - count * card_height) / (count - 1) if count > 1 else 0
            x = 6 if side == "left" else width - card_width - 6
            for row, action in enumerate(actions):
                y = round(6 + row * (card_height + gap))
                self.cards[action].place(
                    x=x,
                    y=y,
                    width=card_width,
                    height=card_height,
                )
                self.cards[action].lift()
                positions[action] = (x, y + card_height // 2, card_width)
        return positions

    def _redraw(self, _event=None) -> None:
        width = max(760, self.winfo_width())
        height = max(230, self.winfo_height())
        self.canvas.delete("diagram")
        self.control_items.clear()
        self.idle_fill.clear()
        card_positions = self._place_cards(width, height)
        cx = width // 2
        cy = height // 2
        field = ui.COLORS["field"]
        border = "#40536a"
        body = "#172330"

        self.canvas.create_text(
            cx,
            12,
            text=f"{self.player.upper()} CONTROL MAP",
            fill=ui.COLORS["faint"],
            font=(ui.FONT_DISPLAY, 7),
            tags="diagram",
        )
        self.canvas.create_polygon(
            cx - 128,
            cy - 50,
            cx - 92,
            cy - 68,
            cx - 43,
            cy - 67,
            cx,
            cy - 55,
            cx + 43,
            cy - 67,
            cx + 92,
            cy - 68,
            cx + 128,
            cy - 50,
            cx + 148,
            cy + 49,
            cx + 134,
            cy + 72,
            cx + 111,
            cy + 73,
            cx + 65,
            cy + 34,
            cx - 65,
            cy + 34,
            cx - 111,
            cy + 73,
            cx - 134,
            cy + 72,
            cx - 148,
            cy + 49,
            fill=body,
            outline=border,
            width=2,
            smooth=True,
            splinesteps=24,
            tags="diagram",
        )

        anchors = {
            "LT": (cx - 84, cy - 76),
            "RT": (cx + 84, cy - 76),
            "LB": (cx - 83, cy - 60),
            "RB": (cx + 83, cy - 60),
            "DPAD_UP": (cx - 91, cy - 12),
            "DPAD_DOWN": (cx - 91, cy + 20),
            "DPAD_LEFT": (cx - 107, cy + 4),
            "DPAD_RIGHT": (cx - 75, cy + 4),
            "LS_UP": (cx - 38, cy - 4),
            "LS_DOWN": (cx - 38, cy + 24),
            "LS_LEFT": (cx - 52, cy + 10),
            "LS_RIGHT": (cx - 24, cy + 10),
            "LEFT_THUMB": (cx - 38, cy + 10),
            "RS_UP": (cx + 34, cy + 18),
            "RS_DOWN": (cx + 34, cy + 46),
            "RS_LEFT": (cx + 20, cy + 32),
            "RS_RIGHT": (cx + 48, cy + 32),
            "RIGHT_THUMB": (cx + 34, cy + 32),
            "Y": (cx + 93, cy - 23),
            "X": (cx + 75, cy - 5),
            "B": (cx + 111, cy - 5),
            "A": (cx + 93, cy + 13),
            "BACK": (cx - 18, cy - 43),
            "START": (cx + 18, cy - 43),
            "HOTKEY": (cx, cy - 45),
        }

        hotkey = self.canvas.create_rectangle(
            cx - 17,
            cy - 53,
            cx + 17,
            cy - 37,
            fill=field,
            outline=border,
            width=1,
            tags="diagram",
        )
        self._register("HOTKEY", hotkey, field)
        self.canvas.create_text(
            cx,
            cy - 45,
            text="KEY",
            fill=ui.COLORS["faint"],
            font=(ui.FONT_BODY, 7),
            tags="diagram",
        )

        for control, label in (("BACK", "−"), ("START", "+")):
            x, y = anchors[control]
            item = self.canvas.create_rectangle(
                x - 7,
                y - 5,
                x + 7,
                y + 5,
                fill=field,
                outline=border,
                width=1,
                tags="diagram",
            )
            self._register(control, item, field)
            self.canvas.create_text(
                x,
                y,
                text=label,
                fill=MUTED,
                font=(ui.FONT_DISPLAY, 6),
                tags="diagram",
            )

        for action, _label, side, control in BINDING_DIAGRAM_LAYOUT[self.player]:
            card_x, card_y, card_width = card_positions[action]
            start_x = card_x + card_width if side == "left" else card_x
            anchor_x, anchor_y = anchors[control]
            elbow_x = cx - 158 if side == "left" else cx + 158
            self.canvas.create_line(
                start_x,
                card_y,
                elbow_x,
                card_y,
                elbow_x,
                anchor_y,
                anchor_x,
                anchor_y,
                fill=self.accent,
                width=1,
                tags="diagram",
            )
            self.canvas.create_oval(
                anchor_x - 2,
                anchor_y - 2,
                anchor_x + 2,
                anchor_y + 2,
                fill=self.accent,
                outline="",
                tags="diagram",
            )

        for control, x1, x2 in (
            ("LT", cx - 112, cx - 58),
            ("RT", cx + 58, cx + 112),
            ("LB", cx - 119, cx - 48),
            ("RB", cx + 48, cx + 119),
        ):
            y1 = cy - 82 if control in ("LT", "RT") else cy - 67
            y2 = y1 + 13
            item = self.canvas.create_rectangle(
                x1,
                y1,
                x2,
                y2,
                fill=field,
                outline=border,
                width=1,
                tags="diagram",
            )
            self._register(control, item, field)
            self.canvas.create_text(
                (x1 + x2) / 2,
                (y1 + y2) / 2,
                text=control,
                fill=MUTED,
                font=(ui.FONT_DISPLAY, 7),
                tags="diagram",
            )

        for control, (x, y), glyph in (
            ("DPAD_UP", anchors["DPAD_UP"], "U"),
            ("DPAD_DOWN", anchors["DPAD_DOWN"], "D"),
            ("DPAD_LEFT", anchors["DPAD_LEFT"], "L"),
            ("DPAD_RIGHT", anchors["DPAD_RIGHT"], "R"),
        ):
            item = self.canvas.create_rectangle(
                x - 8,
                y - 8,
                x + 8,
                y + 8,
                fill=field,
                outline=border,
                tags="diagram",
            )
            self._register(control, item, field)
            self.canvas.create_text(
                x,
                y,
                text=glyph,
                fill=MUTED,
                font=(ui.FONT_DISPLAY, 6),
                tags="diagram",
            )

        for prefix, x, y in (
            ("LS", cx - 38, cy + 10),
            ("RS", cx + 34, cy + 32),
        ):
            ring = self.canvas.create_oval(
                x - 20,
                y - 20,
                x + 20,
                y + 20,
                fill=field,
                outline=border,
                width=2,
                tags="diagram",
            )
            for suffix in ("UP", "DOWN", "LEFT", "RIGHT"):
                self.control_items[prefix + "_" + suffix] = [ring]
            self.idle_fill[ring] = field
            thumb_control = "LEFT_THUMB" if prefix == "LS" else "RIGHT_THUMB"
            thumb = self.canvas.create_oval(
                x - 9,
                y - 9,
                x + 9,
                y + 9,
                fill="#41536a",
                outline="#62758d",
                tags="diagram",
            )
            self._register(thumb_control, thumb, "#41536a")

        for control, color in (
            ("Y", "#e5c34d"),
            ("X", "#58a9ef"),
            ("B", "#e66b72"),
            ("A", "#55ca91"),
        ):
            x, y = anchors[control]
            item = self.canvas.create_oval(
                x - 9,
                y - 9,
                x + 9,
                y + 9,
                fill=field,
                outline=color,
                width=2,
                tags="diagram",
            )
            self._register(control, item, field)
            self.canvas.create_text(
                x,
                y,
                text=control,
                fill=color,
                font=(ui.FONT_DISPLAY, 7),
                tags="diagram",
            )

        self.status_item = self.canvas.create_text(
            cx,
            height - 9,
            text="Assigned controller idle",
            fill=ui.COLORS["faint"],
            font=(ui.FONT, 7),
            tags="diagram",
        )
        self._update_visual_state()

    def update_live(self, states, controller_names=None) -> None:
        self._last_states = states
        self._last_names = controller_names or {}
        self._update_visual_state()

    def _update_visual_state(self) -> None:
        if not hasattr(self, "status_item"):
            return
        assigned = self.owner.config.get("devices", {}).get(self.player)
        state = (
            self._last_states[assigned]
            if isinstance(assigned, int) and assigned < len(self._last_states)
            else None
        )
        strengths: dict[str, float] = {}
        if state is not None:
            for control in (
                tuple(controls.XINPUT_BUTTONS) + controls.XINPUT_AXIS_CONTROLS
            ):
                strengths[control] = controls.xinput_control_strength(state, control)
        item_strengths: dict[int, float] = {}
        for control, items in self.control_items.items():
            for item in items:
                item_strengths[item] = max(
                    item_strengths.get(item, 0.0),
                    strengths.get(control, 0.0),
                )
        for item, strength in item_strengths.items():
            self.canvas.itemconfigure(
                item,
                fill=(
                    ACTIVE
                    if strength > 0.55
                    else (
                        ui.COLORS["blue_deep"]
                        if strength > 0.05
                        else self.idle_fill.get(item, ui.COLORS["field"])
                    )
                ),
            )
        active = sorted(
            (
                (strength, control)
                for control, strength in strengths.items()
                if strength > 0.12
            ),
            reverse=True,
        )
        if not isinstance(assigned, int):
            status = "No controller assigned"
        elif state is None:
            status = f"Pad {assigned + 1} offline"
        elif active:
            status = "  /  ".join(control for _value, control in active[:2])
        else:
            name = self._last_names.get(assigned, f"Pad {assigned + 1}")
            status = f"{name} idle"
        self.canvas.itemconfigure(
            self.status_item,
            text=status,
            fill=ACTIVE if active else ui.COLORS["faint"],
        )


class Configurator:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("FF12 Local Co-op — Studio")
        self.root.geometry("960x640")
        self.root.minsize(860, 560)
        self.root.configure(bg=BG)
        try:
            self.root.iconbitmap(default="")
        except tk.TclError:
            pass
        self.config, self.controller_config_report = controls.load_config_with_report()
        self.controller_names = self._discover_controller_names()
        self.binding_buttons: dict[tuple[str, str, int], ttk.Button] = {}
        self.player_diagrams: dict[str, PlayerBindingDiagram] = {}
        self.assignment_vars: dict[str, tk.StringVar] = {}
        self.assign_target: str | None = None
        self.capture_target: tuple[str, str, int] | None = None
        self.capture_baseline: set[tuple[int, str]] = set()
        self.states = controls.read_xinput_states()
        self.theme_selection = self._load_theme_selection()
        self.theme_vars: dict[int, tk.StringVar] = {}
        self.theme_preview_widgets: dict[int, dict[str, tk.Widget]] = {}
        self.theme_images: dict[int, ImageTk.PhotoImage] = {}
        # Who joins Vaan when the story first forms the party. Authored here
        # and never rewritten by in-game character swaps.
        self.coop_roster = coop_roster.load()
        self.roster_vars: dict[tuple[int, str], tk.BooleanVar] = {}
        self.roster_status_var: tk.StringVar | None = None
        self.theme_dirty = False
        self._style()
        self._build()
        if "--themes" in sys.argv[1:]:
            self.app_notebook.select(1)
        self.root.bind_all("<KeyPress>", self._key_pressed, add=True)
        self.root.after(30, self._poll_controllers)

    @staticmethod
    def _discover_controller_names() -> dict[int, str]:
        names: dict[int, str] = {}
        if pygame is None:
            return names
        try:
            pygame.joystick.init()
            for index in range(min(4, pygame.joystick.get_count())):
                joystick = pygame.joystick.Joystick(index)
                joystick.init()
                name = joystick.get_name().strip()
                if name:
                    names[index] = name
            pygame.joystick.quit()
        except pygame.error:
            return {}
        return names

    def _style(self) -> None:
        style = ui.apply_theme(self.root)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure(
            "Toggle.TCheckbutton",
            background=PANEL,
            foreground=TEXT,
            font=(ui.FONT_DISPLAY, 9),
        )
        style.map(
            "Toggle.TCheckbutton",
            background=[("active", PANEL)],
            foreground=[("active", TEXT)],
        )
        style.configure(
            "P1Assign.TButton",
            background=BLUE,
            foreground="#07131F",
            padding=(10, 6),
            font=(ui.FONT_DISPLAY, 8),
        )
        style.map(
            "P1Assign.TButton",
            background=[("active", ui.COLORS["blue_hover"])],
        )
        style.configure(
            "P2Assign.TButton",
            background=YELLOW,
            foreground="#211803",
            padding=(10, 6),
            font=(ui.FONT_DISPLAY, 8),
        )
        style.map(
            "P2Assign.TButton",
            background=[("active", ui.COLORS["gold_hover"])],
        )
        style.configure(
            "P3Assign.TButton",
            background=ACTIVE,
            foreground="#061A11",
            padding=(10, 6),
            font=(ui.FONT_DISPLAY, 8),
        )
        style.map(
            "P3Assign.TButton",
            background=[("active", ui.COLORS["green_hover"])],
        )
        style.configure(
            "Studio.TNotebook",
            background=BG,
            borderwidth=0,
            tabmargins=(0, 0, 0, 0),
        )
        style.configure(
            "Studio.TNotebook.Tab",
            background=ui.COLORS["surface"],
            foreground=MUTED,
            bordercolor=ui.COLORS["border_soft"],
            padding=(16, 8),
            font=(ui.FONT_DISPLAY, 9),
        )
        style.map(
            "Studio.TNotebook.Tab",
            background=[
                ("selected", ui.COLORS["card"]),
                ("active", ui.COLORS["card_alt"]),
            ],
            foreground=[("selected", TEXT), ("active", TEXT)],
            bordercolor=[("selected", BLUE)],
        )
        style.configure(
            "Inner.TNotebook",
            background=PANEL,
            borderwidth=0,
        )
        style.configure(
            "Inner.TNotebook.Tab",
            background=ui.COLORS["surface"],
            foreground=MUTED,
            bordercolor=ui.COLORS["border_soft"],
            padding=(13, 6),
            font=(ui.FONT_DISPLAY, 8),
        )
        style.map(
            "Inner.TNotebook.Tab",
            background=[
                ("selected", PANEL),
                ("active", PANEL_2),
            ],
            foreground=[("selected", TEXT), ("active", TEXT)],
            bordercolor=[("selected", YELLOW)],
        )
        style.configure(
            "Primary.TButton",
            padding=(11, 6),
            font=(ui.FONT_DISPLAY, 9),
        )
        style.configure(
            "Gold.TButton",
            padding=(11, 6),
            font=(ui.FONT_DISPLAY, 9),
        )
        style.configure("Secondary.TButton", padding=(10, 6))
        style.configure("Bind.TButton", padding=(7, 5), font=(ui.FONT, 8))
        style.configure(
            "BindMini.TButton",
            background=ui.COLORS["card_alt"],
            foreground=TEXT,
            bordercolor=ui.COLORS["border"],
            padding=(2, 1),
            font=(ui.FONT, 7),
        )
        style.configure(
            "BindMiniEmpty.TButton",
            background=ui.COLORS["field"],
            foreground=ui.COLORS["faint"],
            bordercolor=ui.COLORS["border_soft"],
            padding=(2, 1),
            font=(ui.FONT, 7),
        )
        style.configure(
            "BindEmpty.TButton",
            background=PANEL,
            foreground=ui.COLORS["faint"],
            bordercolor=ui.COLORS["border_soft"],
            padding=(7, 5),
            font=(ui.FONT, 8),
        )
        style.configure("TCombobox", padding=(6, 5))

    def _build(self) -> None:
        header = tk.Frame(
            self.root,
            bg=ui.COLORS["surface"],
            highlightbackground=ui.COLORS["border_soft"],
            highlightthickness=0,
        )
        header.pack(fill="x")
        brand = tk.Frame(header, bg=ui.COLORS["surface"])
        brand.pack(side="left", padx=30, pady=17)
        tk.Label(
            brand,
            text="THE ZODIAC AGE  /  LOCAL CO-OP",
            bg=ui.COLORS["surface"],
            fg=YELLOW,
            font=(ui.FONT_DISPLAY, 9),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            brand,
            text="Co-op Studio",
            bg=ui.COLORS["surface"],
            fg=TEXT,
            font=(ui.FONT_DISPLAY, 24),
            anchor="w",
        ).pack(anchor="w", pady=(0, 1))
        tk.Label(
            brand,
            text="One place for player controls and character-owned visual themes.",
            bg=ui.COLORS["surface"],
            fg=MUTED,
            font=(ui.FONT, 9),
            anchor="w",
        ).pack(anchor="w")

        status_chip = tk.Frame(
            header,
            bg=ui.COLORS["card_alt"],
            highlightbackground=ui.COLORS["border"],
            highlightthickness=1,
        )
        status_chip.pack(side="right", padx=30, pady=21)
        tk.Label(
            status_chip,
            text="●",
            bg=ui.COLORS["card_alt"],
            fg=ACTIVE,
            font=(ui.FONT_DISPLAY, 12),
        ).pack(side="left", padx=(12, 6), pady=9)
        tk.Label(
            status_chip,
            text="LIVE CONFIG  /  HOT RELOAD",
            bg=ui.COLORS["card_alt"],
            fg=TEXT,
            font=(ui.FONT_DISPLAY, 9),
        ).pack(side="left", padx=(0, 12), pady=9)

        self.app_notebook = ttk.Notebook(self.root, style="Studio.TNotebook")
        self.app_notebook.pack(fill="both", expand=True, padx=24, pady=(18, 22))
        controls_page = tk.Frame(self.app_notebook, bg=BG)
        themes_page = tk.Frame(self.app_notebook, bg=BG)
        self.app_notebook.add(controls_page, text="  CONTROLS  ")
        self.app_notebook.add(themes_page, text="  CHARACTER THEMES  ")
        self._build_controls_page(controls_page)
        self._build_themes_page(themes_page)

    def _build(self) -> None:
        """Build the compact studio shell."""

        header = tk.Frame(
            self.root,
            bg=ui.COLORS["surface"],
            highlightbackground=ui.COLORS["border_soft"],
            highlightthickness=0,
        )
        header.pack(fill="x")
        brand = tk.Frame(header, bg=ui.COLORS["surface"])
        brand.pack(side="left", padx=18, pady=10)
        tk.Label(
            brand,
            text="FF12 CO-OP STUDIO",
            bg=ui.COLORS["surface"],
            fg=TEXT,
            font=(ui.FONT_DISPLAY, 15),
            anchor="w",
        ).pack(side="left")
        tk.Label(
            brand,
            text="controls  ·  character themes",
            bg=ui.COLORS["surface"],
            fg=MUTED,
            font=(ui.FONT, 8),
            anchor="w",
        ).pack(side="left", padx=(10, 0), pady=(3, 0))
        tk.Label(
            header,
            text="●  LIVE CONFIG",
            bg=ui.COLORS["surface"],
            fg=ACTIVE,
            font=(ui.FONT_DISPLAY, 8),
        ).pack(side="right", padx=18)

        self.app_notebook = ttk.Notebook(self.root, style="Studio.TNotebook")
        self.app_notebook.pack(fill="both", expand=True, padx=14, pady=(10, 14))
        controls_page = tk.Frame(self.app_notebook, bg=BG)
        themes_page = tk.Frame(self.app_notebook, bg=BG)
        self.app_notebook.add(controls_page, text="CONTROLS")
        self.app_notebook.add(themes_page, text="CHARACTER THEMES")
        self._build_controls_page(controls_page)
        self._build_themes_page(themes_page)

    def _build_controls_page(self, parent: tk.Widget) -> None:
        intro = tk.Frame(parent, bg=BG)
        intro.pack(fill="x", padx=4, pady=(16, 12))
        tk.Label(
            intro,
            text="Input ownership",
            bg=BG,
            fg=TEXT,
            font=(ui.FONT_DISPLAY, 18),
        ).pack(side="left")
        tk.Label(
            intro,
            text="Bind keyboards and XInput devices independently for all players.",
            bg=BG,
            fg=MUTED,
            font=(ui.FONT, 9),
        ).pack(side="left", padx=(14, 0), pady=(7, 0))

        self.controller_canvas = ModernControllerCanvas(parent)
        self.controller_canvas.pack(fill="x", padx=4, pady=(0, 12))

        self.notebook = ttk.Notebook(parent, style="Inner.TNotebook")
        self.notebook.pack(fill="both", expand=True, padx=4)
        self._make_player_tab_modern("p1", "PLAYER 1")
        self._make_player_tab_modern("p2", "PLAYER 2")
        self._make_player_tab_modern("p3", "PLAYER 3")

        footer = tk.Frame(
            parent,
            bg=ui.COLORS["surface"],
            highlightbackground=ui.COLORS["border_soft"],
            highlightthickness=1,
        )
        footer.pack(fill="x", padx=4, pady=(12, 4))
        self.status_var = tk.StringVar(
            value="Controller bindings · saved changes apply on the next start."
        )
        tk.Label(
            footer,
            textvariable=self.status_var,
            bg=ui.COLORS["surface"],
            fg=MUTED,
            font=(ui.FONT, 9),
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=14)
        ttk.Button(
            footer,
            text="RELOAD",
            style="Secondary.TButton",
            command=self.reload,
        ).pack(side="right", padx=(7, 10), pady=8)
        ttk.Button(
            footer,
            text="DEFAULTS",
            style="Secondary.TButton",
            command=self.restore_defaults,
        ).pack(side="right", padx=(7, 0), pady=8)
        ttk.Button(
            footer,
            text="SAVE CONTROLS",
            style="Primary.TButton",
            command=self.save,
        ).pack(side="right", pady=8)

    def _build_controls_page(self, parent: tk.Widget) -> None:
        heading = tk.Frame(parent, bg=BG)
        heading.pack(fill="x", padx=2, pady=(8, 6))
        tk.Label(
            heading,
            text="Controls",
            bg=BG,
            fg=TEXT,
            font=(ui.FONT_DISPLAY, 13),
        ).pack(side="left")
        tk.Label(
            heading,
            text="Select a player, then bind directly on their controller map.",
            bg=BG,
            fg=MUTED,
            font=(ui.FONT, 8),
        ).pack(side="left", padx=(10, 0), pady=(2, 0))

        self.notebook = ttk.Notebook(parent, style="Inner.TNotebook")
        self.notebook.pack(fill="both", expand=True, padx=2)
        self._make_player_tab_diagram("p1", "PLAYER 1")
        self._make_player_tab_diagram("p2", "PLAYER 2")
        self._make_player_tab_diagram("p3", "PLAYER 3")

        footer = tk.Frame(
            parent,
            bg=ui.COLORS["surface"],
            highlightbackground=ui.COLORS["border_soft"],
            highlightthickness=1,
        )
        footer.pack(fill="x", padx=2, pady=(7, 2))
        self.status_var = tk.StringVar(
            value="Controller bindings · saved changes apply on the next start."
        )
        tk.Label(
            footer,
            textvariable=self.status_var,
            bg=ui.COLORS["surface"],
            fg=MUTED,
            font=(ui.FONT, 8),
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=11)
        ttk.Button(
            footer,
            text="RELOAD",
            style="Secondary.TButton",
            command=self.reload,
        ).pack(side="right", padx=(5, 7), pady=5)
        ttk.Button(
            footer,
            text="DEFAULTS",
            style="Secondary.TButton",
            command=self.restore_defaults,
        ).pack(side="right", padx=(5, 0), pady=5)
        ttk.Button(
            footer,
            text="SAVE CONTROLS",
            style="Primary.TButton",
            command=self.save,
        ).pack(side="right", pady=5)

    @staticmethod
    def _load_theme_selection() -> menu_themes.ThemeSelection:
        try:
            return menu_themes.load_selection()
        except (OSError, ValueError):
            return menu_themes.ThemeSelection(
                character_overrides=tuple(
                    sorted(menu_themes.DEFAULT_CHARACTER_THEMES.items())
                )
            )

    @staticmethod
    def _theme_asset_root() -> Path:
        if getattr(sys, "frozen", False):
            return Path(getattr(sys, "_MEIPASS"))
        return Path(__file__).resolve().parents[1]

    @staticmethod
    def _theme_hex(rgb: tuple[int, int, int]) -> str:
        return "#" + "".join(f"{max(0, min(255, value)):02x}" for value in rgb)

    def _theme_key_from_display(self, value: str) -> str:
        for name in menu_themes.PRESETS:
            if theme_display_name(name) == value:
                return name
        return menu_themes.normalize_name(value)

    def _build_themes_page(self, parent: tk.Widget) -> None:
        top = tk.Frame(parent, bg=BG)
        top.pack(fill="x", padx=4, pady=(16, 12))
        title = tk.Frame(top, bg=BG)
        title.pack(side="left", fill="x", expand=True)
        tk.Label(
            title,
            text="Character visual identity",
            bg=BG,
            fg=TEXT,
            font=(ui.FONT_DISPLAY, 18),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            title,
            text=(
                "One preset controls the menu, target circle, target info, "
                "party HUD text and life gauge together."
            ),
            bg=BG,
            fg=MUTED,
            font=(ui.FONT, 9),
            anchor="w",
        ).pack(anchor="w", pady=(3, 0))
        identity_chip = tk.Frame(
            top,
            bg=ui.COLORS["blue_deep"],
            highlightbackground=BLUE,
            highlightthickness=1,
        )
        identity_chip.pack(side="right", padx=(18, 0), pady=(2, 0))
        tk.Label(
            identity_chip,
            text="CHARACTER-OWNED  •  PLAYER-INDEPENDENT",
            bg=ui.COLORS["blue_deep"],
            fg=ui.COLORS["blue_hover"],
            font=(ui.FONT_DISPLAY, 8),
            padx=12,
            pady=8,
        ).pack()

        palette_card = tk.Frame(
            parent,
            bg=PANEL,
            highlightbackground=ui.COLORS["border_soft"],
            highlightthickness=1,
        )
        palette_card.pack(fill="x", padx=4, pady=(0, 12))
        tk.Label(
            palette_card,
            text="AVAILABLE PRESETS",
            bg=PANEL,
            fg=MUTED,
            font=(ui.FONT_DISPLAY, 8),
            anchor="w",
        ).pack(fill="x", padx=14, pady=(10, 7))
        palette_row = tk.Frame(palette_card, bg=PANEL)
        palette_row.pack(fill="x", padx=10, pady=(0, 11))
        for column, name in enumerate(menu_themes.PRESETS):
            theme = menu_themes.get_theme(name)
            tile = tk.Frame(
                palette_row,
                bg=ui.COLORS["surface"],
                highlightbackground=ui.COLORS["border_soft"],
                highlightthickness=1,
            )
            tile.grid(row=0, column=column, sticky="ew", padx=3)
            palette_row.columnconfigure(column, weight=1)
            swatch = tk.Canvas(
                tile,
                width=82,
                height=17,
                bg=ui.COLORS["surface"],
                highlightthickness=0,
            )
            swatch.pack(fill="x", padx=5, pady=(5, 3))
            swatch.create_rectangle(
                0,
                0,
                28,
                17,
                fill=self._theme_hex(theme.panel_dark),
                outline="",
            )
            swatch.create_rectangle(
                28,
                0,
                55,
                17,
                fill=self._theme_hex(theme.accent),
                outline="",
            )
            swatch.create_rectangle(
                55,
                0,
                84,
                17,
                fill=self._theme_hex(theme.highlight),
                outline="",
            )
            tk.Label(
                tile,
                text=theme_display_name(name).upper(),
                bg=ui.COLORS["surface"],
                fg=TEXT,
                font=(ui.FONT_DISPLAY, 7),
            ).pack(padx=4, pady=(0, 5))

        cards = tk.Frame(parent, bg=BG)
        cards.pack(fill="both", expand=True, padx=4)
        cards.columnconfigure(0, weight=1, uniform="character")
        cards.columnconfigure(1, weight=1, uniform="character")
        display_values = tuple(theme_display_name(name) for name in menu_themes.PRESETS)
        for index, profile in enumerate(menu_themes.PLAYABLE_CHARACTER_PROFILES):
            card = tk.Frame(
                cards,
                bg=PANEL,
                highlightbackground=ui.COLORS["border_soft"],
                highlightthickness=1,
            )
            card.grid(
                row=index // 2,
                column=index % 2,
                sticky="nsew",
                padx=(0, 6) if index % 2 == 0 else (6, 0),
                pady=(0, 8),
            )
            cards.rowconfigure(index // 2, weight=1, uniform="character")

            portrait_path = (
                self._theme_asset_root()
                / "assets"
                / "portraits"
                / profile.portrait_filename
            )
            try:
                portrait = Image.open(portrait_path).convert("RGBA")
                portrait.thumbnail((70, 84), Image.Resampling.LANCZOS)
            except OSError:
                portrait = Image.new("RGBA", (64, 80), (0, 0, 0, 0))
            photo = ImageTk.PhotoImage(portrait)
            self.theme_images[profile.member_id] = photo
            portrait_shell = tk.Frame(
                card,
                bg=ui.COLORS["surface"],
                width=86,
                height=100,
                highlightbackground=ui.COLORS["border"],
                highlightthickness=1,
            )
            portrait_shell.pack(side="left", fill="y", padx=8, pady=8)
            portrait_shell.pack_propagate(False)
            tk.Label(
                portrait_shell,
                image=photo,
                bg=ui.COLORS["surface"],
                bd=0,
            ).pack(expand=True)

            content = tk.Frame(card, bg=PANEL)
            content.pack(
                side="left",
                fill="both",
                expand=True,
                padx=(2, 10),
                pady=7,
            )
            heading = tk.Frame(content, bg=PANEL)
            heading.pack(fill="x")
            tk.Label(
                heading,
                text=profile.name,
                bg=PANEL,
                fg=TEXT,
                font=(ui.FONT_DISPLAY, 13),
                anchor="w",
            ).pack(side="left")
            tk.Label(
                heading,
                text=f"MEMBER {profile.member_id:02d}",
                bg=PANEL,
                fg=ui.COLORS["faint"],
                font=(ui.FONT_DISPLAY, 7),
                anchor="e",
            ).pack(side="right", pady=(4, 0))

            selected_name = self.theme_selection.for_character(profile.member_id)
            variable = tk.StringVar(value=theme_display_name(selected_name))
            self.theme_vars[profile.member_id] = variable
            selector = ttk.Combobox(
                content,
                textvariable=variable,
                values=display_values,
                state="readonly",
            )
            selector.pack(fill="x", pady=(5, 5))
            selector.bind(
                "<<ComboboxSelected>>",
                lambda _event, member_id=profile.member_id: self._theme_changed(
                    member_id
                ),
            )

            preview = tk.Canvas(
                content,
                height=18,
                bg=PANEL,
                highlightthickness=0,
            )
            preview.pack(fill="x")
            preview.bind(
                "<Configure>",
                lambda _event, member_id=profile.member_id: self._refresh_theme_preview(
                    member_id
                ),
            )
            tags = tk.Frame(content, bg=PANEL)
            tags.pack(fill="x", pady=(4, 0))
            for label in ("MENU", "HUD", "TARGET", "LIFE"):
                tk.Label(
                    tags,
                    text=label,
                    bg=ui.COLORS["surface"],
                    fg=MUTED,
                    font=(ui.FONT_DISPLAY, 7),
                    padx=6,
                    pady=2,
                ).pack(side="left", padx=(0, 4))
            self.theme_preview_widgets[profile.member_id] = {
                "preview": preview,
                "card": card,
            }
            self._refresh_theme_preview(profile.member_id)

        footer = tk.Frame(
            parent,
            bg=ui.COLORS["surface"],
            highlightbackground=ui.COLORS["border_soft"],
            highlightthickness=1,
        )
        footer.pack(
            side="bottom",
            fill="x",
            padx=4,
            pady=(2, 4),
            before=cards,
        )
        self.theme_status_var = tk.StringVar(
            value=f"Theme profiles: {menu_themes.CONFIG_PATH}"
        )
        tk.Label(
            footer,
            textvariable=self.theme_status_var,
            bg=ui.COLORS["surface"],
            fg=MUTED,
            font=(ui.FONT, 9),
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=14)
        ttk.Button(
            footer,
            text="RELOAD",
            style="Secondary.TButton",
            command=self.reload_themes,
        ).pack(side="right", padx=(7, 10), pady=8)
        ttk.Button(
            footer,
            text="DEFAULT THEMES",
            style="Secondary.TButton",
            command=self.restore_default_themes,
        ).pack(side="right", padx=(7, 0), pady=8)
        ttk.Button(
            footer,
            text="SAVE ALL THEMES",
            style="Gold.TButton",
            command=self.save_themes,
        ).pack(side="right", pady=8)

    def _build_themes_page(self, parent: tk.Widget) -> None:
        heading = tk.Frame(parent, bg=BG)
        heading.pack(fill="x", padx=2, pady=(8, 8))
        heading_text = tk.Frame(heading, bg=BG)
        heading_text.pack(side="left", fill="x", expand=True)
        tk.Label(
            heading_text,
            text="Character themes",
            bg=BG,
            fg=TEXT,
            font=(ui.FONT_DISPLAY, 13),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            heading_text,
            text="Each profile changes menu · cursor · target · HUD · life gauge.",
            bg=BG,
            fg=MUTED,
            font=(ui.FONT, 8),
            anchor="w",
        ).pack(anchor="w", pady=(1, 0))
        tk.Label(
            heading,
            text=f"{len(menu_themes.PRESETS)} PRESETS  ·  HOT RELOAD",
            bg=ui.COLORS["blue_deep"],
            fg=ui.COLORS["blue_hover"],
            font=(ui.FONT_DISPLAY, 8),
            padx=9,
            pady=5,
        ).pack(side="right")

        footer = tk.Frame(
            parent,
            bg=ui.COLORS["surface"],
            highlightbackground=ui.COLORS["border_soft"],
            highlightthickness=1,
        )
        footer.pack(side="bottom", fill="x", padx=2, pady=(7, 2))
        self.theme_status_var = tk.StringVar(
            value="Character profiles · changes are hot-reloaded by the co-op."
        )
        tk.Label(
            footer,
            textvariable=self.theme_status_var,
            bg=ui.COLORS["surface"],
            fg=MUTED,
            font=(ui.FONT, 8),
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=11)
        ttk.Button(
            footer,
            text="RELOAD",
            style="Secondary.TButton",
            command=self.reload_themes,
        ).pack(side="right", padx=(5, 7), pady=5)
        ttk.Button(
            footer,
            text="DEFAULTS",
            style="Secondary.TButton",
            command=self.restore_default_themes,
        ).pack(side="right", padx=(5, 0), pady=5)
        ttk.Button(
            footer,
            text="SAVE THEMES",
            style="Gold.TButton",
            command=self.save_themes,
        ).pack(side="right", pady=5)

        cards = tk.Frame(parent, bg=BG)
        cards.pack(fill="x", padx=2)
        for column in range(3):
            cards.columnconfigure(column, weight=1, uniform="character")
        display_values = tuple(theme_display_name(name) for name in menu_themes.PRESETS)
        for index, profile in enumerate(menu_themes.PLAYABLE_CHARACTER_PROFILES):
            card = tk.Frame(
                cards,
                bg=PANEL,
                highlightbackground=ui.COLORS["border_soft"],
                highlightthickness=1,
            )
            card.grid(
                row=index // 3,
                column=index % 3,
                sticky="ew",
                padx=(
                    (0, 4) if index % 3 == 0 else ((4, 0) if index % 3 == 2 else (4, 4))
                ),
                pady=(0, 8),
            )

            portrait_path = (
                self._theme_asset_root()
                / "assets"
                / "portraits"
                / profile.portrait_filename
            )
            try:
                portrait = Image.open(portrait_path).convert("RGBA")
                portrait.thumbnail((52, 64), Image.Resampling.LANCZOS)
            except OSError:
                portrait = Image.new("RGBA", (48, 60), (0, 0, 0, 0))
            photo = ImageTk.PhotoImage(portrait)
            self.theme_images[profile.member_id] = photo
            portrait_shell = tk.Frame(
                card,
                bg=ui.COLORS["surface"],
                width=64,
                height=78,
                highlightbackground=ui.COLORS["border"],
                highlightthickness=1,
            )
            portrait_shell.pack(side="left", padx=7, pady=7)
            portrait_shell.pack_propagate(False)
            tk.Label(
                portrait_shell,
                image=photo,
                bg=ui.COLORS["surface"],
                bd=0,
            ).pack(expand=True)

            content = tk.Frame(card, bg=PANEL)
            content.pack(
                side="left",
                fill="both",
                expand=True,
                padx=(0, 7),
                pady=7,
            )
            heading_row = tk.Frame(content, bg=PANEL)
            heading_row.pack(fill="x")
            tk.Label(
                heading_row,
                text=profile.name,
                bg=PANEL,
                fg=TEXT,
                font=(ui.FONT_DISPLAY, 11),
                anchor="w",
            ).pack(side="left")
            tk.Label(
                heading_row,
                text=f"#{profile.member_id:02d}",
                bg=PANEL,
                fg=ui.COLORS["faint"],
                font=(ui.FONT_DISPLAY, 7),
            ).pack(side="right", pady=(2, 0))

            selected_name = self.theme_selection.for_character(profile.member_id)
            variable = tk.StringVar(value=theme_display_name(selected_name))
            self.theme_vars[profile.member_id] = variable
            selector = ttk.Combobox(
                content,
                textvariable=variable,
                values=display_values,
                state="readonly",
                font=(ui.FONT, 8),
            )
            selector.pack(fill="x", pady=(5, 4))
            selector.bind(
                "<<ComboboxSelected>>",
                lambda _event, member_id=profile.member_id: self._theme_changed(
                    member_id
                ),
            )

            preview = tk.Canvas(
                content,
                height=10,
                bg=PANEL,
                highlightthickness=0,
            )
            preview.pack(fill="x")
            preview.bind(
                "<Configure>",
                lambda _event, member_id=profile.member_id: self._refresh_theme_preview(
                    member_id
                ),
            )
            self.theme_preview_widgets[profile.member_id] = {
                "preview": preview,
                "card": card,
            }
            self._refresh_theme_preview(profile.member_id)

    def _make_theme_card(
        self,
        grid: tk.Widget,
        profile: menu_themes.CharacterThemeProfile,
        *,
        row: int,
        column: int,
        column_count: int,
        display_values: tuple[str, ...],
    ) -> None:
        card = tk.Frame(
            grid,
            bg=PANEL,
            highlightbackground=ui.COLORS["border_soft"],
            highlightthickness=1,
        )
        card.grid(
            row=row,
            column=column,
            sticky="ew",
            padx=(
                (0, 3)
                if column == 0
                else ((3, 0) if column == column_count - 1 else (3, 3))
            ),
            pady=(0, 6),
        )
        portrait_path = (
            self._theme_asset_root()
            / "assets"
            / "portraits"
            / profile.portrait_filename
        )
        try:
            portrait = Image.open(portrait_path).convert("RGBA")
            portrait.thumbnail((46, 56), Image.Resampling.LANCZOS)
        except OSError:
            portrait = Image.new("RGBA", (44, 54), (0, 0, 0, 0))
        photo = ImageTk.PhotoImage(portrait)
        self.theme_images[profile.member_id] = photo
        portrait_shell = tk.Frame(
            card,
            bg=ui.COLORS["surface"],
            width=56,
            height=68,
            highlightbackground=ui.COLORS["border"],
            highlightthickness=1,
        )
        portrait_shell.pack(side="left", padx=6, pady=6)
        portrait_shell.pack_propagate(False)
        tk.Label(
            portrait_shell,
            image=photo,
            bg=ui.COLORS["surface"],
            bd=0,
        ).pack(expand=True)

        content = tk.Frame(card, bg=PANEL)
        content.pack(
            side="left",
            fill="both",
            expand=True,
            padx=(0, 6),
            pady=6,
        )
        tk.Label(
            content,
            text=profile.name,
            bg=PANEL,
            fg=TEXT,
            font=(ui.FONT_DISPLAY, 10),
            anchor="w",
        ).pack(fill="x")
        selected_name = self.theme_selection.for_character(profile.member_id)
        variable = tk.StringVar(value=theme_display_name(selected_name))
        self.theme_vars[profile.member_id] = variable
        selector = ttk.Combobox(
            content,
            textvariable=variable,
            values=display_values,
            state="readonly",
            font=(ui.FONT, 8),
        )
        selector.pack(fill="x", pady=(4, 3))
        selector.bind(
            "<<ComboboxSelected>>",
            lambda _event, member_id=profile.member_id: self._theme_changed(member_id),
        )
        preview = tk.Canvas(
            content,
            height=8,
            bg=PANEL,
            highlightthickness=0,
        )
        preview.pack(fill="x")
        preview.bind(
            "<Configure>",
            lambda _event, member_id=profile.member_id: self._refresh_theme_preview(
                member_id
            ),
        )
        if profile.member_id in coop_roster.SELECTABLE_MEMBER_IDS:
            self._make_roster_row(content, profile.member_id)
        self.theme_preview_widgets[profile.member_id] = {
            "preview": preview,
            "card": card,
        }
        self._refresh_theme_preview(profile.member_id)

    def _make_roster_row(self, content: tk.Widget, member_id: int) -> None:
        """Two mutually exclusive seat checkboxes for one character."""

        row = tk.Frame(content, bg=PANEL)
        row.pack(fill="x", pady=(5, 0))
        tk.Label(
            row,
            text="SEAT",
            bg=PANEL,
            fg=MUTED,
            font=(ui.FONT_DISPLAY, 7),
        ).pack(side="left", padx=(0, 6))
        for role, label in (("p2", "P2"), ("p3", "P3")):
            variable = tk.BooleanVar(value=self.coop_roster.role_of(member_id) == role)
            self.roster_vars[(member_id, role)] = variable
            ttk.Checkbutton(
                row,
                text=label,
                variable=variable,
                style="Toggle.TCheckbutton",
                command=(lambda m=member_id, r=role: self._roster_toggled(m, r)),
            ).pack(side="left", padx=(0, 8))

    def _roster_toggled(self, member_id: int, role: str) -> None:
        """Give this seat to this character, or release it when unchecked."""

        variable = self.roster_vars[(member_id, role)]
        if variable.get():
            self.coop_roster = self.coop_roster.assign(role, member_id)
        elif self.coop_roster.role_of(member_id) == role:
            self.coop_roster = self.coop_roster.assign(role, None)
        self._refresh_roster_boxes()

    def _refresh_roster_boxes(self) -> None:
        """Redraw every box from the roster so the two seats stay disjoint."""

        for (member_id, role), variable in self.roster_vars.items():
            variable.set(self.coop_roster.role_of(member_id) == role)
        if self.roster_status_var is not None:
            self.roster_status_var.set(self._roster_summary())

    def _roster_summary(self) -> str:
        parts = []
        for role, label in (("p2", "P2"), ("p3", "P3")):
            member_id = getattr(self.coop_roster, role)
            name = (
                coop_roster.MEMBER_NAMES.get(member_id, "?")
                if member_id is not None
                else "empty"
            )
            parts.append(f"{label} {name}")
        return "Joins Vaan on a new game:  " + "  ·  ".join(parts)

    def _build_themes_page(self, parent: tk.Widget) -> None:
        """Build clearly separated main-party and guest theme sections."""

        heading = tk.Frame(parent, bg=BG)
        heading.pack(fill="x", padx=2, pady=(8, 7))
        tk.Label(
            heading,
            text="Character themes",
            bg=BG,
            fg=TEXT,
            font=(ui.FONT_DISPLAY, 13),
        ).pack(side="left")
        tk.Label(
            heading,
            text="One profile controls menu · cursor · target · HUD · life.",
            bg=BG,
            fg=MUTED,
            font=(ui.FONT, 8),
        ).pack(side="left", padx=(10, 0), pady=(2, 0))
        tk.Label(
            heading,
            text=f"{len(menu_themes.PRESETS)} PRESETS",
            bg=ui.COLORS["blue_deep"],
            fg=ui.COLORS["blue_hover"],
            font=(ui.FONT_DISPLAY, 8),
            padx=8,
            pady=4,
        ).pack(side="right")

        footer = tk.Frame(
            parent,
            bg=ui.COLORS["surface"],
            highlightbackground=ui.COLORS["border_soft"],
            highlightthickness=1,
        )
        footer.pack(side="bottom", fill="x", padx=2, pady=(7, 2))
        self.theme_status_var = tk.StringVar(
            value="Character profiles · hot reload enabled."
        )
        tk.Label(
            footer,
            textvariable=self.theme_status_var,
            bg=ui.COLORS["surface"],
            fg=MUTED,
            font=(ui.FONT, 8),
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=10)
        ttk.Button(
            footer,
            text="RELOAD",
            style="Secondary.TButton",
            command=self.reload_themes,
        ).pack(side="right", padx=(5, 7), pady=5)
        ttk.Button(
            footer,
            text="DEFAULTS",
            style="Secondary.TButton",
            command=self.restore_default_themes,
        ).pack(side="right", padx=(5, 0), pady=5)
        ttk.Button(
            footer,
            text="SAVE THEMES",
            style="Gold.TButton",
            command=self.save_themes,
        ).pack(side="right", pady=5)

        display_values = tuple(theme_display_name(name) for name in menu_themes.PRESETS)
        body = tk.Frame(parent, bg=BG)
        body.pack(fill="x", padx=2)

        main_heading = tk.Frame(body, bg=BG)
        main_heading.pack(fill="x", pady=(0, 5))
        tk.Label(
            main_heading,
            text="MAIN PARTY",
            bg=BG,
            fg=MUTED,
            font=(ui.FONT_DISPLAY, 8),
            anchor="w",
        ).pack(side="left")
        self.roster_status_var = tk.StringVar(value=self._roster_summary())
        tk.Label(
            main_heading,
            textvariable=self.roster_status_var,
            bg=BG,
            fg=ui.COLORS["faint"],
            font=(ui.FONT, 8),
        ).pack(side="left", padx=(10, 0))
        tk.Label(
            body,
            text=(
                "P2 / P3 pick who joins Vaan when the story first forms the "
                "party. One seat each; taking a seat frees it elsewhere. "
                "Vaan is always P1, and this does not change a save already "
                "in progress."
            ),
            bg=BG,
            fg=ui.COLORS["faint"],
            font=(ui.FONT, 8),
            anchor="w",
            justify="left",
            wraplength=760,
        ).pack(fill="x", pady=(0, 6))
        main_grid = tk.Frame(body, bg=BG)
        main_grid.pack(fill="x")
        for column in range(3):
            main_grid.columnconfigure(column, weight=1, uniform="main")
        for index, profile in enumerate(menu_themes.MAIN_CHARACTER_PROFILES):
            self._make_theme_card(
                main_grid,
                profile,
                row=index // 3,
                column=index % 3,
                column_count=3,
                display_values=display_values,
            )

        guest_heading = tk.Frame(body, bg=BG)
        guest_heading.pack(fill="x", pady=(5, 5))
        tk.Label(
            guest_heading,
            text="GUESTS",
            bg=BG,
            fg=MUTED,
            font=(ui.FONT_DISPLAY, 8),
            anchor="w",
        ).pack(side="left")
        tk.Label(
            guest_heading,
            text="Lamont automatically uses Larsa's profile.",
            bg=BG,
            fg=ui.COLORS["faint"],
            font=(ui.FONT, 8),
        ).pack(side="left", padx=(9, 0))
        guest_grid = tk.Frame(body, bg=BG)
        guest_grid.pack(fill="x")
        for column in range(4):
            guest_grid.columnconfigure(column, weight=1, uniform="guest")
        for index, profile in enumerate(menu_themes.GUEST_CHARACTER_PROFILES):
            self._make_theme_card(
                guest_grid,
                profile,
                row=0,
                column=index,
                column_count=4,
                display_values=display_values,
            )

    def _refresh_theme_preview(self, member_id: int) -> None:
        widgets = self.theme_preview_widgets.get(member_id)
        variable = self.theme_vars.get(member_id)
        if not widgets or variable is None:
            return
        name = self._theme_key_from_display(variable.get())
        theme = menu_themes.get_theme(name)
        preview = widgets["preview"]
        if not isinstance(preview, tk.Canvas):
            return
        preview.delete("all")
        width = max(120, preview.winfo_width())
        stops = (
            theme.panel_dark,
            theme.panel_light,
            theme.accent,
            theme.highlight,
        )
        segment = width / len(stops)
        for index, color in enumerate(stops):
            preview.create_rectangle(
                round(index * segment),
                0,
                round((index + 1) * segment),
                8,
                fill=self._theme_hex(color),
                outline="",
            )

    def _theme_changed(self, member_id: int) -> None:
        self.theme_dirty = True
        self._refresh_theme_preview(member_id)
        profile = next(
            profile
            for profile in menu_themes.PLAYABLE_CHARACTER_PROFILES
            if profile.member_id == member_id
        )
        theme_name = self._theme_key_from_display(self.theme_vars[member_id].get())
        self.theme_status_var.set(
            f"{profile.name}: {theme_display_name(theme_name)} "
            "selected for menu, HUD, target and life gauge. Save to apply."
        )

    def save_themes(self) -> None:
        themes = {
            member_id: self._theme_key_from_display(variable.get())
            for member_id, variable in self.theme_vars.items()
        }
        try:
            self.theme_selection = save_character_themes(themes)
            self.coop_roster = coop_roster.save(self.coop_roster)
        except OSError as error:
            messagebox.showerror("Could not save themes", str(error), parent=self.root)
            return
        self._refresh_roster_boxes()
        self.theme_dirty = False
        self.theme_status_var.set(
            "Themes saved and hot-reloaded. "
            + self._roster_summary()
            + " (applies to a new game)."
        )

    def reload_themes(self) -> None:
        self.theme_selection = self._load_theme_selection()
        self.coop_roster = coop_roster.load()
        self._refresh_roster_boxes()
        for profile in menu_themes.PLAYABLE_CHARACTER_PROFILES:
            variable = self.theme_vars.get(profile.member_id)
            if variable is None:
                continue
            variable.set(
                theme_display_name(
                    self.theme_selection.for_character(profile.member_id)
                )
            )
            self._refresh_theme_preview(profile.member_id)
        self.theme_dirty = False
        self.theme_status_var.set("Character themes reloaded from disk.")

    def restore_default_themes(self) -> None:
        if not messagebox.askyesno(
            "Restore default themes",
            "Restore the six default character themes?",
            parent=self.root,
        ):
            return
        for profile in menu_themes.PLAYABLE_CHARACTER_PROFILES:
            self.theme_vars[profile.member_id].set(
                theme_display_name(profile.default_theme)
            )
            self._refresh_theme_preview(profile.member_id)
        self.theme_dirty = True
        self.theme_status_var.set(
            "Default character themes loaded. Save all themes to apply."
        )

    def _make_player_tab(self, player: str, title: str) -> None:
        outer = ttk.Frame(self.notebook, style="Panel.TFrame")
        self.notebook.add(outer, text=title)
        note = (
            "Used for the leader menu while the co-op interface is active."
            if player == "p1"
            else "Always used by Penelo: movement, independent battle menu and interactions."
        )
        accent = BLUE if player == "p1" else (YELLOW if player == "p2" else ACTIVE)
        tk.Label(
            outer,
            text=note,
            bg=PANEL,
            fg=accent,
            font=(ui.FONT, 10),
            anchor="w",
            justify="left",
            wraplength=1040,
        ).pack(fill="x", padx=20, pady=(16, 10))
        assignment = tk.Frame(
            outer,
            bg=PANEL_2,
            highlightbackground=accent,
            highlightthickness=1,
        )
        assignment.pack(fill="x", padx=18, pady=(0, 10))
        variable = tk.StringVar()
        self.assignment_vars[player] = variable
        tk.Label(
            assignment,
            textvariable=variable,
            bg=PANEL_2,
            fg=TEXT,
            font=(ui.FONT_DISPLAY, 10),
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=16, pady=12)
        ttk.Button(
            assignment,
            text="ASSIGN — PRESS START",
            style=("P1Assign.TButton" if player == "p1" else "P2Assign.TButton"),
            command=lambda p=player: self.start_assignment(p),
        ).pack(side="right", padx=10, pady=8)
        tk.Label(
            outer,
            text=(
                "Click a slot and press a keyboard key or controller input. "
                "Right-click any slot to clear it."
            ),
            bg=PANEL,
            fg=MUTED,
            font=(ui.FONT, 9),
            anchor="w",
        ).pack(fill="x", padx=20, pady=(0, 9))

        canvas = tk.Canvas(outer, bg=PANEL, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        body = ttk.Frame(canvas, style="Panel.TFrame")
        body.bind(
            "<Configure>", lambda _e, c=canvas: c.configure(scrollregion=c.bbox("all"))
        )
        window = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.bind(
            "<Configure>",
            lambda e, c=canvas, w=window: c.itemconfigure(w, width=e.width),
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=(0, 10))

        tk.Label(
            body,
            text="ACTION",
            bg=PANEL,
            fg=MUTED,
            font=(ui.FONT_DISPLAY, 8),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=12, pady=8)
        for slot in range(controls.MAX_BINDINGS_PER_ACTION):
            tk.Label(
                body,
                text=f"BINDING {slot + 1}",
                bg=PANEL,
                fg=MUTED,
                font=(ui.FONT_DISPLAY, 8),
            ).grid(row=0, column=slot + 1, sticky="ew", padx=5, pady=8)
        body.columnconfigure(0, weight=2)
        for column in range(1, controls.MAX_BINDINGS_PER_ACTION + 1):
            body.columnconfigure(column, weight=3)
        for row, (action, label) in enumerate(controls.PLAYER_ACTIONS[player], start=1):
            tk.Label(
                body, text=label, bg=PANEL, fg=TEXT, font=(ui.FONT, 10), anchor="w"
            ).grid(row=row, column=0, sticky="ew", padx=12, pady=5)
            for slot in range(controls.MAX_BINDINGS_PER_ACTION):
                button = ttk.Button(
                    body,
                    style="Bind.TButton",
                    command=lambda p=player, a=action, s=slot: self.start_capture(
                        p, a, s
                    ),
                )
                button.grid(row=row, column=slot + 1, sticky="ew", padx=5, pady=4)
                button.bind(
                    "<Button-3>",
                    lambda _e, p=player, a=action, s=slot: self.clear_binding(p, a, s),
                )
                self.binding_buttons[(player, action, slot)] = button
        self._refresh_buttons()

    def _make_player_tab_modern(self, player: str, title: str) -> None:
        outer = ttk.Frame(self.notebook, style="Card.TFrame")
        self.notebook.add(outer, text=title)
        accent = BLUE if player == "p1" else YELLOW
        role = (
            "PRIMARY ROLE / PLAYER 1"
            if player == "p1"
            else (
                "CO-OP ROLE / PLAYER 2" if player == "p2" else "CO-OP ROLE / PLAYER 3"
            )
        )
        note = (
            "Follows whichever character currently belongs to Player 1. "
            "These bindings control the co-op interface."
            if player == "p1"
            else (
                "Follows whichever character Player 2 selected with Z: "
                "movement, menus, targeting and interactions."
                if player == "p2"
                else "Controls the configured third party member: movement, "
                "independent menu and targeting."
            )
        )

        intro = tk.Frame(outer, bg=PANEL)
        intro.pack(fill="x", padx=10, pady=(8, 5))
        tk.Label(
            intro,
            text=role,
            bg=PANEL,
            fg=accent,
            font=(ui.FONT_DISPLAY, 9),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            intro,
            text=note,
            bg=PANEL,
            fg=MUTED,
            font=(ui.FONT, 8),
            anchor="w",
            justify="left",
            wraplength=1040,
        ).pack(fill="x", pady=(1, 0))

        assignment = tk.Frame(
            outer,
            bg=ui.COLORS["surface"],
            highlightbackground=accent,
            highlightthickness=1,
        )
        assignment.pack(fill="x", padx=10, pady=(0, 6))
        assignment_text = tk.Frame(assignment, bg=ui.COLORS["surface"])
        assignment_text.pack(side="left", fill="x", expand=True, padx=10, pady=6)
        tk.Label(
            assignment_text,
            text="ASSIGNED DEVICE",
            bg=ui.COLORS["surface"],
            fg=MUTED,
            font=(ui.FONT_DISPLAY, 8),
            anchor="w",
        ).pack(anchor="w")
        variable = tk.StringVar()
        self.assignment_vars[player] = variable
        tk.Label(
            assignment_text,
            textvariable=variable,
            bg=ui.COLORS["surface"],
            fg=TEXT,
            font=(ui.FONT_DISPLAY, 9),
            anchor="w",
        ).pack(anchor="w", pady=(1, 0))
        ttk.Button(
            assignment,
            text="ASSIGN — PRESS START",
            style=(
                "P1Assign.TButton"
                if player == "p1"
                else ("P2Assign.TButton" if player == "p2" else "P3Assign.TButton")
            ),
            command=lambda p=player: self.start_assignment(p),
        ).pack(side="right", padx=8, pady=6)

        hint = tk.Frame(outer, bg=PANEL)
        hint.pack(fill="x", padx=10, pady=(0, 5))
        tk.Label(
            hint,
            text="Click a binding slot, then press a keyboard key or controller input.",
            bg=PANEL,
            fg=MUTED,
            font=(ui.FONT, 8),
            anchor="w",
        ).pack(side="left")
        tk.Label(
            hint,
            text="Right-click clears a slot",
            bg=PANEL,
            fg=ui.COLORS["faint"],
            font=(ui.FONT, 8),
            anchor="e",
        ).pack(side="right")

        scroller = tk.Frame(outer, bg=PANEL)
        scroller.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        canvas = tk.Canvas(
            scroller,
            bg=PANEL,
            highlightthickness=0,
            borderwidth=0,
        )
        scrollbar = ttk.Scrollbar(scroller, orient="vertical", command=canvas.yview)
        body = tk.Frame(canvas, bg=PANEL)
        body.bind(
            "<Configure>",
            lambda _event, c=canvas: c.configure(scrollregion=c.bbox("all")),
        )
        window = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.bind(
            "<Configure>",
            lambda event, c=canvas, w=window: c.itemconfigure(w, width=event.width),
        )
        canvas.bind(
            "<MouseWheel>",
            lambda event, c=canvas: c.yview_scroll(int(-event.delta / 120), "units"),
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        header = tk.Frame(body, bg=ui.COLORS["surface"])
        header.pack(fill="x", padx=5, pady=(0, 2))
        tk.Label(
            header,
            text="ACTION",
            bg=ui.COLORS["surface"],
            fg=MUTED,
            font=(ui.FONT_DISPLAY, 8),
            width=19,
            anchor="w",
            padx=8,
            pady=5,
        ).pack(side="left")
        for slot in range(controls.MAX_BINDINGS_PER_ACTION):
            tk.Label(
                header,
                text=f"BINDING {slot + 1}",
                bg=ui.COLORS["surface"],
                fg=MUTED,
                font=(ui.FONT_DISPLAY, 8),
                anchor="center",
                pady=5,
            ).pack(side="left", fill="x", expand=True, padx=3)

        for row_index, (action, label) in enumerate(controls.PLAYER_ACTIONS[player]):
            row_bg = ui.COLORS["card_alt"] if row_index % 2 == 0 else ui.COLORS["card"]
            row = tk.Frame(body, bg=row_bg)
            row.pack(fill="x", padx=5, pady=1)
            tk.Label(
                row,
                text=label,
                bg=row_bg,
                fg=TEXT,
                font=(ui.FONT, 9),
                width=19,
                anchor="w",
                padx=8,
                pady=4,
            ).pack(side="left")
            for slot in range(controls.MAX_BINDINGS_PER_ACTION):
                button = ttk.Button(
                    row,
                    style="Bind.TButton",
                    command=lambda p=player, a=action, s=slot: self.start_capture(
                        p, a, s
                    ),
                )
                button.pack(
                    side="left",
                    fill="x",
                    expand=True,
                    padx=3,
                    pady=2,
                )
                button.bind(
                    "<Button-3>",
                    lambda _event, p=player, a=action, s=slot: self.clear_binding(
                        p, a, s
                    ),
                )
                self.binding_buttons[(player, action, slot)] = button
        self._refresh_buttons()

    def _make_player_tab_diagram(self, player: str, title: str) -> None:
        outer = ttk.Frame(self.notebook, style="Card.TFrame")
        self.notebook.add(outer, text=title)
        accent = BLUE if player == "p1" else (YELLOW if player == "p2" else ACTIVE)
        role = (
            "Player 1 follows the current leader"
            if player == "p1"
            else (
                "Player 2 follows the character selected with Z"
                if player == "p2"
                else "Player 3 follows the configured third character"
            )
        )

        assignment = tk.Frame(
            outer,
            bg=ui.COLORS["surface"],
            highlightbackground=accent,
            highlightthickness=1,
        )
        assignment.pack(fill="x", padx=6, pady=(6, 4))
        tk.Label(
            assignment,
            text=f"{player.upper()}  /  {role}",
            bg=ui.COLORS["surface"],
            fg=accent,
            font=(ui.FONT_DISPLAY, 8),
            anchor="w",
        ).pack(side="left", padx=(9, 12), pady=7)
        variable = tk.StringVar()
        self.assignment_vars[player] = variable
        tk.Label(
            assignment,
            textvariable=variable,
            bg=ui.COLORS["surface"],
            fg=TEXT,
            font=(ui.FONT, 8),
            anchor="w",
        ).pack(side="left", fill="x", expand=True, pady=7)
        ttk.Button(
            assignment,
            text="ASSIGN - PRESS START",
            style=(
                "P1Assign.TButton"
                if player == "p1"
                else ("P2Assign.TButton" if player == "p2" else "P3Assign.TButton")
            ),
            command=lambda p=player: self.start_assignment(p),
        ).pack(side="right", padx=6, pady=4)

        hint = tk.Frame(outer, bg=PANEL)
        hint.pack(fill="x", padx=8, pady=(0, 3))
        tk.Label(
            hint,
            text=("Click any field, then press a controller input or keyboard key."),
            bg=PANEL,
            fg=MUTED,
            font=(ui.FONT, 7),
        ).pack(side="left")
        tk.Label(
            hint,
            text="Right-click clears  /  three inputs may share one action",
            bg=PANEL,
            fg=ui.COLORS["faint"],
            font=(ui.FONT, 7),
        ).pack(side="right")

        diagram = PlayerBindingDiagram(outer, self, player)
        diagram.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.player_diagrams[player] = diagram
        self._refresh_buttons()

    def _refresh_assignments(self) -> None:
        for player, variable in self.assignment_vars.items():
            index = self.config.get("devices", {}).get(player)
            if isinstance(index, int):
                name = self.controller_names.get(
                    index, f"XInput Controller {index + 1}"
                )
                variable.set(
                    f"{name}  [XInput {index + 1}] owns {player.upper()} globally"
                )
            else:
                variable.set(f"No controller assigned to {player.upper()}")

    def _bindings(self, player: str, action: str):
        return self.config["players"][player][action]

    def _refresh_buttons(self) -> None:
        for (player, action, slot), button in self.binding_buttons.items():
            bindings = self._bindings(player, action)
            binding = bindings[slot] if slot < len(bindings) else None
            assigned = self.config.get("devices", {}).get(player)
            is_diagram = bool(getattr(button, "_diagram_binding", False))
            button.configure(
                text=(
                    compact_binding_label(binding, assigned)
                    if is_diagram
                    else full_binding_label(binding, assigned)
                ),
                style=(
                    "BindMini.TButton"
                    if binding and is_diagram
                    else (
                        "BindMiniEmpty.TButton"
                        if is_diagram
                        else ("Bind.TButton" if binding else "BindEmpty.TButton")
                    )
                ),
            )
        self._refresh_assignments()

    def start_assignment(self, player: str) -> None:
        self.capture_target = None
        self.assign_target = player
        self.states = controls.read_xinput_states()
        self.capture_baseline = self._active_pad_controls(self.states, threshold=0.55)
        self.status_var.set(
            f"Waiting for {player.upper()}: press START on the controller that should own this player."
        )

    def assign_controller(self, player: str, index: int) -> None:
        previous_owners = [
            role
            for role in ("p1", "p2", "p3")
            if role != player and self.config["devices"].get(role) == index
        ]
        self.config["devices"][player] = index
        for role in previous_owners:
            self.config["devices"][role] = None
        for binding_list in self.config["players"][player].values():
            for binding in binding_list:
                if binding.get("device") == "xinput":
                    binding["index"] = index
        self.assign_target = None
        self.capture_baseline.clear()
        suffix = (
            "; "
            + ", ".join(role.upper() for role in previous_owners)
            + " unassigned to prevent interference"
            if previous_owners
            else ""
        )
        self.status_var.set(
            f"{self.controller_names.get(index, f'Controller {index + 1}')} "
            f"assigned exclusively to {player.upper()}{suffix}. Press Save and apply."
        )
        self._refresh_buttons()

    def start_capture(self, player: str, action: str, slot: int) -> None:
        self.assign_target = None
        self.capture_target = (player, action, slot)
        self.states = controls.read_xinput_states()
        self.capture_baseline = self._active_pad_controls(self.states, threshold=0.55)
        capture_button = self.binding_buttons[self.capture_target]
        capture_button.configure(
            text=(
                "PRESS..."
                if getattr(capture_button, "_diagram_binding", False)
                else "Press a key or controller input…"
            )
        )
        self.status_var.set(
            "Listening… press Esc normally if you want to bind Esc; right-click clears a slot."
        )
        self.root.focus_force()

    def _set_binding(self, binding) -> None:
        if self.capture_target is None:
            return
        player, action, slot = self.capture_target
        if binding.get("device") == "xinput":
            assigned = self.config.get("devices", {}).get(player)
            if not isinstance(assigned, int):
                self.assign_controller(player, int(binding["index"]))
                assigned = int(binding["index"])
            binding["index"] = assigned
        bindings = self._bindings(player, action)
        if slot < len(bindings):
            bindings[slot] = binding
        else:
            bindings.append(binding)
        # A duplicate in the same action is useless; keep the newly captured slot.
        seen = set()
        unique = []
        for value in bindings:
            signature = tuple(sorted(value.items()))
            if signature not in seen:
                seen.add(signature)
                unique.append(value)
        self.config["players"][player][action] = unique[
            : controls.MAX_BINDINGS_PER_ACTION
        ]
        self.capture_target = None
        self.assign_target = None
        self.capture_baseline.clear()
        self.status_var.set("Binding changed. Press Save and apply.")
        self._refresh_buttons()

    def clear_binding(self, player: str, action: str, slot: int) -> None:
        bindings = self._bindings(player, action)
        if slot < len(bindings):
            bindings.pop(slot)
        self.capture_target = None
        self.status_var.set("Binding cleared. Press Save and apply.")
        self._refresh_buttons()

    def _key_pressed(self, event) -> str | None:
        if self.capture_target is None:
            return None
        vk = int(event.keycode)
        if 0 < vk < 256:
            self._set_binding(controls.key_binding(vk))
            return "break"
        return None

    @staticmethod
    def _active_pad_controls(states, threshold=0.72):
        active = set()
        names = tuple(controls.XINPUT_BUTTONS) + controls.XINPUT_AXIS_CONTROLS
        for index, state in enumerate(states):
            for control in names:
                if controls.xinput_control_strength(state, control) >= threshold:
                    active.add((index, control))
        return active

    def _poll_controllers(self) -> None:
        self.states = controls.read_xinput_states()
        for diagram in self.player_diagrams.values():
            diagram.update_live(self.states, self.controller_names)
        if self.assign_target is not None:
            active = self._active_pad_controls(self.states)
            new_inputs = active - self.capture_baseline
            starts = sorted(
                (index, control) for index, control in new_inputs if control == "START"
            )
            if starts:
                self.assign_controller(self.assign_target, starts[0][0])
            else:
                self.capture_baseline &= active
        elif self.capture_target is not None:
            active = self._active_pad_controls(self.states)
            new_inputs = sorted(active - self.capture_baseline)
            if new_inputs:
                index, control = new_inputs[0]
                self._set_binding(controls.pad_binding(index, control))
            else:
                self.capture_baseline &= active
        self.root.after(30, self._poll_controllers)

    def save(self) -> None:
        try:
            controls.save_config(self.config)
        except controls.UnsupportedControllerConfigVersion as error:
            self.reload()
            messagebox.showerror(
                "Read-only control configuration",
                "This control file was created by a newer mod version "
                f"({error.source_version}; supported: {error.supported_version}). "
                "It was preserved without changes.",
                parent=self.root,
            )
            return
        except OSError as error:
            messagebox.showerror("Could not save", str(error), parent=self.root)
            return
        self.config, self.controller_config_report = controls.load_config_with_report()
        self._refresh_buttons()
        self.status_var.set(
            "Saved. The running co-op will reload these bindings automatically."
        )

    def reload(self) -> None:
        self.config, self.controller_config_report = controls.load_config_with_report()
        self.capture_target = None
        self.assign_target = None
        self._refresh_buttons()
        self.status_var.set("Configuration reloaded from disk.")

    def restore_defaults(self) -> None:
        if self.controller_config_report.incompatible_future:
            messagebox.showerror(
                "Read-only control configuration",
                "This control file was created by a newer mod version and was "
                "preserved without changes.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "Restore defaults",
            "Restore all Player 1, Player 2 and Player 3 bindings?",
            parent=self.root,
        ):
            return
        self.config = controls.default_config()
        self.capture_target = None
        self.assign_target = None
        self._refresh_buttons()
        self.status_var.set("Defaults loaded. Press Save and apply to write them.")

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    Configurator().run()


if __name__ == "__main__":
    main()
