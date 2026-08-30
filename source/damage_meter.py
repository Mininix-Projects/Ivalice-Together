from __future__ import annotations

import ctypes as ct
import hashlib
import json
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from PIL import Image

import atomic_files
import coop_runtime as runtime
import headless_world_target_overlay as target_info
import launcher_core
import menu_themes
import session_state


MODE_AREA = "area"
MODE_INDEFINITE = "indefinite"
MODES = frozenset((MODE_AREA, MODE_INDEFINITE))

PANEL_X = 1688
PANEL_Y = 724
PANEL_WIDTH = 230
PANEL_HEIGHT = 99
ROW_PITCH = 31
ROW_REORDER_DURATION = 0.32
TEXT_SCALE = 0.57
AREA_REAPPEAR_DELAY = 1.25
CONFIG_POLL_SECONDS = 0.25
RUNTIME_RETRY_SECONDS = 0.5
SESSION_FILE_VERSION = 1
SESSION_SAVE_INTERVAL = 0.5
SESSION_TOTAL_LIMIT = 10**18

HOOK_MAGIC = runtime.DAMAGE_METER_HOOK_MAGIC
PROXY_MAGIC = runtime.DAMAGE_METER_PROXY_MAGIC
HOOK_VERSION = 2
HOOK_ALLOCATION_SIZE = 0x10000
HOOK_WRAPPER_OFFSET = 0x100
PROXY_ALLOCATION_OFFSET = runtime.DAMAGE_METER_PROXY_ALLOCATION_OFFSET
PROXY_OLD_TARGET_OFFSET = runtime.DAMAGE_METER_PROXY_OLD_TARGET_OFFSET
PROXY_CODE_OFFSET = runtime.DAMAGE_METER_PROXY_CODE_OFFSET
PROXY_IMAGE_SIZE = runtime.DAMAGE_METER_PROXY_WORKSPACE_SIZE
PROXY_JUMP_SIZE = 0x10
RING_OFFSET = 0x1000
RING_CAPACITY = 256
EVENT_SIZE = 0x30
HEADER_VERSION_OFFSET = 0x08
HEADER_CAPACITY_OFFSET = 0x0C
HEADER_SEQUENCE_OFFSET = 0x10
HEADER_OLD_TARGET_OFFSET = runtime.DAMAGE_METER_HOOK_OLD_TARGET_OFFSET
HEADER_SITE_OFFSET = 0x20
HEADER_WRAPPER_SIZE_OFFSET = 0x28
HEADER_SIZE_OFFSET = 0x2C
HEADER_WRAPPER_SHA256_OFFSET = 0x30
HEADER_WRAPPER_SHA256_SIZE = 0x20
HOOK_HEADER_FORMAT = "<8sIIQQQII32s"
HOOK_HEADER_SIZE = struct.calcsize(HOOK_HEADER_FORMAT)
INVALID_LOCATION = 0xFFFFFFFF


@dataclass(frozen=True)
class DamageMeterSettings:
    enabled: bool = False
    mode: str = MODE_AREA
    effective_only: bool = False
    session_id: int = 1

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> "DamageMeterSettings":
        mode = str(config.get("damage_meter_mode", MODE_AREA))
        if mode not in MODES:
            mode = MODE_AREA
        try:
            session_id = int(config.get("damage_meter_session_id", 1))
        except (TypeError, ValueError):
            session_id = 1
        return cls(
            enabled=bool(config.get("damage_meter_enabled", False)),
            mode=mode,
            effective_only=bool(config.get("damage_meter_effective", False)),
            session_id=min(0x7FFFFFFF, max(1, session_id)),
        )


class DamageMeterSettingsTracker:
    def __init__(
        self,
        path: Path | None = None,
        poll_seconds: float = CONFIG_POLL_SECONDS,
    ) -> None:
        self.path = launcher_core.launcher_config_path() if path is None else path
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.settings = DamageMeterSettings()
        self._stamp: tuple[int, int] | None = None
        self._next_poll = 0.0
        self.reload(force=True)

    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def reload(
        self,
        *,
        force: bool = False,
        now: float | None = None,
    ) -> DamageMeterSettings:
        now = time.monotonic() if now is None else float(now)
        if not force and now < self._next_poll:
            return self.settings
        self._next_poll = now + self.poll_seconds
        stamp = self._file_stamp()
        if not force and stamp == self._stamp:
            return self.settings
        self._stamp = stamp
        self.settings = DamageMeterSettings.from_config(
            launcher_core.load_launcher_config(self.path)
        )
        return self.settings


