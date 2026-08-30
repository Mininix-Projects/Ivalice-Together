from __future__ import annotations

import struct
import time

import coop_runtime as coop


UI_MANAGER_GLOBAL = 0x0209BE80
COMMAND_SELECTION_FLAGS_OFFSET = 0xA09A
COMMAND_SELECTION_ACTIVE_MASK = 0x08

SETTINGS_POINTER_GLOBAL = 0x0215E6E0
BATTLE_MODE_ACTIVE_MASK = 0x08

WAIT_MENU_DESIRED_OFFSET = coop.WAIT_MENU_DESIRED_OFFSET
WAIT_MENU_APPLIED_OFFSET = coop.WAIT_MENU_APPLIED_OFFSET
COMMAND_BLOCK_CAVE_OFFSET = 0x200
NATIVE_COMMAND_ACTOR_GLOBAL = coop.NATIVE_COMMAND_ACTOR_GLOBAL
NATIVE_COMMAND_FLAG_GLOBAL = coop.NATIVE_COMMAND_FLAG_GLOBAL
WAIT_MENU_CLOSE_TIMEOUT_SECONDS = 0.5

WAIT_PREDICATE_HOOK_SITE = 0x0022AA50
WAIT_PREDICATE_ORIGINAL_FUNCTION = 0x0028F6A0
WAIT_PREDICATE_ORIGINAL = bytes.fromhex("E8 4B 4C 06 00")
WAIT_PREDICATE_STATE_HOOK_SITE = 0x002E2344
WAIT_PREDICATE_STATE_ORIGINAL = bytes.fromhex("E8 57 D3 FA FF")
WAIT_PREDICATE_SITES = (
    (WAIT_PREDICATE_HOOK_SITE, WAIT_PREDICATE_ORIGINAL),
    (WAIT_PREDICATE_STATE_HOOK_SITE, WAIT_PREDICATE_STATE_ORIGINAL),
)
WAIT_PREDICATE_PATCH_SIZE = 5

WAIT_PREDICATE_CAVE_MAGIC = b"F12WPR03"
WAIT_PREDICATE_CAVE_SIZE = 0x1000
WAIT_PREDICATE_CAVE_CODE_OFFSET = 0x100
WAIT_PREDICATE_CAVE_FLAG_OFFSET = 0x200


def command_selection_value(current: int, desired: bool) -> int:
    current = int(current) & 0xFF
    if desired:
        return current | COMMAND_SELECTION_ACTIVE_MASK
    return current & ~COMMAND_SELECTION_ACTIVE_MASK


def _rel32(source_after_instruction: int, target: int) -> bytes:
    displacement = int(target) - int(source_after_instruction)
    if not -(1 << 31) <= displacement < (1 << 31):
        raise ValueError("target is outside rel32 range")
    return struct.pack("<i", displacement)


def wait_predicate_cave(code_address: int, flag_address: int) -> bytes:
    code = bytearray()
    code += b"\xe8"
    code += _rel32(
        code_address + len(code) + 4,
        WAIT_PREDICATE_ORIGINAL_FUNCTION,
    )
    code += b"\x85\xc0"
    code += b"\x75\x0a"
    code += b"\x80\x3d"
    code += _rel32(code_address + len(code) + 5, flag_address)
    code += b"\x00"
    code += b"\x0f\x95\xc0"
    code += b"\xc3"
    return bytes(code)


def wait_predicate_site_patch(
    code_address: int,
    site_address: int = WAIT_PREDICATE_HOOK_SITE,
) -> bytes:
    return b"\xe8" + _rel32(
        site_address + WAIT_PREDICATE_PATCH_SIZE,
        code_address,
    )


