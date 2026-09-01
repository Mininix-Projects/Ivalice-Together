"""Initialize the processes that provide co-op mode."""

from __future__ import annotations

import ctypes as ct
import hashlib
import mmap
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes as wt
from dataclasses import dataclass
from pathlib import Path

import atomic_files
import controller_bindings
import controller_state_transport
import coop_roster
import coop_runtime as coop
import d3d_panel_transport
import helper_lease
import loot_ownership
import menu_themes
import runtime_entry
import runtime_paths
import runtime_shutdown
import runtime_status
import session_state
import story_party_patch
from penelo_story_guard import BATTLE_INFO_GLOBAL

HERE = Path(__file__).resolve().parent
ROOT = runtime_paths.resolve_game_root(__file__)
DATA_DIRECTORY = runtime_paths.resolve_config_directory(__file__)
RUNTIME_DIRECTORY = runtime_paths.resolve_runtime_directory(__file__)
PYTHON = Path(sys.executable)
LOG_ROOT = RUNTIME_DIRECTORY / "runtime_logs_headless"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class GameExitedBeforeStartup(RuntimeError):
    """The selected FF12 PID disappeared before it became attachable."""


class SupervisorAlreadyRunning(RuntimeError):
    """Another supervisor owns the exact selected game process."""


RENDER_DLL = Path(
    os.environ.get(
        "FF12_COOP_RENDER_DLL",
        str(HERE / "p2_menu_color_dx11_v85.dll"),
    )
).resolve()
CAVE_BOOTSTRAP_DLL = Path(
    os.environ.get(
        "FF12_COOP_CAVE_BOOTSTRAP_DLL",
        str(HERE / "ivalice-together-autostart.dll"),
    )
).resolve()
RUNTIME_BINARY_DIRECTORY = DATA_DIRECTORY / "runtime"
CAVE_BOOTSTRAP_TIMEOUT = 5.0
RING_READY_OFFSET = 0xD80
RING_READY_MAGIC = 0x33474E52
RING_PRESENT_COUNT_OFFSET = 0xD90
MOVEMENT_START_ATTEMPTS = 2
MOVEMENT_READY_TIMEOUT = 18.0
MOVEMENT_READY_POLL_INTERVAL = 0.05
CHILD_TERMINATE_TIMEOUT = 2.0
CHILD_COOPERATIVE_SHUTDOWN_TIMEOUT = 4.0
CHILD_READY_TIMEOUT = 30.0
CHILD_READY_POLL_INTERVAL = 0.05
CHILD_LOG_READ_CHUNK_SIZE = 64 * 1024
CHILD_START_ATTEMPTS = 2
COOPERATIVE_ONLY_CHILD_ROLES = frozenset({"penelo_guard"})
HELPER_STALL_GRACE_SECONDS = 8.0
HELPER_RECOVERY_BACKOFF_SECONDS = 0.25
SUPERVISOR_RESTART_INITIAL_SECONDS = 0.25
SUPERVISOR_RESTART_MAX_SECONDS = 5.0
SUPERVISOR_LIFETIME_OPEN_ATTEMPTS = 5
SUPERVISOR_LIFETIME_OPEN_RETRY_SECONDS = 0.1
RENDER_READY_TIMEOUT = 30.0
RUNTIME_READY_POLL_INTERVAL = 0.05
RUNTIME_READY_STABLE_SECONDS = 0.5
RUNTIME_READY_TIMEOUT = 45.0
GAME_STARTUP_TIMEOUT = 120.0
GAME_STARTUP_STABLE_SECONDS = 2.0
GAME_STARTUP_POLL_INTERVAL = 0.1
INVALID_HUD_PARTY_SLOT = 0xFFFFFFFF
P1_GAUGE_THEME_PALETTE_OFFSET = 0xE00
P2_GAUGE_THEME_PALETTE_OFFSET = 0xB00
P3_GAUGE_THEME_PALETTE_OFFSET = 0x8240

# Global launcher transaction.  The local native runtime owns bytes +0x10..+0x58
# in each allocation header; +0x60 is intentionally reserved for the launcher
# that coordinates every helper and the renderer as one installation.
GLOBAL_INSTALL_MARKER_OFFSET = 0x60
GLOBAL_INSTALL_MARKER_MAGIC = b"ITGLBL02"
GLOBAL_INSTALL_MARKER_VERSION = 2
GLOBAL_INSTALL_PREPARED = 1
GLOBAL_INSTALL_COMMITTED = 2
GLOBAL_INSTALL_COMPONENT_RENDERER = 1 << 0
GLOBAL_INSTALL_COMPONENT_HEADLESS_MENU = 1 << 1
GLOBAL_INSTALL_COMPONENT_STORY_PARTY = 1 << 2
GLOBAL_INSTALL_COMPONENT_PENELO_GUARD = 1 << 3
GLOBAL_INSTALL_COMPONENT_MOVEMENT = 1 << 4
GLOBAL_INSTALL_COMPONENT_MASK = (
    GLOBAL_INSTALL_COMPONENT_RENDERER
    | GLOBAL_INSTALL_COMPONENT_HEADLESS_MENU
    | GLOBAL_INSTALL_COMPONENT_STORY_PARTY
    | GLOBAL_INSTALL_COMPONENT_PENELO_GUARD
    | GLOBAL_INSTALL_COMPONENT_MOVEMENT
)
GLOBAL_INSTALL_FEATURE_UPSTREAM_CAMERA_V2 = 1 << 0
GLOBAL_INSTALL_FEATURE_START_WITH_ALL = 1 << 1
GLOBAL_INSTALL_MARKER_STRUCT = struct.Struct("<8sIIII16s16s")
# Explicitly rev this value whenever the global component topology, marker ABI,
# or a component's native layout changes.  It is deliberately not derived from
# a single source file or a temporary/frozen extraction path.
# Build 109: microsecond Present timing and frame-clocked actor sampling change
# the process-resident camera ABI and the frozen native resource set.
GLOBAL_INSTALL_LAYOUT_ID = bytes.fromhex("f27d69812c074b92a74aa1f05867202c")


@dataclass(frozen=True)
class GlobalInstallPreflight:
    """Read-only classification of the three shared runtime allocations."""

    mode: str
    bases: tuple[int, int, int]
    feature_bits: int


@dataclass
class _PersistentSupervisorContext:
    """Process-lifetime ownership shared by retries on one supervisor thread."""

    pid: int
    mutex: object
    owner_thread_id: int
    resident_possible: bool = False


_PERSISTENT_SUPERVISOR_CONTEXT: _PersistentSupervisorContext | None = None


class ExternalHelper:
    """A helper inherited from a prior supervisor, observed through its lease."""

    _ff12_external = True

    def __init__(self, game_pid: int, role: str) -> None:
        self.game_pid = int(game_pid)
        self.role = str(role)
        # The precise process id is intentionally not guessed.  Movement's
        # native heartbeat supplies an independently validated owner when one
        # is needed; zero tells generic supervision not to bind to a guess.
        self.pid = 0

    def poll(self) -> int | None:
        try:
            return None if helper_lease.active(self.game_pid, self.role) else 1
        except OSError:
            # A failed observation is never authority to start a duplicate.
            return None


class _ExternalStream:
    def close(self) -> None:
        pass

SCRIPT_RUNTIME_MODES = {
    "story_party_patch.py": "story_party",
    "penelo_story_guard.py": "penelo_guard",
    "coop_runtime.py": "movement",
    "headless_menu_shell.py": "headless_menu",
}

# The helper publishes infrastructure liveness in a dedicated seqlock. Never
# reuse gameplay fields here: filtering and movement ownership are intentionally
# inactive during focus changes, F6/F7/F8, KO/status, loading and handoffs.
MOVEMENT_LIFETIME_READY_OFFSET = (
    coop.P3_COMMAND_BLOCK_OFFSET + coop.MOVEMENT_HELPER_LIVENESS_OFFSET
)


ERROR_ALREADY_EXISTS = 183
kernel32 = ct.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.argtypes = [ct.c_void_p, ct.c_bool, ct.c_wchar_p]
kernel32.CreateMutexW.restype = ct.c_void_p
kernel32.CloseHandle.argtypes = [ct.c_void_p]
kernel32.CloseHandle.restype = ct.c_bool
kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ct.POINTER(wt.DWORD)]
kernel32.GetExitCodeProcess.restype = wt.BOOL
user32 = ct.WinDLL("user32", use_last_error=True)
user32.EnumWindows.argtypes = [ct.c_void_p, wt.LPARAM]
user32.EnumWindows.restype = wt.BOOL
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ct.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.GetClientRect.argtypes = [wt.HWND, ct.POINTER(wt.RECT)]
user32.GetClientRect.restype = wt.BOOL
user32.SendMessageTimeoutW.argtypes = [
    wt.HWND,
    wt.UINT,
    wt.WPARAM,
    wt.LPARAM,
    wt.UINT,
    wt.UINT,
    ct.POINTER(ct.c_size_t),
]
user32.SendMessageTimeoutW.restype = wt.LPARAM

WM_NULL = 0x0000
SMTO_BLOCK = 0x0001
SMTO_ABORTIFHUNG = 0x0002
STILL_ACTIVE = 259


def u32(process, address: int) -> int:
    return struct.unpack("<I", process.read(address, 4))[0]


def game_window_state(pid: int) -> tuple[int, int, int, bool] | None:
    """Return the largest visible process window and its responsiveness."""
    candidates: list[tuple[int, int, int]] = []
    callback_type = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    @callback_type
    def visit(window, _parameter):
        window_pid = wt.DWORD()
        user32.GetWindowThreadProcessId(window, ct.byref(window_pid))
        if window_pid.value != pid or not user32.IsWindowVisible(window):
            return True
        client = wt.RECT()
        if user32.GetClientRect(window, ct.byref(client)):
            width = max(0, client.right - client.left)
            height = max(0, client.bottom - client.top)
            if width and height:
                candidates.append((int(window), width, height))
        return True

    user32.EnumWindows(visit, 0)
    if not candidates:
        return None
    window, width, height = max(candidates, key=lambda item: item[1] * item[2])
    response = ct.c_size_t()
    responsive = bool(
        user32.SendMessageTimeoutW(
            window,
            WM_NULL,
            0,
            0,
            SMTO_BLOCK | SMTO_ABORTIFHUNG,
            250,
            ct.byref(response),
        )
    )
    return window, width, height, responsive


def loaded_module_names(pid: int) -> frozenset[str]:
    import inject_dll

    return frozenset(name.casefold() for name, _path in inject_dll.loaded_modules(pid))


def game_startup_snapshot(pid: int) -> tuple[bool, tuple[int, int, int] | None, str]:
    window_state = game_window_state(pid)
    try:
        modules = loaded_module_names(pid)
    except OSError as error:
        code = getattr(error, "winerror", None) or error.errno
        return False, None, f"modulos ainda indisponiveis ({code})"
    graphics_ready = {"d3d11.dll", "dxgi.dll"}.issubset(modules)
    if window_state is None:
        return False, None, "janela do jogo ainda nao foi criada"
    window, width, height, responsive = window_state
    identity = window, width, height
    ready = responsive and graphics_ready and width >= 320 and height >= 180
    detail = (
        f"janela={window:#x} cliente={width}x{height} "
        f"responde={int(responsive)} d3d11={int(graphics_ready)}"
    )
    return ready, identity, detail


def wait_game_startup_ready(
    process,
    pid: int,
    *,
    timeout: float = GAME_STARTUP_TIMEOUT,
    stable_seconds: float = GAME_STARTUP_STABLE_SECONDS,
    poll_interval: float = GAME_STARTUP_POLL_INTERVAL,
) -> None:
    """Wait for native rendering to stabilize before accessing game memory."""
    deadline = time.monotonic() + timeout
    stable_since = None
    stable_identity = None
    last_detail = "processo encontrado"
    while time.monotonic() < deadline:
        if not game_alive(process):
            raise GameExitedBeforeStartup("jogo encerrou antes de inicializar o D3D11")
        ready, identity, last_detail = game_startup_snapshot(pid)
        if ready and identity == stable_identity:
            stable_for = (
                time.monotonic() - stable_since if stable_since is not None else 0
            )
            if stable_since is not None and stable_for >= stable_seconds:
                print(f"CO-OP STARTUP ESTAVEL {last_detail}", flush=True)
                return
        elif ready:
            stable_identity = identity
            stable_since = time.monotonic()
        else:
            stable_identity = None
            stable_since = None
        time.sleep(poll_interval)
    raise TimeoutError("jogo nao estabilizou para iniciar o co-op: " + last_detail)


def authored_session_identity(
    roster: coop_roster.CoopRoster,
    existing_p1: int = coop_roster.VAAN_ID,
) -> session_state.SessionIdentity:
    p2 = roster.p2
    p3 = roster.p3
    if p2 is None:
        p2 = p3 if p3 is not None else coop_roster.DEFAULT_P2
        p3 = None
    p1 = existing_p1
    if p1 not in session_state.MAIN_PARTY_MEMBER_IDS or p1 in (p2, p3):
        p1 = coop_roster.VAAN_ID
    return session_state.SessionIdentity(p1, p2, p3)


def existing_session_p1() -> int:
    try:
        config = session_state.load_config()
        member_id = int(config["players"]["p1"]["party_member_id"])
    except (KeyError, TypeError, ValueError):
        return coop_roster.VAAN_ID
    if member_id not in session_state.MAIN_PARTY_MEMBER_IDS:
        return coop_roster.VAAN_ID
    return member_id


def seed_authored_session_identity(
    roster: coop_roster.CoopRoster | None = None,
) -> session_state.SessionIdentity:
    if roster is None:
        roster = coop_roster.load()
    identity = authored_session_identity(roster, existing_session_p1())
    session_state.save_identity(identity)
    return identity


def movement_readiness_snapshot(
    process,
    base: int,
    movement_patch: bytes,
    *,
    expected_owner_pid: int | None = None,
) -> tuple[bool, str]:
    current_hook = None
    try:
        current_hook = process.read(
            coop.MOVEMENT_HOOK_SITE,
            len(movement_patch),
        )
        hook_text = current_hook.hex(" ")
    except (OSError, RuntimeError, ValueError) as error:
        hook_text = f"erro:{type(error).__name__}:{error}"
    liveness = (False, 0, 0, 0, 0, 0)
    try:
        liveness = coop.read_movement_helper_liveness(process, base=base)
        (
            coherent,
            sequence,
            magic,
            version,
            owner_pid,
            heartbeat_ms,
        ) = liveness
        heartbeat_age_ms = (
            int(time.monotonic() * 1000.0) - heartbeat_ms if heartbeat_ms else None
        )
        heartbeat_fresh = bool(
            heartbeat_age_ms is not None
            and -1000
            <= heartbeat_age_ms
            <= int(coop.MOVEMENT_HELPER_LIVENESS_STALE_SECONDS * 1000.0)
        )
        owner_matches = bool(
            expected_owner_pid is None or owner_pid == int(expected_owner_pid)
        )
        liveness_ready = bool(coherent and heartbeat_fresh and owner_matches)
        liveness_text = (
            f"ready={int(liveness_ready)} coherent={int(coherent)} seq={sequence} "
            f"magic={magic:#x} version={version} owner={owner_pid} "
            f"expected_owner={expected_owner_pid!r} heartbeat={heartbeat_ms} "
            f"age_ms={heartbeat_age_ms!r}"
        )
    except (OSError, RuntimeError, ValueError, struct.error) as error:
        liveness_ready = False
        liveness_text = f"erro:{type(error).__name__}:{error}"
    diagnostic = (
        f"base={base:#x} "
        f"hook_atual={hook_text} "
        f"hook_esperado={movement_patch.hex(' ')} "
        f"liveness={liveness_text}"
    )
    return current_hook == movement_patch and liveness_ready, diagnostic