class Panel(Protocol):
    def draw(
        self,
        image: Image.Image,
        logical_x: float,
        logical_y: float,
        opacity: float = 1.0,
    ) -> None: ...

    def hide(self) -> None: ...

    def set_game_foreground(self, foreground: bool) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class DamageEvent:
    sequence: int
    source_actor: int
    target_actor: int
    amount: int
    location: int
    effective_amount: int | None = None


def _normalized_totals(value: object) -> dict[int, int]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[int, int] = {}
    for raw_member_id, raw_amount in value.items():
        try:
            member_id = int(raw_member_id)
            amount = int(raw_amount)
        except (TypeError, ValueError):
            continue
        if member_id not in session_state.PARTY_NAMES or amount < 0:
            continue
        result[member_id] = min(amount, SESSION_TOTAL_LIMIT)
    return result


class DamageSessionStore:
    def __init__(
        self,
        session_id: int,
        path: Path | None = None,
        *,
        save_interval: float = SESSION_SAVE_INTERVAL,
    ) -> None:
        self.session_id = min(0x7FFFFFFF, max(1, int(session_id)))
        self.path = (
            launcher_core.coop_directory() / "damage_meter_session.json"
            if path is None
            else Path(path)
        )
        self.save_interval = max(0.0, float(save_interval))
        self.gross: dict[int, int] = {}
        self.effective: dict[int, int] = {}
        self.dirty = False
        self.next_save_at = 0.0
        self.last_error: tuple[str, str] | None = None
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return
        if not isinstance(payload, dict) or payload.get("version") != SESSION_FILE_VERSION:
            return
        try:
            stored_session_id = int(payload.get("session_id", 0))
        except (TypeError, ValueError):
            return
        if stored_session_id != self.session_id:
            self.dirty = True
            return
        self.gross = _normalized_totals(payload.get("gross"))
        self.effective = _normalized_totals(payload.get("effective"))

    def update(
        self,
        gross: Mapping[int, int],
        effective: Mapping[int, int],
        *,
        now: float,
        force: bool = False,
    ) -> bool:
        normalized_gross = _normalized_totals(gross)
        normalized_effective = _normalized_totals(effective)
        if normalized_gross != self.gross or normalized_effective != self.effective:
            self.gross = normalized_gross
            self.effective = normalized_effective
            self.dirty = True
        if not self.dirty or (not force and float(now) < self.next_save_at):
            return False
        payload = {
            "version": SESSION_FILE_VERSION,
            "session_id": self.session_id,
            "gross": {str(key): value for key, value in sorted(self.gross.items())},
            "effective": {
                str(key): value for key, value in sorted(self.effective.items())
            },
        }
        try:
            atomic_files.write_text(
                self.path,
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            )
        except OSError as error:
            signature = type(error).__name__, str(error)
            if signature != self.last_error:
                print(
                    f"DAMAGE_METER SESSION SAVE RETRY {signature[0]}: {signature[1]}",
                    flush=True,
                )
                self.last_error = signature
            self.next_save_at = float(now) + self.save_interval
            return False
        self.dirty = False
        self.last_error = None
        self.next_save_at = float(now) + self.save_interval
        return True


@dataclass(frozen=True)
class DamageRow:
    member_id: int
    amount: int
    party_order: int


@dataclass(frozen=True)
class RowMotion:
    start_y: float
    target_y: float
    started_at: float


class RowAnimator:
    def __init__(self, duration: float = ROW_REORDER_DURATION) -> None:
        self.duration = max(0.001, float(duration))
        self.motions: dict[int, RowMotion] = {}

    def _position(self, motion: RowMotion, now: float) -> float:
        progress = min(1.0, max(0.0, (float(now) - motion.started_at) / self.duration))
        eased = progress * progress * (3.0 - 2.0 * progress)
        return motion.start_y + (motion.target_y - motion.start_y) * eased

    def layout(
        self, rows: Sequence[DamageRow], now: float
    ) -> tuple[tuple[DamageRow, int], ...]:
        members = {row.member_id for row in rows}
        self.motions = {
            member_id: motion
            for member_id, motion in self.motions.items()
            if member_id in members
        }

        layout = []
        for index, row in enumerate(rows):
            target_y = float(index * ROW_PITCH)
            motion = self.motions.get(row.member_id)
            if motion is None:
                motion = RowMotion(target_y, target_y, float(now))
            elif motion.target_y != target_y:
                motion = RowMotion(self._position(motion, now), target_y, float(now))
            self.motions[row.member_id] = motion
            layout.append((row, round(self._position(motion, now))))
        return tuple(layout)


