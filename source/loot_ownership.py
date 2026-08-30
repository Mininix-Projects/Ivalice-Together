from __future__ import annotations

import struct

import coop_runtime as coop


OWNERSHIP_QUERY = 0x003273B0
OWNERSHIP_CALL_SITE = 0x003110DF
STOCK_CALL = bytes.fromhex("E8 CC 62 01 00")

CAVE_MAGIC = b"CoopLoot"
CAVE_SIZE = 0x1000
CAVE_CODE_OFFSET = 0x100


def _rel32(after_instruction: int, target: int) -> bytes:
    return struct.pack("<i", target - after_instruction)


def build_owner_translation(code_address: int, command_block: int) -> bytes:
    code = bytearray()
    fixups: list[tuple[int, str]] = []
    labels: dict[str, int] = {}

    def label(name: str) -> None:
        labels[name] = len(code)

    def branch(opcode: int, target: str) -> None:
        code.extend((opcode, 0))
        fixups.append((len(code) - 1, target))

    third_block = command_block + coop.P3_COMMAND_BLOCK_OFFSET - 0x200

    code += b"\x49\xbb" + struct.pack("<Q", command_block)
    code += b"\x41\x83\x7b" + bytes((coop.COOP_ACTIVE_OFFSET,)) + b"\x00"
    branch(0x74, "ask")

    code += b"\x41\x8b\x43" + bytes((coop.P2_LOOT_HANDLE_OFFSET,))
    code += b"\x85\xc0"
    branch(0x74, "second")
    code += b"\x3b\xc8"
    branch(0x74, "owned")

    label("second")
    code += b"\x49\xba" + struct.pack("<Q", third_block)
    code += b"\x41\x8b\x42" + bytes((coop.P3_LOOT_HANDLE_OFFSET,))
    code += b"\x85\xc0"
    branch(0x74, "ask")
    code += b"\x3b\xc8"
    branch(0x75, "ask")

    label("owned")
    code += b"\xb8\x01\x00\x00\x00\xc3"

    label("ask")

    code += b"\xe9" + _rel32(code_address + len(code) + 5, OWNERSHIP_QUERY)

    for position, target in fixups:
        displacement = labels[target] - (position + 1)
        if not -128 <= displacement <= 127:
            raise ValueError("desvio curto fora de alcance no cave de loot")
        code[position] = displacement & 0xFF
    return bytes(code)


def call_site_patch(code_address: int) -> bytes:
    return b"\xe8" + _rel32(OWNERSHIP_CALL_SITE + len(STOCK_CALL), code_address)


class LootOwnershipBridge:
    def __init__(self, process, command_block: int) -> None:
        self.process = process
        self.command_block = int(command_block)
        self.base = 0
        self.code = 0
        self.installed = False
        self.owns_static = False

    def _recover(self) -> int:
        instruction = self.process.read(OWNERSHIP_CALL_SITE, len(STOCK_CALL))
        if instruction[:1] != b"\xe8":
            return 0
        target = (
            OWNERSHIP_CALL_SITE
            + len(STOCK_CALL)
            + struct.unpack("<i", instruction[1:])[0]
        )
        candidate = target - CAVE_CODE_OFFSET
        try:
            if self.process.read(candidate, len(CAVE_MAGIC)) == CAVE_MAGIC:
                return candidate
        except OSError:
            pass
        return 0

    def _claim_page(self) -> int:
        base = self.process.ensure_damage_number_theme_allocation()
        return base + coop.LOOT_OWNERSHIP_WORKSPACE_OFFSET

    def _page_image(self, base: int) -> bytes:
        code = int(base) + CAVE_CODE_OFFSET
        body = build_owner_translation(code, self.command_block)
        if CAVE_CODE_OFFSET + len(body) > CAVE_SIZE:
            raise RuntimeError("loot-ownership code exceeds its reserved page")
        page = bytearray(CAVE_SIZE)
        page[: len(CAVE_MAGIC)] = CAVE_MAGIC
        page[CAVE_CODE_OFFSET : CAVE_CODE_OFFSET + len(body)] = body
        return bytes(page)

    def install(self) -> bool:
        if self.installed:
            return False
        recovered = self._recover()
        if recovered:
            self.base = recovered
            self.code = recovered + CAVE_CODE_OFFSET
            if self.process.read(self.base, CAVE_SIZE) != self._page_image(self.base):
                raise RuntimeError(
                    "active loot-ownership cave mismatch; refusing hot repair"
                )
            self.installed = True
            return False

        current = self.process.read(OWNERSHIP_CALL_SITE, len(STOCK_CALL))
        if current != STOCK_CALL:
            raise RuntimeError(
                f"a chamada de posse do loot ja foi alterada: {current.hex(' ')}"
            )

        self.base = self._claim_page()
        self.code = self.base + CAVE_CODE_OFFSET
        if self.process.read(self.base, CAVE_SIZE) != b"\x00" * CAVE_SIZE:
            raise RuntimeError(
                "non-empty loot-ownership page with a stock root; restart the game"
            )
        # One complete immutable page is published before the call root.  A
        # failed/ambiguous root write is never followed by page repair.
        self.process.write(self.base, self._page_image(self.base))
        self.process.patch_code(OWNERSHIP_CALL_SITE, call_site_patch(self.code))
        self.installed = True
        self.owns_static = True
        return True

    def maintain(self, enabled: bool) -> bool:
        if enabled:
            return self.install()
        return self.restore()

    def restore(self) -> bool:
        if not self.installed:
            return False
        # The cave already gates itself on COOP_ACTIVE.  Movement shutdown
        # clears that dynamic flag; keeping this immutable call/cave resident
        # prevents a later helper from overwriting code a game thread may still
        # be executing.
        self.installed = False
        self.owns_static = False
        return False


__all__ = [
    "LootOwnershipBridge",
    "OWNERSHIP_CALL_SITE",
    "OWNERSHIP_QUERY",
    "STOCK_CALL",
    "build_owner_translation",
    "call_site_patch",
]