def reset_movement_helper_ready(process, base: int) -> None:
    coop.clear_movement_helper_liveness(process, base=base)


def acquire_mutex(pid: int):
    # CreateMutexW reports an existing object through last-error even on a
    # successful return.  Clear residual thread state first so an unrelated
    # earlier Win32 call can never make a newly-created supervisor look like a
    # duplicate.
    ct.set_last_error(0)
    handle = kernel32.CreateMutexW(
        None, True, f"Local\\FF12LocalCoopHeadlessLauncher_{pid}"
    )
    if not handle:
        raise OSError(ct.get_last_error(), "CreateMutexW")
    try:
        if ct.get_last_error() == ERROR_ALREADY_EXISTS:
            raise SupervisorAlreadyRunning(
                "o co-op headless ja esta ligado para este jogo"
            )
        return handle
    except BaseException:
        # The Win32 object already exists after CreateMutexW returns.  Protect
        # the handle against duplicate detection and asynchronous interruption
        # in the Python instructions that follow the system call.
        kernel32.CloseHandle(handle)
        raise


def acquire_session_mutex(pid: int) -> tuple[object, bool]:
    """Borrow the persistent runner's mutex or acquire one for a one-shot call."""

    context = _PERSISTENT_SUPERVISOR_CONTEXT
    if context is not None:
        if (
            context.pid != int(pid)
            or context.owner_thread_id != threading.get_ident()
        ):
            raise RuntimeError("persistent supervisor mutex belongs to another session")
        return context.mutex, False
    return acquire_mutex(pid), True


def mark_persistent_runtime_resident(pid: int) -> None:
    """Record that teardown can no longer assume a pre-commit installation."""

    context = _PERSISTENT_SUPERVISOR_CONTEXT
    if context is None:
        return
    if (
        context.pid != int(pid)
        or context.owner_thread_id != threading.get_ident()
    ):
        raise RuntimeError("persistent supervisor context changed during commit")
    context.resident_possible = True


def open_supervisor_lifetime_process(pid: int):
    """Open an identity-pinning process handle across short access races."""

    last_error: OSError | None = None
    for attempt in range(SUPERVISOR_LIFETIME_OPEN_ATTEMPTS):
        try:
            return coop.RemoteProcess(pid)
        except OSError as error:
            last_error = error
            if attempt + 1 < SUPERVISOR_LIFETIME_OPEN_ATTEMPTS:
                time.sleep(SUPERVISOR_LIFETIME_OPEN_RETRY_SECONDS)
    if last_error is None:
        raise AssertionError("lifetime process retry loop did not run")
    raise last_error


def game_alive(process) -> bool:
    """Return process lifetime without confusing one memory fault with exit.

    Loading and teardown of individual game objects can make an otherwise valid
    ReadProcessMemory sample fail.  The process handle has an exact lifetime
    signal, so production uses it and keeps the memory probe only for the small
    process doubles used by diagnostics and tests.
    """

    handle = getattr(process, "handle", None)
    if handle:
        exit_code = wt.DWORD()
        if kernel32.GetExitCodeProcess(handle, ct.byref(exit_code)):
            return exit_code.value == STILL_ACTIVE
        # A failed lifetime query is uncertainty, not an exit signal.  The
        # supervisor owns this handle until final cleanup; only a successful
        # non-STILL_ACTIVE result can authorize tearing down every helper.
        # Falling back to a remote-memory probe here would again confuse a
        # loading/read race with process death.
        return True
    try:
        process.read(0x120000, 2)
        return True
    except OSError:
        return False


def best_effort_diagnostic(message: str, *, stderr: bool = False) -> None:
    """Keep a broken diagnostic stream outside the runtime's control plane."""

    try:
        print(
            str(message),
            file=sys.stderr if stderr else sys.stdout,
            flush=True,
        )
    except Exception:
        pass


def exception_diagnostic(error: BaseException) -> str:
    """Format an exception without trusting a third-party ``__str__`` method."""

    name = type(error).__name__
    try:
        return f"{name}: {error}"
    except Exception:
        return name


def publish_runtime_status(
    pid: int,
    state: str,
    message: str,
    *,
    warning: str,
) -> None:
    """Publish launcher UI state without making storage a startup dependency."""

    try:
        runtime_status.write_status(
            pid,
            state,
            message,
            data_directory=RUNTIME_DIRECTORY,
        )
    except Exception as error:
        best_effort_diagnostic(
            f"{warning}: {exception_diagnostic(error)}",
            stderr=True,
        )


def clear_runtime_status(pid: int) -> None:
    """Remove disposable status without masking lifecycle or cleanup results."""

    try:
        runtime_status.clear_status(
            pid,
            data_directory=RUNTIME_DIRECTORY,
        )
    except Exception as error:
        best_effort_diagnostic(
            "CO-OP STATUS: clear warning: " + exception_diagnostic(error),
            stderr=True,
        )


def party_layouts_ready(process) -> bool:
    battle_info = process.u64(BATTLE_INFO_GLOBAL)
    if not battle_info:
        return False
    layouts = []
    for layout_id in (1, 2):
        slots = struct.unpack(
            "<9H", process.read(battle_info + 0x5A48 + layout_id * 0x12, 0x12)
        )
        members = {value for value in slots[:4] if value != 0xFFFF}
        if len(members) < 1:
            return False
        layouts.append(members)
    return layouts[0] == layouts[1]


def available_identity(
    configured: session_state.SessionIdentity,
    current_layout,
    ui_layout,
    leader: int | None,
) -> session_state.SessionIdentity:
    identity = session_state.available_identity(
        configured, current_layout, ui_layout, leader
    )
    if identity is None:
        raise RuntimeError("party estavel nao tem nenhum membro")
    return identity


def startup_identity(
    configured: session_state.SessionIdentity,
    current_layout,
    ui_layout,
    leader: int | None,
    p1_alive: bool | None,
) -> session_state.SessionIdentity:
    transient_owner = session_state.p1_controlled_party_member_id(
        configured,
        current_layout,
        ui_layout,
        leader,
    )
    if (
        transient_owner == leader
        and leader in session_state.PARTY_NAMES
        and leader not in session_state.MAIN_PARTY_MEMBER_IDS
    ):
        return configured

    p1_present = bool(
        configured.p1_party_member_id in current_layout[:4]
        and configured.p1_party_member_id in ui_layout[:4]
    )
    if (
        session_state.ko_promoted_follower_role(
            configured,
            leader,
            p1_alive,
            p1_present=p1_present,
        )
        is not None
    ):
        return configured
    return available_identity(configured, current_layout, ui_layout, leader)


def sync_identity_to_live_party(
    process,
    actors=None,
) -> session_state.SessionIdentity:
    battle_info = process.u64(BATTLE_INFO_GLOBAL)
    current = struct.unpack("<9H", process.read(battle_info + 0x5A48 + 1 * 0x12, 0x12))
    ui = struct.unpack("<9H", process.read(battle_info + 0x5A48 + 2 * 0x12, 0x12))
    leader_value = process.u32(battle_info + session_state.BATTLE_LEADER_OFFSET)
    leader = None if leader_value == 0xFFFF else leader_value
    configured = session_state.SessionIdentity.from_config(session_state.load_config())
    p1_actor = 0
    try:
        if actors is None:
            actors = coop.resolve_active_actors(process)
        party_actor_slots = coop.resolve_active_party_actor_slots(process, actors)
        p1_actor = next(
            (
                entry[1]
                for party_slot, entry in party_actor_slots.items()
                if 0 <= int(party_slot) < 4
                and current[int(party_slot)] == configured.p1_party_member_id
            ),
            0,
        )
        p1_alive = coop.actor_is_alive(process, p1_actor)
    except (OSError, RuntimeError, struct.error, ValueError):
        p1_alive = None
    identity = startup_identity(
        configured,
        current,
        ui,
        leader,
        p1_alive,
    )
    if identity != configured:
        session_state.save_identity(identity)
        print(
            "CO-OP PARTY: personagem ausente substituido automaticamente "
            f"p1={identity.name('p1')} p2={identity.name('p2')}",
            flush=True,
        )
    return identity


def sync_identity_when_stable(
    process,
    actors=None,
    *,
    recover_children=None,
    poll_interval: float = GAME_STARTUP_POLL_INTERVAL,
) -> session_state.SessionIdentity:
    """Retry the narrow post-load identity sample across world transitions."""

    current_actors = actors
    while True:
        if not game_alive(process):
            raise RuntimeError("game exited while synchronizing co-op identity")
        if recover_children is not None:
            recover_children()
        try:
            return sync_identity_to_live_party(process, current_actors)
        except (
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            IndexError,
            struct.error,
        ):
            current_actors = None
            time.sleep(poll_interval)


def failed_child_codes(child_records) -> list[int]:
    return [
        code for child, *_rest in child_records if (code := child.poll()) is not None
    ]


def child_role(child_record) -> str:
    return child_record[3].name.removesuffix(".out.log")


ROLE_TRANSACTION_WORKERS = frozenset({"headless_menu", "movement"})


def _publish_role_admission_fence_confirmed(process, closed: bool) -> None:
    """Publish one fence edge and require exact remote readback."""

    expected = 1 if closed else 0
    write_error = None
    try:
        coop.publish_native_leader_role_shutdown_fence(process, closed)
    except (OSError, RuntimeError, struct.error, ValueError) as error:
        write_error = error
    raw = process.read(coop.NATIVE_LEADER_ROLE_SHUTDOWN_FENCE, 4)
    if not isinstance(raw, (bytes, bytearray, memoryview)) or len(raw) != 4:
        raise struct.error("role admission fence readback is unavailable")
    observed = struct.unpack("<I", raw)[0]
    if observed != expected:
        if write_error is not None:
            raise RuntimeError("role admission fence publication is ambiguous") from write_error
        raise RuntimeError(
            f"role admission fence did not persist: {observed} != {expected}"
        )


def _start_recovery_child(
    process,
    role,
    factory,
    *,
    attempts,
    backoff,
):
    last_error = None
    attempt = 0
    while attempts is None or attempt < attempts:
        attempt += 1
        if not game_alive(process):
            raise RuntimeError(f"game exited during helper {role} recovery")
        try:
            return factory(), attempt
        except (
            OSError,
            RuntimeError,
            TimeoutError,
            subprocess.SubprocessError,
        ) as error:
            last_error = error
            if (attempts is None or attempt < attempts) and backoff > 0:
                time.sleep(min(backoff * attempt, 5.0))
    raise RuntimeError(
        f"helper {role} did not recover after {attempts} attempts: {last_error}"
    )


def recover_role_transaction_workers(
    process,
    child_records,
    factories,
    *,
    attempts=None,
    backoff=0.25,
    health_checks=None,
    force_roles=(),
) -> bool:
    """Restart producer/consumer as one fenced dependency transaction.

    Headless is quiesced first, movement is replaced and proven ready first,
    and headless is admitted only while the fence remains CLOSED.  Any
    uncertainty returns/raises with admission still closed and never mutates an
    external live producer.
    """

    indexed = {
        child_role(record): (index, record)
        for index, record in enumerate(tuple(child_records))
        if child_role(record) in ROLE_TRANSACTION_WORKERS
    }
    if set(indexed) != ROLE_TRANSACTION_WORKERS:
        return False
    forced = set(force_roles) & ROLE_TRANSACTION_WORKERS
    failed = {
        role
        for role, (_index, record) in indexed.items()
        if record[0].poll() is not None
    }
    if not (failed or forced):
        return False

    _publish_role_admission_fence_confirmed(process, True)
    headless_index, headless_record = indexed["headless_menu"]
    movement_index, movement_record = indexed["movement"]
    headless = headless_record[0]
    if bool(getattr(headless, "_ff12_external", False)) and headless.poll() is None:
        # The supervisor cannot signal or prove quiescence of an inherited
        # producer.  Leave both helpers untouched and keep admission CLOSED.
        return False
    if bool(getattr(headless, "_ff12_external", False)):
        detach_child(headless_record)
    else:
        cleanup_child(headless_record, preserve_resident_runtime=True)
        if headless.poll() is None:
            raise RuntimeError("headless role producer did not quiesce")

    # The only supervisor-owned repair is the prefix before REQUEST exists.
    # General transfer adoption owns process-local publisher reservations and
    # therefore remains exclusively movement's startup responsibility.
    coop.repair_abandoned_native_leader_role_claim_for_restart(process)

    movement = movement_record[0]
    movement_restart_required = bool(
        "movement" in failed
        or "movement" in forced
        or movement.poll() is not None
    )
    if not movement_restart_required:
        checker = (health_checks or {}).get("movement")
        if checker is None or not checker(movement_record)[0]:
            # A live consumer is preserved during headless-only recovery, but
            # liveness alone is insufficient: its native hook + heartbeat must
            # be revalidated before a replacement producer can start.
            return False
        replacement_movement = movement_record
        movement_attempt = 0
    else:
        if bool(getattr(movement, "_ff12_external", False)):
            detach_child(movement_record)
        else:
            cleanup_child(movement_record, preserve_resident_runtime=True)
            if movement.poll() is None:
                raise RuntimeError("movement role consumer did not quiesce")
        replacement_movement, movement_attempt = _start_recovery_child(
            process,
            "movement",
            factories["movement"],
            attempts=attempts,
            backoff=backoff,
        )
        child_records[movement_index] = replacement_movement

    # A fresh movement helper opens admission after its first heartbeat.  No
    # producer exists yet, but close it again before starting headless so the
    # supervisor owns the final OPEN edge.
    _publish_role_admission_fence_confirmed(process, True)
    replacement_headless, headless_attempt = _start_recovery_child(
        process,
        "headless_menu",
        factories["headless_menu"],
        attempts=attempts,
        backoff=backoff,
    )
    child_records[headless_index] = replacement_headless

    checker = (health_checks or {}).get("movement")
    if checker is not None:
        ready, diagnostic = checker(replacement_movement)
        if not ready:
            cleanup_child(
                replacement_headless,
                preserve_resident_runtime=True,
            )
            raise RuntimeError(
                "replacement movement consumer is not ready: " + diagnostic
            )
    _publish_role_admission_fence_confirmed(process, False)
    print(
        "CO-OP ROLE WORKERS RECOVERED "
        f"movement_attempt={movement_attempt} "
        f"headless_attempt={headless_attempt}",
        flush=True,
    )
    return True


