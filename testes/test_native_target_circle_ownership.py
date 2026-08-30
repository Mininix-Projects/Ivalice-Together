from __future__ import annotations

import inspect
import struct
import unittest
from unittest import mock

import native_target_circle_controller as target_circle
import native_target_circle_router as target_router


MANAGER = 0x300000
ALLOCATOR = 0x380000
POST_MANAGER = 0x320000
POST_ALLOCATOR = 0x3A0000
NODE = 0x400000
TARGET = 0x100012


class MemoryProcess:
    def __init__(self) -> None:
        self.memory: dict[int, int] = {}
        self.writes: list[tuple[int, bytes]] = []

    def seed(self, address: int, payload: bytes) -> None:
        for offset, value in enumerate(payload):
            self.memory[address + offset] = value

    def read(self, address: int, size: int) -> bytes:
        return bytes(self.memory.get(address + offset, 0) for offset in range(size))

    def write(self, address: int, payload: bytes | bytearray) -> None:
        payload = bytes(payload)
        self.writes.append((address, payload))
        self.seed(address, payload)

    def u32(self, address: int) -> int:
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address: int) -> int:
        return struct.unpack("<Q", self.read(address, 8))[0]

    def seed_u16(self, address: int, value: int) -> None:
        self.seed(address, struct.pack("<H", value))

    def seed_u32(self, address: int, value: int) -> None:
        self.seed(address, struct.pack("<I", value))

    def seed_u64(self, address: int, value: int) -> None:
        self.seed(address, struct.pack("<Q", value))


class FakeRouter:
    patched = False

    def __init__(self, publication: target_router.TargetCircleOwnership | None):
        self.publication = publication
        self.pending: target_router.TargetCircleCreateJournal | None = None
        self.releases: list[target_router.TargetCircleOwnership] = []
        self.retirements: list[target_router.TargetCircleOwnership] = []
        self.consumed: list[target_router.TargetCircleCreateJournal] = []
        self.discarded: list[target_router.TargetCircleCreateJournal] = []
        self.process: MemoryProcess | None = None

    @staticmethod
    def maintain() -> bool:
        return True

    @staticmethod
    def resident_exact() -> bool:
        return True

    def ownership(self, owner_tag: int):
        if self.publication is None or self.publication.owner_tag != owner_tag:
            return None
        return self.publication

    def pending_creation(self):
        return self.pending

    def consume_creation(self, journal: target_router.TargetCircleCreateJournal) -> bool:
        if self.pending != journal:
            return False
        if journal.status == 4 and journal.factory_entered:
            return False
        if journal.status == 3 and (
            self.publication is None
            or not (
                self.publication.owner_tag == journal.owner_tag
                and self.publication.manager == journal.manager
                and self.publication.allocator == journal.allocator
                and self.publication.node == journal.node
            )
        ):
            return False
        self.consumed.append(journal)
        self.pending = None
        return True

    def claim_ownership(
        self,
        owner_tag: int,
        allocator: int,
        node: int,
        target: int,
        palette: int,
    ) -> target_router.TargetCircleOwnership:
        if self.publication is not None:
            raise RuntimeError("already published")
        assert self.process is not None
        manager = self.process.u64(target_circle.TARGET_UI_MANAGER_GLOBAL)
        self.publication = publication(
            owner_tag=owner_tag,
            manager=manager,
            allocator=allocator,
            node=node,
            target=target,
            palette=palette,
        )
        return self.publication

    def update_ownership(
        self,
        owned: target_router.TargetCircleOwnership,
        *,
        target: int,
        palette: int,
    ) -> target_router.TargetCircleOwnership:
        if self.publication != owned:
            raise RuntimeError("stale publication")
        assert self.process is not None
        self.process.seed_u32(owned.node + 0xD2C, target)
        self.process.seed_u32(owned.node + 0xD7C, palette)
        self.publication = target_router.TargetCircleOwnership(
            owner_tag=owned.owner_tag,
            token=owned.token,
            manager=owned.manager,
            allocator=owned.allocator,
            node=owned.node,
            target=target,
            palette=palette,
        )
        return self.publication

    def reconcile_ownership(
        self,
        owned: target_router.TargetCircleOwnership,
    ) -> target_router.TargetCircleOwnership:
        if self.publication != owned or self.process is None:
            raise RuntimeError("stale publication")
        self.publication = target_router.TargetCircleOwnership(
            owner_tag=owned.owner_tag,
            token=owned.token,
            manager=owned.manager,
            allocator=owned.allocator,
            node=owned.node,
            target=self.process.u32(owned.node + 0xD2C),
            palette=self.process.u32(owned.node + 0xD7C),
        )
        return self.publication

    def discard_creation_from_obsolete_allocator(
        self,
        journal: target_router.TargetCircleCreateJournal,
        *,
        manager: int,
        allocator: int,
    ) -> bool:
        if (
            self.pending != journal
            or not manager
            or not allocator
            or not journal.allocator
            or allocator == journal.allocator
        ):
            return False
        if self.publication is not None:
            if not (
                self.publication.owner_tag == journal.owner_tag
                and self.publication.manager == journal.manager
                and self.publication.allocator == journal.allocator
                and self.publication.node == journal.node
            ):
                return False
            self.publication = None
        self.discarded.append(journal)
        self.pending = None
        return True

    @staticmethod
    def discard_failed_creation_from_quiescent_node(
        journal: target_router.TargetCircleCreateJournal,
    ) -> bool:
        del journal
        return False

    def release_ownership(self, ownership: target_router.TargetCircleOwnership) -> bool:
        self.releases.append(ownership)
        if self.publication != ownership:
            return False
        self.publication = None
        return True

    @staticmethod
    def release_quiescent_ownership(
        ownership: target_router.TargetCircleOwnership,
    ) -> bool:
        del ownership
        return False

    def release_frontend_reset_ownership(
        self,
        ownership: target_router.TargetCircleOwnership,
    ) -> bool:
        if self.publication != ownership or self.process is None:
            return False
        if not (
            self.process.u64(target_circle.TARGET_UI_MANAGER_GLOBAL)
            == ownership.manager
            and self.process.u64(
                ownership.manager + target_circle.TARGET_CIRCLE_ALLOCATOR_OFFSET
            )
            == ownership.allocator
            and self.process.u64(ownership.manager + 0x10F00) == 0
            and self.process.u64(ownership.manager + 0x10EF0) == 0
            and struct.unpack(
                "<H", self.process.read(ownership.node + 0xD54, 2)
            )[0]
            == 0
        ):
            return False
        self.publication = None
        return True

    def retire_ownership(self, ownership: target_router.TargetCircleOwnership) -> bool:
        if self.publication != ownership or self.process is None:
            return False
        self.retirements.append(ownership)
        self.process.write(ownership.node + 0xD54, struct.pack("<H", 5))
        self.publication = None
        return True


def publication(
    *,
    owner_tag: int = target_router.P1_TARGET_OWNER_TAG,
    token: int = 1,
    manager: int = MANAGER,
    allocator: int = ALLOCATOR,
    node: int = NODE,
    target: int = TARGET,
    palette: int = 4,
) -> target_router.TargetCircleOwnership:
    return target_router.TargetCircleOwnership(
        owner_tag=owner_tag,
        token=token,
        manager=manager,
        allocator=allocator,
        node=node,
        target=target,
        palette=palette,
    )