class NativeWaitPredicateHook:
    def __init__(self, process: coop.RemoteProcess):
        self.process = process
        self.base = 0
        self.code = 0
        self.flag = 0
        self.installed = False
        self.complete = False
        self.active = False

    def _recover_candidate(self, site_address: int) -> int:
        instruction = self.process.read(site_address, WAIT_PREDICATE_PATCH_SIZE)
        if not instruction or instruction[0] != 0xE8:
            return 0
        target = (
            site_address
            + WAIT_PREDICATE_PATCH_SIZE
            + struct.unpack("<i", instruction[1:5])[0]
        )
        candidate = target - WAIT_PREDICATE_CAVE_CODE_OFFSET
        try:
            if (
                self.process.read(candidate, len(WAIT_PREDICATE_CAVE_MAGIC))
                == WAIT_PREDICATE_CAVE_MAGIC
            ):
                return candidate
        except OSError:
            pass
        return 0

    def _recover_from_sites(self) -> int:
        recovered = {
            candidate
            for site_address, _original in WAIT_PREDICATE_SITES
            if (candidate := self._recover_candidate(site_address))
        }
        if len(recovered) > 1:
            raise RuntimeError(
                "native Wait-predicate sites point at different owned caves"
            )
        return next(iter(recovered), 0)

    def _allocate(self) -> int:
        recovered = self._recover_from_sites()
        if recovered:
            return recovered
        base = self.process.ensure_damage_number_theme_allocation()
        candidate = base + coop.NATIVE_WAIT_PREDICATE_WORKSPACE_OFFSET
        current = self.process.read(candidate, len(WAIT_PREDICATE_CAVE_MAGIC))
        if current != WAIT_PREDICATE_CAVE_MAGIC:
            self.process.write(candidate, WAIT_PREDICATE_CAVE_MAGIC)
        return candidate

    def _bind(self, base: int) -> None:
        self.base = base
        self.code = self.base + WAIT_PREDICATE_CAVE_CODE_OFFSET
        self.flag = self.base + WAIT_PREDICATE_CAVE_FLAG_OFFSET

    def _recover_existing(self) -> bool:
        recovered = self._recover_from_sites()
        if not recovered:
            return False
        self._bind(recovered)
        expected_code = wait_predicate_cave(self.code, self.flag)
        if self.process.read(self.code, len(expected_code)) != expected_code:
            raise RuntimeError(
                "native Wait-predicate cave has unexpected code; "
                "refusing to rewrite live executable memory"
            )
        complete = all(
            self.process.read(site_address, WAIT_PREDICATE_PATCH_SIZE)
            == wait_predicate_site_patch(self.code, site_address)
            for site_address, _original in WAIT_PREDICATE_SITES
        )
        if not complete:
            raise RuntimeError(
                "partial native Wait-predicate installation found; restart the game"
            )
        self.installed = True
        self.complete = True
        return True

    def install(self) -> bool:
        if self.installed and self.complete:
            return True

        sites = {
            site_address: self.process.read(site_address, WAIT_PREDICATE_PATCH_SIZE)
            for site_address, _original in WAIT_PREDICATE_SITES
        }
        recovered = self._recover_from_sites()
        for site_address, original in WAIT_PREDICATE_SITES:
            expected_patch = (
                wait_predicate_site_patch(
                    recovered + WAIT_PREDICATE_CAVE_CODE_OFFSET,
                    site_address,
                )
                if recovered
                else None
            )
            if (
                sites[site_address] != original
                and sites[site_address] != expected_patch
            ):
                raise RuntimeError(
                    f"native Wait-predicate site {site_address:#x} is owned "
                    "by another hook; refusing to overwrite"
                )

        if recovered:
            self._recover_existing()
        else:
            self._bind(self._allocate())
            self.process.write(self.flag, b"\x00")
            self.process.write(
                self.code,
                wait_predicate_cave(self.code, self.flag),
            )

        for site_address, _original in WAIT_PREDICATE_SITES:
            expected_patch = wait_predicate_site_patch(
                self.code,
                site_address,
            )
            if sites[site_address] != expected_patch:
                self.process.patch_code(site_address, expected_patch)
        self.installed = True
        self.complete = True
        return True

    def maintain(self, desired: bool) -> bool:
        desired = bool(desired)
        if desired and not self.complete:
            self.install()
        elif not desired and not self.installed:
            self._recover_existing()
        if not self.installed:
            return not desired
        value = bytes((int(desired),))
        if self.process.read(self.flag, 1) != value:
            self.process.write(self.flag, value)
        self.active = desired
        return True

    def restore(self) -> None:
        try:
            if self.installed:
                self.maintain(False)
        except (OSError, RuntimeError, ValueError):
            pass


def native_battle_mode_is_wait(process: coop.RemoteProcess) -> bool:
    settings = process.u64(SETTINGS_POINTER_GLOBAL)
    if not settings:
        return False
    return not bool(process.u32(settings) & BATTLE_MODE_ACTIVE_MASK)