def recover_failed_children(
    process,
    child_records,
    factories,
    *,
    attempts: int | None = None,
    backoff: float = 0.25,
    health_checks=None,
) -> bool:
    """Replace exited helpers in-place so every observer sees the new record.

    Production supervision retries for as long as the game is alive. Tests and
    explicit diagnostic callers may provide a finite attempt limit.
    """

    if attempts is not None and attempts < 1:
        raise ValueError("helper recovery attempts must be positive")
    pair_present = ROLE_TRANSACTION_WORKERS.issubset(
        {child_role(record) for record in child_records}
    )
    pair_failed = bool(
        pair_present
        and any(
            child_role(record) in ROLE_TRANSACTION_WORKERS
            and record[0].poll() is not None
            for record in child_records
        )
    )
    recovered = False
    if pair_failed:
        recovered = recover_role_transaction_workers(
            process,
            child_records,
            factories,
            attempts=attempts,
            backoff=backoff,
            health_checks=health_checks,
        )
    for index, child_record in enumerate(tuple(child_records)):
        code = child_record[0].poll()
        if code is None:
            continue
        role = child_role(child_record)
        if pair_failed and role in ROLE_TRANSACTION_WORKERS:
            # A blocked external/quiescence case deliberately leaves the pair
            # fenced for a later coordinated retry.  Independent replacement
            # would reopen the original producer/consumer race.
            continue
        try:
            factory = factories[role]
        except KeyError as error:
            raise RuntimeError(f"helper recovery factory missing: {role}") from error
        detail = child_failure_detail(child_record)
        cleanup_child(child_record)
        last_error = None
        attempt = 0
        while attempts is None or attempt < attempts:
            attempt += 1
            if not game_alive(process):
                raise RuntimeError("game exited during helper recovery")
            try:
                replacement = factory()
            except (
                OSError,
                RuntimeError,
                TimeoutError,
                subprocess.SubprocessError,
            ) as error:
                last_error = error
                if (attempts is None or attempt < attempts) and backoff > 0:
                    time.sleep(min(backoff * attempt, 5.0))
                continue
            child_records[index] = replacement
            recovered = True
            print(
                f"CO-OP HELPER RECOVERED role={role} "
                f"exit_code={code} attempt={attempt}",
                flush=True,
            )
            break
        else:
            suffix = f" detail={detail}" if detail else ""
            raise RuntimeError(
                f"helper {role} did not recover after {attempts} attempts; "
                f"exit_code={code}{suffix}: {last_error}"
            )
    return recovered


def supervise_children(
    process,
    child_records,
    factories,
    health_checks,
    unhealthy_since,
    *,
    now: float | None = None,
    stall_grace: float = HELPER_STALL_GRACE_SECONDS,
    restart_live_stalls: bool = False,
) -> bool:
    """Recover exited helpers; observe live stalls without guessing death.

    A single failed health sample is deliberately ignored.  Loading, a modal,
    or a short remote-memory retry can delay a worker without invalidating it.
    More importantly, a stale heartbeat does not prove that a still-live worker
    is dead: process scheduling, a debugger, a fatal game modal and CPU-heavy
    validation can all stop the pulse for longer than the grace interval.  The
    production supervisor therefore never terminates a live helper from this
    observation alone.  Explicit diagnostics may opt into replacement with
    ``restart_live_stalls=True``; exited helpers are always recovered.
    """

    if stall_grace < 0:
        raise ValueError("helper stall grace must not be negative")
    recovered = recover_failed_children(
        process,
        child_records,
        factories,
        health_checks=health_checks,
    )
    sampled_at = time.monotonic() if now is None else float(now)
    restart_records = []
    live_roles = set()

    for child_record in tuple(child_records):
        child = child_record[0]
        if child.poll() is not None:
            continue
        role = child_role(child_record)
        live_roles.add(role)
        checker = health_checks.get(role)
        if checker is None:
            unhealthy_since.pop(role, None)
            continue
        try:
            healthy, diagnostic = checker(child_record)
        except (
            OSError,
            RuntimeError,
            TimeoutError,
            ValueError,
            KeyError,
            IndexError,
            struct.error,
        ) as error:
            healthy = False
            diagnostic = f"{type(error).__name__}: {error}"

        child_pid = int(getattr(child, "pid", 0) or 0)
        if healthy:
            unhealthy_since.pop(role, None)
            continue
        prior = unhealthy_since.get(role)
        if prior is None or prior[0] != child_pid:
            unhealthy_since[role] = (child_pid, sampled_at, diagnostic)
            continue
        first_unhealthy = prior[1]
        unhealthy_since[role] = (child_pid, first_unhealthy, diagnostic)
        if sampled_at - first_unhealthy < stall_grace:
            continue
        if not restart_live_stalls or role in COOPERATIVE_ONLY_CHILD_ROLES:
            # No cooperative-only role currently publishes a watchdog pulse.
            # Keep this invariant explicit if one is added later: a stale
            # observation can never justify orphaning its native remote call.
            # The same fail-safe is the production default for every live
            # helper: health telemetry is evidence for diagnostics, not proof
            # that destructive replacement is safe.
            continue
        restart_records.append((role, child_record, diagnostic))

    for role in tuple(unhealthy_since):
        if role not in live_roles:
            unhealthy_since.pop(role, None)

    pair_restart_roles = {
        role
        for role, _child_record, _diagnostic in restart_records
        if role in ROLE_TRANSACTION_WORKERS
    }
    if pair_restart_roles and ROLE_TRANSACTION_WORKERS.issubset(
        {child_role(record) for record in child_records}
    ):
        for role, _child_record, diagnostic in restart_records:
            if role in pair_restart_roles:
                print(
                    f"CO-OP HELPER STALLED role={role}; "
                    f"coordinated restart: {diagnostic}",
                    file=sys.stderr,
                    flush=True,
                )
        pair_recovered = recover_role_transaction_workers(
            process,
            child_records,
            factories,
            health_checks=health_checks,
            force_roles=pair_restart_roles,
        )
        if pair_recovered:
            recovered = True
            for role in ROLE_TRANSACTION_WORKERS:
                unhealthy_since.pop(role, None)
        restart_records = [
            entry
            for entry in restart_records
            if entry[0] not in ROLE_TRANSACTION_WORKERS
        ]

    for role, child_record, diagnostic in restart_records:
        print(
            f"CO-OP HELPER STALLED role={role}; restarting: {diagnostic}",
            file=sys.stderr,
            flush=True,
        )
        cleanup_child(child_record)
        unhealthy_since.pop(role, None)

    if restart_records:
        recovered = (
            recover_failed_children(
                process,
                child_records,
                factories,
                health_checks=health_checks,
            )
            or recovered
        )
    return recovered


def start_required_child(
    process,
    role: str,
    factory,
    *,
    backoff: float = HELPER_RECOVERY_BACKOFF_SECONDS,
):
    """Keep starting one required helper while the selected game is alive."""

    attempt = 0
    while True:
        if not game_alive(process):
            raise RuntimeError(f"game exited while helper {role} was starting")
        attempt += 1
        try:
            return factory()
        except (
            OSError,
            RuntimeError,
            TimeoutError,
            subprocess.SubprocessError,
        ) as error:
            print(
                "CO-OP HELPER: waiting to recover "
                f"{role} attempt={attempt}: {type(error).__name__}: {error}",
                file=sys.stderr,
                flush=True,
            )
            if backoff > 0:
                time.sleep(min(backoff * attempt, 5.0))


def wait_required_component(
    process,
    label: str,
    waiter,
    *,
    backoff: float = HELPER_RECOVERY_BACKOFF_SECONDS,
) -> None:
    """Keep a bootstrap component armed across temporary readiness loss."""

    attempt = 0
    while True:
        if not game_alive(process):
            raise RuntimeError(f"game exited while waiting for {label}")
        attempt += 1
        try:
            waiter()
            return
        except (
            OSError,
            RuntimeError,
            TimeoutError,
            ValueError,
            struct.error,
        ) as error:
            print(
                f"CO-OP COMPONENT WAIT label={label} attempt={attempt}: "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
                flush=True,
            )
            if backoff > 0:
                time.sleep(min(backoff * attempt, 5.0))


def wait_gameplay(
    process,
    timeout: float | None = None,
    child_records=(),
    recover_children=None,
):
    deadline = None if timeout is None else time.monotonic() + timeout
    stable_since = None
    while deadline is None or time.monotonic() < deadline:
        if not game_alive(process):
            raise RuntimeError("jogo encerrou antes do co-op ficar disponivel")
        if recover_children is not None:
            recover_children()
        else:
            failed = failed_child_codes(child_records)
            if not failed:
                failed = None
        if recover_children is None and failed:
            raise RuntimeError(
                f"helper do co-op encerrou durante o Load Game: {failed}"
            )
        try:
            actors = coop.resolve_active_actors(process)
            party_actor_slots = coop.resolve_active_party_actor_slots(
                process,
                actors,
            )
            ready = bool(party_actor_slots) and party_layouts_ready(process)
        except (
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            IndexError,
            struct.error,
        ):
            # Actor arrays and party layouts are replaced independently while
            # loading.  A mixed or unreadable sample is ordinary transition
            # state, not evidence that the supervisor or helpers failed.
            actors = []
            ready = False
        if ready:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= 2.0:
                return actors
        else:
            stable_since = None
        time.sleep(0.1)
    raise RuntimeError("timeout esperando um personagem ativo")


