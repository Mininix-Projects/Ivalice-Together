from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import struct
import threading

import pytest

import coop_runtime as coop
import native_area_target_circle_controller as area_circle
import native_target_circle_router as target_router
import native_ui_tick_mux as ui_mux


class FaultProcess:
    def __init__(
        self,
        *,
        damage_base: int = 0x60000000,
        primary_base: int = 0x50000000,
        dual_base: int = 0x70000000,
    ) -> None:
        self.damage_base = damage_base
        self.primary_base = primary_base
        self.dual_base = dual_base
        self.memory: dict[int, int] = {}
        self.events: list[tuple[str, int, bytes]] = []
        self.failure: tuple[str, int, bool] | None = None
        self.seed(ui_mux.UI_MANAGER_TICK_CALL, ui_mux.ORIGINAL_TICK_CALL)
        self.seed(
            target_router.TARGET_PALETTE_COMMAND_LOAD_SITE,
            target_router.TARGET_PALETTE_COMMAND_LOAD_ORIGINAL,
        )
        self.seed(
            target_router.TARGET_PALETTE_FIELD_LOAD_SITE,
            target_router.TARGET_PALETTE_FIELD_LOAD_ORIGINAL,
        )
        self.seed(
            target_router.TARGET_PALETTE_CREATE_LOAD_SITE,
            target_router.TARGET_PALETTE_CREATE_LOAD_ORIGINAL,
        )
        self.seed(primary_base, coop.CAVE_MAGIC)

    def seed(self, address: int, payload: bytes | bytearray) -> None:
        for offset, value in enumerate(bytes(payload)):
            self.memory[int(address) + offset] = value

    def read(self, address: int, size: int) -> bytes:
        return bytes(
            self.memory.get(int(address) + offset, 0) for offset in range(int(size))
        )

    def _mutate(self, kind: str, address: int, payload: bytes) -> None:
        failure = self.failure
        if failure is not None and failure[:2] == (kind, int(address)):
            self.failure = None
            if failure[2]:
                self.events.append((kind, int(address), payload))
                self.seed(address, payload)
            raise OSError(f"simulated {kind} failure at {address:#x}")
        self.events.append((kind, int(address), payload))
        self.seed(address, payload)

    def write(self, address: int, payload: bytes | bytearray) -> None:
        self._mutate("write", address, bytes(payload))

    def patch_code(self, address: int, payload: bytes | bytearray) -> None:
        self._mutate("patch", address, bytes(payload))

    def u32(self, address: int) -> int:
        return struct.unpack("<I", self.read(address, 4))[0]

    def u64(self, address: int) -> int:
        return struct.unpack("<Q", self.read(address, 8))[0]

    def ensure_damage_number_theme_allocation(self) -> int:
        return self.damage_base

    def ensure_cave_allocation(self, *, allow_allocate: bool = True) -> int:
        del allow_allocate
        return self.primary_base

    def ensure_dual_interaction_allocation(
        self,
        *,
        allow_allocate: bool = True,
    ) -> int:
        del allow_allocate
        return self.dual_base

    def fail_once(self, kind: str, address: int, *, after: bool) -> None:
        self.failure = (kind, int(address), bool(after))


class SilentCorruptionProcess(FaultProcess):
    def __init__(self) -> None:
        super().__init__()
        self.corrupt_after: tuple[str, int, int] | None = None

    def _mutate(self, kind: str, address: int, payload: bytes) -> None:
        super()._mutate(kind, address, payload)
        corruption = self.corrupt_after
        if corruption is not None and corruption[:2] == (kind, int(address)):
            self.corrupt_after = None
            self.seed(int(address) + corruption[2], b"\xCC")


