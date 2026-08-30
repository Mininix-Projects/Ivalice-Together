import struct
import unittest
from unittest import mock

import headless_menu_shell as menu


class FakeProcess:
    def __init__(self):
        self.data = {}
        self.patch_calls = 0
        self.writes = []
        self.dual_base = 0x5A000000
        self.before_consumer_claim = None
        self.consumer_calls = 0
        self.consumer_rejections = 0
        self.defer_consumer_completion = False
        self.atomic_exact_result = 1
        self.atomic_exact_handle = 0x00100019
        for site, original in (
            (
                menu.legacy.coop.FOLLOWER_NPC_ENTRY_GUARD_SITE,
                menu.legacy.coop.FOLLOWER_NPC_ENTRY_GUARD_ORIGINAL,
            ),
            (
                menu.legacy.coop.FOLLOWER_NPC_ACCEPT_GUARD_SITE,
                menu.legacy.coop.FOLLOWER_NPC_ACCEPT_GUARD_ORIGINAL,
            ),
            (
                menu.legacy.coop.FOLLOWER_NPC_CLEANUP_GUARD_SITE,
                menu.legacy.coop.FOLLOWER_NPC_CLEANUP_GUARD_ORIGINAL,
            ),
        ):
            for offset, value in enumerate(original):
                self.data[site + offset] = value

    def _consumer_gate_allows(self, mailbox):
        policy = self.u32(mailbox + menu.P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET)
        if policy == menu.P2_INTERACTION_CONSUMER_POLICY_TREASURE:
            return True
        if not (
            self.read(menu.NATIVE_RENDERING_FLAGS, 1)[0]
            & menu.NATIVE_UI_RENDER_ENABLED_MASK
        ):
            return False
        gate_handle = self.u32(
            mailbox + menu.P2_INTERACTION_CONSUMER_GATE_HANDLE_OFFSET
        )
        gate_entry = menu.native_interaction.resolve_handle(self, gate_handle)
        gate_actor = self.u64(gate_entry + 0x30) if gate_entry else 0
        if not gate_actor:
            return False
        flags = self.u64(gate_actor)
        if policy in (
            menu.P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_LEADER,
            menu.P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_EXACT,
            menu.P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_EXACT_SCAN,
        ):
            return menu.follower_interaction_leader_allows(flags)
        return menu.interaction_press_allowed(
            flags,
            self.u32(gate_actor + menu.P2_ACTION_STATE_OFFSET),
            self.u32(gate_actor + menu.P2_ACTION_CURRENT_OFFSET),
        )

    def _consumer_target_allows(self, target_entry):
        if self.read(target_entry + 0x18, 1) != b"\x01" and not self.u64(
            target_entry + 0x40
        ):
            return False
        if self.u32(target_entry + 0x1C) & 0x2000 and not self.u64(
            target_entry + 0xF8
        ):
            return False
        return True

    def write(self, address, payload):
        self.writes.append((address, bytes(payload)))
        for offset, value in enumerate(payload):
            self.data[address + offset] = value
        mailbox = self.dual_base + menu.P2_INTERACTION_CONSUMER_MAILBOX_OFFSET
        if address == mailbox + menu.P2_INTERACTION_CONSUMER_ARMED_OFFSET and bytes(
            payload
        ) == struct.pack("<I", 1):
            if self.before_consumer_claim is not None:
                self.before_consumer_claim(self, mailbox)
            if not self._consumer_gate_allows(mailbox):
                for offset, value in enumerate(struct.pack("<I", 3)):
                    self.data[
                        mailbox + menu.P2_INTERACTION_CONSUMER_ARMED_OFFSET + offset
                    ] = value
                self.data[mailbox + menu.P2_INTERACTION_CONSUMER_DONE_OFFSET] = 1
                self.consumer_rejections += 1
                return
            for offset, value in enumerate(struct.pack("<I", 2)):
                self.data[
                    mailbox + menu.P2_INTERACTION_CONSUMER_ARMED_OFFSET + offset
                ] = value
            if self.defer_consumer_completion:
                return
            policy = self.u32(
                mailbox + menu.P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET
            )
            source_handle = self.u32(
                mailbox + menu.P2_INTERACTION_CONSUMER_SOURCE_HANDLE_OFFSET
            )
            source_entry = menu.native_interaction.resolve_handle(self, source_handle)
            if not source_entry:
                self.data[mailbox + menu.P2_INTERACTION_CONSUMER_DONE_OFFSET] = 1
                return
            if policy in (
                menu.P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_EXACT,
                menu.P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_EXACT_SCAN,
            ):
                if not self.u64(source_entry + 0xB8):
                    self.data[mailbox + menu.P2_INTERACTION_CONSUMER_DONE_OFFSET] = 1
                    return
                result = int(bool(self.atomic_exact_result))
                target_entry = menu.native_interaction.resolve_handle(
                    self, self.atomic_exact_handle
                )
                result = int(bool(result and target_entry))
                if (
                    result
                    and policy
                    == menu.P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_EXACT
                    and not self._consumer_target_allows(target_entry)
                ):
                    result = 0
                if result and policy == menu.P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_EXACT_SCAN:
                    descriptor = struct.pack(
                        "<6I",
                        menu.NATIVE_INTERACTION_DESCRIPTOR_DISTANCE_BITS,
                        (self.atomic_exact_handle >> 16) & 0xF,
                        self.atomic_exact_handle & 0xFFFF,
                        2,
                        self.atomic_exact_handle,
                        menu.NATIVE_INTERACTION_DESCRIPTOR_READY_FLAGS,
                    )
                    for offset, value in enumerate(descriptor):
                        self.data[
                            mailbox
                            + menu.P2_INTERACTION_CONSUMER_DESCRIPTOR_OFFSET
                            + offset
                        ] = value
                for offset, value in enumerate(struct.pack("<I", result)):
                    self.data[
                        mailbox
                        + menu.P2_INTERACTION_CONSUMER_EXACT_RESULT_OFFSET
                        + offset
                    ] = value
                self.data[mailbox + menu.P2_INTERACTION_CONSUMER_HIT_OFFSET] = result
                self.data[mailbox + menu.P2_INTERACTION_CONSUMER_DONE_OFFSET] = 1
                if policy == menu.P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_EXACT:
                    self.consumer_calls += result
                return
            descriptor = self.read(
                mailbox + menu.P2_INTERACTION_CONSUMER_DESCRIPTOR_OFFSET,
                0x18,
            )
            target_handle = struct.unpack_from("<I", descriptor, 0x10)[0]
            target_entry = menu.native_interaction.resolve_handle(self, target_handle)
            result = int(
                bool(target_entry and self._consumer_target_allows(target_entry))
            )
            self.consumer_calls += result
            self.data[mailbox + menu.P2_INTERACTION_CONSUMER_HIT_OFFSET] = result
            for offset, value in enumerate(struct.pack("<I", result)):
                self.data[
                    mailbox
                    + menu.P2_INTERACTION_CONSUMER_EXACT_RESULT_OFFSET
                    + offset
                ] = value
            self.data[mailbox + menu.P2_INTERACTION_CONSUMER_DONE_OFFSET] = 1

    def read(self, address, size):
        return bytes(self.data.get(address + offset, 0) for offset in range(size))

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]

    def patch_code(self, address, payload):
        self.patch_calls += 1
        self.write(address, payload)

    def ensure_dual_interaction_allocation(self):
        menu.legacy.coop.set_dual_interaction_base(self.dual_base)
        return self.dual_base


class FakeBridge:
    def __init__(self):
        self.pulses = []
        self.releases = 0

    def pulse(self, button, duration=None):
        self.pulses.append((button, duration))

    def release(self):
        self.releases += 1


