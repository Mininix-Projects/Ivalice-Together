"""The native manual-command gate must remove only the Berserk bit."""

import os
import struct
import unittest
from pathlib import Path

from capstone import CS_ARCH_X86, CS_MODE_64, Cs

import coop_runtime as coop
import headless_world_target_overlay as overlay




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


def status_bit(name):
    for bit, label, _icon, _good in overlay.STATUS_EFFECTS:
        if label == name:
            return bit
    raise AssertionError(f"status {name!r} ausente da tabela")


def gate_mask(payload):
    """Read the immediate operand from the `test ecx, imm32` gate."""

    return struct.unpack_from("<I", payload, 2)[0]


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


class FakeProcess:
    def __init__(self, current):
        self.current = bytes(current)
        self.patches = []

    def read(self, address, size):
        if address != coop.MANUAL_COMMAND_STATUS_GATE_SITE:
            raise AssertionError(f"leitura inesperada em {address:#x}")
        return self.current[:size]

    def patch_code(self, address, data):
        self.patches.append((address, bytes(data)))
        self.current = bytes(data)


class ManualCommandGateTests(unittest.TestCase):
    def test_original_bytes_match_the_shipped_executable(self):
        if not GAME_EXE.is_file():
            self.skipTest("executavel do jogo indisponivel")
        observed = read_exe_at(
            coop.MANUAL_COMMAND_STATUS_GATE_SITE,
            len(coop.MANUAL_COMMAND_STATUS_GATE_ORIGINAL),
        )
        self.assertEqual(observed, coop.MANUAL_COMMAND_STATUS_GATE_ORIGINAL)

    def test_patch_only_clears_the_berserk_bit(self):
        original = gate_mask(coop.MANUAL_COMMAND_STATUS_GATE_ORIGINAL)
        patched = gate_mask(coop.MANUAL_COMMAND_STATUS_GATE_PATCHED)
        berserk = 1 << status_bit("Berserk")

        self.assertEqual(berserk, coop.BERSERK_STATUS_MASK)
        self.assertEqual(original & berserk, berserk)
        self.assertEqual(patched & berserk, 0)
        self.assertEqual(original ^ patched, berserk)

    def test_the_other_command_locking_statuses_survive(self):
        patched = gate_mask(coop.MANUAL_COMMAND_STATUS_GATE_PATCHED)
        for name in ("ko", "Stone", "Stop", "Sleep", "Confuse", "Disable", "X-Zone"):
            with self.subTest(status=name):
                self.assertTrue(patched & (1 << status_bit(name)))

    def test_patch_keeps_the_same_instruction_shape(self):
        decoder = Cs(CS_ARCH_X86, CS_MODE_64)
        site = coop.MANUAL_COMMAND_STATUS_GATE_SITE
        decoded = []
        for payload in (
            coop.MANUAL_COMMAND_STATUS_GATE_ORIGINAL,
            coop.MANUAL_COMMAND_STATUS_GATE_PATCHED,
        ):
            items = list(decoder.disasm(payload, site))
            self.assertEqual(len(items), 1)
            item = items[0]
            self.assertEqual(item.mnemonic, "test")
            self.assertEqual(item.size, len(payload))
            decoded.append(item.op_str.split(",")[0].strip())
        self.assertEqual(decoded[0], "ecx")
        self.assertEqual(decoded[0], decoded[1])

    def test_install_patches_and_is_idempotent(self):
        process = FakeProcess(coop.MANUAL_COMMAND_STATUS_GATE_ORIGINAL)
        coop.install_berserk_manual_command_gate(process)
        self.assertEqual(
            process.patches,
            [
                (
                    coop.MANUAL_COMMAND_STATUS_GATE_SITE,
                    coop.MANUAL_COMMAND_STATUS_GATE_PATCHED,
                )
            ],
        )
        coop.install_berserk_manual_command_gate(process)
        self.assertEqual(len(process.patches), 1)

    def test_restore_refuses_to_rewrite_the_resident_gate(self):
        process = FakeProcess(coop.MANUAL_COMMAND_STATUS_GATE_PATCHED)
        with self.assertRaisesRegex(RuntimeError, "process-lifetime"):
            coop.restore_berserk_manual_command_gate(process)
        self.assertEqual(process.current, coop.MANUAL_COMMAND_STATUS_GATE_PATCHED)
        self.assertEqual(process.patches, [])

        stock = FakeProcess(coop.MANUAL_COMMAND_STATUS_GATE_ORIGINAL)
        self.assertFalse(coop.restore_berserk_manual_command_gate(stock))
        self.assertEqual(stock.patches, [])

    def test_unknown_bytes_abort_instead_of_corrupting_code(self):
        process = FakeProcess(b"\x90" * len(coop.MANUAL_COMMAND_STATUS_GATE_ORIGINAL))
        with self.assertRaises(RuntimeError):
            coop.install_berserk_manual_command_gate(process)
        self.assertEqual(process.patches, [])

    def test_runtime_flag_is_on(self):
        self.assertTrue(coop.BERSERK_MANUAL_COMMAND_ENABLED)


if __name__ == "__main__":
    unittest.main()