class DamageTotals:
    def __init__(
        self,
        mode: str,
        *,
        effective_only: bool = False,
        indefinite_gross: Mapping[int, int] | None = None,
        indefinite_effective: Mapping[int, int] | None = None,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"unsupported damage-meter mode: {mode}")
        self.mode = mode
        self.effective_only = bool(effective_only)
        self.current_location: int | None = None
        self.area_gross: dict[int, int] = {}
        self.area_effective: dict[int, int] = {}
        self.indefinite_gross = _normalized_totals(indefinite_gross)
        self.indefinite_effective = _normalized_totals(indefinite_effective)
        self.area = self.area_effective if self.effective_only else self.area_gross
        self.indefinite = (
            self.indefinite_effective
            if self.effective_only
            else self.indefinite_gross
        )

    def observe_location(self, location: int | None) -> None:
        if location is None or int(location) == INVALID_LOCATION:
            return
        location = int(location)
        if location != self.current_location:
            self.current_location = location
            self.area_gross.clear()
            self.area_effective.clear()

    def add(self, event: DamageEvent, member_id: int | None) -> bool:
        gross = max(0, int(event.amount))
        effective = max(
            0,
            int(
                event.effective_amount
                if event.effective_amount is not None
                else event.amount
            ),
        )
        if member_id is None or (gross <= 0 and effective <= 0):
            return False
        member_id = int(member_id)
        self.indefinite_gross[member_id] = min(
            SESSION_TOTAL_LIMIT,
            self.indefinite_gross.get(member_id, 0) + gross,
        )
        self.indefinite_effective[member_id] = min(
            SESSION_TOTAL_LIMIT,
            self.indefinite_effective.get(member_id, 0) + effective,
        )
        if (
            self.current_location is not None
            and event.location == self.current_location
        ):
            self.area_gross[member_id] = min(
                SESSION_TOTAL_LIMIT,
                self.area_gross.get(member_id, 0) + gross,
            )
            self.area_effective[member_id] = min(
                SESSION_TOTAL_LIMIT,
                self.area_effective.get(member_id, 0) + effective,
            )
        return True

    def value(self, member_id: int) -> int:
        values = self.area if self.mode == MODE_AREA else self.indefinite
        return values.get(int(member_id), 0)


def materialized_party_members(
    active_party: Sequence[tuple[int, str, int, int]],
) -> tuple[int, ...]:
    members = []
    for member_id, _name, _handle, actor in active_party:
        member_id = int(member_id)
        if member_id in session_state.PARTY_NAMES and actor and member_id not in members:
            members.append(member_id)
    return tuple(members)


def active_actor_owners(
    active_party: Sequence[tuple[int, str, int, int]],
    members: Sequence[int],
) -> dict[int, int]:
    active_members = frozenset(int(member_id) for member_id in members)
    return {
        int(actor): int(member_id)
        for member_id, _name, _handle, actor in active_party
        if member_id in active_members and actor
    }


def rank_rows(
    members: Sequence[int], totals: DamageTotals, *, show_zero: bool = True
) -> tuple[DamageRow, ...]:
    rows = tuple(
        DamageRow(int(member_id), totals.value(member_id), order)
        for order, member_id in enumerate(members)
        if show_zero or totals.value(member_id) > 0
    )
    return tuple(sorted(rows, key=lambda row: (-row.amount, row.party_order)))


def render_meter(
    rows: Sequence[DamageRow],
    selection: menu_themes.ThemeSelection,
    y_positions: Mapping[int, int] | None = None,
) -> Image.Image:
    image = Image.new("RGBA", (PANEL_WIDTH, PANEL_HEIGHT), (0, 0, 0, 0))
    for index, row in enumerate(rows[:3]):
        theme_name = selection.for_character(row.member_id)
        label = target_info.FONT.render(
            f"DMG: {row.amount}",
            scale=TEXT_SCALE,
            style=target_info.target_theme_style(theme_name),
        )
        y = index * ROW_PITCH
        if y_positions is not None:
            y = y_positions.get(row.member_id, y)
        image.alpha_composite(label, (4, y))
    return image


class AreaTransitionGate:
    def __init__(self, delay: float = AREA_REAPPEAR_DELAY) -> None:
        self.delay = max(0.0, float(delay))
        self.location: int | None = None
        self.transition_observed = False
        self.visible_at = 0.0

    def ready(self, snapshot: session_state.SessionSnapshot, now: float) -> bool:
        location = snapshot.current_location
        coherent = bool(
            not snapshot.loading
            and location is not None
            and location != INVALID_LOCATION
            and location == snapshot.ui_location
        )
        if not coherent:
            self.transition_observed = True
            return False

        location = int(location)
        if self.location is None:
            self.location = location
            if self.transition_observed:
                self.visible_at = float(now) + self.delay
        elif location != self.location or self.transition_observed:
            self.location = location
            self.visible_at = float(now) + self.delay
        self.transition_observed = False
        return float(now) >= self.visible_at


