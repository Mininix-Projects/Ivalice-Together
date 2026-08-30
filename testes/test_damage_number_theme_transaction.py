import struct
import unittest
from types import SimpleNamespace
from unittest import mock

import coop_runtime as coop


class FakeProcess:
    def __init__(self, memory):
        self.memory = {int(address): bytes(payload) for address, payload in memory.items()}
        self.writes = []
        self.patches = []

    def read(self, address, size):
        payload = self.memory.get(int(address))
        if payload is None or len(payload) < int(size):
            raise AssertionError(f"unexpected read at {address:#x} size {size:#x}")
        return payload[: int(size)]

    def write(self, address, payload):
        payload = bytes(payload)
        self.writes.append((int(address), payload))
        self.memory[int(address)] = payload

    def patch_code(self, address, payload):
        payload = bytes(payload)
        self.patches.append((int(address), payload))
        self.memory[int(address)] = payload


class DamageNumberThemeTransactionTests(unittest.TestCase):
    def setUp(self):
        coop.set_damage_number_theme_base(0x31000000)
        self.source_patch = b"\xe8" + coop.rel32(
            coop.DAMAGE_NUMBER_SOURCE_CALL_SITE,
            5,
            coop.DAMAGE_NUMBER_SOURCE_WRAPPER,
        )
        self.init_slots = (
            coop.DAMAGE_NUMBER_INIT_WRAPPER,
            coop.DAMAGE_NUMBER_INIT_WRAPPER_ALT,
        )
        self.init_patches = tuple(
            b"\xe8" + coop.rel32(coop.DAMAGE_NUMBER_INIT_CALL_SITE, 5, slot)
            for slot in self.init_slots
        )
        self.source_wrapper = coop.build_damage_number_source_wrapper(
            coop.DAMAGE_NUMBER_SOURCE_WRAPPER,
            coop.DAMAGE_NUMBER_SOURCE_STATE,
        )
        self.init_wrappers = tuple(
            coop.build_damage_number_init_wrapper(
                slot,
                coop.DAMAGE_NUMBER_SOURCE_STATE,
                coop.COMMAND_BLOCK,
                coop.third_player_block(),
                coop.P1_DAMAGE_NUMBER_COLOR,
                coop.P2_DAMAGE_NUMBER_COLOR,
                coop.P3_DAMAGE_NUMBER_COLOR,
                coop.DAMAGE_NUMBER_BRIGHTNESS_STATE,
            )
            for slot in self.init_slots
        )

    def active_memory(self, *, source_wrapper=None, init_index=0):
        return {
            coop.DAMAGE_NUMBER_SOURCE_CALL_SITE: self.source_patch,
            coop.DAMAGE_NUMBER_INIT_CALL_SITE: self.init_patches[init_index],
            coop.DAMAGE_NUMBER_SOURCE_WRAPPER: (
                self.source_wrapper if source_wrapper is None else source_wrapper
            ),
            self.init_slots[init_index]: self.init_wrappers[init_index],
        }

    def test_exact_active_topology_is_adopted_without_any_mutation(self):
        process = FakeProcess(self.active_memory())

        coop.install_damage_number_source_themes(process)

        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_partial_topology_aborts_before_any_mutation(self):
        process = FakeProcess(
            {
                coop.DAMAGE_NUMBER_SOURCE_CALL_SITE: self.source_patch,
                coop.DAMAGE_NUMBER_INIT_CALL_SITE: (
                    coop.DAMAGE_NUMBER_INIT_CALL_ORIGINAL
                ),
            }
        )

        with self.assertRaisesRegex(RuntimeError, "partially installed"):
            coop.install_damage_number_source_themes(process)

        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_active_wrapper_mismatch_aborts_without_hot_repair(self):
        process = FakeProcess(
            self.active_memory(source_wrapper=b"\xcc" * len(self.source_wrapper))
        )

        with self.assertRaisesRegex(RuntimeError, "source wrapper mismatch"):
            coop.install_damage_number_source_themes(process)

        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_fresh_install_publishes_source_root_last(self):
        process = FakeProcess(
            {
                coop.DAMAGE_NUMBER_SOURCE_CALL_SITE: (
                    coop.DAMAGE_NUMBER_SOURCE_CALL_ORIGINAL
                ),
                coop.DAMAGE_NUMBER_INIT_CALL_SITE: (
                    coop.DAMAGE_NUMBER_INIT_CALL_ORIGINAL
                ),
                coop.DAMAGE_NUMBER_BRIGHTNESS_STATE: struct.pack(
                    "<I", coop.DAMAGE_NUMBER_BRIGHTNESS_DEFAULT
                ),
            }
        )

        coop.install_damage_number_source_themes(process)

        self.assertEqual(
            process.patches,
            [
                (coop.DAMAGE_NUMBER_INIT_CALL_SITE, self.init_patches[0]),
                (coop.DAMAGE_NUMBER_SOURCE_CALL_SITE, self.source_patch),
            ],
        )
        self.assertEqual(process.writes[-1], (self.init_slots[0], self.init_wrappers[0]))

    def test_signed_proxy_adoption_never_detaches_or_rewrites_roots(self):
        proxy_entry = (
            coop.DAMAGE_NUMBER_THEME_BASE
            + coop.DAMAGE_METER_PROXY_WORKSPACE_OFFSET
            + coop.DAMAGE_METER_PROXY_CODE_OFFSET
        )
        proxy_call = b"\xe8" + coop.rel32(
            coop.DAMAGE_NUMBER_SOURCE_CALL_SITE,
            5,
            proxy_entry,
        )
        memory = self.active_memory()
        memory[coop.DAMAGE_NUMBER_SOURCE_CALL_SITE] = proxy_call
        process = FakeProcess(memory)
        resident = SimpleNamespace(previous_call=self.source_patch)

        with mock.patch(
            "damage_meter.inspect_resident_damage_hook",
            return_value=resident,
        ) as inspect:
            coop.install_damage_number_source_themes(process)

        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])
        inspect.assert_called_once_with(
            process,
            coop.DAMAGE_NUMBER_THEME_BASE,
            current_call=proxy_call,
        )


if __name__ == "__main__":
    unittest.main()
