"""Signed hot-reinstall regressions for the process-lifetime damage hook."""

import hashlib
import struct
import unittest
from unittest import mock

import damage_meter as meter


class CountingDamageProcess:
    MOVEMENT_BASE = 0x30000000
    THEME_BASE = 0x31000000
    HOOK_BASE = 0x71000000

    def __init__(self) -> None:
        self.handle = 0x1234
        self.memory: dict[int, int] = {}
        self.writes: list[tuple[int, bytes]] = []
        self.patches: list[tuple[int, bytes]] = []
        movement_target = self.MOVEMENT_BASE + meter.runtime.MOVEMENT_CAVE_OFFSET
        self.seed(
            meter.runtime.MOVEMENT_HOOK_SITE,
            b"\xE9"
            + meter.runtime.rel32(
                meter.runtime.MOVEMENT_HOOK_SITE,
                5,
                movement_target,
            ),
        )
        self.seed(self.MOVEMENT_BASE, meter.runtime.CAVE_MAGIC)
        self.seed(self.THEME_BASE, meter.runtime.DAMAGE_NUMBER_THEME_MAGIC)
        theme_target = (
            self.THEME_BASE + meter.runtime.DAMAGE_NUMBER_SOURCE_WRAPPER_OFFSET
        )
        self.seed(
            meter.runtime.DAMAGE_NUMBER_SOURCE_CALL_SITE,
            b"\xE8"
            + meter.runtime.rel32(
                meter.runtime.DAMAGE_NUMBER_SOURCE_CALL_SITE,
                5,
                theme_target,
            ),
        )

    @property
    def proxy_base(self) -> int:
        return self.THEME_BASE + meter.runtime.DAMAGE_METER_PROXY_WORKSPACE_OFFSET

    def seed(self, address: int, payload: bytes) -> None:
        for offset, value in enumerate(bytes(payload)):
            self.memory[int(address) + offset] = value

    def read(self, address: int, size: int) -> bytes:
        return bytes(
            self.memory.get(int(address) + offset, 0)
            for offset in range(int(size))
        )

    def write(self, address: int, payload: bytes) -> None:
        payload = bytes(payload)
        self.writes.append((int(address), payload))
        self.seed(address, payload)

    def patch_code(self, address: int, payload: bytes) -> None:
        payload = bytes(payload)
        self.patches.append((int(address), payload))
        self.seed(address, payload)

    def u64(self, address: int) -> int:
        return struct.unpack("<Q", self.read(address, 8))[0]

    def ensure_damage_number_theme_allocation(self) -> int:
        return self.THEME_BASE

    def clear_activity(self) -> None:
        self.writes.clear()
        self.patches.clear()


