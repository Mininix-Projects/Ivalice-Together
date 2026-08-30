from __future__ import annotations

import ctypes as ct
from ctypes import wintypes as wt
import struct
import threading
import time

import coop_roster
import coop_runtime as coop
import helper_lease
import party_intent_channel
import runtime_shutdown
import story_removal_journal


BATTLE_INFO_GLOBAL = 0x2EBF190
CONTROLLED_HANDLE_GLOBAL = 0x22C7FE0
CONTROLLED_ROOT_GLOBAL = 0x2099D78
NATIVE_MENU_STATE = 0x2092730
LOCATION_SAVE_GLOBAL = 0x2092758
LOCATION_CURRENT_OFFSET = 0x1244
LOCATION_UI_OFFSET = 0x125C
LOCATION_FLAGS_OFFSET = 0x1262
LOCATION_TOWN_FLAG = 0x01
USER_CONTROL_GLOBAL = 0x2099D70
USER_CONTROL_ENABLED_OFFSET = 0x14
ADD_PARTY_MEMBER = 0x3172E0
REMOVE_PARTY_MEMBER = 0x328B20
PARTY_ALL_READ = 0x37C980

REMOVE_PARTY_MEMBER_ORIGINAL = b"\x48\x83\xec\x28\x0f\xb6\xd1"
REMOTE_CALL_CODE_OFFSET = 0x000
REMOTE_CALL_CODE_CAPACITY = 0x100
REMOTE_CALL_RESULT_OFFSET = 0x100
REMOTE_CALL_RESULT_SIZE = 0x08
REMOTE_CALL_REGION_SIZE = 0x200
REMOTE_CALL_CLAIM_OFFSET = 0x108
REMOTE_CALL_CLAIM_MAGIC = b"PCR2"
REMOTE_CALL_REGION_OFFSETS = (0x000, 0x700, 0x900, 0xB00, 0xD00)
REMOVE_FILTER_STATE_OFFSET = 0x200
REMOVE_FILTER_STATE_CAPACITY = 0x100
REMOVE_FILTER_CAVE_OFFSET = 0x300


REMOVE_FILTER_CAVE_CAPACITY = 0x200
MAX_REMOVE_FILTER_MEMBERS = 5

assert REMOTE_CALL_CODE_OFFSET + REMOTE_CALL_CODE_CAPACITY <= REMOTE_CALL_RESULT_OFFSET
assert REMOTE_CALL_RESULT_OFFSET + REMOTE_CALL_RESULT_SIZE <= REMOTE_CALL_REGION_SIZE
assert REMOTE_CALL_REGION_SIZE <= REMOVE_FILTER_STATE_OFFSET
assert REMOVE_FILTER_STATE_OFFSET + REMOVE_FILTER_STATE_CAPACITY <= REMOVE_FILTER_CAVE_OFFSET
assert (
    REMOVE_FILTER_CAVE_OFFSET + REMOVE_FILTER_CAVE_CAPACITY
    <= story_removal_journal.DEFAULT_STATE_OFFSET
)
assert (
    story_removal_journal.DEFAULT_CAVE_OFFSET
    + story_removal_journal.CAVE_REGION_SIZE
    <= REMOTE_CALL_REGION_OFFSETS[1]
)
assert all(
    left + REMOTE_CALL_REGION_SIZE <= right
    for left, right in zip(REMOTE_CALL_REGION_OFFSETS, REMOTE_CALL_REGION_OFFSETS[1:])
)
assert (
    REMOTE_CALL_REGION_OFFSETS[-1] + REMOTE_CALL_REGION_SIZE
    <= coop.PARTY_GUARD_WORKSPACE_SIZE
)

PARTY_MEMBER_OFFSET = 0x08
PARTY_MEMBER_STRIDE = 0x1C8
PARTY_LAYOUT_OFFSET = 0x5A48
PARTY_LAYOUT_STRIDE = 0x12
PARTY_LEADER_ID_OFFSET = 0x5AA4
DEFAULT_LAYOUT = 0
CURRENT_LAYOUT = 1
UI_LAYOUT = 2
MENU_LAYOUT = 3
PARTY_LAYOUT_COUNT = 5
LIVE_LAYOUT_IDS = (
    DEFAULT_LAYOUT,
    CURRENT_LAYOUT,
    UI_LAYOUT,
    MENU_LAYOUT,
)
ACTIVE_STORAGE_SLOTS = 4
COOP_ACTIVE_SLOTS = 3


BLOCK_NATIVE_STORY_REMOVALS = False

VAAN_ID = 0
PENELO_ID = 5
ASHE_ID = 1
BALTHIER_ID = 3
MAIN_MEMBER_IDS = frozenset(range(6))


def _protected_member_ids() -> tuple[int, ...]:
    try:
        return coop_roster.initial_members()
    except (OSError, TypeError, ValueError):
        return ()


PROTECTED_MEMBER_IDS = _protected_member_ids()
REKS_ID = 6
EMPTY = 0xFFFF
IN_PARTY_FLAG = 0x10
JOINED_ONCE_FLAG = 0x08
POLL_SECONDS = 0.20
TRANSIENT_LOG_INTERVAL_SECONDS = 30.0
REPAIR_DEBOUNCE_SECONDS = 1.25
POST_REPAIR_COOLDOWN_SECONDS = 4.0
REMOTE_CALL_WAIT_SLICE_MS = 250
REMOTE_CALL_MAX_WAIT_SECONDS = 30.0
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF

kernel32 = ct.WinDLL("kernel32", use_last_error=True)
kernel32.CreateRemoteThread.argtypes = [
    wt.HANDLE,
    ct.c_void_p,
    ct.c_size_t,
    ct.c_void_p,
    ct.c_void_p,
    wt.DWORD,
    ct.POINTER(wt.DWORD),
]
kernel32.CreateRemoteThread.restype = wt.HANDLE
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.WaitForSingleObject.restype = wt.DWORD
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL


def report_transient_read(
    error: BaseException,
    *,
    now: float,
    next_log_at: float,
) -> float:
    """Report persistent loading races without producing a per-tick log."""

    if now < next_log_at:
        return next_log_at
    try:
        description = str(error)
    except BaseException:
        description = "<unprintable>"
    try:
        print(
            "PENELO_GUARD transient read: "
            f"{type(error).__name__}: {description}",
            flush=True,
        )
    except BaseException:
        # Logging is diagnostic only and cannot own the helper lifetime.
        pass
    return now + TRANSIENT_LOG_INTERVAL_SECONDS


def unpack_layout(data: bytes) -> tuple[int, ...]:
    if len(data) != PARTY_LAYOUT_STRIDE:
        raise ValueError("party layout must contain exactly nine u16 slots")
    return struct.unpack("<9H", data)


def pack_layout(slots: tuple[int, ...] | list[int]) -> bytes:
    if len(slots) != 9:
        raise ValueError("party layout must contain exactly nine slots")
    return struct.pack("<9H", *slots)


def normalize_coop_roster(
    slots: tuple[int, ...],
    *,
    prefer_active: bool = True,
) -> tuple[int, ...]:
    return coop_roster.normalize_live_party_layout(
        slots,
        PROTECTED_MEMBER_IDS,
        empty=EMPTY,
        prefer_active=prefer_active,
    )


def layout_has_single_protected_members(slots: tuple[int, ...]) -> bool:
    return all(slots.count(member_id) == 1 for member_id in PROTECTED_MEMBER_IDS)


def layout_has_unique_members(slots: tuple[int, ...]) -> bool:
    members = [value for value in slots if value != EMPTY]
    return len(members) == len(set(members))


def layout_has_no_guest_in_selectable_trio(slots: tuple[int, ...]) -> bool:
    return all(
        member_id == EMPTY or member_id in range(6)
        for member_id in slots[:COOP_ACTIVE_SLOTS]
    )


def active_main_members(slots: tuple[int, ...]) -> tuple[int, ...] | None:
    members = tuple(member_id for member_id in slots[:3] if member_id != EMPTY)
    if (
        not 1 <= len(members) <= 3
        or len(set(members)) != len(members)
        or any(member_id not in MAIN_MEMBER_IDS for member_id in members)
    ):
        return None
    return members


def coherent_active_main_members(
    layouts: tuple[tuple[int, ...], ...],
) -> tuple[int, ...] | None:
    if len(layouts) < PARTY_LAYOUT_COUNT:
        return None
    formations = tuple(
        active_main_members(layouts[layout_id]) for layout_id in LIVE_LAYOUT_IDS
    )
    if any(formation is None for formation in formations):
        return None
    expected = set(formations[0])
    if any(set(formation) != expected for formation in formations[1:]):
        return None
    return formations[0]


def apply_active_main_members(
    slots: tuple[int, ...],
    active_members: tuple[int, ...],
) -> tuple[int, ...]:
    if len(slots) != 9:
        raise ValueError("party layout must contain exactly nine slots")
    try:
        accepted = party_intent_channel.normalize_active_members(active_members)
    except (TypeError, ValueError):
        return slots
    concrete = [member_id for member_id in slots if member_id != EMPTY]
    if len(concrete) != len(set(concrete)):
        return slots
    if any(member_id not in slots or slots[3] == member_id for member_id in accepted):
        return slots

    result = list(slots)

    for target_index, member_id in enumerate(accepted):
        if result[target_index] == member_id:
            continue
        source_index = result.index(member_id)
        if source_index == 3:
            return slots
        result[target_index], result[source_index] = (
            result[source_index],
            result[target_index],
        )

    for active_index in range(len(accepted), 3):
        if result[active_index] == EMPTY:
            continue
        reserve_index = next(
            (index for index in range(4, 9) if result[index] == EMPTY),
            None,
        )
        if reserve_index is None:
            return slots
        result[reserve_index] = result[active_index]
        result[active_index] = EMPTY
    return tuple(result)


