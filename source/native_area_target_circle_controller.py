from __future__ import annotations

import math
import struct
import threading
import time

import coop_runtime as coop
import native_ui_tick_mux as ui_tick_mux
from native_target_circle_router import (
    TARGET_UI_MANAGER_GLOBAL,
    _patch_rel32,
)


AREA_CIRCLE_FACTORY = 0x2911D0
AREA_CIRCLE_APPEND = 0x3F34D0
AREA_CIRCLE_LIST_OFFSET = 0x10F40
SETTINGS_GLOBAL = 0x215E6E0
SETTINGS_FLAGS_OFFSET = 0x04
AREA_RENDER_ENABLED = 1 << 22

REGION_MAGIC = b"FF12NAO2"
REGION_SIZE = 0x1000
CODE_OFFSET = ui_tick_mux.AREA_CIRCLE_HANDLER_OFFSET
STATE_OFFSET = 0x800
STATE_SIZE = REGION_SIZE - STATE_OFFSET

AREA_SEQUENCE_OFFSET = 0x04
AREA_NODE_OFFSET = 0x08
AREA_TARGET_OFFSET = 0x10
AREA_PALETTE_OFFSET = 0x14
AREA_RADIUS_OFFSET = 0x18
AREA_OBSERVED_TARGET_OFFSET = 0x1C
AREA_SAVED_LIST_OFFSET = 0x20
AREA_ACK_SEQUENCE_OFFSET = 0x28
AREA_OWNER_TAG_OFFSET = 0x2C
AREA_STATE_MAGIC_OFFSET = 0x40
AREA_STATE_MAGIC = b"AOCMBX2!"

P1_OWNER_TAG = int.from_bytes(b"AOP1", "little")
P2_OWNER_TAG = int.from_bytes(b"AOP2", "little")
P3_OWNER_TAG = int.from_bytes(b"AOP3", "little")


# All P1/P2/P3 instances deliberately share one owner-neutral callback and
# mailbox.  This lock makes the idle-check plus publication indivisible among
# Python controller threads; the game-thread callback still synchronizes via
# the state 0 -> 1 -> 2 -> 3/4 protocol.
_SERVICE_LOCK = threading.RLock()


class NativeAreaRenderGate:
    MAINTAIN_SECONDS = 0.10

    def __init__(self, process: coop.RemoteProcess) -> None:
        self.process = process
        self.address = 0
        self.saved_enabled = False
        self.owned = False
        self.last_written = 0
        self.next_maintenance = 0.0

    def _flags_address(self) -> int:
        settings = self.process.u64(SETTINGS_GLOBAL)
        return settings + SETTINGS_FLAGS_OFFSET if settings else 0

    def _release(self) -> None:
        if not self.owned:
            return
        try:
            live_address = self._flags_address()
            flags = self.process.u32(self.address) if live_address == self.address else 0
            if (
                live_address == self.address
                and not self.saved_enabled
                and flags == self.last_written
                and flags & AREA_RENDER_ENABLED
            ):
                self.process.write(
                    self.address,
                    struct.pack("<I", flags & ~AREA_RENDER_ENABLED),
                )
        except OSError:
            pass
        self.address = 0
        self.saved_enabled = False
        self.owned = False
        self.last_written = 0
        self.next_maintenance = 0.0

    def sync(self, active: bool) -> bool:
        if not active:
            self._release()
            return False

        try:
            address = self._flags_address()
            if not address:
                return False
            if self.owned and address != self.address:
                self._release()
            if not self.owned:
                flags = self.process.u32(address)
                self.address = address
                self.saved_enabled = bool(flags & AREA_RENDER_ENABLED)
                self.owned = True
            now = time.monotonic()
            if now >= self.next_maintenance:
                flags = self.process.u32(address)
                if not flags & AREA_RENDER_ENABLED:
                    target = flags | AREA_RENDER_ENABLED
                    self.process.write(
                        address,
                        struct.pack("<I", target),
                    )
                    self.last_written = target
                self.next_maintenance = now + self.MAINTAIN_SECONDS
            return True
        except OSError:
            self.next_maintenance = 0.0
            return False

    def clear(self) -> None:
        self._release()

    def restore(self) -> None:
        self._release()