class InteractionOwnerRouterTests(unittest.TestCase):
    def test_consumer_claims_request_atomically_before_reading_policy(self):
        wrapper = menu.build_p2_interaction_consumer_wrapper(0x51001000)
        claim = b"\xb8\x01\x00\x00\x00\x41\xba\x02\x00\x00\x00"
        claim += b"\xf0\x44\x0f\xb1\x53\x08\x0f\x85"
        policy = b"\x83\x7b" + bytes(
            (menu.P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET,)
        )

        self.assertEqual(wrapper.count(b"\xf0\x44\x0f\xb1\x53\x08"), 1)
        self.assertLess(wrapper.index(claim), wrapper.index(policy))
        self.assertNotIn(b"\xc7\x43\x08\x02\x00\x00\x00", wrapper)

    def test_exact_installed_router_is_adopted_without_any_write(self):
        owner = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        writes_before = len(self.process.writes)
        patches_before = self.process.patch_calls

        adopted = menu.NativeWorldInteractionRouter(self.process, FakeBridge())

        self.assertEqual(len(self.process.writes), writes_before)
        self.assertEqual(self.process.patch_calls, patches_before)
        self.assertTrue(adopted.consumer_installed)
        self.assertFalse(adopted.consumer_static_owned)
        self.assertTrue(adopted.atomic_npc_guards_installed)
        self.assertFalse(adopted.atomic_npc_guards_owned)

        adopted.shutdown()
        self.assertEqual(self.process.patch_calls, patches_before)
        self.assertEqual(
            self.process.read(
                menu.NATIVE_INTERACTION_CONSUMER_CALL,
                len(owner.consumer_patch),
            ),
            owner.consumer_patch,
        )

    def test_active_atomic_cave_mismatch_aborts_without_hot_repair(self):
        owner = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        self.process.write(owner.atomic_npc_cave + 0x20, b"\xCC")
        writes_before = len(self.process.writes)
        patches_before = self.process.patch_calls

        with self.assertRaisesRegex(RuntimeError, "atomic NPC helper cave mismatch"):
            menu.NativeWorldInteractionRouter(self.process, FakeBridge())

        self.assertEqual(len(self.process.writes), writes_before)
        self.assertEqual(self.process.patch_calls, patches_before)
        self.assertEqual(
            self.process.read(
                menu.NATIVE_INTERACTION_CONSUMER_CALL,
                len(owner.consumer_patch),
            ),
            owner.consumer_patch,
        )

    def test_game_thread_rejects_interaction_after_ui_suppression_begins(self):
        mailbox = 0x51001000
        wrapper = menu.build_p2_interaction_consumer_wrapper(mailbox)
        ui_guard = (
            b"\x48\xb8"
            + struct.pack("<Q", menu.NATIVE_RENDERING_FLAGS)
            + b"\xf6\x00"
            + bytes((menu.NATIVE_UI_RENDER_ENABLED_MASK,))
        )
        guard = wrapper.index(ui_guard)
        jump = guard + len(ui_guard)
        claim = wrapper.index(b"\xf0\x44\x0f\xb1\x53\x08")
        reject = wrapper.index(b"\xc7\x43\x08\x03\x00\x00\x00")
        relative = struct.unpack_from("<i", wrapper, jump + 2)[0]

        self.assertLess(claim, guard)
        self.assertEqual(wrapper[jump : jump + 2], b"\x0f\x84")
        self.assertEqual(jump + 6 + relative, reject)
        self.assertLess(claim, reject)

    def test_follower_policy_skips_only_stock_state_action_checks(self):
        wrapper = menu.build_p2_interaction_consumer_wrapper(0x51001000)
        policy = b"\x83\x7b" + bytes(
            (
                menu.P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET,
                menu.P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_LEADER,
            )
        )
        state_check = b"\x83\xb8\xb4\x06\x00\x00\x01"
        action_check = b"\x81\xb8\x14\x07\x00\x00\x21\x40\x00\x00"
        claim = wrapper.index(b"\xf0\x44\x0f\xb1\x53\x08")
        atomic_helper = (
            0x51001000
            - menu.P2_INTERACTION_CONSUMER_MAILBOX_OFFSET
            + menu.FOLLOWER_NPC_ATOMIC_CAVE_OFFSET
        )
        consume = wrapper.index(
            b"\x48\xb8" + struct.pack("<Q", atomic_helper) + b"\xff\xd0"
        )
        state = wrapper.index(state_check)
        action = wrapper.index(action_check)
        policy_position = wrapper.rfind(policy, 0, state)
        jump = policy_position + len(policy)
        relative = struct.unpack_from("<i", wrapper, jump + 2)[0]

        self.assertGreaterEqual(policy_position, 0)
        self.assertEqual(wrapper[jump : jump + 2], b"\x0f\x84")
        self.assertEqual(jump + 6 + relative, consume)
        self.assertLess(claim, policy_position)
        self.assertLess(policy_position, state)
        self.assertLess(state, action)
        self.assertLess(action, consume)

    def test_follower_gate_checks_execution_bit_before_policy_branch(self):
        wrapper = menu.build_p2_interaction_consumer_wrapper(0x51001000)
        battle_root = (
            b"\x48\xb8"
            + struct.pack("<Q", menu.session_state.BATTLE_INFO_GLOBAL)
            + b"\x48\x8b\x00"
        )
        leader_compare = b"\x3b\x43" + bytes(
            (menu.P2_INTERACTION_CONSUMER_LEADER_MEMBER_OFFSET,)
        )
        gate_handle = b"\x8b\x4b" + bytes(
            (menu.P2_INTERACTION_CONSUMER_GATE_HANDLE_OFFSET,)
        )
        resolver = (
            b"\x48\xb8"
            + struct.pack("<Q", menu.NATIVE_ENTITY_RESOLVER)
            + b"\xff\xd0"
        )
        actor_from_entry = b"\x48\x8b\x40\x30"
        bit24 = b"\xf7\x00\x00\x00\x00\x01"
        bit46 = b"\xf7\x40\x04\x00\x40\x00\x00"
        follower_policy = b"\x83\x7b" + bytes(
            (
                menu.P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET,
                menu.P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_LEADER,
            )
        )
        claim = wrapper.index(b"\xf0\x44\x0f\xb1\x53\x08")

        positions = tuple(
            wrapper.index(pattern)
            for pattern in (
                gate_handle,
                resolver,
                actor_from_entry,
                bit24,
                bit46,
                follower_policy,
            )
        )
        self.assertEqual(positions, tuple(sorted(positions)))
        self.assertLess(claim, positions[0])
        self.assertNotIn(battle_root, wrapper)
        self.assertNotIn(leader_compare, wrapper)
        self.assertNotIn(
            b"\x48\x8b\x43"
            + bytes((menu.P2_INTERACTION_CONSUMER_GATE_ACTOR_OFFSET,)),
            wrapper,
        )

    def test_reject_path_leaves_stock_globals_untouched_before_native_consumer(self):
        wrapper = menu.build_p2_interaction_consumer_wrapper(0x51001000)
        rejected = b"\xc7\x43\x08\x03\x00\x00\x00"
        native_call = (
            b"\x48\xb8"
            + struct.pack("<Q", menu.NATIVE_INTERACTION_CONSUMER_TARGET)
            + b"\xff\xd0"
        )
        self.assertNotIn(struct.pack("<Q", menu.NATIVE_INTERACTION_SOURCE), wrapper)
        self.assertNotIn(struct.pack("<Q", menu.NATIVE_INTERACTION_DESCRIPTOR), wrapper)
        self.assertLess(wrapper.index(rejected), wrapper.index(native_call))

    def setUp(self):
        self.process = FakeProcess()
        self.bridge = FakeBridge()
        self.process.write(
            menu.NATIVE_INTERACTION_CONSUMER_CALL,
            menu.NATIVE_INTERACTION_CONSUMER_CALL_ORIGINAL,
        )
        self.private = 0x60000000
        self.p1 = 0x71000000
        self.p2 = 0x72000000
        self.slot = 3
        self.handle = 0x00100011
        self.objects = 0x73000000
        self.interaction_object = 0x74000000
        self.owner_actor = 0x75000000
        self.leader_actor = 0x75100000
        self.owner_entry = 0x75400000
        self.leader_entry = 0x75500000
        self.owner_handle = 0x00100020
        self.leader_handle = 0x00100021
        self.leader_member_id = 0
        self.battle_info = 0x75200000
        self.old_descriptor = struct.pack("<6I", 0, 0, 0, 0, 0, 0x20)
        command = menu.legacy.coop.COMMAND_BLOCK
        self.process.write(
            command + menu.P2_INTERACTION_STATE_POINTER_OFFSET,
            struct.pack("<Q", self.private),
        )
        self.process.write(
            command + menu.P2_INTERACTION_ENTRY_POINTER_OFFSET,
            struct.pack("<Q", self.p2),
        )
        self.process.write(menu.NATIVE_INTERACTION_SOURCE, struct.pack("<Q", self.p1))
        self.process.write(self.p1 + menu.NATIVE_INTERACTION_RANGE_FLAG_OFFSET, b"\x08")
        self.process.write(
            self.private + menu.P2_INTERACTION_RESOURCE_SLOTS_OFFSET + self.slot * 8,
            struct.pack("<Q", 0x12345678),
        )
        self.process.write(
            self.private + menu.P2_INTERACTION_TEXT_SLOTS_OFFSET + self.slot * 8,
            struct.pack("<Q", 0x23456789),
        )
        self.process.write(
            self.private + menu.P2_INTERACTION_STATE_SLOTS_OFFSET + self.slot * 4,
            struct.pack("<I", 2),
        )
        self.process.write(
            self.private + menu.P2_INTERACTION_HANDLE_SLOTS_OFFSET + self.slot * 4,
            struct.pack("<I", self.handle),
        )
        registry = menu.native_interaction.REGISTRY_BASE
        self.process.write(registry + 8, struct.pack("<Q", self.objects))
        self.process.write(self.objects, struct.pack("<I", 0x40))
        self.process.write(
            self.objects + 8 + (self.handle & 0xFFFF) * 8,
            struct.pack("<Q", self.interaction_object),
        )
        self.process.write(self.interaction_object + 0x16, struct.pack("<H", 1))
        self.process.write(self.interaction_object + 0x18, struct.pack("<I", 1))
        self.process.write(
            self.interaction_object
            + menu.native_interaction.INTERACTION_NAME_KEY_OFFSET,
            struct.pack("<h", 0),
        )
        self.process.write(self.interaction_object + 0x0C, b"\x40")
        self.process.write(menu.NATIVE_INTERACTION_CAN_USE_GATE, b"\x00")
        self.process.write(
            menu.NATIVE_RENDERING_FLAGS,
            bytes((menu.NATIVE_UI_RENDER_ENABLED_MASK,)),
        )
        self.process.write(menu.NATIVE_INTERACTION_DESCRIPTOR, self.old_descriptor)
        self.process.write(self.owner_actor, struct.pack("<Q", 0))
        self.process.write(self.owner_actor + 0x08, struct.pack("<I", self.owner_handle))
        self.process.write(
            self.objects + 8 + (self.owner_handle & 0xFFFF) * 8,
            struct.pack("<Q", self.owner_entry),
        )
        self.process.write(self.owner_entry + 0x16, struct.pack("<H", 1))
        self.process.write(self.owner_entry + 0x30, struct.pack("<Q", self.owner_actor))
        self.process.write(
            self.owner_actor + menu.P2_ACTION_STATE_OFFSET,
            struct.pack("<I", 1),
        )
        self.process.write(
            self.owner_actor + menu.P2_ACTION_CURRENT_OFFSET,
            struct.pack("<I", 0x4021),
        )
        self.process.write(
            menu.session_state.BATTLE_INFO_GLOBAL,
            struct.pack("<Q", self.battle_info),
        )
        self.process.write(
            self.battle_info + menu.session_state.BATTLE_LEADER_OFFSET,
            struct.pack("<I", self.leader_member_id),
        )
        self.process.write(self.leader_actor, struct.pack("<Q", 0))
        self.process.write(
            self.leader_actor + 0x08,
            struct.pack("<I", self.leader_handle),
        )
        self.process.write(
            self.objects + 8 + (self.leader_handle & 0xFFFF) * 8,
            struct.pack("<Q", self.leader_entry),
        )
        self.process.write(self.leader_entry + 0x16, struct.pack("<H", 1))
        self.process.write(self.leader_entry + 0x30, struct.pack("<Q", self.leader_actor))

    def test_promotes_exact_owner_through_one_persistent_consumer_hook(self):
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        installed_patches = self.process.patch_calls
        writes_before = len(self.process.writes)
        self.assertTrue(
            router.trigger(
                self.handle,
                self.slot,
                owner_actor=self.owner_actor,
                leader_actor=self.leader_actor,
            )
        )
        trigger_writes = self.process.writes[writes_before:]
        self.assertFalse(
            any(address == menu.NATIVE_INTERACTION_SOURCE for address, _ in trigger_writes)
        )
        mailbox = self.process.dual_base + menu.P2_INTERACTION_CONSUMER_MAILBOX_OFFSET
        self.assertEqual(
            self.process.u32(mailbox + menu.P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET),
            menu.P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_LEADER,
        )
        # The leader and source cross the Python/native boundary only as
        # generation handles. The game thread resolves both at consumption.
        self.assertEqual(
            self.process.u32(
                mailbox + menu.P2_INTERACTION_CONSUMER_LEADER_MEMBER_OFFSET
            ),
            0,
        )
        self.assertEqual(
            self.process.u32(mailbox + menu.P2_INTERACTION_CONSUMER_GATE_HANDLE_OFFSET),
            self.leader_handle,
        )
        self.assertEqual(
            self.process.u64(mailbox + menu.P2_INTERACTION_CONSUMER_GATE_ACTOR_OFFSET),
            0,
        )
        self.assertEqual(
            self.process.u32(mailbox + menu.P2_INTERACTION_CONSUMER_SOURCE_HANDLE_OFFSET),
            self.owner_handle,
        )
        self.assertEqual(
            self.process.read(menu.NATIVE_INTERACTION_DESCRIPTOR, 0x18),
            self.old_descriptor,
        )
        self.assertEqual(self.process.read(self.interaction_object + 0x0C, 1), b"\x40")
        self.assertEqual(
            self.process.read(menu.NATIVE_INTERACTION_CAN_USE_GATE, 1), b"\x00"
        )
        # The interaction edge belongs to the native descriptor only.  No
        # global controller Confirm may leak into Vaan's command menu.
        self.assertFalse(self.bridge.pulses)
        self.assertEqual(self.bridge.releases, 0)
        # The persistent consumer and three exact-scan guards are installed at
        # construction; the individual press does not rewrite any hook site.
        self.assertEqual(installed_patches, 4)
        self.assertEqual(self.process.patch_calls, installed_patches)
        self.assertEqual(self.process.u64(menu.NATIVE_INTERACTION_SOURCE), self.p1)
        self.assertEqual(
            self.process.read(self.p1 + menu.NATIVE_INTERACTION_RANGE_FLAG_OFFSET, 1),
            b"\x08",
        )
        self.assertEqual(
            self.process.read(menu.NATIVE_INTERACTION_DESCRIPTOR, 0x18),
            self.old_descriptor,
        )
        self.assertEqual(self.process.read(self.interaction_object + 0x0C, 1), b"\x40")
        self.assertEqual(
            self.process.read(menu.NATIVE_INTERACTION_CAN_USE_GATE, 1), b"\x00"
        )
        self.assertEqual(
            self.process.read(
                menu.legacy.coop.COMMAND_BLOCK + menu.P2_INTERACTION_OWNER_OFFSET,
                1,
            ),
            bytes((menu.INTERACTION_OWNER_NONE,)),
        )

    def test_mailbox_publication_does_not_touch_native_state_before_claim(self):
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        writes_before = len(self.process.writes)

        self.assertTrue(
            router.trigger(
                self.handle,
                self.slot,
                owner_actor=self.owner_actor,
                leader_actor=self.leader_actor,
            )
        )

        writes = self.process.writes[writes_before:]
        armed_index = next(
            index
            for index, write in enumerate(writes)
            if write
            == (
                router.consumer_mailbox + menu.P2_INTERACTION_CONSUMER_ARMED_OFFSET,
                struct.pack("<I", 1),
            )
        )
        forbidden_addresses = {
            menu.NATIVE_INTERACTION_SOURCE,
            menu.NATIVE_INTERACTION_DESCRIPTOR,
            menu.NATIVE_INTERACTION_CAN_USE_GATE,
            self.interaction_object + 0x0C,
            self.p1 + menu.NATIVE_INTERACTION_RANGE_FLAG_OFFSET,
        }
        self.assertFalse(
            any(address in forbidden_addresses for address, _ in writes[:armed_index])
        )
        mailbox_descriptor = self.process.read(
            router.consumer_mailbox + menu.P2_INTERACTION_CONSUMER_DESCRIPTOR_OFFSET,
            0x18,
        )
        self.assertEqual(
            struct.unpack_from("<I", mailbox_descriptor, 0x14)[0] & 1,
            1,
        )
        self.assertEqual(
            self.process.u32(
                router.consumer_mailbox
                + menu.P2_INTERACTION_CONSUMER_SOURCE_HANDLE_OFFSET
            ),
            self.owner_handle,
        )
        self.assertEqual(
            self.process.u32(
                router.consumer_mailbox
                + menu.P2_INTERACTION_CONSUMER_GATE_HANDLE_OFFSET
            ),
            self.leader_handle,
        )

    def test_p3_uses_its_explicit_snapshot_and_owner_without_aliasing_p2(self):
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        p3_handle = 0x00100022
        p3_entry = 0x76000000
        self.process.write(
            self.objects + 8 + (p3_handle & 0xFFFF) * 8,
            struct.pack("<Q", p3_entry),
        )
        self.process.write(p3_entry + 0x16, struct.pack("<H", 1))
        self.process.write(p3_entry + 0x30, struct.pack("<Q", self.owner_actor))
        writes_before = len(self.process.writes)
        self.assertTrue(
            router.trigger(
                self.handle,
                self.slot,
                owner_actor=self.owner_actor,
                leader_actor=self.leader_actor,
                source_handle=p3_handle,
                private_state_pointer=self.private,
                interaction_owner=menu.INTERACTION_OWNER_P3,
            )
        )
        trigger_writes = self.process.writes[writes_before:]
        self.assertFalse(
            any(address == menu.NATIVE_INTERACTION_SOURCE for address, _ in trigger_writes)
        )
        self.assertIn(
            (
                router.consumer_mailbox
                + menu.P2_INTERACTION_CONSUMER_SOURCE_HANDLE_OFFSET,
                struct.pack("<I", p3_handle),
            ),
            trigger_writes,
        )
        self.assertIn(
            (
                menu.legacy.coop.COMMAND_BLOCK + menu.P2_INTERACTION_OWNER_OFFSET,
                bytes((menu.INTERACTION_OWNER_P3,)),
            ),
            self.process.writes,
        )
        self.assertEqual(
            self.process.read(
                menu.legacy.coop.COMMAND_BLOCK + menu.P2_INTERACTION_OWNER_OFFSET,
                1,
            ),
            bytes((menu.INTERACTION_OWNER_NONE,)),
        )

    def _native_publication_snapshot(self):
        return (
            self.process.read(menu.NATIVE_INTERACTION_SOURCE, 8),
            self.process.read(menu.NATIVE_INTERACTION_CAN_USE_GATE, 1),
            self.process.read(menu.NATIVE_INTERACTION_DESCRIPTOR, 0x18),
            self.process.read(self.interaction_object + 0x0C, 1),
        )

    def test_stale_source_handle_fails_closed_without_native_publication(self):
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        original = self._native_publication_snapshot()

        def invalidate_source(live_process, _mailbox):
            live_process.write(self.owner_entry + 0x16, b"\x00\x00")

        self.process.before_consumer_claim = invalidate_source
        self.assertFalse(
            router.trigger(
                self.handle,
                self.slot,
                owner_actor=self.owner_actor,
                leader_actor=self.leader_actor,
            )
        )
        self.assertEqual(self.process.consumer_calls, 0)
        self.assertEqual(self._native_publication_snapshot(), original)

    def test_stale_gate_handle_is_rejected_before_custom_consumer(self):
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        original = self._native_publication_snapshot()

        def invalidate_gate(live_process, _mailbox):
            live_process.write(self.leader_entry + 0x16, b"\x00\x00")

        self.process.before_consumer_claim = invalidate_gate
        self.assertFalse(
            router.trigger(
                self.handle,
                self.slot,
                owner_actor=self.owner_actor,
                leader_actor=self.leader_actor,
            )
        )
        self.assertEqual(self.process.consumer_calls, 0)
        self.assertEqual(self.process.consumer_rejections, 1)
        self.assertEqual(self._native_publication_snapshot(), original)

    def test_stale_target_handle_fails_closed_without_native_publication(self):
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        original = self._native_publication_snapshot()

        def invalidate_target(live_process, _mailbox):
            live_process.write(self.interaction_object + 0x16, b"\x00\x00")

        self.process.before_consumer_claim = invalidate_target
        self.assertFalse(
            router.trigger(
                self.handle,
                self.slot,
                owner_actor=self.owner_actor,
                leader_actor=self.leader_actor,
            )
        )
        self.assertEqual(self.process.consumer_calls, 0)
        self.assertEqual(self._native_publication_snapshot(), original)

    def test_partial_target_hierarchy_fails_closed_before_native_consumer(self):
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        original = self._native_publication_snapshot()
        self.process.write(self.interaction_object + 0x18, b"\x00" * 4)
        self.process.write(self.interaction_object + 0x40, b"\x00" * 8)

        self.assertFalse(
            router.trigger(
                self.handle,
                self.slot,
                owner_actor=self.owner_actor,
                leader_actor=self.leader_actor,
            )
        )
        self.assertEqual(self.process.consumer_calls, 0)
        self.assertEqual(self._native_publication_snapshot(), original)

    def test_missing_flagged_target_auxiliary_fails_closed(self):
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        original = self._native_publication_snapshot()
        self.process.write(self.interaction_object + 0x1C, struct.pack("<I", 0x2000))
        self.process.write(self.interaction_object + 0xF8, b"\x00" * 8)

        self.assertFalse(
            router.trigger(
                self.handle,
                self.slot,
                owner_actor=self.owner_actor,
                leader_actor=self.leader_actor,
            )
        )
        self.assertEqual(self.process.consumer_calls, 0)
        self.assertEqual(self._native_publication_snapshot(), original)

    def test_timeout_quarantines_claimed_mailbox_until_done(self):
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        mailbox = router.consumer_mailbox
        self.process.defer_consumer_completion = True

        with mock.patch.object(router, "CONSUMER_TIMEOUT_SECONDS", 0.001):
            self.assertFalse(
                router.trigger(
                    self.handle,
                    self.slot,
                    owner_actor=self.owner_actor,
                    leader_actor=self.leader_actor,
                )
            )

        self.assertTrue(router.consumer_quarantined)
        self.assertEqual(
            self.process.u32(mailbox + menu.P2_INTERACTION_CONSUMER_ARMED_OFFSET),
            2,
        )
        descriptor = self.process.read(
            mailbox + menu.P2_INTERACTION_CONSUMER_DESCRIPTOR_OFFSET,
            0x18,
        )
        self.assertFalse(
            router.trigger(
                self.handle,
                self.slot,
                owner_actor=self.owner_actor,
                leader_actor=self.leader_actor,
            )
        )
        self.assertEqual(
            self.process.read(
                mailbox + menu.P2_INTERACTION_CONSUMER_DESCRIPTOR_OFFSET,
                0x18,
            ),
            descriptor,
        )

        self.process.write(
            mailbox + menu.P2_INTERACTION_CONSUMER_DONE_OFFSET,
            struct.pack("<I", 1),
        )
        router.maintain(now=1.0)
        self.assertFalse(router.consumer_quarantined)
        self.assertEqual(
            self.process.u32(mailbox + menu.P2_INTERACTION_CONSUMER_ARMED_OFFSET),
            0,
        )

        self.process.defer_consumer_completion = False
        self.assertTrue(
            router.trigger(
                self.handle,
                self.slot,
                owner_actor=self.owner_actor,
                leader_actor=self.leader_actor,
            )
        )

    def test_descriptor_only_npc_uses_consumer_without_fabricating_slot(self):
        self.process.write(
            self.private + menu.P2_INTERACTION_EXACT_DESCRIPTOR_OFFSET,
            struct.pack(
                "<6I",
                menu.NATIVE_INTERACTION_DESCRIPTOR_DISTANCE_BITS,
                (self.handle >> 16) & 0xF,
                self.handle & 0xFFFF,
                2,
                self.handle,
                menu.NATIVE_INTERACTION_DESCRIPTOR_READY_FLAGS,
            ),
        )
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        writes_before = len(self.process.writes)

        self.assertTrue(
            router.trigger(
                self.handle,
                -1,
                owner_actor=self.owner_actor,
                leader_actor=self.leader_actor,
            )
        )

        new_writes = self.process.writes[writes_before:]
        self.assertFalse(
            any(address == menu.NATIVE_INTERACTION_OUTPUT for address, _ in new_writes)
        )
        self.assertIn(
            (
                router.consumer_mailbox + menu.P2_INTERACTION_CONSUMER_ARMED_OFFSET,
                struct.pack("<I", 1),
            ),
            new_writes,
        )

    def test_rejects_press_during_leader_weapon_handling_without_publication(self):
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        self.process.write(
            self.leader_actor,
            struct.pack("<Q", 1 << 24),
        )
        writes_before = len(self.process.writes)
        self.assertFalse(
            router.trigger(
                self.handle,
                self.slot,
                owner_actor=self.owner_actor,
                leader_actor=self.leader_actor,
            )
        )
        new_writes = self.process.writes[writes_before:]
        self.assertNotIn(
            (menu.NATIVE_INTERACTION_SOURCE, struct.pack("<Q", self.p2)),
            new_writes,
        )

    def test_busy_follower_does_not_block_idle_leader(self):
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        self.process.write(self.owner_actor, struct.pack("<Q", 1 << 46))
        self.process.write(
            self.owner_actor + menu.P2_ACTION_STATE_OFFSET,
            struct.pack("<I", 10),
        )
        self.process.write(
            self.owner_actor + menu.P2_ACTION_CURRENT_OFFSET,
            struct.pack("<I", 0x96),
        )

        self.assertTrue(
            router.trigger(
                self.handle,
                self.slot,
                owner_actor=self.owner_actor,
                leader_actor=self.leader_actor,
            )
        )

    def test_leader_hit_reaction_blocks_idle_follower(self):
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        self.process.write(self.leader_actor, struct.pack("<Q", 1 << 46))

        self.assertFalse(
            router.trigger(
                self.handle,
                self.slot,
                owner_actor=self.owner_actor,
                leader_actor=self.leader_actor,
            )
        )

    def test_treasure_ignores_leader_and_native_animation_gates(self):
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        self.process.write(
            self.interaction_object
            + menu.native_interaction.INTERACTION_NAME_KEY_OFFSET,
            struct.pack("<h", menu.native_interaction.TREASURE_NAME_KEY),
        )
        self.process.write(self.leader_actor, struct.pack("<Q", 1 << 46))
        self.process.write(self.owner_actor, struct.pack("<Q", 1 << 24))
        self.process.write(menu.NATIVE_RENDERING_FLAGS, b"\x00")
        self.process.write(
            menu.NATIVE_INTERACTION_SUPPRESSION_FLAGS,
            struct.pack("<I", menu.NATIVE_INTERACTION_SUPPRESSION_MASK),
        )
        self.process.write(
            menu.NATIVE_INTERACTION_EVENT_FLAGS,
            struct.pack("<I", menu.NATIVE_INTERACTION_EVENT_BUSY_MASK),
        )

        self.assertTrue(
            router.trigger(
                self.handle,
                self.slot,
                owner_actor=self.owner_actor,
                leader_actor=self.leader_actor,
            )
        )
        mailbox = self.process.dual_base + menu.P2_INTERACTION_CONSUMER_MAILBOX_OFFSET
        self.assertEqual(
            self.process.u32(mailbox + menu.P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET),
            menu.P2_INTERACTION_CONSUMER_POLICY_TREASURE,
        )

    def test_battle_leader_id_churn_is_not_an_extra_animation_gate(self):
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        self.process.write(
            self.battle_info + menu.session_state.BATTLE_LEADER_OFFSET,
            struct.pack("<I", self.leader_member_id + 1),
        )

        self.assertTrue(
            router.trigger(
                self.handle,
                self.slot,
                owner_actor=self.owner_actor,
                leader_actor=self.leader_actor,
            )
        )

    def test_each_route_overwrites_shared_gate_policy(self):
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        mailbox = self.process.dual_base + menu.P2_INTERACTION_CONSUMER_MAILBOX_OFFSET

        self.assertTrue(
            router.trigger(
                self.handle,
                self.slot,
                owner_actor=self.owner_actor,
                leader_actor=self.leader_actor,
            )
        )
        self.assertEqual(
            self.process.u32(mailbox + menu.P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET),
            menu.P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_LEADER,
        )

        self.assertTrue(
            router.trigger_native_p1(
                self.handle,
                owner_actor=self.owner_actor,
            )
        )
        self.assertEqual(
            self.process.u32(mailbox + menu.P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET),
            menu.P2_INTERACTION_CONSUMER_POLICY_STOCK_OWNER,
        )
        self.assertEqual(
            self.process.u32(mailbox + menu.P2_INTERACTION_CONSUMER_GATE_HANDLE_OFFSET),
            self.owner_handle,
        )
        self.assertEqual(
            self.process.u64(mailbox + menu.P2_INTERACTION_CONSUMER_GATE_ACTOR_OFFSET),
            0,
        )

        self.assertTrue(
            router.trigger(
                self.handle,
                self.slot,
                owner_actor=self.owner_actor,
                leader_actor=self.leader_actor,
            )
        )
        self.assertEqual(
            self.process.u32(mailbox + menu.P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET),
            menu.P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_LEADER,
        )

    def test_game_thread_rejects_weapon_handling_started_after_preflight(self):
        for animation_flag in (1 << 24,):
            with self.subTest(animation_flag=animation_flag):
                process = self.process
                router = menu.NativeWorldInteractionRouter(process, self.bridge)

                def begin_animation(live_process, _mailbox):
                    live_process.write(
                        self.leader_actor,
                        struct.pack("<Q", animation_flag),
                    )

                process.before_consumer_claim = begin_animation
                self.assertFalse(
                    router.trigger(
                        self.handle,
                        self.slot,
                        owner_actor=self.owner_actor,
                        leader_actor=self.leader_actor,
                    )
                )
                self.assertEqual(process.consumer_calls, 0)
                self.assertEqual(process.consumer_rejections, 1)

                # A rejected edge is discarded, not queued. Clearing the bit
                # does nothing until a fresh trigger is submitted.
                process.before_consumer_claim = None
                process.write(self.leader_actor, struct.pack("<Q", 0))
                self.assertTrue(
                    router.trigger(
                        self.handle,
                        self.slot,
                        owner_actor=self.owner_actor,
                        leader_actor=self.leader_actor,
                    )
                )
                self.assertEqual(process.consumer_calls, 1)
                self.assertEqual(process.consumer_rejections, 1)

                # Reset counters for the next subcase while preserving the
                # same fully restored fake native publication.
                process.consumer_calls = 0
                process.consumer_rejections = 0

    def test_game_thread_ignores_unrelated_leader_id_churn(self):
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)

        def change_leader(live_process, _mailbox):
            live_process.write(
                self.battle_info + menu.session_state.BATTLE_LEADER_OFFSET,
                struct.pack("<I", self.leader_member_id + 1),
            )

        self.process.before_consumer_claim = change_leader
        self.assertTrue(
            router.trigger(
                self.handle,
                self.slot,
                owner_actor=self.owner_actor,
                leader_actor=self.leader_actor,
            )
        )
        self.assertEqual(self.process.consumer_calls, 1)
        self.assertEqual(self.process.consumer_rejections, 0)

    def test_accepts_stable_weapon_drawn_owner(self):
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        self.process.write(
            self.owner_actor,
            struct.pack("<Q", 1 << 23),
        )
        self.assertTrue(
            router.trigger_native_p1(
                self.handle,
                owner_actor=self.owner_actor,
            )
        )

    def test_rejects_stale_private_handle(self):
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        self.assertFalse(
            router.trigger(
                0x00100012,
                self.slot,
                owner_actor=self.owner_actor,
                leader_actor=self.leader_actor,
            )
        )
        self.assertEqual(self.process.u64(menu.NATIVE_INTERACTION_SOURCE), self.p1)
        self.assertFalse(self.bridge.pulses)

    def test_p1_native_interaction_uses_descriptor_without_confirm_pulse(self):
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        self.assertTrue(
            router.trigger_native_p1(
                self.handle,
                owner_actor=self.owner_actor,
            )
        )
        mailbox = self.process.dual_base + menu.P2_INTERACTION_CONSUMER_MAILBOX_OFFSET
        published = self.process.read(
            mailbox + menu.P2_INTERACTION_CONSUMER_DESCRIPTOR_OFFSET,
            0x18,
        )
        self.assertEqual(struct.unpack_from("<I", published, 0x10)[0], self.handle)
        self.assertEqual(struct.unpack_from("<I", published, 0x14)[0] & 1, 1)
        self.assertFalse(self.bridge.pulses)
        self.assertEqual(self.process.u64(menu.NATIVE_INTERACTION_SOURCE), self.p1)
        self.assertEqual(
            self.process.read(menu.NATIVE_INTERACTION_DESCRIPTOR, 0x18),
            self.old_descriptor,
        )
        self.assertEqual(self.process.read(self.interaction_object + 0x0C, 1), b"\x40")
        self.assertEqual(
            self.process.read(menu.NATIVE_INTERACTION_CAN_USE_GATE, 1), b"\x00"
        )

    def test_p1_native_interaction_rejects_handling_actor(self):
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        self.process.write(self.owner_actor, struct.pack("<Q", 1 << 24))
        writes_before = len(self.process.writes)
        self.assertFalse(
            router.trigger_native_p1(
                self.handle,
                owner_actor=self.owner_actor,
            )
        )
        new_writes = self.process.writes[writes_before:]
        mailbox = self.process.dual_base + menu.P2_INTERACTION_CONSUMER_MAILBOX_OFFSET
        self.assertNotIn(
            (
                mailbox + menu.P2_INTERACTION_CONSUMER_ARMED_OFFSET,
                struct.pack("<I", 1),
            ),
            new_writes,
        )

    def test_p1_treasure_ignores_owner_animation_gate(self):
        router = menu.NativeWorldInteractionRouter(self.process, self.bridge)
        self.process.write(
            self.interaction_object
            + menu.native_interaction.INTERACTION_NAME_KEY_OFFSET,
            struct.pack("<h", menu.native_interaction.TREASURE_NAME_KEY),
        )
        self.process.write(self.owner_actor, struct.pack("<Q", 1 << 24))

        self.assertTrue(
            router.trigger_native_p1(
                self.handle,
                owner_actor=self.owner_actor,
            )
        )
        mailbox = self.process.dual_base + menu.P2_INTERACTION_CONSUMER_MAILBOX_OFFSET
        self.assertEqual(
            self.process.u32(mailbox + menu.P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET),
            menu.P2_INTERACTION_CONSUMER_POLICY_TREASURE,
        )