def _target_create_payload() -> bytes:
    payload = bytearray(target_router.TARGET_STATE_MAGIC_OFFSET)
    struct.pack_into(
        "<I",
        payload,
        target_router.TARGET_OPERATION_OFFSET,
        target_router.TARGET_OPERATION_CREATE,
    )
    struct.pack_into(
        "<I", payload, target_router.TARGET_HANDLE_OFFSET, 0x100008
    )
    struct.pack_into("<I", payload, target_router.TARGET_VARIANT_OFFSET, 1)
    struct.pack_into(
        "<I",
        payload,
        target_router.TARGET_OWNER_OFFSET,
        target_router.P1_TARGET_OWNER_TAG,
    )
    struct.pack_into(
        "<I", payload, target_router.TARGET_DESIRED_PALETTE_OFFSET, 4
    )
    return bytes(payload)


def test_mux_target_and_area_subregions_exactly_partition_their_workspaces() -> None:
    assert ui_mux.REGION_SIZE == target_router.SERVICE_OFFSET
    assert target_router.SERVICE_OFFSET <= target_router.CODE_OFFSET
    assert target_router.CODE_OFFSET < target_router.STATE_OFFSET
    assert target_router.STATE_OFFSET + target_router.STATE_SIZE == (
        target_router.REGION_SIZE
    )
    assert area_circle.CODE_OFFSET < area_circle.STATE_OFFSET
    assert area_circle.STATE_OFFSET + area_circle.STATE_SIZE == (
        area_circle.REGION_SIZE
    )
    assert ui_mux.CODE_OFFSET + len(
        ui_mux.build_mux(
            0x60002100,
            (0x60002040, 0x60002048, 0x60002050),
        )
    ) <= ui_mux.REGION_SIZE
    assert target_router.CODE_OFFSET + len(
        target_router.build_target_dispatcher(0x60002300, 0x60002800)
    ) <= target_router.STATE_OFFSET
    assert area_circle.CODE_OFFSET + len(
        area_circle.build_create_dispatcher(0x60003100, 0x60003800)
    ) <= area_circle.STATE_OFFSET


def test_mux_fresh_install_is_complete_before_its_permanent_root() -> None:
    process = FaultProcess()

    mux = ui_mux.ensure_installed(process)

    assert mux.installed
    assert mux.owns_static
    assert process.read(ui_mux.UI_MANAGER_TICK_CALL, 5) == mux.patch
    first_root = next(
        index
        for index, event in enumerate(process.events)
        if event[:2] == ("patch", ui_mux.UI_MANAGER_TICK_CALL)
    )
    assert (
        "patch",
        mux.region,
        mux._static_image(),
    ) in process.events[:first_root]
    assert process.read(mux.region, ui_mux.REGION_SIZE) == mux._static_image()

    snapshot = process.read(mux.region, ui_mux.REGION_SIZE)
    event_count = len(process.events)
    mux.restore()
    assert len(process.events) == event_count
    assert process.read(ui_mux.UI_MANAGER_TICK_CALL, 5) == mux.patch
    assert process.read(mux.region, ui_mux.REGION_SIZE) == snapshot


def test_mux_exact_hot_adoption_is_read_only() -> None:
    process = FaultProcess()
    owner = ui_mux.ensure_installed(process)
    process.events.clear()

    adopted = ui_mux.ensure_installed(process)

    assert adopted.installed
    assert not adopted.owns_static
    assert adopted.patch == owner.patch
    assert process.events == []


def test_headless_slot_uses_primary_not_dual_interaction_base_authority() -> None:
    process = FaultProcess()
    assert process.primary_base != process.dual_base
    mux = ui_mux.ensure_installed(process)
    slot = mux.slot_address(ui_mux.SLOT_HEADLESS_CORE)

    with pytest.raises(RuntimeError, match="invalid native UI tick handler"):
        mux.register_handler(
            ui_mux.SLOT_HEADLESS_CORE,
            process.dual_base + ui_mux.HEADLESS_CORE_HANDLER_OFFSET,
        )
    assert process.read(slot, 8) == b"\x00" * 8

    handler = process.primary_base + ui_mux.HEADLESS_CORE_HANDLER_OFFSET
    assert mux.register_handler(ui_mux.SLOT_HEADLESS_CORE, handler)
    assert mux.handler(ui_mux.SLOT_HEADLESS_CORE) == handler