class RemoteCaller:
    def __init__(self, process: coop.RemoteProcess, workspace: int) -> None:
        self.process = process
        self.workspace = int(workspace)
        if self.workspace <= 0:
            raise ValueError("party guard workspace must be positive")
        # Serialise calls with close().  In particular, close must never mark
        # the shared workspace reusable while native code may still be reading
        # its call stub or writing its result.
        self._lifetime_lock = threading.Lock()
        self.call_workspace = self._claim_call_workspace()

    def _claim_call_workspace(self) -> int:
        empty = b"\x00" * REMOTE_CALL_REGION_SIZE
        expected = bytearray(empty)
        expected[
            REMOTE_CALL_CLAIM_OFFSET : REMOTE_CALL_CLAIM_OFFSET
            + len(REMOTE_CALL_CLAIM_MAGIC)
        ] = REMOTE_CALL_CLAIM_MAGIC
        for offset in REMOTE_CALL_REGION_OFFSETS:
            candidate = self.workspace + offset
            if self.process.read(candidate, REMOTE_CALL_REGION_SIZE) != empty:
                continue
            self.process.write(
                candidate + REMOTE_CALL_CLAIM_OFFSET,
                REMOTE_CALL_CLAIM_MAGIC,
            )
            if self.process.read(candidate, REMOTE_CALL_REGION_SIZE) != bytes(expected):
                raise RuntimeError(
                    "party guard remote call slot changed while being claimed; "
                    "restart the game"
                )
            return candidate
        raise RuntimeError(
            "all party guard remote call slots are quarantined; restart the game"
        )

    def close(self) -> None:
        with self._lifetime_lock:
            # call() retains this lock until the remote thread is signalled or
            # the game process itself is signalled.  The slice belongs to the
            # shared rel32 allocation and remains valid until process exit.
            call_workspace = self.call_workspace
            self.call_workspace = 0
            self.workspace = 0
            if call_workspace:
                try:
                    self.process.write(
                        call_workspace,
                        b"\x00" * REMOTE_CALL_REGION_SIZE,
                    )
                except BaseException:
                    # A failed/partial clear remains non-zero and is therefore
                    # quarantined by the next helper instead of being reused.
                    pass

    def _wait_for_thread(self, thread) -> bool | None:
        """Wait for completion, exit, or an unprovable wait result.

        A timeout is only a polling interval.  It must never transfer ownership
        of the workspace back to Python while the remote instruction pointer
        can still refer to it.  ``None`` tells call() to quarantine the mutable
        slot permanently instead of hanging the helper or reusing uncertain
        native storage.
        """

        deadline = time.monotonic() + REMOTE_CALL_MAX_WAIT_SECONDS
        while True:
            wait_result = kernel32.WaitForSingleObject(
                thread,
                REMOTE_CALL_WAIT_SLICE_MS,
            )
            if wait_result == WAIT_OBJECT_0:
                return True

            process_wait = kernel32.WaitForSingleObject(self.process.handle, 0)
            if process_wait == WAIT_OBJECT_0:
                return False
            if wait_result == WAIT_TIMEOUT:
                if time.monotonic() >= deadline:
                    # The thread can outlive this Python helper safely only if
                    # its immutable call slice is never recycled.  Bounding
                    # the wait prevents a native loading lock from wedging the
                    # party guard forever; call() performs that quarantine.
                    return None
                continue

            # WAIT_FAILED or another unexpected status provides no evidence
            # that the remote thread stopped.  Closing its kernel handle is
            # safe, but the call slot itself must remain immutable forever.
            return None

    def call(self, function: int, rcx: int = 0, rdx: int = 0) -> int:
        with self._lifetime_lock:
            if self.workspace <= 0 or self.call_workspace <= 0:
                raise RuntimeError("party guard remote caller is closed")
            code_address = self.call_workspace + REMOTE_CALL_CODE_OFFSET
            result_address = self.call_workspace + REMOTE_CALL_RESULT_OFFSET
            code = bytearray(b"\x48\x83\xec\x28")
            code += b"\x48\xb9" + struct.pack("<Q", rcx)
            code += b"\x48\xba" + struct.pack("<Q", rdx)
            code += b"\x48\xb8" + struct.pack("<Q", function)
            code += b"\xff\xd0"
            code += b"\x48\xba" + struct.pack("<Q", result_address)
            code += b"\x48\x89\x02"
            code += b"\x48\x83\xc4\x28\x33\xc0\xc3"
            if len(code) > REMOTE_CALL_CODE_CAPACITY:
                raise RuntimeError("party guard remote call stub exceeds its slot")
            self.process.write(code_address, code)
            self.process.write(result_address, b"\x00" * REMOTE_CALL_RESULT_SIZE)

            thread_id = wt.DWORD()
            thread = kernel32.CreateRemoteThread(
                self.process.handle,
                None,
                0,
                ct.c_void_p(code_address),
                None,
                0,
                ct.byref(thread_id),
            )
            if not thread:
                raise OSError(ct.get_last_error(), "CreateRemoteThread party guard")
            deferred_error: BaseException | None = None
            try:
                while True:
                    try:
                        completed = self._wait_for_thread(thread)
                        break
                    except BaseException as error:
                        # A Python interruption is not evidence that the native
                        # thread stopped.  Defer it until the thread (or game)
                        # is signalled so close/restart cannot reuse this slice
                        # while RIP may still point into its mutable stub.
                        if deferred_error is None:
                            deferred_error = error
            finally:
                kernel32.CloseHandle(thread)
            if completed is None:
                # Do not clear this slot in close(): RIP may still refer to its
                # stub.  A restarted helper claims another reserved slice.
                self.call_workspace = 0
                self.workspace = 0
            if deferred_error is not None:
                raise deferred_error
            if completed is None:
                raise RuntimeError(
                    "party guard remote thread wait became unprovable; "
                    "call slot quarantined"
                )
            if not completed:
                raise RuntimeError("game exited during party native call")
            return self.process.u64(result_address)


def build_penelo_remove_filter(
    cave: int,
    enabled_address: int,
    protected_member_ids: tuple[int, ...] | None = None,
) -> bytes:
    if protected_member_ids is None:
        protected_member_ids = PROTECTED_MEMBER_IDS
    raw_member_ids = tuple(protected_member_ids)
    if any(
        not isinstance(member_id, int) or member_id < 0 or member_id > 0xFF
        for member_id in raw_member_ids
    ):
        raise ValueError("party member IDs must fit in one byte")
    member_ids = tuple(dict.fromkeys(raw_member_ids))
    if not member_ids or len(member_ids) > MAX_REMOVE_FILTER_MEMBERS:
        raise ValueError("remove filter supports one to five co-op companions")
    if any(
        member_id not in coop_roster.SELECTABLE_MEMBER_IDS for member_id in member_ids
    ):
        raise ValueError("remove filter supports only permanent non-Vaan members")

    code = bytearray()
    code += b"\x48\xb8" + struct.pack("<Q", enabled_address)
    code += b"\x80\x38\x01"
    comparison_size = 5 * len(member_ids)
    code += b"\x75" + bytes((comparison_size + 3,))
    for index, member_id in enumerate(member_ids):
        code += b"\x80\xf9" + bytes((member_id,))
        if index + 1 < len(member_ids):
            remaining_comparisons = 5 * (len(member_ids) - index - 1)
            code += b"\x74" + bytes((remaining_comparisons,))
        else:
            code += b"\x75\x03"
    code += b"\x31\xc0\xc3"
    code += REMOVE_PARTY_MEMBER_ORIGINAL
    jump_site = cave + len(code)
    code += b"\xe9" + coop.rel32(jump_site, 5, REMOVE_PARTY_MEMBER + 7)
    if len(code) > REMOVE_FILTER_CAVE_CAPACITY:
        raise RuntimeError("remove filter exceeds its reserved code cave")
    return bytes(code)