def build_create_dispatcher(
    cave: int,
    state: int,
    owner_tag: int | None = None,
) -> bytes:
    """Build the owner-neutral permanent AoE callback.

    ``owner_tag`` remains accepted for source compatibility, but ownership is
    read from the stable request mailbox so P1/P2/P3 never rewrite this cave.
    """

    del owner_tag
    code = bytearray()
    labels: dict[str, int] = {}
    fixups: list[tuple[int, str]] = []

    def mark(name: str) -> None:
        labels[name] = len(code)

    def branch(opcode: bytes, target: str) -> None:
        code.extend(opcode)
        fixups.append((len(code), target))
        code.extend(b"\x00" * 4)

    code += b"\x48\xb8" + struct.pack("<Q", int(state))
    code += b"\x83\x38\x01"
    branch(b"\x0f\x85", "idle")
    code += b"\xc7\x00\x02\x00\x00\x00"

    # Four nonvolatile saves retain callback-entry alignment; 0x28 supplies
    # native shadow space and aligns both factory and append calls.
    code += b"\x53\x55\x56\x57"
    code += b"\x48\x83\xec\x28"
    code += b"\x48\xbd" + struct.pack("<Q", int(state))
    code += b"\x48\xb8" + struct.pack("<Q", TARGET_UI_MANAGER_GLOBAL)
    code += b"\x48\x8b\x18"
    code += b"\x8b\x7d" + bytes((AREA_TARGET_OFFSET,))
    code += b"\x8b\x75" + bytes((AREA_PALETTE_OFFSET,))
    code += b"\x48\x85\xdb"
    branch(b"\x0f\x84", "error")
    code += b"\x85\xff"
    branch(b"\x0f\x84", "error")
    code += b"\x83\xfe\x06"
    branch(b"\x0f\x87", "error")

    # Reject zero, negative, NaN and infinite radii inside the game-thread
    # callback as well as in Python.  Loading cannot turn a malformed mailbox
    # into a factory call.
    code += b"\x8b\x45" + bytes((AREA_RADIUS_OFFSET,))
    code += b"\x85\xc0"
    branch(b"\x0f\x8e", "error")
    code += b"\x25\x00\x00\x80\x7f\x3d\x00\x00\x80\x7f"
    branch(b"\x0f\x84", "error")

    # The only accepted ownership identities are the three stable player tags.
    code += b"\x8b\x45" + bytes((AREA_OWNER_TAG_OFFSET,))
    for tag in (P1_OWNER_TAG, P2_OWNER_TAG):
        code += b"\x3d" + struct.pack("<I", tag)
        branch(b"\x0f\x84", "owner_valid")
    code += b"\x3d" + struct.pack("<I", P3_OWNER_TAG)
    branch(b"\x0f\x85", "error")

    mark("owner_valid")

    code += b"\x48\x8b\x83" + struct.pack("<I", AREA_CIRCLE_LIST_OFFSET)
    code += b"\x48\x89\x45" + bytes((AREA_SAVED_LIST_OFFSET,))
    code += b"\x48\xc7\x83" + struct.pack("<I", AREA_CIRCLE_LIST_OFFSET)
    code += b"\x00\x00\x00\x00"

    code += b"\x48\x8b\xcb"
    code += b"\x8b\xd7"
    code += b"\xf3\x0f\x10\x55" + bytes((AREA_RADIUS_OFFSET,))
    code += b"\x41\xb9\x03\x00\x00\x00"
    code += b"\x49\xba" + struct.pack("<Q", AREA_CIRCLE_FACTORY)
    code += b"\x41\xff\xd2"
    code += b"\x48\x89\x45" + bytes((AREA_NODE_OFFSET,))
    code += b"\x48\x85\xc0"
    branch(b"\x0f\x84", "restore_error")

    code += b"\x89\x70\x48"
    code += b"\x8b\x55" + bytes((AREA_OWNER_TAG_OFFSET,))
    code += b"\x89\x50\x54"
    code += b"\x89\x7d" + bytes((AREA_OBSERVED_TARGET_OFFSET,))

    code += b"\x48\x8b\x4d" + bytes((AREA_SAVED_LIST_OFFSET,))
    code += b"\x48\x85\xc9"
    branch(b"\x0f\x84", "keep_new")
    code += b"\x48\x8b\x55" + bytes((AREA_NODE_OFFSET,))
    code += b"\x49\xba" + struct.pack("<Q", AREA_CIRCLE_APPEND)
    code += b"\x41\xff\xd2"
    code += b"\x48\x85\xc0"
    branch(b"\x0f\x84", "restore_error")
    code += b"\x48\x89\x83" + struct.pack("<I", AREA_CIRCLE_LIST_OFFSET)
    branch(b"\xe9", "success")

    mark("keep_new")
    code += b"\x48\x8b\x45" + bytes((AREA_NODE_OFFSET,))
    code += b"\x48\x89\x83" + struct.pack("<I", AREA_CIRCLE_LIST_OFFSET)
    branch(b"\xe9", "success")

    mark("restore_error")
    code += b"\x48\x8b\x45" + bytes((AREA_SAVED_LIST_OFFSET,))
    code += b"\x48\x89\x83" + struct.pack("<I", AREA_CIRCLE_LIST_OFFSET)
    branch(b"\xe9", "error")

    mark("success")
    code += b"\xb8\x03\x00\x00\x00"
    branch(b"\xe9", "publish")

    mark("error")
    code += b"\xb8\x04\x00\x00\x00"

    mark("publish")
    code += b"\x8b\x55" + bytes((AREA_SEQUENCE_OFFSET,))
    code += b"\x89\x55" + bytes((AREA_ACK_SEQUENCE_OFFSET,))
    # State is the final mailbox access before stack-only cleanup and RET.
    code += b"\x89\x45\x00"
    code += b"\x48\x83\xc4\x28\x5f\x5e\x5d\x5b\xc3"

    mark("idle")
    code += b"\xc3"

    for offset, target in fixups:
        if target not in labels:
            raise RuntimeError(f"unknown native AoE callback label {target!r}")
        _patch_rel32(code, offset, int(cave), labels[target])
    return bytes(code)