def test_mux_silent_region_corruption_is_caught_before_root_publication() -> None:
    process = SilentCorruptionProcess()
    mux = ui_mux.NativeUITickMux(process, process.damage_base)
    process.corrupt_after = ("patch", mux.region, ui_mux.CODE_OFFSET + 7)

    with pytest.raises(RuntimeError, match="changed before root publication"):
        mux.ensure_installed()

    assert process.read(ui_mux.UI_MANAGER_TICK_CALL, 5) == (
        ui_mux.ORIGINAL_TICK_CALL
    )
    assert not any(
        kind == "patch" and address == ui_mux.UI_MANAGER_TICK_CALL
        for kind, address, _payload in process.events
    )


@pytest.mark.parametrize("boundary", ("region", "root"))
@pytest.mark.parametrize("after", (False, True))
def test_mux_failure_before_or_after_publication_is_exactly_recoverable(
    boundary: str,
    after: bool,
) -> None:
    process = FaultProcess()
    template = ui_mux.NativeUITickMux(process, process.damage_base)
    kind, address = (
        ("patch", template.region)
        if boundary == "region"
        else ("patch", ui_mux.UI_MANAGER_TICK_CALL)
    )
    process.fail_once(kind, address, after=after)

    with pytest.raises(RuntimeError, match="restart the game"):
        template.ensure_installed()

    region = process.read(template.region, ui_mux.REGION_SIZE)
    root = process.read(ui_mux.UI_MANAGER_TICK_CALL, 5)
    if boundary == "region" and not after:
        assert region == b"\x00" * ui_mux.REGION_SIZE
    else:
        assert region == template._static_image()
    if boundary == "root" and after:
        assert root == template.patch
    else:
        assert root == ui_mux.ORIGINAL_TICK_CALL

    events_before_retry = len(process.events)
    recovered = ui_mux.ensure_installed(process)
    assert recovered.resident_exact()
    if boundary == "root" and after:
        assert len(process.events) == events_before_retry


@pytest.mark.parametrize("root_patched", (False, True))
def test_mux_partial_region_is_rejected_without_mutation(root_patched: bool) -> None:
    process = FaultProcess()
    mux = ui_mux.NativeUITickMux(process, process.damage_base)
    process.seed(mux.region + ui_mux.CODE_OFFSET + 3, b"\xCC")
    if root_patched:
        process.seed(ui_mux.UI_MANAGER_TICK_CALL, mux.patch)
    process.events.clear()

    with pytest.raises(RuntimeError, match="region mismatch"):
        mux.ensure_installed()

    assert process.events == []


@pytest.mark.parametrize("service", ("target", "area"))
@pytest.mark.parametrize("boundary", ("static", "state"))
def test_foreign_service_is_rejected_before_broker_or_root_mutation(
    service: str,
    boundary: str,
) -> None:
    process = FaultProcess()
    if service == "target":
        region = (
            process.damage_base + coop.NATIVE_TARGET_ROUTER_WORKSPACE_OFFSET
        )
        static_address = region + target_router.SERVICE_OFFSET
        state = region + target_router.STATE_OFFSET

        def constructor():
            return target_router.NativeTargetCircleRetargeter(process)

    else:
        static_address = (
            process.damage_base + coop.NATIVE_AREA_TARGET_WORKSPACE_OFFSET
        )
        state = static_address + area_circle.STATE_OFFSET

        def constructor():
            return area_circle.NativeAreaTargetCircleController(
                process,
                owner_tag=area_circle.P2_OWNER_TAG,
                palette=4,
            )

    corrupt_address = static_address if boundary == "static" else state
    process.seed(corrupt_address + 0x60, b"\xCC")
    process.events.clear()

    with pytest.raises(RuntimeError, match="partial or foreign"):
        constructor()

    assert process.events == []
    assert process.read(ui_mux.UI_MANAGER_TICK_CALL, 5) == (
        ui_mux.ORIGINAL_TICK_CALL
    )
    broker = (
        process.damage_base + coop.NATIVE_TARGET_ROUTER_WORKSPACE_OFFSET
    )
    assert process.read(broker, ui_mux.REGION_SIZE) == b"\x00" * ui_mux.REGION_SIZE