def decode_penelo_remove_filter(
    process: coop.RemoteProcess, patch: bytes
) -> tuple[int, int] | None:
    if (
        len(patch) != len(REMOVE_PARTY_MEMBER_ORIGINAL)
        or patch[:1] != b"\xe9"
        or patch[5:] != b"\x90" * (len(patch) - 5)
    ):
        return None
    cave = REMOVE_PARTY_MEMBER + 5 + struct.unpack_from("<i", patch, 1)[0]
    try:
        header = process.read(cave, 15)
        if (
            len(header) != 15
            or header[:2] != b"\x48\xb8"
            or header[10:14] != b"\x80\x38\x01\x75"
        ):
            return None
        state = struct.unpack_from("<Q", header, 2)[0]
        if state != cave - (REMOVE_FILTER_CAVE_OFFSET - REMOVE_FILTER_STATE_OFFSET):
            return None
        comparison_bytes = header[14] - 3
        if comparison_bytes < 5 or comparison_bytes % 5:
            return None
        member_count = comparison_bytes // 5
        if member_count > MAX_REMOVE_FILTER_MEMBERS:
            return None

        code_size = 30 + member_count * 5
        resident = process.read(cave, code_size)
        if len(resident) != code_size:
            return None
        member_ids = tuple(
            resident[17 + member_index * 5] for member_index in range(member_count)
        )
        expected = build_penelo_remove_filter(cave, state, member_ids)
        if resident != expected:
            return None
    except (IndexError, OSError, RuntimeError, ValueError, struct.error):
        return None
    return cave, state


class PeneloRemovalFilter:
    def __init__(self, process: coop.RemoteProcess, workspace: int) -> None:
        self.process = process
        self.state = workspace + REMOVE_FILTER_STATE_OFFSET
        self.cave = workspace + REMOVE_FILTER_CAVE_OFFSET
        self.patch = b""
        self.owns_patch = False
        current = process.read(REMOVE_PARTY_MEMBER, len(REMOVE_PARTY_MEMBER_ORIGINAL))
        resident = decode_penelo_remove_filter(process, current)
        if current == REMOVE_PARTY_MEMBER_ORIGINAL:
            self.patch = b"\xe9" + coop.rel32(REMOVE_PARTY_MEMBER, 5, self.cave)
            self.patch += b"\x90" * (
                len(REMOVE_PARTY_MEMBER_ORIGINAL) - len(self.patch)
            )
            code = build_penelo_remove_filter(self.cave, self.state)
            image = code + b"\x00" * (REMOVE_FILTER_CAVE_CAPACITY - len(code))
            empty_state = b"\x00" * REMOVE_FILTER_STATE_CAPACITY
            empty_cave = b"\x00" * REMOVE_FILTER_CAVE_CAPACITY
            current_state = process.read(self.state, REMOVE_FILTER_STATE_CAPACITY)
            current_cave = process.read(self.cave, REMOVE_FILTER_CAVE_CAPACITY)
            if current_state != empty_state:
                raise RuntimeError(
                    "non-empty Penelo removal filter state with a stock root; "
                    "restart the game"
                )
            if current_cave not in (empty_cave, image):
                raise RuntimeError(
                    "non-empty Penelo removal filter cave with a stock root; "
                    "restart the game"
                )
            if current_cave != image:
                process.write(self.cave, image)
            if process.read(self.cave, REMOVE_FILTER_CAVE_CAPACITY) != image:
                raise RuntimeError("Penelo removal filter cave failed to prepare")
            if process.read(self.state, REMOVE_FILTER_STATE_CAPACITY) != empty_state:
                raise RuntimeError(
                    "Penelo removal filter state changed before root commit; "
                    "restart the game"
                )
            if (
                process.read(
                    REMOVE_PARTY_MEMBER,
                    len(REMOVE_PARTY_MEMBER_ORIGINAL),
                )
                != REMOVE_PARTY_MEMBER_ORIGINAL
            ):
                raise RuntimeError(
                    "removePartyMember root changed before commit; restart the game"
                )
            process.patch_code(REMOVE_PARTY_MEMBER, self.patch)
            if process.read(REMOVE_PARTY_MEMBER, len(self.patch)) != self.patch:
                raise RuntimeError("Penelo removal filter root failed to commit")
            self.owns_patch = True
        elif resident is not None:
            if resident != (self.cave, self.state):
                raise RuntimeError(
                    "active Penelo removal filter escaped its reserved workspace"
                )
            self.patch = current
            expected = build_penelo_remove_filter(self.cave, self.state)
            expected_image = expected + b"\x00" * (
                REMOVE_FILTER_CAVE_CAPACITY - len(expected)
            )
            if (
                process.read(self.cave, REMOVE_FILTER_CAVE_CAPACITY)
                != expected_image
            ):
                raise RuntimeError("active Penelo removal filter cave mismatch")
            state_image = process.read(self.state, REMOVE_FILTER_STATE_CAPACITY)
            if (
                len(state_image) != REMOVE_FILTER_STATE_CAPACITY
                or state_image[0] not in (0, 1)
                or any(state_image[1:])
            ):
                raise RuntimeError("active Penelo removal filter state mismatch")

        else:
            raise RuntimeError(
                "removePartyMember is already patched by another component"
            )
        self.installed = True

    def set_enabled(self, enabled: bool) -> None:
        if self.installed:
            self.process.write(self.state, bytes((1 if enabled else 0,)))

    def restore(self) -> None:
        if not self.installed:
            return
        try:
            # Disabled state is the exact native pass-through.  Keep the root
            # and immutable cave resident for the game process lifetime; a
            # replacement guard can then adopt them without rewriting code that
            # another game thread may still be executing.
            self.process.write(self.state, b"\x00")
        except OSError:
            pass
        self.installed = False
        self.owns_patch = False