class NativeWaitModeBridge:
    def __init__(
        self,
        process: coop.RemoteProcess,
        base: int,
        wait_predicate_hook: NativeWaitPredicateHook | None = None,
    ):
        self.process = process
        self.base = base

        self.command_block = base + COMMAND_BLOCK_CAVE_OFFSET
        self.manager = 0
        self.address = 0
        self.original = 0
        self.last_written = 0
        self.applied = False

        self.wait_predicate_hook = wait_predicate_hook or NativeWaitPredicateHook(
            process
        )

        self._publish_lifecycle(False)

    @property
    def lifecycle_desired_address(self) -> int:
        return self.command_block + WAIT_MENU_DESIRED_OFFSET

    @property
    def lifecycle_applied_address(self) -> int:
        return self.command_block + WAIT_MENU_APPLIED_OFFSET

    def _publish_lifecycle(self, desired: bool) -> None:
        target = bytes((int(bool(desired)),))
        if self.process.read(self.lifecycle_desired_address, 1) != target:
            self.process.write(self.lifecycle_desired_address, target)

    def owns_native_presentation(self) -> bool:
        try:
            return bool(
                self.process.read(self.lifecycle_applied_address, 1) == b"\x01"
                and self.process.u32(NATIVE_COMMAND_FLAG_GLOBAL) == 1
                and self.process.u32(NATIVE_COMMAND_ACTOR_GLOBAL) != 0
            )
        except (OSError, RuntimeError, struct.error, ValueError):
            return False

    def lifecycle_close_complete(self) -> bool:
        try:
            return self.process.read(self.lifecycle_applied_address, 1) == b"\x00"
        except (OSError, RuntimeError, ValueError):
            return False

    def _resolve(self) -> int:
        manager = self.process.u64(UI_MANAGER_GLOBAL)
        if not manager:
            return 0
        if manager != self.manager:
            self.manager = manager
            self.address = manager + COMMAND_SELECTION_FLAGS_OFFSET
            self.original = 0
            self.last_written = 0
            self.applied = False
        return self.address

    def maintain(
        self,
        desired: bool,
        *,
        native_presentation: bool = True,
    ) -> bool:
        desired = bool(desired)
        presentation_desired = bool(desired and native_presentation)
        wait_ok = True

        try:
            wait_ok = self.wait_predicate_hook.maintain(
                desired and native_battle_mode_is_wait(self.process)
            )
            address = self._resolve()
            if not address:
                if not presentation_desired:
                    self._publish_lifecycle(False)
                return (not presentation_desired) and wait_ok
            current = self.process.read(address, 1)[0]
            if presentation_desired:
                if not self.applied:
                    self.original = current
                target = command_selection_value(current, True)
                if current != target:
                    self.process.write(address, bytes((target,)))
                changed = not self.applied
                self.last_written = target
                self.applied = True
                self._publish_lifecycle(True)
                if changed:
                    print("NATIVE_WAIT_COMMAND_STATE OPEN", flush=True)
                return wait_ok

            if not self.applied:
                self._publish_lifecycle(False)
                return (not desired) or wait_ok

            if current == self.last_written:
                target = (current & ~COMMAND_SELECTION_ACTIVE_MASK) | (
                    self.original & COMMAND_SELECTION_ACTIVE_MASK
                )
                if current != target:
                    self.process.write(address, bytes((target,)))
            self.applied = False
            self.original = 0
            self.last_written = 0
            self._publish_lifecycle(False)
            print("NATIVE_WAIT_COMMAND_STATE CLOSED", flush=True)
            return (not desired) or wait_ok
        except (OSError, RuntimeError, ValueError):
            return False

    def restore(self) -> None:
        self.wait_predicate_hook.restore()
        try:
            self.maintain(False)
            deadline = time.monotonic() + WAIT_MENU_CLOSE_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                if self.process.read(self.lifecycle_applied_address, 1) == b"\x00":
                    break
                time.sleep(0.01)
        except (OSError, RuntimeError, ValueError):
            pass


__all__ = [
    "COMMAND_SELECTION_ACTIVE_MASK",
    "COMMAND_SELECTION_FLAGS_OFFSET",
    "NativeWaitPredicateHook",
    "NativeWaitModeBridge",
    "SETTINGS_POINTER_GLOBAL",
    "UI_MANAGER_GLOBAL",
    "WAIT_PREDICATE_HOOK_SITE",
    "WAIT_PREDICATE_STATE_HOOK_SITE",
    "command_selection_value",
    "native_battle_mode_is_wait",
    "wait_predicate_cave",
    "wait_predicate_site_patch",
]