class FollowerWorldInteractionLeaderTests(unittest.TestCase):
    def setUp(self):
        self.process = FakeProcess()
        self.live = menu.CoopMenuRuntime.__new__(menu.CoopMenuRuntime)
        self.live.process = self.process
        self.dead_p1 = 0x81000000
        self.p2_actor = 0x82000000
        self.p3_actor = 0x83000000
        self.guest_actor = 0x84000000
        self.battle_info = 0x85000000
        self.live.party = [
            ("Vaan", 0x100001, self.dead_p1),
            ("Penelo", 0x100002, self.p2_actor),
            ("Balthier", 0x100003, self.p3_actor),
        ]
        self.process.write(
            menu.session_state.BATTLE_INFO_GLOBAL,
            struct.pack("<Q", self.battle_info),
        )
        for actor, handle in (
            (self.dead_p1, 0x100001),
            (self.p2_actor, 0x100002),
            (self.p3_actor, 0x100003),
            (self.guest_actor, 0x100004),
        ):
            self.process.write(actor, struct.pack("<Q", 0))
            self.process.write(actor + 0x08, struct.pack("<I", handle))

    def set_native_leader(self, member_id, actor, active_party):
        self.live.session_snapshot = mock.Mock(leader_party_member_id=member_id)
        self.live.active_party = active_party
        self.process.write(
            self.battle_info + menu.session_state.BATTLE_LEADER_OFFSET,
            struct.pack("<I", member_id),
        )
        return actor

    def test_p2_p3_follow_ko_promoted_native_leader_not_party_index_zero(self):
        leader = self.set_native_leader(
            5,
            self.p2_actor,
            [
                (0, "Vaan", 0x100001, self.dead_p1),
                (5, "Penelo", 0x100002, self.p2_actor),
                (1, "Balthier", 0x100003, self.p3_actor),
            ],
        )
        self.process.write(self.dead_p1, struct.pack("<Q", 1 << 46))

        self.assertTrue(self.live.p2_world_interaction_allowed())
        self.assertTrue(self.live.p3_world_interaction_allowed())

        self.process.write(leader, struct.pack("<Q", 1 << 46))
        self.assertFalse(self.live.p2_world_interaction_allowed())
        self.assertFalse(self.live.p3_world_interaction_allowed())

    def test_guest_leader_and_stable_drawn_stance_are_supported(self):
        leader = self.set_native_leader(
            8,
            self.guest_actor,
            [
                (8, "Guest", 0x100004, self.guest_actor),
                (5, "Penelo", 0x100002, self.p2_actor),
                (1, "Balthier", 0x100003, self.p3_actor),
            ],
        )
        self.process.write(leader, struct.pack("<Q", 1 << 23))

        self.assertTrue(self.live.p2_world_interaction_allowed())
        self.assertTrue(self.live.p3_world_interaction_allowed())

        self.process.write(leader, struct.pack("<Q", 1 << 24))
        self.assertFalse(self.live.p2_world_interaction_allowed())
        self.assertFalse(self.live.p3_world_interaction_allowed())

    def test_missing_native_leader_fails_closed_without_generation_barrier(self):
        self.set_native_leader(5, self.p2_actor, [])
        self.assertFalse(self.live.p2_world_interaction_allowed())
        self.assertFalse(self.live.p3_world_interaction_allowed())

        self.live.active_party = [
            (5, "Penelo", 0x100002, self.p2_actor),
        ]
        self.process.write(
            self.battle_info + menu.session_state.BATTLE_LEADER_OFFSET,
            struct.pack("<I", 1),
        )
        self.assertTrue(self.live.p2_world_interaction_allowed())
        self.assertTrue(self.live.p3_world_interaction_allowed())

    def test_idle_leader_stays_continuously_available_for_240_ticks(self):
        self.set_native_leader(
            5,
            self.p2_actor,
            [
                (5, "Penelo", 0x100002, self.p2_actor),
                (1, "Balthier", 0x100003, self.p3_actor),
            ],
        )

        for _ in range(240):
            self.assertTrue(self.live.p2_world_interaction_allowed())
            self.assertTrue(self.live.p3_world_interaction_allowed())

    def test_queued_leader_action_fields_do_not_close_the_chest_gate(self):
        leader = self.set_native_leader(
            5,
            self.p2_actor,
            [
                (5, "Penelo", 0x100002, self.p2_actor),
                (1, "Balthier", 0x100003, self.p3_actor),
            ],
        )
        for state, action in ((4, 0x96), (7, 0x00), (10, 0x52)):
            with self.subTest(state=state, action=action):
                self.process.write(leader, struct.pack("<Q", 0))
                self.process.write(
                    leader + menu.P2_ACTION_STATE_OFFSET,
                    struct.pack("<I", state),
                )
                self.process.write(
                    leader + menu.P2_ACTION_CURRENT_OFFSET,
                    struct.pack("<I", action),
                )
                self.assertTrue(self.live.p2_world_interaction_allowed())
                self.assertTrue(self.live.p3_world_interaction_allowed())


