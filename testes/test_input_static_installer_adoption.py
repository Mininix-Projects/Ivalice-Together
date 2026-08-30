import struct

import pytest

import coop_runtime as coop


MOVEMENT_ORIGINAL = bytes.fromhex("40 53 56 41 56")


class MemoryProcess:
    def __init__(self):
        self.regions: dict[int, bytearray] = {}
        self.events: list[tuple[str, int, bytes | int]] = []

    def seed(self, address: int, payload: bytes) -> None:
        self.regions[int(address)] = bytearray(payload)

    def _region(self, address: int, size: int):
        for base, payload in self.regions.items():
            offset = int(address) - base
            if 0 <= offset and offset + int(size) <= len(payload):
                return payload, offset
        return None

    def read(self, address: int, size: int) -> bytes:
        self.events.append(("read", int(address), int(size)))
        found = self._region(address, size)
        if found is None:
            return b"\x00" * int(size)
        payload, offset = found
        return bytes(payload[offset : offset + int(size)])

    def _write(self, address: int, payload: bytes) -> None:
        found = self._region(address, len(payload))
        if found is None:
            self.seed(address, payload)
            return
        region, offset = found
        region[offset : offset + len(payload)] = payload

    def write(self, address: int, payload: bytes) -> None:
        payload = bytes(payload)
        self.events.append(("write", int(address), payload))
        self._write(address, payload)

    def patch_code(self, address: int, payload: bytes) -> None:
        payload = bytes(payload)
        self.events.append(("patch", int(address), payload))
        self._write(address, payload)

    def mutations(self):
        return tuple(event for event in self.events if event[0] != "read")


def _specs():
    return {
        "movement": {
            "installer": coop.install_movement_override,
            "site": coop.MOVEMENT_HOOK_SITE,
            "original": MOVEMENT_ORIGINAL,
            "patch": b"\xe9"
            + coop.rel32(coop.MOVEMENT_HOOK_SITE, 5, coop.MOVEMENT_CAVE),
            "cave": coop.MOVEMENT_CAVE,
            "cave_size": (
                coop.ACTOR_PHYSICS_HELPER_OFFSET - coop.MOVEMENT_CAVE_OFFSET
            ),
            "fresh_writes": (coop.MOVEMENT_CAVE,),
        },
        "p1-sdl": {
            "installer": coop.install_p1_sdl_xinput_bridge,
            "site": coop.XINPUT_STATE_RESULT_SITE,
            "original": coop.XINPUT_STATE_RESULT_ORIGINAL,
            "patch": b"\xe9"
            + coop.rel32(
                coop.XINPUT_STATE_RESULT_SITE,
                5,
                coop.P1_SDL_XINPUT_CAVE,
            )
            + b"\x90\x90",
            "cave": coop.P1_SDL_XINPUT_CAVE,
            "cave_size": coop.P1_SDL_XINPUT_CAVE_SIZE,
            "fresh_writes": (coop.P1_SDL_XINPUT_STATE, coop.P1_SDL_XINPUT_CAVE),
        },
        "p2-p3-xinput": {
            "installer": coop.install_p2_device_isolation,
            "site": coop.XINPUT_DEVICE_FINALIZE_SITE,
            "original": coop.XINPUT_DEVICE_FINALIZE_ORIGINAL,
            "patch": b"\xe9"
            + coop.rel32(
                coop.XINPUT_DEVICE_FINALIZE_SITE,
                5,
                coop.P2_DEVICE_FILTER_CAVE,
            ),
            "cave": coop.P2_DEVICE_FILTER_CAVE,
            "cave_size": coop.P2_DEVICE_FILTER_CAVE_SIZE,
            "fresh_writes": (coop.P2_DEVICE_FILTER_STATE, coop.P2_DEVICE_FILTER_CAVE),
        },
        "directinput": {
            "installer": coop.install_dinput_gamepad_filter,
            "site": coop.DINPUT_GAMEPAD_FINALIZE_SITE,
            "original": coop.DINPUT_GAMEPAD_FINALIZE_ORIGINAL,
            "patch": b"\xe9"
            + coop.rel32(
                coop.DINPUT_GAMEPAD_FINALIZE_SITE,
                5,
                coop.DINPUT_GAMEPAD_FILTER_CAVE,
            )
            + b"\x90" * 3,
            "cave": coop.DINPUT_GAMEPAD_FILTER_CAVE,
            "cave_size": coop.DINPUT_GAMEPAD_FILTER_CAVE_SIZE,
            "fresh_writes": (coop.DINPUT_GAMEPAD_FILTER_CAVE,),
        },
        "steam-input": {
            "installer": coop.install_steam_gamepad_filter,
            "site": coop.STEAM_GAMEPAD_FINALIZE_SITE,
            "original": coop.STEAM_GAMEPAD_FINALIZE_ORIGINAL,
            "patch": b"\xe9"
            + coop.rel32(
                coop.STEAM_GAMEPAD_FINALIZE_SITE,
                5,
                coop.STEAM_GAMEPAD_FILTER_CAVE,
            )
            + b"\x90" * 3,
            "cave": coop.STEAM_GAMEPAD_FILTER_CAVE,
            "cave_size": coop.STEAM_GAMEPAD_FILTER_CAVE_SIZE,
            "fresh_writes": (coop.STEAM_GAMEPAD_FILTER_CAVE,),
        },
    }


