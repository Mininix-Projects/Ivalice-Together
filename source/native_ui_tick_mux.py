from __future__ import annotations

import struct
import threading

import coop_runtime as coop


UI_MANAGER_TICK_CALL = 0x28E480
UI_MANAGER_TICK = 0x290CF0
UI_MANAGER_TICK_RETURN = UI_MANAGER_TICK_CALL + 5
ORIGINAL_TICK_CALL = b"\xe8\x6b\x28\x00\x00"

# The broker owns only the first 0x200 bytes of the target-router workspace.
# The target-circle service starts at +0x200 and remains independently
# preflighted by native_target_circle_router.
REGION_SIZE = 0x200
HEADER_SIZE = 0x100
CODE_OFFSET = 0x100
CODE_SIZE = REGION_SIZE - CODE_OFFSET
REGION_MAGIC = b"FF12UT1"
REGION_VERSION = 1

SLOT_HEADLESS_CORE = "headless-core"
SLOT_TARGET_CIRCLE = "target-circle"
SLOT_AREA_CIRCLE = "area-circle"
SLOT_OFFSETS = {
    SLOT_HEADLESS_CORE: 0x40,
    SLOT_TARGET_CIRCLE: 0x48,
    SLOT_AREA_CIRCLE: 0x50,
}

# Fixed callback locations are part of the process-lifetime layout.  A slot is
# either zero or this exact address; an arbitrary executable pointer is never
# adopted.
HEADLESS_CORE_HANDLER_OFFSET = 0xE000
TARGET_CIRCLE_HANDLER_OFFSET = 0x300
AREA_CIRCLE_HANDLER_OFFSET = 0x100


# Installation and slot publication are rare, process-lifetime operations.
# One module-wide re-entrant lock is deliberately stronger than a per-object
# lock: independent service constructors still share the same root and slot
# table, even though each one creates its own NativeUITickMux facade.
_TOPOLOGY_LOCK = threading.RLock()


def _rel32(source: int, size: int, target: int) -> bytes:
    return struct.pack("<i", int(target) - (int(source) + int(size)))


def build_mux(cave: int, slot_addresses: tuple[int, ...]) -> bytes:
    """Build the permanent UI-tick fan-out.

    The replaced instruction was a CALL made with the caller's aligned stack.
    Preserve every Win64 argument register, including the four scalar/vector
    argument lanes, while callbacks run.  Each registered callback is a
    no-argument RET function and must preserve nonvolatile registers itself.
    """

    code = bytearray()
    fixups: list[tuple[int, int]] = []

    # Four pushes retain the entry alignment.  The frame provides Win64 shadow
    # space plus four 16-byte SIMD save slots before every callback call.
    code += b"\x51\x52\x41\x50\x41\x51"  # rcx, rdx, r8, r9
    code += b"\x48\x83\xec\x60"
    for modrm, offset in zip((0x44, 0x4C, 0x54, 0x5C), (0x20, 0x30, 0x40, 0x50)):
        code += b"\xf3\x0f\x7f" + bytes((modrm, 0x24, offset))

    for slot_address in slot_addresses:
        code += b"\x48\xb8" + struct.pack("<Q", int(slot_address))
        code += b"\x48\x8b\x00\x48\x85\xc0"
        code += b"\x0f\x84"
        displacement = len(code)
        code += b"\x00" * 4
        code += b"\xff\xd0"
        fixups.append((displacement, len(code)))

    for modrm, offset in zip((0x44, 0x4C, 0x54, 0x5C), (0x20, 0x30, 0x40, 0x50)):
        code += b"\xf3\x0f\x6f" + bytes((modrm, 0x24, offset))
    code += b"\x48\x83\xc4\x60"
    code += b"\x41\x59\x41\x58\x5a\x59"

    call_source = int(cave) + len(code)
    code += b"\xe8" + _rel32(call_source, 5, UI_MANAGER_TICK)
    jump_source = int(cave) + len(code)
    code += b"\xe9" + _rel32(jump_source, 5, UI_MANAGER_TICK_RETURN)

    for displacement, target_offset in fixups:
        struct.pack_into("<i", code, displacement, target_offset - (displacement + 4))
    if len(code) > CODE_SIZE:
        raise RuntimeError(f"native UI tick mux overflow ({len(code)} > {CODE_SIZE})")
    return bytes(code)


