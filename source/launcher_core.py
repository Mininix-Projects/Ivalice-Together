from __future__ import annotations

import ctypes as ct
import json
import os
import sys
import threading
import time
from ctypes import wintypes as wt
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Iterable

import atomic_files
import controller_bindings as controls
import coop_roster
import game_process
import runtime_paths
import runtime_status

GAME_EXECUTABLE = "FFXII_TZA.exe"
AUTOSTART_MODULE_SHA256 = (
    "493e5dc5c865cc5969a948318086b6e434f9b0b8eb5a84abb1a4a8ddbb328ba0"
)
RUNTIME_MUTEX_PREFIX = "Local\\FF12LocalCoopHeadlessLauncher_"
LAUNCHER_MUTEX_NAME = "Local\\FF12TZA_LocalCoopLauncher"
SYNCHRONIZE = 0x00100000
ERROR_FILE_NOT_FOUND = 2
ERROR_ALREADY_EXISTS = 183
DAMAGE_NUMBER_LOCALES = ("us", "fr", "de", "es", "it", "kr", "cn", "ch", "in")
DAMAGE_NUMBER_OVERRIDE_PATHS = tuple(
    Path(
        "ps2data/image/ff12/myoshiok/"
        f"{locale}/packfiles/tex2pack_ys.bin.dir/section_000.tm2"
    )
    for locale in DAMAGE_NUMBER_LOCALES
)
GAME_OVERRIDE_PATHS = (
    Path("gamedata/d3d11/artdata/menu/targetline_p_clut_2.dds.phyre"),
    Path("gamedata/d3d11/artdata/menu/targetline_p_clut_6.dds.phyre"),
    *DAMAGE_NUMBER_OVERRIDE_PATHS,
)
LAUNCHER_VERSION = "1.0"
LAUNCHER_CONFIG_VERSION = 6
SUPPORTED_LANGUAGES = frozenset(("en", "pt"))
DAMAGE_METER_MODES = frozenset(("area", "indefinite"))
DEFAULT_LAUNCHER_CONFIG: dict[str, Any] = {
    "version": LAUNCHER_CONFIG_VERSION,
    "language": "en",
    "menu_sounds": True,
    "battle_menu_sounds": {"p1": True, "p2": True, "p3": True},
    "sound_volume": 1.0,
    "minimize_when_ready": True,
    "damage_meter_enabled": False,
    "damage_meter_mode": "area",
    "damage_meter_effective": False,
    "damage_meter_session_id": 1,
}
CONTROLLER_ALIAS_CAPTURE_WINDOW = 0.75


def game_root() -> Path:
    return runtime_paths.resolve_game_root(__file__)


def coop_directory() -> Path:
    return runtime_paths.resolve_config_directory(__file__)


def bundled_asset(filename: str) -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundle_root:
        bundled = Path(bundle_root) / "launcher_assets" / filename
        if bundled.is_file():
            return bundled
    return Path(__file__).resolve().parent / "launcher_assets" / filename


def bundled_portrait(filename: str) -> Path:
    safe_name = Path(str(filename)).name
    bundle_root = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundle_root:
        bundled = Path(bundle_root) / "assets" / "portraits" / safe_name
        if bundled.is_file():
            return bundled
    return Path(__file__).resolve().parent / "assets" / "portraits" / safe_name


def game_override_bundle_root() -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundle_root:
        return Path(bundle_root) / "game_overrides"
    return Path(__file__).resolve().parent / "game_overrides"