class P1NativeInteractionDetectionTests(unittest.TestCase):
    def setUp(self):
        self.process = FakeProcess()
        self.live = menu.CoopMenuRuntime.__new__(menu.CoopMenuRuntime)
        self.live.process = self.process
        self.live.p1_world_interaction_last = None
        self.live.p1_world_interaction_last_time = 0.0
        self.process.write(
            menu.legacy.coop.COMMAND_BLOCK + menu.P2_INTERACTION_OWNER_OFFSET,
            bytes((menu.INTERACTION_OWNER_NONE,)),
        )

    def publish_descriptor(
        self,
        *,
        state=2,
        handle=0x00100015,
        can_use=1,
    ):
        self.process.write(
            menu.NATIVE_INTERACTION_DESCRIPTOR,
            struct.pack(
                "<6I",
                0x3F1999AC,
                (handle >> 16) & 0xF,
                handle & 0xFFFF,
                state,
                handle,
                0x01010100,
            ),
        )
        self.process.write(
            menu.NATIVE_INTERACTION_CAN_USE_GATE,
            bytes((can_use,)),
        )

    def publish_output(self, *, state=1, handle=0x0010000F, text=0x1234):
        self.process.write(
            menu.NATIVE_INTERACTION_OUTPUT,
            struct.pack("<QQII", 0, text, state, handle),
        )

    @mock.patch.object(
        menu.native_interaction, "resolve_handle", return_value=0x74000000
    )
    def test_prefers_exact_talkable_descriptor_over_outer_output(self, _resolve):
        self.publish_descriptor()
        self.publish_output()

        self.assertEqual(
            self.live.native_p1_interaction(),
            (2, 0x00100015, 0, -1),
        )

    @mock.patch.object(
        menu.native_interaction, "resolve_handle", return_value=0x74000000
    )
    def test_keeps_descriptor_across_one_native_frame_dropout(self, _resolve):
        self.publish_descriptor()
        self.publish_output()
        expected = self.live.native_p1_interaction()

        self.publish_descriptor(state=0, handle=0, can_use=0)
        self.assertEqual(self.live.native_p1_interaction(), expected)

    @mock.patch.object(
        menu.native_interaction, "resolve_handle", return_value=0x74000000
    )
    def test_does_not_treat_p2_transaction_descriptor_as_p1(self, _resolve):
        self.publish_descriptor()
        self.publish_output()
        self.process.write(
            menu.legacy.coop.COMMAND_BLOCK + menu.P2_INTERACTION_OWNER_OFFSET,
            bytes((menu.INTERACTION_OWNER_P2,)),
        )

        self.assertEqual(
            self.live.native_p1_interaction(),
            (1, 0x0010000F, 0x1234, -1),
        )


