import struct


TREASURE_PRESENTATION_MODE_SITE = 0x0050FB3A
TREASURE_MODAL_MODE = bytes.fromhex("C7 06 00 00 01 00")
TREASURE_COMBAT_LOG_MODE = bytes.fromhex("C7 06 01 00 00 00")

TREASURE_ACTOR_ACTION_CALL_SITE = 0x0035588D
TREASURE_ACTOR_ACTION_ORIGINAL = bytes.fromhex("E8 FE 37 F1 FF")
TREASURE_INTERACTION_STATE = 0x0208CCF8
TREASURE_NO_ACTOR_CAVE_OFFSET = 0x1300
TREASURE_NO_ACTOR_CAVE_SIZE = 0x100

TREASURE_SOUND_CALL_SITE = 0x0050FBA8
TREASURE_SOUND_CALL_ORIGINAL = bytes.fromhex("E8 93 E3 E4 FF")
TREASURE_SOUND_NATIVE_HANDLER = 0x0035DF40
TREASURE_SOUND_CAVE_OFFSET = 0x1500
TREASURE_SOUND_CAVE_SIZE = 0x100
TREASURE_SOUND_STATE_OFFSET = 0xED04
TREASURE_SOUND_ID = 0x27


def _rel32(source: int, size: int, target: int) -> bytes:
    return struct.pack("<i", target - (source + size))


def build_no_actor_wrapper(cave: int) -> bytes:
    """Publish a completed interaction without submitting the actor action."""
    code = bytearray()
    code += b"\x48\xb8" + struct.pack("<Q", TREASURE_INTERACTION_STATE)
    code += b"\x48\xc7\x00\x01\x00\x00\x00"
    code += b"\x48\x89\x48\x08"
    code += b"\x48\x89\x50\x10"
    code += b"\x4c\x89\x40\x18"
    code += b"\xc7\x40\x20\x00\x00\x00\x00"
    code += b"\xc3"
    if len(code) > TREASURE_NO_ACTOR_CAVE_SIZE:
        raise RuntimeError("treasure no-actor wrapper exceeds its cave")
    return bytes(code)


def build_sound_wrapper(cave: int, sound_state: int) -> bytes:
    code = bytearray()
    code += b"\x48\xb8" + struct.pack("<Q", sound_state)
    code += b"\xc7\x00" + struct.pack("<I", TREASURE_SOUND_ID)
    jump_site = cave + len(code)
    code += b"\xe9" + _rel32(jump_site, 5, TREASURE_SOUND_NATIVE_HANDLER)
    if len(code) > TREASURE_SOUND_CAVE_SIZE:
        raise RuntimeError("treasure sound wrapper exceeds its cave")
    return bytes(code)