def ensure_runtime_storage(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / f".ff12coop-write-{os.getpid()}.tmp"
    try:
        probe.write_bytes(b"ok")
    except OSError as error:
        raise PermissionError(
            "O configurador não consegue gravar as configurações. "
            "Confira as permissões da pasta do mod."
        ) from error
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def launcher_config_path() -> Path:
    return coop_directory() / "launcher_config.json"


def normalize_launcher_config(payload: Any) -> dict[str, Any]:
    result = dict(DEFAULT_LAUNCHER_CONFIG)
    if not isinstance(payload, dict):
        return result
    language = payload.get("language")
    if isinstance(language, str) and language in SUPPORTED_LANGUAGES:
        result["language"] = language
    result["menu_sounds"] = bool(payload.get("menu_sounds", True))
    configured_battle_sounds = payload.get("battle_menu_sounds")
    if not isinstance(configured_battle_sounds, dict):
        configured_battle_sounds = {}
    result["battle_menu_sounds"] = {
        player: bool(configured_battle_sounds.get(player, True))
        for player in ("p1", "p2", "p3")
    }
    result["minimize_when_ready"] = bool(payload.get("minimize_when_ready", True))
    result["damage_meter_enabled"] = bool(payload.get("damage_meter_enabled", False))
    damage_meter_mode = str(payload.get("damage_meter_mode", "area")).strip().lower()
    result["damage_meter_mode"] = (
        damage_meter_mode if damage_meter_mode in DAMAGE_METER_MODES else "area"
    )
    result["damage_meter_effective"] = bool(
        payload.get("damage_meter_effective", False)
    )
    try:
        session_id = int(payload.get("damage_meter_session_id", 1))
    except (TypeError, ValueError):
        session_id = 1
    result["damage_meter_session_id"] = min(0x7FFFFFFF, max(1, session_id))
    try:
        volume = float(payload.get("sound_volume", result["sound_volume"]))
    except (TypeError, ValueError):
        volume = result["sound_volume"]
    result["sound_volume"] = min(1.0, max(0.0, volume))
    return result


def load_launcher_config(path: Path | None = None) -> dict[str, Any]:
    target = launcher_config_path() if path is None else path
    try:
        return normalize_launcher_config(json.loads(target.read_text(encoding="utf-8")))
    except (OSError, TypeError, ValueError):
        return dict(DEFAULT_LAUNCHER_CONFIG)


def save_launcher_config(
    config: dict[str, Any], path: Path | None = None
) -> dict[str, Any]:
    target = launcher_config_path() if path is None else path
    normalized = normalize_launcher_config(config)
    normalized["version"] = LAUNCHER_CONFIG_VERSION
    atomic_files.write_text(
        target,
        json.dumps(normalized, indent=2, ensure_ascii=False) + "\n",
    )
    return normalized


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
    binding: dict[str, Any] | None,
    assigned_device: int | dict[str, Any] | None = None,
) -> str:
    if not binding:
        return "+"
    if binding.get("device") == "keyboard":
        vk = int(binding.get("vk", 0))
        return controls.VK_NAMES.get(vk, f"VK{vk:02X}")
    if binding.get("device") == "xinput":
        control = str(binding.get("control", "?"))
        label = _COMPACT_PAD_NAMES.get(control, control)
        if isinstance(assigned_device, int):
            return f"P{assigned_device + 1} {label}"
        return f"PAD {label}"
    return "+"


@dataclass(frozen=True)
class CaptureResult:
    kind: str
    message: str