class WrapperBuilder:
    def __init__(self, address: int) -> None:
        self.address = int(address)
        self.code = bytearray()
        self.labels: dict[str, int] = {}
        self.fixups: list[tuple[int, str]] = []

    def emit(self, data: bytes) -> None:
        self.code.extend(data)

    def label(self, name: str) -> None:
        self.labels[name] = len(self.code)

    def jcc(self, opcode: int, label: str) -> None:
        self.code.extend((0x0F, opcode))
        self.fixups.append((len(self.code), label))
        self.code.extend(b"\x00" * 4)

    def finish(self) -> bytes:
        for offset, label in self.fixups:
            target = self.address + self.labels[label]
            source_after = self.address + offset + 4
            struct.pack_into("<i", self.code, offset, target - source_after)
        return bytes(self.code)


def build_wrapper(wrapper_address: int, ring_base: int, old_target: int) -> bytes:
    builder = WrapperBuilder(wrapper_address)
    builder.emit(b"\x50\x51\x52\x41\x50\x41\x51\x41\x52\x41\x53")

    # Native damage arrives as a negative signed value. Recovery is non-negative.
    builder.emit(b"\x85\xd2")
    builder.jcc(0x8D, "done")
    builder.emit(b"\xf7\xda")

    # RSI is the target's battle state here, before HP is changed. Keep both the
    # displayed amount and the amount that can actually be removed from its HP.
    builder.emit(b"\x41\x89\xd0")
    builder.emit(b"\x44\x3b\x46\x48")
    builder.emit(b"\x44\x0f\x47\x46\x48")

    builder.emit(b"\x49\xb9" + struct.pack("<Q", session_state.LOCATION_SAVE_GLOBAL))
    builder.emit(b"\x4d\x8b\x09")
    builder.emit(b"\x4d\x85\xc9")
    builder.jcc(0x84, "done")
    builder.emit(
        b"\x45\x8b\x89" + struct.pack("<I", session_state.LOCATION_CURRENT_OFFSET)
    )
    builder.emit(b"\x41\x83\xf9\xff")
    builder.jcc(0x84, "done")

    builder.emit(b"\x48\xb9" + struct.pack("<Q", ring_base))
    builder.emit(b"\x48\x8b\x41\x10")
    builder.emit(b"\x48\xff\xc0")
    builder.emit(b"\x49\x89\xc3")
    builder.emit(b"\x41\x81\xe3" + struct.pack("<I", RING_CAPACITY - 1))
    builder.emit(b"\x4d\x6b\xdb" + bytes((EVENT_SIZE,)))
    builder.emit(b"\x49\x81\xc3" + struct.pack("<I", RING_OFFSET))
    builder.emit(b"\x49\x01\xcb")
    builder.emit(b"\x49\x89\x6b\x08")
    builder.emit(b"\x49\x89\x5b\x10")
    builder.emit(b"\x41\x89\x53\x18")
    builder.emit(b"\x45\x89\x43\x1c")
    builder.emit(b"\x45\x89\x4b\x20")
    builder.emit(b"\x41\xc7\x43\x24\xff\xff\xff\xff")
    builder.emit(b"\x49\x89\x03")
    builder.emit(b"\x48\x89\x41\x10")

    builder.label("done")
    builder.emit(b"\x41\x5b\x41\x5a\x41\x59\x41\x58\x5a\x59\x58")
    builder.emit(build_absolute_jump(old_target))
    return builder.finish()


def build_absolute_jump(target: int) -> bytes:
    # push rax; mov rax, target; xchg [rsp], rax; ret
    # This preserves RAX while transferring control to any 64-bit address.
    return b"\x50\x48\xb8" + struct.pack("<Q", int(target)) + b"\x48\x87\x04\x24\xc3"


def build_hook_header(
    old_target: int,
    site: int,
    wrapper: bytes,
    *,
    sequence: int = 0,
) -> bytes:
    wrapper = bytes(wrapper)
    return struct.pack(
        HOOK_HEADER_FORMAT,
        HOOK_MAGIC,
        HOOK_VERSION,
        RING_CAPACITY,
        int(sequence) & 0xFFFFFFFFFFFFFFFF,
        int(old_target),
        int(site),
        len(wrapper),
        HOOK_HEADER_SIZE,
        hashlib.sha256(wrapper).digest(),
    )