@pytest.mark.parametrize("service", ("target", "area"))
@pytest.mark.parametrize("boundary", ("static", "state", "slot"))
@pytest.mark.parametrize("after", (False, True))
def test_service_publication_failure_never_rewrites_a_completed_image(
    service: str,
    boundary: str,
    after: bool,
) -> None:
    process = FaultProcess()
    mux = ui_mux.ensure_installed(process)
    if service == "target":
        region = (
            process.damage_base + coop.NATIVE_TARGET_ROUTER_WORKSPACE_OFFSET
        )
        static_address = region + target_router.SERVICE_OFFSET
        state = region + target_router.STATE_OFFSET
        slot = mux.slot_address(ui_mux.SLOT_TARGET_CIRCLE)
        def constructor():
            return target_router.NativeTargetCircleRetargeter(process)
    else:
        region = process.damage_base + coop.NATIVE_AREA_TARGET_WORKSPACE_OFFSET
        static_address = region
        state = region + area_circle.STATE_OFFSET
        slot = mux.slot_address(ui_mux.SLOT_AREA_CIRCLE)
        def constructor():
            return area_circle.NativeAreaTargetCircleController(
                process,
                owner_tag=area_circle.P2_OWNER_TAG,
                palette=4,
            )
    address = {"static": static_address, "state": state, "slot": slot}[boundary]
    operation = "patch" if boundary == "static" else "write"
    process.fail_once(operation, address, after=after)

    with pytest.raises(RuntimeError, match="restart the game"):
        constructor()

    assert process.read(ui_mux.UI_MANAGER_TICK_CALL, 5) == mux.patch
    assert not any(
        kind == "patch" and payload == ui_mux.ORIGINAL_TICK_CALL
        for kind, _address, payload in process.events
    )
    static_was_complete = any(
        kind == "patch" and event_address == static_address
        for kind, event_address, _payload in process.events
    )
    state_was_complete = any(
        kind == "write" and event_address == state
        for kind, event_address, _payload in process.events
    )
    process.events.clear()

    recovered = constructor()
    assert recovered.resident_exact()
    if static_was_complete:
        assert not any(event[1] == static_address for event in process.events)
    if state_was_complete:
        assert not any(event[1] == state for event in process.events)


@pytest.mark.parametrize(
    ("site", "original"),
    (
        (
            target_router.TARGET_PALETTE_COMMAND_LOAD_SITE,
            target_router.TARGET_PALETTE_COMMAND_LOAD_ORIGINAL,
        ),
        (
            target_router.TARGET_PALETTE_FIELD_LOAD_SITE,
            target_router.TARGET_PALETTE_FIELD_LOAD_ORIGINAL,
        ),
        (
            target_router.TARGET_PALETTE_CREATE_LOAD_SITE,
            target_router.TARGET_PALETTE_CREATE_LOAD_ORIGINAL,
        ),
    ),
)
@pytest.mark.parametrize("after", (False, True))
def test_each_palette_root_publication_is_transactionally_recoverable(
    site: int,
    original: bytes,
    after: bool,
) -> None:
    process = FaultProcess()
    ui_mux.ensure_installed(process)
    process.fail_once("patch", site, after=after)

    with pytest.raises(RuntimeError, match="restart the game"):
        target_router.NativeTargetCircleRetargeter(process)

    observed = process.read(site, len(original))
    assert observed != original if after else observed == original

    recovered = target_router.NativeTargetCircleRetargeter(process)
    assert recovered.resident_exact()
    assert all(
        process.read(root_site, len(patch)) == patch
        for root_site, _stock, patch in recovered._palette_root_contracts
    )


def test_hot_adoption_requires_all_three_exact_palette_roots() -> None:
    process = FaultProcess()
    owner = target_router.NativeTargetCircleRetargeter(process)
    site, original, _patch = owner._palette_root_contracts[1]
    process.seed(site, original)
    process.events.clear()

    with pytest.raises(RuntimeError, match="partial or foreign"):
        target_router.NativeTargetCircleRetargeter(process)

    assert process.events == []


