"""Static contracts for the Present-owned native camera bridge.

These checks deliberately cover invariants that the Python resampler tests
cannot observe: publication must never block DXGI Present, and every native
camera consumer must receive one atomically committed command snapshot rather
than fields copied piecemeal through the legacy command block.
"""

from __future__ import annotations

import sys
from pathlib import Path


DEVELOPMENT = Path(__file__).resolve().parents[1]
SOURCE = DEVELOPMENT / "source"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

import camera_frame_sync as sync  # noqa: E402


CPP_SOURCE = (
    DEVELOPMENT / "fontes_nativos" / "p2_menu_color_dx11.cpp"
).read_text(encoding="utf-8")
PYTHON_SOURCE = (SOURCE / "camera_frame_sync.py").read_text(encoding="utf-8")
RUNTIME_SOURCE = (SOURCE / "coop_runtime.py").read_text(encoding="utf-8")


def function_body(source: str, signature: str, next_signature: str) -> str:
    start = source.index(signature)
    end = source.index(next_signature, start)
    return source[start:end]


def test_output_abi_is_one_canonical_eight_float_snapshot_and_atomic_commit():
    assert sync.CAMERA_OUTPUT_PAYLOAD.format == "<8f"
    assert sync.CAMERA_OUTPUT_PAYLOAD.size == 0x20
    assert sync.CAMERA_OUTPUT_SLOT_OFFSETS == (0xB0, 0xD0)
    assert sync.CAMERA_OUTPUT_COMMIT_OFFSET == 0xA8
    assert sync.CAMERA_OUTPUT_ACTIVE_OFFSET == sync.CAMERA_OUTPUT_COMMIT_OFFSET + 4
    assert sync.CAMERA_OUTPUT_COMMIT_OFFSET % 8 == 0
    assert (
        sync.CAMERA_OUTPUT_SLOT_OFFSETS[-1] + sync.CAMERA_OUTPUT_SLOT_SIZE
        <= sync.CAMERA_FRAME_STATE_SIZE
    )

    payload = function_body(
        CPP_SOURCE,
        "struct CameraOutputPayload",
        "#pragma pack(pop)",
    )
    for field in (
        "float focus[3]",
        "float extra_distance",
        "float yaw_offset",
        "float pitch_offset",
        "float collision_exclusion[2]",
    ):
        assert field in payload

    publisher = function_body(
        CPP_SOURCE,
        "static bool publish_camera_output(",
        "static bool legacy_camera_gate_is_active(",
    )
    assert "InterlockedExchange64" in publisher
    assert "(uint64_t{1} << 32) | next_generation" in publisher
    assert "safe_write(slot, &output, sizeof(output))" in publisher


def test_present_bridge_resamples_zoom_instead_of_copying_producer_steps():
    state = function_body(
        CPP_SOURCE,
        "struct CameraBridgeState",
        "static CameraBridgeState g_camera_bridge",
    )
    resampler = function_body(
        CPP_SOURCE,
        "static bool resample_camera_goal(",
        "static void publish_camera_frame_for_present(",
    )
    publisher = function_body(
        CPP_SOURCE,
        "static bool publish_camera_output(",
        "static bool legacy_camera_gate_is_active(",
    )

    assert "float extra_distance" in state
    assert "goal.extra_distance - g_camera_bridge.extra_distance" in resampler
    assert "output_extra_distance = g_camera_bridge.extra_distance" in resampler
    assert "output.extra_distance = extra_distance" in publisher
    assert "output.extra_distance = goal->extra_distance" not in publisher


def test_renderer_never_republishes_camera_fields_through_legacy_command_memory():
    # The only command-block field the renderer may inspect is Python's
    # lifecycle gate.  Focus/zoom/orbit/exclusion travel together in the output
    # slot so no native hook can observe a mixed Present.
    for forbidden in (
        "kCameraCommandFocusOffset",
        "kCameraCommandYawPitchOffset",
        "kCameraCommandExclusionOffset",
        "publish_legacy_camera",
        "clear_legacy_camera_payload",
    ):
        assert forbidden not in CPP_SOURCE

    assert "build_camera_snapshot_helper" in RUNTIME_SOURCE
    assert "CAMERA_OUTPUT_COMMIT_OFFSET" in RUNTIME_SOURCE
    assert "CAMERA_OUTPUT_SLOT_OFFSETS" in RUNTIME_SOURCE


def test_present_camera_publication_uses_a_nonblocking_single_writer_guard():
    present = function_body(
        CPP_SOURCE,
        "static HRESULT STDMETHODCALLTYPE hooked_present(",
        "static LRESULT CALLBACK dummy_window_proc",
    )
    acquire = "InterlockedCompareExchange(&g_camera_publish_busy, 1, 0) == 0"
    release = "InterlockedExchange(&g_camera_publish_busy, 0)"
    original = "return g_original_present(swapchain, sync, flags)"

    assert "static volatile LONG g_camera_publish_busy = 0" in CPP_SOURCE
    assert acquire in present
    assert release in present
    assert original in present
    assert present.index(acquire) < present.index("publish_camera_frame_for_present")
    assert present.index("publish_camera_frame_for_present") < present.index(release)
    assert present.index(release) < present.index(original)
    for blocking_call in ("Sleep(", "WaitForSingleObject(", "WaitForSingleObjectEx("):
        assert blocking_call not in present


def test_camera_clock_is_qpc_derived_on_both_sides_of_the_mailbox():
    publisher = function_body(
        CPP_SOURCE,
        "static void publish_camera_frame_for_present(",
        "static bool valid_actor_for_handle(",
    )
    assert "high_resolution_tick_us()" in publisher
    assert "kCameraPresentTickSlotOffsets[next_present & 1u]" in publisher
    assert publisher.index("InterlockedExchange64") < publisher.index(
        "InterlockedIncrement(present_count)"
    )
    assert "GetTickCount64" not in publisher
    assert "QueryPerformanceCounter" in CPP_SOURCE
    assert "QueryPerformanceFrequency" in CPP_SOURCE

    assert "QueryPerformanceCounter" in PYTHON_SOURCE
    assert "QueryPerformanceFrequency" in PYTHON_SOURCE
    assert "1_000_000" in PYTHON_SOURCE
    assert "time.monotonic_ns()" in PYTHON_SOURCE