def build_proxy_image(hook_base: int, old_target: int, wrapper: int) -> bytes:
    image = (
        PROXY_MAGIC + struct.pack("<QQ", int(hook_base), int(old_target))
    ).ljust(PROXY_CODE_OFFSET, b"\0") + build_absolute_jump(wrapper)
    if len(image) > PROXY_IMAGE_SIZE:
        raise RuntimeError("damage-meter proxy exceeds its reserved workspace")
    return image.ljust(PROXY_IMAGE_SIZE, b"\0")


@dataclass(frozen=True)
class ResidentDamageHook:
    hook_base: int
    wrapper: int
    previous_target: int
    site: int
    sequence: int
    installed_call: bytes
    previous_call: bytes


def inspect_resident_damage_hook(
    process,
    damage_base: int,
    *,
    current_call: bytes | None = None,
) -> ResidentDamageHook | None:
    """Validate and describe an active v2 hook without mutating the process."""

    site = runtime.DAMAGE_NUMBER_SOURCE_CALL_SITE
    proxy_base = int(damage_base) + runtime.DAMAGE_METER_PROXY_WORKSPACE_OFFSET
    proxy_entry = proxy_base + PROXY_CODE_OFFSET
    proxy_call = b"\xE8" + runtime.rel32(site, 5, proxy_entry)
    if current_call is None:
        current_call = process.read(site, len(proxy_call))
    current_call = bytes(current_call)
    if current_call != proxy_call:
        return None

    proxy_image = process.read(proxy_base, PROXY_IMAGE_SIZE)
    if proxy_image[: len(PROXY_MAGIC)] != PROXY_MAGIC:
        raise RuntimeError("damage-number call points to an unsigned proxy")
    existing_base, restored_target = struct.unpack_from(
        "<QQ", proxy_image, PROXY_ALLOCATION_OFFSET
    )
    if not existing_base:
        raise RuntimeError("damage-meter proxy has no hook allocation")
    expected_old_target = (
        int(damage_base) + runtime.DAMAGE_NUMBER_SOURCE_WRAPPER_OFFSET
    )
    if restored_target != expected_old_target:
        raise RuntimeError("damage-meter proxy old target mismatch")

    wrapper_address = existing_base + HOOK_WRAPPER_OFFSET
    expected_wrapper = build_wrapper(
        wrapper_address,
        existing_base,
        restored_target,
    )
    raw_header = process.read(existing_base, HOOK_HEADER_SIZE)
    try:
        (
            magic,
            version,
            capacity,
            sequence,
            header_old_target,
            header_site,
            wrapper_size,
            header_size,
            wrapper_digest,
        ) = struct.unpack(HOOK_HEADER_FORMAT, raw_header)
    except struct.error as error:
        raise RuntimeError("damage-meter hook header is truncated") from error
    expected_digest = hashlib.sha256(expected_wrapper).digest()
    if (
        magic != HOOK_MAGIC
        or version != HOOK_VERSION
        or capacity != RING_CAPACITY
        or header_old_target != restored_target
        or header_site != site
        or wrapper_size != len(expected_wrapper)
        or header_size != HOOK_HEADER_SIZE
        or wrapper_digest != expected_digest
        or raw_header
        != build_hook_header(
            restored_target,
            site,
            expected_wrapper,
            sequence=sequence,
        )
    ):
        raise RuntimeError("damage-meter hook header/signature mismatch")
    actual_wrapper = process.read(wrapper_address, wrapper_size)
    if (
        actual_wrapper != expected_wrapper
        or hashlib.sha256(actual_wrapper).digest() != wrapper_digest
    ):
        raise RuntimeError("damage-meter hook wrapper mismatch")
    expected_proxy = build_proxy_image(
        existing_base,
        restored_target,
        wrapper_address,
    )
    if proxy_image != expected_proxy:
        raise RuntimeError("damage-meter proxy image mismatch")

    return ResidentDamageHook(
        hook_base=existing_base,
        wrapper=wrapper_address,
        previous_target=restored_target,
        site=site,
        sequence=sequence,
        installed_call=proxy_call,
        previous_call=b"\xE8" + runtime.rel32(site, 5, restored_target),
    )