@pytest.mark.parametrize("service", ("target", "area"))
@pytest.mark.parametrize("boundary", ("static", "state"))
def test_service_silent_corruption_is_caught_before_slot_publication(
    service: str,
    boundary: str,
) -> None:
    process = SilentCorruptionProcess()
    mux = ui_mux.ensure_installed(process)
    if service == "target":
        region = (
            process.damage_base + coop.NATIVE_TARGET_ROUTER_WORKSPACE_OFFSET
        )
        static_address = region + target_router.SERVICE_OFFSET
        state = region + target_router.STATE_OFFSET
        slot = mux.slot_address(ui_mux.SLOT_TARGET_CIRCLE)

        def constructor():
            return target_router.NativeTargetCircleRetargeter(process)

    else:
        region = process.damage_base + coop.NATIVE_AREA_TARGET_WORKSPACE_OFFSET
        static_address = region
        state = region + area_circle.STATE_OFFSET
        slot = mux.slot_address(ui_mux.SLOT_AREA_CIRCLE)

        def constructor():
            return area_circle.NativeAreaTargetCircleController(
                process,
                owner_tag=area_circle.P2_OWNER_TAG,
                palette=4,
            )

    corrupt_address = static_address if boundary == "static" else state
    operation = "patch" if boundary == "static" else "write"
    process.corrupt_after = (operation, corrupt_address, 0x60)

    with pytest.raises(RuntimeError, match="did not persist"):
        constructor()

    assert process.read(slot, 8) == b"\x00" * 8
    assert not any(address == slot for _kind, address, _payload in process.events)


@pytest.mark.parametrize("service", ("target", "area"))
def test_concurrent_service_construction_publishes_each_image_once(
    service: str,
) -> None:
    process = FaultProcess()
    start = threading.Barrier(8)

    if service == "target":
        region = (
            process.damage_base + coop.NATIVE_TARGET_ROUTER_WORKSPACE_OFFSET
        )
        static_address = region + target_router.SERVICE_OFFSET
        state = region + target_router.STATE_OFFSET

        def constructor(index: int):
            del index
            start.wait()
            return target_router.NativeTargetCircleRetargeter(process)

        slot_name = ui_mux.SLOT_TARGET_CIRCLE
    else:
        static_address = (
            process.damage_base + coop.NATIVE_AREA_TARGET_WORKSPACE_OFFSET
        )
        state = static_address + area_circle.STATE_OFFSET

        def constructor(index: int):
            start.wait()
            return area_circle.NativeAreaTargetCircleController(
                process,
                owner_tag=(
                    area_circle.P1_OWNER_TAG,
                    area_circle.P2_OWNER_TAG,
                    area_circle.P3_OWNER_TAG,
                )[index % 3],
                palette=index % 7,
            )

        slot_name = ui_mux.SLOT_AREA_CIRCLE

    with ThreadPoolExecutor(max_workers=8) as executor:
        services = list(executor.map(constructor, range(8)))

    mux = services[0].mux
    slot = mux.slot_address(slot_name)
    assert all(service_instance.resident_exact() for service_instance in services)
    assert sum(event[1] == static_address for event in process.events) == 1
    assert sum(event[1] == state for event in process.events) == 1
    assert sum(event[1] == slot for event in process.events) == 1


def test_three_area_owners_hot_adopt_one_identical_callback_read_only() -> None:
    process = FaultProcess()
    p1 = area_circle.NativeAreaTargetCircleController(
        process,
        owner_tag=area_circle.P1_OWNER_TAG,
        palette=0,
    )
    static_image = process.read(p1.region, area_circle.STATE_OFFSET)
    process.events.clear()

    p2 = area_circle.NativeAreaTargetCircleController(
        process,
        owner_tag=area_circle.P2_OWNER_TAG,
        palette=4,
    )
    p3 = area_circle.NativeAreaTargetCircleController(
        process,
        owner_tag=area_circle.P3_OWNER_TAG,
        palette=2,
    )

    assert p2._dispatch_code == p1._dispatch_code == p3._dispatch_code
    assert process.read(p1.region, area_circle.STATE_OFFSET) == static_image
    assert process.events == []