def allocate_region(process: coop.RemoteProcess) -> int:
    base = process.ensure_damage_number_theme_allocation()
    return base + coop.NATIVE_AREA_TARGET_WORKSPACE_OFFSET


class NativeAreaTargetCircleController:
    RETRY_SECONDS = 0.04
    MAINTAIN_SECONDS = 0.10

    def __init__(
        self,
        process: coop.RemoteProcess,
        *,
        owner_tag: int,
        palette: int,
    ) -> None:
        self.process = process
        self.owner_tag = int(owner_tag) & 0xFFFFFFFF
        if self.owner_tag not in (P1_OWNER_TAG, P2_OWNER_TAG, P3_OWNER_TAG):
            raise ValueError("AoE owner tag must identify P1, P2 or P3")
        self.palette = self._checked_palette(palette)
        self.region = allocate_region(process)
        self.cave = self.region + CODE_OFFSET
        self.state = self.region + STATE_OFFSET
        self.uses_shared_cave = True
        self.node = 0
        self.target = 0
        self.next_retry = 0.0
        self.next_maintenance = 0.0
        self.published_radius = 0.0
        self.published_collision_radius: float | None = None
        self.published_palette: int | None = None
        self.patched = False
        self.quarantined = False
        self.owns_static = False
        self._dispatch_code = build_create_dispatcher(self.cave, self.state)
        self._dispatch_patch: bytes | None = None
        self._sequence = 0
        damage_base = self.region - coop.NATIVE_AREA_TARGET_WORKSPACE_OFFSET
        self.mux = ui_tick_mux.NativeUITickMux(process, damage_base)
        self._install_handler()
        try:
            self._recover(self._nodes())
        except OSError:
            pass

    def _static_image(self) -> bytes:
        image = bytearray(STATE_OFFSET)
        struct.pack_into(
            "<8sIIII",
            image,
            0,
            REGION_MAGIC,
            2,
            REGION_SIZE,
            CODE_OFFSET,
            STATE_OFFSET,
        )
        if CODE_OFFSET + len(self._dispatch_code) > STATE_OFFSET:
            raise RuntimeError("native AoE callback exceeds its immutable reservation")
        image[CODE_OFFSET : CODE_OFFSET + len(self._dispatch_code)] = (
            self._dispatch_code
        )
        return bytes(image)

    @staticmethod
    def _valid_state(image: bytes) -> bool:
        if (
            len(image) != STATE_SIZE
            or image[
                AREA_STATE_MAGIC_OFFSET : AREA_STATE_MAGIC_OFFSET
                + len(AREA_STATE_MAGIC)
            ]
            != AREA_STATE_MAGIC
            or any(image[0x30:AREA_STATE_MAGIC_OFFSET])
            or any(image[AREA_STATE_MAGIC_OFFSET + len(AREA_STATE_MAGIC) :])
        ):
            return False
        status = struct.unpack_from("<I", image, 0)[0]
        if status not in (0, 1, 2, 3, 4):
            return False
        sequence = struct.unpack_from("<I", image, AREA_SEQUENCE_OFFSET)[0]
        ack = struct.unpack_from("<I", image, AREA_ACK_SEQUENCE_OFFSET)[0]
        if status:
            owner_tag = struct.unpack_from("<I", image, AREA_OWNER_TAG_OFFSET)[0]
            if owner_tag not in (P1_OWNER_TAG, P2_OWNER_TAG, P3_OWNER_TAG):
                return False
            if not sequence:
                return False
        if status == 1 and ack != 0:
            return False
        if status == 2 and ack not in (0, sequence):
            return False
        if status in (3, 4) and ack != sequence:
            return False
        if status == 0 and ack not in (0, sequence):
            return False
        return True

    @staticmethod
    def _initial_state_image() -> bytes:
        image = bytearray(STATE_SIZE)
        image[
            AREA_STATE_MAGIC_OFFSET : AREA_STATE_MAGIC_OFFSET
            + len(AREA_STATE_MAGIC)
        ] = AREA_STATE_MAGIC
        return bytes(image)

    def _install_handler(self) -> None:
        with _SERVICE_LOCK:
            self._install_handler_locked()

    def _install_handler_locked(self) -> None:
        expected = self._static_image()
        current = self.process.read(self.region, len(expected))
        state_image = self.process.read(self.state, STATE_SIZE)
        slot_address = self.mux.slot_address(ui_tick_mux.SLOT_AREA_CIRCLE)
        slot = self.process.read(slot_address, 8)
        expected_slot = struct.pack("<Q", self.cave)

        initial_state = self._initial_state_image()
        fresh = bool(
            current == b"\x00" * len(expected)
            and state_image == b"\x00" * STATE_SIZE
            and slot == b"\x00" * 8
        )
        staged = bool(
            current == expected
            and state_image == b"\x00" * STATE_SIZE
            and slot == b"\x00" * 8
        )
        prepared = bool(
            current == expected
            and state_image == initial_state
            and slot == b"\x00" * 8
        )
        adopted = bool(
            current == expected
            and slot == expected_slot
            and self._valid_state(state_image)
        )
        if not (fresh or staged or prepared or adopted):
            raise RuntimeError(
                "native AoE callback topology is partial or foreign; "
                "restart the game"
            )

        # Refuse a foreign local reservation before installing the shared
        # broker/root.  This keeps the whole multi-image preflight read-only.
        self.mux.ensure_installed()

        if fresh:
            try:
                self.process.patch_code(self.region, expected)
            except BaseException as error:
                raise RuntimeError(
                    "native AoE callback preparation failed; restart the game"
                ) from error
            self.owns_static = True
            if self.process.read(self.region, len(expected)) != expected:
                raise RuntimeError(
                    "native AoE callback preparation did not persist; "
                    "restart the game"
                )

        if fresh or staged:
            try:
                self.process.write(self.state, initial_state)
            except BaseException as error:
                raise RuntimeError(
                    "native AoE mailbox preparation failed; restart the game"
                ) from error
            self.owns_static = True
            if self.process.read(self.state, STATE_SIZE) != initial_state:
                raise RuntimeError(
                    "native AoE mailbox preparation did not persist; "
                    "restart the game"
                )

        if not adopted:
            if not (
                self.mux.resident_exact()
                and self.process.read(self.region, len(expected)) == expected
                and self.process.read(self.state, STATE_SIZE) == initial_state
                and self.process.read(slot_address, 8) == b"\x00" * 8
            ):
                raise RuntimeError(
                    "native AoE topology changed before callback publication; "
                    "restart the game"
                )
            try:
                self.mux.register_handler(ui_tick_mux.SLOT_AREA_CIRCLE, self.cave)
            except BaseException as error:
                raise RuntimeError(
                    "native AoE callback publication failed; restart the game"
                ) from error
            self.owns_static = True

        if not self.resident_exact():
            raise RuntimeError(
                "native AoE callback publication could not be verified; "
                "restart the game"
            )

        self._dispatch_patch = self.mux.patch
        state_image = self.process.read(self.state, STATE_SIZE)
        if not self._valid_state(state_image):
            raise RuntimeError("native AoE mailbox is invalid; restart the game")
        self._sequence = struct.unpack_from(
            "<I", state_image, AREA_SEQUENCE_OFFSET
        )[0]
        status = struct.unpack_from("<I", state_image, 0)[0]
        self.patched = status in (1, 2)
        self.quarantined = self.patched

    def resident_exact(self) -> bool:
        try:
            expected = self._static_image()
            return bool(
                self.mux.handler_registered(
                    ui_tick_mux.SLOT_AREA_CIRCLE,
                    self.cave,
                )
                and self.process.read(self.region, len(expected)) == expected
            )
        except OSError:
            return False

    def maintain(self) -> bool:
        # Compatibility for narrow state-only test doubles.  Production
        # instances always own a mux and therefore take the exact resident
        # topology proof below.
        if not hasattr(self, "mux"):
            return not getattr(self, "patched", False)
        if not self.resident_exact():
            self.quarantined = True
            return False
        try:
            state_image = self.process.read(self.state, STATE_SIZE)
        except OSError:
            self.quarantined = True
            return False
        if not self._valid_state(state_image):
            self.quarantined = True
            return False
        dispatch_state = struct.unpack_from("<I", state_image)[0]
        if dispatch_state in (1, 2):
            self.patched = True
            self.quarantined = True
            return False
        if dispatch_state not in (0, 3, 4):
            self.quarantined = True
            return False
        self.patched = False
        self.quarantined = False
        return True

    def _arm(
        self,
        payload: bytes | bytearray,
        code: bytes | None = None,
    ) -> None:
        del code  # compatibility with the retired owner-specific one-shot cave
        with _SERVICE_LOCK:
            if not self.maintain():
                raise RuntimeError("native AoE dispatcher is still in flight")
            payload = bytes(payload)
            if len(payload) > AREA_STATE_MAGIC_OFFSET:
                raise RuntimeError(
                    "native AoE mailbox payload exceeds its declared fields "
                    f"({len(payload)} > {AREA_STATE_MAGIC_OFFSET})"
                )
            sequence = (self._sequence + 1) & 0xFFFFFFFF
            sequence = sequence or 1
            mailbox = bytearray(STATE_SIZE)
            mailbox[: len(payload)] = payload
            struct.pack_into("<I", mailbox, 0, 0)
            struct.pack_into("<I", mailbox, AREA_SEQUENCE_OFFSET, sequence)
            struct.pack_into("<I", mailbox, AREA_ACK_SEQUENCE_OFFSET, 0)
            struct.pack_into("<I", mailbox, AREA_OWNER_TAG_OFFSET, self.owner_tag)
            mailbox[
                AREA_STATE_MAGIC_OFFSET : AREA_STATE_MAGIC_OFFSET
                + len(AREA_STATE_MAGIC)
            ] = AREA_STATE_MAGIC

            self.patched = True
            self.quarantined = False
            try:
                self.process.write(self.state, mailbox)
                self.process.write(self.state, struct.pack("<I", 1))
            except BaseException as error:
                self.quarantined = True
                raise RuntimeError("native AoE mailbox publication failed") from error
            self._sequence = sequence

    def _result_ready(self) -> bool:
        return self.process.u32(self.state + AREA_ACK_SEQUENCE_OFFSET) == self._sequence

    @staticmethod
    def _checked_palette(palette: int) -> int:
        palette = int(palette)
        if not 0 <= palette <= 6:
            raise ValueError(f"invalid native AoE CLUT {palette}")
        return palette

    def _nodes(self) -> list[int]:
        manager = self.process.u64(TARGET_UI_MANAGER_GLOBAL)
        node = self.process.u64(manager + AREA_CIRCLE_LIST_OFFSET) if manager else 0
        result: list[int] = []
        seen: set[int] = set()

        while node and node not in seen and len(result) < 64:
            seen.add(node)
            result.append(node)
            node = self.process.u64(node + 0x70)
        return result

    def _recover(self, nodes: list[int]) -> None:
        if self.node in nodes:
            return
        self.node = 0
        for node in nodes:
            if self.process.u32(node + 0x54) == self.owner_tag:
                self.node = node
                self.target = self.process.u32(node + 0x6C)
                return

    def _create(self, target: int, radius: float) -> int:
        payload = bytearray(0x40)
        struct.pack_into("<I", payload, AREA_TARGET_OFFSET, target & 0xFFFFFFFF)
        struct.pack_into("<I", payload, AREA_PALETTE_OFFSET, self.palette)
        struct.pack_into("<f", payload, AREA_RADIUS_OFFSET, float(radius))
        try:
            self._arm(payload)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                state = self.process.u32(self.state)
                if state == 3:
                    if not self._result_ready():
                        raise RuntimeError(
                            "native AoE creator returned a stale acknowledgement"
                        )
                    node = self.process.u64(self.state + AREA_NODE_OFFSET)
                    observed = self.process.u32(
                        self.state + AREA_OBSERVED_TARGET_OFFSET
                    )
                    if (
                        not node
                        or observed != target
                        or self.process.u32(node + 0x54) != self.owner_tag
                    ):
                        raise RuntimeError(
                            f"native AoE create mismatch {node:#x}/{observed:#x}"
                        )
                    return node
                if state == 4:
                    raise RuntimeError("native AoE creator rejected its context")
                time.sleep(0.001)
            raise RuntimeError("native AoE creator timed out")
        finally:
            self.maintain()

    def sync(
        self,
        target: int,
        radius: float,
        *,
        palette: int | None = None,
        collision_radius: float | None = None,
    ) -> bool:
        if palette is not None:
            self.palette = self._checked_palette(palette)
        target = int(target) & 0xFFFFFFFF
        radius = float(radius)
        if not target or not math.isfinite(radius) or radius <= 0.0:
            self.clear()
            return False

        now = time.monotonic()
        collision = (
            float(collision_radius)
            if collision_radius is not None
            and math.isfinite(float(collision_radius))
            and 0.01 <= float(collision_radius) <= 100.0
            else None
        )
        if (
            self.node
            and self.target == target
            and self.published_radius == radius
            and self.published_collision_radius == collision
            and self.published_palette == self.palette
            and now < self.next_maintenance
        ):
            return True
        if not self.maintain():
            return False
        try:
            nodes = self._nodes()
            self._recover(nodes)
            if self.node and (
                self.node not in nodes
                or self.process.u32(self.node + 0x54) != self.owner_tag
            ):
                self.node = 0

            if not self.node:
                if now < self.next_retry:
                    return False
                manager = self.process.u64(TARGET_UI_MANAGER_GLOBAL)
                if not manager:
                    self.next_retry = now + self.RETRY_SECONDS
                    return False
                self.node = self._create(target, radius)
                self.target = target

            live_target = self.process.u32(self.node + 0x6C)
            flags = self.process.u32(self.node + 0x50)
            target_changed = live_target != target
            flags = (flags | 0x01) & ~0x02
            if self.process.u32(self.node + 0x48) != self.palette:
                self.process.write(
                    self.node + 0x48,
                    struct.pack("<I", self.palette),
                )
            if self.process.u32(self.node + 0x50) != flags:
                self.process.write(self.node + 0x50, struct.pack("<I", flags))
            if self.process.u32(self.node + 0x54) != self.owner_tag:
                self.process.write(
                    self.node + 0x54,
                    struct.pack("<I", self.owner_tag),
                )
            if collision is not None:
                live_collision = struct.unpack(
                    "<f", self.process.read(self.node + 0x60, 4)
                )[0]
                if live_collision != collision:
                    self.process.write(
                        self.node + 0x60,
                        struct.pack("<f", collision),
                    )
            live_radius = struct.unpack("<f", self.process.read(self.node + 0x64, 4))[0]
            if live_radius != radius:
                self.process.write(self.node + 0x64, struct.pack("<f", radius))
            if target_changed:
                self.process.write(
                    self.node + 0x68,
                    struct.pack("<f", 0.0),
                )
                self.process.write(
                    self.node + 0x6C,
                    struct.pack("<I", target),
                )
            self.target = target
            self.published_radius = radius
            self.published_collision_radius = collision
            self.published_palette = self.palette
            self.next_retry = 0.0
            self.next_maintenance = now + self.MAINTAIN_SECONDS
            return True
        except (OSError, RuntimeError):
            self.next_retry = now + self.RETRY_SECONDS
            return False

    def _hide(self, *, release_owner: bool) -> None:
        if not release_owner and not self.target:
            return
        if not self.node:
            self.target = 0
            return
        try:
            nodes = self._nodes()
            if (
                self.node in nodes
                and self.process.u32(self.node + 0x54) == self.owner_tag
            ):
                flags = self.process.u32(self.node + 0x50) & ~0x01
                self.process.write(
                    self.node + 0x50,
                    struct.pack("<I", flags),
                )
                if release_owner:
                    self.process.write(
                        self.node + 0x54,
                        struct.pack("<I", 0),
                    )
                self.process.write(
                    self.node + 0x6C,
                    struct.pack("<I", 0),
                )
        except OSError:
            pass
        self.target = 0
        self.published_radius = 0.0
        self.published_collision_radius = None
        self.published_palette = None
        self.next_maintenance = 0.0
        if release_owner:
            self.node = 0

    def clear(self) -> None:
        if not self.maintain():
            return
        self._hide(release_owner=False)

    def restore(self) -> bool:
        with _SERVICE_LOCK:
            if not self.maintain():
                return False
            self._hide(release_owner=True)
            try:
                state = getattr(self, "state", 0)
                if state:
                    self.process.write(state, b"\x00" * 4)
            except OSError:
                return False
            self.owns_static = False
            return True


__all__ = [
    "AREA_RENDER_ENABLED",
    "AREA_CIRCLE_FACTORY",
    "AREA_CIRCLE_LIST_OFFSET",
    "NativeAreaRenderGate",
    "NativeAreaTargetCircleController",
    "P1_OWNER_TAG",
    "P2_OWNER_TAG",
    "P3_OWNER_TAG",
    "build_create_dispatcher",
]