class PrivateNpcInteractionDetectionTests(unittest.TestCase):
    def setUp(self):
        self.process = FakeProcess()
        self.live = menu.CoopMenuRuntime.__new__(menu.CoopMenuRuntime)
        self.live.process = self.process
        self.private = 0x61000000
        self.process.write(
            menu.legacy.coop.COMMAND_BLOCK + menu.P2_INTERACTION_STATE_POINTER_OFFSET,
            struct.pack("<Q", self.private),
        )

    def publish_slot(self, slot, *, state, handle, text):
        # The cave keeps the last populated pass instead of clearing the
        # snapshot every pass, and stamps MARK from TICK when it publishes.
        # A fixture that leaves both at zero reads as "nothing was ever
        # published", so stamp them the way a real publication would.
        self.process.write(
            self.private + menu.legacy.coop.DUAL_INTERACTION_PUBLISH_TICK_OFFSET,
            struct.pack("<I", 1),
        )
        self.process.write(
            self.private + menu.legacy.coop.DUAL_INTERACTION_PUBLISH_MARK_OFFSET,
            struct.pack("<I", 1),
        )
        self.process.write(
            self.private + menu.P2_INTERACTION_STATE_SLOTS_OFFSET + slot * 4,
            struct.pack("<I", state),
        )
        self.process.write(
            self.private + menu.P2_INTERACTION_HANDLE_SLOTS_OFFSET + slot * 4,
            struct.pack("<I", handle),
        )
        self.process.write(
            self.private + menu.P2_INTERACTION_TEXT_SLOTS_OFFSET + slot * 8,
            struct.pack("<Q", text),
        )

    def publish_exact(self, *, state, handle, can_use=1):
        self.process.write(
            self.private + menu.P2_INTERACTION_EXACT_DESCRIPTOR_OFFSET,
            struct.pack(
                "<6I",
                menu.NATIVE_INTERACTION_DESCRIPTOR_DISTANCE_BITS,
                (handle >> 16) & 0xF,
                handle & 0xFFFF,
                state,
                handle,
                menu.NATIVE_INTERACTION_DESCRIPTOR_READY_FLAGS,
            ),
        )
        self.process.write(
            self.private + menu.P2_INTERACTION_EXACT_CAN_USE_OFFSET,
            bytes((can_use,)),
        )

    def test_prefers_exact_talkable_npc_over_higher_outer_slot(self):
        talkable = 0x00100015
        outer = 0x0010000F
        self.publish_slot(3, state=2, handle=talkable, text=0xABCD)
        self.publish_slot(7, state=1, handle=outer, text=0x1234)
        self.publish_exact(state=2, handle=talkable)

        self.assertEqual(
            self.live.private_p2_interaction(),
            (2, talkable, 0xABCD, 3),
        )

    def test_falls_back_to_native_aggregate_when_exact_is_not_ready(self):
        outer = 0x0010000F
        self.publish_slot(7, state=1, handle=outer, text=0x1234)
        self.publish_exact(state=0, handle=0, can_use=0)

        self.assertEqual(
            self.live.private_p2_interaction(),
            (1, outer, 0x1234, 7),
        )

    @mock.patch.object(menu, "FOLLOWER_NPC_INTERACTIONS_ENABLED", False)
    @mock.patch.object(
        menu.native_interaction, "resolve_handle", return_value=0x74000000
    )
    def test_disabled_follower_npc_drops_descriptor_only_prompt(self, _resolve):
        npc = 0x00100019
        self.publish_exact(state=2, handle=npc)

        self.assertIsNone(self.live.private_p2_interaction())

    @mock.patch.object(menu, "FOLLOWER_NPC_INTERACTIONS_ENABLED", True)
    @mock.patch.object(
        menu.native_interaction, "resolve_handle", return_value=0x74000000
    )
    def test_opt_in_returns_descriptor_only_npc_without_any_prompt_slot(self, _resolve):
        npc = 0x00100019
        self.publish_exact(state=2, handle=npc)

        self.assertEqual(
            self.live.private_p2_interaction(),
            (2, npc, 0, -1),
        )