@pytest.mark.parametrize("dispatch_state", (1, 2))
def test_target_active_hot_adoption_is_read_only_and_quarantined(
    dispatch_state: int,
) -> None:
    process = FaultProcess()
    owner = target_router.NativeTargetCircleRetargeter(process)
    state_image = bytearray(owner._initial_state_image())
    struct.pack_into("<I", state_image, 0, dispatch_state)
    struct.pack_into(
        "<I",
        state_image,
        target_router.TARGET_OPERATION_OFFSET,
        target_router.TARGET_OPERATION_CREATE,
    )
    struct.pack_into(
        "<I", state_image, target_router.TARGET_HANDLE_OFFSET, 0x100008
    )
    struct.pack_into("<I", state_image, target_router.TARGET_VARIANT_OFFSET, 1)
    struct.pack_into(
        "<I",
        state_image,
        target_router.TARGET_OWNER_OFFSET,
        target_router.P1_TARGET_OWNER_TAG,
    )
    struct.pack_into(
        "<I",
        state_image,
        target_router.TARGET_SEQUENCE_OFFSET,
        2,
    )
    process.seed(owner.state, state_image)
    process.events.clear()

    adopted = target_router.NativeTargetCircleRetargeter(process)

    assert process.events == []
    assert adopted.patched
    assert adopted.quarantined
    assert not adopted.restore()
    assert process.events == []

    process.seed(adopted.state, struct.pack("<I", 4))
    process.seed(
        adopted.state + target_router.TARGET_ACK_SEQUENCE_OFFSET,
        struct.pack("<I", 2),
    )
    assert adopted.maintain()


def test_shared_area_mailbox_blocks_other_owner_until_terminal() -> None:
    process = FaultProcess()
    p1 = area_circle.NativeAreaTargetCircleController(
        process,
        owner_tag=area_circle.P1_OWNER_TAG,
        palette=0,
    )
    p2 = area_circle.NativeAreaTargetCircleController(
        process,
        owner_tag=area_circle.P2_OWNER_TAG,
        palette=4,
    )
    payload = bytearray(0x40)
    struct.pack_into("<I", payload, area_circle.AREA_TARGET_OFFSET, 0x100008)
    struct.pack_into("<I", payload, area_circle.AREA_PALETTE_OFFSET, 0)
    struct.pack_into("<f", payload, area_circle.AREA_RADIUS_OFFSET, 8.0)
    p1._arm(payload)
    snapshot = process.read(p1.state, area_circle.STATE_SIZE)
    process.events.clear()

    with pytest.raises(RuntimeError, match="still in flight"):
        p2._arm(payload)
    assert process.events == []
    assert process.read(p1.state, area_circle.STATE_SIZE) == snapshot

    process.seed(p1.state, struct.pack("<I", 3))
    process.seed(
        p1.state + area_circle.AREA_ACK_SEQUENCE_OFFSET,
        struct.pack("<I", p1._sequence),
    )
    p2._arm(payload)
    assert process.u32(p1.state) == 1
    assert process.u32(p1.state + area_circle.AREA_OWNER_TAG_OFFSET) == (
        area_circle.P2_OWNER_TAG
    )


def test_target_mailbox_blocks_sibling_router_and_restore_never_unpatches() -> None:
    process = FaultProcess()
    first = target_router.NativeTargetCircleRetargeter(process)
    second = target_router.NativeTargetCircleRetargeter(process)
    payload = _target_create_payload()
    first.arm(payload)
    static_image = process.read(
        first.region + target_router.SERVICE_OFFSET,
        target_router.STATE_OFFSET - target_router.SERVICE_OFFSET,
    )
    root = process.read(ui_mux.UI_MANAGER_TICK_CALL, 5)
    process.events.clear()

    with pytest.raises(RuntimeError, match="still in flight"):
        second.arm(payload)
    assert not first.restore()
    assert process.events == []
    assert process.read(ui_mux.UI_MANAGER_TICK_CALL, 5) == root
    assert process.read(
        first.region + target_router.SERVICE_OFFSET,
        len(static_image),
    ) == static_image

    process.seed(first.state, struct.pack("<I", 4))
    process.seed(
        first.state + target_router.TARGET_ACK_SEQUENCE_OFFSET,
        struct.pack("<I", first._sequence),
    )
    journal = first.pending_creation()
    assert journal is not None
    assert first.consume_creation(journal)
    assert first.restore()
    assert process.read(ui_mux.UI_MANAGER_TICK_CALL, 5) == root
    assert not any(kind == "patch" for kind, _address, _payload in process.events)