class DamageHook:
    def __init__(self, process: runtime.RemoteProcess) -> None:
        self.process = process
        self.base = 0
        self.wrapper = 0
        self.old_call = b""
        self.installed_call = b""
        self.owns_static = False

    def _require_coop_runtime(self) -> None:
        instruction = self.process.read(runtime.MOVEMENT_HOOK_SITE, 5)
        if instruction[0] != 0xE9:
            raise RuntimeError("co-op runtime is not active")
        target = (
            runtime.MOVEMENT_HOOK_SITE + 5 + struct.unpack("<i", instruction[1:])[0]
        )
        cave_bases = (
            target - runtime.MOVEMENT_CAVE_OFFSET,
            target - 0x100,
        )
        runtime_base = 0
        for candidate in dict.fromkeys(cave_bases):
            try:
                if (
                    self.process.read(candidate, len(runtime.CAVE_MAGIC))
                    == runtime.CAVE_MAGIC
                ):
                    runtime_base = candidate
                    break
            except OSError:
                continue
        if not runtime_base:
            raise RuntimeError("co-op cave signature does not match")

        source_site = runtime.DAMAGE_NUMBER_SOURCE_CALL_SITE
        source_call = self.process.read(source_site, 5)
        if source_call[:1] != b"\xe8":
            raise RuntimeError("battle-number theme runtime is not ready")
        source_target = source_site + 5 + struct.unpack("<i", source_call[1:])[0]

        theme_target = source_target
        proxy_base = source_target - PROXY_CODE_OFFSET
        try:
            proxy_active = self.process.read(proxy_base, len(PROXY_MAGIC)) == PROXY_MAGIC
        except OSError:
            proxy_active = False
        if proxy_active:
            theme_target = self.process.u64(proxy_base + PROXY_OLD_TARGET_OFFSET)

        theme_base = theme_target - runtime.DAMAGE_NUMBER_SOURCE_WRAPPER_OFFSET
        try:
            theme_ready = (
                self.process.read(theme_base, len(runtime.DAMAGE_NUMBER_THEME_MAGIC))
                == runtime.DAMAGE_NUMBER_THEME_MAGIC
            )
        except OSError:
            theme_ready = False
        if not theme_ready:
            raise RuntimeError("battle-number theme runtime is not ready")

    def install(self) -> None:
        self._require_coop_runtime()
        site = runtime.DAMAGE_NUMBER_SOURCE_CALL_SITE
        current = self.process.read(site, 5)
        if current[0] != 0xE8:
            raise RuntimeError("damage-number source call is not a relative call")
        old_target = site + 5 + struct.unpack("<i", current[1:])[0]

        damage_base = self.process.ensure_damage_number_theme_allocation()
        proxy_base = damage_base + runtime.DAMAGE_METER_PROXY_WORKSPACE_OFFSET
        proxy_entry = proxy_base + PROXY_CODE_OFFSET
        resident = inspect_resident_damage_hook(
            self.process,
            damage_base,
            current_call=current,
        )
        if resident is not None:
            self.base = resident.hook_base
            self.wrapper = resident.wrapper
            self.old_call = resident.previous_call
            self.installed_call = resident.installed_call
            self.owns_static = False
            return

        proxy_image = self.process.read(proxy_base, PROXY_IMAGE_SIZE)
        if proxy_image != b"\0" * PROXY_IMAGE_SIZE:
            raise RuntimeError("inactive damage-meter proxy workspace is not empty")

        expected_old_target = damage_base + runtime.DAMAGE_NUMBER_SOURCE_WRAPPER_OFFSET
        if old_target != expected_old_target:
            raise RuntimeError(f"unknown damage-number hook at {old_target:#x}")

        allocation = runtime.allocate_executable(
            self.process.handle, HOOK_ALLOCATION_SIZE
        )
        if not allocation:
            raise OSError(ct.get_last_error(), "damage-meter allocation failed")
        self.base = int(allocation)
        self.wrapper = self.base + HOOK_WRAPPER_OFFSET
        wrapper = build_wrapper(self.wrapper, self.base, old_target)
        if len(wrapper) > RING_OFFSET - HOOK_WRAPPER_OFFSET:
            raise RuntimeError("damage-meter wrapper exceeds its code region")
        header = build_hook_header(old_target, site, wrapper)
        proxy = build_proxy_image(self.base, old_target, self.wrapper)
        try:
            self.process.write(self.base, header)
            self.process.write(self.wrapper, wrapper)
            self.process.write(proxy_base, proxy)
            self.old_call = current
            self.installed_call = b"\xe8" + runtime.rel32(site, 5, proxy_entry)
            self.process.patch_code(site, self.installed_call)
        except BaseException:
            # Once any remote byte may have been published, freeing this
            # allocation is unsafe: a game thread can already be executing the
            # proxy/wrapper even if PatchCode reported a failure. Leave the
            # immutable allocation resident for a later exact adoption.
            self.base = 0
            self.wrapper = 0
            self.old_call = b""
            self.installed_call = b""
            self.owns_static = False
            raise
        self.owns_static = True

    def intact(self) -> bool:
        return (
            self.process.read(runtime.DAMAGE_NUMBER_SOURCE_CALL_SITE, 5)
            == self.installed_call
        )

    def sequence(self) -> int:
        return self.process.u64(self.base + HEADER_SEQUENCE_OFFSET)

    def read_since(self, sequence: int) -> tuple[int, tuple[DamageEvent, ...]]:
        published = self.sequence()
        if published <= sequence:
            return published, ()
        start = max(sequence + 1, published - RING_CAPACITY + 1)
        events = []
        for expected in range(start, published + 1):
            offset = (
                self.base + RING_OFFSET + (expected & (RING_CAPACITY - 1)) * EVENT_SIZE
            )
            raw = self.process.read(offset, EVENT_SIZE)
            event_sequence, source, target, amount, effective, location, _member = (
                struct.unpack_from("<QQQIIII", raw)
            )
            if event_sequence == expected:
                events.append(
                    DamageEvent(expected, source, target, amount, location, effective)
                )
        return published, tuple(events)

    def close(self) -> None:
        if not self.base:
            return
        # The root, proxy and allocation are process-lifetime residents. There
        # is no safe quiescence proof for a game thread that may already have
        # entered either wrapper; detaching and especially VirtualFreeEx would
        # create a use-after-free window. A future instance adopts them exactly.
        self.base = 0
        self.wrapper = 0
        self.old_call = b""
        self.installed_call = b""
        self.owns_static = False