class AtomicFollowerNpcInteractionTests(unittest.TestCase):
    BASE = 0x5A000000
    MAILBOX = BASE + menu.P2_INTERACTION_CONSUMER_MAILBOX_OFFSET
    CAVE = BASE + menu.FOLLOWER_NPC_ATOMIC_CAVE_OFFSET

    @staticmethod
    def absolute_call_targets(code):
        targets = []
        marker = b"\x48\xb8"
        for offset in range(len(code) - 11):
            if code[offset : offset + 2] != marker:
                continue
            if code[offset + 10 : offset + 12] != b"\xff\xd0":
                continue
            targets.append(struct.unpack_from("<Q", code, offset + 2)[0])
        return targets

    def test_atomic_helper_runs_all_five_native_registries_once(self):
        code = menu.build_atomic_follower_npc_interaction_cave(
            self.CAVE,
            self.MAILBOX,
        )
        calls = self.absolute_call_targets(code)
        self.assertEqual(
            calls.count(menu.legacy.coop.INTERACTION_NATIVE_INDICATOR_SCAN),
            5,
        )
        for registry in range(5):
            sequence = (
                b"\xb9"
                + struct.pack("<I", registry)
                + b"\x48\xb8"
                + struct.pack(
                    "<Q", menu.legacy.coop.INTERACTION_NATIVE_INDICATOR_SCAN
                )
                + b"\xff\xd0"
            )
            self.assertEqual(code.count(sequence), 1)
        self.assertEqual(
            calls.count(menu.legacy.coop.INTERACTION_BUILD_EXACT_HANDLE),
            1,
        )
        self.assertEqual(calls.count(menu.NATIVE_ENTITY_RESOLVER), 4)
        self.assertEqual(calls.count(menu.NATIVE_INTERACTION_CONSUMER_TARGET), 1)
        self.assertEqual(
            calls.count(menu.NATIVE_INTERACTION_AUXILIARY_RESOLVER),
            1,
        )

    def test_atomic_helper_resolves_source_handle_at_native_consumption(self):
        code = menu.build_atomic_follower_npc_interaction_cave(
            self.CAVE,
            self.MAILBOX,
        )
        source_resolution = (
            b"\x8b\x4b"
            + bytes((menu.P2_INTERACTION_CONSUMER_SOURCE_HANDLE_OFFSET,))
            + b"\x85\xc9\x0f\x84"
        )
        raw_source_load = b"\x48\x8b\x4b" + bytes(
            (menu.P2_INTERACTION_CONSUMER_SOURCE_OFFSET,)
        )
        resolver_call = (
            b"\x48\xb8"
            + struct.pack("<Q", menu.NATIVE_ENTITY_RESOLVER)
            + b"\xff\xd0\x48\x85\xc0\x0f\x84"
        )
        publish_live_entry = (
            b"\x49\xbb"
            + struct.pack("<Q", menu.NATIVE_INTERACTION_SOURCE)
            + b"\x49\x89\x03"
        )

        positions = tuple(
            code.index(pattern)
            for pattern in (source_resolution, resolver_call, publish_live_entry)
        )
        self.assertEqual(positions, tuple(sorted(positions)))
        self.assertNotIn(raw_source_load, code)

    def test_exact_scan_rejects_source_without_live_physics_before_publication(self):
        code = menu.build_atomic_follower_npc_interaction_cave(
            self.CAVE,
            self.MAILBOX,
        )
        source_resolver = (
            b"\x48\xb8"
            + struct.pack("<Q", menu.NATIVE_ENTITY_RESOLVER)
            + b"\xff\xd0\x48\x85\xc0"
        )
        physics_guard = b"\x48\x83\xb8\xb8\x00\x00\x00\x00\x0f\x84"
        publish_source = (
            b"\x49\xbb"
            + struct.pack("<Q", menu.NATIVE_INTERACTION_SOURCE)
            + b"\x49\x89\x03"
        )

        positions = tuple(
            code.index(pattern)
            for pattern in (source_resolver, physics_guard, publish_source)
        )
        self.assertEqual(positions, tuple(sorted(positions)))

    def test_target_is_resolved_again_after_consumer_before_flag_restore(self):
        code = menu.build_atomic_follower_npc_interaction_cave(
            self.CAVE,
            self.MAILBOX,
        )
        consumer_call = (
            b"\x48\xb8"
            + struct.pack("<Q", menu.NATIVE_INTERACTION_CONSUMER_TARGET)
            + b"\xff\xd0"
        )
        resolver_call = (
            b"\x48\xb8"
            + struct.pack("<Q", menu.NATIVE_ENTITY_RESOLVER)
            + b"\xff\xd0"
        )
        conditional_restore = b"\x8a\x50\x0c\x3a\x54\x24\x59"
        consumer = code.index(consumer_call)
        post_consumer_resolve = code.index(resolver_call, consumer + len(consumer_call))
        restore = code.index(conditional_restore, post_consumer_resolve)

        self.assertLess(consumer, post_consumer_resolve)
        self.assertLess(post_consumer_resolve, restore)

    def test_target_subobjects_are_validated_before_native_consumer(self):
        code = menu.build_atomic_follower_npc_interaction_cave(
            self.CAVE,
            self.MAILBOX,
        )
        hierarchy_guard = b"\x80\x78\x18\x01\x0f\x84"
        hierarchy_pointer = b"\x48\x83\x78\x40\x00\x0f\x84"
        auxiliary_flag = b"\xf7\x40\x1c\x00\x20\x00\x00\x0f\x84"
        auxiliary_call = (
            b"\x48\xb8"
            + struct.pack("<Q", menu.NATIVE_INTERACTION_AUXILIARY_RESOLVER)
            + b"\xff\xd0\x48\x85\xc0\x0f\x84"
        )
        consumer_call = (
            b"\x48\xb8"
            + struct.pack("<Q", menu.NATIVE_INTERACTION_CONSUMER_TARGET)
            + b"\xff\xd0"
        )

        positions = tuple(
            code.index(pattern)
            for pattern in (
                hierarchy_guard,
                hierarchy_pointer,
                auxiliary_flag,
                auxiliary_call,
                consumer_call,
            )
        )
        self.assertEqual(positions, tuple(sorted(positions)))

    def test_atomic_helper_never_loads_mailbox_raw_object_or_source_pointers(self):
        code = menu.build_atomic_follower_npc_interaction_cave(
            self.CAVE,
            self.MAILBOX,
        )
        for offset in (
            menu.P2_INTERACTION_CONSUMER_OBJECT_OFFSET,
            menu.P2_INTERACTION_CONSUMER_SOURCE_OFFSET,
        ):
            self.assertNotIn(b"\x48\x8b\x43" + bytes((offset,)), code)

    def test_atomic_helper_owns_only_interaction_singletons(self):
        code = menu.build_atomic_follower_npc_interaction_cave(
            self.CAVE,
            self.MAILBOX,
        )
        for address in (
            menu.NATIVE_INTERACTION_SOURCE,
            menu.NATIVE_INTERACTION_CAN_USE_GATE,
            menu.NATIVE_INTERACTION_DESCRIPTOR,
        ):
            self.assertGreaterEqual(code.count(struct.pack("<Q", address)), 2)
        self.assertNotIn(
            struct.pack("<Q", menu.legacy.coop.CONTROLLED_HANDLE_GLOBAL),
            code,
        )
        self.assertLessEqual(len(code), menu.FOLLOWER_NPC_ATOMIC_CAVE_LIMIT)

    def test_object_guards_listen_only_to_the_atomic_scan_flag(self):
        menu.legacy.coop.set_dual_interaction_base(self.BASE)
        active = (
            self.MAILBOX + menu.P2_INTERACTION_CONSUMER_EXACT_SCAN_ACTIVE_OFFSET
        )
        marker = b"\x48\xb8" + struct.pack("<Q", active) + b"\x83\x38\x00"
        for builder in (
            menu.legacy.coop._build_follower_npc_entry_guard_cave,
            menu.legacy.coop._build_follower_npc_accept_guard_cave,
            menu.legacy.coop._build_follower_npc_cleanup_guard_cave,
        ):
            self.assertIn(marker, builder(active))
            self.assertNotIn(
                struct.pack("<Q", menu.legacy.coop.DUAL_INTERACTION_STATE),
                builder(active),
            )

    def test_guard_cleanup_is_process_lifetime_and_disarms_only_its_state(self):
        process = FakeProcess()
        menu.legacy.coop.set_dual_interaction_base(self.BASE)
        active = (
            self.MAILBOX + menu.P2_INTERACTION_CONSUMER_EXACT_SCAN_ACTIVE_OFFSET
        )
        menu.legacy.coop.install_follower_npc_private_object_guards(
            process,
            active_state_address=active,
        )
        site = menu.legacy.coop.FOLLOWER_NPC_ENTRY_GUARD_SITE
        installed = process.read(
            site,
            len(menu.legacy.coop.FOLLOWER_NPC_ENTRY_GUARD_ORIGINAL),
        )

        self.assertFalse(
            menu.legacy.coop.restore_follower_npc_private_object_guards(
                process,
                active_state_address=menu.legacy.coop.DUAL_INTERACTION_STATE,
            )
        )
        self.assertEqual(process.read(site, len(installed)), installed)
        process.write(active, b"\x01\x00\x00\x00")
        self.assertTrue(
            menu.legacy.coop.restore_follower_npc_private_object_guards(
                process,
                active_state_address=active,
            )
        )
        self.assertEqual(
            process.read(
                site,
                len(menu.legacy.coop.FOLLOWER_NPC_ENTRY_GUARD_ORIGINAL),
            ),
            installed,
        )
        self.assertEqual(process.read(active, 4), b"\x00" * 4)

    def test_exact_follower_npc_guards_are_adopted_without_any_write(self):
        process = FakeProcess()
        menu.legacy.coop.set_dual_interaction_base(self.BASE)
        active = (
            self.MAILBOX + menu.P2_INTERACTION_CONSUMER_EXACT_SCAN_ACTIVE_OFFSET
        )

        self.assertTrue(
            menu.legacy.coop.install_follower_npc_private_object_guards(
                process,
                active_state_address=active,
            )
        )
        writes_before = len(process.writes)
        patches_before = process.patch_calls

        self.assertFalse(
            menu.legacy.coop.install_follower_npc_private_object_guards(
                process,
                active_state_address=active,
            )
        )
        self.assertEqual(len(process.writes), writes_before)
        self.assertEqual(process.patch_calls, patches_before)

    def test_follower_npc_guard_cave_mismatch_aborts_without_mutation(self):
        process = FakeProcess()
        menu.legacy.coop.set_dual_interaction_base(self.BASE)
        active = (
            self.MAILBOX + menu.P2_INTERACTION_CONSUMER_EXACT_SCAN_ACTIVE_OFFSET
        )
        menu.legacy.coop.install_follower_npc_private_object_guards(
            process,
            active_state_address=active,
        )
        cave = menu.legacy.coop.FOLLOWER_NPC_ACCEPT_GUARD_CAVE
        process.data[cave] ^= 0x01
        memory_before = dict(process.data)
        writes_before = len(process.writes)
        patches_before = process.patch_calls

        with self.assertRaisesRegex(RuntimeError, "guard cave mismatch"):
            menu.legacy.coop.install_follower_npc_private_object_guards(
                process,
                active_state_address=active,
            )

        self.assertEqual(process.data, memory_before)
        self.assertEqual(len(process.writes), writes_before)
        self.assertEqual(process.patch_calls, patches_before)

    def make_router(self):
        process = FakeProcess()
        process.write(
            menu.NATIVE_INTERACTION_CONSUMER_CALL,
            menu.NATIVE_INTERACTION_CONSUMER_CALL_ORIGINAL,
        )
        process.write(
            menu.NATIVE_RENDERING_FLAGS,
            bytes((menu.NATIVE_UI_RENDER_ENABLED_MASK,)),
        )
        owner_actor = 0x75000000
        source_handle = 0x00100031
        source_entry = 0x75100000
        objects = 0x75200000
        registry = menu.native_interaction.REGISTRY_BASE
        process.write(registry + 8, struct.pack("<Q", objects))
        process.write(objects, struct.pack("<I", 0x40))
        process.write(
            objects + 8 + (source_handle & 0xFFFF) * 8,
            struct.pack("<Q", source_entry),
        )
        process.write(source_entry + 0x16, struct.pack("<H", 1))
        process.write(source_entry + 0x30, struct.pack("<Q", owner_actor))
        process.write(source_entry + 0xB8, struct.pack("<Q", 0x75300000))
        target_entry = 0x75400000
        process.write(
            objects + 8 + (process.atomic_exact_handle & 0xFFFF) * 8,
            struct.pack("<Q", target_entry),
        )
        process.write(target_entry + 0x16, struct.pack("<H", 1))
        process.write(target_entry + 0x18, struct.pack("<I", 1))
        process.write(owner_actor, b"\x00" * 8)
        process.write(owner_actor + 0x08, struct.pack("<I", source_handle))
        return (
            process,
            menu.NativeWorldInteractionRouter(process, FakeBridge()),
            owner_actor,
            source_handle,
        )

    def test_atomic_request_consumes_only_when_native_scan_finds_a_candidate(self):
        process, router, owner_actor, source_handle = self.make_router()
        patches = process.patch_calls
        self.assertTrue(
            router.trigger_atomic_follower_npc(
                owner_actor=owner_actor,
                source_handle=source_handle,
                interaction_owner=menu.INTERACTION_OWNER_P2,
            )
        )
        self.assertEqual(process.patch_calls, patches)
        self.assertFalse(router.active)
        self.assertEqual(
            process.u32(
                self.MAILBOX + menu.P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET
            ),
            menu.P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_EXACT,
        )

        process.atomic_exact_result = 0
        self.assertFalse(
            router.trigger_atomic_follower_npc(
                owner_actor=owner_actor,
                source_handle=source_handle,
                interaction_owner=menu.INTERACTION_OWNER_P3,
            )
        )
        self.assertFalse(router.active)

    def test_atomic_request_rejects_rematerializing_source_without_physics(self):
        process, router, owner_actor, source_handle = self.make_router()
        source_entry = menu.native_interaction.resolve_handle(process, source_handle)
        process.write(source_entry + 0xB8, b"\x00" * 8)
        original = (
            process.read(menu.NATIVE_INTERACTION_SOURCE, 8),
            process.read(menu.NATIVE_INTERACTION_CAN_USE_GATE, 1),
            process.read(menu.NATIVE_INTERACTION_DESCRIPTOR, 0x18),
        )

        self.assertFalse(
            router.trigger_atomic_follower_npc(
                owner_actor=owner_actor,
                source_handle=source_handle,
                interaction_owner=menu.INTERACTION_OWNER_P2,
            )
        )
        self.assertEqual(process.consumer_calls, 0)
        self.assertEqual(
            (
                process.read(menu.NATIVE_INTERACTION_SOURCE, 8),
                process.read(menu.NATIVE_INTERACTION_CAN_USE_GATE, 1),
                process.read(menu.NATIVE_INTERACTION_DESCRIPTOR, 0x18),
            ),
            original,
        )

    def test_consumed_npc_holds_follower_stationary_until_dialog_closes(self):
        process, router, owner_actor, source_handle = self.make_router()
        owner_address = (
            menu.legacy.coop.COMMAND_BLOCK + menu.P2_INTERACTION_OWNER_OFFSET
        )

        self.assertTrue(
            router.trigger_atomic_follower_npc(
                owner_actor=owner_actor,
                source_handle=source_handle,
                interaction_owner=menu.INTERACTION_OWNER_P2,
            )
        )
        self.assertEqual(
            process.read(owner_address, 1), bytes((menu.INTERACTION_OWNER_P2,))
        )

        opened_before = router.dialog_hold_open_until
        router.maintain(modal_active=False, now=opened_before - 0.01)
        self.assertEqual(
            process.read(owner_address, 1), bytes((menu.INTERACTION_OWNER_P2,))
        )
        router.maintain(modal_active=True, now=opened_before)
        router.maintain(modal_active=False, now=opened_before + 0.01)
        self.assertEqual(
            process.read(owner_address, 1), bytes((menu.INTERACTION_OWNER_P2,))
        )
        router.maintain(
            modal_active=False,
            now=opened_before + menu.NativeWorldInteractionRouter.DIALOG_RELEASE_SECONDS + 0.02,
        )
        self.assertEqual(
            process.read(owner_address, 1), bytes((menu.INTERACTION_OWNER_NONE,))
        )

    def test_prompt_scan_publishes_private_result_without_consuming_npc(self):
        process, router, owner_actor, source_handle = self.make_router()

        router.maintain_prompt_scans(
            enabled=True,
            candidates={2: (owner_actor, source_handle)},
            now=10.0,
        )
        router.maintain_prompt_scans(
            enabled=True,
            candidates={2: (owner_actor, source_handle)},
            now=10.01,
        )

        self.assertEqual(
            router.prompt_interaction(2, now=10.01),
            (2, process.atomic_exact_handle, 0, -1),
        )
        self.assertEqual(process.consumer_calls, 0)
        self.assertEqual(
            process.u32(
                self.MAILBOX + menu.P2_INTERACTION_CONSUMER_GATE_POLICY_OFFSET
            ),
            menu.P2_INTERACTION_CONSUMER_POLICY_FOLLOWER_EXACT_SCAN,
        )

    def test_prompt_timeout_never_reuses_claimed_mailbox(self):
        process, router, owner_actor, source_handle = self.make_router()
        process.defer_consumer_completion = True
        armed_address = self.MAILBOX + menu.P2_INTERACTION_CONSUMER_ARMED_OFFSET

        router.maintain_prompt_scans(
            enabled=True,
            candidates={2: (owner_actor, source_handle)},
            now=20.0,
        )
        router.maintain_prompt_scans(
            enabled=True,
            candidates={2: (owner_actor, source_handle)},
            now=20.3,
        )
        router.maintain_prompt_scans(
            enabled=True,
            candidates={2: (owner_actor, source_handle)},
            now=20.6,
        )

        self.assertTrue(router.consumer_quarantined)
        self.assertTrue(router.prompt_scan_abandoned)
        self.assertEqual(process.u32(armed_address), 2)
        self.assertEqual(
            sum(
                address == armed_address and payload == struct.pack("<I", 1)
                for address, payload in process.writes
            ),
            1,
        )

        process.write(
            self.MAILBOX + menu.P2_INTERACTION_CONSUMER_DONE_OFFSET,
            struct.pack("<I", 1),
        )
        router.maintain_prompt_scans(
            enabled=False,
            candidates={},
            now=20.7,
        )
        self.assertFalse(router.consumer_quarantined)
        self.assertFalse(router.prompt_scan_abandoned)
        self.assertEqual(router.prompt_scan_player, 0)
        self.assertEqual(process.u32(armed_address), 0)
        self.assertIsNone(router.prompt_interaction(2, now=20.7))

    def test_late_atomic_completion_preserves_dialog_hold_after_timeout(self):
        process, router, owner_actor, source_handle = self.make_router()
        process.defer_consumer_completion = True
        owner_address = (
            menu.legacy.coop.COMMAND_BLOCK + menu.P2_INTERACTION_OWNER_OFFSET
        )

        with mock.patch.object(router, "CONSUMER_TIMEOUT_SECONDS", 0.001):
            self.assertTrue(
                router.trigger_atomic_follower_npc(
                    owner_actor=owner_actor,
                    source_handle=source_handle,
                    interaction_owner=menu.INTERACTION_OWNER_P2,
                )
            )
        self.assertTrue(router.consumer_quarantined)
        self.assertEqual(
            process.read(owner_address, 1), bytes((menu.INTERACTION_OWNER_P2,))
        )

        process.write(
            self.MAILBOX + menu.P2_INTERACTION_CONSUMER_EXACT_RESULT_OFFSET,
            struct.pack("<I", 1),
        )
        process.write(
            self.MAILBOX + menu.P2_INTERACTION_CONSUMER_DONE_OFFSET,
            struct.pack("<I", 1),
        )
        router.maintain(now=30.0)

        self.assertFalse(router.consumer_quarantined)
        self.assertEqual(router.dialog_hold_owner, menu.INTERACTION_OWNER_P2)
        self.assertEqual(
            process.read(owner_address, 1), bytes((menu.INTERACTION_OWNER_P2,))
        )