class ControlEditor:
    def __init__(self, path: Path | None = None) -> None:
        self.path = controls.CONFIG_PATH if path is None else path
        self.config, self.normalization_report = controls.load_config_with_report(
            self.path
        )
        self.dirty = self.normalization_report.needs_persist
        self.capture_target: tuple[str, str, int] | None = None
        self.assign_target: str | None = None
        self.capture_baseline: set[tuple[int, str]] = set()
        self.sdl_capture_baseline: set[tuple[tuple[Any, ...], str]] = set()
        self.assignment_xinput_pending: int | None = None
        self.assignment_sdl_pending: list[dict[str, Any]] = []
        self.assignment_pending_since: float | None = None

    @property
    def read_only(self) -> bool:
        return self.normalization_report.incompatible_future

    def _require_writable(self) -> None:
        if self.read_only:
            source_version = self.normalization_report.source_version
            assert source_version is not None
            raise controls.UnsupportedControllerConfigVersion(source_version)

    def reload(self) -> None:
        self.config, self.normalization_report = controls.load_config_with_report(
            self.path
        )
        self.dirty = self.normalization_report.needs_persist
        self.cancel_capture()

    def restore_defaults(self) -> None:
        self._require_writable()
        self.config = controls.default_config()
        _normalized, self.normalization_report = controls.normalize_config_with_report(
            self.config
        )
        self.dirty = True
        self.cancel_capture()

    def save(self) -> None:
        self._require_writable()
        controls.save_config(self.config, self.path)
        self.config, self.normalization_report = controls.load_config_with_report(
            self.path
        )
        self.dirty = False
        self.cancel_capture()

    def bindings(self, player: str, action: str) -> list[dict[str, Any]]:
        return self.config["players"][player][action]

    def assigned(self, player: str) -> int | dict[str, Any] | None:
        return controls.normalize_device_assignment(
            self.config.get("devices", {}).get(player)
        )

    def desktop_owner(self) -> str | None:
        return controls.desktop_owner(self.config)

    def owns_desktop(self, player: str) -> bool:
        return controls.player_owns_desktop(self.config, player)

    def assign_desktop(self, player: str) -> str | None:
        self._require_writable()
        previous = self.desktop_owner()
        controls.set_desktop_owner(self.config, player)
        if previous != player:
            self.dirty = True
        self.cancel_capture()
        return previous

    def clear_desktop(self, player: str) -> bool:
        self._require_writable()
        if not self.owns_desktop(player):
            return False
        controls.set_desktop_owner(self.config, None)
        self.dirty = True
        self.cancel_capture()
        return True

    @staticmethod
    def active_pad_controls(
        states: Iterable[Any], threshold: float = 0.72
    ) -> set[tuple[int, str]]:
        active: set[tuple[int, str]] = set()
        names = tuple(controls.XINPUT_BUTTONS) + controls.XINPUT_AXIS_CONTROLS
        for index, state in enumerate(states):
            for control in names:
                if controls.xinput_control_strength(state, control) >= threshold:
                    active.add((index, control))
        return active

    @staticmethod
    def active_sdl_controls(
        states: Iterable[controls.SDLControllerState], threshold: float = 0.72
    ) -> set[tuple[tuple[Any, ...], str]]:
        active: set[tuple[tuple[Any, ...], str]] = set()
        names = tuple(controls.XINPUT_BUTTONS) + controls.XINPUT_AXIS_CONTROLS
        for state in states:
            key = controls.device_assignment_key(state.assignment)
            if key is None:
                continue
            for control in names:
                if controls.sdl_control_strength(state, control) >= threshold:
                    active.add((key, control))
        return active

    def begin_assignment(
        self,
        player: str,
        states: Iterable[Any],
        sdl_states: Iterable[controls.SDLControllerState] = (),
    ) -> None:
        self._require_writable()
        self.capture_target = None
        self.assign_target = player
        self._clear_assignment_pending()
        self.capture_baseline = self.active_pad_controls(states, 0.55)
        self.sdl_capture_baseline = self.active_sdl_controls(sdl_states, 0.55)

    def begin_capture(
        self,
        player: str,
        action: str,
        slot: int,
        states: Iterable[Any],
        sdl_states: Iterable[controls.SDLControllerState] = (),
    ) -> None:
        self._require_writable()
        if player not in controls.PLAYER_ACTIONS:
            raise ValueError(f"unknown player: {player}")
        if action not in dict(controls.PLAYER_ACTIONS[player]):
            raise ValueError(f"unknown action for {player}: {action}")
        if not 0 <= int(slot) < controls.MAX_BINDINGS_PER_ACTION:
            raise ValueError(
                "binding slot must be 0.."
                f"{controls.MAX_BINDINGS_PER_ACTION - 1}"
            )
        self.assign_target = None
        self._clear_assignment_pending()
        self.capture_target = (player, action, int(slot))
        self.capture_baseline = self.active_pad_controls(states, 0.55)
        self.sdl_capture_baseline = self.active_sdl_controls(sdl_states, 0.55)

    def cancel_capture(self) -> None:
        self.capture_target = None
        self.assign_target = None
        self.capture_baseline.clear()
        self.sdl_capture_baseline.clear()
        self._clear_assignment_pending()

    def _clear_assignment_pending(self) -> None:
        self.assignment_xinput_pending = None
        self.assignment_sdl_pending = []
        self.assignment_pending_since = None

    def assign_controller(
        self,
        player: str,
        assignment: int | dict[str, Any] | None,
    ) -> str | None:
        self._require_writable()
        if player not in ("p1", "p2", "p3"):
            raise ValueError(f"unknown player: {player}")
        normalized = controls.normalize_device_assignment(assignment)
        if assignment is not None and normalized is None:
            raise ValueError("unsupported controller assignment")
        assignment = normalized
        previous_assignment = self.assigned(player)
        displaced_assignment = (
            None
            if controls.device_assignments_overlap(
                self.config,
                previous_assignment,
                assignment,
            )
            else previous_assignment
        )
        displaced_role: str | None = None
        if assignment is not None:
            for role in ("p1", "p2", "p3"):
                if (
                    role != player
                    and controls.device_assignments_overlap(
                        self.config,
                        self.config["devices"].get(role),
                        assignment,
                    )
                ):
                    if displaced_role is None:
                        displaced_role = role
                        self.config["devices"][role] = displaced_assignment
                        self._rewrite_gamepad_binding_indices(
                            role,
                            displaced_assignment,
                        )
                    else:
                        # A normalized configuration should already be exclusive.
                        # If a hand-edited file contains extra owners, retain one
                        # deterministic swap and remove every remaining duplicate.
                        self.config["devices"][role] = None
        self.config["devices"][player] = assignment
        self._rewrite_gamepad_binding_indices(player, assignment)
        self.dirty = True
        self.cancel_capture()
        return displaced_role

    def _rewrite_gamepad_binding_indices(
        self,
        player: str,
        assignment: int | dict[str, Any] | None,
    ) -> None:
        if assignment is None:
            return
        binding_index = assignment if isinstance(assignment, int) else 0
        default_actions = controls.default_config()["players"][player]
        for action, values in self.config["players"][player].items():
            has_pad_binding = False
            for binding in values:
                if binding.get("device") == "xinput":
                    binding["index"] = binding_index
                    has_pad_binding = True

            if not has_pad_binding:
                default_pad = next(
                    (
                        binding
                        for binding in default_actions.get(action, ())
                        if binding.get("device") == "xinput"
                    ),
                    None,
                )
                if default_pad is not None:
                    values[:] = values[:2]
                    values.append(
                        controls.pad_binding(
                            binding_index,
                            str(default_pad["control"]),
                        )
                    )

    def _set_binding(self, binding: dict[str, Any]) -> CaptureResult:
        if self.capture_target is None:
            return CaptureResult("ignored", "Nenhuma captura ativa.")
        player, action, slot = self.capture_target
        if binding.get("device") == "keyboard" and not self.owns_desktop(player):
            owner = self.desktop_owner()
            if owner is None:
                message = (
                    "Teclado e mouse não estão atribuídos. Atribua o dispositivo "
                    f"compartilhado a {player.upper()} antes de usar uma tecla."
                )
            else:
                message = (
                    f"Teclado e mouse pertencem a {owner.upper()}. Transfira o "
                    "dispositivo compartilhado antes de usar uma tecla em "
                    f"{player.upper()}."
                )
            return CaptureResult("invalid", message)
        if binding.get("device") == "xinput":
            return self._set_gamepad_binding(
                int(binding["index"]),
                str(binding["control"]),
            )
        return self._store_binding(player, action, slot, binding)

    def _set_gamepad_binding(
        self,
        assignment: int | dict[str, Any],
        control: str,
    ) -> CaptureResult:
        if self.capture_target is None:
            return CaptureResult("ignored", "Nenhuma captura ativa.")
        player, action, slot = self.capture_target
        normalized = controls.normalize_device_assignment(assignment)
        if normalized is None:
            return CaptureResult("invalid", "Controle não suportado.")
        assigned = self.assigned(player)
        if assigned is None:
            self.assign_controller(player, normalized)
            self.capture_target = (player, action, slot)
            assigned = normalized
        elif not controls.device_assignments_overlap(
            self.config,
            assigned,
            normalized,
        ):
            return CaptureResult(
                "wrong_pad",
                f"Use o controle atribuído a {player.upper()}.",
            )
        binding_index = assigned if isinstance(assigned, int) else 0
        binding = controls.pad_binding(binding_index, str(control))
        return self._store_binding(player, action, slot, binding)

    def _store_binding(
        self,
        player: str,
        action: str,
        slot: int,
        binding: dict[str, Any],
    ) -> CaptureResult:
        self._require_writable()
        values = list(self.bindings(player, action))
        if slot < len(values):
            values[slot] = binding
        else:
            while len(values) < slot:
                values.append({})
            values.append(binding)
        seen: set[tuple[tuple[str, Any], ...]] = set()
        unique: list[dict[str, Any]] = []
        for value in values:
            if not value:
                continue
            signature = tuple(sorted(value.items()))
            if signature in seen:
                continue
            seen.add(signature)
            unique.append(value)
        self.config["players"][player][action] = unique[
            : controls.MAX_BINDINGS_PER_ACTION
        ]
        self.dirty = True
        self.cancel_capture()
        return CaptureResult("bound", "Comando alterado.")

    def feed_keyboard_vk(self, vk: int) -> CaptureResult:
        if not 0 < int(vk) < 256:
            return CaptureResult("invalid", "Tecla não suportada.")
        return self._set_binding(controls.key_binding(int(vk)))

    def clear_binding(self, player: str, action: str, slot: int) -> bool:
        self._require_writable()
        values = list(self.bindings(player, action))
        if slot >= len(values):
            return False
        values.pop(slot)
        self.config["players"][player][action] = values
        self.dirty = True
        self.cancel_capture()
        return True

    @staticmethod
    def _assignment_result(
        player: str,
        assignment: int | dict[str, Any],
        displaced_role: str | None,
        *,
        unified_backends: bool = False,
    ) -> CaptureResult:
        swap_note = (
            f"; o controle anterior de {player.upper()} foi movido para "
            f"{displaced_role.upper()}"
            if displaced_role is not None
            else ""
        )
        name = (
            f"Controle {assignment + 1}"
            if isinstance(assignment, int)
            else str(assignment["name"])
        )
        message = f"{name} atribuído a {player.upper()}{swap_note}."
        if unified_backends:
            message += " XInput e SDL foram reconhecidos como o mesmo controle físico."
        return CaptureResult("assigned", message)

    def poll_assignment(
        self,
        states: Iterable[Any],
        sdl_states: Iterable[controls.SDLControllerState],
        *,
        now: float | None = None,
    ) -> CaptureResult | None:
        """Capture one physical pad and merge duplicate XInput/SDL identities."""
        if self.assign_target is None:
            return None
        observed_at = time.monotonic() if now is None else float(now)
        state_list = list(states)
        sdl_state_list = list(sdl_states)
        active = self.active_pad_controls(state_list, 0.72)
        active_sdl = self.active_sdl_controls(sdl_state_list, 0.72)
        new_inputs = active - self.capture_baseline
        new_sdl_inputs = active_sdl - self.sdl_capture_baseline
        self.capture_baseline = active
        self.sdl_capture_baseline = active_sdl

        xinput_starts = sorted(
            {index for index, control in new_inputs if control == "START"}
        )
        sdl_starts = sorted(
            {key for key, control in new_sdl_inputs if control == "START"}
        )
        has_xinput_anchor = bool(
            xinput_starts or self.assignment_xinput_pending is not None
        )
        if len(xinput_starts) > 1 or (
            len(sdl_starts) > 1 and not has_xinput_anchor
        ):
            self._clear_assignment_pending()
            return CaptureResult(
                "invalid",
                "Mais de um controle respondeu. Solte todos e pressione START somente no controle desejado.",
            )

        xinput = xinput_starts[0] if xinput_starts else None
        state_by_key = {
            controls.device_assignment_key(state.assignment): state
            for state in sdl_state_list
        }
        observed_sdl = [state_by_key[key].assignment for key in sdl_starts]
        if (
            xinput is not None
            and self.assignment_xinput_pending is not None
            and xinput != self.assignment_xinput_pending
        ):
            self._clear_assignment_pending()
            return CaptureResult(
                "invalid",
                "Mais de um controle respondeu. Solte todos e pressione START somente no controle desejado.",
            )

        if xinput is not None and self.assignment_xinput_pending is None:
            self.assignment_xinput_pending = xinput
        pending_sdl_keys = {
            controls.device_assignment_key(value)
            for value in self.assignment_sdl_pending
        }
        for sdl in observed_sdl:
            key = controls.device_assignment_key(sdl)
            if key not in pending_sdl_keys:
                self.assignment_sdl_pending.append(dict(sdl))
                pending_sdl_keys.add(key)
        if (xinput is not None or observed_sdl) and self.assignment_pending_since is None:
            self.assignment_pending_since = observed_at

        pending_xinput = self.assignment_xinput_pending
        pending_sdl = list(self.assignment_sdl_pending)
        if pending_xinput is not None and pending_sdl:
            controls.register_device_alias_group(
                self.config,
                pending_xinput,
                pending_sdl,
            )
            player = self.assign_target
            displaced_role = self.assign_controller(player, pending_xinput)
            return self._assignment_result(
                player,
                pending_xinput,
                displaced_role,
                unified_backends=True,
            )

        if self.assignment_pending_since is None:
            return None
        if observed_at - self.assignment_pending_since < CONTROLLER_ALIAS_CAPTURE_WINDOW:
            return None

        assignment: int | dict[str, Any]
        if pending_xinput is not None:
            assignment = pending_xinput
        elif len(pending_sdl) == 1:
            assignment = pending_sdl[0]
        else:
            self._clear_assignment_pending()
            return None
        player = self.assign_target
        displaced_role = self.assign_controller(player, assignment)
        return self._assignment_result(player, assignment, displaced_role)

    def poll_xinput(self, states: Iterable[Any]) -> CaptureResult | None:
        state_list = list(states)
        active = self.active_pad_controls(state_list, 0.72)
        new_inputs = active - self.capture_baseline
        if self.assign_target is not None:
            starts = sorted(
                (index, control) for index, control in new_inputs if control == "START"
            )
            if starts:
                player = self.assign_target
                index = starts[0][0]
                displaced_role = self.assign_controller(player, index)
                return self._assignment_result(player, index, displaced_role)
        elif self.capture_target is not None and new_inputs:
            candidates: list[tuple[float, int, str]] = []
            for index, control in new_inputs:
                strength = controls.xinput_control_strength(state_list[index], control)
                candidates.append((strength, index, control))
            _strength, index, control = sorted(
                candidates, key=lambda item: (-item[0], item[1], item[2])
            )[0]
            result = self._set_binding(controls.pad_binding(index, control))
            if result.kind == "wrong_pad":
                self.capture_baseline |= {(index, control)}
            return result
        self.capture_baseline &= active
        return None

    def poll_sdl(
        self,
        states: Iterable[controls.SDLControllerState],
    ) -> CaptureResult | None:
        state_list = list(states)
        state_by_key = {
            controls.device_assignment_key(state.assignment): state
            for state in state_list
        }
        active = self.active_sdl_controls(state_list, 0.72)
        new_inputs = active - self.sdl_capture_baseline
        if self.assign_target is not None:
            starts = sorted(
                (key, control) for key, control in new_inputs if control == "START"
            )
            if starts:
                player = self.assign_target
                key = starts[0][0]
                state = state_by_key[key]
                displaced_role = self.assign_controller(player, state.assignment)
                return self._assignment_result(
                    player,
                    state.assignment,
                    displaced_role,
                )
        elif self.capture_target is not None and new_inputs:
            candidates: list[tuple[float, tuple[Any, ...], str]] = []
            for key, control in new_inputs:
                state = state_by_key[key]
                candidates.append(
                    (controls.sdl_control_strength(state, control), key, control)
                )
            _strength, key, control = sorted(
                candidates,
                key=lambda item: (-item[0], item[1], item[2]),
            )[0]
            state = state_by_key[key]
            result = self._set_gamepad_binding(state.assignment, control)
            if result.kind == "wrong_pad":
                self.sdl_capture_baseline.add((key, control))
            return result
        self.sdl_capture_baseline &= active
        return None