class DamageMeter:
    def __init__(
        self,
        process: runtime.RemoteProcess,
        panel: Panel,
        mode: str,
        *,
        effective_only: bool = False,
        session_id: int = 1,
        session_store: DamageSessionStore | None = None,
    ) -> None:
        self.process = process
        self.panel = panel
        self.hook = DamageHook(process)
        self.session_store = session_store or DamageSessionStore(session_id)
        self.totals = DamageTotals(
            mode,
            effective_only=effective_only,
            indefinite_gross=self.session_store.gross,
            indefinite_effective=self.session_store.effective,
        )
        self.transition = AreaTransitionGate()
        self.row_animator = RowAnimator()
        self.render_key: tuple[object, ...] | None = None
        self.image: Image.Image | None = None
        try:
            self.hook.install()
            self.sequence = self.hook.sequence()
        except Exception:
            self.hook.close()
            raise

    def update(
        self,
        snapshot: session_state.SessionSnapshot,
        active_party: Sequence[tuple[int, str, int, int]],
        selection: menu_themes.ThemeSelection,
        *,
        presentation_visible: bool,
        now: float,
    ) -> None:
        if not self.hook.intact():
            raise RuntimeError("damage-meter hook was replaced")

        coherent_location = (
            snapshot.current_location
            if snapshot.current_location == snapshot.ui_location
            else None
        )
        self.totals.observe_location(coherent_location)

        # Runtime actor materialization is authoritative here. Party/UI layouts
        # can briefly disagree while players leave, rejoin, or change roles.
        members = materialized_party_members(active_party)
        member_by_actor = active_actor_owners(active_party, members)
        party_actors = set(member_by_actor)
        self.sequence, events = self.hook.read_since(self.sequence)
        totals_changed = False
        for event in events:
            member_id = member_by_actor.get(event.source_actor)
            if member_id is not None and event.target_actor not in party_actors:
                totals_changed = self.totals.add(event, member_id) or totals_changed
        if totals_changed or self.session_store.dirty:
            self.session_store.update(
                self.totals.indefinite_gross,
                self.totals.indefinite_effective,
                now=now,
            )

        rows = rank_rows(members, self.totals)
        row_layout = self.row_animator.layout(rows, now)
        y_positions = {row.member_id: y for row, y in row_layout}
        themes = tuple(selection.for_character(row.member_id) for row in rows)
        render_key = (rows, themes, tuple(y_positions.items()))
        if render_key != self.render_key:
            self.image = render_meter(rows, selection, y_positions)
            self.render_key = render_key

        visible = bool(
            presentation_visible
            and rows
            and self.image is not None
            and self.transition.ready(snapshot, now)
        )
        if visible:
            self.panel.draw(self.image, PANEL_X, PANEL_Y)
        else:
            self.panel.hide()

    def close(self) -> None:
        self.session_store.update(
            self.totals.indefinite_gross,
            self.totals.indefinite_effective,
            now=time.monotonic(),
            force=True,
        )
        self.panel.hide()
        self.hook.close()