@pytest.mark.parametrize("service", ("target", "area"))
def test_concurrent_sibling_arms_allow_exactly_one_request(service: str) -> None:
    process = FaultProcess()
    if service == "target":
        first = target_router.NativeTargetCircleRetargeter(process)
        second = target_router.NativeTargetCircleRetargeter(process)
        payload = _target_create_payload()
        arm = target_router.NativeTargetCircleRetargeter.arm
        state = first.state
    else:
        first = area_circle.NativeAreaTargetCircleController(
            process,
            owner_tag=area_circle.P1_OWNER_TAG,
            palette=0,
        )
        second = area_circle.NativeAreaTargetCircleController(
            process,
            owner_tag=area_circle.P2_OWNER_TAG,
            palette=4,
        )
        payload = bytearray(0x40)
        struct.pack_into("<I", payload, area_circle.AREA_TARGET_OFFSET, 0x100008)
        struct.pack_into("<I", payload, area_circle.AREA_PALETTE_OFFSET, 0)
        struct.pack_into("<f", payload, area_circle.AREA_RADIUS_OFFSET, 8.0)
        arm = area_circle.NativeAreaTargetCircleController._arm
        state = first.state

    start = threading.Barrier(2)

    def submit(instance) -> bool:
        start.wait()
        try:
            arm(instance, payload)
        except RuntimeError:
            return False
        return True

    process.events.clear()
    with ThreadPoolExecutor(max_workers=2) as executor:
        accepted = list(executor.map(submit, (first, second)))

    assert sorted(accepted) == [False, True]
    assert process.u32(state) == 1
    assert sum(event[1] == state for event in process.events) == 2


@pytest.mark.parametrize("service", ("target", "area"))
def test_corrupt_mailbox_quarantines_without_overwrite_or_restore(
    service: str,
) -> None:
    process = FaultProcess()
    if service == "target":
        controller = target_router.NativeTargetCircleRetargeter(process)
        payload = _target_create_payload()
        corrupt_offset = (
            target_router.TARGET_STATE_MAGIC_OFFSET
            + len(target_router.TARGET_STATE_MAGIC)
        )
        arm = controller.arm
    else:
        controller = area_circle.NativeAreaTargetCircleController(
            process,
            owner_tag=area_circle.P1_OWNER_TAG,
            palette=0,
        )
        payload = bytearray(0x40)
        corrupt_offset = (
            area_circle.AREA_STATE_MAGIC_OFFSET
            + len(area_circle.AREA_STATE_MAGIC)
        )
        arm = controller._arm

    process.seed(controller.state + corrupt_offset, b"\xCC")
    process.events.clear()

    assert not controller.maintain()
    with pytest.raises(RuntimeError, match="still in flight"):
        arm(payload)
    assert not controller.restore()
    assert process.events == []


@pytest.mark.parametrize("service", ("target", "area"))
def test_terminal_state_without_matching_ack_never_releases_mailbox(
    service: str,
) -> None:
    process = FaultProcess()
    if service == "target":
        controller = target_router.NativeTargetCircleRetargeter(process)
        payload = _target_create_payload()
        arm = controller.arm
    else:
        controller = area_circle.NativeAreaTargetCircleController(
            process,
            owner_tag=area_circle.P1_OWNER_TAG,
            palette=0,
        )
        payload = bytearray(0x40)
        arm = controller._arm

    arm(payload)
    process.seed(controller.state, struct.pack("<I", 3))
    process.events.clear()

    assert not controller.maintain()
    with pytest.raises(RuntimeError, match="still in flight"):
        arm(payload)
    assert not controller.restore()
    assert process.events == []
