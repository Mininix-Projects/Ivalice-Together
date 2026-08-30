"""Controller input remains active after alt-tab; mouse and keyboard do not."""

import inspect
import os
import struct
import unittest
from pathlib import Path

from capstone import CS_ARCH_X86, CS_MODE_64, Cs

import coop_runtime as coop
import headless_menu_shell as menu_shell




def find_game_executable() -> Path:
    configured = os.environ.get("FF12_GAME_ROOT")
    if configured:
        return Path(configured) / "x64" / "FFXII_TZA.exe"
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "x64" / "FFXII_TZA.exe"
        if candidate.is_file():
            return candidate
    return Path("__missing_FFXII_TZA.exe")


GAME_EXE = find_game_executable()
IMAGE_BASE = 0x120000
# 0xD500 belongs to the dual-interaction allocation beside the other input
# caves (P2_DEVICE_FILTER 0xD000 and P1_SDL_XINPUT 0xD300/0xD400).
CAVE_BASE = 0x5BAB0000

# This is the mouse/keyboard focus gate (0x7A2440..0x7A3105). It must remain
# intact because it suppresses mouse and keyboard input in the background.
MOUSE_KEYBOARD_FOCUS_GATE = 0x7A30BB
MOUSE_KEYBOARD_FOCUS_ORIGINAL = b"\x75\x1e"


def read_exe_at(virtual_address, size):
    data = GAME_EXE.read_bytes()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    sections = struct.unpack_from("<H", data, pe + 6)[0]
    optional = struct.unpack_from("<H", data, pe + 20)[0]
    table = pe + 24 + optional
    rva = virtual_address - IMAGE_BASE
    for index in range(sections):
        entry = table + index * 40
        _vsize, vaddr, rsize, pointer = struct.unpack_from("<IIII", data, entry + 8)
        if vaddr <= rva < vaddr + rsize:
            start = pointer + (rva - vaddr)
            return data[start : start + size]
    raise AssertionError(f"{virtual_address:#x} fora das secoes mapeadas")


def disassemble(payload, address):
    decoder = Cs(CS_ARCH_X86, CS_MODE_64)
    decoder.detail = True
    return list(decoder.disasm(bytes(payload), address))


class FakeProcess:
    def __init__(self, current, *, cave=None):
        self.current = bytes(current)
        self.cave = bytearray(
            b"\x00" * coop.PAD_BACKGROUND_INPUT_CAVE_SIZE
            if cave is None
            else bytes(cave)
        )
        if len(self.cave) != coop.PAD_BACKGROUND_INPUT_CAVE_SIZE:
            raise ValueError("invalid fake pad-background cave size")
        self.patches = []
        self.writes = {}
        self.operations = []

    def read(self, address, size):
        if address == coop.PAD_BACKGROUND_INPUT_SITE:
            return self.current[:size]
        if address == coop.PAD_BACKGROUND_INPUT_CAVE:
            return bytes(self.cave[:size])
        raise AssertionError(f"leitura inesperada em {address:#x}")

    def write(self, address, data):
        payload = bytes(data)
        self.operations.append(("write", address, payload))
        self.writes[address] = payload
        if address == coop.PAD_BACKGROUND_INPUT_CAVE:
            self.cave[: len(payload)] = payload

    def patch_code(self, address, data):
        payload = bytes(data)
        self.operations.append(("patch", address, payload))
        self.patches.append((address, payload))
        self.current = payload