def create_journal(
    *,
    status: int = 3,
    owner_tag: int = target_router.P1_TARGET_OWNER_TAG,
    factory_entered: bool = True,
    manager: int = MANAGER,
    allocator: int = ALLOCATOR,
    node: int = NODE,
    target: int = TARGET,
    palette: int = 4,
) -> target_router.TargetCircleCreateJournal:
    return target_router.TargetCircleCreateJournal(
        status=status,
        sequence=9,
        owner_tag=owner_tag,
        factory_entered=factory_entered,
        manager=manager if factory_entered else 0,
        allocator=allocator if factory_entered else 0,
        node=node if factory_entered else 0,
        target=target,
        observed=target if status == 3 else 0,
        palette=palette if status == 3 else 0,
    )


def live_process(*, target: int = TARGET, palette: int = 4) -> MemoryProcess:
    process = MemoryProcess()
    process.seed_u64(target_circle.TARGET_UI_MANAGER_GLOBAL, MANAGER)
    process.seed_u64(MANAGER + target_circle.TARGET_CIRCLE_ALLOCATOR_OFFSET, ALLOCATOR)
    process.seed_u64(MANAGER + 0x10F00, NODE)
    process.seed_u64(MANAGER + 0x10EF0, NODE)
    process.seed_u64(NODE + 0xD70, 0)
    process.seed_u16(NODE + 0xD54, 2)
    process.seed_u32(NODE + 0xD2C, target)
    process.seed_u32(NODE + 0xD7C, palette)
    return process


def controller(
    process: MemoryProcess,
    router: FakeRouter,
    *,
    owner_tag: int = target_router.P1_TARGET_OWNER_TAG,
) -> target_circle.NativeTargetCircleController:
    instance = target_circle.NativeTargetCircleController.__new__(
        target_circle.NativeTargetCircleController
    )
    instance.process = process
    instance.router = router
    router.process = process
    instance.owner_tag = owner_tag
    instance.allowed_palettes = frozenset(range(7))
    instance.palette = 4
    instance.node = 0
    instance.target = 0
    instance.published_palette = None
    instance.ownership = None
    instance.failure_count = 0
    instance.next_retry = 0.0
    instance.next_maintenance = 0.0
    return instance


def registry_router(
    process: MemoryProcess,
    owned: target_router.TargetCircleOwnership | None = None,
) -> target_router.NativeTargetCircleRetargeter:
    router = target_router.NativeTargetCircleRetargeter.__new__(
        target_router.NativeTargetCircleRetargeter
    )
    router.process = process
    router.state = 0x500000
    router._sequence = 0
    router.patched = False
    router.quarantined = False
    router.owns_static = False
    router.maintain = lambda: True
    router.resident_exact = lambda: True
    image = bytearray(router._initial_state_image())
    if owned is not None:
        slot = target_router.TARGET_OWNER_TAGS.index(owned.owner_tag)
        offset = (
            target_router.TARGET_OWNERSHIP_RECORDS_OFFSET
            + slot * target_router.TARGET_OWNERSHIP_RECORD_SIZE
        )
        image[offset : offset + target_router.TARGET_OWNERSHIP_RECORD_SIZE] = (
            target_router._pack_ownership_record(owned)
        )
        next_token = (owned.token + 1) & 0xFFFFFFFF or 1
        struct.pack_into(
            "<I",
            image,
            target_router.TARGET_OWNERSHIP_NEXT_TOKEN_OFFSET,
            next_token,
        )
    process.seed(router.state, image)
    return router


def seed_create_journal(
    process: MemoryProcess,
    router: target_router.NativeTargetCircleRetargeter,
    journal: target_router.TargetCircleCreateJournal,
) -> None:
    process.seed_u32(router.state, journal.status)
    process.seed_u32(
        router.state + target_router.TARGET_OPERATION_OFFSET,
        target_router.TARGET_OPERATION_CREATE,
    )
    process.seed_u64(
        router.state + target_router.TARGET_NODE_OFFSET,
        journal.node,
    )
    process.seed_u32(
        router.state + target_router.TARGET_HANDLE_OFFSET,
        journal.target,
    )
    process.seed_u32(
        router.state + target_router.TARGET_VARIANT_OFFSET,
        1,
    )
    process.seed_u32(
        router.state + target_router.TARGET_OBSERVED_OFFSET,
        journal.observed,
    )
    process.seed_u32(
        router.state + target_router.TARGET_OBSERVED_PALETTE_OFFSET,
        journal.palette,
    )
    process.seed_u32(
        router.state + target_router.TARGET_DESIRED_PALETTE_OFFSET,
        journal.palette if journal.status == 3 else 4,
    )
    process.seed_u32(
        router.state + target_router.TARGET_SEQUENCE_OFFSET,
        journal.sequence,
    )
    process.seed_u32(
        router.state + target_router.TARGET_OWNER_OFFSET,
        journal.owner_tag,
    )
    process.seed_u32(
        router.state + target_router.TARGET_FACTORY_ENTERED_OFFSET,
        int(journal.factory_entered),
    )
    process.seed_u64(
        router.state + target_router.TARGET_RESULT_MANAGER_OFFSET,
        journal.manager,
    )
    process.seed_u64(
        router.state + target_router.TARGET_RESULT_ALLOCATOR_OFFSET,
        journal.allocator,
    )
    process.seed_u64(
        router.state + target_router.TARGET_RESULT_RESERVED_OFFSET,
        0,
    )
    process.seed_u32(
        router.state + target_router.TARGET_ACK_SEQUENCE_OFFSET,
        journal.sequence,
    )


def seed_inflight_create(
    process: MemoryProcess,
    router: target_router.NativeTargetCircleRetargeter,
    *,
    status: int,
    factory_entered: bool,
) -> None:
    process.seed_u32(router.state, status)
    process.seed_u32(
        router.state + target_router.TARGET_OPERATION_OFFSET,
        target_router.TARGET_OPERATION_CREATE,
    )
    process.seed_u32(
        router.state + target_router.TARGET_HANDLE_OFFSET,
        TARGET,
    )
    process.seed_u32(
        router.state + target_router.TARGET_VARIANT_OFFSET,
        1,
    )
    process.seed_u32(
        router.state + target_router.TARGET_SEQUENCE_OFFSET,
        9,
    )
    process.seed_u32(
        router.state + target_router.TARGET_OWNER_OFFSET,
        target_router.P1_TARGET_OWNER_TAG,
    )
    process.seed_u32(
        router.state + target_router.TARGET_DESIRED_PALETTE_OFFSET,
        4,
    )
    process.seed_u32(
        router.state + target_router.TARGET_FACTORY_ENTERED_OFFSET,
        int(factory_entered),
    )
    if status == 2 and factory_entered:
        # While the callback owns state 2 these fields are work journal/scratch;
        # they become the effective heap identity only before terminal state.
        process.seed_u64(
            router.state + target_router.TARGET_RESULT_MANAGER_OFFSET,
            MANAGER,
        )
        process.seed_u64(
            router.state + target_router.TARGET_RESULT_ALLOCATOR_OFFSET,
            NODE,
        )