class DamageHookHotReinstallTests(unittest.TestCase):
    def install_fresh(self):
        process = CountingDamageProcess()
        hook = meter.DamageHook(process)
        with mock.patch.object(
            meter.runtime,
            "allocate_executable",
            return_value=process.HOOK_BASE,
        ):
            hook.install()
        return process, hook

    def adopt(self, process):
        adopted = meter.DamageHook(process)
        with mock.patch.object(
            meter.runtime,
            "allocate_executable",
            side_effect=AssertionError("exact adoption must not allocate"),
        ):
            adopted.install()
        return adopted

    def assert_read_only_failure(self, process, pattern):
        expected_memory = dict(process.memory)
        process.clear_activity()
        candidate = meter.DamageHook(process)
        with (
            mock.patch.object(
                meter.runtime,
                "allocate_executable",
                side_effect=AssertionError("mismatch must fail before allocation"),
            ),
            self.assertRaisesRegex(RuntimeError, pattern),
        ):
            candidate.install()
        self.assertFalse(candidate.owns_static)
        self.assertEqual(candidate.base, 0)
        self.assertEqual(process.memory, expected_memory)
        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_fresh_install_publishes_signed_header_wrapper_and_full_proxy(self):
        process, hook = self.install_fresh()
        site = meter.runtime.DAMAGE_NUMBER_SOURCE_CALL_SITE
        old_target = (
            process.THEME_BASE + meter.runtime.DAMAGE_NUMBER_SOURCE_WRAPPER_OFFSET
        )
        wrapper = meter.build_wrapper(hook.wrapper, hook.base, old_target)
        raw_header = process.read(hook.base, meter.HOOK_HEADER_SIZE)
        fields = struct.unpack(meter.HOOK_HEADER_FORMAT, raw_header)

        self.assertTrue(hook.owns_static)
        self.assertEqual(meter.HOOK_VERSION, 2)
        self.assertEqual(meter.HOOK_HEADER_SIZE, 0x50)
        self.assertEqual(fields[0], meter.HOOK_MAGIC)
        self.assertEqual(fields[1], meter.HOOK_VERSION)
        self.assertEqual(fields[2], meter.RING_CAPACITY)
        self.assertEqual(fields[3], 0)
        self.assertEqual(fields[4], old_target)
        self.assertEqual(fields[5], site)
        self.assertEqual(fields[6], len(wrapper))
        self.assertEqual(fields[7], meter.HOOK_HEADER_SIZE)
        self.assertEqual(fields[8], hashlib.sha256(wrapper).digest())
        self.assertEqual(
            raw_header,
            meter.build_hook_header(old_target, site, wrapper),
        )
        self.assertEqual(process.read(hook.wrapper, len(wrapper)), wrapper)
        self.assertEqual(
            process.read(process.proxy_base, meter.PROXY_IMAGE_SIZE),
            meter.build_proxy_image(hook.base, old_target, hook.wrapper),
        )
        self.assertEqual(len(process.writes[-1][1]), meter.PROXY_IMAGE_SIZE)
        self.assertEqual(
            process.read(site, 5),
            b"\xE8"
            + meter.runtime.rel32(
                site,
                5,
                process.proxy_base + meter.PROXY_CODE_OFFSET,
            ),
        )

    def test_exact_adoption_is_read_only_and_does_not_claim_static_ownership(self):
        process, owner = self.install_fresh()
        process.seed(owner.base + meter.HEADER_SEQUENCE_OFFSET, struct.pack("<Q", 91))
        expected_memory = dict(process.memory)
        process.clear_activity()

        adopted = self.adopt(process)

        self.assertEqual(adopted.base, owner.base)
        self.assertEqual(adopted.wrapper, owner.wrapper)
        self.assertFalse(adopted.owns_static)
        self.assertEqual(adopted.sequence(), 91)
        self.assertEqual(process.memory, expected_memory)
        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_header_metadata_and_digest_mismatches_abort_read_only(self):
        cases = (
            ("old_target", meter.HEADER_OLD_TARGET_OFFSET, "header/signature"),
            ("site", meter.HEADER_SITE_OFFSET, "header/signature"),
            ("wrapper_size", meter.HEADER_WRAPPER_SIZE_OFFSET, "header/signature"),
            (
                "wrapper_digest",
                meter.HEADER_WRAPPER_SHA256_OFFSET,
                "header/signature",
            ),
        )
        for case, offset, error in cases:
            with self.subTest(case=case):
                process, owner = self.install_fresh()
                original = process.read(owner.base + offset, 1)[0]
                process.seed(owner.base + offset, bytes((original ^ 0x01,)))
                self.assert_read_only_failure(process, error)

    def test_internally_consistent_proxy_cannot_chain_a_different_theme(self):
        process, owner = self.install_fresh()
        alternate_theme = 0x32000000
        alternate_target = (
            alternate_theme + meter.runtime.DAMAGE_NUMBER_SOURCE_WRAPPER_OFFSET
        )
        process.seed(alternate_theme, meter.runtime.DAMAGE_NUMBER_THEME_MAGIC)
        alternate_wrapper = meter.build_wrapper(
            owner.wrapper,
            owner.base,
            alternate_target,
        )
        process.seed(
            owner.base,
            meter.build_hook_header(
                alternate_target,
                meter.runtime.DAMAGE_NUMBER_SOURCE_CALL_SITE,
                alternate_wrapper,
            ),
        )
        process.seed(owner.wrapper, alternate_wrapper)
        process.seed(
            process.proxy_base,
            meter.build_proxy_image(
                owner.base,
                alternate_target,
                owner.wrapper,
            ),
        )

        self.assert_read_only_failure(process, "proxy old target mismatch")

    def test_wrapper_byte_mismatch_aborts_read_only(self):
        process, owner = self.install_fresh()
        offset = owner.wrapper + 7
        process.seed(offset, bytes((process.read(offset, 1)[0] ^ 0x01,)))

        self.assert_read_only_failure(process, "wrapper mismatch")

    def test_any_proxy_image_mismatch_aborts_read_only(self):
        for case, offset in (
            ("jump", meter.PROXY_CODE_OFFSET + 3),
            ("trailing_padding", meter.PROXY_CODE_OFFSET + meter.PROXY_JUMP_SIZE + 9),
        ):
            with self.subTest(case=case):
                process, _owner = self.install_fresh()
                address = process.proxy_base + offset
                process.seed(address, bytes((process.read(address, 1)[0] ^ 0x01,)))
                self.assert_read_only_failure(process, "proxy image mismatch")

    def test_inactive_nonempty_proxy_aborts_before_allocation_or_write(self):
        process = CountingDamageProcess()
        process.seed(process.proxy_base + meter.PROXY_IMAGE_SIZE - 1, b"\xA5")

        self.assert_read_only_failure(process, "proxy workspace is not empty")

    def test_ambiguous_root_patch_failure_never_frees_and_remains_adoptable(self):
        process = CountingDamageProcess()
        hook = meter.DamageHook(process)
        normal_patch = process.patch_code

        def publish_then_fail(address, payload):
            normal_patch(address, payload)
            raise OSError("ambiguous remote patch result")

        process.patch_code = publish_then_fail
        with (
            mock.patch.object(
                meter.runtime,
                "allocate_executable",
                return_value=process.HOOK_BASE,
            ),
            mock.patch.object(
                meter.runtime.kernel32,
                "VirtualFreeEx",
                side_effect=AssertionError("published allocation must stay resident"),
            ) as free,
            self.assertRaisesRegex(OSError, "ambiguous remote patch result"),
        ):
            hook.install()

        self.assertEqual(hook.base, 0)
        self.assertFalse(hook.owns_static)
        free.assert_not_called()
        process.patch_code = normal_patch
        process.clear_activity()

        adopted = self.adopt(process)

        self.assertEqual(adopted.base, process.HOOK_BASE)
        self.assertFalse(adopted.owns_static)
        self.assertEqual(process.writes, [])
        self.assertEqual(process.patches, [])

    def test_owner_and_adopted_close_leave_root_proxy_and_allocation_resident(self):
        for ownership in ("owner", "adopted"):
            with self.subTest(ownership=ownership):
                process, owner = self.install_fresh()
                hook = owner
                if ownership == "adopted":
                    process.clear_activity()
                    hook = self.adopt(process)
                expected_memory = dict(process.memory)
                process.clear_activity()

                with mock.patch.object(
                    meter.runtime.kernel32,
                    "VirtualFreeEx",
                    side_effect=AssertionError("resident hook must never be freed"),
                ) as free:
                    hook.close()

                self.assertEqual(hook.base, 0)
                self.assertEqual(hook.wrapper, 0)
                self.assertFalse(hook.owns_static)
                self.assertEqual(process.memory, expected_memory)
                self.assertEqual(process.writes, [])
                self.assertEqual(process.patches, [])
                free.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