class PeneloStoryGuard:
    def __init__(
        self,
        process: coop.RemoteProcess,
        *,
        game_pid: int | None = None,
    ) -> None:
        self.process = process
        if process.read(ADD_PARTY_MEMBER, 5) != b"\x48\x89\x5c\x24\x08":
            raise RuntimeError("unexpected addPartyMember build")
        if process.read(PARTY_ALL_READ, 4) != b"\x48\x83\xec\x28":
            raise RuntimeError("unexpected partyallread build")
        damage_base = process.ensure_damage_number_theme_allocation()
        self.caller = RemoteCaller(
            process,
            damage_base + coop.PARTY_GUARD_WORKSPACE_OFFSET,
        )
        try:
            self.removal_filter = PeneloRemovalFilter(process, self.caller.workspace)
            self.story_removal_journal = story_removal_journal.StoryRemovalJournal(
                process, self.caller.workspace
            )
            # Executable adoption is read-only, but a dead helper may have left
            # the dynamic policy armed.  This instance starts disarmed and owns
            # the lease, so fail open before observing any new session state.
            self.removal_filter.set_enabled(False)
        except Exception:
            # Constructor rollback is independent and preserves the original
            # structural error.  Close the mutable native-call slot before
            # disarming either process-lifetime static consumer.
            for component, method in (
                (self.caller, "close"),
                (getattr(self, "removal_filter", None), "restore"),
                (getattr(self, "story_removal_journal", None), "restore"),
            ):
                operation = getattr(component, method, None)
                if callable(operation):
                    try:
                        operation()
                    except BaseException:
                        pass
            raise
        try:
            self.game_pid = int(game_pid) if game_pid is not None else 0
            self.intent_path = (
                party_intent_channel.channel_path(self.game_pid)
                if self.game_pid > 0
                else None
            )
            initial_intent = (
                party_intent_channel.read_party_intent(
                    self.intent_path,
                    game_pid=self.game_pid,
                )
                if self.intent_path is not None
                else None
            )

            self.last_party_intent_token = (
                initial_intent.token if initial_intent is not None else None
            )
            self.story_removal_counters = self.story_removal_journal.snapshot()
            self.accepted_active_members: tuple[int, ...] | None = None
            self.pending_story_restore: set[int] = set()
            self.story_removal_watches: dict[int, float] = {}
            self.last_active_anomaly: tuple[int, ...] | None = None
            self.armed = False
            self.bad_signature = None
            self.bad_since = 0.0
            self.cooldown_until = 0.0
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        # Stop/await native calls first.  A concurrent repair must finish before
        # its mutable call slot is recycled or the removal pass-through changes.
        failures = []
        for label, operation in (
            ("caller", self.caller.close),
            ("filter", self.removal_filter.restore),
            ("journal", self.story_removal_journal.restore),
        ):
            try:
                operation()
            except BaseException as error:
                failures.append(f"{label}:{type(error).__name__}")
        if failures:
            try:
                print(
                    "PENELO_GUARD cleanup partial " + ",".join(failures),
                    flush=True,
                )
            except BaseException:
                pass

    def disarm_session(self, reason: str) -> None:
        if self.armed:
            self.removal_filter.set_enabled(False)
            print(f"PENELO_GUARD disarmed: {reason}", flush=True)
        self.armed = False
        self.bad_signature = None
        self.bad_since = 0.0
        self.cooldown_until = 0.0
        self.accepted_active_members = None
        self.pending_story_restore.clear()
        self.story_removal_watches.clear()
        self.last_active_anomaly = None
        try:
            self.story_removal_counters = self.story_removal_journal.snapshot()
        except OSError:
            pass

    @staticmethod
    def is_early_reks_generation(
        penelo_flags: int, default_layout: tuple[int, ...]
    ) -> bool:
        active = set(default_layout[:ACTIVE_STORAGE_SLOTS])
        return bool(
            not (penelo_flags & JOINED_ONCE_FLAG)
            and REKS_ID in active
            and VAAN_ID not in active
            and PENELO_ID not in active
        )

    def battle_info(self) -> int:
        base = self.process.u64(BATTLE_INFO_GLOBAL)
        if not base:
            return 0
        self.process.read(base + PARTY_MEMBER_OFFSET, 1)
        self.process.read(base + PARTY_LAYOUT_OFFSET, PARTY_LAYOUT_STRIDE)
        return base

    def layout(self, base: int, layout_id: int) -> tuple[int, ...]:
        return unpack_layout(
            self.process.read(
                base + PARTY_LAYOUT_OFFSET + layout_id * PARTY_LAYOUT_STRIDE,
                PARTY_LAYOUT_STRIDE,
            )
        )

    def controlled_actor_valid(self) -> bool:
        handle = self.process.u32(CONTROLLED_HANDLE_GLOBAL)
        root = self.process.u64(CONTROLLED_ROOT_GLOBAL)
        if not handle or not root:
            return False
        actor = self.process.u64(root + 0x30)
        return bool(actor and self.process.u32(actor + 0x08) == handle)

    def active_party_has_vaan(self, base: int) -> bool:
        return VAAN_ID in self.layout(base, DEFAULT_LAYOUT)[:4]

    def repair_window_open(
        self,
        base: int,
        layouts: tuple[tuple[int, ...], ...] | None = None,
    ) -> bool:
        if not self.controlled_actor_valid():
            return False

        if layouts is None:
            layouts = tuple(self.layout(base, index) for index in range(5))
        active_generations = tuple(
            {member_id for member_id in layouts[layout_id][:4] if member_id != EMPTY}
            for layout_id in (DEFAULT_LAYOUT, CURRENT_LAYOUT, UI_LAYOUT)
        )
        if (
            not active_generations[0]
            or not active_generations[1]
            or active_generations[1] != active_generations[2]
        ):
            return False
        control = self.process.u64(USER_CONTROL_GLOBAL)
        location = self.process.u64(LOCATION_SAVE_GLOBAL)
        if not control or not location or self.process.u32(NATIVE_MENU_STATE):
            return False
        if not self.process.read(control + USER_CONTROL_ENABLED_OFFSET, 1)[0]:
            return False
        current_location = self.process.u32(location + LOCATION_CURRENT_OFFSET)
        ui_location = self.process.u32(location + LOCATION_UI_OFFSET)
        return bool(current_location != 0xFFFFFFFF and current_location == ui_location)

    def controlled_party_is_vaan(self, base: int) -> bool:
        return bool(
            self.controlled_actor_valid()
            and self.process.u32(base + PARTY_LEADER_ID_OFFSET) == VAAN_ID
            and self.active_party_has_vaan(base)
        )

    def town_materialization_is_validly_collapsed(
        self,
        base: int,
        layouts: tuple[tuple[int, ...], ...],
        *,
        require_complete_roster: bool = True,
    ) -> bool:
        if len(layouts) != PARTY_LAYOUT_COUNT:
            return False
        default = layouts[DEFAULT_LAYOUT]
        current = layouts[CURRENT_LAYOUT]
        ui = layouts[UI_LAYOUT]
        menu = layouts[MENU_LAYOUT]
        if not (
            layout_has_unique_members(default)
            and layout_has_unique_members(current)
            and layout_has_unique_members(ui)
            and layout_has_unique_members(menu)
            and layout_has_no_guest_in_selectable_trio(default)
            and layout_has_no_guest_in_selectable_trio(current)
            and layout_has_no_guest_in_selectable_trio(ui)
            and layout_has_no_guest_in_selectable_trio(menu)
        ):
            return False
        if require_complete_roster and not (
            layout_has_single_protected_members(default)
            and layout_has_single_protected_members(menu)
        ):
            return False

        default_active = active_main_members(default)
        menu_active = active_main_members(menu)
        current_materialized = tuple(
            member_id
            for member_id in current[:ACTIVE_STORAGE_SLOTS]
            if member_id != EMPTY
        )
        ui_materialized = tuple(
            member_id for member_id in ui[:ACTIVE_STORAGE_SLOTS] if member_id != EMPTY
        )
        if (
            default_active is None
            or menu_active is None
            or set(default_active) != set(menu_active)
            or current_materialized != ui_materialized
            or len(current_materialized) != 1
            or current_materialized[0] not in MAIN_MEMBER_IDS
            or current[:ACTIVE_STORAGE_SLOTS] != ui[:ACTIVE_STORAGE_SLOTS]
        ):
            return False
        for member_id in PROTECTED_MEMBER_IDS:
            if (member_id in default[:ACTIVE_STORAGE_SLOTS]) != (
                member_id in menu[:ACTIVE_STORAGE_SLOTS]
            ):
                return False

        materialized_member = current_materialized[0]
        leader = self.process.u32(base + PARTY_LEADER_ID_OFFSET)
        leader_matches_formation = bool(
            leader != EMPTY and leader in default_active and leader in menu_active
        )
        vaan_town_handoff = bool(
            leader == materialized_member == VAAN_ID
            and default.count(VAAN_ID) == 1
            and menu.count(VAAN_ID) == 1
        )
        if not (leader_matches_formation or vaan_town_handoff):
            return False
        if materialized_member != leader and (
            materialized_member != VAAN_ID
            or default.count(VAAN_ID) != 1
            or menu.count(VAAN_ID) != 1
        ):
            return False

        try:
            location = self.process.u64(LOCATION_SAVE_GLOBAL)
            if not location:
                return False
            current_location = self.process.u32(location + LOCATION_CURRENT_OFFSET)
            ui_location = self.process.u32(location + LOCATION_UI_OFFSET)
            if current_location == 0xFFFFFFFF or current_location != ui_location:
                return False
            runtime_town = bool(
                self.process.read(location + LOCATION_FLAGS_OFFSET, 1)[0]
                & LOCATION_TOWN_FLAG
            )
            metadata_town = False
            if not runtime_town:
                try:
                    metadata_town = bool(
                        coop.session_state._metadata_marks_town(
                            self.process,
                            current_location,
                        )
                    )
                except (AttributeError, OSError, RuntimeError, struct.error):
                    metadata_town = False
            if runtime_town or metadata_town:
                return True

            vaan_story_slot = (
                EMPTY,
                EMPTY,
                EMPTY,
                VAAN_ID,
            )
            return bool(
                current[:ACTIVE_STORAGE_SLOTS] == vaan_story_slot
                and self.repair_window_open(base, layouts)
            )
        except (OSError, RuntimeError, struct.error, ValueError):
            return False

    def signature(self, base: int):
        flags = tuple(
            self.process.read(
                base + PARTY_MEMBER_OFFSET + member_id * PARTY_MEMBER_STRIDE,
                1,
            )[0]
            for member_id in PROTECTED_MEMBER_IDS
        )
        layouts = tuple(self.layout(base, index) for index in range(5))
        return flags, layouts

    def consume_manual_party_intent(
        self,
        base: int,
        layouts: tuple[tuple[int, ...], ...],
        story_counters: tuple[int, ...],
    ) -> bool:
        if self.intent_path is None:
            return False
        record = party_intent_channel.read_party_intent(
            self.intent_path,
            game_pid=self.game_pid,
        )
        if record is None or record.token == self.last_party_intent_token:
            return False
        live = coherent_active_main_members(layouts)
        if record.battle_info != base:
            self.last_party_intent_token = record.token
            print(
                "PARTY_ACTIVE_INTENT_IGNORED "
                f"epoch={record.epoch} generation={record.battle_info:#x} "
                f"live={base:#x}",
                flush=True,
            )
            return False
        if live is None or tuple(record.active_members) != tuple(live):
            return False
        self.last_party_intent_token = record.token
        self.accepted_active_members = record.active_members

        self.story_removal_counters = story_counters
        self.pending_story_restore.clear()
        self.story_removal_watches.clear()
        self.last_active_anomaly = None
        self.bad_signature = None
        self.bad_since = 0.0
        print(
            "PARTY_ACTIVE_INTENT_ADOPTED "
            f"epoch={record.epoch} members="
            f"{','.join(map(str, record.active_members))}",
            flush=True,
        )
        return True

    def observe_story_removals(
        self,
        layouts: tuple[tuple[int, ...], ...],
        story_counters: tuple[int, ...],
        *,
        now: float,
    ) -> None:
        changed = story_removal_journal.changed_members(
            self.story_removal_counters,
            story_counters,
        )
        self.story_removal_counters = story_counters
        accepted = self.accepted_active_members
        if accepted is None:
            self.story_removal_watches.clear()
            return
        for member_id in changed:
            if member_id in PROTECTED_MEMBER_IDS and member_id in accepted:
                self.story_removal_watches[member_id] = float(now) + 2.0

        current_active = {
            member_id
            for member_id in layouts[DEFAULT_LAYOUT][:3]
            if member_id in MAIN_MEMBER_IDS
        }
        for member_id, deadline in tuple(self.story_removal_watches.items()):
            if member_id not in current_active:
                self.pending_story_restore.add(member_id)
                del self.story_removal_watches[member_id]
                print(
                    f"PARTY_STORY_REMOVAL member={member_id} restore=armed",
                    flush=True,
                )
            elif float(now) >= deadline:
                del self.story_removal_watches[member_id]

    def observe_unproven_active_change(
        self,
        layouts: tuple[tuple[int, ...], ...],
    ) -> None:
        accepted = self.accepted_active_members
        current = coherent_active_main_members(layouts)
        if accepted is None or current is None or set(current) == set(accepted):
            self.last_active_anomaly = None
            return
        signature = tuple(sorted(current))
        if (
            not self.pending_story_restore
            and not self.story_removal_watches
            and signature != self.last_active_anomaly
        ):
            self.last_active_anomaly = signature
            print(
                "PARTY_ACTIVE_CHANGE_UNPROVEN "
                f"accepted={','.join(map(str, accepted))} "
                f"live={','.join(map(str, current))}; no force",
                flush=True,
            )

    @staticmethod
    def healthy(signature) -> bool:
        flags, layouts = signature
        if (
            len(flags) != len(PROTECTED_MEMBER_IDS)
            or len(layouts) != PARTY_LAYOUT_COUNT
        ):
            return False

        default = layouts[DEFAULT_LAYOUT]
        current = layouts[CURRENT_LAYOUT]
        ui = layouts[UI_LAYOUT]
        menu = layouts[MENU_LAYOUT]

        if not (
            layout_has_single_protected_members(default)
            and layout_has_single_protected_members(menu)
            and layout_has_unique_members(default)
            and layout_has_unique_members(current)
            and layout_has_unique_members(ui)
            and layout_has_unique_members(menu)
            and layout_has_no_guest_in_selectable_trio(default)
            and layout_has_no_guest_in_selectable_trio(current)
            and layout_has_no_guest_in_selectable_trio(ui)
            and layout_has_no_guest_in_selectable_trio(menu)
        ):
            return False
        for member_id in PROTECTED_MEMBER_IDS:
            member_active = member_id in default[:ACTIVE_STORAGE_SLOTS]
            if (member_id in menu[:ACTIVE_STORAGE_SLOTS]) != member_active:
                return False
            expected_materialized = 1 if member_active else 0
            if (
                current.count(member_id) != expected_materialized
                or ui.count(member_id) != expected_materialized
            ):
                return False
            if member_active and (
                member_id not in current[:ACTIVE_STORAGE_SLOTS]
                or member_id not in ui[:ACTIVE_STORAGE_SLOTS]
            ):
                return False
        return True

    def repair(
        self,
        base: int,
        signature,
        *,
        restore_active: tuple[int, ...] | None = None,
    ) -> None:
        flags, layouts = signature
        town_actor_collapsed = self.town_materialization_is_validly_collapsed(
            base,
            layouts,
            require_complete_roster=False,
        )

        formatted = ", ".join(
            f"{member_id}:{member_flags:#04x}"
            for member_id, member_flags in zip(PROTECTED_MEMBER_IDS, flags)
        )
        print(
            f"PENELO_GUARD repairing flags=({formatted}) layouts={layouts}",
            flush=True,
        )
        default_before = layouts[DEFAULT_LAYOUT]
        menu_before = layouts[MENU_LAYOUT]
        for member_id, member_flags in zip(PROTECTED_MEMBER_IDS, flags):
            # Only Vaan is materialized in town. The native call recreates that
            # actor, so restoring the roster bit is sufficient in this state.
            already_rostered = bool(
                default_before.count(member_id) == 1
                and menu_before.count(member_id) == 1
            )
            if member_flags & IN_PARTY_FLAG or already_rostered:
                continue
            if town_actor_collapsed:
                member = base + PARTY_MEMBER_OFFSET + member_id * PARTY_MEMBER_STRIDE
                self.process.write(
                    member,
                    bytes((member_flags | IN_PARTY_FLAG,)),
                )
                print(
                    f"PENELO_GUARD membership restored in town member={member_id}",
                    flush=True,
                )
                continue
            result = self.caller.call(ADD_PARTY_MEMBER, member_id, 0)
            print(
                f"PENELO_GUARD addPartyMember member={member_id} result={result:#x}",
                flush=True,
            )
            time.sleep(0.15)
            base = self.battle_info()
            if not base:
                # A loading transition can legitimately replace battle-info
                # immediately after the game-thread native call.  Classify it
                # as frame I/O so main retries, while true ABI RuntimeErrors
                # still escape to the supervisor.
                raise OSError("battle info changed after adding co-op member")

        # Current/UI are materialized generations and never receive fabricated
        # reserve IDs here. A bootstrap path remains for a fresh Vaan generation
        # if the prepatched story record did not insert the companions. It is
        # disabled after a baseline/manual epoch so it cannot overwrite the
        # player's reserve selection.
        initial_vaan_solo = bool(
            restore_active is None
            and self.accepted_active_members is None
            and not any(member_flags & JOINED_ONCE_FLAG for member_flags in flags)
            and self.controlled_party_is_vaan(base)
        )
        bootstrap_active = (
            (VAAN_ID,) if initial_vaan_solo and PROTECTED_MEMBER_IDS else None
        )
        for layout_id in (DEFAULT_LAYOUT, MENU_LAYOUT):
            current = self.layout(base, layout_id)
            desired = normalize_coop_roster(
                current,
                prefer_active=False,
            )
            if restore_active:
                desired = apply_active_main_members(desired, restore_active)
            elif bootstrap_active:
                desired = apply_active_main_members(desired, bootstrap_active)
            if current != desired:
                self.process.write(
                    base + PARTY_LAYOUT_OFFSET + layout_id * PARTY_LAYOUT_STRIDE,
                    pack_layout(desired),
                )

        if town_actor_collapsed:
            print(
                "PENELO_GUARD roster restored; actor refresh deferred in town",
                flush=True,
            )
        else:
            self.caller.call(PARTY_ALL_READ)
        self.cooldown_until = time.monotonic() + POST_REPAIR_COOLDOWN_SECONDS
        self.bad_signature = None
        self.bad_since = 0.0
        print("PENELO_GUARD repair submitted", flush=True)

    def tick(self) -> None:
        now = time.monotonic()
        base = self.battle_info()
        if not base:
            self.disarm_session("battle info unavailable")
            return

        penelo_flags = self.process.read(
            base + PARTY_MEMBER_OFFSET + PENELO_ID * PARTY_MEMBER_STRIDE,
            1,
        )[0]
        default_layout = self.layout(base, DEFAULT_LAYOUT)
        if self.armed and self.is_early_reks_generation(penelo_flags, default_layout):
            self.disarm_session("fresh Reks generation")

        just_armed = False
        if not self.armed:
            if not (
                penelo_flags & JOINED_ONCE_FLAG or self.controlled_party_is_vaan(base)
            ):
                return
            self.armed = True
            just_armed = True
            self.removal_filter.set_enabled(BLOCK_NATIVE_STORY_REMOVALS)

            self.story_removal_counters = self.story_removal_journal.snapshot()
            print(
                "PENELO_GUARD armed: Vaan control/co-op history confirmed; "
                "native story removals left enabled",
                flush=True,
            )

        signature = self.signature(base)
        layouts = signature[1]
        story_counters = self.story_removal_journal.snapshot()
        manual_adopted = self.consume_manual_party_intent(
            base,
            layouts,
            story_counters,
        )
        if not manual_adopted and not just_armed:
            self.observe_story_removals(
                layouts,
                story_counters,
                now=now,
            )

        coherent_active = coherent_active_main_members(layouts)
        if (
            self.accepted_active_members is None
            and coherent_active is not None
            and layout_has_single_protected_members(layouts[DEFAULT_LAYOUT])
            and layout_has_single_protected_members(layouts[MENU_LAYOUT])
            and self.repair_window_open(base, layouts)
        ):
            self.accepted_active_members = coherent_active
            print(
                f"PARTY_ACTIVE_BASELINE members={','.join(map(str, coherent_active))}",
                flush=True,
            )

        if (
            self.pending_story_restore
            and coherent_active is not None
            and self.accepted_active_members is not None
            and set(coherent_active) == set(self.accepted_active_members)
        ):
            self.pending_story_restore.clear()
            self.last_active_anomaly = None
            print("PARTY_STORY_RESTORE_CONFIRMED", flush=True)

        self.observe_unproven_active_change(layouts)
        needs_membership_repair = not (
            self.healthy(signature)
            or self.town_materialization_is_validly_collapsed(base, layouts)
        )
        needs_active_restore = bool(
            self.pending_story_restore and self.accepted_active_members
        )
        if not needs_membership_repair and not needs_active_restore:
            self.bad_signature = None
            self.bad_since = 0.0
            return
        if now < self.cooldown_until:
            return

        if not self.repair_window_open(base, layouts):
            self.bad_signature = None
            self.bad_since = 0.0
            return
        repair_signature = (
            signature,
            tuple(sorted(self.pending_story_restore)),
            self.accepted_active_members if needs_active_restore else None,
        )
        if repair_signature != self.bad_signature:
            self.bad_signature = repair_signature
            self.bad_since = now
            return
        if now - self.bad_since < REPAIR_DEBOUNCE_SECONDS:
            return
        self.repair(
            base,
            signature,
            restore_active=(
                self.accepted_active_members if needs_active_restore else None
            ),
        )