class NativeUITickMux:
    def __init__(self, process, damage_base: int) -> None:
        self.process = process
        self.damage_base = int(damage_base)
        self.region = (
            self.damage_base + coop.NATIVE_TARGET_ROUTER_WORKSPACE_OFFSET
        )
        self.cave = self.region + CODE_OFFSET
        self.patch = b"\xe9" + _rel32(UI_MANAGER_TICK_CALL, 5, self.cave)
        self.installed = False
        self.owns_static = False

    def _slot_address(self, slot: str) -> int:
        try:
            return self.region + SLOT_OFFSETS[str(slot)]
        except KeyError as error:
            raise ValueError(f"unknown native UI tick mux slot {slot!r}") from error

    def slot_address(self, slot: str) -> int:
        return self._slot_address(slot)

    def handler(self, slot: str) -> int:
        return struct.unpack(
            "<Q", self.process.read(self._slot_address(slot), 8)
        )[0]

    def _expected_handler(self, slot: str, address: int) -> bool:
        address = int(address)
        if slot == SLOT_TARGET_CIRCLE:
            return address == self.region + TARGET_CIRCLE_HANDLER_OFFSET
        if slot == SLOT_AREA_CIRCLE:
            return address == (
                self.damage_base
                + coop.NATIVE_AREA_TARGET_WORKSPACE_OFFSET
                + AREA_CIRCLE_HANDLER_OFFSET
            )
        if slot != SLOT_HEADLESS_CORE:
            return False
        # Resolve the primary allocation read-only instead of accepting any
        # merely aligned +E000 address.  The dual-interaction allocation has
        # its own independent +E000 range and is never authoritative here.
        try:
            primary_base = int(
                self.process.ensure_cave_allocation(allow_allocate=False)
            )
        except (AttributeError, OSError, TypeError, ValueError):
            return False
        return bool(
            primary_base
            and address == primary_base + HEADLESS_CORE_HANDLER_OFFSET
        )

    def _static_image(self) -> bytes:
        image = bytearray(REGION_SIZE)
        struct.pack_into(
            "<8sIIII",
            image,
            0,
            REGION_MAGIC,
            REGION_VERSION,
            REGION_SIZE,
            CODE_OFFSET,
            len(SLOT_OFFSETS),
        )
        code = build_mux(
            self.cave,
            tuple(self._slot_address(slot) for slot in SLOT_OFFSETS),
        )
        image[CODE_OFFSET : CODE_OFFSET + len(code)] = code
        return bytes(image)

    @staticmethod
    def _without_slots(image: bytes) -> bytes:
        normalized = bytearray(image)
        for offset in SLOT_OFFSETS.values():
            normalized[offset : offset + 8] = b"\x00" * 8
        return bytes(normalized)

    def _validate_slots(self, image: bytes) -> None:
        for slot, offset in SLOT_OFFSETS.items():
            address = struct.unpack_from("<Q", image, offset)[0]
            if address and not self._expected_handler(slot, address):
                raise RuntimeError(
                    f"native UI tick mux slot {slot!r} is foreign: {address:#x}"
                )

    def _validate_exact_region(self, image: bytes) -> None:
        expected = self._static_image()
        if self._without_slots(image) != expected:
            raise RuntimeError("native UI tick mux region mismatch; restart the game")
        self._validate_slots(image)

    def ensure_installed(self) -> NativeUITickMux:
        with _TOPOLOGY_LOCK:
            return self._ensure_installed_locked()

    def _ensure_installed_locked(self) -> NativeUITickMux:
        root = self.process.read(UI_MANAGER_TICK_CALL, len(ORIGINAL_TICK_CALL))
        region_image = self.process.read(self.region, REGION_SIZE)
        root_stock = root == ORIGINAL_TICK_CALL
        root_patched = root == self.patch
        if not root_stock and not root_patched:
            raise RuntimeError(
                "native UI tick root is occupied: " + root.hex(" ")
            )

        fresh = region_image == b"\x00" * REGION_SIZE
        if not fresh:
            self._validate_exact_region(region_image)
            if root_stock and any(
                struct.unpack_from("<Q", region_image, offset)[0]
                for offset in SLOT_OFFSETS.values()
            ):
                raise RuntimeError(
                    "native UI tick mux has published handlers without its root; "
                    "restart the game"
                )

        if root_patched:
            if fresh:
                raise RuntimeError(
                    "native UI tick root is published without its mux region; "
                    "restart the game"
                )
            self.installed = True
            self.owns_static = False
            return self

        if fresh:
            try:
                # patch_code supplies the required instruction-cache flush.
                # The root is still stock, so the full image cannot execute
                # while it is being prepared.
                self.process.patch_code(self.region, self._static_image())
            except BaseException as error:
                raise RuntimeError(
                    "native UI tick mux preparation failed; restart the game"
                ) from error

        # A successful WriteProcessMemory return is not the publication proof.
        # Re-read the whole reservation and root immediately before exposing
        # the cave to the game thread.  This also catches short/faulty writes
        # and mutations by an unrelated injector without trying to repair
        # executable memory that may already have been entered.
        try:
            region_image = self.process.read(self.region, REGION_SIZE)
            self._validate_exact_region(region_image)
            root = self.process.read(
                UI_MANAGER_TICK_CALL,
                len(ORIGINAL_TICK_CALL),
            )
        except BaseException as error:
            raise RuntimeError(
                "native UI tick mux changed before root publication; "
                "restart the game"
            ) from error
        if any(
            struct.unpack_from("<Q", region_image, offset)[0]
            for offset in SLOT_OFFSETS.values()
        ):
            raise RuntimeError(
                "native UI tick mux handler appeared before root publication; "
                "restart the game"
            )
        if root == self.patch:
            self.installed = True
            return self
        if root != ORIGINAL_TICK_CALL:
            raise RuntimeError(
                "native UI tick root changed before publication; restart the game"
            )

        # A stock root plus one complete exact region is a safe prepared state:
        # no thread can have entered it, and publishing the sole root finishes
        # the transaction.  Never erase or rewrite that prepared image.
        self.owns_static = True
        try:
            self.process.patch_code(UI_MANAGER_TICK_CALL, self.patch)
        except BaseException as error:
            raise RuntimeError(
                "native UI tick mux root publication failed; restart the game"
            ) from error
        try:
            if self.process.read(UI_MANAGER_TICK_CALL, 5) != self.patch:
                raise RuntimeError("root publication did not persist")
            published_region = self.process.read(self.region, REGION_SIZE)
            self._validate_exact_region(published_region)
            if any(
                struct.unpack_from("<Q", published_region, offset)[0]
                for offset in SLOT_OFFSETS.values()
            ):
                raise RuntimeError("handler appeared during root publication")
        except BaseException as error:
            raise RuntimeError(
                "native UI tick mux publication could not be verified; "
                "restart the game"
            ) from error
        self.installed = True
        return self

    def resident_exact(self) -> bool:
        try:
            if self.process.read(UI_MANAGER_TICK_CALL, 5) != self.patch:
                return False
            image = self.process.read(self.region, REGION_SIZE)
            self._validate_exact_region(image)
            return True
        except (OSError, RuntimeError):
            return False

    def handler_registered(self, slot: str, address: int) -> bool:
        if not self.resident_exact() or not self._expected_handler(slot, address):
            return False
        return self.process.read(self._slot_address(slot), 8) == struct.pack(
            "<Q", int(address)
        )

    def register_handler(self, slot: str, address: int) -> bool:
        with _TOPOLOGY_LOCK:
            return self._register_handler_locked(slot, address)

    def _register_handler_locked(self, slot: str, address: int) -> bool:
        slot = str(slot)
        address = int(address)
        if not self.installed or not self.resident_exact():
            raise RuntimeError("native UI tick mux is not resident and exact")
        if not self._expected_handler(slot, address):
            raise RuntimeError(
                f"invalid native UI tick handler address for {slot!r}: {address:#x}"
            )
        slot_address = self._slot_address(slot)
        current = self.process.read(slot_address, 8)
        expected = struct.pack("<Q", address)
        if current == expected:
            return False
        if current != b"\x00" * 8:
            raise RuntimeError(
                f"native UI tick mux slot {slot!r} is already occupied"
            )

        # The caller must prepare and verify its complete immutable callback
        # image before this aligned, one-way pointer publication.
        self.process.write(slot_address, expected)
        try:
            if not self.handler_registered(slot, address):
                raise RuntimeError("handler publication did not persist")
        except BaseException as error:
            raise RuntimeError(
                f"native UI tick mux slot {slot!r} publication could not be "
                "verified; restart the game"
            ) from error
        return True

    def restore(self) -> None:
        # Root, mux cave and callback pointers are process-lifetime.  Individual
        # services disarm only their own mutable mailboxes.
        self.installed = False
        self.owns_static = False


def ensure_installed(process) -> NativeUITickMux:
    base = process.ensure_damage_number_theme_allocation()
    return NativeUITickMux(process, base).ensure_installed()


def register_handler(process, slot: str, address: int) -> NativeUITickMux:
    mux = ensure_installed(process)
    mux.register_handler(slot, address)
    return mux


__all__ = [
    "AREA_CIRCLE_HANDLER_OFFSET",
    "CODE_OFFSET",
    "HEADLESS_CORE_HANDLER_OFFSET",
    "NativeUITickMux",
    "ORIGINAL_TICK_CALL",
    "REGION_MAGIC",
    "REGION_SIZE",
    "SLOT_AREA_CIRCLE",
    "SLOT_HEADLESS_CORE",
    "SLOT_TARGET_CIRCLE",
    "TARGET_CIRCLE_HANDLER_OFFSET",
    "UI_MANAGER_TICK",
    "UI_MANAGER_TICK_CALL",
    "UI_MANAGER_TICK_RETURN",
    "build_mux",
    "ensure_installed",
    "register_handler",
]