@pytest.fixture(autouse=True)
def runtime_bases():
    coop.set_cave_base(0x5BAA0000)
    coop.set_dual_interaction_base(0x5C000000)


def _stock_process(spec) -> MemoryProcess:
    process = MemoryProcess()
    process.seed(spec["site"], spec["original"])
    process.seed(spec["cave"], b"\x00" * spec["cave_size"])
    return process


@pytest.mark.parametrize("name", tuple(_specs()))
def test_fresh_install_preflights_both_images_and_publishes_root_last(name):
    spec = _specs()[name]
    process = _stock_process(spec)

    assert spec["installer"](process) is True

    first_mutation = next(
        index for index, event in enumerate(process.events) if event[0] != "read"
    )
    preflight_reads = process.events[:first_mutation]
    assert [(kind, address) for kind, address, _value in preflight_reads] == [
        ("read", spec["site"]),
        ("read", spec["cave"]),
    ]
    mutations = process.mutations()
    assert [address for kind, address, _payload in mutations if kind == "write"] == list(
        spec["fresh_writes"]
    )
    assert mutations[-1] == ("patch", spec["site"], spec["patch"])
    assert not any(
        kind == "patch" and payload == spec["original"]
        for kind, _address, payload in mutations
    )


@pytest.mark.parametrize("name", tuple(_specs()))
def test_exact_resident_install_is_adopted_without_any_mutation(name):
    spec = _specs()[name]
    process = _stock_process(spec)
    assert spec["installer"](process) is True
    process.events.clear()

    assert spec["installer"](process) is False
    assert process.mutations() == ()


@pytest.mark.parametrize("name", tuple(_specs()))
def test_active_cave_mismatch_aborts_before_any_mutation(name):
    spec = _specs()[name]
    process = _stock_process(spec)
    assert spec["installer"](process) is True
    cave = process.regions[spec["cave"]]
    cave[0] ^= 0xFF
    process.events.clear()

    with pytest.raises(RuntimeError, match="active .* cave mismatch"):
        spec["installer"](process)
    assert process.mutations() == ()


@pytest.mark.parametrize("name", tuple(_specs()))
def test_nonempty_workspace_with_stock_root_aborts_before_any_mutation(name):
    spec = _specs()[name]
    process = _stock_process(spec)
    process.regions[spec["cave"]][0] = 0xCC
    process.events.clear()

    with pytest.raises(RuntimeError, match="non-empty .* cave with a stock root"):
        spec["installer"](process)
    assert process.mutations() == ()


@pytest.mark.parametrize("name", tuple(_specs()))
def test_unknown_root_aborts_before_any_mutation(name):
    spec = _specs()[name]
    process = _stock_process(spec)
    process.seed(spec["site"], b"\xCC" * len(spec["original"]))
    process.events.clear()

    with pytest.raises(RuntimeError, match="unexpected bytes"):
        spec["installer"](process)
    assert process.mutations() == ()


def test_legacy_movement_root_aborts_before_reading_or_mutating_the_new_cave():
    spec = _specs()["movement"]
    process = _stock_process(spec)
    legacy_cave = coop.COMMAND_BLOCK - 0x200 + 0x100
    legacy_patch = b"\xe9" + coop.rel32(coop.MOVEMENT_HOOK_SITE, 5, legacy_cave)
    process.seed(coop.MOVEMENT_HOOK_SITE, legacy_patch)
    process.events.clear()

    with pytest.raises(RuntimeError, match="legacy movement hook"):
        coop.install_movement_override(process)
    assert process.events == [
        ("read", coop.MOVEMENT_HOOK_SITE, len(MOVEMENT_ORIGINAL))
    ]


def test_p2_device_state_starts_disarmed_before_the_detour_is_published():
    spec = _specs()["p2-p3-xinput"]
    process = _stock_process(spec)

    assert coop.install_p2_device_isolation(process) is True
    state_write = next(
        payload
        for kind, address, payload in process.mutations()
        if kind == "write" and address == coop.P2_DEVICE_FILTER_STATE
    )
    assert state_write[:4] == b"\x00" * 4
    assert struct.unpack_from(
        "<I", state_write, coop.P1_NATIVE_CONTROLLER_SLOT_OFFSET
    )[0] == coop.INVALID_NATIVE_CONTROLLER_SLOT