def main() -> int:
    pid = coop.find_pid()
    lease = helper_lease.HelperLease(pid, "penelo_guard")
    try:
        process = coop.RemoteProcess(pid)
    except BaseException:
        try:
            lease.close()
        except BaseException:
            pass
        raise

    guard = None
    try:
        if not PROTECTED_MEMBER_IDS:
            print(
                f"PENELO_GUARD ready pid={pid}; start with all characters disabled",
                flush=True,
            )
            while not runtime_shutdown.requested():
                if not coop.movement_runtime_process_alive(process):
                    break
                time.sleep(POLL_SECONDS)
            return 0

        guard = PeneloStoryGuard(process, game_pid=pid)
        print(f"PENELO_GUARD ready pid={pid}; waiting for Vaan control", flush=True)
        next_transient_log_at = 0.0
        while True:
            if runtime_shutdown.requested():
                break
            if not coop.movement_runtime_process_alive(process):
                break
            try:
                guard.tick()
            except (OSError, struct.error) as error:
                if not coop.movement_runtime_process_alive(process):
                    break
                next_transient_log_at = report_transient_read(
                    error,
                    now=time.monotonic(),
                    next_log_at=next_transient_log_at,
                )
            time.sleep(POLL_SECONDS)
    finally:
        try:
            if guard is not None:
                try:
                    guard.close()
                except BaseException:
                    pass
        finally:
            try:
                process.close()
            except BaseException:
                pass
            try:
                lease.close()
            except BaseException:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