def process_ids(
    executable: str = GAME_EXECUTABLE,
    *,
    expected_path: Path | None = None,
) -> tuple[int, ...]:
    if expected_path is not None:
        return game_process.matching_process_ids(expected_path)
    return game_process.named_process_ids(executable)


def acquire_launcher_instance() -> int | None:
    if os.name != "nt":
        return 1
    kernel32 = ct.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
    kernel32.CreateMutexW.restype = wt.HANDLE
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.CloseHandle.restype = wt.BOOL
    ct.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, LAUNCHER_MUTEX_NAME)
    if not handle:
        raise ct.WinError(ct.get_last_error())
    if ct.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return int(handle)


def release_launcher_instance(handle: int | None) -> None:
    if os.name != "nt" or not handle:
        return
    kernel32 = ct.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.CloseHandle.restype = wt.BOOL
    kernel32.CloseHandle(handle)


def runtime_mutex_active(game_pid: int) -> bool:
    if os.name != "nt" or int(game_pid) <= 0:
        return False
    kernel32 = ct.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
    kernel32.OpenMutexW.restype = wt.HANDLE
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.CloseHandle.restype = wt.BOOL
    ct.set_last_error(0)
    handle = kernel32.OpenMutexW(
        SYNCHRONIZE,
        False,
        f"{RUNTIME_MUTEX_PREFIX}{int(game_pid)}",
    )
    if not handle:
        # Only the exact "object does not exist" result proves absence.  An
        # access, handle-table, or transient query failure must never authorize
        # a second supervisor for the same live game process.
        return ct.get_last_error() != ERROR_FILE_NOT_FOUND
    kernel32.CloseHandle(handle)
    return True


