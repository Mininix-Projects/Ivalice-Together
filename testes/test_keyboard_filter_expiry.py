"""The keyboard filter must not outlive the helper that owns it.

The launcher terminates its children, so coop_runtime.main()'s finally never
runs and the filter stayed armed inside the game with nothing left to disarm
it: keyboard and pad dead, mouse still working because it never passes through
the filter. The cave now spends a budget the live helper refills.
"""

import faulthandler
import unittest
import inspect

from capstone import CS_ARCH_X86, CS_MODE_64, Cs
from unicorn import UC_ARCH_X86, UC_MODE_64, Uc
from unicorn.x86_const import UC_X86_REG_RSI, UC_X86_REG_RSP

import coop_runtime as coop
import runtime_lifecycle


class KeyboardFilterExpiryTests(unittest.TestCase):
    class TransactionProcess:
        original = b"\x48\x8b\xce\xe8\x3e\xfe\xff\xff"

        def __init__(self, *, fail_write=False, fail_patch_after=False):
            self.root = self.original
            self.regions = {}
            self.events = []
            self.fail_write = fail_write
            self.fail_patch_after = fail_patch_after

        def read(self, address, size):
            if address == coop.KEYBOARD_HOOK_SITE:
                return self.root[:size]
            return self.regions.get(address, b"")[:size].ljust(size, b"\x00")

        def write(self, address, data):
            self.events.append(("write", address, bytes(data)))
            if self.fail_write:
                raise OSError("injected keyboard cave failure")
            self.regions[address] = bytes(data)

        def patch_code(self, address, data):
            self.events.append(("patch", address, bytes(data)))
            self.root = bytes(data)
            if self.fail_patch_after:
                raise OSError("injected keyboard root failure")

    def setUp(self):
        self.md = Cs(CS_ARCH_X86, CS_MODE_64)

    def test_budget_lives_inside_the_filters_own_cave(self):
        # Anywhere in the shared command block risks colliding with another
        # subsystem's field; the filter's own cave cannot.
        self.assertGreaterEqual(coop.KEYBOARD_FILTER_BUDGET_CAVE_OFFSET, 0)
        self.assertLess(coop.KEYBOARD_FILTER_BUDGET_CAVE_OFFSET + 4, 0x100)

    def test_budget_address_follows_the_keyboard_cave(self):
        self.assertEqual(
            coop.KEYBOARD_FILTER_BUDGET,
            coop.KEYBOARD_CAVE + coop.KEYBOARD_FILTER_BUDGET_CAVE_OFFSET,
        )

    def test_refill_expires_in_about_a_second_of_polls(self):
        # Long enough that a live helper never lets it lapse, short enough
        # that a dead one frees the input almost immediately.
        self.assertGreater(coop.KEYBOARD_FILTER_BUDGET_REFILL, 60)
        self.assertLessEqual(coop.KEYBOARD_FILTER_BUDGET_REFILL, 600)

    def test_helper_refreshes_the_budget_every_iteration(self):
        source = inspect.getsource(coop.main)
        loop = source.index("while True:")
        budget = source.index("KEYBOARD_FILTER_BUDGET", loop)
        refill = source.index("KEYBOARD_FILTER_BUDGET_REFILL", budget)
        heartbeat = source.index("if now >= next_liveness_at:", loop)
        recovery = source.index(
            "leader_role_transfer = read_native_leader_role_transfer(process)", loop
        )
        self.assertLess(loop, budget)
        self.assertLess(budget, refill)
        self.assertLess(refill, recovery)
        self.assertLess(heartbeat, recovery)

    def test_desktop_owner_policy_matrix(self):
        required = runtime_lifecycle.native_desktop_filter_required
        for active, owner, expected in (
            (False, None, False),
            (False, "p1", False),
            (False, "p2", False),
            (True, "p1", False),
            (True, "p2", True),
            (True, "p3", True),
            (True, None, True),
        ):
            with self.subTest(active=active, owner=owner):
                self.assertIs(required(active, owner), expected)

    def test_runtime_publishes_filter_only_when_owner_policy_changes(self):
        source = inspect.getsource(coop.main)
        self.assertIn("bindings.desktop_owner()", source)
        self.assertIn(
            "keyboard_filter_active != published_keyboard_filter_active",
            source,
        )

    def test_active_startup_applies_owner_policy_before_the_first_poll(self):
        source = inspect.getsource(coop.main)
        install = source.index("install_runtime(")
        startup = source.index("startup_keyboard_filter =", install)
        publish = source.index("KEYBOARD_FILTER_ACTIVE_OFFSET", startup)
        loop = source.index("while True:", publish)
        self.assertLess(install, startup)
        self.assertLess(startup, publish)
        self.assertLess(publish, loop)

    def test_f8_recomputes_filter_after_toggling_active_in_same_branch(self):
        source = inspect.getsource(coop.main)
        toggle = source.index("active = not active")
        recompute = source.index("toggled_keyboard_filter =", toggle)
        publish = source.index("KEYBOARD_FILTER_ACTIVE_OFFSET", recompute)
        follower_cleanup = source.index("clear_second_player_command", publish)
        self.assertLess(toggle, recompute)
        self.assertLess(recompute, publish)
        self.assertLess(publish, follower_cleanup)

    def test_shutdown_releases_filter_before_optional_actor_handoff_io(self):
        source = inspect.getsource(coop.shutdown_runtime)
        release = source.index("KEYBOARD_FILTER_ACTIVE_OFFSET")
        budget = source.index("KEYBOARD_FILTER_BUDGET", release)
        handoff = source.index("if handoff_state is not None", budget)
        self.assertLess(release, budget)
        self.assertLess(budget, handoff)

    def test_cave_checks_and_spends_the_budget_before_filtering(self):
        instructions = self._cave_instructions()
        mnemonics = [(m, o) for m, o in instructions]
        # Reads the budget, bails out when spent, and spends one unit.
        self.assertIn(("cmp", "dword ptr [r10], 0"), mnemonics)
        self.assertIn(("dec", "dword ptr [r10]"), mnemonics)
        self.assertTrue(
            any(m == "jle" for m, _ in mnemonics),
            "an exhausted budget must skip the filter",
        )

    def test_native_passthrough_is_only_shared_map_and_speed_actions(self):
        self.assertEqual(
            coop.KEYBOARD_NATIVE_PASSTHROUGH_INDICES,
            (
                0x0C,
                0x19,
                0x25,
                0x26,
                0x27,
                0x33,
                0x56,
            ),
        )
        for camera_action in (0x07, 0x17, 0x3F, 0x40, 0x41, 0x42, 0x52):
            self.assertNotIn(
                camera_action,
                coop.KEYBOARD_NATIVE_PASSTHROUGH_INDICES,
            )
        # Pause is returned exactly once through the native dialog bridge.
        self.assertNotIn(0x24, coop.KEYBOARD_NATIVE_PASSTHROUGH_INDICES)
        self.assertNotIn(0x05, coop.KEYBOARD_NATIVE_PASSTHROUGH_INDICES)  # F
        self.assertNotIn(0x11, coop.KEYBOARD_NATIVE_PASSTHROUGH_INDICES)  # R

    def test_native_passthrough_loop_uses_raw_directinput_and_normalized_state(self):
        cave = self._cave_bytes()
        self.assertLessEqual(len(cave), coop.KEYBOARD_FILTER_BUDGET_CAVE_OFFSET)
        self.assertTrue(
            cave.endswith(bytes(coop.KEYBOARD_NATIVE_PASSTHROUGH_INDICES))
        )
        instructions = self._cave_instructions()
        self.assertIn(
            ("movabs", f"r11, {coop.KEYBOARD_NATIVE_KEYMAP:#x}"),
            instructions,
        )
        self.assertIn(("movzx", "eax, byte ptr [r10]"), instructions)
        self.assertIn(
            ("movzx", "edx, byte ptr [r11 + rax]"),
            instructions,
        )
        self.assertIn(
            ("cmp", "byte ptr [rsp + rdx + 0x20], 0"),
            instructions,
        )
        self.assertIn(("setl", "dl"), instructions)
        self.assertIn(
            ("mov", "byte ptr [rsi + rax + 0x231], dl"),
            instructions,
        )

    def test_native_passthrough_emulation_preserves_only_verified_global_keys(self):
        cave = self._cave_bytes()
        # Keep the synthetic input object away from every supported relocated
        # code-cave base used by the native-layout test matrix.
        device = 0x60000000
        stack = 0x40008000
        keymap_page = coop.KEYBOARD_NATIVE_KEYMAP & ~0xFFF
        dik_by_index = {
            0x07: 0x23,
            0x0C: 0x32,
            0x17: 0x2D,
            0x19: 0x2C,
            0x25: 0x3B,
            0x26: 0x3C,
            0x27: 0x3D,
            0x33: 0x0F,
            0x3F: 0xCB,
            0x40: 0xCD,
            0x41: 0xC8,
            0x42: 0xD0,
            0x52: 0x52,
            0x56: 0x4E,
        }

        # Unicorn probes reserved Windows address ranges with SEH while mapping.
        # Pytest's faulthandler would report those handled probes as crashes.
        restore_faulthandler = faulthandler.is_enabled()
        if restore_faulthandler:
            faulthandler.disable()
        try:
            emulator = Uc(UC_ARCH_X86, UC_MODE_64)
            emulator.mem_map(0x700000, 0x200000)
            emulator.mem_map(keymap_page, 0x2000)
            # Other native-layout tests intentionally relocate the shared
            # cave globals.  Map the cave actually used by this generated
            # payload instead of relying on the process-default allocation.
            emulator.mem_map(coop.KEYBOARD_CAVE & ~0xFFF, 0x10000)
            emulator.mem_map(device, 0x1000)
            emulator.mem_map(0x40000000, 0x10000)
        finally:
            if restore_faulthandler:
                faulthandler.enable()
        emulator.mem_write(coop.KEYBOARD_CAVE, cave)
        # Minimal x64 memset(rcx, dl, r8) preserving nonvolatile rdi.
        emulator.mem_write(
            coop.MEMSET_FUNCTION,
            b"\x57\x48\x89\xcf\x4c\x89\xc1\x88\xd0\xf3\xaa\x5f\xc3",
        )
        emulator.mem_write(coop.KEYBOARD_FINALIZER, b"\xc3")
        keymap = bytearray(0x100)
        for internal, dik in dik_by_index.items():
            keymap[internal] = dik
        # W is intentionally not global and must remain filtered.
        keymap[0x16] = 0x11
        emulator.mem_write(coop.KEYBOARD_NATIVE_KEYMAP, bytes(keymap))

        def run(active, budget):
            for offset in (0x30, 0x130, 0x231):
                emulator.mem_write(device + offset, b"\xa5" * 0x100)
            raw = bytearray(0x100)
            raw[dik_by_index[0x07]] = 0x80  # camera reset: P1-only
            raw[dik_by_index[0x0C]] = 0x80  # map: shared
            raw[dik_by_index[0x27]] = 0x01
            raw[0x11] = 0x80
            emulator.mem_write(stack + 0x20, bytes(raw))
            emulator.mem_write(
                coop.COMMAND_BLOCK + coop.KEYBOARD_FILTER_ACTIVE_OFFSET,
                int(active).to_bytes(4, "little"),
            )
            emulator.mem_write(
                coop.KEYBOARD_FILTER_BUDGET,
                int(budget).to_bytes(4, "little"),
            )
            emulator.reg_write(UC_X86_REG_RSI, device)
            emulator.reg_write(UC_X86_REG_RSP, stack)
            emulator.emu_start(
                coop.KEYBOARD_CAVE,
                coop.KEYBOARD_HOOK_RETURN,
                count=10000,
            )
            states = tuple(
                bytes(emulator.mem_read(device + offset, 0x100))
                for offset in (0x30, 0x130, 0x231)
            )
            remaining = int.from_bytes(
                emulator.mem_read(coop.KEYBOARD_FILTER_BUDGET, 4), "little"
            )
            return states, remaining

        filtered, remaining = run(1, 5)
        self.assertEqual(filtered[0], b"\x00" * 0x100)
        self.assertEqual(filtered[1], b"\x00" * 0x100)
        self.assertEqual(filtered[2][0x07], 0)
        self.assertEqual(filtered[2][0x0C], 1)
        self.assertEqual(filtered[2][0x27], 0)
        self.assertEqual(filtered[2][0x16], 0)
        self.assertEqual(sum(filtered[2]), 1)
        self.assertEqual(remaining, 4)

        for active, budget in ((0, 5), (1, 0)):
            with self.subTest(active=active, budget=budget):
                untouched, remaining = run(active, budget)
                self.assertTrue(all(state == b"\xa5" * 0x100 for state in untouched))
                self.assertEqual(remaining, budget)

    def test_keyboard_filter_is_one_way_and_exactly_adoptable(self):
        process = self.TransactionProcess()
        self.assertTrue(coop.install_keyboard_filter(process))
        self.assertEqual(
            [(kind, address) for kind, address, _payload in process.events],
            [
                ("write", coop.KEYBOARD_CAVE),
                ("patch", coop.KEYBOARD_HOOK_SITE),
            ],
        )
        process.events.clear()

        self.assertFalse(coop.install_keyboard_filter(process))
        self.assertEqual(process.events, [])

    def test_keyboard_cave_failure_never_publishes_the_root(self):
        process = self.TransactionProcess(fail_write=True)
        with self.assertRaisesRegex(OSError, "keyboard cave"):
            coop.install_keyboard_filter(process)
        self.assertEqual(process.root, process.original)
        self.assertFalse(any(kind == "patch" for kind, *_rest in process.events))

    def test_keyboard_post_root_failure_is_read_only_recoverable(self):
        process = self.TransactionProcess(fail_patch_after=True)
        with self.assertRaisesRegex(OSError, "keyboard root"):
            coop.install_keyboard_filter(process)
        process.fail_patch_after = False
        process.events.clear()

        self.assertFalse(coop.install_keyboard_filter(process))
        self.assertEqual(process.events, [])

    def test_exhausted_budget_and_cleared_flag_reach_the_same_passthrough(self):
        instructions = self._cave_instructions()
        targets = {
            operands for mnemonic, operands in instructions if mnemonic in ("je", "jle")
        }
        self.assertEqual(
            len(targets),
            1,
            f"both bail-outs must land on the passthrough, got {targets}",
        )

    def _cave_bytes(self):
        # The exact stock bytes the installer expects at the hook site.
        original = b"\x48\x8b\xce\xe8\x3e\xfe\xff\xff"

        class FakeProcess:
            def __init__(self):
                self.cave = None

            def read(self, address, size):
                if address == coop.KEYBOARD_HOOK_SITE:
                    return original[:size]
                return b"\x00" * size

            def write(self, address, data):
                if address == coop.KEYBOARD_CAVE:
                    self.cave = bytes(data)

            def patch_code(self, address, data):
                pass

        process = FakeProcess()
        try:
            coop.install_keyboard_filter(process)
        except Exception as error:
            self.fail(f"installer rejected the fake process: {error}")
        self.assertIsNotNone(process.cave, "installer did not publish a cave")
        return process.cave

    def _cave_instructions(self):
        cave = self._cave_bytes()
        return [
            (i.mnemonic, i.op_str)
            for i in self.md.disasm(cave, coop.KEYBOARD_CAVE)
        ]


if __name__ == "__main__":
    unittest.main()