class LiveDamageMeter:
    def __init__(
        self,
        process: runtime.RemoteProcess,
        panel_factory: Callable[[], Panel],
        *,
        settings_tracker: DamageMeterSettingsTracker | None = None,
        meter_factory: Callable[..., DamageMeter] = DamageMeter,
    ) -> None:
        self.process = process
        self.panel_factory = panel_factory
        self.settings_tracker = settings_tracker or DamageMeterSettingsTracker()
        self.meter_factory = meter_factory
        self.settings = self.settings_tracker.settings
        self.panel: Panel | None = None
        self.meter: DamageMeter | None = None
        self.retry_at = 0.0
        self.last_start_error: tuple[str, str] | None = None

    def _close_active(self) -> None:
        meter, panel = self.meter, self.panel
        self.meter = None
        self.panel = None
        if meter is not None:
            try:
                meter.close()
            except (OSError, RuntimeError):
                pass
        if panel is not None:
            try:
                panel.close()
            except (OSError, RuntimeError):
                pass

    def _start(self, *, foreground: bool, now: float) -> bool:
        panel = None
        meter = None
        try:
            panel = self.panel_factory()
            meter = self.meter_factory(
                self.process,
                panel,
                self.settings.mode,
                effective_only=self.settings.effective_only,
                session_id=self.settings.session_id,
            )
            panel.set_game_foreground(foreground)
        except (OSError, RuntimeError, ValueError, struct.error) as error:
            if meter is not None:
                try:
                    meter.close()
                except (OSError, RuntimeError):
                    pass
            if panel is not None:
                try:
                    panel.close()
                except (OSError, RuntimeError):
                    pass
            self.retry_at = float(now) + RUNTIME_RETRY_SECONDS
            signature = type(error).__name__, str(error)
            if signature != self.last_start_error:
                print(
                    f"DAMAGE_METER WAITING {signature[0]}: {signature[1]}",
                    flush=True,
                )
                self.last_start_error = signature
            return False

        self.panel = panel
        self.meter = meter
        self.last_start_error = None
        print(
            "DAMAGE_METER ON "
            f"mode={self.settings.mode} "
            f"effective={int(self.settings.effective_only)} "
            f"session={self.settings.session_id}",
            flush=True,
        )
        return True

    def update(
        self,
        snapshot: session_state.SessionSnapshot,
        active_party: Sequence[tuple[int, str, int, int]],
        selection: menu_themes.ThemeSelection,
        *,
        presentation_visible: bool,
        foreground: bool,
        now: float,
    ) -> None:
        settings = self.settings_tracker.reload(now=now)
        if settings != self.settings:
            previous = self.settings
            self._close_active()
            self.settings = settings
            self.retry_at = float(now)
            self.last_start_error = None
            if previous.enabled and not settings.enabled:
                print("DAMAGE_METER OFF", flush=True)
            else:
                print(
                    "DAMAGE_METER CONFIG "
                    f"enabled={int(settings.enabled)} "
                    f"mode={settings.mode} "
                    f"effective={int(settings.effective_only)} "
                    f"session={settings.session_id}",
                    flush=True,
                )

        if not self.settings.enabled:
            return
        if self.meter is None:
            if float(now) < self.retry_at or not self._start(
                foreground=foreground,
                now=now,
            ):
                return

        try:
            panel = self.panel
            meter = self.meter
            if panel is None or meter is None:
                raise RuntimeError(
                    "damage meter start completed without live components"
                )
            panel.set_game_foreground(foreground)
            meter.update(
                snapshot,
                active_party,
                selection,
                presentation_visible=presentation_visible,
                now=now,
            )
        except (OSError, RuntimeError, ValueError, struct.error) as error:
            print(
                f"DAMAGE_METER RESTARTING {type(error).__name__}: {error}",
                flush=True,
            )
            self._close_active()
            self.retry_at = float(now) + RUNTIME_RETRY_SECONDS

    def close(self) -> None:
        self._close_active()


__all__ = [
    "AREA_REAPPEAR_DELAY",
    "AreaTransitionGate",
    "DamageEvent",
    "DamageHook",
    "DamageMeter",
    "DamageMeterSettings",
    "DamageMeterSettingsTracker",
    "DamageSessionStore",
    "DamageRow",
    "DamageTotals",
    "LiveDamageMeter",
    "MODE_AREA",
    "MODE_INDEFINITE",
    "MODES",
    "ROW_REORDER_DURATION",
    "ResidentDamageHook",
    "RowAnimator",
    "RowMotion",
    "active_actor_owners",
    "build_hook_header",
    "build_proxy_image",
    "inspect_resident_damage_hook",
    "materialized_party_members",
    "build_wrapper",
    "rank_rows",
    "render_meter",
]