class NativeTreasureLog:
    def __init__(self, process, cave_base: int) -> None:
        self.process = process
        self.cave = int(cave_base) + TREASURE_NO_ACTOR_CAVE_OFFSET
        self.sound_cave = int(cave_base) + TREASURE_SOUND_CAVE_OFFSET
        self.sound_state = int(cave_base) + TREASURE_SOUND_STATE_OFFSET
        self._actor_patch = b"\xe8" + _rel32(
            TREASURE_ACTOR_ACTION_CALL_SITE,
            5,
            self.cave,
        )
        self._sound_patch = b"\xe8" + _rel32(
            TREASURE_SOUND_CALL_SITE,
            5,
            self.sound_cave,
        )
        self.active = False
        self.owned = False
        self._presentation_owned = False
        self._actor_action_owned = False
        self._sound_owned = False

    @staticmethod
    def _slot_image(code: bytes, size: int) -> bytes:
        code = bytes(code)
        if len(code) > int(size):
            raise RuntimeError("native treasure-log wrapper exceeds its cave")
        return code + b"\x00" * (int(size) - len(code))

    def _preflight_install(self) -> bool:
        """Validate the complete static topology before the first mutation.

        Return ``True`` only when an exact resident installation can be adopted.
        A mixed original/patched topology is deliberately not repaired live:
        another process may still be executing one of its caves.
        """

        actor_wrapper = build_no_actor_wrapper(self.cave)
        sound_wrapper = build_sound_wrapper(self.sound_cave, self.sound_state)
        presentation = self.process.read(
            TREASURE_PRESENTATION_MODE_SITE,
            len(TREASURE_MODAL_MODE),
        )
        actor = self.process.read(
            TREASURE_ACTOR_ACTION_CALL_SITE,
            len(TREASURE_ACTOR_ACTION_ORIGINAL),
        )
        sound = self.process.read(
            TREASURE_SOUND_CALL_SITE,
            len(TREASURE_SOUND_CALL_ORIGINAL),
        )

        if presentation not in (TREASURE_MODAL_MODE, TREASURE_COMBAT_LOG_MODE):
            raise RuntimeError(
                "treasure presentation code does not match Steam 1.0.4.0"
            )
        if actor not in (TREASURE_ACTOR_ACTION_ORIGINAL, self._actor_patch):
            raise RuntimeError(
                "treasure actor-action code does not match Steam 1.0.4.0"
            )
        if sound not in (TREASURE_SOUND_CALL_ORIGINAL, self._sound_patch):
            raise RuntimeError("treasure sound call does not match Steam 1.0.4.0")

        installed = (
            presentation == TREASURE_COMBAT_LOG_MODE,
            actor == self._actor_patch,
            sound == self._sound_patch,
        )
        if any(installed) and not all(installed):
            raise RuntimeError(
                "partial native treasure-log installation found; restart the game"
            )
        actor_image = self._slot_image(actor_wrapper, TREASURE_NO_ACTOR_CAVE_SIZE)
        sound_image = self._slot_image(sound_wrapper, TREASURE_SOUND_CAVE_SIZE)
        current_actor_cave = self.process.read(self.cave, len(actor_image))
        current_sound_cave = self.process.read(self.sound_cave, len(sound_image))
        if all(installed):
            if current_actor_cave != actor_image:
                raise RuntimeError("treasure no-actor wrapper is not intact")
            if current_sound_cave != sound_image:
                raise RuntimeError("treasure sound wrapper is not intact")
            return True

        # A stock root is fresh only while the complete reserved slots are
        # pristine.  Non-zero bytes with stock roots are an old/partial
        # process-lifetime topology and must never be overwritten live.
        if current_actor_cave != b"\x00" * len(actor_image):
            raise RuntimeError(
                "non-empty treasure no-actor cave with stock roots; restart the game"
            )
        if current_sound_cave != b"\x00" * len(sound_image):
            raise RuntimeError(
                "non-empty treasure sound cave with stock roots; restart the game"
            )
        return False

    def install(self) -> None:
        if self._preflight_install():
            self.active = True
            self.owned = False
            return

        actor_wrapper = build_no_actor_wrapper(self.cave)
        sound_wrapper = build_sound_wrapper(self.sound_cave, self.sound_state)
        try:
            # Both immutable caves are complete before any executable root can
            # reach them.  Presentation is the final feature commit point.
            self.process.write(self.cave, actor_wrapper)
            self.process.write(self.sound_cave, sound_wrapper)

            # Ownership is recorded before each patch: patch_code failure is
            # ambiguous after WriteProcessMemory/FlushInstructionCache.  No
            # later exception may roll a root back or rewrite either cave.
            self._actor_action_owned = True
            self.process.patch_code(
                TREASURE_ACTOR_ACTION_CALL_SITE,
                self._actor_patch,
            )
            self._sound_owned = True
            self.process.patch_code(TREASURE_SOUND_CALL_SITE, self._sound_patch)
            self._presentation_owned = True
            self.process.patch_code(
                TREASURE_PRESENTATION_MODE_SITE,
                TREASURE_COMBAT_LOG_MODE,
            )
        except BaseException:
            # Preserve a possibly reachable process-lifetime topology.  A later
            # helper observes the partial roots and requests a game restart;
            # it never attempts live repair.
            self.active = False
            self.owned = bool(
                self._presentation_owned
                or self._actor_action_owned
                or self._sound_owned
            )
            raise
        self.active = True
        self.owned = bool(
            self._presentation_owned or self._actor_action_owned or self._sound_owned
        )

    def restore(self) -> None:
        """Disarm dynamic state while leaving immutable code process-resident."""

        try:
            # This value is diagnostic/transient; clearing it cannot redirect
            # instruction flow.  Roots and caves remain byte-for-byte resident
            # so a replacement helper can adopt them read-only.
            self.process.write(self.sound_state, b"\x00" * 4)
        except OSError:
            pass
        finally:
            self.active = False
            self.owned = False
            self._presentation_owned = False
            self._actor_action_owned = False
            self._sound_owned = False