class PadBackgroundInputTests(unittest.TestCase):
    def setUp(self):
        coop.set_dual_interaction_base(CAVE_BASE)

    def test_gate_bytes_match_the_shipped_executable(self):
        if not GAME_EXE.is_file():
            self.skipTest("executavel do jogo indisponivel")
        observed = read_exe_at(
            coop.PAD_BACKGROUND_INPUT_SITE,
            len(coop.PAD_BACKGROUND_INPUT_ORIGINAL),
        )
        self.assertEqual(observed, coop.PAD_BACKGROUND_INPUT_ORIGINAL)

    def test_the_replaced_branch_is_the_unfocused_bail_out(self):
        items = disassemble(
            coop.PAD_BACKGROUND_INPUT_ORIGINAL, coop.PAD_BACKGROUND_INPUT_SITE
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].mnemonic, "jne")
        # The jump target is the epilogue that clears the flag and skips the pad.
        self.assertEqual(items[0].op_str, hex(0x7A2410))
        self.assertEqual(
            items[0].address + items[0].size,
            coop.PAD_BACKGROUND_INPUT_FOCUSED_ENTRY,
        )

    def test_resume_entry_lands_right_after_the_shared_flag_store(self):
        if not GAME_EXE.is_file():
            self.skipTest("executavel do jogo indisponivel")
        store = disassemble(
            read_exe_at(coop.PAD_BACKGROUND_INPUT_FOCUSED_ENTRY, 16),
            coop.PAD_BACKGROUND_INPUT_FOCUSED_ENTRY,
        )[0]
        self.assertEqual(store.mnemonic, "mov")
        # The skipped instruction is exactly `mov byte [rip+d], 1` on the flag.
        target = store.address + store.size + store.operands[0].mem.disp
        self.assertEqual(target, coop.INPUT_FOCUS_FLAG_GLOBAL)
        self.assertEqual(
            store.address + store.size, coop.PAD_BACKGROUND_INPUT_RESUME_ENTRY
        )

    def test_mouse_and_keyboard_gate_is_left_alone(self):
        if not GAME_EXE.is_file():
            self.skipTest("executavel do jogo indisponivel")
        observed = read_exe_at(
            MOUSE_KEYBOARD_FOCUS_GATE, len(MOUSE_KEYBOARD_FOCUS_ORIGINAL)
        )
        self.assertEqual(observed, MOUSE_KEYBOARD_FOCUS_ORIGINAL)
        # No address written by co-op may overlap this gate.
        for name in dir(coop):
            if not name.endswith(("_SITE", "_HOOK_SITE", "_CALL")):
                continue
            value = getattr(coop, name)
            if isinstance(value, int):
                self.assertNotEqual(value, MOUSE_KEYBOARD_FOCUS_GATE, name)

    def test_cave_keeps_the_shared_flag_honest_while_unfocused(self):
        cave = coop.PAD_BACKGROUND_INPUT_CAVE
        self.assertEqual(cave, CAVE_BASE + coop.PAD_BACKGROUND_INPUT_CAVE_OFFSET)
        items = disassemble(coop.build_pad_background_input_cave(cave), cave)
        self.assertEqual([item.mnemonic for item in items], ["je", "mov", "jmp", "jmp"])

        branch, store, resume, focused = items
        # With focus, execution enters the original path, which writes 1 itself.
        self.assertEqual(
            int(focused.op_str, 16), coop.PAD_BACKGROUND_INPUT_FOCUSED_ENTRY
        )
        self.assertEqual(int(branch.op_str, 16), focused.address)
        # Without focus, it writes 0 to the shared flag and rejoins after the store.
        self.assertEqual(
            store.address + store.size + store.operands[0].mem.disp,
            coop.INPUT_FOCUS_FLAG_GLOBAL,
        )
        self.assertEqual(store.operands[1].imm, 0)
        self.assertEqual(int(resume.op_str, 16), coop.PAD_BACKGROUND_INPUT_RESUME_ENTRY)

    def test_cave_fits_its_reserved_slot(self):
        code = coop.build_pad_background_input_cave(coop.PAD_BACKGROUND_INPUT_CAVE)
        self.assertLessEqual(len(code), coop.PAD_BACKGROUND_INPUT_CAVE_SIZE)

    def test_cave_offset_does_not_collide_with_a_neighbour(self):
        offset = coop.PAD_BACKGROUND_INPUT_CAVE_OFFSET
        end = offset + coop.PAD_BACKGROUND_INPUT_CAVE_SIZE
        for name in dir(coop):
            if not name.endswith("_CAVE_OFFSET") or name.startswith(
                "PAD_BACKGROUND_INPUT"
            ):
                continue
            other = getattr(coop, name)
            if isinstance(other, int):
                self.assertFalse(offset <= other < end, name)
        self.assertFalse(
            offset <= coop.P1_SDL_XINPUT_STATE_OFFSET < end, "P1_SDL_XINPUT_STATE"
        )

    def test_install_detours_the_gate_and_is_idempotent(self):
        process = FakeProcess(coop.PAD_BACKGROUND_INPUT_ORIGINAL)
        self.assertTrue(coop.install_pad_background_input(process))
        self.assertIn(coop.PAD_BACKGROUND_INPUT_CAVE, process.writes)
        detour = process.patches[-1][1]
        self.assertEqual(len(detour), len(coop.PAD_BACKGROUND_INPUT_ORIGINAL))
        jump = disassemble(detour[:5], coop.PAD_BACKGROUND_INPUT_SITE)[0]
        self.assertEqual(jump.mnemonic, "jmp")
        self.assertEqual(int(jump.op_str, 16), coop.PAD_BACKGROUND_INPUT_CAVE)

        self.assertEqual(
            [operation[0] for operation in process.operations],
            ["write", "patch"],
        )

        # An exact resident topology is adopted without any remote mutation.
        process.patches.clear()
        process.writes.clear()
        process.operations.clear()
        self.assertFalse(coop.install_pad_background_input(process))
        self.assertEqual(process.operations, [])
        self.assertEqual(process.current, detour)

    def test_restore_returns_the_native_gate(self):
        process = FakeProcess(coop.PAD_BACKGROUND_INPUT_ORIGINAL)
        coop.install_pad_background_input(process)
        process.patches.clear()
        coop.restore_pad_background_input(process)
        self.assertEqual(process.current, coop.PAD_BACKGROUND_INPUT_ORIGINAL)
        coop.restore_pad_background_input(process)
        self.assertEqual(len(process.patches), 1)

    def test_unknown_bytes_abort_instead_of_corrupting_code(self):
        process = FakeProcess(b"\x90" * len(coop.PAD_BACKGROUND_INPUT_ORIGINAL))
        with self.assertRaises(RuntimeError):
            coop.install_pad_background_input(process)
        self.assertEqual(process.patches, [])
        self.assertEqual(process.writes, {})

    def test_stock_root_with_nonempty_cave_aborts_without_repair(self):
        cave = bytearray(b"\x00" * coop.PAD_BACKGROUND_INPUT_CAVE_SIZE)
        cave[7] = 0xCC
        process = FakeProcess(coop.PAD_BACKGROUND_INPUT_ORIGINAL, cave=cave)

        with self.assertRaisesRegex(RuntimeError, "non-empty pad background input"):
            coop.install_pad_background_input(process)

        self.assertEqual(process.operations, [])

    def test_active_root_with_mismatched_cave_aborts_without_detach(self):
        detour = (
            b"\xe9"
            + coop.rel32(
                coop.PAD_BACKGROUND_INPUT_SITE,
                5,
                coop.PAD_BACKGROUND_INPUT_CAVE,
            )
            + b"\x90"
        )
        process = FakeProcess(detour)

        with self.assertRaisesRegex(RuntimeError, "active pad background input cave"):
            coop.install_pad_background_input(process)

        self.assertEqual(process.operations, [])
        self.assertEqual(process.current, detour)

    def test_restore_rejects_foreign_root_without_overwriting_it(self):
        foreign = b"\x90" * len(coop.PAD_BACKGROUND_INPUT_ORIGINAL)
        process = FakeProcess(foreign)

        with self.assertRaisesRegex(RuntimeError, "unexpected bytes at pad focus gate"):
            coop.restore_pad_background_input(process)

        self.assertEqual(process.operations, [])
        self.assertEqual(process.current, foreign)

    def test_runtime_flag_is_on(self):
        self.assertTrue(coop.PAD_BACKGROUND_INPUT_ENABLED)

    def test_both_runtime_pollers_keep_pad_live_but_keyboard_focus_bound(self):
        movement = inspect.getsource(coop.main)
        menus = inspect.getsource(menu_shell.CoopMenuRuntime.input)
        expected = "begin_frame(True, keyboard_enabled=foreground)"

        self.assertIn(expected, movement)
        self.assertIn(expected, menus)

    def test_custom_menu_binding_gates_do_not_require_foreground(self):
        source = inspect.getsource(menu_shell.CoopMenuRuntime.input)

        self.assertNotIn("p1_binding_enabled = foreground", source)
        self.assertNotIn(
            "foreground\n            and p2_manual_control_enabled", source
        )
        self.assertNotIn(
            "foreground\n            and p3_manual_control_enabled", source
        )


if __name__ == "__main__":
    unittest.main()