def open_child_log_streams(log_dir: Path, role: str):
    """Open readiness logs, falling back without weakening marker proof."""

    primary = Path(log_dir)
    candidates = [primary]
    try:
        fallback = (
            Path(tempfile.gettempdir())
            / "Ivalice Together"
            / "runtime_logs_fallback"
            / primary.name
        )
        if fallback != primary:
            candidates.append(fallback)
    except (OSError, RuntimeError, TypeError, ValueError):
        pass

    first_error: Exception | None = None
    for directory in candidates:
        stdout = None
        stderr = None
        stdout_path = directory / f"{role}.out.log"
        stderr_path = directory / f"{role}.err.log"
        shutdown_path = directory / f"{role}.shutdown"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            try:
                shutdown_path.unlink()
            except FileNotFoundError:
                pass
            stdout = stdout_path.open("ab", buffering=0)
            stderr = stderr_path.open("ab", buffering=0)
            try:
                stdout_start = os.fstat(stdout.fileno()).st_size
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                try:
                    stdout_start = stdout_path.stat().st_size
                except (OSError, RuntimeError, TypeError, ValueError):
                    stdout_start = 0
            try:
                stderr_start = os.fstat(stderr.fileno()).st_size
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                try:
                    stderr_start = stderr_path.stat().st_size
                except (OSError, RuntimeError, TypeError, ValueError):
                    stderr_start = 0
            return (
                stdout,
                stderr,
                stdout_path,
                stderr_path,
                shutdown_path,
                stdout_start,
                stderr_start,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            if first_error is None:
                first_error = error
            for stream in (stdout, stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
    if first_error is not None:
        raise OSError(
            "no child log directory is available: "
            f"{exception_diagnostic(first_error)}"
        ) from first_error
    raise OSError("no child log directory is available")


def stream_file_identity(stream) -> tuple[int, int] | None:
    """Return a stable file identity when the host filesystem exposes one."""

    try:
        info = os.fstat(stream.fileno())
        inode = int(info.st_ino)
        if not inode:
            return None
        return int(info.st_dev), inode
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return None


def start_child(log_dir: Path, role: str, script: str, *args: str):
    (
        stdout,
        stderr,
        stdout_path,
        stderr_path,
        shutdown_path,
        stdout_start,
        stderr_start,
    ) = open_child_log_streams(log_dir, role)
    try:
        try:
            runtime_mode = SCRIPT_RUNTIME_MODES[script]
        except KeyError as error:
            raise ValueError(f"helper runtime desconhecido: {script}") from error
        command = runtime_entry.worker_command(
            runtime_mode,
            HERE / script,
            *args,
        )
        environment = os.environ.copy()

        environment.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
        environment[runtime_paths.GAME_ROOT_ENV] = str(ROOT)
        environment[runtime_paths.DATA_DIRECTORY_ENV] = str(DATA_DIRECTORY)
        environment["FF12_COOP_STDOUT_LOG"] = str(stdout_path)
        environment["FF12_COOP_STDERR_LOG"] = str(stderr_path)
        environment[runtime_shutdown.SHUTDOWN_FILE_ENV] = str(shutdown_path)
        environment.pop(controller_state_transport.ROLE_ENV, None)
        if role == "movement":
            environment[controller_state_transport.ROLE_ENV] = (
                controller_state_transport.ROLE_WRITER
            )
        elif role == "headless_menu":
            environment[controller_state_transport.ROLE_ENV] = (
                controller_state_transport.ROLE_READER
            )
        if role == "headless_menu":
            environment["FF12_COOP_BOOTSTRAP_ACTIVE"] = "1"
        child = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=CREATE_NO_WINDOW,
            env=environment,
        )

        child._ff12_stdout_start = stdout_start
        child._ff12_stderr_start = stderr_start
        child._ff12_shutdown_path = shutdown_path
        child._ff12_stdout_identity = stream_file_identity(stdout)
    except Exception:
        for stream in (stdout, stderr):
            try:
                stream.close()
            except Exception:
                pass
        raise
    return child, stdout, stderr, stdout_path, stderr_path


def external_child_record(log_dir: Path, game_pid: int, role: str):
    """Describe an inherited helper without taking destructive ownership."""

    # An inherited helper is proven by its process-lifetime lease, not by its
    # old diagnostic directory.  The paths may remain unreadable; callers
    # already treat missing failure detail as empty, while retaining the role
    # encoded in the filename.
    stdout_path = log_dir / f"{role}.out.log"
    stderr_path = log_dir / f"{role}.err.log"
    child = ExternalHelper(game_pid, role)
    child._ff12_stdout_start = 0
    child._ff12_stderr_start = 0
    return child, _ExternalStream(), _ExternalStream(), stdout_path, stderr_path


def start_or_attach_required_child(
    process,
    game_pid: int,
    log_dir: Path,
    role: str,
    factory,
    *,
    backoff: float = HELPER_RECOVERY_BACKOFF_SECONDS,
):
    """Attach to one live lease or persistently start the missing helper."""

    attempt = 0
    while True:
        if not game_alive(process):
            raise RuntimeError(f"game exited while helper {role} was reconnecting")
        try:
            if helper_lease.active(game_pid, role):
                print(f"CO-OP HELPER ADOPTED role={role}", flush=True)
                return external_child_record(log_dir, game_pid, role)
        except OSError as error:
            # Starting a possible duplicate is less safe than retaining an
            # uncertain owner.  Retry the read-only lease probe indefinitely.
            attempt += 1
            print(
                f"CO-OP HELPER LEASE RETRY role={role} "
                f"attempt={attempt} error={error}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(min(max(backoff, 0.01) * attempt, 5.0))
            continue
        try:
            return factory()
        except (
            OSError,
            RuntimeError,
            TimeoutError,
            subprocess.SubprocessError,
        ) as error:
            attempt += 1
            print(
                f"CO-OP HELPER RECONNECT RETRY role={role} "
                f"attempt={attempt} error={type(error).__name__}: {error}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(min(max(backoff, 0.01) * attempt, 5.0))


def reset_session_logs(log_dir: Path) -> None:
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        # start_child selects a writable fallback directory.  Clearing old
        # diagnostics in the preferred directory is never a startup gate.
        return
    try:
        stale_logs = log_dir.glob("*.log")
        for stale in stale_logs:
            try:
                stale.unlink()
            except (OSError, RuntimeError, TypeError, ValueError):
                pass
    except (OSError, RuntimeError, TypeError, ValueError):
        pass


def child_failure_detail(child_record, limit: int = 600) -> str:
    child, _stdout, _stderr, _stdout_path, stderr_path = child_record
    start_offset = int(getattr(child, "_ff12_stderr_start", 0))
    try:
        with stderr_path.open("rb") as reader:
            reader.seek(0, os.SEEK_END)
            end_offset = reader.tell()
            reader.seek(
                max(start_offset, end_offset - CHILD_LOG_READ_CHUNK_SIZE)
            )
            material = reader.read(CHILD_LOG_READ_CHUNK_SIZE).decode(
                "utf-8",
                errors="replace",
            )
    except (OSError, RuntimeError, TypeError, ValueError):
        return ""
    lines = [line.strip() for line in material.splitlines() if line.strip()]
    if not lines:
        return ""
    detail = next(
        (
            line
            for line in reversed(lines)
            if any(
                marker in line
                for marker in (
                    "OSError:",
                    "RuntimeError:",
                    "TimeoutError:",
                    "ValueError:",
                    "FileNotFoundError:",
                    "ModuleNotFoundError:",
                )
            )
        ),
        lines[-1],
    )
    return detail[-limit:]


def cleanup_child(child_record, *, preserve_resident_runtime: bool = False) -> None:
    child, stdout, stderr, stdout_path, _stderr_path = child_record
    if bool(getattr(child, "_ff12_external", False)):
        # An inherited helper is not our child and may still be executing
        # native process-lifetime code.  Closing local placeholders is the only
        # authorized cleanup; never publish its shutdown file or terminate it.
        for stream in (stdout, stderr):
            try:
                stream.close()
            except Exception:
                pass
        return
    role = stdout_path.name.removesuffix(".out.log")
    shutdown_path = Path(
        getattr(
            child,
            "_ff12_shutdown_path",
            stdout_path.with_name(f"{role}.shutdown"),
        )
    )
    try:
        if child.poll() is None:
            try:
                if preserve_resident_runtime:
                    shutdown_path.write_text(
                        runtime_shutdown.PRESERVE_RESIDENT_RUNTIME_MARKER,
                        encoding="utf-8",
                    )
                else:
                    shutdown_path.touch()
                while True:
                    try:
                        child.wait(timeout=CHILD_COOPERATIVE_SHUTDOWN_TIMEOUT)
                        break
                    except subprocess.TimeoutExpired:
                        if role not in COOPERATIVE_ONLY_CHILD_ROLES:
                            raise
                        # penelo_guard may be waiting for native code executing
                        # from its shared workspace. Killing the Python owner
                        # would orphan that game thread and let a replacement
                        # overwrite instructions still in use. Keep waiting;
                        # game exit also signals the native thread and releases
                        # the guard.
            except subprocess.TimeoutExpired:
                child.terminate()
                try:
                    child.wait(timeout=CHILD_TERMINATE_TIMEOUT)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=CHILD_TERMINATE_TIMEOUT)
    finally:
        try:
            shutdown_path.unlink()
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        for stream in (stdout, stderr):
            try:
                stream.close()
            except Exception:
                pass


def cleanup_children(child_records) -> None:
    """Quiesce the role-transfer producer before the movement consumer.

    ``headless_menu`` is the only process allowed to begin a native leader
    role transaction.  The movement helper may clear shared ownership gates
    during its own cooperative shutdown, so it must be stopped last and only
    after every supervisor-owned producer has verifiably exited.  An inherited
    (external) headless helper can only be detached, never signalled; in that
    case movement is detached as well and its remote state remains untouched.
    """

    records = tuple(child_records)

    def role_of(record) -> str | None:
        try:
            return child_role(record)
        except (AttributeError, IndexError, TypeError, ValueError):
            # Preserve the generic cleanup helper contract used by diagnostic
            # callers while reserving strict ordering for real child records.
            return None

    headless_records = [
        record for record in records if role_of(record) == "headless_menu"
    ]
    movement_records = [record for record in records if role_of(record) == "movement"]
    ordinary_records = [
        record
        for record in records
        if role_of(record) not in ("headless_menu", "movement")
    ]
    first_error = None

    producer_quiesced = True
    for child_record in reversed(headless_records):
        if bool(getattr(child_record[0], "_ff12_external", False)):
            # We do not own this producer and therefore cannot prove it has
            # stopped.  Detach local placeholders without touching movement.
            detach_child(child_record)
            producer_quiesced = False
            continue
        try:
            cleanup_child(child_record)
        except Exception as error:
            producer_quiesced = False
            if first_error is None:
                first_error = error

    for child_record in reversed(ordinary_records):
        try:
            cleanup_child(child_record)
        except Exception as error:
            if first_error is None:
                first_error = error

    if producer_quiesced:
        for child_record in reversed(movement_records):
            try:
                cleanup_child(child_record)
            except Exception as error:
                if first_error is None:
                    first_error = error
    else:
        # Closing supervisor-owned streams is safe; invoking cleanup_child is
        # not, because it could ask movement to disarm while a producer is
        # still able to publish a role transfer after movement's last sample.
        for child_record in movement_records:
            detach_child(child_record)

    if first_error is not None:
        raise first_error


def detach_child(child_record) -> None:
    """Release only supervisor-owned handles, leaving a live helper intact."""

    _child, stdout, stderr, _stdout_path, _stderr_path = child_record
    for stream in (stdout, stderr):
        try:
            stream.close()
        except Exception:
            pass


def detach_children(child_records) -> None:
    for child_record in reversed(child_records):
        detach_child(child_record)


def retire_supervised_children(
    process,
    child_records,
    *,
    resident_committed: bool,
) -> None:
    """Stop pre-commit children, but preserve committed helpers on uncertainty."""

    if resident_committed and game_alive(process):
        # Native code and helper leases deliberately outlive their supervisor.
        # A Python-side exception is not authority to publish shutdown files or
        # terminate otherwise healthy workers.  Close only this process's log
        # handles; a replacement supervisor can observe and adopt every lease.
        detach_children(child_records)
        print(
            "CO-OP SUPERVISOR DETACHED; committed helpers retained",
            file=sys.stderr,
            flush=True,
        )
        return
    cleanup_children(child_records)


def wait_child_ready(
    child_record,
    marker: str,
    *,
    timeout: float = CHILD_READY_TIMEOUT,
    poll_interval: float = CHILD_READY_POLL_INTERVAL,
) -> None:
    child, _stdout, _stderr, stdout_path, _stderr_path = child_record
    start_offset = max(0, int(getattr(child, "_ff12_stdout_start", 0)))
    expected_identity = getattr(child, "_ff12_stdout_identity", None)
    marker_bytes = marker.encode("utf-8")
    deadline = time.monotonic() + timeout
    observed = b""
    next_offset = start_offset
    last_observation_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            code = child.poll()
        except (OSError, RuntimeError) as error:
            # A failed process observation cannot prove that the child exited.
            code = None
            last_observation_error = error
        if code is not None:
            detail = child_failure_detail(child_record)
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(
                f"helper encerrou com codigo {code} antes de publicar {marker}"
                f"{suffix}"
            )

        reader = None
        marker_seen = False
        try:
            reader = stdout_path.open("rb")
            current_identity = stream_file_identity(reader)
            if (
                expected_identity is not None
                and current_identity is not None
                and current_identity != expected_identity
            ):
                raise OSError("readiness log was replaced after child spawn")
            try:
                current_size = os.fstat(reader.fileno()).st_size
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                current_size = None
            if current_size is not None and current_size < next_offset:
                raise OSError("readiness log was truncated after child spawn")
            reader.seek(next_offset)
            chunk = reader.read(CHILD_LOG_READ_CHUNK_SIZE)
            if chunk is None:
                chunk = b""
            if not isinstance(chunk, bytes):
                raise TypeError("readiness log returned non-byte material")
            if chunk:
                next_offset += len(chunk)
                observed = (observed + chunk)[-max(4096, len(marker_bytes) * 2) :]
                if marker_bytes in observed:
                    marker_seen = True
            last_observation_error = None
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            # Diagnostic storage can disappear briefly (antivirus, profile
            # remount, fallback-volume hiccup).  Keep observing the exact child
            # and reopen the same file; never turn that uncertainty into leave-
            # one-running-and-spawn-another behavior.
            last_observation_error = error
        finally:
            if reader is not None:
                try:
                    reader.close()
                except Exception as error:
                    if not marker_seen:
                        last_observation_error = error
        if marker_seen:
            return
        time.sleep(poll_interval)
    suffix = ""
    if last_observation_error is not None:
        suffix = "; observacao indisponivel: " + exception_diagnostic(
            last_observation_error
        )
    raise TimeoutError(f"helper nao publicou prontidao: {marker}{suffix}")


def start_ready_child(
    log_dir: Path,
    role: str,
    script: str,
    marker: str,
    *args: str,
    attempts: int = CHILD_START_ATTEMPTS,
    game_process=None,
):
    if attempts < 1:
        raise ValueError("child start attempts must be positive")
    last_error: Exception | None = None
    for attempt in range(attempts):
        child_record = start_child(log_dir, role, script, *args)
        while True:
            try:
                wait_child_ready(child_record, marker)
                return child_record
            except (OSError, RuntimeError, TimeoutError) as error:
                last_error = error
                try:
                    exit_code = child_record[0].poll()
                except (OSError, RuntimeError):
                    exit_code = None
                if exit_code is None:
                    if game_process is not None and not game_alive(game_process):
                        cleanup_child(child_record)
                        raise RuntimeError(
                            f"game exited while helper {role} was publishing "
                            "readiness"
                        ) from error
                    best_effort_diagnostic(
                        "CO-OP HELPER: mantendo PID vivo enquanto a prontidao "
                        f"de {role} segue incerta: {exception_diagnostic(error)}",
                        stderr=True,
                    )
                    # ``wait_child_ready`` normally consumed a complete timeout
                    # window.  The short delay also prevents a mocked or broken
                    # observer from becoming a hot loop.
                    if CHILD_READY_POLL_INTERVAL > 0:
                        time.sleep(CHILD_READY_POLL_INTERVAL)
                    continue
                # Only a successful poll with a concrete exit code authorizes
                # cleanup and creation of a replacement process.
                cleanup_child(child_record)
                if attempt + 1 < attempts:
                    best_effort_diagnostic(
                        f"CO-OP HELPER: reiniciando {role} apos saida "
                        f"comprovada ({exit_code})"
                    )
                break
    if last_error is None:
        raise RuntimeError(f"helper {role} failed without a startup diagnostic")
    raise last_error


def expected_movement_hook_patch(base: int) -> bytes:
    return b"\xe9" + coop.rel32(
        coop.MOVEMENT_HOOK_SITE,
        5,
        base + coop.MOVEMENT_CAVE_OFFSET,
    )


def start_movement_helper(
    process,
    base: int,
    log_dir: Path,
    movement_args,
    *,
    attempts: int = MOVEMENT_START_ATTEMPTS,
    ready_timeout: float = MOVEMENT_READY_TIMEOUT,
    poll_interval: float = MOVEMENT_READY_POLL_INTERVAL,
):
    if attempts < 1:
        raise ValueError("movement attempts must be positive")
    preserve_resident_runtime = "--preserve-resident-state" in movement_args

    def cleanup_movement(record):
        if preserve_resident_runtime:
            cleanup_child(record, preserve_resident_runtime=True)
        else:
            cleanup_child(record)

    movement_patch = expected_movement_hook_patch(base)
    failures = []
    for attempt in range(1, attempts + 1):
        if not game_alive(process):
            raise RuntimeError("jogo encerrou antes do movimento P2 ficar pronto")

        reset_movement_helper_ready(process, base)
        try:
            movement = start_child(
                log_dir, "movement", "coop_runtime.py", *movement_args
            )
        except (OSError, subprocess.SubprocessError) as error:
            failure = f"nao iniciou ({type(error).__name__}: {error})"
            failures.append(f"tentativa {attempt}: {failure}")
            if attempt < attempts:
                print(
                    "CO-OP MOVEMENT: tentativa "
                    f"{attempt}/{attempts} falhou; reiniciando helper",
                    file=sys.stderr,
                    flush=True,
                )
            continue
        deadline = time.monotonic() + ready_timeout
        failure = None
        while True:
            code = movement[0].poll()
            if code is not None:
                failure = f"helper encerrou com codigo {code}"
                detail = child_failure_detail(movement)
                if detail:
                    failure += f": {detail}"
                else:
                    failure += f"; consulte {movement[4]}"
                break
            if not game_alive(process):
                cleanup_movement(movement)
                raise RuntimeError("jogo encerrou enquanto o movimento P2 iniciava")
            deadline_reached = time.monotonic() >= deadline
            ready, diagnostic = movement_readiness_snapshot(
                process,
                base,
                movement_patch,
                expected_owner_pid=getattr(movement[0], "pid", None),
            )
            if ready:
                return movement
            if deadline_reached:
                print(
                    "CO-OP MOVEMENT TIMEOUT "
                    f"tentativa={attempt}/{attempts} {diagnostic}",
                    file=sys.stderr,
                    flush=True,
                )
                failure = f"timeout ({diagnostic})"
                break
            time.sleep(poll_interval)

        cleanup_movement(movement)
        failures.append(f"tentativa {attempt}: {failure}")
        if attempt < attempts:
            print(
                "CO-OP MOVEMENT: tentativa "
                f"{attempt}/{attempts} falhou; reiniciando helper",
                file=sys.stderr,
                flush=True,
            )

    raise RuntimeError(
        "movimento P2 nao ficou pronto apos "
        f"{attempts} tentativas: " + "; ".join(failures)
    )


def materialize_runtime_dll(
    source: Path,
    destination_root: Path = RUNTIME_BINARY_DIRECTORY,
) -> Path:
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(source)
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()[:12]
    destination_root = Path(destination_root)
    destination = destination_root / (f"{source.stem}-{digest}{source.suffix.lower()}")
    if destination.is_file() and destination.read_bytes() == payload:
        return destination

    destination_root.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        atomic_files.replace_file(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    return destination


def materialize_render_dll(
    source: Path = RENDER_DLL,
    destination_root: Path = RUNTIME_BINARY_DIRECTORY,
) -> Path:
    return materialize_runtime_dll(source, destination_root)


def _runtime_cave_publications(process) -> dict[str, int]:
    publications = (
        (runtime_paths.CAVE_BASE_ENV, coop.CAVE_MAGIC),
        (runtime_paths.DUAL_CAVE_BASE_ENV, coop.DUAL_INTERACTION_MAGIC),
        (runtime_paths.DAMAGE_CAVE_BASE_ENV, coop.DAMAGE_NUMBER_THEME_MAGIC),
    )
    bases: dict[str, int] = {}
    for variable, magic in publications:
        candidate = coop.published_allocation_candidate(process, variable, magic)
        if not candidate:
            candidate = coop.find_rel32_magic_allocation(process, magic)
        if candidate:
            bases[variable] = candidate
    return bases


def global_install_feature_bits(*, upstream_camera_v2: bool) -> int:
    return (
        int(bool(upstream_camera_v2))
        * GLOBAL_INSTALL_FEATURE_UPSTREAM_CAMERA_V2
        | int(bool(story_party_patch.START_WITH_ALL))
        * GLOBAL_INSTALL_FEATURE_START_WITH_ALL
    )


def _global_install_build_id(feature_bits: int) -> bytes:
    digest = hashlib.blake2s(digest_size=16, person=b"ITGLB002")
    digest.update(GLOBAL_INSTALL_LAYOUT_ID)
    digest.update(struct.pack("<II", int(feature_bits), GLOBAL_INSTALL_COMPONENT_MASK))
    return digest.digest()


def global_install_marker(*, state: int, feature_bits: int) -> bytes:
    return GLOBAL_INSTALL_MARKER_STRUCT.pack(
        GLOBAL_INSTALL_MARKER_MAGIC,
        GLOBAL_INSTALL_MARKER_VERSION,
        int(state),
        int(feature_bits),
        GLOBAL_INSTALL_COMPONENT_MASK,
        GLOBAL_INSTALL_LAYOUT_ID,
        _global_install_build_id(feature_bits),
    )


def _ordered_runtime_bases(publications: dict[str, int]) -> tuple[int, int, int]:
    return (
        int(publications.get(runtime_paths.CAVE_BASE_ENV, 0)),
        int(publications.get(runtime_paths.DUAL_CAVE_BASE_ENV, 0)),
        int(publications.get(runtime_paths.DAMAGE_CAVE_BASE_ENV, 0)),
    )


def _read_global_install_markers(
    process,
    bases: tuple[int, int, int],
) -> tuple[bytes, bytes, bytes]:
    try:
        payloads = tuple(
            process.read(
                base + GLOBAL_INSTALL_MARKER_OFFSET,
                GLOBAL_INSTALL_MARKER_STRUCT.size,
            )
            for base in bases
        )
    except (OSError, RuntimeError, ValueError, struct.error) as error:
        raise RuntimeError(
            "global runtime headers are unreadable; restart the game"
        ) from error
    if any(len(payload) != GLOBAL_INSTALL_MARKER_STRUCT.size for payload in payloads):
        raise RuntimeError("global runtime header is truncated; restart the game")
    return payloads


def _assert_pristine_local_headers(
    process,
    bases: tuple[int, int, int],
) -> None:
    try:
        payloads = tuple(
            process.read(
                base + coop.RUNTIME_INSTALL_MARKER_OFFSET,
                coop.RUNTIME_INSTALL_MARKER_STRUCT.size,
            )
            for base in bases
        )
    except (OSError, RuntimeError, ValueError, struct.error) as error:
        raise RuntimeError(
            "local runtime headers are unreadable; restart the game"
        ) from error
    empty = b"\x00" * coop.RUNTIME_INSTALL_MARKER_STRUCT.size
    if any(payload != empty for payload in payloads):
        raise RuntimeError(
            "global marker is absent from an installed runtime; restart the game"
        )


def _assert_pristine_allocation_images(
    process,
    bases: tuple[int, int, int],
) -> None:
    allocations = (
        (bases[0], coop.CAVE_ALLOCATION_SIZE, coop.CAVE_MAGIC),
        (
            bases[1],
            coop.DUAL_INTERACTION_ALLOCATION_SIZE,
            coop.DUAL_INTERACTION_MAGIC,
        ),
        (
            bases[2],
            coop.DAMAGE_NUMBER_THEME_ALLOCATION_SIZE,
            coop.DAMAGE_NUMBER_THEME_MAGIC,
        ),
    )
    try:
        for base, size, magic in allocations:
            image = process.read(base, size)
            if len(image) != size or image[: len(magic)] != magic:
                raise RuntimeError("runtime allocation signature differs")
            if any(image[len(magic) :]):
                raise RuntimeError("runtime allocation contains resident state")
    except (OSError, RuntimeError, ValueError, struct.error) as error:
        raise RuntimeError(
            "zero-marker runtime allocations are not pristine; restart the game"
        ) from error


def classify_global_install(
    process,
    *,
    upstream_camera_v2: bool,
) -> GlobalInstallPreflight:
    """Classify startup without allocating, writing, injecting, or spawning."""

    publications = _runtime_cave_publications(process)
    bases = _ordered_runtime_bases(publications)
    feature_bits = global_install_feature_bits(
        upstream_camera_v2=upstream_camera_v2
    )
    present_count = sum(bool(base) for base in bases)
    if present_count == 0:
        return GlobalInstallPreflight("fresh", bases, feature_bits)
    if present_count != len(bases):
        raise RuntimeError(
            "partial global runtime allocation set found; restart the game"
        )

    payloads = _read_global_install_markers(process, bases)
    empty = b"\x00" * GLOBAL_INSTALL_MARKER_STRUCT.size
    if all(payload == empty for payload in payloads):
        _assert_pristine_local_headers(process, bases)
        _assert_pristine_allocation_images(process, bases)
        return GlobalInstallPreflight("fresh", bases, feature_bits)
    if len(set(payloads)) != 1:
        raise RuntimeError("global runtime triple commit is partial; restart the game")

    (
        magic,
        version,
        state,
        resident_features,
        component_mask,
        layout_id,
        build_id,
    ) = GLOBAL_INSTALL_MARKER_STRUCT.unpack(payloads[0])
    expected_build_id = _global_install_build_id(feature_bits)
    if (
        magic != GLOBAL_INSTALL_MARKER_MAGIC
        or version != GLOBAL_INSTALL_MARKER_VERSION
        or resident_features != feature_bits
        or component_mask != GLOBAL_INSTALL_COMPONENT_MASK
        or layout_id != GLOBAL_INSTALL_LAYOUT_ID
        or build_id != expected_build_id
    ):
        raise RuntimeError(
            "global runtime layout/build differs; restart the game before upgrading"
        )
    if state == GLOBAL_INSTALL_PREPARED:
        raise RuntimeError(
            "global runtime installation was interrupted while PREPARED; "
            "restart the game"
        )
    if state != GLOBAL_INSTALL_COMMITTED:
        raise RuntimeError("global runtime is uncommitted; restart the game")
    return GlobalInstallPreflight("adopt", bases, feature_bits)


def _renderer_module_present(pid: int) -> tuple[bool, str]:
    modules = loaded_module_names(pid)
    source_name = RENDER_DLL.name.casefold()
    materialized_prefix = f"{RENDER_DLL.stem.casefold()}-"
    matches = sorted(
        name
        for name in modules
        if name == source_name
        or (name.startswith(materialized_prefix) and name.endswith(".dll"))
    )
    return bool(matches), ",".join(matches) if matches else "ausente"


class _NativeInstallReadView:
    """Read-only overlay used to validate a signed dynamic hook chain."""

    def __init__(self, process, overlays: dict[int, bytes]) -> None:
        self.process = process
        self.overlays = {int(address): bytes(payload) for address, payload in overlays.items()}

    def __getattr__(self, name):
        return getattr(self.process, name)

    def read(self, address: int, size: int) -> bytes:
        payload = self.overlays.get(int(address))
        if payload is not None and int(size) <= len(payload):
            return payload[: int(size)]
        return self.process.read(address, size)

    def u64(self, address: int) -> int:
        return struct.unpack("<Q", self.read(address, 8))[0]


def _native_install_validation_view(
    process,
    damage_base: int,
) -> object:
    """Unwrap only an exact signed damage-meter proxy for manifest reads."""

    site = coop.DAMAGE_NUMBER_SOURCE_CALL_SITE
    proxy_base = damage_base + coop.DAMAGE_METER_PROXY_WORKSPACE_OFFSET
    proxy_entry = proxy_base + coop.DAMAGE_METER_PROXY_CODE_OFFSET
    proxy_call = b"\xe8" + coop.rel32(site, 5, proxy_entry)
    try:
        current = process.read(site, len(proxy_call))
    except (OSError, RuntimeError, ValueError, struct.error):
        return process
    if current != proxy_call:
        return process

    # Import only when the call site actually targets the meter workspace.  The
    # public inspector is also used by DamageHook.install(), keeping adoption's
    # signature contract identical to the live component's own contract.
    import damage_meter

    try:
        resident = damage_meter.inspect_resident_damage_hook(
            process,
            damage_base,
            current_call=current,
        )
    except (OSError, RuntimeError, ValueError, struct.error) as error:
        raise RuntimeError(
            "committed runtime has an invalid damage-meter proxy"
        ) from error
    if resident is None:
        raise RuntimeError(
            "committed runtime damage-meter proxy was not recognized"
        )
    return _NativeInstallReadView(process, {site: resident.previous_call})


def _validate_global_native_layout(
    process,
    bases: tuple[int, int, int],
    *,
    feature_bits: int,
) -> None:
    upstream_camera_v2 = bool(
        feature_bits & GLOBAL_INSTALL_FEATURE_UPSTREAM_CAMERA_V2
    )
    validation_process = _native_install_validation_view(
        process,
        bases[2],
    )
    try:
        mode, local_bases, _regions = coop.inspect_native_install(
            validation_process,
            upstream_camera_v2=upstream_camera_v2,
        )
    except (OSError, RuntimeError, ValueError, struct.error) as error:
        raise RuntimeError(
            "committed global runtime failed native layout validation"
        ) from error
    if mode != "reuse" or tuple(local_bases) != bases:
        raise RuntimeError(
            "committed global runtime has no matching local manifest"
        )


def validate_global_adoption_structure(
    process,
    pid: int,
    preflight: GlobalInstallPreflight,
) -> None:
    """Revalidate immutable committed topology without requiring live helpers."""

    if preflight.mode != "adopt" or not all(preflight.bases):
        raise ValueError("global adoption requires three committed allocations")
    committed = global_install_marker(
        state=GLOBAL_INSTALL_COMMITTED,
        feature_bits=preflight.feature_bits,
    )
    if _read_global_install_markers(
        process,
        preflight.bases,
    ) != (committed,) * len(preflight.bases):
        raise RuntimeError("global runtime commit changed during adoption")
    _validate_global_native_layout(
        process,
        preflight.bases,
        feature_bits=preflight.feature_bits,
    )

    renderer_present, renderer_detail = _renderer_module_present(pid)
    if not renderer_present:
        raise RuntimeError(
            "committed global runtime renderer is not loaded: " + renderer_detail
        )

    if _read_global_install_markers(
        process,
        preflight.bases,
    ) != (committed,) * len(preflight.bases):
        raise RuntimeError("global runtime commit changed during adoption")


def validate_global_adoption(
    process,
    pid: int,
    preflight: GlobalInstallPreflight,
) -> None:
    """Revalidate committed topology and the currently attached live helpers."""

    validate_global_adoption_structure(process, pid, preflight)
    committed = global_install_marker(
        state=GLOBAL_INSTALL_COMMITTED,
        feature_bits=preflight.feature_bits,
    )

    base = preflight.bases[0]
    movement_ready, movement_detail = movement_readiness_snapshot(
        process,
        base,
        expected_movement_hook_patch(base),
    )
    if not movement_ready:
        raise RuntimeError(
            "committed global runtime movement heartbeat is stale: "
            + movement_detail
        )
    menu_ready, menu_detail = menu_publisher_liveness_snapshot(pid)
    if not menu_ready:
        raise RuntimeError(
            "committed global runtime menu heartbeat is stale: " + menu_detail
        )

    # Both barriers are observation-only.  They require counters to advance,
    # proving that loaded modules and mappings are live rather than leftovers.
    wait_renderer_ready(process, base, RING_READY_MAGIC)
    wait_panel_transport_ready(pid)
    if _read_global_install_markers(
        process,
        preflight.bases,
    ) != (committed,) * len(preflight.bases):
        raise RuntimeError("global runtime commit changed during adoption")


def _assert_fresh_global_headers(
    process,
    bases: tuple[int, int, int],
) -> None:
    if not all(bases) or len(set(bases)) != len(bases):
        raise RuntimeError("global runtime allocations are missing or aliased")
    _assert_pristine_local_headers(process, bases)
    payloads = _read_global_install_markers(process, bases)
    empty = b"\x00" * GLOBAL_INSTALL_MARKER_STRUCT.size
    if any(payload != empty for payload in payloads):
        raise RuntimeError(
            "global runtime changed after preflight; restart the game"
        )
    _assert_pristine_allocation_images(process, bases)


def publish_global_install_prepared(
    process,
    bases: tuple[int, int, int],
    *,
    feature_bits: int,
) -> None:
    """Publish PREPARED to all headers before the first component starts."""

    bases = tuple(int(base) for base in bases)
    _assert_fresh_global_headers(process, bases)
    payload = global_install_marker(
        state=GLOBAL_INSTALL_PREPARED,
        feature_bits=feature_bits,
    )
    for base in bases:
        process.write(base + GLOBAL_INSTALL_MARKER_OFFSET, payload)
    if _read_global_install_markers(process, bases) != (payload,) * len(bases):
        raise RuntimeError("global PREPARED marker did not round-trip")


def commit_global_install(
    process,
    bases: tuple[int, int, int],
    *,
    feature_bits: int,
    before_primary_commit=None,
) -> None:
    """Commit secondary headers first and the primary header last."""

    bases = tuple(int(base) for base in bases)
    prepared = global_install_marker(
        state=GLOBAL_INSTALL_PREPARED,
        feature_bits=feature_bits,
    )
    if _read_global_install_markers(process, bases) != (prepared,) * len(bases):
        raise RuntimeError("global PREPARED transaction changed before commit")

    committed = global_install_marker(
        state=GLOBAL_INSTALL_COMMITTED,
        feature_bits=feature_bits,
    )
    # The primary member is the commit point.  A crash before its final write is
    # necessarily observed as a mixed triple and therefore cannot be adopted.
    for base in bases[1:]:
        process.write(base + GLOBAL_INSTALL_MARKER_OFFSET, committed)
        if process.read(
            base + GLOBAL_INSTALL_MARKER_OFFSET,
            GLOBAL_INSTALL_MARKER_STRUCT.size,
        ) != committed:
            raise RuntimeError("global secondary COMMIT marker did not round-trip")
    if before_primary_commit is not None:
        # From this instruction onward a failed WriteProcessMemory result is
        # ambiguous: the primary bytes may already be resident.  Arm teardown
        # before attempting the irreversible publication, not after its readback.
        before_primary_commit()
    process.write(bases[0] + GLOBAL_INSTALL_MARKER_OFFSET, committed)
    if _read_global_install_markers(process, bases) != (committed,) * len(bases):
        raise RuntimeError("global COMMIT marker did not round-trip")


def finalize_global_install(
    process,
    bases: tuple[int, int, int],
    *,
    feature_bits: int,
    before_primary_commit=None,
) -> None:
    """Validate every local native byte before publishing the global commit."""

    bases = tuple(int(base) for base in bases)
    _validate_global_native_layout(
        process,
        bases,
        feature_bits=feature_bits,
    )
    commit_global_install(
        process,
        bases,
        feature_bits=feature_bits,
        before_primary_commit=before_primary_commit,
    )


def ensure_runtime_cave_bootstrap(
    process,
    pid: int,
    *,
    timeout: float = CAVE_BOOTSTRAP_TIMEOUT,
    inject_first: bool = False,
) -> dict[str, int]:
    expected_variables = {
        runtime_paths.CAVE_BASE_ENV,
        runtime_paths.DUAL_CAVE_BASE_ENV,
        runtime_paths.DAMAGE_CAVE_BASE_ENV,
    }
    bases = {} if inject_first else _runtime_cave_publications(process)
    if inject_first or set(bases) != expected_variables:
        import inject_dll

        bootstrap = materialize_runtime_dll(CAVE_BOOTSTRAP_DLL)
        inject_dll.inject(str(bootstrap), pid=pid)
        deadline = time.monotonic() + timeout
        while True:
            bases = _runtime_cave_publications(process)
            if set(bases) == expected_variables:
                break
            if time.monotonic() >= deadline:
                missing = sorted(expected_variables - set(bases))
                raise TimeoutError(
                    "bootstrap nativo nao publicou as regioes rel32: "
                    + ", ".join(missing)
                )
            time.sleep(0.02)

    for variable, address in bases.items():
        os.environ[variable] = f"{address:#x}"
    return bases


def inject_render_dll(pid: int) -> None:
    renderer = materialize_render_dll()
    if getattr(sys, "frozen", False):
        import inject_dll

        inject_dll.inject(str(renderer), pid=pid)
        return
    result = subprocess.run(
        [str(PYTHON), str(HERE / "inject_dll.py"), str(renderer)],
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15.0,
        creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"renderer D3D11 nao carregou: {detail}")


def initialize_runtime_memory_and_renderer(
    process,
    pid: int,
    *,
    before_renderer=None,
) -> tuple[int, int, int, session_state.SessionIdentity]:
    ensure_runtime_cave_bootstrap(process, pid)
    wait_game_startup_ready(process, pid)
    base = process.ensure_cave_allocation()
    dual_base = process.ensure_dual_interaction_allocation()
    damage_base = process.ensure_damage_number_theme_allocation()
    os.environ[runtime_paths.CAVE_BASE_ENV] = f"{base:#x}"
    os.environ[runtime_paths.DUAL_CAVE_BASE_ENV] = f"{dual_base:#x}"
    os.environ[runtime_paths.DAMAGE_CAVE_BASE_ENV] = f"{damage_base:#x}"

    bases = (base, dual_base, damage_base)
    if before_renderer is not None:
        before_renderer(bases)

    identity = seed_authored_session_identity()
    configured_roles = [
        f"p1={identity.name('p1')}",
        f"p2={identity.name('p2')}",
    ]
    if identity.p3_party_member_id is not None:
        configured_roles.append(f"p3={identity.name('p3')}")
    print(
        "CO-OP PARTY: formacao do launcher publicada " + " ".join(configured_roles),
        flush=True,
    )
    inject_render_dll(pid)
    return base, dual_base, damage_base, identity


def wait_renderer_ready(
    process,
    base: int,
    expected_magic: int = RING_READY_MAGIC,
    *,
    timeout: float = RENDER_READY_TIMEOUT,
    poll_interval: float = CHILD_READY_POLL_INTERVAL,
    recover_children=None,
) -> None:
    deadline = time.monotonic() + timeout
    first_present_count = None
    last_magic = None
    last_present_count = None
    while time.monotonic() < deadline:
        if recover_children is not None:
            recover_children()
        try:
            last_magic = u32(process, base + RING_READY_OFFSET)
            last_present_count = u32(process, base + RING_PRESENT_COUNT_OFFSET)
        except (OSError, RuntimeError, ValueError, struct.error):
            first_present_count = None
            time.sleep(poll_interval)
            continue
        if last_magic == expected_magic:
            if first_present_count is None:
                first_present_count = last_present_count
            elif last_present_count != first_present_count:
                return
        time.sleep(poll_interval)
    raise RuntimeError(
        "renderer 3D nao apresentou frames: "
        f"magic={last_magic!r} present={last_present_count!r}"
    )


def panel_transport_snapshot(pid: int) -> tuple[int, ...]:
    mapping = mmap.mmap(
        -1,
        d3d_panel_transport.MAPPING_SIZE,
        tagname=f"Local\\FF12CoopPanels_{int(pid)}",
        access=mmap.ACCESS_READ,
    )
    try:
        return struct.unpack_from("<8I3Q", mapping, 0)
    finally:
        mapping.close()


def menu_publisher_liveness_snapshot(pid: int) -> tuple[bool, str]:
    """Observe only the Python menu publisher, not mutable render visibility."""

    try:
        snapshot = panel_transport_snapshot(pid)
        (
            magic,
            version,
            header_size,
            panel_count,
            slot_size,
            pixel_capacity,
            publisher_ready,
            _renderer_ready,
            publisher_heartbeat,
            _renderer_heartbeat,
            _renderer_frame,
        ) = snapshot
        now_ms = d3d_panel_transport.tick_ms()
        age_ms = (
            now_ms - publisher_heartbeat if publisher_heartbeat else None
        )
        header_ready = bool(
            magic == d3d_panel_transport.MAGIC
            and version == d3d_panel_transport.VERSION
            and header_size == d3d_panel_transport.HEADER_SIZE
            and panel_count == d3d_panel_transport.PANEL_COUNT
            and slot_size == d3d_panel_transport.SLOT_SIZE
            and pixel_capacity == d3d_panel_transport.PIXEL_CAPACITY
            and publisher_ready == 1
        )
        heartbeat_ready = bool(
            age_ms is not None
            and -1000 <= age_ms <= d3d_panel_transport.RENDERER_STALE_MS
        )
        ready = bool(header_ready and heartbeat_ready)
        return ready, (
            f"header={int(header_ready)} publisher={publisher_ready} "
            f"heartbeat={publisher_heartbeat} age_ms={age_ms!r}"
        )
    except (OSError, RuntimeError, ValueError, struct.error) as error:
        return False, f"{type(error).__name__}: {error}"


def wait_panel_transport_ready(
    pid: int,
    *,
    timeout: float = CHILD_READY_TIMEOUT,
    poll_interval: float = CHILD_READY_POLL_INTERVAL,
    recover_children=None,
) -> None:
    deadline = time.monotonic() + timeout
    previous = None
    last = None
    while time.monotonic() < deadline:
        if recover_children is not None:
            recover_children()
        try:
            last = panel_transport_snapshot(pid)
        except (OSError, ValueError):
            time.sleep(poll_interval)
            continue
        (
            magic,
            version,
            header_size,
            panel_count,
            slot_size,
            pixel_capacity,
            publisher_ready,
            renderer_ready,
            publisher_heartbeat,
            renderer_heartbeat,
            renderer_frame,
        ) = last
        now_ms = d3d_panel_transport.tick_ms()
        header_ready = bool(
            magic == d3d_panel_transport.MAGIC
            and version == d3d_panel_transport.VERSION
            and header_size == d3d_panel_transport.HEADER_SIZE
            and panel_count == d3d_panel_transport.PANEL_COUNT
            and slot_size == d3d_panel_transport.SLOT_SIZE
            and pixel_capacity == d3d_panel_transport.PIXEL_CAPACITY
            and publisher_ready == 1
            and renderer_ready == 1
        )
        heartbeat_ready = bool(
            publisher_heartbeat
            and renderer_heartbeat
            and now_ms >= publisher_heartbeat
            and now_ms >= renderer_heartbeat
            and now_ms - publisher_heartbeat <= d3d_panel_transport.RENDERER_STALE_MS
            and now_ms - renderer_heartbeat <= d3d_panel_transport.RENDERER_STALE_MS
        )
        advanced = bool(
            previous is not None
            and publisher_heartbeat != previous[8]
            and renderer_heartbeat != previous[9]
            and renderer_frame != previous[10]
        )
        if header_ready and heartbeat_ready and advanced:
            return
        previous = last
        time.sleep(poll_interval)
    raise RuntimeError(
        f"menu/renderer nao publicaram heartbeat conjunto: snapshot={last!r}"
    )


def live_party_generation(process):
    battle_info = process.u64(BATTLE_INFO_GLOBAL)
    if not battle_info:
        raise RuntimeError("battle info indisponivel")
    current = struct.unpack(
        "<9H",
        process.read(battle_info + 0x5A48 + 1 * 0x12, 0x12),
    )
    ui = struct.unpack(
        "<9H",
        process.read(battle_info + 0x5A48 + 2 * 0x12, 0x12),
    )
    leader_value = process.u32(battle_info + session_state.BATTLE_LEADER_OFFSET)
    leader = None if leader_value == session_state.EMPTY_PARTY_ID else leader_value
    return battle_info, current, ui, leader


def configured_role_slots(
    identity: session_state.SessionIdentity,
    current,
    ui,
    leader: int | None,
    *,
    allow_latent_followers: bool = False,
) -> tuple[dict[str, int], dict[str, int]] | None:
    role_members = {
        "p1": session_state.p1_controlled_party_member_id(
            identity,
            current,
            ui,
            leader,
        ),
    }
    if identity.p2_party_member_id is not None:
        role_members["p2"] = identity.p2_party_member_id
    if identity.p3_party_member_id is not None:
        role_members["p3"] = identity.p3_party_member_id
    role_slots = {}
    for role, member_id in role_members.items():
        current_slots = [
            slot for slot, value in enumerate(current[:4]) if value == member_id
        ]
        ui_slots = [slot for slot, value in enumerate(ui[:4]) if value == member_id]
        if len(current_slots) != 1 or len(ui_slots) != 1:
            if (
                allow_latent_followers
                and role != "p1"
                and not current_slots
                and not ui_slots
            ):
                continue
            return None
        role_slots[role] = current_slots[0]
    if len(set(role_slots.values())) != len(role_slots):
        return None
    return role_members, role_slots


def runtime_role_slots(
    identity: session_state.SessionIdentity,
    current,
    ui,
    leader: int | None,
) -> tuple[dict[str, int], dict[str, int]] | None:
    """Resolve only roles that can be controlled in the current stable party."""
    active_main_party = session_state.active_main_party_members(current)
    if active_main_party:
        live_identity = session_state.available_identity(identity, current, ui, leader)
        if live_identity is None:
            return None
        # A configured follower may be in reserve, temporarily removed by the
        # story, or not materialized yet. P1 is still a valid co-op runtime;
        # movement/menu workers already reconnect followers when their actors
        # return. Startup must not kill those workers merely because only P1 is
        # present in the current field layout.
        return configured_role_slots(
            live_identity,
            current,
            ui,
            leader,
            allow_latent_followers=True,
        )

    return configured_role_slots(
        identity,
        current,
        ui,
        leader,
        allow_latent_followers=True,
    )


def expected_role_theme_palettes(
    role_members: dict[str, int],
    selection: menu_themes.ThemeSelection,
) -> dict[str, bytes]:
    offsets = {
        "p1": P1_GAUGE_THEME_PALETTE_OFFSET,
        "p2": P2_GAUGE_THEME_PALETTE_OFFSET,
        "p3": P3_GAUGE_THEME_PALETTE_OFFSET,
    }
    return {
        role: struct.pack(
            "<8i",
            *menu_themes.gauge_palette(selection.for_player(int(role[1]), member_id)),
        )
        for role, member_id in role_members.items()
        if role in offsets
    }


def configured_controller_isolation_mask(config=None) -> int:
    if config is None:
        config = controller_bindings.load_config()
    devices = config.get("devices", {})
    indices = coop.native_controller_isolation_indices(devices.get("p1"))
    return coop.controller_isolation_mask(indices)


def _relative_call_target(site: int, instruction: bytes) -> int | None:
    if len(instruction) != 5 or instruction[:1] != b"\xe8":
        return None
    return site + 5 + struct.unpack("<i", instruction[1:])[0]


def damage_theme_runtime_ready(process, damage_base: int) -> bool:
    if (
        process.read(damage_base, len(coop.DAMAGE_NUMBER_THEME_MAGIC))
        != coop.DAMAGE_NUMBER_THEME_MAGIC
    ):
        return False
    site = coop.DAMAGE_NUMBER_SOURCE_CALL_SITE
    target = _relative_call_target(site, process.read(site, 5))
    theme_wrapper = damage_base + coop.DAMAGE_NUMBER_SOURCE_WRAPPER_OFFSET
    if target == theme_wrapper:
        return True

    proxy_base = damage_base + coop.DAMAGE_METER_PROXY_WORKSPACE_OFFSET
    proxy_entry = proxy_base + coop.DAMAGE_METER_PROXY_CODE_OFFSET
    if target != proxy_entry:
        return False
    if (
        process.read(proxy_base, len(coop.DAMAGE_METER_PROXY_MAGIC))
        != coop.DAMAGE_METER_PROXY_MAGIC
    ):
        return False
    hook_base = process.u64(proxy_base + coop.DAMAGE_METER_PROXY_ALLOCATION_OFFSET)
    previous_target = process.u64(
        proxy_base + coop.DAMAGE_METER_PROXY_OLD_TARGET_OFFSET
    )
    return bool(
        previous_target == theme_wrapper
        and process.read(hook_base, len(coop.DAMAGE_METER_HOOK_MAGIC))
        == coop.DAMAGE_METER_HOOK_MAGIC
        and process.u64(hook_base + coop.DAMAGE_METER_HOOK_OLD_TARGET_OFFSET)
        == theme_wrapper
    )


def loot_runtime_ready(process, damage_base: int) -> bool:
    workspace = damage_base + coop.LOOT_OWNERSHIP_WORKSPACE_OFFSET
    code = workspace + loot_ownership.CAVE_CODE_OFFSET
    return bool(
        process.read(workspace, len(loot_ownership.CAVE_MAGIC))
        == loot_ownership.CAVE_MAGIC
        and process.read(
            loot_ownership.OWNERSHIP_CALL_SITE,
            len(loot_ownership.STOCK_CALL),
        )
        == loot_ownership.call_site_patch(code)
    )


@dataclass(frozen=True)
class RuntimeReadiness:
    structural_ready: bool
    operational_ready: bool
    operational_state: str
    structural_failures: tuple[str, ...]
    structural_diagnostic: str
    operational_diagnostic: str

    @property
    def diagnostic(self) -> str:
        failures = ",".join(self.structural_failures) or "none"
        return (
            f"structural={int(self.structural_ready)} failures={failures} "
            f"operational={self.operational_state} "
            f"{self.structural_diagnostic} {self.operational_diagnostic}"
        )


def classified_runtime_readiness_snapshot(
    process,
    base: int,
    dual_base: int,
    damage_base: int,
    identity: session_state.SessionIdentity,
    *,
    start_with_all: bool,
    theme_selection: menu_themes.ThemeSelection,
    expected_device_mask: int,
) -> RuntimeReadiness:
    """Classify immutable runtime integrity separately from mutable gameplay.

    Only structural failures may block startup.  Actor ownership, mailboxes,
    HUD slots, themes, roster state and menu availability are observations: all
    of them legitimately change during KO/status effects, loading, modals,
    reserve changes and manual-control toggles.
    """

    movement_patch = expected_movement_hook_patch(base)
    movement_ready, movement_diagnostic = movement_readiness_snapshot(
        process,
        base,
        movement_patch,
    )
    structural_failures: list[str] = []
    try:
        gauge_patch = (
            b"\xe9"
            + coop.rel32(
                coop.GAUGE_COLOR_HOOK_SITE,
                5,
                base + 0x900,
            )
            + b"\x90" * 3
        )
        keyboard_patch = (
            b"\xe9"
            + coop.rel32(
                coop.KEYBOARD_HOOK_SITE,
                5,
                base + 0x300,
            )
            + b"\x90" * 3
        )
        device_patch = b"\xe9" + coop.rel32(
            coop.XINPUT_DEVICE_FINALIZE_SITE,
            5,
            dual_base + coop.P2_DEVICE_FILTER_CAVE_OFFSET,
        )
        p1_sdl_patch = (
            b"\xe9"
            + coop.rel32(
                coop.XINPUT_STATE_RESULT_SITE,
                5,
                dual_base + coop.P1_SDL_XINPUT_CAVE_OFFSET,
            )
            + b"\x90\x90"
        )
        dinput_patch = (
            b"\xe9"
            + coop.rel32(
                coop.DINPUT_GAMEPAD_FINALIZE_SITE,
                5,
                dual_base + coop.DINPUT_GAMEPAD_FILTER_CAVE_OFFSET,
            )
            + b"\x90" * 3
        )
        steam_input_patch = (
            b"\xe9"
            + coop.rel32(
                coop.STEAM_GAMEPAD_FINALIZE_SITE,
                5,
                dual_base + coop.STEAM_GAMEPAD_FILTER_CAVE_OFFSET,
            )
            + b"\x90" * 3
        )
        gauge_hook_ready = bool(
            process.read(coop.GAUGE_COLOR_HOOK_SITE, len(gauge_patch)) == gauge_patch
        )
        keyboard_hook_ready = bool(
            process.read(coop.KEYBOARD_HOOK_SITE, len(keyboard_patch)) == keyboard_patch
        )
        device_hook_ready = bool(
            process.read(coop.XINPUT_DEVICE_FINALIZE_SITE, len(device_patch))
            == device_patch
            and process.read(dual_base, len(coop.DUAL_INTERACTION_MAGIC))
            == coop.DUAL_INTERACTION_MAGIC
        )
        p1_sdl_hook_ready = bool(
            process.read(coop.XINPUT_STATE_RESULT_SITE, len(p1_sdl_patch))
            == p1_sdl_patch
        )
        dinput_hook_ready = bool(
            process.read(coop.DINPUT_GAMEPAD_FINALIZE_SITE, len(dinput_patch))
            == dinput_patch
        )
        steam_input_hook_ready = bool(
            process.read(
                coop.STEAM_GAMEPAD_FINALIZE_SITE,
                len(steam_input_patch),
            )
            == steam_input_patch
        )

        menu_hook_ready = True
        if start_with_all:
            menu_cave = base + story_party_patch.MENU_FEATURE_GATE_CAVE_OFFSET
            menu_state = base + story_party_patch.MENU_FEATURE_GATE_STATE_OFFSET
            expected_menu_hook = b"\xe9" + coop.rel32(
                story_party_patch.MENU_FEATURE_GATE_SITE,
                5,
                menu_cave,
            )
            expected_menu_body = story_party_patch.build_early_menu_feature_gate(
                menu_cave,
                menu_state,
            )
            menu_hook_ready = bool(
                process.read(
                    story_party_patch.MENU_FEATURE_GATE_SITE,
                    len(expected_menu_hook),
                )
                == expected_menu_hook
                and process.read(menu_cave, len(expected_menu_body))
                == expected_menu_body
            )

        renderer_ready = bool(
            u32(process, base + RING_READY_OFFSET) == RING_READY_MAGIC
        )
        damage_ready = damage_theme_runtime_ready(process, damage_base)
        loot_ready = loot_runtime_ready(process, damage_base)

        structural_checks = (
            ("movement", movement_ready),
            ("gauge_hook", gauge_hook_ready),
            ("keyboard_hook", keyboard_hook_ready),
            ("device_hook", device_hook_ready),
            ("p1_sdl_hook", p1_sdl_hook_ready),
            ("dinput_hook", dinput_hook_ready),
            ("steam_input_hook", steam_input_hook_ready),
            ("menu_hook", menu_hook_ready),
            ("renderer", renderer_ready),
            ("damage", damage_ready),
            ("loot", loot_ready),
        )
        structural_failures.extend(
            name for name, ready in structural_checks if not ready
        )
        structural_diagnostic = (
            f"hooks={int(gauge_hook_ready)}/{int(keyboard_hook_ready)}/"
            f"{int(device_hook_ready)}/{int(p1_sdl_hook_ready)}/"
            f"{int(dinput_hook_ready)}/{int(steam_input_hook_ready)} "
            f"menu_hook={int(menu_hook_ready)} renderer={int(renderer_ready)} "
            f"damage={int(damage_ready)} loot={int(loot_ready)} "
            f"{movement_diagnostic}"
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        IndexError,
        struct.error,
    ) as error:
        structural_failures.append(f"snapshot:{type(error).__name__}:{error}")
        structural_diagnostic = (
            f"structural_snapshot_error={type(error).__name__}:{error} "
            f"{movement_diagnostic}"
        )

    try:
        keyboard_budget = process.u32(
            base + 0x300 + coop.KEYBOARD_FILTER_BUDGET_CAVE_OFFSET
        )
        device_mask = process.u32(dual_base + coop.P2_DEVICE_FILTER_STATE_OFFSET)
        native_slot = process.u32(
            dual_base
            + coop.P2_DEVICE_FILTER_STATE_OFFSET
            + coop.P1_NATIVE_CONTROLLER_SLOT_OFFSET
        )
        coherent_device_mask = (
            coop.controller_isolation_mask(
                tuple(index for index in range(4) if index != native_slot)
            )
            if 0 <= native_slot <= 3
            else coop.controller_isolation_mask(range(4))
        )
        # F8 intentionally releases isolation.  Any nonzero publication should
        # nevertheless agree with the effective native P1 slot, including
        # hotplug fallback to a different live XInput index.
        device_state_ready = device_mask in (0, coherent_device_mask)

        battle_info, current, ui, leader = live_party_generation(process)
        active_main_party = any(
            member_id in session_state.MAIN_PARTY_MEMBER_IDS
            for member_id in current[:4]
        )
        role_generation = runtime_role_slots(identity, current, ui, leader)
        if role_generation is None:
            operational_diagnostic = (
                f"roles=incoherent current={current[:4]} ui={ui[:4]} "
                f"leader={leader!r} budget={keyboard_budget} "
                f"mask={device_mask:#x}/coherent={coherent_device_mask:#x}/"
                f"configured={expected_device_mask:#x} native_slot={native_slot}"
            )
            return RuntimeReadiness(
                structural_ready=not structural_failures,
                operational_ready=False,
                operational_state="transition",
                structural_failures=tuple(structural_failures),
                structural_diagnostic=structural_diagnostic,
                operational_diagnostic=operational_diagnostic,
            )
        role_members, role_slots = role_generation
        actors = coop.resolve_active_actors(process)
        actor_slots = coop.resolve_active_party_actor_slots(process, actors)
        materialized = {
            role: actor_slots.get(slot) for role, slot in role_slots.items()
        }
        roles_materialized = all(materialized.values())

        p2_actor = process.u64(coop.COMMAND_BLOCK + 0x10)
        p2_expected = materialized["p2"][1] if materialized.get("p2") else 0
        p2_hud_slot = process.u32(coop.COMMAND_BLOCK + coop.P2_HUD_PARTY_SLOT_OFFSET)
        p1_hud_slot = process.u32(coop.COMMAND_BLOCK + coop.P1_HUD_PARTY_SLOT_OFFSET)
        coop_active = process.u32(coop.COMMAND_BLOCK + coop.COOP_ACTIVE_OFFSET)
        p2_movement_active = process.u32(
            coop.COMMAND_BLOCK + coop.MOVEMENT_ACTIVE_OFFSET
        )
        controls_ready = bool(
            roles_materialized and coop_active == 1 and p1_hud_slot == role_slots["p1"]
        )
        if "p2" in role_slots:
            controls_ready = bool(
                controls_ready
                and p2_movement_active == 1
                and p2_actor == p2_expected
                and p2_hud_slot == role_slots["p2"]
            )

        p3_actor = 0
        p3_expected = 0
        p3_hud_slot = INVALID_HUD_PARTY_SLOT
        p3_movement_active = 0
        if "p3" in role_slots:
            p3_block = base + coop.P3_COMMAND_BLOCK_OFFSET
            p3_actor = process.u64(p3_block + coop.P3_ACTOR_OFFSET)
            p3_expected = materialized["p3"][1] if materialized.get("p3") else 0
            p3_hud_slot = process.u32(p3_block + coop.P3_HUD_PARTY_SLOT_OFFSET)
            p3_movement_active = process.u32(p3_block + coop.P3_MOVEMENT_ACTIVE_OFFSET)
            controls_ready = bool(
                controls_ready
                and p3_movement_active == 1
                and p3_actor == p3_expected
                and p3_hud_slot == role_slots["p3"]
            )

        palette_offsets = {
            "p1": P1_GAUGE_THEME_PALETTE_OFFSET,
            "p2": P2_GAUGE_THEME_PALETTE_OFFSET,
            "p3": P3_GAUGE_THEME_PALETTE_OFFSET,
        }
        expected_palettes = expected_role_theme_palettes(
            role_members,
            theme_selection,
        )
        themes_ready = all(
            process.read(base + palette_offsets[role], len(expected)) == expected
            for role, expected in expected_palettes.items()
        )

        start_roster_ready = True
        start_menus_ready = True
        menu_flags = None
        menu_gate = None
        if start_with_all and active_main_party:
            required_roster = session_state.MAIN_PARTY_MEMBER_IDS
            default_layout = struct.unpack(
                "<9H",
                process.read(
                    battle_info
                    + session_state.BATTLE_PARTY_LAYOUT_OFFSET
                    + session_state.BATTLE_DEFAULT_LAYOUT
                    * session_state.BATTLE_PARTY_LAYOUT_STRIDE,
                    session_state.BATTLE_PARTY_LAYOUT_STRIDE,
                ),
            )
            menu_layout = struct.unpack(
                "<9H",
                process.read(
                    battle_info
                    + session_state.BATTLE_PARTY_LAYOUT_OFFSET
                    + session_state.BATTLE_MENU_LAYOUT
                    * session_state.BATTLE_PARTY_LAYOUT_STRIDE,
                    session_state.BATTLE_PARTY_LAYOUT_STRIDE,
                ),
            )
            start_roster_ready = bool(
                all(
                    default_layout.count(member_id) == 1
                    for member_id in required_roster
                )
                and all(
                    menu_layout.count(member_id) == 1 for member_id in required_roster
                )
            )
            menu_info = process.u64(story_party_patch.MENU_INFO_GLOBAL)
            if menu_info:
                menu_flags = process.read(
                    menu_info + story_party_patch.MENU_SELECTABLE_FLAGS_OFFSET,
                    1,
                )[0]
                menu_gate = process.read(
                    base + story_party_patch.MENU_FEATURE_GATE_STATE_OFFSET,
                    1,
                )[0]
                start_menus_ready = bool(
                    menu_flags & story_party_patch.MENU_START_SELECTABLE_MASK
                    == story_party_patch.MENU_START_SELECTABLE_MASK
                    and menu_gate == 1
                )
            else:
                start_menus_ready = False

        operational_ready = bool(
            controls_ready
            and themes_ready
            and start_roster_ready
            and start_menus_ready
            and keyboard_budget > 0
            and device_state_ready
        )
        operational_diagnostic = (
            f"roles={role_members} slots={role_slots} "
            f"p1hud={p1_hud_slot} "
            f"p2={p2_actor:#x}/{p2_expected:#x} hud={p2_hud_slot} "
            f"active={coop_active}/{p2_movement_active} "
            f"p3={p3_actor:#x}/{p3_expected:#x} hud={p3_hud_slot} "
            f"p3active={p3_movement_active} themes={int(themes_ready)} "
            f"budget={keyboard_budget} "
            f"mask={device_mask:#x}/coherent={coherent_device_mask:#x}/"
            f"configured={expected_device_mask:#x} native_slot={native_slot} "
            f"roster={int(start_roster_ready)} "
            f"menus={menu_flags!r}/{menu_gate!r}/{menu_hook_ready!r} "
        )
        return RuntimeReadiness(
            structural_ready=not structural_failures,
            operational_ready=operational_ready,
            operational_state="ready" if operational_ready else "degraded",
            structural_failures=tuple(structural_failures),
            structural_diagnostic=structural_diagnostic,
            operational_diagnostic=operational_diagnostic,
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        IndexError,
        struct.error,
    ) as error:
        return RuntimeReadiness(
            structural_ready=not structural_failures,
            operational_ready=False,
            operational_state="transition",
            structural_failures=tuple(structural_failures),
            structural_diagnostic=structural_diagnostic,
            operational_diagnostic=(
                f"operational_snapshot_error={type(error).__name__}:{error}"
            ),
        )


def runtime_readiness_snapshot(
    process,
    base: int,
    dual_base: int,
    damage_base: int,
    identity: session_state.SessionIdentity,
    *,
    start_with_all: bool,
    theme_selection: menu_themes.ThemeSelection,
    expected_device_mask: int,
) -> tuple[bool, str]:
    snapshot = classified_runtime_readiness_snapshot(
        process,
        base,
        dual_base,
        damage_base,
        identity,
        start_with_all=start_with_all,
        theme_selection=theme_selection,
        expected_device_mask=expected_device_mask,
    )
    return snapshot.structural_ready, snapshot.diagnostic


def wait_runtime_ready(
    process,
    base: int,
    dual_base: int,
    damage_base: int,
    identity: session_state.SessionIdentity,
    child_records,
    *,
    start_with_all: bool,
    timeout: float | None = RUNTIME_READY_TIMEOUT,
    poll_interval: float = RUNTIME_READY_POLL_INTERVAL,
    stable_seconds: float = RUNTIME_READY_STABLE_SECONDS,
    recover_children=None,
) -> None:
    deadline = None if timeout is None else time.monotonic() + timeout
    stable_since = None
    last_diagnostic = "sem amostra"
    theme_tracker = menu_themes.ThemeTracker()
    identity_tracker = session_state.SessionTracker(initial_identity=identity)
    expected_device_mask = configured_controller_isolation_mask()
    while deadline is None or time.monotonic() < deadline:
        if not game_alive(process):
            raise RuntimeError("jogo encerrou durante a confirmacao final do co-op")
        if recover_children is not None:
            recover_children()
        else:
            failed = failed_child_codes(child_records)
            if not failed:
                failed = None
        if recover_children is None and failed:
            raise RuntimeError(
                f"helper do co-op encerrou antes do commit final: {failed}"
            )

        current_identity = identity_tracker.reload()
        theme_selection = theme_tracker.reload()
        ready, last_diagnostic = runtime_readiness_snapshot(
            process,
            base,
            dual_base,
            damage_base,
            current_identity,
            start_with_all=start_with_all,
            theme_selection=theme_selection,
            expected_device_mask=expected_device_mask,
        )
        if ready:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= stable_seconds:
                print(f"CO-OP COMMIT CONFIRMADO {last_diagnostic}", flush=True)
                return
        else:
            stable_since = None
        time.sleep(poll_interval)
    raise TimeoutError("co-op nao confirmou o contrato final: " + last_diagnostic)


def configure_committed_runtime_bases(bases: tuple[int, int, int]) -> None:
    """Publish an already-validated allocation set to replacement helpers."""

    base, dual_base, damage_base = (int(value) for value in bases)
    if not base or not dual_base or not damage_base or len(set(bases)) != 3:
        raise ValueError("committed runtime bases must be distinct and non-zero")
    os.environ[runtime_paths.CAVE_BASE_ENV] = f"{base:#x}"
    os.environ[runtime_paths.DUAL_CAVE_BASE_ENV] = f"{dual_base:#x}"
    os.environ[runtime_paths.DAMAGE_CAVE_BASE_ENV] = f"{damage_base:#x}"
    coop.set_cave_base(base)
    coop.set_dual_interaction_base(dual_base)


def runtime_helper_factories(
    process,
    base: int,
    log_dir: Path,
    movement_args: list[str],
):
    return {
        "headless_menu": lambda: start_ready_child(
            log_dir,
            "headless_menu",
            "headless_menu_shell.py",
            "HEADLESS_MENU_V2_READY",
            game_process=process,
        ),
        "story_party": lambda: start_ready_child(
            log_dir,
            "story_party",
            "story_party_patch.py",
            "STORY_PARTY_PATCH ready",
            game_process=process,
        ),
        "penelo_guard": lambda: start_ready_child(
            log_dir,
            "penelo_guard",
            "penelo_story_guard.py",
            "PENELO_GUARD ready",
            game_process=process,
        ),
        "movement": lambda: start_movement_helper(
            process,
            base,
            log_dir,
            movement_args,
        ),
    }


def supervise_committed_runtime(
    process,
    pid: int,
    preflight: GlobalInstallPreflight,
    log_dir: Path,
) -> int:
    """Reconnect a new supervisor to resident code and surviving helpers."""

    validate_global_adoption_structure(process, pid, preflight)
    configure_committed_runtime_bases(preflight.bases)
    base = preflight.bases[0]
    movement_args = [
        "--active",
        "--camera-focus-v2",
        "--preserve-resident-state",
    ]
    if not (
        preflight.feature_bits & GLOBAL_INSTALL_FEATURE_UPSTREAM_CAMERA_V2
    ):
        movement_args.remove("--camera-focus-v2")
    factories = runtime_helper_factories(process, base, log_dir, movement_args)
    children = []
    try:
        # Adoption may find any subset of old helpers.  Close admission before
        # attaching/starting the menu producer; an external movement helper
        # will not observe this launcher write itself, so the launcher reopens
        # only after its native hook + heartbeat are independently proven.
        wait_required_component(
            process,
            "role admission fence close",
            lambda: _publish_role_admission_fence_confirmed(process, True),
        )
        for role in ("headless_menu", "story_party", "penelo_guard", "movement"):
            children.append(
                start_or_attach_required_child(
                    process,
                    pid,
                    log_dir,
                    role,
                    factories[role],
                )
            )

        helper_health_state = {}
        movement_patch = expected_movement_hook_patch(base)
        helper_health_checks = {
            "movement": lambda record: movement_readiness_snapshot(
                process,
                base,
                movement_patch,
                expected_owner_pid=(getattr(record[0], "pid", 0) or None),
            ),
            "headless_menu": lambda _record: menu_publisher_liveness_snapshot(pid),
        }

        def recover_children():
            return supervise_children(
                process,
                children,
                factories,
                helper_health_checks,
                helper_health_state,
            )

        def reopen_role_admission_after_movement_ready():
            recover_children()
            movement_record = next(
                record for record in children if child_role(record) == "movement"
            )
            ready, diagnostic = movement_readiness_snapshot(
                process,
                base,
                movement_patch,
                expected_owner_pid=(
                    getattr(movement_record[0], "pid", 0) or None
                ),
            )
            if not ready:
                raise RuntimeError(
                    "movement role consumer is not ready: " + diagnostic
                )
            _publish_role_admission_fence_confirmed(process, False)

        wait_required_component(
            process,
            "movement role consumer",
            reopen_role_admission_after_movement_ready,
        )

        wait_required_component(
            process,
            "renderer heartbeat",
            lambda: wait_renderer_ready(
                process,
                base,
                RING_READY_MAGIC,
                recover_children=recover_children,
            ),
        )
        wait_required_component(
            process,
            "menu panel transport",
            lambda: wait_panel_transport_ready(
                pid,
                recover_children=recover_children,
            ),
        )
        print(
            "CO-OP HEADLESS RECONNECTED "
            f"pid={pid} layout={GLOBAL_INSTALL_LAYOUT_ID.hex()}",
            flush=True,
        )
        publish_runtime_status(
            pid,
            "ready",
            "Co-op residente reassumido; supervisao dos helpers restaurada.",
            warning="CO-OP STATUS: reconnect warning",
        )

        while game_alive(process):
            recover_children()
            time.sleep(0.5)
        return 0
    finally:
        try:
            retire_supervised_children(
                process,
                children,
                resident_committed=True,
            )
        finally:
            clear_runtime_status(pid)


def main() -> int:
    pid = coop.find_pid()
    mutex = None
    owns_session_mutex = False
    try:
        mutex, owns_session_mutex = acquire_session_mutex(pid)
        process = open_supervisor_lifetime_process(pid)
    except BaseException:
        # The mutex is acquired before the writable process handle so that two
        # supervisors can never race installation.  A one-shot constructor
        # failure releases its mutex; the persistent runner deliberately keeps
        # its borrowed mutex while deciding whether the resident session retries.
        if owns_session_mutex and mutex is not None:
            kernel32.CloseHandle(mutex)
        raise
    children = []
    log_dir = LOG_ROOT / str(pid)
    owns_fresh_session = False
    global_install_committed = False
    try:
        upstream_camera_v2 = "--legacy-camera" not in sys.argv[1:]
        preflight = classify_global_install(
            process,
            upstream_camera_v2=upstream_camera_v2,
        )
        if preflight.mode == "adopt":
            mark_persistent_runtime_resident(pid)
            # A committed native topology outlives its Python supervisor.  A
            # replacement must therefore resume supervision instead of merely
            # observing stale heartbeats forever.  Named helper leases let it
            # adopt survivors and start only roles whose owning process ended.
            return supervise_committed_runtime(process, pid, preflight, log_dir)

        owns_fresh_session = True
        reset_session_logs(log_dir)
        clear_runtime_status(pid)
        publish_runtime_status(
            pid,
            "preparing",
            "Sessão co-op encontrada. Conectando controles e interface…",
            warning="CO-OP STATUS: preparation warning",
        )
        def prepare_global_headers(bases):
            bases = tuple(bases)
            if any(preflight.bases) and bases != preflight.bases:
                raise RuntimeError(
                    "runtime allocations changed after global preflight; "
                    "restart the game"
                )
            publish_global_install_prepared(
                process,
                bases,
                feature_bits=preflight.feature_bits,
            )

        base, dual_base, damage_base, identity = initialize_runtime_memory_and_renderer(
            process,
            pid,
            before_renderer=prepare_global_headers,
        )
        # Fresh dual memory is zero-filled, which would otherwise look like an
        # open role-admission gate while the menu producer starts ahead of the
        # movement consumer.  Close it before the producer can become ready;
        # movement reopens only after native install and its first heartbeat.
        wait_required_component(
            process,
            "role admission fence close",
            lambda: _publish_role_admission_fence_confirmed(process, True),
        )

        movement_args = [
            "--active",
            "--camera-focus-v2",
            "--preserve-resident-state",
        ]
        if not upstream_camera_v2:
            movement_args.remove("--camera-focus-v2")

        helper_factories = runtime_helper_factories(
            process,
            base,
            log_dir,
            movement_args,
        )

        # Start the menu first because it blocks P1's native command. The other
        # helpers load while the renderer constructs its panels.  Every initial
        # helper uses the same persistent policy as steady-state recovery; an
        # early worker crash must not tear down helpers that are already sound.
        menu = start_required_child(
            process,
            "headless_menu",
            lambda: start_ready_child(
                log_dir,
                "headless_menu",
                "headless_menu_shell.py",
                "HEADLESS_MENU_V2_READY",
                game_process=process,
            ),
        )
        children.append(menu)

        story_party = start_required_child(
            process,
            "story_party",
            helper_factories["story_party"],
        )
        children.append(story_party)

        penelo_guard = start_required_child(
            process,
            "penelo_guard",
            helper_factories["penelo_guard"],
        )
        children.append(penelo_guard)

        # A live hook is not a live controller: after a forced launcher restart,
        # the JMP can remain installed with nobody reading WASD or updating the
        # command block. Always recreate the idempotent helper so it revalidates
        # every patch. Shared focus is published before the game solves camera
        # collision; the former late translation competed with walls.
        movement = start_required_child(
            process,
            "movement",
            helper_factories["movement"],
        )
        children.append(movement)
        wait_required_component(
            process,
            "movement role admission open",
            lambda: _publish_role_admission_fence_confirmed(process, False),
        )

        helper_health_state = {}
        movement_patch = expected_movement_hook_patch(base)
        helper_health_checks = {
            "movement": lambda record: movement_readiness_snapshot(
                process,
                base,
                movement_patch,
                expected_owner_pid=(getattr(record[0], "pid", 0) or None),
            ),
            "headless_menu": lambda _record: menu_publisher_liveness_snapshot(pid),
        }

        def recover_children():
            return supervise_children(
                process,
                children,
                helper_factories,
                helper_health_checks,
                helper_health_state,
            )

        wait_required_component(
            process,
            "renderer heartbeat",
            lambda: wait_renderer_ready(
                process,
                base,
                RING_READY_MAGIC,
                recover_children=recover_children,
            ),
        )
        wait_required_component(
            process,
            "menu panel transport",
            lambda: wait_panel_transport_ready(
                pid,
                recover_children=recover_children,
            ),
        )

        print(f"CO-OP ARMADO pid={pid}", flush=True)
        publish_runtime_status(
            pid,
            "preparing",
            "Núcleo conectado. Carregue ou continue um save para concluir a validação.",
            warning="CO-OP STATUS: armed-state warning",
        )

        actors = wait_gameplay(process, None, children, recover_children)
        identity = sync_identity_when_stable(
            process,
            actors,
            recover_children=recover_children,
        )

        publish_runtime_status(
            pid,
            "preparing",
            "Save carregado. Confirmando controles, HUD e cores…",
            warning="CO-OP STATUS: validation warning",
        )

        wait_required_component(
            process,
            "renderer heartbeat",
            lambda: wait_renderer_ready(
                process,
                base,
                RING_READY_MAGIC,
                recover_children=recover_children,
            ),
        )
        wait_required_component(
            process,
            "menu panel transport",
            lambda: wait_panel_transport_ready(
                pid,
                recover_children=recover_children,
            ),
        )
        wait_runtime_ready(
            process,
            base,
            dual_base,
            damage_base,
            identity,
            children,
            start_with_all=story_party_patch.START_WITH_ALL,
            timeout=None,
            recover_children=recover_children,
        )
        def mark_global_commit_possible() -> None:
            nonlocal global_install_committed
            # Arm before the primary WriteProcessMemory call.  A failed API
            # result cannot prove that the bytes were not copied remotely.
            mark_persistent_runtime_resident(pid)
            global_install_committed = True

        finalize_global_install(
            process,
            (base, dual_base, damage_base),
            feature_bits=preflight.feature_bits,
            before_primary_commit=mark_global_commit_possible,
        )
        if not global_install_committed:
            raise AssertionError("global commit completed without arming residency")
        print(
            "CO-OP HEADLESS PRONTO "
            f"pid={pid} party="
            + ",".join(f"{slot}:{actor:#x}" for slot, actor, *_ in actors),
            flush=True,
        )
        publish_runtime_status(
            pid,
            "ready",
            "Co-op ativo. Launcher reconectado à sessão.",
            warning="CO-OP STATUS: ready-state warning",
        )

        while game_alive(process):
            recover_children()
            time.sleep(0.5)
        return 0
    finally:
        try:
            if owns_fresh_session:
                retire_supervised_children(
                    process,
                    children,
                    resident_committed=global_install_committed,
                )
        finally:
            try:
                if owns_fresh_session:
                    clear_runtime_status(pid)
            finally:
                try:
                    process.close()
                finally:
                    if owns_session_mutex and mutex is not None:
                        kernel32.CloseHandle(mutex)


def report_supervisor_recovery(pid: int, error: Exception) -> None:
    """Publish diagnostics without making observability a liveness dependency."""

    best_effort_diagnostic(
        "CO-OP SUPERVISOR RECOVERING "
        f"pid={pid} error={exception_diagnostic(error)}",
        stderr=True,
    )
    publish_runtime_status(
        pid,
        "preparing",
        "Co-op residente preservado. Reconectando a supervisao...",
        warning="CO-OP STATUS: reconnect warning",
    )


def run_persistent_supervisor() -> int:
    """Keep supervising one exact game process across Python-side failures.

    Resident native code and helper leases survive a committed supervisor
    failure.  Holding a second process handle pins lifetime identity even if
    Windows later recycles the numeric PID; every retry is also bound through
    the configured-PID environment inherited by replacement helpers.
    """

    global _PERSISTENT_SUPERVISOR_CONTEXT

    if _PERSISTENT_SUPERVISOR_CONTEXT is not None:
        raise SupervisorAlreadyRunning("persistent supervisor is already active")
    pid = coop.find_pid()
    mutex = None
    context = None
    lifetime_process = None
    pid_environment = runtime_paths.GAME_PID_ENV
    previous_pid = None
    previous_pid_observed = False
    restart_delay = SUPERVISOR_RESTART_INITIAL_SECONDS
    try:
        try:
            mutex = acquire_mutex(pid)
        except SupervisorAlreadyRunning:
            # runtime_entry performs an observation-only duplicate check first,
            # but another autostart worker can win between that check and this
            # atomic acquisition.  That benign race exits successfully.
            return 0
        context = _PersistentSupervisorContext(
            pid=pid,
            mutex=mutex,
            owner_thread_id=threading.get_ident(),
        )
        _PERSISTENT_SUPERVISOR_CONTEXT = context
        previous_pid = os.environ.get(pid_environment)
        previous_pid_observed = True
        os.environ[pid_environment] = str(pid)
        try:
            lifetime_process = open_supervisor_lifetime_process(pid)
        except OSError as error:
            if error.errno == 87:
                raise GameExitedBeforeStartup(
                    "jogo encerrou antes do supervisor abrir seu handle de vida"
                ) from error
            raise
        while True:
            try:
                return main()
            except GameExitedBeforeStartup:
                # Preserve the autostart contract: Steam can replace its
                # bootstrap process, and runtime_entry handles that case.
                raise
            except SupervisorAlreadyRunning:
                # A race with another healthy autostart worker is not a crash
                # and must never become an endless duplicate retry loop.
                return 0
            except Exception as error:
                if not context.resident_possible:
                    # Before an adopted commit or the primary commit attempt,
                    # no resident session is safe to claim.  Structural and
                    # PREPARED failures retain their original fail-closed path.
                    if not game_alive(lifetime_process):
                        raise GameExitedBeforeStartup(
                            "jogo encerrou durante a inicializacao do supervisor"
                        ) from error
                    raise
                if not game_alive(lifetime_process):
                    return 0
                report_supervisor_recovery(pid, error)
                time.sleep(restart_delay)
                restart_delay = min(
                    restart_delay * 2.0,
                    SUPERVISOR_RESTART_MAX_SECONDS,
                )
    finally:
        try:
            if lifetime_process is not None:
                lifetime_process.close()
        finally:
            try:
                if previous_pid_observed:
                    if previous_pid is None:
                        os.environ.pop(pid_environment, None)
                    else:
                        os.environ[pid_environment] = previous_pid
            finally:
                if _PERSISTENT_SUPERVISOR_CONTEXT is context:
                    _PERSISTENT_SUPERVISOR_CONTEXT = None
                if mutex is not None:
                    kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    try:
        raise SystemExit(run_persistent_supervisor())
    except Exception as error:
        print(f"ERRO AO LIGAR CO-OP HEADLESS: {error}", file=sys.stderr, flush=True)
        raise
