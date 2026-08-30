from __future__ import annotations

import struct

import coop_runtime as coop


REMOVE_PARTY_MEMBER = 0x328B20
SCRIPT_REMOVE_CALL = 0x34CE31
SCRIPT_REMOVE_RETURN = 0x34CE36
SCRIPT_REMOVE_CALL_ORIGINAL = b"\xe8\xea\xbc\xfd\xff"


MEMBER_COUNTER_COUNT = 16
COUNTER_BYTES = MEMBER_COUNTER_COUNT * 4
STATE_MAGIC_OFFSET = COUNTER_BYTES
STATE_MAGIC = b"SRJ1"
DEFAULT_STATE_OFFSET = 0x500
DEFAULT_CAVE_OFFSET = 0x600
STATE_REGION_SIZE = DEFAULT_CAVE_OFFSET - DEFAULT_STATE_OFFSET
CAVE_REGION_SIZE = 0x100


def build_story_removal_cave(cave: int, state: int) -> bytes:
    del cave
    code = bytearray()
    code += b"\x48\xb8" + struct.pack("<Q", state)

    code += b"\x83\xf9\x10"
    code += b"\x73\x04"

    code += b"\xf0\xff\x04\x88"
    code += b"\x48\xb8" + struct.pack("<Q", REMOVE_PARTY_MEMBER)
    code += b"\xff\xe0"
    return bytes(code)


def build_story_removal_patch(cave: int) -> bytes:
    return b"\xe8" + coop.rel32(SCRIPT_REMOVE_CALL, 5, cave)


def build_story_removal_state_image() -> bytes:
    image = b"\x00" * STATE_MAGIC_OFFSET + STATE_MAGIC
    if len(image) > STATE_REGION_SIZE:
        raise RuntimeError("story-removal state exceeds its reserved region")
    return image + b"\x00" * (STATE_REGION_SIZE - len(image))


def build_story_removal_cave_image(cave: int, state: int) -> bytes:
    code = build_story_removal_cave(cave, state)
    if len(code) > CAVE_REGION_SIZE:
        raise RuntimeError("story-removal cave exceeds its reserved region")
    return code + b"\x00" * (CAVE_REGION_SIZE - len(code))


def decode_story_removal_hook(
    process: coop.RemoteProcess,
    patch: bytes,
) -> tuple[int, int] | None:
    if len(patch) != 5 or patch[:1] != b"\xe8":
        return None
    cave = SCRIPT_REMOVE_CALL + 5 + struct.unpack_from("<i", patch, 1)[0]
    try:
        prefix = process.read(cave, 10)
        if prefix[:2] != b"\x48\xb8":
            return None
        state = struct.unpack_from("<Q", prefix, 2)[0]
        expected = build_story_removal_cave_image(cave, state)
        if process.read(cave, CAVE_REGION_SIZE) != expected:
            return None
        state_tail = process.read(
            state + STATE_MAGIC_OFFSET,
            STATE_REGION_SIZE - STATE_MAGIC_OFFSET,
        )
        if (
            state_tail[: len(STATE_MAGIC)] != STATE_MAGIC
            or any(state_tail[len(STATE_MAGIC) :])
        ):
            return None
    except (OSError, struct.error):
        return None
    return cave, state


def changed_members(
    previous: tuple[int, ...],
    current: tuple[int, ...],
) -> tuple[int, ...]:
    if len(previous) != MEMBER_COUNTER_COUNT or len(current) != MEMBER_COUNTER_COUNT:
        raise ValueError("story-removal snapshots must contain sixteen counters")
    return tuple(
        member_id
        for member_id, (before, after) in enumerate(zip(previous, current))
        if ((int(after) - int(before)) & 0xFFFFFFFF) != 0
    )


class StoryRemovalJournal:
    def __init__(
        self,
        process: coop.RemoteProcess,
        workspace: int,
    ) -> None:
        self.process = process
        self.state = int(workspace) + DEFAULT_STATE_OFFSET
        self.cave = int(workspace) + DEFAULT_CAVE_OFFSET
        current = process.read(SCRIPT_REMOVE_CALL, 5)
        resident = decode_story_removal_hook(process, current)
        if current == SCRIPT_REMOVE_CALL_ORIGINAL:
            state_image = build_story_removal_state_image()
            cave_image = build_story_removal_cave_image(self.cave, self.state)
            empty_state = b"\x00" * STATE_REGION_SIZE
            empty_cave = b"\x00" * CAVE_REGION_SIZE
            current_state = process.read(self.state, STATE_REGION_SIZE)
            current_cave = process.read(self.cave, CAVE_REGION_SIZE)
            if current_state not in (empty_state, state_image):
                raise RuntimeError(
                    "story-removal state is neither empty nor exactly prepared; "
                    "restart the game"
                )
            if current_cave not in (empty_cave, cave_image):
                raise RuntimeError(
                    "story-removal cave is neither empty nor exactly prepared; "
                    "restart the game"
                )
            self.owns_patch = True
            if current_state != state_image:
                process.write(self.state, state_image)
            if process.read(self.state, STATE_REGION_SIZE) != state_image:
                raise RuntimeError("story-removal state failed to prepare")
            if current_cave != cave_image:
                process.write(self.cave, cave_image)
            if process.read(self.cave, CAVE_REGION_SIZE) != cave_image:
                raise RuntimeError("story-removal cave failed to prepare")
            self.patch = build_story_removal_patch(self.cave)
            if process.read(SCRIPT_REMOVE_CALL, 5) != SCRIPT_REMOVE_CALL_ORIGINAL:
                raise RuntimeError(
                    "story-removal root changed before commit; restart the game"
                )
            process.patch_code(SCRIPT_REMOVE_CALL, self.patch)
            if process.read(SCRIPT_REMOVE_CALL, 5) != self.patch:
                raise RuntimeError("story-removal root failed to commit")
        elif resident is not None:
            self.owns_patch = False
            if resident != (self.cave, self.state):
                raise RuntimeError(
                    "active story-removal hook escaped its reserved workspace"
                )
            self.patch = current
        else:
            raise RuntimeError(
                "story removePartyMember call is already patched by another component"
            )
        self.installed = True

    def snapshot(self) -> tuple[int, ...]:
        return struct.unpack(
            f"<{MEMBER_COUNTER_COUNT}I",
            self.process.read(self.state, COUNTER_BYTES),
        )

    def restore(self) -> None:
        if not self.installed:
            return
        # The cave has no dynamic policy: it only increments counters and then
        # tail-calls the stock function.  Leaving the exact root + immutable
        # cave resident is the only race-free shutdown.  Removing the CALL can
        # never prove that another game thread has already left the cave.
        self.installed = False
        self.owns_patch = False


__all__ = [
    "SCRIPT_REMOVE_CALL",
    "SCRIPT_REMOVE_CALL_ORIGINAL",
    "StoryRemovalJournal",
    "build_story_removal_cave",
    "build_story_removal_cave_image",
    "build_story_removal_patch",
    "build_story_removal_state_image",
    "changed_members",
    "decode_story_removal_hook",
]