class NativeTargetCircleOwnershipTests(unittest.TestCase):
    def test_resident_v7_mailbox_and_v4_ownership_layout_are_explicit(self) -> None:
        self.assertEqual(target_router.REGION_MAGIC, b"FF12NCR7")
        self.assertEqual(target_router.TARGET_STATE_MAGIC, b"TCRMBX6!")
        self.assertEqual(target_router.TARGET_OWNERSHIP_MAGIC, b"TCOWNR6!")
        self.assertEqual(target_router.TARGET_OWNERSHIP_VERSION, 4)
        self.assertEqual(target_router.STATE_OFFSET, 0xE70)
        self.assertEqual(target_router.STATE_SIZE, 0x190)
        self.assertEqual(target_router.TARGET_OWNERSHIP_RECORD_SIZE, 0x30)
        self.assertEqual(
            struct.calcsize("<IIQQQIIII"),
            target_router.TARGET_OWNERSHIP_RECORD_SIZE,
        )
        self.assertEqual(
            target_router.TARGET_RESULT_RESERVED_OFFSET + 8,
            target_router.TARGET_ACK_SEQUENCE_OFFSET,
        )
        self.assertLessEqual(
            target_router.TARGET_OWNERSHIP_RECORDS_END,
            target_router.STATE_SIZE,
        )
        self.assertEqual(
            target_router.STATE_OFFSET + target_router.STATE_SIZE,
            target_router.REGION_SIZE,
        )

        image = bytearray(
            target_router.NativeTargetCircleRetargeter._initial_state_image()
        )
        self.assertTrue(
            target_router.NativeTargetCircleRetargeter._valid_state(image)
        )
        image[
            target_router.TARGET_OWNERSHIP_MAGIC_OFFSET :
            target_router.TARGET_OWNERSHIP_MAGIC_OFFSET + 8
        ] = b"TCOWNR4!"
        self.assertFalse(
            target_router.NativeTargetCircleRetargeter._valid_state(image)
        )

        router = target_router.NativeTargetCircleRetargeter.__new__(
            target_router.NativeTargetCircleRetargeter
        )
        router._dispatch_code = target_router.build_target_dispatcher(
            0x60000300,
            0x60000E70,
        )
        router.service = 0x60000200
        router.state = 0x60000E70
        self.assertLessEqual(
            target_router.CODE_OFFSET + len(router._dispatch_code),
            target_router.STATE_OFFSET,
        )
        static = router._static_image()
        magic, version, service, code, state = struct.unpack_from(
            "<8sIIII",
            static,
        )
        self.assertEqual(
            (magic, version, service, code, state),
            (
                b"FF12NCR7",
                7,
                target_router.SERVICE_OFFSET,
                target_router.CODE_OFFSET,
                target_router.STATE_OFFSET,
            ),
        )

    def test_restart_during_prefactory_or_factory_owned_state_never_rearms(
        self,
    ) -> None:
        phases = (
            (1, False),
            (2, False),
            (2, True),
        )
        for status, entered in phases:
            with self.subTest(status=status, factory_entered=entered):
                process = live_process()
                router = registry_router(process)
                router.maintain = (
                    target_router.NativeTargetCircleRetargeter.maintain.__get__(
                        router,
                        target_router.NativeTargetCircleRetargeter,
                    )
                )
                seed_inflight_create(
                    process,
                    router,
                    status=status,
                    factory_entered=entered,
                )
                instance = controller(process, router)
                attempts: list[int] = []
                instance._create = lambda target, palette: attempts.append(target)

                self.assertFalse(instance.sync(TARGET))
                self.assertFalse(instance.restore())

                self.assertEqual(attempts, [])
                self.assertEqual(process.u32(router.state), status)
                self.assertTrue(router.patched)
                self.assertTrue(router.quarantined)

    def test_partial_postfactory_terminal_is_permanently_fail_closed(self) -> None:
        process = live_process()
        router = registry_router(process)
        router.maintain = target_router.NativeTargetCircleRetargeter.maintain.__get__(
            router,
            target_router.NativeTargetCircleRetargeter,
        )
        journal = create_journal(status=4, factory_entered=True)
        seed_create_journal(process, router, journal)
        # Simulate a torn/partial terminal publication: entered is durable but
        # the callback's effective allocator field is not.
        process.seed_u64(
            router.state + target_router.TARGET_RESULT_ALLOCATOR_OFFSET,
            0,
        )
        instance = controller(process, router)
        attempts: list[int] = []
        instance._create = lambda target, palette: attempts.append(target)

        self.assertFalse(instance.sync(TARGET))
        self.assertFalse(instance.restore())

        self.assertEqual(attempts, [])
        self.assertEqual(process.u32(router.state), 4)
        self.assertTrue(router.quarantined)

    def test_constructor_requires_explicit_owner_and_has_no_palette_recovery_api(
        self,
    ) -> None:
        parameters = inspect.signature(
            target_circle.NativeTargetCircleController
        ).parameters
        self.assertIn("owner_tag", parameters)
        self.assertEqual(parameters["owner_tag"].default, inspect.Parameter.empty)
        self.assertNotIn("alternate_palettes", parameters)

    def test_snapshot_retries_instead_of_mixing_manager_generations(self) -> None:
        process = MemoryProcess()
        process.seed_u64(
            target_circle.TARGET_UI_MANAGER_GLOBAL,
            POST_MANAGER,
        )
        process.seed_u64(
            MANAGER + target_circle.TARGET_CIRCLE_ALLOCATOR_OFFSET,
            ALLOCATOR,
        )
        process.seed_u64(MANAGER + 0x10F00, 0)
        process.seed_u64(MANAGER + 0x10EF0, 0)
        process.seed_u64(
            POST_MANAGER + target_circle.TARGET_CIRCLE_ALLOCATOR_OFFSET,
            POST_ALLOCATOR,
        )
        process.seed_u64(POST_MANAGER + 0x10F00, NODE)
        process.seed_u64(POST_MANAGER + 0x10EF0, NODE)
        process.seed_u64(NODE + 0xD70, 0)
        original_u64 = process.u64
        global_reads = 0

        def manager_changes_after_first_sample(address: int) -> int:
            nonlocal global_reads
            if address == target_circle.TARGET_UI_MANAGER_GLOBAL:
                global_reads += 1
                if global_reads == 1:
                    return MANAGER
            return original_u64(address)

        process.u64 = manager_changes_after_first_sample
        instance = controller(process, FakeRouter(None))

        snapshot = instance._snapshot()

        self.assertEqual(snapshot.manager, POST_MANAGER)
        self.assertEqual(snapshot.allocator, POST_ALLOCATOR)
        self.assertEqual(snapshot.nodes, (NODE,))
        self.assertGreaterEqual(global_reads, 3)

    def test_replacement_controller_adopts_and_consumes_resident_success(
        self,
    ) -> None:
        process = live_process(target=TARGET + 1, palette=2)
        router = registry_router(process)
        journal = create_journal(target=TARGET + 1, palette=2)
        seed_create_journal(process, router, journal)
        instance = controller(process, router)

        self.assertTrue(instance.sync(TARGET + 1, palette=2))

        self.assertEqual(instance.node, NODE)
        self.assertEqual(instance.target, TARGET + 1)
        self.assertIsNone(router.pending_creation())
        owned = router.ownership(target_router.P1_TARGET_OWNER_TAG)
        self.assertIsNotNone(owned)
        self.assertEqual(owned.manager, MANAGER)
        self.assertEqual(owned.allocator, ALLOCATOR)
        self.assertEqual(owned.node, NODE)
        self.assertEqual(owned.target, TARGET + 1)
        self.assertEqual(owned.palette, 2)

    def test_restart_after_claim_recovers_exact_token_then_consumes_journal(
        self,
    ) -> None:
        process = live_process(target=TARGET + 1, palette=2)
        owned = publication(token=7, target=TARGET + 1, palette=2)
        router = registry_router(process, owned)
        journal = create_journal()
        seed_create_journal(process, router, journal)
        instance = controller(process, router)

        self.assertTrue(instance.sync(TARGET + 1, palette=2))

        self.assertEqual(instance.ownership, owned)
        self.assertEqual(router.ownership(owned.owner_tag), owned)
        self.assertIsNone(router.pending_creation())

    def test_terminal_success_waits_for_state_one_to_become_live_without_recreate(
        self,
    ) -> None:
        process = live_process()
        process.seed_u16(NODE + 0xD54, 1)
        router = FakeRouter(None)
        journal = create_journal()
        router.pending = journal
        instance = controller(process, router)
        attempts: list[int] = []
        instance._create = lambda target, palette: attempts.append(target)

        self.assertFalse(instance.sync(TARGET))
        self.assertEqual(attempts, [])
        self.assertEqual(router.pending, journal)
        self.assertIsNone(router.publication)

        process.seed_u16(NODE + 0xD54, 2)
        instance.next_retry = 0.0
        self.assertTrue(instance.sync(TARGET))
        self.assertEqual(attempts, [])
        self.assertIsNone(router.pending)
        self.assertIsNotNone(router.publication)

    def test_ambiguous_factory_journal_blocks_every_new_create(self) -> None:
        process = live_process()
        router = FakeRouter(None)
        router.pending = create_journal(status=4, factory_entered=True)
        instance = controller(process, router)
        attempts: list[int] = []

        def unexpected_create(target: int, palette: int):
            del palette
            attempts.append(target)
            raise AssertionError("a second CREATE must never be issued")

        instance._create = unexpected_create

        self.assertFalse(instance.sync(TARGET))
        instance.next_retry = 0.0
        self.assertFalse(instance.sync(TARGET))

        self.assertEqual(attempts, [])
        self.assertEqual(
            router.pending,
            create_journal(status=4, factory_entered=True),
        )
        self.assertEqual(router.consumed, [])

    def test_rejected_create_releases_exact_inactive_unlinked_owner(self) -> None:
        process = live_process()
        old_owner = publication(owner_tag=target_router.P1_TARGET_OWNER_TAG)
        router = registry_router(process, old_owner)
        journal = create_journal(
            status=4,
            owner_tag=target_router.P2_TARGET_OWNER_TAG,
            factory_entered=True,
        )
        seed_create_journal(process, router, journal)
        process.seed_u64(MANAGER + 0x10F00, 0)
        process.seed_u64(MANAGER + 0x10EF0, 0)
        process.seed_u64(NODE + 0xD68, 0)
        process.seed_u64(NODE + 0xD70, 0)
        process.seed_u16(NODE + 0xD54, 0)
        process.writes.clear()

        instance = controller(
            process,
            router,
            owner_tag=target_router.P2_TARGET_OWNER_TAG,
        )
        self.assertFalse(instance._reconcile_pending_creation(journal))

        self.assertIsNone(router.pending_creation())
        self.assertIsNone(router.ownership(target_router.P1_TARGET_OWNER_TAG))
        self.assertEqual(process.read(NODE + 0xD54, 2), b"\x00\x00")
        self.assertFalse(
            any(NODE <= address < NODE + 0x1000 for address, _ in process.writes)
        )

    def test_rejected_create_keeps_every_nonquiescent_identity_fail_closed(self) -> None:
        mutations = {
            "linked": lambda process: process.seed_u64(MANAGER + 0x10F00, NODE),
            "current": lambda process: process.seed_u64(MANAGER + 0x10EF0, NODE),
            "active": lambda process: process.seed_u16(NODE + 0xD54, 2),
            "previous": lambda process: process.seed_u64(NODE + 0xD68, NODE + 0x1000),
            "next": lambda process: process.seed_u64(NODE + 0xD70, NODE + 0x1000),
            "target": lambda process: process.seed_u32(NODE + 0xD2C, TARGET + 1),
            "palette": lambda process: process.seed_u32(NODE + 0xD7C, 3),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                process = live_process()
                old_owner = publication(owner_tag=target_router.P1_TARGET_OWNER_TAG)
                router = registry_router(process, old_owner)
                journal = create_journal(
                    status=4,
                    owner_tag=target_router.P2_TARGET_OWNER_TAG,
                    factory_entered=True,
                )
                seed_create_journal(process, router, journal)
                process.seed_u64(MANAGER + 0x10F00, 0)
                process.seed_u64(MANAGER + 0x10EF0, 0)
                process.seed_u64(NODE + 0xD68, 0)
                process.seed_u64(NODE + 0xD70, 0)
                process.seed_u16(NODE + 0xD54, 0)
                mutate(process)
                before = process.read(router.state, target_router.STATE_SIZE)
                process.writes.clear()

                self.assertFalse(
                    router.discard_failed_creation_from_quiescent_node(journal)
                )

                self.assertEqual(
                    process.read(router.state, target_router.STATE_SIZE), before
                )
                self.assertEqual(router.pending_creation(), journal)
                self.assertEqual(
                    router.ownership(target_router.P1_TARGET_OWNER_TAG), old_owner
                )
                self.assertEqual(process.writes, [])

    def test_recover_releases_exact_inactive_unlinked_ownership(self) -> None:
        process = live_process()
        owned = publication(owner_tag=target_router.P1_TARGET_OWNER_TAG)
        router = registry_router(process, owned)
        process.seed_u64(MANAGER + 0x10F00, 0)
        process.seed_u64(MANAGER + 0x10EF0, 0)
        process.seed_u64(NODE + 0xD68, 0)
        process.seed_u64(NODE + 0xD70, 0)
        process.seed_u16(NODE + 0xD54, 0)
        instance = controller(process, router)
        process.writes.clear()

        instance._recover(instance._snapshot())

        self.assertIsNone(router.ownership(target_router.P1_TARGET_OWNER_TAG))
        self.assertIsNone(instance.ownership)
        self.assertEqual(instance.node, 0)
        self.assertEqual(process.read(NODE + 0xD54, 2), b"\x00\x00")
        self.assertFalse(
            any(NODE <= address < NODE + 0x1000 for address, _ in process.writes)
        )

    def test_recover_keeps_unsettled_unlinked_ownership_fail_closed(self) -> None:
        process = live_process()
        owned = publication(owner_tag=target_router.P1_TARGET_OWNER_TAG)
        router = registry_router(process, owned)
        process.seed_u64(MANAGER + 0x10F00, 0)
        process.seed_u64(MANAGER + 0x10EF0, 0)
        process.seed_u64(NODE + 0xD68, 0)
        process.seed_u64(NODE + 0xD70, 0)
        process.seed_u16(NODE + 0xD54, 1)
        instance = controller(process, router)
        process.writes.clear()

        with self.assertRaisesRegex(RuntimeError, "identity is ambiguous"):
            instance._recover(instance._snapshot())

        self.assertEqual(router.ownership(target_router.P1_TARGET_OWNER_TAG), owned)
        self.assertEqual(process.writes, [])

    def test_frontend_reset_releases_only_sidecar_after_pool_block_was_reset(
        self,
    ) -> None:
        process = live_process()
        owned = publication(owner_tag=target_router.P1_TARGET_OWNER_TAG, token=22)
        router = registry_router(process, owned)

        # The fullscreen frontend has emptied the manager and recycled the
        # inactive block contents.  This is the exact live failure captured
        # after opening the map with a co-op selector circle visible.
        process.seed_u64(MANAGER + 0x10F00, 0)
        process.seed_u64(MANAGER + 0x10EF0, 0)
        process.seed_u16(NODE + 0xD54, 0)
        process.seed_u32(NODE + 0xD2C, 0)
        process.seed_u32(NODE + 0xD7C, 0x42FF0000)
        process.seed_u64(NODE + 0xD68, 0x3F80000000000000)
        process.seed_u64(NODE + 0xD70, 0x4300000043000000)

        payload = router._owned_payload(
            owned,
            operation=target_router.TARGET_OPERATION_RECONCILE,
        )
        sequence = router.arm(payload)
        process.seed_u32(router.state + target_router.TARGET_OBSERVED_OFFSET, TARGET)
        process.seed_u32(
            router.state + target_router.TARGET_OBSERVED_PALETTE_OFFSET,
            owned.palette,
        )
        process.seed_u32(
            router.state + target_router.TARGET_ACK_SEQUENCE_OFFSET,
            sequence,
        )
        process.seed_u32(router.state, 3)
        process.writes.clear()

        self.assertTrue(router.release_frontend_reset_ownership(owned))

        self.assertIsNone(router.ownership(target_router.P1_TARGET_OWNER_TAG))
        self.assertIsNone(router.pending_creation())
        self.assertFalse(
            any(NODE <= address < NODE + 0x1000 for address, _ in process.writes)
        )

    def test_frontend_reset_release_rejects_every_ambiguous_pool_state(self) -> None:
        mutations = {
            "linked_head": lambda process: process.seed_u64(
                MANAGER + 0x10F00, NODE + 0x1000
            ),
            "linked_current": lambda process: process.seed_u64(
                MANAGER + 0x10EF0, NODE + 0x1000
            ),
            "active_owned_block": lambda process: process.seed_u16(
                NODE + 0xD54, 2
            ),
            "different_manager": lambda process: process.seed_u64(
                target_circle.TARGET_UI_MANAGER_GLOBAL, POST_MANAGER
            ),
            "different_allocator": lambda process: process.seed_u64(
                MANAGER + target_circle.TARGET_CIRCLE_ALLOCATOR_OFFSET,
                POST_ALLOCATOR,
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                process = live_process()
                owned = publication(owner_tag=target_router.P1_TARGET_OWNER_TAG)
                router = registry_router(process, owned)
                process.seed_u64(MANAGER + 0x10F00, 0)
                process.seed_u64(MANAGER + 0x10EF0, 0)
                process.seed_u16(NODE + 0xD54, 0)
                mutate(process)
                before = process.read(router.state, target_router.STATE_SIZE)
                process.writes.clear()

                self.assertFalse(router.release_frontend_reset_ownership(owned))

                self.assertEqual(
                    router.ownership(target_router.P1_TARGET_OWNER_TAG), owned
                )
                self.assertEqual(
                    process.read(router.state, target_router.STATE_SIZE), before
                )
                self.assertEqual(process.writes, [])

    def test_frontend_suspension_recovers_reset_owner_without_recreating(self) -> None:
        process = live_process()
        owned = publication(owner_tag=target_router.P1_TARGET_OWNER_TAG)
        router = FakeRouter(owned)
        instance = controller(process, router)
        process.seed_u64(MANAGER + 0x10F00, 0)
        process.seed_u64(MANAGER + 0x10EF0, 0)
        process.seed_u16(NODE + 0xD54, 0)
        process.seed_u32(NODE + 0xD2C, 0)
        process.seed_u32(NODE + 0xD7C, 0x42FF0000)
        process.writes.clear()

        self.assertTrue(instance.suspend_for_native_frontend())

        self.assertIsNone(router.publication)
        self.assertIsNone(instance.ownership)
        self.assertEqual(instance.node, 0)
        self.assertFalse(
            any(NODE <= address < NODE + 0x1000 for address, _ in process.writes)
        )

    def test_foreign_pending_create_cannot_be_overwritten(self) -> None:
        process = live_process()
        router = FakeRouter(None)
        router.pending = create_journal(
            owner_tag=target_router.P1_TARGET_OWNER_TAG
        )
        instance = controller(
            process,
            router,
            owner_tag=target_router.P2_TARGET_OWNER_TAG,
        )
        attempts: list[int] = []
        instance._create = lambda target, palette: attempts.append(target)

        self.assertFalse(instance.sync(TARGET))
        self.assertEqual(attempts, [])
        self.assertIsNotNone(router.pending)

    def test_safe_prefactory_rejection_is_consumed_but_not_retried_immediately(
        self,
    ) -> None:
        process = live_process()
        router = FakeRouter(None)
        rejected = create_journal(status=4, factory_entered=False)
        router.pending = rejected
        instance = controller(process, router)
        attempts: list[int] = []
        instance._create = lambda target, palette: attempts.append(target)

        with mock.patch.object(target_circle.time, "monotonic", return_value=10.0):
            self.assertFalse(instance.sync(TARGET))

        self.assertIsNone(router.pending)
        self.assertEqual(router.consumed, [rejected])
        self.assertEqual(attempts, [])
        self.assertGreaterEqual(instance.next_retry, 10.5)

    def test_clear_reconciles_then_retires_an_unconsumed_success(self) -> None:
        process = live_process()
        router = FakeRouter(None)
        journal = create_journal()
        router.pending = journal
        instance = controller(process, router)

        instance.clear()

        self.assertEqual(router.consumed, [journal])
        self.assertEqual(len(router.retirements), 1)
        self.assertIsNone(router.publication)
        self.assertEqual(process.read(NODE + 0xD54, 2), struct.pack("<H", 5))
        self.assertEqual(instance.node, 0)

    def test_failure_backoff_is_measured_after_a_slow_create_failure(self) -> None:
        process = MemoryProcess()
        process.seed_u64(target_circle.TARGET_UI_MANAGER_GLOBAL, MANAGER)
        process.seed_u64(
            MANAGER + target_circle.TARGET_CIRCLE_ALLOCATOR_OFFSET,
            ALLOCATOR,
        )
        process.seed_u64(MANAGER + 0x10F00, 0)
        process.seed_u64(MANAGER + 0x10EF0, 0)
        instance = controller(process, FakeRouter(None))
        instance._create = mock.Mock(side_effect=OSError("late read failed"))

        with mock.patch.object(
            target_circle.time,
            "monotonic",
            side_effect=(100.0, 500.0),
        ):
            self.assertFalse(instance.sync(TARGET))

        self.assertEqual(instance.next_retry, 500.5)

    def test_unchanged_explicitly_owned_circle_performs_no_native_write(self) -> None:
        process = live_process()
        router = FakeRouter(publication())
        instance = controller(process, router)
        instance._recover(instance._snapshot())
        process.writes.clear()

        self.assertTrue(instance.sync(TARGET, palette=4))
        self.assertEqual(process.writes, [])

    def test_stock_nodes_are_never_recovered_by_palette(self) -> None:
        for palette in range(7):
            with self.subTest(palette=palette):
                process = live_process(palette=palette)
                router = FakeRouter(None)
                instance = controller(process, router)

                instance._recover(instance._snapshot())
                instance.clear()

                self.assertEqual(instance.node, 0)
                self.assertEqual(process.writes, [])

    def test_post_allocation_failure_reconciles_journal_without_second_create(
        self,
    ) -> None:
        process = MemoryProcess()
        process.seed_u64(target_circle.TARGET_UI_MANAGER_GLOBAL, MANAGER)
        process.seed_u64(
            MANAGER + target_circle.TARGET_CIRCLE_ALLOCATOR_OFFSET,
            ALLOCATOR,
        )
        process.seed_u64(MANAGER + 0x10F00, 0)
        process.seed_u64(MANAGER + 0x10EF0, 0)
        router = FakeRouter(None)
        instance = controller(process, router)
        attempts = []

        def allocated_then_read_failed(target: int, palette: int):
            assert palette == 4
            attempts.append(target)
            # The callback, not the stale Python preflight, defines the heap
            # identity of the allocation transaction.
            process.seed_u64(
                target_circle.TARGET_UI_MANAGER_GLOBAL,
                POST_MANAGER,
            )
            process.seed_u64(
                POST_MANAGER + target_circle.TARGET_CIRCLE_ALLOCATOR_OFFSET,
                POST_ALLOCATOR,
            )
            process.seed_u64(POST_MANAGER + 0x10F00, NODE)
            process.seed_u64(POST_MANAGER + 0x10EF0, NODE)
            process.seed_u64(NODE + 0xD70, 0)
            process.seed_u16(NODE + 0xD54, 2)
            process.seed_u32(NODE + 0xD2C, target)
            process.seed_u32(NODE + 0xD7C, 4)
            router.pending = create_journal(
                target=target,
                manager=POST_MANAGER,
                allocator=POST_ALLOCATOR,
            )
            raise OSError("post-callback mailbox read failed")

        instance._create = allocated_then_read_failed

        self.assertFalse(instance.sync(TARGET))
        self.assertEqual(attempts, [TARGET])
        expected_journal = create_journal(
            manager=POST_MANAGER,
            allocator=POST_ALLOCATOR,
        )
        self.assertEqual(router.pending, expected_journal)

        instance.next_retry = 0.0
        self.assertTrue(instance.sync(TARGET))
        self.assertEqual(attempts, [TARGET])
        self.assertIsNone(router.pending)
        self.assertEqual(router.consumed, [expected_journal])
        self.assertIsNotNone(router.publication)
        self.assertEqual(router.publication.manager, POST_MANAGER)
        self.assertEqual(router.publication.allocator, POST_ALLOCATOR)

    def test_owner_tag_cannot_adopt_another_players_publication(self) -> None:
        process = live_process()
        router = FakeRouter(publication())
        instance = controller(
            process,
            router,
            owner_tag=target_router.P2_TARGET_OWNER_TAG,
        )

        instance._recover(instance._snapshot())
        instance.clear()

        self.assertEqual(instance.node, 0)
        self.assertEqual(process.writes, [])
        self.assertIsNotNone(router.publication)

    def test_exact_publication_recovers_and_clear_retires_only_that_node(self) -> None:
        process = live_process()
        owned = publication()
        router = FakeRouter(owned)
        instance = controller(process, router)

        instance._recover(instance._snapshot())
        process.writes.clear()
        instance.clear()

        self.assertEqual(process.writes, [(NODE + 0xD54, struct.pack("<H", 5))])
        self.assertEqual(router.retirements, [owned])
        self.assertEqual(router.releases, [])
        self.assertIsNone(router.publication)
        self.assertIsNone(instance.ownership)

    def test_clear_after_restart_recovers_then_retires_resident_ownership(
        self,
    ) -> None:
        process = live_process()
        owned = publication(token=11)
        router = FakeRouter(owned)
        instance = controller(process, router)

        instance.clear()

        self.assertEqual(router.retirements, [owned])
        self.assertIsNone(router.publication)
        self.assertEqual(process.read(NODE + 0xD54, 2), struct.pack("<H", 5))
        self.assertIsNone(instance.ownership)

    def test_clear_is_fail_closed_for_every_live_identity_mismatch(self) -> None:
        mutations = {
            "manager": lambda process: process.seed_u64(
                target_circle.TARGET_UI_MANAGER_GLOBAL, MANAGER + 0x1000
            ),
            "membership": lambda process: process.seed_u64(MANAGER + 0x10F00, 0),
            "state": lambda process: process.seed_u16(NODE + 0xD54, 3),
        }
        for mismatch, mutate in mutations.items():
            with self.subTest(mismatch=mismatch):
                process = live_process()
                owned = publication()
                router = FakeRouter(owned)
                instance = controller(process, router)
                instance._recover(instance._snapshot())
                process.writes.clear()
                mutate(process)

                instance.clear()

                self.assertEqual(process.writes, [])
                self.assertEqual(router.publication, owned)
                self.assertEqual(instance.ownership, owned)

    def test_clear_reconciles_completed_metadata_write_then_retires(self) -> None:
        process = live_process(target=TARGET + 1, palette=2)
        owned = publication(token=9)
        router = FakeRouter(owned)
        instance = controller(process, router)
        instance.node = NODE
        instance.target = TARGET
        instance.published_palette = 4
        instance.ownership = owned

        instance.clear()

        self.assertEqual(len(router.retirements), 1)
        retired = router.retirements[0]
        self.assertEqual(retired.token, 9)
        self.assertEqual(retired.target, TARGET + 1)
        self.assertEqual(retired.palette, 2)
        self.assertIsNone(router.publication)
        self.assertEqual(process.read(NODE + 0xD54, 2), struct.pack("<H", 5))

    def test_new_allocator_retires_only_the_obsolete_sidecar(self) -> None:
        process = live_process()
        owned = publication()
        router = FakeRouter(owned)
        instance = controller(process, router)
        instance._recover(instance._snapshot())
        process.seed_u64(
            target_circle.TARGET_UI_MANAGER_GLOBAL,
            POST_MANAGER,
        )
        process.seed_u64(
            POST_MANAGER + target_circle.TARGET_CIRCLE_ALLOCATOR_OFFSET,
            POST_ALLOCATOR,
        )
        process.seed_u64(POST_MANAGER + 0x10F00, 0)
        process.seed_u64(POST_MANAGER + 0x10EF0, 0)
        process.writes.clear()

        instance.clear()

        self.assertEqual(process.writes, [])
        self.assertIsNone(router.publication)
        self.assertEqual(router.releases, [owned])
        self.assertIsNone(instance.ownership)

    def test_new_allocator_discards_old_create_journal_without_touching_reused_node(
        self,
    ) -> None:
        process = live_process()
        owned = publication(token=17)
        router = registry_router(process, owned)
        journal = create_journal()
        seed_create_journal(process, router, journal)

        # A load has installed a genuinely different nonzero allocator while
        # reusing the same virtual node address for a stock circle.
        process.seed_u64(target_circle.TARGET_UI_MANAGER_GLOBAL, POST_MANAGER)
        process.seed_u64(
            POST_MANAGER + target_circle.TARGET_CIRCLE_ALLOCATOR_OFFSET,
            POST_ALLOCATOR,
        )
        process.seed_u64(POST_MANAGER + 0x10F00, NODE)
        process.seed_u64(POST_MANAGER + 0x10EF0, NODE)
        process.seed_u16(NODE + 0xD54, 2)
        process.seed_u32(NODE + 0xD2C, TARGET + 0x40)
        process.seed_u32(NODE + 0xD7C, 6)
        process.writes.clear()

        self.assertTrue(
            router.discard_creation_from_obsolete_allocator(
                journal,
                manager=POST_MANAGER,
                allocator=POST_ALLOCATOR,
            )
        )

        self.assertIsNone(router.pending_creation())
        self.assertIsNone(router.ownership(owned.owner_tag))
        self.assertEqual(process.read(NODE + 0xD54, 2), struct.pack("<H", 2))
        self.assertEqual(process.u32(NODE + 0xD2C), TARGET + 0x40)
        self.assertEqual(process.u32(NODE + 0xD7C), 6)
        self.assertFalse(
            any(NODE <= address < NODE + 0x1000 for address, _ in process.writes)
        )

    def test_old_create_journal_remains_a_circuit_breaker_without_distinct_allocator(
        self,
    ) -> None:
        for current_allocator in (0, ALLOCATOR):
            with self.subTest(current_allocator=current_allocator):
                process = live_process()
                router = registry_router(process)
                journal = create_journal(status=4, factory_entered=True)
                seed_create_journal(process, router, journal)
                process.seed_u64(
                    target_circle.TARGET_UI_MANAGER_GLOBAL,
                    POST_MANAGER,
                )
                process.seed_u64(
                    POST_MANAGER + target_circle.TARGET_CIRCLE_ALLOCATOR_OFFSET,
                    current_allocator,
                )
                before = process.read(router.state, target_router.STATE_SIZE)
                process.writes.clear()

                self.assertFalse(
                    router.discard_creation_from_obsolete_allocator(
                        journal,
                        manager=POST_MANAGER,
                        allocator=current_allocator,
                    )
                )

                self.assertEqual(
                    process.read(router.state, target_router.STATE_SIZE), before
                )
                self.assertEqual(router.pending_creation(), journal)
                self.assertEqual(process.writes, [])

    def test_load_between_create_callback_and_claim_discards_only_old_journal(
        self,
    ) -> None:
        process = live_process()
        router = FakeRouter(None)
        journal = create_journal()
        router.pending = journal
        instance = controller(process, router)
        attempts: list[int] = []
        instance._create = lambda target, palette: attempts.append(target)

        # The callback completed on ALLOCATOR, then a load replaced it before
        # Python could claim.  NODE is now stock data in POST_ALLOCATOR.
        process.seed_u64(target_circle.TARGET_UI_MANAGER_GLOBAL, POST_MANAGER)
        process.seed_u64(
            POST_MANAGER + target_circle.TARGET_CIRCLE_ALLOCATOR_OFFSET,
            POST_ALLOCATOR,
        )
        process.seed_u64(POST_MANAGER + 0x10F00, NODE)
        process.seed_u64(POST_MANAGER + 0x10EF0, NODE)
        process.seed_u32(NODE + 0xD2C, TARGET + 0x40)
        process.seed_u32(NODE + 0xD7C, 6)
        process.writes.clear()

        self.assertFalse(instance.sync(TARGET))

        self.assertEqual(router.discarded, [journal])
        self.assertIsNone(router.pending)
        self.assertIsNone(router.publication)
        self.assertEqual(attempts, [])
        self.assertEqual(process.read(NODE + 0xD54, 2), struct.pack("<H", 2))
        self.assertEqual(process.u32(NODE + 0xD2C), TARGET + 0x40)
        self.assertEqual(process.u32(NODE + 0xD7C), 6)
        self.assertEqual(process.writes, [])

    def test_same_allocator_identity_mismatch_blocks_a_replacement_create(
        self,
    ) -> None:
        process = live_process()
        process.seed_u16(NODE + 0xD54, 3)
        owned = publication()
        router = FakeRouter(owned)
        instance = controller(process, router)
        attempts: list[int] = []
        instance._create = lambda target, palette: attempts.append(target)

        self.assertFalse(instance.sync(TARGET))

        self.assertEqual(attempts, [])
        self.assertEqual(router.publication, owned)
        self.assertEqual(router.releases, [])
        self.assertEqual(instance.node, 0)

    def test_restart_reconciles_completed_retarget_and_palette_without_create(
        self,
    ) -> None:
        process = live_process(target=TARGET + 1, palette=2)
        owned = publication(token=13)
        router = FakeRouter(owned)
        instance = controller(process, router)

        instance._recover(instance._snapshot())

        self.assertEqual(instance.node, NODE)
        self.assertEqual(instance.target, TARGET + 1)
        self.assertEqual(instance.published_palette, 2)
        self.assertEqual(router.publication.token, 13)
        self.assertEqual(router.publication.target, TARGET + 1)
        self.assertEqual(router.publication.palette, 2)
        self.assertEqual(router.releases, [])

    def test_stale_token_cannot_clear_republished_same_address(self) -> None:
        process = live_process()
        stale = publication(token=7)
        current = publication(token=8)
        router = FakeRouter(current)
        instance = controller(process, router)
        instance.node = NODE
        instance.target = TARGET
        instance.published_palette = 4
        instance.ownership = stale

        instance.clear()

        self.assertEqual(process.writes, [])
        self.assertEqual(router.publication, current)
        self.assertEqual(router.releases, [])

    def test_router_retirement_has_no_python_side_effect_after_callback_failure(
        self,
    ) -> None:
        process = live_process()
        owned = publication()
        router = registry_router(process, owned)
        process.writes.clear()

        router._execute_owned = mock.Mock(side_effect=RuntimeError("rejected"))

        self.assertFalse(router.retire_ownership(owned))

        self.assertEqual(process.read(NODE + 0xD54, 2), struct.pack("<H", 2))
        self.assertEqual(process.writes, [])
        self.assertEqual(router.ownership(owned.owner_tag), owned)

    def test_success_journal_requires_exact_ownership_before_consumption(
        self,
    ) -> None:
        process = live_process()
        router = registry_router(process)
        journal = create_journal()
        seed_create_journal(process, router, journal)

        self.assertEqual(router.pending_creation(), journal)
        self.assertFalse(router.consume_creation(journal))

        owned = router.claim_ownership(
            target_router.P1_TARGET_OWNER_TAG,
            ALLOCATOR,
            NODE,
            TARGET,
            4,
        )
        self.assertTrue(router.consume_creation(journal))
        self.assertIsNone(router.pending_creation())
        self.assertEqual(router.ownership(owned.owner_tag), owned)

    def test_stock_circle_cannot_be_claimed_without_successful_create_journal(
        self,
    ) -> None:
        process = live_process()
        router = registry_router(process)
        process.writes.clear()

        with self.assertRaisesRegex(RuntimeError, "successful CREATE journal"):
            router.claim_ownership(
                target_router.P1_TARGET_OWNER_TAG,
                ALLOCATOR,
                NODE,
                TARGET,
                4,
            )

        self.assertIsNone(router.ownership(target_router.P1_TARGET_OWNER_TAG))
        self.assertEqual(process.read(NODE + 0xD54, 2), struct.pack("<H", 2))
        self.assertEqual(process.writes, [])

    def test_router_restore_refuses_any_resident_ownership(self) -> None:
        process = live_process()
        owned = publication()
        router = registry_router(process, owned)
        before = process.read(router.state, target_router.STATE_SIZE)

        self.assertFalse(router.restore())

        self.assertEqual(process.read(router.state, target_router.STATE_SIZE), before)
        self.assertEqual(router.ownership(owned.owner_tag), owned)

    def test_ambiguous_factory_journal_survives_arm_restore_and_consumption(
        self,
    ) -> None:
        process = live_process()
        router = registry_router(process)
        journal = create_journal(status=4, factory_entered=True)
        seed_create_journal(process, router, journal)
        before = process.read(router.state, target_router.STATE_SIZE)
        payload = bytearray(0x48)
        struct.pack_into("<I", payload, target_router.TARGET_HANDLE_OFFSET, TARGET)
        struct.pack_into(
            "<I",
            payload,
            target_router.TARGET_OWNER_OFFSET,
            target_router.P2_TARGET_OWNER_TAG,
        )

        self.assertFalse(router.consume_creation(journal))
        self.assertFalse(router.restore())
        with self.assertRaisesRegex(RuntimeError, "unconsumed"):
            router.arm(payload)

        self.assertEqual(process.read(router.state, target_router.STATE_SIZE), before)
        self.assertEqual(router.pending_creation(), journal)

    def test_prefactory_rejection_can_be_consumed_without_ownership(self) -> None:
        process = live_process()
        router = registry_router(process)
        journal = create_journal(status=4, factory_entered=False)
        seed_create_journal(process, router, journal)

        self.assertTrue(router.consume_creation(journal))
        self.assertIsNone(router.pending_creation())
        self.assertEqual(process.u32(router.state), 0)

    def test_raw_address_cannot_reach_the_authenticated_retarget_api(self) -> None:
        process = live_process()
        router = registry_router(process, publication())
        process.writes.clear()

        with self.assertRaisesRegex(TypeError, "explicit ownership"):
            router.retarget(NODE, TARGET + 1)

        self.assertEqual(process.writes, [])
        self.assertEqual(process.u32(NODE + 0xD2C), TARGET)
        self.assertEqual(process.u32(NODE + 0xD7C), 4)

    def test_arm_preserves_persistent_ownership_registry(self) -> None:
        process = MemoryProcess()
        router = target_router.NativeTargetCircleRetargeter.__new__(
            target_router.NativeTargetCircleRetargeter
        )
        router.process = process
        router.state = 0x500000
        router._sequence = 0
        router.patched = False
        router.quarantined = False
        router.maintain = lambda: True

        image = bytearray(router._initial_state_image())
        owned = publication(token=7)
        offset = target_router.TARGET_OWNERSHIP_RECORDS_OFFSET
        image[offset : offset + target_router.TARGET_OWNERSHIP_RECORD_SIZE] = (
            target_router._pack_ownership_record(owned)
        )
        process.seed(router.state, image)
        before = process.read(
            router.state + target_router.TARGET_OWNERSHIP_MAGIC_OFFSET,
            target_router.STATE_SIZE - target_router.TARGET_OWNERSHIP_MAGIC_OFFSET,
        )

        payload = router._owned_payload(
            owned,
            operation=target_router.TARGET_OPERATION_RECONCILE,
        )
        router.arm(payload)

        after = process.read(
            router.state + target_router.TARGET_OWNERSHIP_MAGIC_OFFSET,
            target_router.STATE_SIZE - target_router.TARGET_OWNERSHIP_MAGIC_OFFSET,
        )
        self.assertEqual(after, before)

    def test_shared_router_instances_advance_the_resident_sequence(self) -> None:
        process = MemoryProcess()
        owned = publication()
        image = bytearray(
            target_router.NativeTargetCircleRetargeter._initial_state_image()
        )
        image[
            target_router.TARGET_OWNERSHIP_RECORDS_OFFSET :
            target_router.TARGET_OWNERSHIP_RECORDS_OFFSET
            + target_router.TARGET_OWNERSHIP_RECORD_SIZE
        ] = target_router._pack_ownership_record(owned)
        struct.pack_into(
            "<I",
            image,
            target_router.TARGET_OWNERSHIP_NEXT_TOKEN_OFFSET,
            owned.token + 1,
        )
        process.seed(0x500000, image)

        def make_router():
            router = target_router.NativeTargetCircleRetargeter.__new__(
                target_router.NativeTargetCircleRetargeter
            )
            router.process = process
            router.state = 0x500000
            router._sequence = 0
            router.patched = False
            router.quarantined = False
            router.maintain = lambda: True
            return router

        payload = target_router.NativeTargetCircleRetargeter._owned_payload(
            owned,
            operation=target_router.TARGET_OPERATION_RECONCILE,
        )
        first = make_router()
        second = make_router()

        first.arm(payload)
        process.seed_u32(first.state, 4)
        process.seed_u32(
            first.state + target_router.TARGET_ACK_SEQUENCE_OFFSET,
            first._sequence,
        )
        second.arm(payload)

        self.assertEqual(first._sequence, 1)
        self.assertEqual(second._sequence, 2)
        self.assertEqual(
            process.u32(second.state + target_router.TARGET_SEQUENCE_OFFSET),
            2,
        )

    def test_registry_rejects_checksum_damage_and_duplicate_nodes(self) -> None:
        image = bytearray(
            target_router.NativeTargetCircleRetargeter._initial_state_image()
        )
        first = publication(token=1)
        first_offset = target_router.TARGET_OWNERSHIP_RECORDS_OFFSET
        image[
            first_offset : first_offset + target_router.TARGET_OWNERSHIP_RECORD_SIZE
        ] = target_router._pack_ownership_record(first)
        struct.pack_into(
            "<I", image, target_router.TARGET_OWNERSHIP_NEXT_TOKEN_OFFSET, 3
        )
        self.assertTrue(target_router.NativeTargetCircleRetargeter._valid_state(image))

        damaged = bytearray(image)
        damaged[first_offset + 0x20] ^= 1
        self.assertFalse(
            target_router.NativeTargetCircleRetargeter._valid_state(damaged)
        )

        duplicate = publication(
            owner_tag=target_router.P2_TARGET_OWNER_TAG,
            token=2,
        )
        second_offset = first_offset + target_router.TARGET_OWNERSHIP_RECORD_SIZE
        image[
            second_offset : second_offset + target_router.TARGET_OWNERSHIP_RECORD_SIZE
        ] = target_router._pack_ownership_record(duplicate)
        self.assertFalse(target_router.NativeTargetCircleRetargeter._valid_state(image))

    def test_released_token_is_not_reused(self) -> None:
        process = live_process()
        router = target_router.NativeTargetCircleRetargeter.__new__(
            target_router.NativeTargetCircleRetargeter
        )
        router.process = process
        router.state = 0x500000
        router.maintain = lambda: True
        process.seed(router.state, router._initial_state_image())
        seed_create_journal(process, router, create_journal())

        first = router.claim_ownership(
            target_router.P1_TARGET_OWNER_TAG, ALLOCATOR, NODE, TARGET, 4
        )
        self.assertTrue(router.release_ownership(first))
        second = router.claim_ownership(
            target_router.P1_TARGET_OWNER_TAG, ALLOCATOR, NODE, TARGET, 4
        )

        self.assertNotEqual(first.token, second.token)
        self.assertEqual((first.token, second.token), (1, 2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