class InteractionTransitionGateTests(unittest.TestCase):
    def snapshot(self, **changes):
        values = {
            "gameplay_ready": True,
            "loading": False,
            "current_location": 0x123,
            "ui_location": 0x123,
        }
        values.update(changes)
        return mock.Mock(**values)

    def test_hides_during_loading_and_for_a_post_load_grace(self):
        gate = menu.InteractionTransitionGate(0.25)
        self.assertFalse(gate.allows(self.snapshot(loading=True), 1.0))
        self.assertFalse(gate.allows(self.snapshot(), 2.0))
        self.assertFalse(gate.allows(self.snapshot(), 2.24))
        self.assertTrue(gate.allows(self.snapshot(), 2.25))

    def test_area_change_restarts_the_grace(self):
        gate = menu.InteractionTransitionGate(0.25)
        gate.allows(self.snapshot(), 1.0)
        self.assertTrue(gate.allows(self.snapshot(), 1.25))
        changed = self.snapshot(current_location=0x124, ui_location=0x124)
        self.assertFalse(gate.allows(changed, 2.0))
        self.assertTrue(gate.allows(changed, 2.25))


class NativeNpcLabelResolutionTests(unittest.TestCase):
    def setUp(self):
        self.process = FakeProcess()
        self.npc = 0x74000000
        self.table = 0x76000000
        self.name_index = 0xE1
        self.process.write(
            self.npc + menu.NATIVE_NPC_NAME_KEY_OFFSET,
            struct.pack("<H", self.name_index),
        )
        self.process.write(
            menu.NATIVE_NPC_TEXT_TABLE_POINTER,
            struct.pack("<Q", self.table),
        )
        self.process.write(
            self.table + menu.NATIVE_NPC_TEXT_TABLE_COUNT_OFFSET,
            struct.pack("<I", 0x1000),
        )

    @mock.patch.object(
        menu.native_interaction, "resolve_handle", return_value=0x74000000
    )
    def test_uses_active_native_name_variant(self, _resolve):
        state_address = (
            menu.NATIVE_NPC_NAME_STATE_BASE
            + menu.NATIVE_NPC_NAME_STATE_BITS_OFFSET
            + (self.name_index >> 3)
        )
        self.process.write(
            state_address,
            bytes((1 << (self.name_index & 7),)),
        )
        jinn_text = 0x77000000
        slot = self.name_index * 2 + 1
        self.process.write(
            self.table + menu.NATIVE_NPC_TEXT_TABLE_ENTRIES_OFFSET + slot * 4,
            struct.pack("<I", jinn_text),
        )
        self.process.write(jinn_text, bytes((0x29, 0x42, 0x47, 0x47, 0)))

        self.assertEqual(
            menu.native_interaction_object_label(self.process, 0x00100019),
            "Jinn",
        )

    @mock.patch.object(
        menu.native_interaction, "resolve_handle", return_value=0x74000000
    )
    def test_negative_name_key_uses_direct_object_text(self, _resolve):
        self.process.write(
            self.npc + menu.NATIVE_NPC_NAME_KEY_OFFSET,
            struct.pack("<H", 0xFFFF),
        )
        direct_text = 0x77000100
        self.process.write(
            self.npc + menu.NATIVE_NPC_DIRECT_TEXT_OFFSET,
            struct.pack("<Q", direct_text),
        )
        self.process.write(
            direct_text,
            bytes((0x23, 0x42, 0x4B, 0x3E, 0x3C, 0x4D, 0)),
        )

        self.assertEqual(
            menu.native_interaction_object_label(self.process, 0x00100019),
            "Direct",
        )

    @mock.patch.object(
        menu.native_interaction, "resolve_handle", return_value=0x74000000
    )
    def test_uses_visual_object_position_for_npc_prompt(self, _resolve):
        expected = (114.9, 21.63, 587.3)
        self.process.write(
            self.npc + menu.NATIVE_NPC_WORLD_POSITION_OFFSET,
            struct.pack("<3f", *expected),
        )

        actual = menu.native_interaction_object_position(
            self.process,
            0x00100019,
        )

        self.assertIsNotNone(actual)
        for value, wanted in zip(actual, expected):
            self.assertAlmostEqual(value, wanted, places=4)

    @mock.patch.object(
        menu.native_interaction, "resolve_handle", return_value=0x74000000
    )
    def test_rejects_invalid_npc_prompt_position(self, _resolve):
        self.process.write(
            self.npc + menu.NATIVE_NPC_WORLD_POSITION_OFFSET,
            struct.pack("<3f", float("nan"), float("nan"), float("nan")),
        )

        self.assertIsNone(
            menu.native_interaction_object_position(self.process, 0x00100019)
        )


if __name__ == "__main__":
    unittest.main()