def active_runtime_pid(game_pids: Iterable[int] | None = None) -> int | None:
    candidates = process_ids() if game_pids is None else tuple(game_pids)
    for pid in candidates:
        if runtime_mutex_active(int(pid)):
            return int(pid)
    return None


@dataclass(frozen=True)
class LaunchEvent:
    kind: str
    message: str


class CoopLaunchSupervisor:
    def __init__(self, root: Path | None = None) -> None:
        self.root = game_root() if root is None else Path(root)
        self.game_executable = (self.root / "x64" / GAME_EXECUTABLE).resolve(
            strict=False
        )
        self.runtime_directory = runtime_paths.resolve_runtime_directory(__file__)
        self.events: Queue[LaunchEvent] = Queue()
        self.thread: threading.Thread | None = None
        self.attached_pid: int | None = None
        self.state = "idle"
        self.last_message = "Configurações prontas. Abra o jogo normalmente."
        self._lock = threading.Lock()
        existing = active_runtime_pid(process_ids(expected_path=self.game_executable))
        if existing is not None:
            self._begin_monitoring(existing, publish_initial=False)

    @property
    def busy(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    @property
    def session_active(self) -> bool:
        return bool(self.busy or self.attached_pid is not None)

    def emit(self, kind: str, message: str) -> None:
        self.events.put(LaunchEvent(kind, message))

    def drain(self) -> list[LaunchEvent]:
        received: list[LaunchEvent] = []
        while True:
            try:
                event = self.events.get_nowait()
            except Empty:
                break
            received.append(event)
            self.state = event.kind
            self.last_message = event.message
        return received

    def _begin_monitoring(
        self,
        game_pid: int,
        *,
        publish_initial: bool,
    ) -> None:
        self.attached_pid = int(game_pid)
        self.state, self.last_message = self._attached_state(int(game_pid))
        self.thread = threading.Thread(
            target=self._monitor_attached,
            args=(int(game_pid), publish_initial),
            name="ff12-coop-reattach",
            daemon=True,
        )
        self.thread.start()

    def attach_if_running(self) -> bool:
        """Attach the configurator to a runtime started outside this tool."""
        with self._lock:
            if self.busy or self.attached_pid is not None:
                return False
            existing = active_runtime_pid(
                process_ids(expected_path=self.game_executable)
            )
            if existing is None:
                return False
            self._begin_monitoring(existing, publish_initial=True)
            return True

    def _attached_state(self, game_pid: int) -> tuple[str, str]:
        status = runtime_status.read_status(
            game_pid,
            data_directory=self.runtime_directory,
        )
        if status is not None:
            return status.state, status.message

        return (
            "preparing",
            "Sessão encontrada. Confirmando controles e interface…",
        )

    def _monitor_attached(
        self,
        game_pid: int,
        publish_initial: bool = True,
    ) -> None:
        self.attached_pid = game_pid
        last_state, message = self._attached_state(game_pid)
        last_message = message
        if publish_initial:
            self.emit(last_state, message)
        while runtime_mutex_active(game_pid):
            state, message = self._attached_state(game_pid)
            if state != last_state or message != last_message:
                self.emit(state, message)
                last_state = state
                last_message = message
            time.sleep(0.35)
        self.attached_pid = None
        if game_pid in process_ids(expected_path=self.game_executable):
            self.emit(
                "idle",
                "A sessão co-op foi encerrada. Pronto para ligar novamente.",
            )
        else:
            self.emit("idle", "Jogo encerrado. Pronto para uma nova sessão.")


def save_roster(roster: coop_roster.CoopRoster) -> coop_roster.CoopRoster:
    return coop_roster.save(roster, coop_directory() / "coop_roster_config.json")


def load_roster() -> coop_roster.CoopRoster:
    return coop_roster.load(coop_directory() / "coop_roster_config.json")


__all__ = [
    "CaptureResult",
    "ControlEditor",
    "CoopLaunchSupervisor",
    "DEFAULT_LAUNCHER_CONFIG",
    "LaunchEvent",
    "bundled_asset",
    "bundled_portrait",
    "compact_binding_label",
    "coop_directory",
    "ensure_runtime_storage",
    "game_root",
    "game_override_bundle_root",
    "load_launcher_config",
    "load_roster",
    "normalize_launcher_config",
    "process_ids",
    "save_launcher_config",
    "save_roster",
]
