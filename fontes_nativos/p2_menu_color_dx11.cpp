#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d11.h>
#include <cmath>
#include <dxgi.h>
#include <d3dcompiler.h>
#include <wincodec.h>
#include <stdint.h>
#include <stdio.h>
#include <math.h>
#include <wchar.h>
#include <algorithm>
#include <vector>

static HRESULT (STDMETHODCALLTYPE *g_original_present)(IDXGISwapChain*, UINT, UINT) = nullptr;
static void** g_present_slot = nullptr;
static volatile LONG g_hook_ready = 0;
static volatile LONG g_render_faulted = 0;
static HMODULE g_module = nullptr;
static constexpr DWORD kHookRetryInitialMilliseconds = 100;
static constexpr DWORD kHookRetryMaximumMilliseconds = 5000;

static ID3D11Device* g_device = nullptr;
static ID3D11DeviceContext* g_context = nullptr;
static IDXGISwapChain* g_game_swapchain = nullptr;
static ID3D11VertexShader* g_vs = nullptr;
static ID3D11PixelShader* g_ps = nullptr;
static ID3D11SamplerState* g_sampler = nullptr;
static ID3D11Buffer* g_constants = nullptr;
static ID3D11RasterizerState* g_rasterizer = nullptr;
static ID3D11DepthStencilState* g_depth = nullptr;
static ID3D11BlendState* g_blend = nullptr;
static ID3D11Texture2D* g_copy = nullptr;
static ID3D11ShaderResourceView* g_copy_srv = nullptr;
static ID3D11Texture2D* g_hand_atlas = nullptr;
static ID3D11ShaderResourceView* g_hand_atlas_srv = nullptr;
static UINT g_atlas_width = 0;
static UINT g_atlas_height = 0;
static UINT g_width = 0;
static UINT g_height = 0;
static DXGI_FORMAT g_format = DXGI_FORMAT_UNKNOWN;

static ID3D11VertexShader* g_ring_vs = nullptr;
static ID3D11PixelShader* g_ring_ps = nullptr;
static ID3D11InputLayout* g_ring_layout = nullptr;
static ID3D11Buffer* g_ring_constants = nullptr;
static ID3D11Buffer* g_ring_vertices = nullptr;
static ID3D11RasterizerState* g_ring_rasterizer = nullptr;
static ID3D11DepthStencilState* g_ring_depth = nullptr;
static ID3D11DepthStencilState* g_ring_depth_reverse = nullptr;
static ID3D11BlendState* g_ring_blend = nullptr;

static constexpr uint32_t kPanelMagic = 0x33504955u;
static constexpr uint32_t kPanelVersion = 1;
static constexpr uint32_t kPanelHeaderSize = 192;
static constexpr uint32_t kPanelCount = 17;
static constexpr uint32_t kPanelSlotHeaderSize = 64;
static constexpr uint32_t kPanelMaxWidth = 1024;
static constexpr uint32_t kPanelMaxHeight = 768;
static constexpr uint32_t kPanelPixelCapacity =
    kPanelMaxWidth * kPanelMaxHeight * 4;
static constexpr uint32_t kPanelSlotSize =
    kPanelSlotHeaderSize + kPanelPixelCapacity;
static constexpr ULONGLONG kPanelPublisherStaleMs = 1500;
static constexpr ULONGLONG kPanelMinimumViewAddress = 0x0000000100000000ull;
static constexpr float kPanelReferenceWidth = 1920.0f;
static constexpr float kPanelReferenceHeight = 1080.0f;

#pragma pack(push, 1)
struct SharedPanelHeader {
    uint32_t magic;
    uint32_t version;
    uint32_t header_size;
    uint32_t panel_count;
    uint32_t slot_size;
    uint32_t pixel_capacity;
    volatile LONG publisher_ready;
    volatile LONG renderer_ready;
    volatile LONG64 publisher_heartbeat;
    volatile LONG64 renderer_heartbeat;
    volatile LONG64 renderer_frame;
    uint8_t reserved0[8];
    volatile LONG acknowledged_generation[kPanelCount];
    uint8_t reserved1[60];
};

struct SharedPanelSlot {
    volatile LONG sequence;
    uint32_t generation;
    uint32_t visible;
    uint32_t width;
    uint32_t height;
    uint32_t stride;
    float logical_x;
    float logical_y;
    float opacity;
    uint32_t pixel_bytes;
    uint32_t z_order;
    uint32_t reserved0;
    uint8_t reserved1[16];
};
#pragma pack(pop)

static_assert(sizeof(SharedPanelHeader) == kPanelHeaderSize,
              "shared panel header layout drifted");
static_assert(sizeof(SharedPanelSlot) == kPanelSlotHeaderSize,
              "shared panel slot layout drifted");

struct PanelConstants {
    float rect[4];
};

struct PanelGpuState {
    ID3D11Texture2D* texture = nullptr;
    ID3D11ShaderResourceView* srv = nullptr;
    uint32_t width = 0;
    uint32_t height = 0;
    uint32_t generation = 0;
    bool visible = false;
    SharedPanelSlot metadata = {};
    std::vector<uint8_t> scratch;
};

static HANDLE g_panel_mapping_handle = nullptr;
static uint8_t* g_panel_mapping = nullptr;
static ULONGLONG g_panel_mapping_retry_at = 0;
static ID3D11VertexShader* g_panel_vs = nullptr;
static ID3D11PixelShader* g_panel_ps = nullptr;
static ID3D11Buffer* g_panel_constants = nullptr;
static ID3D11SamplerState* g_panel_sampler = nullptr;
static ID3D11BlendState* g_panel_blend = nullptr;
static PanelGpuState g_panel_states[kPanelCount];

static constexpr uintptr_t kCameraViewMatrix = 0x1F7E528;
static constexpr uintptr_t kActorManagerGlobal = 0x2098E18;
static constexpr uintptr_t kCameraProjectionMatrix = 0x2CB84B0;
static constexpr uintptr_t kRingReadyOffset = 0xD80;
static constexpr uintptr_t kRingP1HandleOffset = 0xD84;
static constexpr uintptr_t kRingP2HandleOffset = 0xD88;
static constexpr uintptr_t kRingDebugModeOffset = 0xD8C;
static constexpr uintptr_t kRingPresentCountOffset = 0xD90;
static constexpr uintptr_t kRingP1DrawCountOffset = 0xD94;
static constexpr uintptr_t kRingP2DrawCountOffset = 0xD98;
static constexpr uintptr_t kRingDepthSeenOffset = 0xD9C;
static constexpr uintptr_t kRingCaptureRequestOffset = 0xDA0;
static constexpr uintptr_t kRingP3HandleOffset = 0xDA4;
static constexpr uintptr_t kRingP3DrawCountOffset = 0xDA8;
static constexpr uintptr_t kCameraFrameStateOffset = 0x2000;
static constexpr uintptr_t kCameraGoalGenerationOffset = 0x08;
static constexpr uintptr_t kCameraGoalAcknowledgementOffset = 0x0C;
static constexpr uintptr_t kCameraPresentTickSlotOffsets[2] = {0x10, 0x18};
static constexpr uintptr_t kCameraGoalSlotOffsets[2] = {0x20, 0x60};
static constexpr uintptr_t kCameraOutputHeaderOffset = 0xA0;
static constexpr uintptr_t kCameraOutputCommitOffset = 0xA8;
static constexpr uintptr_t kCameraOutputSlotOffsets[2] = {0xB0, 0xD0};
static constexpr uintptr_t kCommandBlockOffset = 0x200;
static constexpr uintptr_t kCameraCommandActiveOffset = 0x3C;
static constexpr uintptr_t kP1ThemeColorOffset = 0xE20;
static constexpr uintptr_t kP2ThemeColorOffset = 0xB20;
static constexpr uintptr_t kP3ThemeColorOffset = 0x8260;
static constexpr uintptr_t kP1MenuThemeColorOffset = 0xE30;
static constexpr uintptr_t kP2MenuThemeColorOffset = 0xB30;
static constexpr uint32_t kRingReadyMagic = 0x33474E52;
static constexpr uint32_t kCameraFrameMagic = 0x324D4143;
static constexpr uint32_t kCameraFrameVersion = 2;
static constexpr float kCameraBridgeTimeConstantSeconds = 0.025f;
static constexpr float kCameraBridgePredictionHorizonSeconds = 0.050f;
static constexpr ULONGLONG kCameraBridgeStaleMicroseconds = 250000;
static constexpr float kCameraBridgeMinimumDeltaSeconds = 0.001f;
static constexpr float kCameraBridgeMaximumDeltaSeconds = 0.050f;
static constexpr UINT kRingSegments = 128;
static constexpr UINT kRingRadialStops = 5;
static constexpr UINT kRingMaxVertices =
    kRingSegments * (kRingRadialStops - 1) * 6;

#pragma pack(push, 1)
struct CameraGoalPayload {
    uint32_t active;
    uint32_t reset_epoch;
    uint64_t producer_tick_us;
    float focus[3];
    float velocity[3];
    float extra_distance;
    float yaw_offset;
    float pitch_offset;
    float collision_exclusion[2];
    uint32_t reserved;
};

struct CameraOutputPayload {
    float focus[3];
    float extra_distance;
    float yaw_offset;
    float pitch_offset;
    float collision_exclusion[2];
};
#pragma pack(pop)

static_assert(sizeof(CameraGoalPayload) == 0x40,
              "camera goal ABI layout drifted");
static_assert(sizeof(CameraOutputPayload) == 0x20,
              "camera output ABI layout drifted");

struct CameraBridgeState {
    uintptr_t cave = 0;
    bool active = false;
    bool have_cached_goal = false;
    uint32_t reset_epoch = 0;
    uint32_t cached_goal_generation = 0;
    ULONGLONG last_present_tick_us = 0;
    float focus[3] = {};
    float extra_distance = 0.0f;
    CameraGoalPayload cached_goal = {};
};

static CameraBridgeState g_camera_bridge;
static volatile LONG g_camera_publish_busy = 0;

struct RingVertex {
    float position[3];
    float color[4];
};

struct RingConstants {
    float view[16];
    float projection[16];
};

struct RingSnapshot {
    uint32_t handle = 0;
    uintptr_t actor = 0;
    float position[3] = {};
    float height = 1.79f;
    float radius = 0.5f;
};

struct RingAnimation {
    uint32_t handle = 0;
    float shown_center[3] = {};
    float shown_radius = 0.0f;
    float from_center[3] = {};
    float from_radius = 0.0f;
    float destination_center[3] = {};
    float destination_radius = 0.0f;
    ULONGLONG switch_started = 0;
    ULONGLONG last_valid = 0;
    ULONGLONG fade_started = 0;
    float fade_from = 0.0f;
    float opacity = 0.0f;
    bool visible_requested = false;
};

static RingAnimation g_ring_animation[3];
static uintptr_t g_cached_target_actor[3] = {};
static uint32_t g_cached_target_handle[3] = {};

static uintptr_t ring_handle_offset(int player) {
    if (player == 0) return kRingP1HandleOffset;
    if (player == 1) return kRingP2HandleOffset;
    return kRingP3HandleOffset;
}

static uintptr_t ring_draw_count_offset(int player) {
    if (player == 0) return kRingP1DrawCountOffset;
    if (player == 1) return kRingP2DrawCountOffset;
    return kRingP3DrawCountOffset;
}

static bool safe_read(uintptr_t address, void* destination, SIZE_T size) {
    if (!address || !destination || !size) return false;
    SIZE_T read = 0;
    return ReadProcessMemory(
        GetCurrentProcess(), reinterpret_cast<const void*>(address),
        destination, size, &read
    ) && read == size;
}

template <class T> static bool safe_read_value(uintptr_t address, T& value) {
    return safe_read(address, &value, sizeof(value));
}

static bool safe_write(uintptr_t address, const void* source, SIZE_T size) {
    if (!address || !source || !size) return false;
    SIZE_T written = 0;
    return WriteProcessMemory(
        GetCurrentProcess(), reinterpret_cast<void*>(address),
        source, size, &written
    ) && written == size;
}

static bool finite_world_value(float value) {
    return isfinite(value) && fabsf(value) < 1.0e8f;
}

static float smoothstep01(float value) {
    value = std::max(0.0f, std::min(1.0f, value));
    return value * value * (3.0f - 2.0f * value);
}

static uintptr_t g_cave_base = 0;

static bool coop_cave_magic_matches(uintptr_t address) {
    char observed[8] = {};
    return safe_read(address, observed, sizeof(observed)) &&
        memcmp(observed, "FF12COOP", sizeof(observed)) == 0;
}

static uintptr_t find_coop_cave() {
    if (g_cave_base && coop_cave_magic_matches(g_cave_base)) {
        return g_cave_base;
    }

    MEMORY_BASIC_INFORMATION information = {};
    for (uintptr_t address = 0x01000000ull; address < 0x7FFF0000ull;) {
        SIZE_T result = VirtualQuery((void*)address, &information, sizeof(information));
        if (!result) break;
        uintptr_t next = (uintptr_t)information.BaseAddress + information.RegionSize;
        if (information.State == MEM_COMMIT &&
            !(information.Protect & (PAGE_NOACCESS | PAGE_GUARD)) &&
            information.RegionSize >= 0x10000 &&
            coop_cave_magic_matches(
                reinterpret_cast<uintptr_t>(information.BaseAddress)
            )) {
            g_cave_base = (uintptr_t)information.BaseAddress;
            return g_cave_base;
        }
        address = next > address ? next : address + 0x10000;
    }
    return 0;
}

enum class CameraGoalReadResult {
    coherent,
    transient,
    unavailable,
    invalid,
};

static uint32_t next_camera_output_generation(uint32_t current) {
    uint32_t next = current + 1u;
    // Zero is the uncommitted sentinel.  Advancing FFFFFFFF directly to one
    // would also keep the odd slot selected and overwrite the snapshot that is
    // still committed, so wrap to the next even, non-zero generation instead.
    if (!next) next = 2u;
    return next;
}

static ULONGLONG high_resolution_tick_us() {
    static const LONGLONG frequency = []() -> LONGLONG {
        LARGE_INTEGER value = {};
        return QueryPerformanceFrequency(&value) ? value.QuadPart : 0;
    }();
    LARGE_INTEGER counter = {};
    if (frequency <= 0 || !QueryPerformanceCounter(&counter) ||
        counter.QuadPart < 0) {
        return 0;
    }
    const ULONGLONG ticks = static_cast<ULONGLONG>(counter.QuadPart);
    const ULONGLONG divisor = static_cast<ULONGLONG>(frequency);
    return (ticks / divisor) * 1000000ull +
        ((ticks % divisor) * 1000000ull) / divisor;
}

static bool finite_camera_goal(const CameraGoalPayload& goal) {
    if (goal.active > 1u) return false;
    if (!goal.active) return true;
    const float values[] = {
        goal.focus[0], goal.focus[1], goal.focus[2],
        goal.velocity[0], goal.velocity[1], goal.velocity[2],
        goal.extra_distance, goal.yaw_offset, goal.pitch_offset,
        goal.collision_exclusion[0], goal.collision_exclusion[1],
    };
    for (float value : values) {
        if (!finite_world_value(value)) return false;
    }
    return true;
}

static CameraGoalReadResult read_camera_goal(
        uintptr_t state, CameraGoalPayload& goal, uint32_t& generation) {
    uint32_t magic = 0;
    uint32_t version = 0;
    uint32_t generation_before = 0;
    if (!safe_read_value(state + 0x00, magic) ||
        !safe_read_value(state + 0x04, version) ||
        !safe_read_value(
            state + kCameraGoalGenerationOffset, generation_before)) {
        return CameraGoalReadResult::transient;
    }
    if (magic != kCameraFrameMagic || version != kCameraFrameVersion ||
        !generation_before) {
        return CameraGoalReadResult::unavailable;
    }

    const uintptr_t slot = state +
        kCameraGoalSlotOffsets[generation_before & 1u];
    CameraGoalPayload candidate = {};
    if (!safe_read(slot, &candidate, sizeof(candidate))) {
        return CameraGoalReadResult::transient;
    }
    MemoryBarrier();
    uint32_t generation_after = 0;
    if (!safe_read_value(
            state + kCameraGoalGenerationOffset, generation_after) ||
        generation_after != generation_before) {
        return CameraGoalReadResult::transient;
    }
    if (!finite_camera_goal(candidate)) {
        return CameraGoalReadResult::invalid;
    }
    goal = candidate;
    generation = generation_before;
    return CameraGoalReadResult::coherent;
}

static bool ensure_camera_output_header(uintptr_t state) {
    uint32_t magic = 0;
    uint32_t version = 0;
    if (safe_read_value(state + kCameraOutputHeaderOffset + 0x00, magic) &&
        safe_read_value(state + kCameraOutputHeaderOffset + 0x04, version) &&
        magic == kCameraFrameMagic && version == kCameraFrameVersion) {
        return true;
    }
    const uint32_t header[4] = {
        kCameraFrameMagic,
        kCameraFrameVersion,
        0u,
        0u,
    };
    return safe_write(
        state + kCameraOutputHeaderOffset, header, sizeof(header));
}

static bool publish_camera_output(
        uintptr_t state, bool active, const CameraGoalPayload* goal,
        const float focus[3], float extra_distance) {
    if (!ensure_camera_output_header(state)) return false;

    uint64_t committed = 0;
    if (!safe_read_value(
            state + kCameraOutputCommitOffset, committed)) {
        return false;
    }
    const uint32_t next_generation =
        next_camera_output_generation(static_cast<uint32_t>(committed));

    // An inactive commit is self-contained: readers reject it before touching
    // a slot, so fail-open does not depend on another payload write succeeding.
    if (!active) {
        MemoryBarrier();
        InterlockedExchange64(
            reinterpret_cast<volatile LONG64*>(
                state + kCameraOutputCommitOffset),
            static_cast<LONG64>(next_generation));
        return true;
    }
    if (!goal || !focus) return false;

    CameraOutputPayload output = {};
    output.focus[0] = focus[0];
    output.focus[1] = focus[1];
    output.focus[2] = focus[2];
    output.extra_distance = extra_distance;
    output.yaw_offset = goal->yaw_offset;
    output.pitch_offset = goal->pitch_offset;
    output.collision_exclusion[0] = goal->collision_exclusion[0];
    output.collision_exclusion[1] = goal->collision_exclusion[1];

    const uintptr_t slot = state +
        kCameraOutputSlotOffsets[next_generation & 1u];
    if (!safe_write(slot, &output, sizeof(output))) return false;
    MemoryBarrier();
    const uint64_t active_commit = (uint64_t{1} << 32) | next_generation;
    InterlockedExchange64(
        reinterpret_cast<volatile LONG64*>(
            state + kCameraOutputCommitOffset),
        static_cast<LONG64>(active_commit));
    return true;
}

static bool legacy_camera_gate_is_active(uintptr_t cave) {
    uint32_t camera_active = 0;
    return cave && safe_read_value(
        cave + kCommandBlockOffset + kCameraCommandActiveOffset,
        camera_active) && camera_active == 1u;
}

static void reset_camera_bridge(uintptr_t cave) {
    g_camera_bridge = CameraBridgeState{};
    g_camera_bridge.cave = cave;
}

static bool resample_camera_goal(
        const CameraGoalPayload& goal, ULONGLONG now,
        float output_focus[3], float& output_extra_distance) {
    if (!goal.active) return false;
    const ULONGLONG age_us = now >= goal.producer_tick_us
        ? now - goal.producer_tick_us
        : 0;
    if (age_us > kCameraBridgeStaleMicroseconds) return false;

    const float prediction_age = std::min(
        static_cast<float>(age_us) / 1000000.0f,
        kCameraBridgePredictionHorizonSeconds);
    float desired[3] = {};
    for (int index = 0; index < 3; ++index) {
        desired[index] = goal.focus[index] +
            goal.velocity[index] * prediction_age;
        if (!finite_world_value(desired[index])) return false;
    }

    const bool reset = !g_camera_bridge.active ||
        g_camera_bridge.reset_epoch != goal.reset_epoch;
    if (reset) {
        for (int index = 0; index < 3; ++index) {
            g_camera_bridge.focus[index] = desired[index];
        }
        g_camera_bridge.extra_distance = goal.extra_distance;
    } else {
        ULONGLONG elapsed_us = 0;
        if (now >= g_camera_bridge.last_present_tick_us) {
            elapsed_us = now - g_camera_bridge.last_present_tick_us;
        }
        const float dt = std::min(
            kCameraBridgeMaximumDeltaSeconds,
            std::max(
                kCameraBridgeMinimumDeltaSeconds,
                static_cast<float>(elapsed_us) / 1000000.0f));
        const bool prediction_live =
            age_us < static_cast<ULONGLONG>(
                kCameraBridgePredictionHorizonSeconds * 1000000.0f);
        const float alpha = 1.0f - expf(
            -dt / std::max(0.001f, kCameraBridgeTimeConstantSeconds));
        for (int index = 0; index < 3; ++index) {
            const float velocity = prediction_live
                ? goal.velocity[index]
                : 0.0f;
            const float predicted =
                g_camera_bridge.focus[index] + velocity * dt;
            const float resampled =
                predicted + (desired[index] - predicted) * alpha;
            if (!finite_world_value(resampled)) return false;
            g_camera_bridge.focus[index] = resampled;
        }
        const float resampled_distance = g_camera_bridge.extra_distance +
            (goal.extra_distance - g_camera_bridge.extra_distance) * alpha;
        if (!finite_world_value(resampled_distance)) return false;
        g_camera_bridge.extra_distance = resampled_distance;
    }

    g_camera_bridge.active = true;
    g_camera_bridge.reset_epoch = goal.reset_epoch;
    output_focus[0] = g_camera_bridge.focus[0];
    output_focus[1] = g_camera_bridge.focus[1];
    output_focus[2] = g_camera_bridge.focus[2];
    output_extra_distance = g_camera_bridge.extra_distance;
    return true;
}

static void publish_camera_frame_for_present(uintptr_t cave) {
    if (!cave) return;
    if (g_camera_bridge.cave != cave) reset_camera_bridge(cave);

    InterlockedExchange(
        reinterpret_cast<volatile LONG*>(cave + kRingReadyOffset),
        static_cast<LONG>(kRingReadyMagic));
    const ULONGLONG now = high_resolution_tick_us();
    const uintptr_t state = cave + kCameraFrameStateOffset;
    volatile LONG* present_count = reinterpret_cast<volatile LONG*>(
        cave + kRingPresentCountOffset);
    const uint32_t current_present = static_cast<uint32_t>(
        InterlockedCompareExchange(present_count, 0, 0));
    const uint32_t next_present = current_present + 1u;
    InterlockedExchange64(
        reinterpret_cast<volatile LONG64*>(
            state + kCameraPresentTickSlotOffsets[next_present & 1u]),
        static_cast<LONG64>(now));
    MemoryBarrier();
    InterlockedIncrement(present_count);
    if (!now) {
        g_camera_bridge.active = false;
        g_camera_bridge.have_cached_goal = false;
        publish_camera_output(state, false, nullptr, nullptr, 0.0f);
        return;
    }

    // Python exclusively owns this lifecycle gate.  The renderer may consume
    // and mirror camera payload while it is armed, but must never re-arm or
    // disarm the gate behind the lifecycle controller's cached state.
    if (!legacy_camera_gate_is_active(cave)) {
        g_camera_bridge.active = false;
        g_camera_bridge.have_cached_goal = false;
        g_camera_bridge.reset_epoch = 0;
        g_camera_bridge.cached_goal_generation = 0;
        publish_camera_output(state, false, nullptr, nullptr, 0.0f);
        g_camera_bridge.last_present_tick_us = now;
        return;
    }

    CameraGoalPayload observed_goal = {};
    uint32_t observed_generation = 0;
    const CameraGoalReadResult read_result = read_camera_goal(
        state, observed_goal, observed_generation);
    if (read_result == CameraGoalReadResult::coherent) {
        g_camera_bridge.cached_goal = observed_goal;
        g_camera_bridge.cached_goal_generation = observed_generation;
        g_camera_bridge.have_cached_goal = true;
        InterlockedExchange(
            reinterpret_cast<volatile LONG*>(
                state + kCameraGoalAcknowledgementOffset),
            static_cast<LONG>(observed_generation));
    } else if (read_result == CameraGoalReadResult::unavailable ||
               read_result == CameraGoalReadResult::invalid) {
        g_camera_bridge.have_cached_goal = false;
        g_camera_bridge.cached_goal_generation = 0;
    }

    bool active = false;
    float output_focus[3] = {
        g_camera_bridge.focus[0],
        g_camera_bridge.focus[1],
        g_camera_bridge.focus[2],
    };
    float output_extra_distance = g_camera_bridge.extra_distance;
    if (g_camera_bridge.have_cached_goal) {
        const CameraGoalPayload& goal = g_camera_bridge.cached_goal;
        active = resample_camera_goal(
            goal, now, output_focus, output_extra_distance);
        if (active && !legacy_camera_gate_is_active(cave)) {
            active = false;
        }
    }

    if (!active) {
        g_camera_bridge.active = false;
        g_camera_bridge.reset_epoch = 0;
    }
    const CameraGoalPayload* output_goal = active
        ? &g_camera_bridge.cached_goal
        : nullptr;
    if (!publish_camera_output(
            state, active, output_goal, output_focus, output_extra_distance)) {
        g_camera_bridge.active = false;
        g_camera_bridge.reset_epoch = 0;
        publish_camera_output(state, false, nullptr, nullptr, 0.0f);
    }
    g_camera_bridge.last_present_tick_us = now;
}

static bool valid_actor_for_handle(uintptr_t actor, uint32_t handle) {
    if (!actor || !handle) return false;
    uint32_t live_handle = 0;
    uintptr_t root = 0;
    uintptr_t physics = 0;
    return safe_read_value(actor + 0x08, live_handle) &&
        live_handle == handle &&
        safe_read_value(actor + 0x10, root) && root &&
        safe_read_value(root + 0xB8, physics) && physics;
}

static uintptr_t resolve_actor_handle(uint32_t handle, int player) {
    if (!handle) return 0;
    if (handle == g_cached_target_handle[player] &&
        valid_actor_for_handle(g_cached_target_actor[player], handle)) {
        return g_cached_target_actor[player];
    }

    uintptr_t manager = 0;
    uint32_t count = 0;
    if (!safe_read_value(kActorManagerGlobal, manager) || !manager ||
        !safe_read_value(manager, count) || !count || count > 4096) {
        return 0;
    }
    for (uint32_t actor_id = 0; actor_id < count; ++actor_id) {
        uintptr_t entry = 0;
        uintptr_t actor = 0;
        if (!safe_read_value(manager + 8 + actor_id * 8ull, entry) || !entry ||
            !safe_read_value(entry + 0x30, actor) || !actor) {
            continue;
        }
        if (valid_actor_for_handle(actor, handle)) {
            g_cached_target_handle[player] = handle;
            g_cached_target_actor[player] = actor;
            return actor;
        }
    }
    g_cached_target_handle[player] = 0;
    g_cached_target_actor[player] = 0;
    return 0;
}

static bool read_ring_snapshot(int player, RingSnapshot& snapshot) {
    uintptr_t cave = find_coop_cave();
    uint32_t enabled = 0;
    uint32_t handle = 0;
    const uintptr_t handle_offset = ring_handle_offset(player);
    if (!cave || !safe_read_value(cave + 0x208, enabled) || enabled != 1 ||
        !safe_read_value(cave + handle_offset, handle) || !handle) {
        return false;
    }

    uintptr_t actor = resolve_actor_handle(handle, player);
    if (!actor) return false;
    float position[3] = {};
    uintptr_t root = 0;
    uintptr_t physics = 0;
    float dimensions[2] = {1.79f, 0.5f};
    if (!safe_read(actor + 0xD0, position, sizeof(position)) ||
        !safe_read_value(actor + 0x10, root) || !root ||
        !safe_read_value(root + 0xB8, physics) || !physics) {
        return false;
    }
    safe_read(physics + 0xC0, dimensions, sizeof(dimensions));
    if (!finite_world_value(position[0]) ||
        !finite_world_value(position[1]) ||
        !finite_world_value(position[2])) {
        return false;
    }
    if (!isfinite(dimensions[0]) || dimensions[0] < 0.1f ||
        dimensions[0] > 100.0f) {
        dimensions[0] = 1.79f;
    }
    if (!isfinite(dimensions[1]) || dimensions[1] < 0.05f ||
        dimensions[1] > 50.0f) {
        dimensions[1] = 0.5f;
    }
    snapshot.handle = handle;
    snapshot.actor = actor;
    memcpy(snapshot.position, position, sizeof(position));
    snapshot.height = dimensions[0];
    snapshot.radius = dimensions[1];
    return true;
}

static bool read_ring_matrices(RingConstants& constants) {
    if (!safe_read(kCameraViewMatrix, constants.view, sizeof(constants.view)) ||
        !safe_read(kCameraProjectionMatrix, constants.projection,
                   sizeof(constants.projection))) {
        return false;
    }
    for (float value : constants.view) {
        if (!isfinite(value)) return false;
    }
    for (float value : constants.projection) {
        if (!isfinite(value)) return false;
    }

    if (fabsf(constants.view[9]) > 0.82f &&
        fabsf(constants.view[10]) < 0.60f &&
        fabsf(constants.view[6]) > 0.82f) {
        return false;
    }
    return true;
}

static void snapshot_center(const RingSnapshot& snapshot, float center[3]) {
    center[0] = snapshot.position[0];
    center[1] = snapshot.position[1] + snapshot.height * 0.54f;
    center[2] = snapshot.position[2];
}

static bool update_ring_animation(
        int player, float center[3], float& radius, float& opacity) {
    const ULONGLONG now = GetTickCount64();
    RingAnimation& animation = g_ring_animation[player];
    RingSnapshot snapshot;
    const bool valid = read_ring_snapshot(player, snapshot);
    bool requested = valid;

    uint32_t published_handle = 0;
    const uintptr_t cave = find_coop_cave();
    const uintptr_t handle_offset = ring_handle_offset(player);
    if (cave) safe_read_value(cave + handle_offset, published_handle);
    if (!valid && published_handle && animation.last_valid &&
        now - animation.last_valid <= 100) {
        requested = true;
    }

    if (valid) {
        float destination[3] = {};
        snapshot_center(snapshot, destination);
        const float destination_radius = snapshot.radius * 1.34f;
        if (snapshot.handle != animation.handle) {
            if (!animation.handle || animation.opacity <= 0.001f) {
                memcpy(animation.shown_center, destination,
                       sizeof(destination));
                animation.shown_radius = destination_radius;
            }
            memcpy(animation.from_center,
                   animation.shown_center, sizeof(destination));
            animation.from_radius = animation.shown_radius;
            animation.switch_started = now;
            animation.handle = snapshot.handle;
        }
        memcpy(animation.destination_center, destination,
               sizeof(destination));
        animation.destination_radius = destination_radius;
        animation.last_valid = now;
    }

    if (requested != animation.visible_requested) {
        animation.visible_requested = requested;
        animation.fade_started = now;
        animation.fade_from = animation.opacity;
    }
    const float fade_elapsed =
        animation.fade_started
            ? (float)(now - animation.fade_started) / 180.0f
            : 1.0f;
    const float fade_t = smoothstep01(fade_elapsed);
    const float fade_target = requested ? 1.0f : 0.0f;
    animation.opacity = animation.fade_from +
        (fade_target - animation.fade_from) * fade_t;

    if (!animation.handle || animation.opacity <= 0.001f) {
        if (!requested) animation.handle = 0;
        return false;
    }

    if (animation.switch_started) {
        const float switch_t = smoothstep01(
            (float)(now - animation.switch_started) / 180.0f
        );
        for (int axis = 0; axis < 3; ++axis) {
            animation.shown_center[axis] =
                animation.from_center[axis] +
                (animation.destination_center[axis] -
                 animation.from_center[axis]) * switch_t;
        }
        animation.shown_radius = animation.from_radius +
            (animation.destination_radius -
             animation.from_radius) * switch_t;
        if (switch_t >= 1.0f) animation.switch_started = 0;
    } else if (valid) {

        memcpy(animation.shown_center,
               animation.destination_center,
               sizeof(animation.shown_center));
        animation.shown_radius = animation.destination_radius;
    }

    memcpy(center, animation.shown_center, sizeof(float) * 3);
    radius = animation.shown_radius;
    opacity = animation.opacity;
    return radius > 0.02f;
}

static void log_line(const char* text) {
    wchar_t path[MAX_PATH] = {};
    FILE* f = nullptr;
    if (g_module && GetModuleFileNameW(g_module, path, MAX_PATH)) {
        wchar_t* slash = wcsrchr(path, L'\\');
        if (slash) {
            wcscpy_s(slash + 1, MAX_PATH - (slash + 1 - path),
                     L"p2_color_dx11.log");
            _wfopen_s(&f, path, L"a");
        }
    }
    if (!f) fopen_s(&f, "p2_color_dx11.log", "a");
    if (f) {
        fprintf(f, "%s\n", text);
        fclose(f);
    }
}

template <class T> static void release_ptr(T*& value) {
    if (value) {
        value->Release();
        value = nullptr;
    }
}

static bool compare_exchange_slot(
    void** slot,
    void* expected,
    void* replacement,
    void** observed
) {
    if (!slot) return false;
    DWORD old_protection = 0;
    if (!VirtualProtect(
            slot,
            sizeof(void*),
            PAGE_EXECUTE_READWRITE,
            &old_protection
        )) {
        return false;
    }
    void* actual = InterlockedCompareExchangePointer(
        reinterpret_cast<PVOID volatile*>(slot),
        replacement,
        expected
    );
    DWORD ignored = 0;
    VirtualProtect(slot, sizeof(void*), old_protection, &ignored);
    FlushInstructionCache(GetCurrentProcess(), slot, sizeof(void*));
    if (observed) *observed = actual;
    return actual == expected;
}

static bool pin_renderer_module() {
    HMODULE pinned = nullptr;
    return g_module && GetModuleHandleExW(
        GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
            GET_MODULE_HANDLE_EX_FLAG_PIN,
        reinterpret_cast<LPCWSTR>(g_module),
        &pinned
    );
}

static void release_partial_d3d_initialization() {
    for (PanelGpuState& state : g_panel_states) {
        release_ptr(state.srv);
        release_ptr(state.texture);
        state.width = 0;
        state.height = 0;
        state.generation = 0;
        state.visible = false;
        state.metadata = {};
        state.scratch.clear();
    }
    release_ptr(g_panel_blend);
    release_ptr(g_panel_sampler);
    release_ptr(g_panel_constants);
    release_ptr(g_panel_ps);
    release_ptr(g_panel_vs);
    release_ptr(g_ring_blend);
    release_ptr(g_ring_depth_reverse);
    release_ptr(g_ring_depth);
    release_ptr(g_ring_rasterizer);
    release_ptr(g_ring_vertices);
    release_ptr(g_ring_constants);
    release_ptr(g_ring_layout);
    release_ptr(g_ring_ps);
    release_ptr(g_ring_vs);
    release_ptr(g_copy_srv);
    release_ptr(g_copy);
    release_ptr(g_hand_atlas_srv);
    release_ptr(g_hand_atlas);
    release_ptr(g_blend);
    release_ptr(g_depth);
    release_ptr(g_rasterizer);
    release_ptr(g_constants);
    release_ptr(g_sampler);
    release_ptr(g_ps);
    release_ptr(g_vs);
    release_ptr(g_context);
    release_ptr(g_device);
    g_game_swapchain = nullptr;
    g_width = 0;
    g_height = 0;
    g_format = DXGI_FORMAT_UNKNOWN;
}

static const char* shader_source = R"HLSL(
Texture2D FrameTexture : register(t0);
Texture2D HandAtlas : register(t1);
SamplerState PointSampler : register(s0);

cbuffer MenuColorParameters : register(b0) {
    float4 MenuRect;
    float2 FrameSize;
    float2 Padding;
    float4 Cursor1Rect;
    float4 Cursor2Rect;
    float4 AtlasRect;
    float2 AtlasSize;
    float2 CursorPadding;
    float2 Cursor1Padding;
    float2 TailPad;
    float4 P1HelpBadgeRect;
    float4 P1HudTextRect;
    float4 P2HudTextRect;
    float4 P1HudTint;
    float4 P2HudTint;
    float4 P1MenuTint;
    float4 P2MenuTint;
    float4 P3HudTextRect;
    float4 P3HudTint;
};

struct VertexOutput {
    float4 position : SV_POSITION;
    float2 uv : TEXCOORD0;
};

VertexOutput VSMain(uint id : SV_VertexID) {
    VertexOutput output;
    float2 corner = float2((id << 1) & 2, id & 2);
    output.uv = corner;
    output.position = float4(
        corner * float2(2.0, -2.0) + float2(-1.0, 1.0),
        0.0,
        1.0
    );
    return output;
}

float handMask(float2 atlasPixel) {
    float3 sampleColor = HandAtlas.Sample(
        PointSampler, atlasPixel / AtlasSize
    ).rgb;
    float maximum = max(sampleColor.r, max(sampleColor.g, sampleColor.b));
    float minimum = min(sampleColor.r, min(sampleColor.g, sampleColor.b));
    float neutral = 1.0 - smoothstep(0.10, 0.34, maximum - minimum);

    return smoothstep(0.30, 0.58, maximum) * neutral;
}

float4 drawCursor(
    float4 color, float2 pixel, float4 rect, float3 tint, float opacity
) {
    if (rect.z <= rect.x || rect.w <= rect.y ||
        opacity <= 0.001 ||
        pixel.x < rect.x || pixel.y < rect.y ||
        pixel.x >= rect.z || pixel.y >= rect.w) {
        return color;
    }
    float2 local = saturate((pixel - rect.xy) / (rect.zw - rect.xy));
    float2 atlasPixel = lerp(AtlasRect.xy, AtlasRect.zw, local);
    float core = handMask(atlasPixel);

    float dropShadow = handMask(atlasPixel - float2(2.6, 5.4)) * 0.94;
    dropShadow = max(
        dropShadow,
        handMask(atlasPixel - float2(4.0, 7.0)) * 0.72
    );
    dropShadow = max(
        dropShadow,
        handMask(atlasPixel - float2(1.2, 8.2)) * 0.44
    );
    color.rgb = lerp(
        color.rgb, float3(0.010, 0.010, 0.016), dropShadow * 0.78 * opacity
    );

    float expanded = core;
    const float2 directions[8] = {
        float2(-1,0), float2(1,0), float2(0,-1), float2(0,1),
        float2(-1,-1), float2(1,-1), float2(-1,1), float2(1,1)
    };
    [unroll] for (int i = 0; i < 8; ++i) {
        expanded = max(expanded, handMask(atlasPixel + directions[i] * 1.7));
        expanded = max(expanded, handMask(atlasPixel + directions[i] * 3.0) * 0.88);
    }
    float outline = saturate(expanded - core * 0.76);
    color.rgb = lerp(
        color.rgb, float3(0.035, 0.035, 0.045), outline * 0.96 * opacity
    );

    float3 source = HandAtlas.Sample(PointSampler, atlasPixel / AtlasSize).rgb;
    float luminance = max(source.r, max(source.g, source.b));
    float shade = saturate((luminance - 0.10) / 0.82);
    float3 glove = lerp(float3(0.68, 0.68, 0.70), float3(1.0, 1.0, 1.0), shade);
    glove *= tint;
    color.rgb = lerp(color.rgb, glove, core * opacity);
    return color;
}

float3 characterCursorTint(float4 theme) {
    if (theme.a <= 0.0) {
        return float3(1.0, 1.0, 1.0);
    }
    float maximum = max(theme.r, max(theme.g, theme.b));
    float3 vivid = theme.rgb / max(maximum, 0.001);

    return lerp(float3(0.72, 0.72, 0.74), vivid, 0.62);
}

float3 characterPanelTint(float4 theme) {

    if (theme.a <= 0.0) {
        return float3(1.0, 0.85, 0.14);
    }
    float maximum = max(theme.r, max(theme.g, theme.b));
    return theme.rgb / max(maximum, 0.001);
}

float partyHudBlueMask(float3 sampleColor) {
    float maximum = max(sampleColor.r, max(sampleColor.g, sampleColor.b));
    float minimum = min(sampleColor.r, min(sampleColor.g, sampleColor.b));
    float saturation = maximum > 0.00001
        ? (maximum - minimum) / maximum : 0.0;
    float blueDominance = sampleColor.b - max(sampleColor.r, sampleColor.g);

    float chromaticBlue = smoothstep(0.130, 0.190, blueDominance);
    float luminousBlue =
        smoothstep(0.560, 0.720, maximum) *
        smoothstep(0.025, 0.075, blueDominance);
    float authoredBlue = max(chromaticBlue, luminousBlue);
    return authoredBlue *
           smoothstep(0.025, 0.18, maximum) *
           smoothstep(0.035, 0.16, saturation);
}

float4 recolorPartyHud(
    float4 color, float2 pixel, float4 rect, float4 tint
) {
    if (tint.a <= 0.0 || pixel.x < rect.x || pixel.y < rect.y ||
        pixel.x >= rect.z || pixel.y >= rect.w) {
        return color;
    }
    float maximum = max(color.r, max(color.g, color.b));

    float mask = partyHudBlueMask(color.rgb);

    const float2 directions[4] = {
        float2(-1, 0), float2(1, 0), float2(0, -1), float2(0, 1)
    };
    float support = 0.0;
    [unroll] for (int i = 0; i < 4; ++i) {
        float2 neighbourPixel = clamp(
            pixel + directions[i], rect.xy + 1.0, rect.zw - 1.0
        );
        float3 neighbour = FrameTexture.Sample(
            PointSampler, neighbourPixel / FrameSize
        ).rgb;
        support += partyHudBlueMask(neighbour);
    }
    support *= 0.25;
    float coherence = smoothstep(0.05, 0.70, support);
    float coverage = smoothstep(0.055, 0.52, maximum);
    float tintMaximum = max(tint.r, max(tint.g, tint.b));
    float tintMinimum = min(tint.r, min(tint.g, tint.b));
    float tintChroma = tintMaximum > 0.00001
        ? (tintMaximum - tintMinimum) / tintMaximum : 0.0;

    float themeSoftness = smoothstep(0.35, 0.65, tintChroma);
    const float3 lumaWeights = float3(0.2126, 0.7152, 0.0722);
    float sourceLuma = dot(color.rgb, lumaWeights);
    float tintLuma = dot(tint.rgb, lumaWeights);
    float3 themed = tint.rgb *
        saturate(sourceLuma / max(tintLuma, 0.001));
    float themeMix = lerp(
        0.32 + 0.68 * coverage,
        0.18 + 0.52 * coverage,
        themeSoftness
    );
    float3 softThemed = lerp(sourceLuma.xxx, themed, themeMix);
    float strength = mask * lerp(0.48, 0.96, coherence) *
                     lerp(0.58, 1.0, coverage);
    strength *= lerp(1.0, 0.72, themeSoftness);
    color.rgb = lerp(color.rgb, softThemed, strength);
    return color;
}

float4 PSMain(VertexOutput input) : SV_TARGET {
    float4 color = FrameTexture.Sample(PointSampler, input.uv);
    float2 pixel = input.position.xy;

    bool insideP1HelpBadge =
        pixel.x >= P1HelpBadgeRect.x && pixel.y >= P1HelpBadgeRect.y &&
        pixel.x < P1HelpBadgeRect.z && pixel.y < P1HelpBadgeRect.w;
    if (insideP1HelpBadge) {
        float warm = min(color.r, color.g) - color.b;
        float value = max(color.r, max(color.g, color.b));

        float warmRatio = warm / max(value, 0.001);
        float rubyMask = smoothstep(0.025, 0.16, warmRatio) *
                         smoothstep(0.004, 0.045, value);

        float shade = saturate(value);
        float themeMaximum = max(
            P1MenuTint.r, max(P1MenuTint.g, P1MenuTint.b)
        );
        float3 badgeTheme = P1MenuTint.a > 0.0
            ? P1MenuTint.rgb / max(themeMaximum, 0.001)
            : float3(0.46, 0.76, 1.0);
        float3 themedShade = lerp(
            badgeTheme * 0.12,
            saturate(badgeTheme * 1.04 + 0.035),
            shade
        );
        color.rgb = lerp(color.rgb, themedShade, rubyMask);
    }

    color = recolorPartyHud(color, pixel, P1HudTextRect, P1HudTint);
    color = recolorPartyHud(color, pixel, P2HudTextRect, P2HudTint);
    color = recolorPartyHud(color, pixel, P3HudTextRect, P3HudTint);

    bool insideMenu = pixel.x >= MenuRect.x && pixel.y >= MenuRect.y &&
                      pixel.x < MenuRect.z && pixel.y < MenuRect.w;
    if (insideMenu) {

    float2 center = (MenuRect.xy + MenuRect.zw) * 0.5;
    float2 halfSize = (MenuRect.zw - MenuRect.xy) * 0.5;
    float cornerRadius = 17.0;
    float2 rounded = abs(pixel - center) - (halfSize - cornerRadius);
    float signedDistance = length(max(rounded, 0.0)) +
                           min(max(rounded.x, rounded.y), 0.0) - cornerRadius;
    if (signedDistance <= 0.0) {

    float maximum = max(color.r, max(color.g, color.b));
    float minimum = min(color.r, min(color.g, color.b));
    float saturation = maximum > 0.00001 ? (maximum - minimum) / maximum : 0.0;
    float blueDominance = color.b - max(color.r, color.g);

    bool bluePixel = color.b > color.r + 0.015 &&
                     color.b > color.g + 0.006 &&
                     saturation > 0.055 && maximum > 0.025;
    if (bluePixel) {

    float textHighlight = smoothstep(0.42, 0.92, maximum) *
                          smoothstep(0.08, 0.32, blueDominance);
    float themedValue = maximum * lerp(1.0, 0.72, textHighlight);
    float chroma = themedValue * saturate(saturation * 0.94);
    float base = themedValue - chroma;
    float3 panelTheme = characterPanelTint(P2MenuTint);
    float3 themedPanel = saturate(
        base.xxx + panelTheme * chroma * 1.035 + 0.004
    );
    float strength = saturate((blueDominance - 0.003) / 0.050);
    strength *= saturate((saturation - 0.025) / 0.16);
    strength = max(strength, 0.28);
    color.rgb = lerp(color.rgb, themedPanel, strength);
    }
    }
    }

    float3 p1CursorTint = characterCursorTint(P1MenuTint);
    float3 p2CursorTint = characterCursorTint(P2MenuTint);
    float trail1 = Cursor1Padding.x;
    float trailAlpha1 = Cursor1Padding.y;
    float4 v1_5 = Cursor1Rect + float4(0, trail1 * 0.92, 0, trail1 * 0.92);
    float4 v1_4 = Cursor1Rect + float4(0, trail1 * 0.74, 0, trail1 * 0.74);
    float4 v1_3 = Cursor1Rect + float4(0, trail1 * 0.56, 0, trail1 * 0.56);
    float4 v1_2 = Cursor1Rect + float4(0, trail1 * 0.38, 0, trail1 * 0.38);
    float4 v1_1 = Cursor1Rect + float4(0, trail1 * 0.20, 0, trail1 * 0.20);
    color = drawCursor(color, pixel, v1_5, p1CursorTint, trailAlpha1 * 0.24);
    color = drawCursor(color, pixel, v1_4, p1CursorTint, trailAlpha1 * 0.32);
    color = drawCursor(color, pixel, v1_3, p1CursorTint, trailAlpha1 * 0.42);
    color = drawCursor(color, pixel, v1_2, p1CursorTint, trailAlpha1 * 0.52);
    color = drawCursor(color, pixel, v1_1, p1CursorTint, trailAlpha1 * 0.62);
    color = drawCursor(
        color, pixel, Cursor1Rect, p1CursorTint, 1.0
    );

    float trail = CursorPadding.x;
    float trailAlpha = CursorPadding.y;
    float4 ghost5 = Cursor2Rect + float4(0, trail * 0.92, 0, trail * 0.92);
    float4 ghost4 = Cursor2Rect + float4(0, trail * 0.74, 0, trail * 0.74);
    float4 ghost3 = Cursor2Rect + float4(0, trail * 0.56, 0, trail * 0.56);
    float4 ghost2 = Cursor2Rect + float4(0, trail * 0.38, 0, trail * 0.38);
    float4 ghost1 = Cursor2Rect + float4(0, trail * 0.20, 0, trail * 0.20);
    color = drawCursor(
        color, pixel, ghost5, p2CursorTint, trailAlpha * 0.24
    );
    color = drawCursor(
        color, pixel, ghost4, p2CursorTint, trailAlpha * 0.32
    );
    color = drawCursor(
        color, pixel, ghost3, p2CursorTint, trailAlpha * 0.42
    );
    color = drawCursor(
        color, pixel, ghost2, p2CursorTint, trailAlpha * 0.52
    );
    color = drawCursor(
        color, pixel, ghost1, p2CursorTint, trailAlpha * 0.62
    );
    color = drawCursor(
        color, pixel, Cursor2Rect, p2CursorTint, 1.0
    );
    return color;
}
)HLSL";

static const char* ring_shader_source = R"HLSL(
cbuffer RingMatrices : register(b0) {
    row_major float4x4 View;
    row_major float4x4 Projection;
};

struct RingInput {
    float3 position : POSITION;
    float4 color : COLOR0;
};

struct RingOutput {
    float4 position : SV_POSITION;
    float4 color : COLOR0;
};

RingOutput RingVS(RingInput input) {
    RingOutput output;
    float4 camera = mul(float4(input.position, 1.0), View);
    output.position = mul(camera, Projection);
    output.color = input.color;
    return output;
}

float4 RingPS(RingOutput input) : SV_TARGET {
    return input.color;
}
)HLSL";

static const char* panel_shader_source = R"HLSL(
cbuffer PanelRect : register(b0) {
    float4 Rect;
};

struct PanelOutput {
    float4 position : SV_POSITION;
    float2 uv : TEXCOORD0;
};

PanelOutput PanelVS(uint vertex_id : SV_VertexID) {
    PanelOutput output;
    float2 uv = float2(
        (vertex_id & 1u) ? 1.0 : 0.0,
        (vertex_id & 2u) ? 1.0 : 0.0
    );
    output.position = float4(
        lerp(Rect.x, Rect.z, uv.x),
        lerp(Rect.y, Rect.w, uv.y),
        0.0,
        1.0
    );
    output.uv = uv;
    return output;
}

Texture2D PanelTexture : register(t0);
SamplerState PanelSampler : register(s0);

float4 PanelPS(PanelOutput input) : SV_TARGET {

    return PanelTexture.Sample(PanelSampler, input.uv);
}
)HLSL";

static bool compile_shaders() {
    ID3DBlob* vs_blob = nullptr;
    ID3DBlob* ps_blob = nullptr;
    ID3DBlob* errors = nullptr;
    HRESULT hr = D3DCompile(
        shader_source, strlen(shader_source), "ff12_p2_menu_color", nullptr,
        nullptr, "VSMain", "vs_4_0", D3DCOMPILE_OPTIMIZATION_LEVEL3, 0,
        &vs_blob, &errors
    );
    if (FAILED(hr)) {
        if (errors) log_line((const char*)errors->GetBufferPointer());
        release_ptr(errors);
        return false;
    }
    release_ptr(errors);
    hr = D3DCompile(
        shader_source, strlen(shader_source), "ff12_p2_menu_color", nullptr,
        nullptr, "PSMain", "ps_4_0", D3DCOMPILE_OPTIMIZATION_LEVEL3, 0,
        &ps_blob, &errors
    );
    if (FAILED(hr)) {
        if (errors) log_line((const char*)errors->GetBufferPointer());
        release_ptr(errors);
        release_ptr(vs_blob);
        return false;
    }
    release_ptr(errors);
    hr = g_device->CreateVertexShader(
        vs_blob->GetBufferPointer(), vs_blob->GetBufferSize(), nullptr, &g_vs
    );
    if (SUCCEEDED(hr)) {
        hr = g_device->CreatePixelShader(
            ps_blob->GetBufferPointer(), ps_blob->GetBufferSize(), nullptr, &g_ps
        );
    }
    release_ptr(vs_blob);
    release_ptr(ps_blob);
    return SUCCEEDED(hr);
}

static bool create_panel_resources() {
    ID3DBlob* vs_blob = nullptr;
    ID3DBlob* ps_blob = nullptr;
    ID3DBlob* errors = nullptr;
    HRESULT hr = D3DCompile(
        panel_shader_source, strlen(panel_shader_source),
        "ff12_headless_panel_compositor", nullptr, nullptr,
        "PanelVS", "vs_4_0", D3DCOMPILE_OPTIMIZATION_LEVEL3, 0,
        &vs_blob, &errors
    );
    if (FAILED(hr)) {
        if (errors) log_line((const char*)errors->GetBufferPointer());
        release_ptr(errors);
        return false;
    }
    release_ptr(errors);
    hr = D3DCompile(
        panel_shader_source, strlen(panel_shader_source),
        "ff12_headless_panel_compositor", nullptr, nullptr,
        "PanelPS", "ps_4_0", D3DCOMPILE_OPTIMIZATION_LEVEL3, 0,
        &ps_blob, &errors
    );
    if (FAILED(hr)) {
        if (errors) log_line((const char*)errors->GetBufferPointer());
        release_ptr(errors);
        release_ptr(vs_blob);
        return false;
    }
    release_ptr(errors);
    hr = g_device->CreateVertexShader(
        vs_blob->GetBufferPointer(), vs_blob->GetBufferSize(), nullptr,
        &g_panel_vs
    );
    if (SUCCEEDED(hr)) {
        hr = g_device->CreatePixelShader(
            ps_blob->GetBufferPointer(), ps_blob->GetBufferSize(), nullptr,
            &g_panel_ps
        );
    }
    release_ptr(vs_blob);
    release_ptr(ps_blob);
    if (FAILED(hr)) return false;

    D3D11_BUFFER_DESC constants = {};
    constants.ByteWidth = sizeof(PanelConstants);
    constants.Usage = D3D11_USAGE_DYNAMIC;
    constants.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    constants.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    if (FAILED(g_device->CreateBuffer(
            &constants, nullptr, &g_panel_constants))) return false;

    D3D11_SAMPLER_DESC sampler = {};
    sampler.Filter = D3D11_FILTER_MIN_MAG_LINEAR_MIP_POINT;
    sampler.AddressU = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler.AddressV = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler.MaxLOD = D3D11_FLOAT32_MAX;
    if (FAILED(g_device->CreateSamplerState(
            &sampler, &g_panel_sampler))) return false;

    D3D11_BLEND_DESC blend = {};
    blend.RenderTarget[0].BlendEnable = TRUE;
    blend.RenderTarget[0].SrcBlend = D3D11_BLEND_ONE;
    blend.RenderTarget[0].DestBlend = D3D11_BLEND_INV_SRC_ALPHA;
    blend.RenderTarget[0].BlendOp = D3D11_BLEND_OP_ADD;
    blend.RenderTarget[0].SrcBlendAlpha = D3D11_BLEND_ONE;
    blend.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_INV_SRC_ALPHA;
    blend.RenderTarget[0].BlendOpAlpha = D3D11_BLEND_OP_ADD;
    blend.RenderTarget[0].RenderTargetWriteMask =
        D3D11_COLOR_WRITE_ENABLE_ALL;
    if (FAILED(g_device->CreateBlendState(
            &blend, &g_panel_blend))) return false;
    return true;
}

static bool create_ring_resources() {
    ID3DBlob* vs_blob = nullptr;
    ID3DBlob* ps_blob = nullptr;
    ID3DBlob* errors = nullptr;
    HRESULT hr = D3DCompile(
        ring_shader_source, strlen(ring_shader_source), "ff12_p2_target_ring",
        nullptr, nullptr, "RingVS", "vs_4_0",
        D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, &vs_blob, &errors
    );
    if (FAILED(hr)) {
        if (errors) log_line((const char*)errors->GetBufferPointer());
        release_ptr(errors);
        return false;
    }
    release_ptr(errors);
    hr = D3DCompile(
        ring_shader_source, strlen(ring_shader_source), "ff12_p2_target_ring",
        nullptr, nullptr, "RingPS", "ps_4_0",
        D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, &ps_blob, &errors
    );
    if (FAILED(hr)) {
        if (errors) log_line((const char*)errors->GetBufferPointer());
        release_ptr(errors);
        release_ptr(vs_blob);
        return false;
    }
    release_ptr(errors);

    hr = g_device->CreateVertexShader(
        vs_blob->GetBufferPointer(), vs_blob->GetBufferSize(), nullptr,
        &g_ring_vs
    );
    if (SUCCEEDED(hr)) {
        hr = g_device->CreatePixelShader(
            ps_blob->GetBufferPointer(), ps_blob->GetBufferSize(), nullptr,
            &g_ring_ps
        );
    }
    const D3D11_INPUT_ELEMENT_DESC input[] = {
        {"POSITION", 0, DXGI_FORMAT_R32G32B32_FLOAT, 0, 0,
         D3D11_INPUT_PER_VERTEX_DATA, 0},
        {"COLOR", 0, DXGI_FORMAT_R32G32B32A32_FLOAT, 0, 12,
         D3D11_INPUT_PER_VERTEX_DATA, 0},
    };
    if (SUCCEEDED(hr)) {
        hr = g_device->CreateInputLayout(
            input, ARRAYSIZE(input), vs_blob->GetBufferPointer(),
            vs_blob->GetBufferSize(), &g_ring_layout
        );
    }
    release_ptr(vs_blob);
    release_ptr(ps_blob);
    if (FAILED(hr)) return false;

    D3D11_BUFFER_DESC constants = {};
    constants.ByteWidth = sizeof(RingConstants);
    constants.Usage = D3D11_USAGE_DYNAMIC;
    constants.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    constants.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    if (FAILED(g_device->CreateBuffer(
            &constants, nullptr, &g_ring_constants))) return false;

    D3D11_BUFFER_DESC vertices = {};
    vertices.ByteWidth = sizeof(RingVertex) * kRingMaxVertices;
    vertices.Usage = D3D11_USAGE_DYNAMIC;
    vertices.BindFlags = D3D11_BIND_VERTEX_BUFFER;
    vertices.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    if (FAILED(g_device->CreateBuffer(
            &vertices, nullptr, &g_ring_vertices))) return false;

    D3D11_RASTERIZER_DESC rasterizer = {};
    rasterizer.FillMode = D3D11_FILL_SOLID;
    rasterizer.CullMode = D3D11_CULL_NONE;
    rasterizer.DepthClipEnable = TRUE;
    rasterizer.MultisampleEnable = TRUE;
    rasterizer.AntialiasedLineEnable = TRUE;
    if (FAILED(g_device->CreateRasterizerState(
            &rasterizer, &g_ring_rasterizer))) return false;

    D3D11_DEPTH_STENCIL_DESC depth = {};
    depth.DepthEnable = TRUE;
    depth.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ZERO;
    depth.DepthFunc = D3D11_COMPARISON_LESS_EQUAL;
    depth.StencilEnable = FALSE;
    if (FAILED(g_device->CreateDepthStencilState(&depth, &g_ring_depth))) {
        return false;
    }

    depth.DepthFunc = D3D11_COMPARISON_GREATER_EQUAL;
    if (FAILED(g_device->CreateDepthStencilState(
            &depth, &g_ring_depth_reverse))) {
        return false;
    }

    D3D11_BLEND_DESC blend = {};
    blend.RenderTarget[0].BlendEnable = TRUE;
    blend.RenderTarget[0].SrcBlend = D3D11_BLEND_SRC_ALPHA;
    blend.RenderTarget[0].DestBlend = D3D11_BLEND_INV_SRC_ALPHA;
    blend.RenderTarget[0].BlendOp = D3D11_BLEND_OP_ADD;
    blend.RenderTarget[0].SrcBlendAlpha = D3D11_BLEND_ONE;
    blend.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_INV_SRC_ALPHA;
    blend.RenderTarget[0].BlendOpAlpha = D3D11_BLEND_OP_ADD;
    blend.RenderTarget[0].RenderTargetWriteMask =
        D3D11_COLOR_WRITE_ENABLE_ALL;
    if (FAILED(g_device->CreateBlendState(&blend, &g_ring_blend))) return false;
    return true;
}

static bool create_static_resources() {

    if (!compile_shaders()) return false;

    D3D11_SAMPLER_DESC sampler = {};
    sampler.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
    sampler.AddressU = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler.AddressV = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler.MaxLOD = D3D11_FLOAT32_MAX;
    if (FAILED(g_device->CreateSamplerState(&sampler, &g_sampler))) return false;

    D3D11_BUFFER_DESC buffer = {};

    buffer.ByteWidth = 256;
    buffer.Usage = D3D11_USAGE_DYNAMIC;
    buffer.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    buffer.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    if (FAILED(g_device->CreateBuffer(&buffer, nullptr, &g_constants))) return false;

    D3D11_RASTERIZER_DESC rasterizer = {};
    rasterizer.FillMode = D3D11_FILL_SOLID;
    rasterizer.CullMode = D3D11_CULL_NONE;
    rasterizer.DepthClipEnable = TRUE;
    if (FAILED(g_device->CreateRasterizerState(&rasterizer, &g_rasterizer))) return false;

    D3D11_DEPTH_STENCIL_DESC depth = {};
    depth.DepthEnable = FALSE;
    depth.StencilEnable = FALSE;
    if (FAILED(g_device->CreateDepthStencilState(&depth, &g_depth))) return false;

    D3D11_BLEND_DESC blend = {};
    blend.RenderTarget[0].BlendEnable = FALSE;
    blend.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
    if (FAILED(g_device->CreateBlendState(&blend, &g_blend))) return false;
    if (!create_ring_resources()) return false;
    if (!create_panel_resources()) return false;
    return true;
}

static bool create_frame_resources(ID3D11Texture2D* backbuffer) {
    D3D11_TEXTURE2D_DESC desc = {};
    backbuffer->GetDesc(&desc);
    if (g_copy && desc.Width == g_width && desc.Height == g_height &&
        desc.Format == g_format) {
        return true;
    }
    release_ptr(g_copy_srv);
    release_ptr(g_copy);
    g_width = desc.Width;
    g_height = desc.Height;
    g_format = desc.Format;

    D3D11_TEXTURE2D_DESC copy_desc = desc;
    copy_desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    copy_desc.CPUAccessFlags = 0;
    copy_desc.Usage = D3D11_USAGE_DEFAULT;
    copy_desc.MiscFlags = 0;
    if (FAILED(g_device->CreateTexture2D(&copy_desc, nullptr, &g_copy))) return false;
    if (FAILED(g_device->CreateShaderResourceView(g_copy, nullptr, &g_copy_srv))) {
        release_ptr(g_copy);
        return false;
    }
    return true;
}

static bool capture_backbuffer_bmp(ID3D11Texture2D* backbuffer) {
    if (!backbuffer || !g_device || !g_context) return false;
    D3D11_TEXTURE2D_DESC desc = {};
    backbuffer->GetDesc(&desc);
    const bool rgba =
        desc.Format == DXGI_FORMAT_R8G8B8A8_UNORM ||
        desc.Format == DXGI_FORMAT_R8G8B8A8_UNORM_SRGB;
    const bool bgra =
        desc.Format == DXGI_FORMAT_B8G8R8A8_UNORM ||
        desc.Format == DXGI_FORMAT_B8G8R8A8_UNORM_SRGB;
    if ((!rgba && !bgra) || desc.SampleDesc.Count != 1) return false;

    D3D11_TEXTURE2D_DESC staging_desc = desc;
    staging_desc.BindFlags = 0;
    staging_desc.MiscFlags = 0;
    staging_desc.Usage = D3D11_USAGE_STAGING;
    staging_desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    ID3D11Texture2D* staging = nullptr;
    if (FAILED(g_device->CreateTexture2D(
            &staging_desc, nullptr, &staging)) || !staging) {
        return false;
    }
    g_context->CopyResource(staging, backbuffer);
    D3D11_MAPPED_SUBRESOURCE mapped = {};
    if (FAILED(g_context->Map(staging, 0, D3D11_MAP_READ, 0, &mapped))) {
        staging->Release();
        return false;
    }

    const size_t row_bytes = (size_t)desc.Width * 4u;
    std::vector<BYTE> pixels(row_bytes * desc.Height);
    for (UINT y = 0; y < desc.Height; ++y) {
        const BYTE* source = reinterpret_cast<const BYTE*>(mapped.pData) +
            (size_t)(desc.Height - 1u - y) * mapped.RowPitch;
        BYTE* destination = pixels.data() + (size_t)y * row_bytes;
        for (UINT x = 0; x < desc.Width; ++x) {
            const BYTE* pixel = source + x * 4u;
            BYTE* output = destination + x * 4u;
            if (rgba) {
                output[0] = pixel[2];
                output[1] = pixel[1];
                output[2] = pixel[0];
            } else {
                output[0] = pixel[0];
                output[1] = pixel[1];
                output[2] = pixel[2];
            }
            output[3] = 0xff;
        }
    }
    g_context->Unmap(staging, 0);
    staging->Release();

    wchar_t path[MAX_PATH] = {};
    GetModuleFileNameW(g_module, path, MAX_PATH);
    wchar_t* slash = wcsrchr(path, L'\\');
    if (!slash) return false;
    wcscpy_s(slash + 1, MAX_PATH - (slash + 1 - path),
             L"ring_capture.bmp");

    BITMAPFILEHEADER file_header = {};
    BITMAPINFOHEADER info_header = {};
    info_header.biSize = sizeof(info_header);
    info_header.biWidth = (LONG)desc.Width;
    info_header.biHeight = (LONG)desc.Height;
    info_header.biPlanes = 1;
    info_header.biBitCount = 32;
    info_header.biCompression = BI_RGB;
    info_header.biSizeImage = (DWORD)pixels.size();
    file_header.bfType = 0x4D42;
    file_header.bfOffBits = sizeof(file_header) + sizeof(info_header);
    file_header.bfSize = file_header.bfOffBits + info_header.biSizeImage;

    HANDLE file = CreateFileW(
        path, GENERIC_WRITE, FILE_SHARE_READ, nullptr, CREATE_ALWAYS,
        FILE_ATTRIBUTE_NORMAL, nullptr
    );
    if (file == INVALID_HANDLE_VALUE) return false;
    DWORD written = 0;
    bool ok = WriteFile(
        file, &file_header, sizeof(file_header), &written, nullptr
    ) && written == sizeof(file_header);
    ok = ok && WriteFile(
        file, &info_header, sizeof(info_header), &written, nullptr
    ) && written == sizeof(info_header);
    ok = ok && WriteFile(
        file, pixels.data(), (DWORD)pixels.size(), &written, nullptr
    ) && written == pixels.size();
    CloseHandle(file);
    return ok;
}
struct ConstantData {
    float rect[4];
    float frame_size[2];
    float padding[2];
    float cursor1[4];
    float cursor2[4];
    float atlas_rect[4];
    float atlas_size[2];
    float cursor_padding[2];
    float cursor1_padding[2];
    float tail_pad[2];
    float p1_help_badge[4];
    float p1_hud_text[4];
    float p2_hud_text[4];
    float p1_hud_tint[4];
    float p2_hud_tint[4];
    float p1_menu_tint[4];
    float p2_menu_tint[4];
    float p3_hud_text[4];
    float p3_hud_tint[4];
};
static_assert(sizeof(ConstantData) == 256,
              "MenuColorParameters constant-buffer layout drifted");

static bool read_theme_tint(
    uintptr_t cave, uintptr_t colour_offset, float tint[4]
) {
    int32_t rgb[3] = {};
    if (!safe_read(cave + colour_offset, rgb, sizeof(rgb))) return false;
    if ((rgb[0] | rgb[1] | rgb[2]) == 0) return false;
    for (int channel = 0; channel < 3; ++channel) {
        const int32_t value = std::max(0, std::min(127, rgb[channel]));
        tint[channel] = (float)value / 127.0f;
    }
    tint[3] = 1.0f;
    return true;
}

static constexpr uint16_t kEmptyPartyMember = 0xFFFFu;
static constexpr uintptr_t kBattleInfoGlobal = 0x02EBF190u;
static constexpr uintptr_t kBattleCurrentLayoutOffset = 0x5A5Au;
static constexpr uintptr_t kBattleUiLayoutOffset = 0x5A6Cu;
static constexpr uintptr_t kNativeRenderingFlags = 0x022C7F90u;
static constexpr uint16_t kNativeUiRenderEnabledMask = 1u << 3;
static constexpr uintptr_t kPresentationSuppressedOffset = 0x8600u;
static constexpr uintptr_t kWaitMenuAppliedOffset = 0x251u;
static constexpr uintptr_t kNativeCommandActor = 0x02092730u;
static constexpr uintptr_t kNativeCommandFlag = 0x02092734u;

static bool owned_wait_presentation(uintptr_t cave) {

    uint8_t applied = 0;
    uint32_t actor = 0;
    uint32_t flag = 0;
    return cave &&
        safe_read_value(cave + kWaitMenuAppliedOffset, applied) &&
        safe_read_value(kNativeCommandActor, actor) &&
        safe_read_value(kNativeCommandFlag, flag) &&
        applied == 1 && actor != 0 && flag == 1;
}

static bool presentation_overlays_suppressed(uintptr_t cave) {

    uint16_t native_flags = kNativeUiRenderEnabledMask;
    if (safe_read_value(kNativeRenderingFlags, native_flags) &&
        !(native_flags & kNativeUiRenderEnabledMask) &&
        !owned_wait_presentation(cave)) {
        return true;
    }
    uint32_t external_gate = 0;
    return cave &&
        safe_read_value(cave + kPresentationSuppressedOffset, external_gate) &&
        external_gate != 0;
}

static constexpr uint32_t party_hud_character_order(uint16_t member) {

    switch (member) {
        case 0: return 0;
        case 3: return 1;
        case 2: return 2;
        case 4: return 3;
        case 1: return 4;
        case 5: return 5;
        default: return 0x100u + static_cast<uint32_t>(member);
    }
}

static constexpr bool party_hud_story_members_follow_mains() {
    for (uint16_t member = 6; member <= 14; ++member) {
        if (party_hud_character_order(member) <=
            party_hud_character_order(5)) {
            return false;
        }
    }
    return true;
}

static constexpr int party_hud_visual_row(
    const uint16_t (&layout)[4], uint32_t slot
) {
    if (slot >= 4 || layout[slot] == kEmptyPartyMember) return -1;
    const uint16_t target_member = layout[slot];
    const uint32_t target_order = party_hud_character_order(target_member);
    uint32_t active_count = 0;
    uint32_t canonical_rank = 0;
    for (uint32_t index = 0; index < 4; ++index) {
        const uint16_t member = layout[index];
        if (member == kEmptyPartyMember) continue;
        ++active_count;
        const uint32_t member_order = party_hud_character_order(member);
        if (member_order < target_order ||
            (member_order == target_order && member < target_member)) {
            ++canonical_rank;
        }
    }
    if (active_count == 0 || canonical_rank >= active_count) return -1;
    return static_cast<int>(4u - active_count + canonical_rank);
}

static constexpr bool party_hud_three_member_permutations_keep_rows(
    uint16_t top_member, uint16_t middle_member, uint16_t bottom_member
) {
    const uint16_t layouts[6][4] = {
        {top_member, middle_member, bottom_member, kEmptyPartyMember},
        {top_member, bottom_member, middle_member, kEmptyPartyMember},
        {middle_member, top_member, bottom_member, kEmptyPartyMember},
        {middle_member, bottom_member, top_member, kEmptyPartyMember},
        {bottom_member, top_member, middle_member, kEmptyPartyMember},
        {bottom_member, middle_member, top_member, kEmptyPartyMember},
    };
    for (uint32_t permutation = 0; permutation < 6; ++permutation) {
        for (uint32_t slot = 0; slot < 3; ++slot) {
            const uint16_t member = layouts[permutation][slot];
            const int expected_row =
                member == top_member ? 1 : member == middle_member ? 2 : 3;
            if (party_hud_visual_row(layouts[permutation], slot) !=
                expected_row) {
                return false;
            }
        }
    }
    return true;
}

static constexpr bool party_hud_all_solo_mains_use_bottom_row() {
    for (uint16_t member = 0; member <= 5; ++member) {
        const uint16_t layout[4] = {
            member, kEmptyPartyMember, kEmptyPartyMember, kEmptyPartyMember
        };
        if (party_hud_visual_row(layout, 0) != 3) return false;
    }
    return true;
}

static constexpr bool party_hud_all_story_members_use_guest_row() {
    for (uint16_t member = 6; member <= 14; ++member) {
        const uint16_t layout[4] = {
            member, 5, kEmptyPartyMember, kEmptyPartyMember
        };
        if (party_hud_visual_row(layout, 1) != 2 ||
            party_hud_visual_row(layout, 0) != 3) {
            return false;
        }
    }
    return true;
}

static_assert(party_hud_character_order(0) < party_hud_character_order(3));
static_assert(party_hud_character_order(3) < party_hud_character_order(2));
static_assert(party_hud_character_order(2) < party_hud_character_order(4));
static_assert(party_hud_character_order(4) < party_hud_character_order(1));
static_assert(party_hud_character_order(1) < party_hud_character_order(5));
static_assert(party_hud_character_order(5) < party_hud_character_order(13));
static_assert(party_hud_story_members_follow_mains());
static_assert(party_hud_all_solo_mains_use_bottom_row());
static_assert(party_hud_three_member_permutations_keep_rows(0, 4, 5));
static_assert(party_hud_three_member_permutations_keep_rows(2, 1, 5));
static_assert(party_hud_all_story_members_use_guest_row());

static constexpr uint16_t kHudLayoutSolo[4] = {
    5, kEmptyPartyMember, kEmptyPartyMember, kEmptyPartyMember
};
static constexpr uint16_t kHudLayoutTwo[4] = {
    2, 0, kEmptyPartyMember, kEmptyPartyMember
};
static constexpr uint16_t kHudLayoutThree[4] = {
    5, 2, 1, kEmptyPartyMember
};
static constexpr uint16_t kHudLayoutFour[4] = {13, 5, 0, 2};
static constexpr uint16_t kHudLayoutGap[4] = {
    5, kEmptyPartyMember, 0, kEmptyPartyMember
};
static constexpr uint16_t kHudLayoutVaanBaschPenelo[4] = {
    5, 4, 0, kEmptyPartyMember
};
static constexpr uint16_t kHudLayoutStoryVariant[4] = {
    7, 5, kEmptyPartyMember, kEmptyPartyMember
};
static_assert(party_hud_visual_row(kHudLayoutSolo, 0) == 3);
static_assert(party_hud_visual_row(kHudLayoutTwo, 0) == 3);
static_assert(party_hud_visual_row(kHudLayoutTwo, 1) == 2);
static_assert(party_hud_visual_row(kHudLayoutThree, 1) == 1);
static_assert(party_hud_visual_row(kHudLayoutThree, 2) == 2);
static_assert(party_hud_visual_row(kHudLayoutThree, 0) == 3);
static_assert(party_hud_visual_row(kHudLayoutFour, 2) == 0);
static_assert(party_hud_visual_row(kHudLayoutFour, 3) == 1);
static_assert(party_hud_visual_row(kHudLayoutFour, 1) == 2);
static_assert(party_hud_visual_row(kHudLayoutFour, 0) == 3);
static_assert(party_hud_visual_row(kHudLayoutGap, 2) == 2);
static_assert(party_hud_visual_row(kHudLayoutGap, 0) == 3);
static_assert(party_hud_visual_row(kHudLayoutGap, 1) == -1);
static_assert(
    party_hud_visual_row(kHudLayoutVaanBaschPenelo, 2) == 1
);
static_assert(
    party_hud_visual_row(kHudLayoutVaanBaschPenelo, 1) == 2
);
static_assert(
    party_hud_visual_row(kHudLayoutVaanBaschPenelo, 0) == 3
);
static_assert(
    party_hud_visual_row(kHudLayoutStoryVariant, 1) == 2
);
static_assert(
    party_hud_visual_row(kHudLayoutStoryVariant, 0) == 3
);

static bool coherent_party_layouts(
    const uint16_t (&current)[4], const uint16_t (&ui)[4]
) {
    uint32_t current_count = 0;
    uint32_t ui_count = 0;
    for (uint32_t left = 0; left < 4; ++left) {
        if (current[left] != kEmptyPartyMember) {
            ++current_count;
            for (uint32_t right = left + 1; right < 4; ++right) {
                if (current[left] == current[right]) return false;
            }
        }
        if (ui[left] != kEmptyPartyMember) {
            ++ui_count;
            for (uint32_t right = left + 1; right < 4; ++right) {
                if (ui[left] == ui[right]) return false;
            }
        }
    }
    if (current_count == 0 || current_count != ui_count) return false;

    for (uint32_t current_index = 0; current_index < 4; ++current_index) {
        const uint16_t member = current[current_index];
        if (member == kEmptyPartyMember) continue;
        bool found = false;
        for (uint32_t ui_index = 0; ui_index < 4; ++ui_index) {
            if (ui[ui_index] == member) {
                found = true;
                break;
            }
        }
        if (!found) return false;
    }
    return true;
}

static bool read_current_party_layout(uint16_t (&layout)[4]) {
    uintptr_t battle_info = 0;
    if (!safe_read_value(kBattleInfoGlobal, battle_info) || !battle_info) {
        return false;
    }
    uint16_t ui_layout[4] = {};
    uint16_t second_current[4] = {};
    uint16_t second_ui[4] = {};
    const uintptr_t current_address =
        battle_info + kBattleCurrentLayoutOffset;
    const uintptr_t ui_address = battle_info + kBattleUiLayoutOffset;
    if (!safe_read(current_address, layout, sizeof(layout)) ||
        !safe_read(ui_address, ui_layout, sizeof(ui_layout)) ||
        !safe_read(current_address, second_current, sizeof(second_current)) ||
        !safe_read(ui_address, second_ui, sizeof(second_ui)) ||
        memcmp(layout, second_current, sizeof(layout)) != 0 ||
        memcmp(ui_layout, second_ui, sizeof(ui_layout)) != 0) {
        return false;
    }
    return coherent_party_layouts(layout, ui_layout);
}

static bool party_hud_layout_reduced() {

    static constexpr uintptr_t kFieldMrpPointer = 0x0209AC68;
    static constexpr uint32_t kLiveMrpMagic = 0x0150524D;
    uintptr_t base = 0;
    uint32_t magic = 0;
    uint32_t group_count = 0;
    uint32_t group_list = 0;
    if (!safe_read_value(kFieldMrpPointer, base) || !base ||
        !safe_read_value(base, magic) || magic != kLiveMrpMagic ||
        !safe_read_value(base + 0x1C, group_count) || group_count <= 40 ||
        !safe_read_value(base + 0x20, group_list) || !group_list) {
        return false;
    }
    const uintptr_t group =
        static_cast<uintptr_t>(group_list) + 40u * 0x14u;
    int16_t rect[4] = {};
    int32_t gap = 0;
    return safe_read(group + 4, rect, sizeof(rect)) &&
        safe_read_value(group + 0x0C, gap) &&
        rect[0] == 122 && rect[1] == 242 &&
        rect[2] == 900 && rect[3] == 44 && gap == 42;
}

static void resolve_hud_theme(
    uintptr_t cave, uintptr_t slot_offset, uintptr_t colour_offset,
    const uint16_t (&layout)[4], float scale, float offset_x, float offset_y,
    bool reduced_layout,
    float rect[4], float tint[4]
) {
    uint32_t slot = 0xFFFFFFFFu;
    if (!safe_read_value(cave + slot_offset, slot) || slot > 3) return;
    const int visual_row = party_hud_visual_row(layout, slot);
    if (visual_row < 0) return;

    const bool is_bottom = visual_row == 3;
    const float top = 790.0f + 60.0f * (float)visual_row;
    rect[0] = offset_x + 1248.0f * scale;
    rect[1] = offset_y + (top + 4.0f) * scale
              - (is_bottom ? 1.0f : 0.0f);
    rect[2] = offset_x + 1822.0f * scale;
    rect[3] = offset_y + (top + (is_bottom ? 56.0f : 60.0f)) * scale
              - (is_bottom ? 4.0f : 0.0f);
    if (reduced_layout) {

        const float anchor_x = offset_x + 1972.0f * scale;
        const float anchor_y = offset_y + 1027.0f * scale;
        rect[0] = anchor_x + (rect[0] - anchor_x) * 0.75f;
        rect[1] = anchor_y + (rect[1] - anchor_y) * 0.75f;
        rect[2] = anchor_x + (rect[2] - anchor_x) * 0.75f;
        rect[3] = anchor_y + (rect[3] - anchor_y) * 0.75f;
    }
    read_theme_tint(cave, colour_offset, tint);
}

static bool p2_menu_rect(ConstantData& constants) {
    const float scale = fminf(
        (float)g_width / kPanelReferenceWidth,
        (float)g_height / kPanelReferenceHeight
    );
    const float offset_x =
        ((float)g_width - kPanelReferenceWidth * scale) * 0.5f;
    const float offset_y =
        ((float)g_height - kPanelReferenceHeight * scale) * 0.5f;

    constants.rect[0] = constants.rect[1] = -10000.0f;
    constants.rect[2] = constants.rect[3] = -9999.0f;
    constants.frame_size[0] = (float)g_width;
    constants.frame_size[1] = (float)g_height;
    constants.padding[0] = constants.padding[1] = 0.0f;
    constants.atlas_rect[0] = 783.0f;
    constants.atlas_rect[1] = 760.0f;
    constants.atlas_rect[2] = 843.0f;
    constants.atlas_rect[3] = 808.0f;
    constants.atlas_size[0] = (float)g_atlas_width;
    constants.atlas_size[1] = (float)g_atlas_height;
    constants.cursor_padding[0] = constants.cursor_padding[1] = 0.0f;
    constants.cursor1_padding[0] = constants.cursor1_padding[1] = 0.0f;
    constants.tail_pad[0] = constants.tail_pad[1] = 0.0f;
    constants.p1_help_badge[0] = constants.p1_help_badge[1] = -10000.0f;
    constants.p1_help_badge[2] = constants.p1_help_badge[3] = -9999.0f;
    constants.p1_hud_text[0] = constants.p1_hud_text[1] = -10000.0f;
    constants.p1_hud_text[2] = constants.p1_hud_text[3] = -9999.0f;
    constants.p2_hud_text[0] = constants.p2_hud_text[1] = -10000.0f;
    constants.p2_hud_text[2] = constants.p2_hud_text[3] = -9999.0f;
    memset(constants.p1_hud_tint, 0, sizeof(constants.p1_hud_tint));
    memset(constants.p2_hud_tint, 0, sizeof(constants.p2_hud_tint));
    memset(constants.p1_menu_tint, 0, sizeof(constants.p1_menu_tint));
    memset(constants.p2_menu_tint, 0, sizeof(constants.p2_menu_tint));
    constants.p3_hud_text[0] = constants.p3_hud_text[1] = -10000.0f;
    constants.p3_hud_text[2] = constants.p3_hud_text[3] = -9999.0f;
    memset(constants.p3_hud_tint, 0, sizeof(constants.p3_hud_tint));

    uintptr_t cave = find_coop_cave();
    if (!cave) return true;

    uint32_t runtime_enabled = 0;
    if (safe_read_value(cave + 0x208, runtime_enabled) &&
        runtime_enabled == 1) {
        uint16_t current_layout[4] = {};
        if (read_current_party_layout(current_layout)) {
            const bool reduced_hud = party_hud_layout_reduced();
            resolve_hud_theme(
                cave, 0x26C, 0xE20, current_layout,
                scale, offset_x, offset_y, reduced_hud,
                constants.p1_hud_text, constants.p1_hud_tint
            );
            resolve_hud_theme(
                cave, 0x270, 0xB20, current_layout,
                scale, offset_x, offset_y, reduced_hud,
                constants.p2_hud_text, constants.p2_hud_tint
            );
            resolve_hud_theme(
                cave, 0x821C, kP3ThemeColorOffset, current_layout,
                scale, offset_x, offset_y, reduced_hud,
                constants.p3_hud_text, constants.p3_hud_tint
            );
        }
        read_theme_tint(
            cave, kP1MenuThemeColorOffset, constants.p1_menu_tint
        );
        read_theme_tint(
            cave, kP2MenuThemeColorOffset, constants.p2_menu_tint
        );

        if (constants.p1_menu_tint[3] <= 0.0f) {
            memcpy(
                constants.p1_menu_tint,
                constants.p1_hud_tint,
                sizeof(constants.p1_menu_tint)
            );
        }
        if (constants.p2_menu_tint[3] <= 0.0f) {
            memcpy(
                constants.p2_menu_tint,
                constants.p2_hud_tint,
                sizeof(constants.p2_menu_tint)
            );
        }
    }

    {
        static float cached_badge[4] = {
            -10000.0f, -10000.0f, -9999.0f, -9999.0f
        };
        static ULONGLONG cached_badge_at = 0;
        const uintptr_t badge = cave + 0xCF40;
        uint32_t badge_magic = 0;
        uint32_t before = 1;
        float logical_badge[4] = {};
        uint32_t after = 0;
        const bool badge_read =
            safe_read_value(badge + 0x00, badge_magic) &&
            safe_read_value(badge + 0x04, before) &&
            safe_read(badge + 0x08, logical_badge, sizeof(logical_badge)) &&
            safe_read_value(badge + 0x18, after);
        const ULONGLONG badge_now = GetTickCount64();
        const bool coherent_badge =
            badge_read && badge_magic == 0x47444142u &&
            before == after && !(before & 1);
        const bool valid_badge = coherent_badge &&
            logical_badge[2] > logical_badge[0] + 8.0f &&
            logical_badge[3] > logical_badge[1] + 8.0f &&
            logical_badge[0] > -128.0f &&
            logical_badge[1] > -128.0f;
        if (valid_badge) {
            memcpy(cached_badge, logical_badge, sizeof(cached_badge));
            cached_badge_at = badge_now;
        }

        if (valid_badge ||
            (cached_badge_at && badge_now - cached_badge_at <= 320)) {
            const float* shown_badge =
                valid_badge ? logical_badge : cached_badge;
            constants.p1_help_badge[0] = offset_x + shown_badge[0] * scale;
            constants.p1_help_badge[1] = offset_y + shown_badge[1] * scale;
            constants.p1_help_badge[2] = offset_x + shown_badge[2] * scale;
            constants.p1_help_badge[3] = offset_y + shown_badge[3] * scale;
        }
    }

    const uintptr_t feed = cave + 0xD00;
    uint32_t magic = 0;
    uint32_t sequence_before = 1;
    float flat_rects[8] = {};
    uint32_t flags = 0;
    float panel_rect[4] = {};
    uint32_t sequence_after = 0;
    const bool feed_read =
        safe_read_value(feed + 0x00, magic) &&
        safe_read_value(feed + 0x04, sequence_before) &&
        safe_read(feed + 0x08, flat_rects, sizeof(flat_rects)) &&
        safe_read_value(feed + 0x28, flags) &&
        safe_read(feed + 0x2C, panel_rect, sizeof(panel_rect)) &&
        safe_read_value(feed + 0x3C, sequence_after);

    static float cached_rects[8] = {};
    static float cached_panel[4] = {};
    static uint32_t cached_flags = 0;
    static ULONGLONG cached_at = 0;
    const ULONGLONG feed_now = GetTickCount64();
    uint32_t logical_open_value = 0;
    const bool logical_open =
        safe_read_value(cave + 0x1D08, logical_open_value) &&
        logical_open_value == 1;
    const bool coherent = feed_read && magic == 0x53525543u &&
        sequence_before == sequence_after && !(sequence_before & 1);
    if (coherent) {

        const bool transient_p2_gap = logical_open && !(flags & 4) &&
            (cached_flags & 4) && feed_now - cached_at <= 50;
        if (transient_p2_gap) {
            memcpy(flat_rects, cached_rects, sizeof(flat_rects));
            memcpy(panel_rect, cached_panel, sizeof(panel_rect));
            flags = cached_flags;
        } else {
            memcpy(cached_rects, flat_rects, sizeof(cached_rects));
            memcpy(cached_panel, panel_rect, sizeof(cached_panel));
            cached_flags = flags;
            cached_at = feed_now;
        }
    } else if (logical_open && cached_at && feed_now - cached_at <= 100) {
        memcpy(flat_rects, cached_rects, sizeof(flat_rects));
        memcpy(panel_rect, cached_panel, sizeof(panel_rect));
        flags = cached_flags;
    } else {
        return true;
    }
    if (!(flags & 7)) return true;

    const float x = panel_rect[0];
    const float y = panel_rect[1];
    const float right = panel_rect[2];
    const float bottom = panel_rect[3];
    const float width = right - x;
    const float height = bottom - y;
    const bool panel_valid = (flags & 4) &&
        width >= 100.0f && height >= 100.0f &&
        width <= 1000.0f && height <= 1000.0f;

    if (panel_valid) {
        constants.rect[0] = offset_x + (x - 9.0f) * scale;
        constants.rect[1] = offset_y + (y - 3.0f) * scale;
        constants.rect[2] = offset_x + (right + 24.0f) * scale;
        constants.rect[3] = offset_y + (bottom + 20.0f) * scale;
    } else {

        constants.rect[0] = constants.rect[1] = -10000.0f;
        constants.rect[2] = constants.rect[3] = -9999.0f;
    }
    constants.frame_size[0] = (float)g_width;
    constants.frame_size[1] = (float)g_height;
    constants.padding[0] = constants.padding[1] = 0.0f;

    constants.atlas_rect[0] = 783.0f;
    constants.atlas_rect[1] = 760.0f;
    constants.atlas_rect[2] = 843.0f;
    constants.atlas_rect[3] = 808.0f;
    constants.atlas_size[0] = (float)g_atlas_width;
    constants.atlas_size[1] = (float)g_atlas_height;
    constants.cursor_padding[0] = constants.cursor_padding[1] = 0.0f;
    constants.cursor1_padding[0] = constants.cursor1_padding[1] = 0.0f;
    constants.tail_pad[0] = constants.tail_pad[1] = 0.0f;

    {
        const ULONGLONG now = GetTickCount64();

        const uint16_t* gv = (const uint16_t*)(cave + 0x1760);
        const uint16_t* gp = (const uint16_t*)(cave + 0x1768);
        if (flags & 1) {
            const float phase1 = (float)(now % 625ull);
            float jab1 = 0.0f;
            if (phase1 >= 390.0f && phase1 < 440.0f) {
                float t = (phase1 - 390.0f) / 50.0f;
                t = t * t * (3.0f - 2.0f * t);
                jab1 = 4.5f * t;
            } else if (phase1 >= 440.0f && phase1 < 515.0f) {
                float t = (phase1 - 440.0f) / 75.0f;
                t = t * t * (3.0f - 2.0f * t);
                jab1 = 4.5f * (1.0f - t);
            }
            static bool position_initialized1 = false;
            static float current_top1 = 0.0f;
            static float start_top1 = 0.0f;
            static float target_top1 = 0.0f;
            static ULONGLONG move_started1 = 0;
            const float wanted_top1 = gv[2] > 0 ? (float)gv[1] : flat_rects[1] + 6.0f;
            float travel_t1 = 1.0f;
            if (!position_initialized1) {
                current_top1 = start_top1 = target_top1 = wanted_top1;
                move_started1 = now;
                position_initialized1 = true;
            } else {
                travel_t1 = fminf(1.0f, (float)(now - move_started1) / 150.0f);
                float eased = 1.0f - powf(1.0f - travel_t1, 3.0f);
                current_top1 = start_top1 + (target_top1 - start_top1) * eased;
                if (fabsf(wanted_top1 - target_top1) > 0.5f) {
                    start_top1 = current_top1;
                    target_top1 = wanted_top1;
                    move_started1 = now;
                    travel_t1 = 0.0f;
                }
            }
            if (travel_t1 < 1.0f) {
                float eased = 1.0f - powf(1.0f - travel_t1, 3.0f);
                current_top1 = start_top1 + (target_top1 - start_top1) * eased;
            } else {
                current_top1 = target_top1;
            }
            float v1x0 = gv[2] > 0 ? (float)gv[0] : flat_rects[0];
            float v1x1 = gv[2] > 0 ? (float)(gv[0] + gv[2]) : flat_rects[2];
            float v1h = gv[2] > 0 ? (float)gv[3] : 48.0f;

            v1x0 += jab1;
            v1x1 += jab1;
            constants.cursor1[0] = offset_x + v1x0 * scale;
            constants.cursor1[1] = offset_y + current_top1 * scale;
            constants.cursor1[2] = offset_x + v1x1 * scale;
            constants.cursor1[3] = offset_y + (current_top1 + v1h) * scale;
            constants.cursor1_padding[0] =
                (start_top1 - current_top1) * scale;
            constants.cursor1_padding[1] =
                powf(1.0f - travel_t1, 0.35f) *
                fminf(1.0f, fabsf(target_top1 - start_top1) / 46.0f);
        }
        if (flags & 2) {

            const float phase = (float)(now % 625ull);
            float jab = 0.0f;
            if (phase >= 390.0f && phase < 440.0f) {
                float t = (phase - 390.0f) / 50.0f;
                t = t * t * (3.0f - 2.0f * t);
                jab = 4.5f * t;
            } else if (phase >= 440.0f && phase < 515.0f) {
                float t = (phase - 440.0f) / 75.0f;
                t = t * t * (3.0f - 2.0f * t);
                jab = 4.5f * (1.0f - t);
            }

            static bool position_initialized = false;
            static float current_top = 0.0f;
            static float start_top = 0.0f;
            static float target_top = 0.0f;
            static ULONGLONG move_started = 0;
            const float wanted_top = gp[2] > 0 ? (float)gp[1] : flat_rects[5] + 6.0f;

            float travel_t = 1.0f;
            if (!position_initialized) {
                current_top = start_top = target_top = wanted_top;
                move_started = now;
                position_initialized = true;
            } else {
                travel_t = fminf(1.0f, (float)(now - move_started) / 150.0f);
                float eased = 1.0f - powf(1.0f - travel_t, 3.0f);
                current_top = start_top + (target_top - start_top) * eased;
                if (fabsf(wanted_top - target_top) > 0.5f) {
                    start_top = current_top;
                    target_top = wanted_top;
                    move_started = now;
                    travel_t = 0.0f;
                }
            }
            if (travel_t < 1.0f) {
                float eased = 1.0f - powf(1.0f - travel_t, 3.0f);
                current_top = start_top + (target_top - start_top) * eased;
            } else {
                current_top = target_top;
            }

            float v2x0 = flat_rects[4];
            float v2x1 = flat_rects[6];
            float v2h = gp[2] > 0 ? (float)gp[3] : 48.0f;
            v2x0 += jab;
            v2x1 += jab;
            constants.cursor2[0] = offset_x + v2x0 * scale;

            const float p2_vertical_alignment = 4.0f;
            constants.cursor2[1] =
                offset_y + (current_top + p2_vertical_alignment) * scale;
            constants.cursor2[2] = offset_x + v2x1 * scale;
            constants.cursor2[3] =
                offset_y +
                (current_top + p2_vertical_alignment + v2h) * scale;
            constants.cursor_padding[0] =
                (start_top - current_top) * scale;
            constants.cursor_padding[1] =
                powf(1.0f - travel_t, 0.35f) *
                fminf(1.0f, fabsf(target_top - start_top) / 46.0f);
        }
    }
    return true;
}

static bool is_game_swapchain(IDXGISwapChain* swapchain) {
    if (!swapchain) return false;
    DXGI_SWAP_CHAIN_DESC desc = {};
    if (FAILED(swapchain->GetDesc(&desc)) || !desc.OutputWindow) return false;
    DWORD window_pid = 0;
    GetWindowThreadProcessId(desc.OutputWindow, &window_pid);
    RECT client = {};
    if (!GetClientRect(desc.OutputWindow, &client)) return false;
    ID3D11Texture2D* backbuffer = nullptr;
    D3D11_TEXTURE2D_DESC buffer_desc = {};
    const HRESULT buffer_hr = swapchain->GetBuffer(
        0, __uuidof(ID3D11Texture2D), (void**)&backbuffer
    );
    if (SUCCEEDED(buffer_hr) && backbuffer) backbuffer->GetDesc(&buffer_desc);
    release_ptr(backbuffer);
    return window_pid == GetCurrentProcessId() &&
        GetAncestor(desc.OutputWindow, GA_ROOT) == desc.OutputWindow &&
        IsWindowVisible(desc.OutputWindow) &&
        client.right - client.left >= 640 &&
        client.bottom - client.top >= 360 &&
        SUCCEEDED(buffer_hr) &&
        buffer_desc.Width >= 640 && buffer_desc.Height >= 360;
}

static bool initialize_for_swapchain(IDXGISwapChain* swapchain) {
    if (g_game_swapchain) {
        if (swapchain != g_game_swapchain) return false;
        if (g_device && SUCCEEDED(g_device->GetDeviceRemovedReason())) {
            return true;
        }
        log_line("D3D11 device was removed; rebuilding renderer resources");
        release_partial_d3d_initialization();
    }
    if (!is_game_swapchain(swapchain)) return false;
    if (FAILED(swapchain->GetDevice(__uuidof(ID3D11Device), (void**)&g_device))) return false;
    g_device->GetImmediateContext(&g_context);
    if (!g_context || !create_static_resources()) {
        log_line("Failed to initialize D3D11 color-pass resources");
        release_partial_d3d_initialization();
        return false;
    }
    g_game_swapchain = swapchain;
    uintptr_t cave = find_coop_cave();
    if (cave) {
        uint32_t magic = kRingReadyMagic;
        WriteProcessMemory(
            GetCurrentProcess(), reinterpret_cast<void*>(cave + kRingReadyOffset),
            &magic, sizeof(magic), nullptr
        );

        uint32_t diagnostics[6] = {};
        WriteProcessMemory(
            GetCurrentProcess(),
            reinterpret_cast<void*>(cave + kRingDebugModeOffset),
            diagnostics, sizeof(diagnostics), nullptr
        );
    }
    log_line("D3D11 in-game color + world-space target rings initialized");
    return true;
}

static void append_ring_vertex(
        std::vector<RingVertex>& vertices, const float center[3],
        float radial, float angle, const float color[4], float opacity) {
    RingVertex vertex = {};

    vertex.position[0] = -(center[0] + cosf(angle) * radial);
    vertex.position[1] = -center[1];
    vertex.position[2] = center[2] + sinf(angle) * radial;
    vertex.color[0] = color[0];
    vertex.color[1] = color[1];
    vertex.color[2] = color[2];
    vertex.color[3] = color[3] * opacity;
    vertices.push_back(vertex);
}

static void read_ring_theme(int player, float color[3]) {
    const float defaults[3][3] = {
        {0.46f, 0.76f, 1.00f},
        {1.00f, 0.85f, 0.14f},
        {1.00f, 0.85f, 0.14f},
    };
    const int safe_player = player < 0 ? 0 : (player > 2 ? 2 : player);
    memcpy(color, defaults[safe_player], sizeof(float) * 3);
    const uintptr_t cave = find_coop_cave();
    if (!cave) return;
    int32_t rgb[3] = {};
    const uintptr_t offset = player == 0
        ? kP1ThemeColorOffset
        : (player == 1 ? kP2ThemeColorOffset : kP3ThemeColorOffset);
    if (!safe_read(cave + offset, rgb, sizeof(rgb))) return;
    if (rgb[0] < 0 || rgb[0] > 127 ||
        rgb[1] < 0 || rgb[1] > 127 ||
        rgb[2] < 0 || rgb[2] > 127 ||
        (rgb[0] | rgb[1] | rgb[2]) == 0) {
        return;
    }
    for (int channel = 0; channel < 3; ++channel) {
        color[channel] = (float)rgb[channel] / 127.0f;
    }
}

static UINT build_ring_vertices(
        int player, const float center[3], float radius, float opacity,
        std::vector<RingVertex>& vertices) {
    const float appear_scale = 0.82f + 0.18f * smoothstep01(opacity);
    radius *= appear_scale;
    const float radial[kRingRadialStops] = {
        radius * 1.11f,
        radius * 1.035f,
        radius * 0.965f,
        radius * 0.785f,
        radius * 0.695f,
    };

    float accent[3] = {};
    read_ring_theme(player, accent);
    const float highlight[3] = {
        fminf(1.0f, accent[0] * 0.56f + 0.44f),
        fminf(1.0f, accent[1] * 0.56f + 0.44f),
        fminf(1.0f, accent[2] * 0.56f + 0.44f),
    };
    const float colors[kRingRadialStops][4] = {
        {accent[0], accent[1], accent[2], 0.00f},
        {accent[0], accent[1], accent[2], 0.60f},
        {highlight[0], highlight[1], highlight[2], 0.98f},
        {accent[0], accent[1], accent[2], 0.92f},
        {accent[0] * 0.86f, accent[1] * 0.86f,
         accent[2] * 0.86f, 0.00f},
    };
    vertices.clear();
    vertices.reserve(kRingMaxVertices);
    for (UINT segment = 0; segment < kRingSegments; ++segment) {
        const float a0 = 6.2831853071795864769f *
            (float)segment / (float)kRingSegments;
        const float a1 = 6.2831853071795864769f *
            (float)(segment + 1) / (float)kRingSegments;
        for (UINT band = 0; band + 1 < kRingRadialStops; ++band) {
            append_ring_vertex(vertices, center, radial[band], a0,
                               colors[band], opacity);
            append_ring_vertex(vertices, center, radial[band], a1,
                               colors[band], opacity);
            append_ring_vertex(vertices, center, radial[band + 1], a1,
                               colors[band + 1], opacity);
            append_ring_vertex(vertices, center, radial[band], a0,
                               colors[band], opacity);
            append_ring_vertex(vertices, center, radial[band + 1], a1,
                               colors[band + 1], opacity);
            append_ring_vertex(vertices, center, radial[band + 1], a0,
                               colors[band + 1], opacity);
        }
    }
    return (UINT)vertices.size();
}

static void render_ring_pass(
        int player,
        ID3D11RenderTargetView* render_target,
        ID3D11DepthStencilView* depth_target,
        const D3D11_VIEWPORT& viewport) {
    float center[3] = {};
    float radius = 0.0f;
    float opacity = 0.0f;
    RingConstants constants = {};
    if (!render_target ||
        !update_ring_animation(player, center, radius, opacity) ||
        !read_ring_matrices(constants)) {
        return;
    }
    static bool depth_status_logged = false;
    if (!depth_status_logged) {
        log_line(
            depth_target
                ? "P2 target ring acquired FF12 scene depth buffer"
                : "P2 target ring has no bound scene depth buffer; fallback active"
        );
        depth_status_logged = true;
    }

    std::vector<RingVertex> vertices;
    const UINT vertex_count = build_ring_vertices(
        player, center, radius, opacity, vertices
    );
    if (!vertex_count || vertex_count > kRingMaxVertices) return;

    D3D11_MAPPED_SUBRESOURCE mapped = {};
    if (FAILED(g_context->Map(
            g_ring_constants, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) {
        return;
    }
    memcpy(mapped.pData, &constants, sizeof(constants));
    g_context->Unmap(g_ring_constants, 0);
    if (FAILED(g_context->Map(
            g_ring_vertices, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) {
        return;
    }
    memcpy(mapped.pData, vertices.data(), vertices.size() * sizeof(RingVertex));
    g_context->Unmap(g_ring_vertices, 0);

    ID3D11Buffer* old_vertex_buffer = nullptr;
    UINT old_stride = 0;
    UINT old_offset = 0;
    g_context->IAGetVertexBuffers(
        0, 1, &old_vertex_buffer, &old_stride, &old_offset
    );

    uint32_t debug_mode = 0;
    uintptr_t cave = find_coop_cave();
    if (cave) safe_read_value(cave + kRingDebugModeOffset, debug_mode);
    if (debug_mode > 2) debug_mode = 0;

    ID3D11DepthStencilView* bound_depth =
        debug_mode == 0 ? nullptr : depth_target;
    ID3D11DepthStencilState* depth_state = g_depth;
    if (bound_depth) {
        depth_state = debug_mode == 2 ? g_ring_depth_reverse : g_ring_depth;
    }

    const float blend_factor[4] = {};
    g_context->OMSetRenderTargets(1, &render_target, bound_depth);
    g_context->OMSetDepthStencilState(depth_state, 0);
    g_context->OMSetBlendState(
        g_ring_blend, blend_factor, 0xffffffffu
    );
    g_context->RSSetState(g_ring_rasterizer);
    g_context->RSSetViewports(1, &viewport);
    UINT stride = sizeof(RingVertex);
    UINT offset = 0;
    g_context->IASetVertexBuffers(0, 1, &g_ring_vertices, &stride, &offset);
    g_context->IASetInputLayout(g_ring_layout);
    g_context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    g_context->VSSetShader(g_ring_vs, nullptr, 0);
    g_context->VSSetConstantBuffers(0, 1, &g_ring_constants);
    g_context->PSSetShader(g_ring_ps, nullptr, 0);
    g_context->Draw(vertex_count, 0);
    if (cave) {
        volatile LONG* counter = reinterpret_cast<volatile LONG*>(
            cave + ring_draw_count_offset(player)
        );
        InterlockedIncrement(counter);
    }

    g_context->IASetVertexBuffers(
        0, 1, &old_vertex_buffer, &old_stride, &old_offset
    );
    release_ptr(old_vertex_buffer);
}

static void close_panel_mapping(bool clear_ready) {
    if (g_panel_mapping) {
        SharedPanelHeader* header =
            reinterpret_cast<SharedPanelHeader*>(g_panel_mapping);
        if (clear_ready && header->magic == kPanelMagic) {
            InterlockedExchange(&header->renderer_ready, 0);
            InterlockedExchange64(&header->renderer_heartbeat, 0);
        }
        UnmapViewOfFile(g_panel_mapping);
        g_panel_mapping = nullptr;
    }
    if (g_panel_mapping_handle) {
        CloseHandle(g_panel_mapping_handle);
        g_panel_mapping_handle = nullptr;
    }
}

static bool valid_panel_header(const SharedPanelHeader* header) {
    return header &&
        header->magic == kPanelMagic &&
        header->version == kPanelVersion &&
        header->header_size == kPanelHeaderSize &&
        header->panel_count == kPanelCount &&
        header->slot_size == kPanelSlotSize &&
        header->pixel_capacity == kPanelPixelCapacity;
}

static bool panel_view_is_high(const void* view) {
    return view &&
        static_cast<ULONGLONG>(reinterpret_cast<ULONG_PTR>(view)) >=
            kPanelMinimumViewAddress;
}

static uint8_t* map_panel_view_high(HANDLE mapping) {
#if defined(_WIN64)

    static constexpr uintptr_t candidates[] = {
        0x0000000100000000ull,
        0x0000000200000000ull,
        0x0000000300000000ull,
        0x0000000400000000ull,
        0x0000000600000000ull,
        0x0000000800000000ull,
        0x0000000C00000000ull,
        0x0000001000000000ull,
        0x0000001800000000ull,
        0x0000002000000000ull,
        0x0000003000000000ull,
        0x0000004000000000ull,
    };
    for (const uintptr_t candidate : candidates) {
        uint8_t* view = reinterpret_cast<uint8_t*>(MapViewOfFileEx(
            mapping, FILE_MAP_READ | FILE_MAP_WRITE, 0, 0, 0,
            reinterpret_cast<void*>(candidate)
        ));
        if (!view) continue;
        if (panel_view_is_high(view)) return view;

        UnmapViewOfFile(view);
    }
#else
    (void)mapping;
#endif
    return nullptr;
}

static bool ensure_panel_mapping() {
    if (g_panel_mapping) {
        if (panel_view_is_high(g_panel_mapping) && valid_panel_header(
                reinterpret_cast<SharedPanelHeader*>(g_panel_mapping))) {
            return true;
        }
        close_panel_mapping(false);
    }
    const ULONGLONG now = GetTickCount64();
    if (now < g_panel_mapping_retry_at) return false;
    g_panel_mapping_retry_at = now + 250;

    wchar_t name[96] = {};
    swprintf_s(
        name, L"Local\\FF12CoopPanels_%lu",
        static_cast<unsigned long>(GetCurrentProcessId())
    );
    HANDLE mapping = OpenFileMappingW(
        FILE_MAP_READ | FILE_MAP_WRITE, FALSE, name
    );
    if (!mapping) return false;
    uint8_t* view = map_panel_view_high(mapping);
    if (!panel_view_is_high(view) || !valid_panel_header(
            reinterpret_cast<SharedPanelHeader*>(view))) {
        if (view) UnmapViewOfFile(view);
        CloseHandle(mapping);
        return false;
    }
    g_panel_mapping_handle = mapping;
    g_panel_mapping = view;
    log_line("D3D11 shared panel transport attached");
    return true;
}

static bool panel_publisher_alive() {
    if (!ensure_panel_mapping()) return false;
    SharedPanelHeader* header =
        reinterpret_cast<SharedPanelHeader*>(g_panel_mapping);
    const ULONGLONG now = GetTickCount64();
    InterlockedExchange(&header->renderer_ready, 1);
    InterlockedExchange64(
        &header->renderer_heartbeat, static_cast<LONG64>(now)
    );
    InterlockedIncrement64(&header->renderer_frame);
    const LONG64 publisher_tick = InterlockedCompareExchange64(
        &header->publisher_heartbeat, 0, 0
    );
    return InterlockedCompareExchange(
               &header->publisher_ready, 0, 0
           ) == 1 &&
        publisher_tick > 0 &&
        now >= static_cast<ULONGLONG>(publisher_tick) &&
        now - static_cast<ULONGLONG>(publisher_tick) <=
            kPanelPublisherStaleMs;
}

static bool recreate_panel_texture(
        PanelGpuState& state, uint32_t width, uint32_t height) {
    release_ptr(state.srv);
    release_ptr(state.texture);
    state.width = 0;
    state.height = 0;

    D3D11_TEXTURE2D_DESC texture = {};
    texture.Width = width;
    texture.Height = height;
    texture.MipLevels = 1;
    texture.ArraySize = 1;
    texture.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    texture.SampleDesc.Count = 1;
    texture.Usage = D3D11_USAGE_DEFAULT;
    texture.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    if (FAILED(g_device->CreateTexture2D(
            &texture, nullptr, &state.texture))) {
        return false;
    }
    if (FAILED(g_device->CreateShaderResourceView(
            state.texture, nullptr, &state.srv))) {
        release_ptr(state.texture);
        return false;
    }
    state.width = width;
    state.height = height;
    return true;
}

static bool update_panel_generation(uint32_t index) {
    if (!g_panel_mapping || index >= kPanelCount) return false;
    SharedPanelHeader* header =
        reinterpret_cast<SharedPanelHeader*>(g_panel_mapping);
    SharedPanelSlot* slot = reinterpret_cast<SharedPanelSlot*>(
        g_panel_mapping + kPanelHeaderSize + index * kPanelSlotSize
    );
    PanelGpuState& state = g_panel_states[index];
    const LONG sequence_before = InterlockedCompareExchange(
        &slot->sequence, 0, 0
    );
    if (!sequence_before || (sequence_before & 1)) return false;

    SharedPanelSlot snapshot = {};
    memcpy(&snapshot, slot, sizeof(snapshot));
    MemoryBarrier();
    const LONG sequence_after = InterlockedCompareExchange(
        &slot->sequence, 0, 0
    );
    if (sequence_before != sequence_after || (sequence_after & 1)) {
        return false;
    }
    if (snapshot.generation == state.generation) return true;

    const bool visible = snapshot.visible == 1;
    if (!visible) {
        state.visible = false;
        state.generation = snapshot.generation;
        memcpy(&state.metadata, &snapshot, sizeof(snapshot));
        InterlockedExchange(
            &header->acknowledged_generation[index],
            static_cast<LONG>(snapshot.generation)
        );
        return true;
    }
    const uint64_t expected =
        static_cast<uint64_t>(snapshot.width) * snapshot.height * 4ull;
    if (!snapshot.width || !snapshot.height ||
        snapshot.width > kPanelMaxWidth ||
        snapshot.height > kPanelMaxHeight ||
        snapshot.stride != snapshot.width * 4u ||
        expected != snapshot.pixel_bytes ||
        expected > kPanelPixelCapacity ||
        snapshot.z_order != index ||
        !isfinite(snapshot.logical_x) ||
        !isfinite(snapshot.logical_y)) {
        state.visible = false;
        return false;
    }

    state.scratch.resize(static_cast<size_t>(snapshot.pixel_bytes));
    const uint8_t* pixels = reinterpret_cast<const uint8_t*>(slot) +
        kPanelSlotHeaderSize;
    memcpy(state.scratch.data(), pixels, state.scratch.size());
    MemoryBarrier();
    if (InterlockedCompareExchange(&slot->sequence, 0, 0) !=
        sequence_before) {
        return false;
    }

    if ((!state.texture || !state.srv ||
         state.width != snapshot.width ||
         state.height != snapshot.height) &&
        !recreate_panel_texture(
            state, snapshot.width, snapshot.height)) {
        state.visible = false;
        return false;
    }
    g_context->UpdateSubresource(
        state.texture, 0, nullptr, state.scratch.data(),
        snapshot.stride, snapshot.pixel_bytes
    );
    memcpy(&state.metadata, &snapshot, sizeof(snapshot));
    state.generation = snapshot.generation;
    state.visible = true;
    InterlockedExchange(
        &header->acknowledged_generation[index],
        static_cast<LONG>(snapshot.generation)
    );
    return true;
}

static void render_shared_panels(
        ID3D11RenderTargetView* render_target,
        const D3D11_VIEWPORT& viewport,
        bool publisher_alive) {
    if (!render_target || !publisher_alive) return;
    for (uint32_t index = 0; index < kPanelCount; ++index) {
        update_panel_generation(index);
    }

    const float blend_factor[4] = {};
    g_context->OMSetRenderTargets(1, &render_target, nullptr);
    g_context->OMSetDepthStencilState(g_depth, 0);
    g_context->OMSetBlendState(
        g_panel_blend, blend_factor, 0xffffffffu
    );
    g_context->RSSetState(g_rasterizer);
    g_context->RSSetViewports(1, &viewport);
    g_context->IASetInputLayout(nullptr);
    g_context->IASetPrimitiveTopology(
        D3D11_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP
    );
    g_context->VSSetShader(g_panel_vs, nullptr, 0);
    g_context->VSSetConstantBuffers(0, 1, &g_panel_constants);
    g_context->PSSetShader(g_panel_ps, nullptr, 0);
    g_context->PSSetSamplers(0, 1, &g_panel_sampler);

    for (uint32_t index = 0; index < kPanelCount; ++index) {
        PanelGpuState& state = g_panel_states[index];
        if (!state.visible || !state.srv) continue;
        const SharedPanelSlot& panel = state.metadata;
        const float left =
            panel.logical_x / kPanelReferenceWidth * 2.0f - 1.0f;
        const float right =
            (panel.logical_x + static_cast<float>(panel.width)) /
                kPanelReferenceWidth * 2.0f - 1.0f;
        const float top =
            1.0f - panel.logical_y / kPanelReferenceHeight * 2.0f;
        const float bottom =
            1.0f -
            (panel.logical_y + static_cast<float>(panel.height)) /
                kPanelReferenceHeight * 2.0f;
        PanelConstants constants = {{left, top, right, bottom}};
        D3D11_MAPPED_SUBRESOURCE mapped = {};
        if (FAILED(g_context->Map(
                g_panel_constants, 0, D3D11_MAP_WRITE_DISCARD,
                0, &mapped))) {
            continue;
        }
        memcpy(mapped.pData, &constants, sizeof(constants));
        g_context->Unmap(g_panel_constants, 0);
        g_context->PSSetShaderResources(0, 1, &state.srv);
        g_context->Draw(4, 0);
    }
    ID3D11ShaderResourceView* null_srv = nullptr;
    g_context->PSSetShaderResources(0, 1, &null_srv);
}

static D3D11_VIEWPORT panel_reference_viewport(UINT width, UINT height) {
    D3D11_VIEWPORT viewport = {};
    viewport.Width = static_cast<float>(width);
    viewport.Height = static_cast<float>(height);
    viewport.MinDepth = 0.0f;
    viewport.MaxDepth = 1.0f;
    if (!width || !height) return viewport;

    const float reference_aspect =
        kPanelReferenceWidth / kPanelReferenceHeight;
    const float output_aspect =
        static_cast<float>(width) / static_cast<float>(height);
    if (output_aspect > reference_aspect) {
        viewport.Width = viewport.Height * reference_aspect;
        viewport.TopLeftX =
            (static_cast<float>(width) - viewport.Width) * 0.5f;
    } else if (output_aspect < reference_aspect) {
        viewport.Height = viewport.Width / reference_aspect;
        viewport.TopLeftY =
            (static_cast<float>(height) - viewport.Height) * 0.5f;
    }
    return viewport;
}

static void render_color_pass(IDXGISwapChain* swapchain) {
    if (!initialize_for_swapchain(swapchain)) return;
    uintptr_t debug_cave = find_coop_cave();

    const bool panels_alive = panel_publisher_alive();
    if (presentation_overlays_suppressed(debug_cave)) return;

    ID3D11Texture2D* backbuffer = nullptr;
    if (FAILED(swapchain->GetBuffer(0, __uuidof(ID3D11Texture2D), (void**)&backbuffer))) return;
    if (!create_frame_resources(backbuffer)) {
        backbuffer->Release();
        return;
    }

    ConstantData constants = {};
    if (!p2_menu_rect(constants)) {
        backbuffer->Release();
        return;
    }

    ID3D11RenderTargetView* new_rtv = nullptr;
    if (FAILED(g_device->CreateRenderTargetView(backbuffer, nullptr, &new_rtv))) {
        backbuffer->Release();
        return;
    }
    g_context->CopyResource(g_copy, backbuffer);

    D3D11_MAPPED_SUBRESOURCE mapped = {};
    if (FAILED(g_context->Map(g_constants, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) {
        new_rtv->Release();
        backbuffer->Release();
        return;
    }
    memcpy(mapped.pData, &constants, sizeof(constants));
    g_context->Unmap(g_constants, 0);

    ID3D11RenderTargetView* old_rtv = nullptr;
    ID3D11DepthStencilView* old_dsv = nullptr;
    ID3D11VertexShader* old_vs = nullptr;
    ID3D11PixelShader* old_ps = nullptr;
    ID3D11InputLayout* old_layout = nullptr;
    ID3D11ShaderResourceView* old_srvs[2] = {};
    ID3D11SamplerState* old_sampler = nullptr;
    ID3D11Buffer* old_vs_cb = nullptr;
    ID3D11Buffer* old_ps_cb = nullptr;
    ID3D11RasterizerState* old_rs = nullptr;
    ID3D11DepthStencilState* old_depth = nullptr;
    ID3D11BlendState* old_blend = nullptr;
    UINT old_stencil_ref = 0;
    UINT old_sample_mask = 0;
    float old_blend_factor[4] = {};
    D3D11_PRIMITIVE_TOPOLOGY old_topology;
    UINT viewport_count = D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE;
    D3D11_VIEWPORT old_viewports[D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE] = {};

    g_context->OMGetRenderTargets(1, &old_rtv, &old_dsv);
    if (debug_cave) {
        InterlockedExchange(reinterpret_cast<volatile LONG*>(
            debug_cave + kRingDepthSeenOffset), old_dsv ? 1 : 0);
    }
    g_context->VSGetShader(&old_vs, nullptr, nullptr);
    g_context->PSGetShader(&old_ps, nullptr, nullptr);
    g_context->IAGetInputLayout(&old_layout);
    g_context->IAGetPrimitiveTopology(&old_topology);
    g_context->PSGetShaderResources(0, 2, old_srvs);
    g_context->PSGetSamplers(0, 1, &old_sampler);
    g_context->VSGetConstantBuffers(0, 1, &old_vs_cb);
    g_context->PSGetConstantBuffers(0, 1, &old_ps_cb);
    g_context->RSGetState(&old_rs);
    g_context->RSGetViewports(&viewport_count, old_viewports);
    g_context->OMGetDepthStencilState(&old_depth, &old_stencil_ref);
    g_context->OMGetBlendState(&old_blend, old_blend_factor, &old_sample_mask);

    D3D11_VIEWPORT viewport = {};
    viewport.Width = (float)g_width;
    viewport.Height = (float)g_height;
    viewport.MinDepth = 0.0f;
    viewport.MaxDepth = 1.0f;
    const D3D11_VIEWPORT panel_viewport =
        panel_reference_viewport(g_width, g_height);
    float blend_factor[4] = {};
    g_context->OMSetRenderTargets(1, &new_rtv, nullptr);
    g_context->OMSetDepthStencilState(g_depth, 0);
    g_context->OMSetBlendState(g_blend, blend_factor, 0xffffffffu);
    g_context->RSSetState(g_rasterizer);
    g_context->RSSetViewports(1, &viewport);
    g_context->IASetInputLayout(nullptr);
    g_context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    g_context->VSSetShader(g_vs, nullptr, 0);
    g_context->PSSetShader(g_ps, nullptr, 0);
    g_context->VSSetConstantBuffers(0, 1, &g_constants);
    g_context->PSSetConstantBuffers(0, 1, &g_constants);
    ID3D11ShaderResourceView* cursor_srvs[2] = {g_copy_srv, g_hand_atlas_srv};
    g_context->PSSetShaderResources(0, 2, cursor_srvs);
    g_context->PSSetSamplers(0, 1, &g_sampler);
    g_context->Draw(3, 0);

    ID3D11ShaderResourceView* null_srvs[2] = {};
    g_context->PSSetShaderResources(0, 2, null_srvs);
    render_ring_pass(0, new_rtv, old_dsv, viewport);
    render_ring_pass(1, new_rtv, old_dsv, viewport);
    render_ring_pass(2, new_rtv, old_dsv, viewport);
    render_shared_panels(new_rtv, panel_viewport, panels_alive);
    if (debug_cave) {
        uint32_t capture_request = 0;
        safe_read_value(
            debug_cave + kRingCaptureRequestOffset, capture_request
        );
        if (capture_request) {
            const bool captured = capture_backbuffer_bmp(backbuffer);
            InterlockedExchange(reinterpret_cast<volatile LONG*>(
                debug_cave + kRingCaptureRequestOffset), 0);
            log_line(captured
                ? "Captured final ring backbuffer to ring_capture.bmp"
                : "Failed to capture final ring backbuffer");
        }
    }
    g_context->OMSetRenderTargets(1, &old_rtv, old_dsv);
    g_context->OMSetDepthStencilState(old_depth, old_stencil_ref);
    g_context->OMSetBlendState(old_blend, old_blend_factor, old_sample_mask);
    g_context->RSSetState(old_rs);
    if (viewport_count) g_context->RSSetViewports(viewport_count, old_viewports);
    g_context->IASetInputLayout(old_layout);
    g_context->IASetPrimitiveTopology(old_topology);
    g_context->VSSetShader(old_vs, nullptr, 0);
    g_context->PSSetShader(old_ps, nullptr, 0);
    g_context->VSSetConstantBuffers(0, 1, &old_vs_cb);
    g_context->PSSetConstantBuffers(0, 1, &old_ps_cb);
    g_context->PSSetShaderResources(0, 2, old_srvs);
    g_context->PSSetSamplers(0, 1, &old_sampler);

    release_ptr(old_rtv);
    release_ptr(old_dsv);
    release_ptr(old_vs);
    release_ptr(old_ps);
    release_ptr(old_layout);
    release_ptr(old_srvs[0]);
    release_ptr(old_srvs[1]);
    release_ptr(old_sampler);
    release_ptr(old_vs_cb);
    release_ptr(old_ps_cb);
    release_ptr(old_rs);
    release_ptr(old_depth);
    release_ptr(old_blend);
    new_rtv->Release();
    backbuffer->Release();
}

static void render_color_pass_cxx_guard(IDXGISwapChain* swapchain) noexcept {
    try {
        render_color_pass(swapchain);
    } catch (...) {
        // Allocation and COM wrapper failures use C++ exceptions, which are
        // distinct from the access-violation boundary below under /EHsc.
        InterlockedExchange(&g_render_faulted, 1);
    }
}

static HRESULT STDMETHODCALLTYPE hooked_present(
        IDXGISwapChain* swapchain, UINT sync, UINT flags) {
    const bool camera_swapchain = g_game_swapchain
        ? swapchain == g_game_swapchain
        : is_game_swapchain(swapchain);
    const bool camera_owner = camera_swapchain &&
        InterlockedCompareExchange(&g_camera_publish_busy, 1, 0) == 0;
    if (camera_owner) {
#ifdef _MSC_VER
        __try {
            publish_camera_frame_for_present(find_coop_cave());
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            // The camera bridge is independent from the overlay fault latch.
            // A bad cave loses one publication and retries at the next Present.
            g_camera_bridge.active = false;
            g_camera_bridge.have_cached_goal = false;
        }
#else
        try {
            publish_camera_frame_for_present(find_coop_cave());
        } catch (...) {
            g_camera_bridge.active = false;
            g_camera_bridge.have_cached_goal = false;
        }
#endif
        InterlockedExchange(&g_camera_publish_busy, 0);
    }
    if (!InterlockedCompareExchange(&g_render_faulted, 0, 0)) {
#ifdef _MSC_VER
        __try {
            render_color_pass_cxx_guard(swapchain);
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            InterlockedExchange(&g_render_faulted, 1);
        }
#else
        render_color_pass_cxx_guard(swapchain);
#endif
    }
    return g_original_present(swapchain, sync, flags);
}

static LRESULT CALLBACK dummy_window_proc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    return DefWindowProcW(window, message, wparam, lparam);
}

enum class HookInstallResult {
    installed,
    class_registration_failed,
    window_creation_failed,
    device_creation_failed,
    hook_publication_failed,
};

static DWORD next_hook_retry_delay(DWORD current) {
    if (current >= kHookRetryMaximumMilliseconds) {
        return kHookRetryMaximumMilliseconds;
    }
    const DWORD doubled = current * 2;
    return doubled < current || doubled > kHookRetryMaximumMilliseconds
        ? kHookRetryMaximumMilliseconds
        : doubled;
}

static const char* hook_install_failure(HookInstallResult result) {
    switch (result) {
        case HookInstallResult::class_registration_failed:
            return "Failed to register dummy D3D11 window class; retrying";
        case HookInstallResult::window_creation_failed:
            return "Failed to create dummy D3D11 window; retrying";
        case HookInstallResult::device_creation_failed:
            return "Failed to create dummy D3D11 swapchain; retrying";
        case HookInstallResult::hook_publication_failed:
            return "Failed to chain Present vtable hook atomically; retrying";
        case HookInstallResult::installed:
            return nullptr;
    }
    return "Unknown renderer hook failure; retrying";
}

static HookInstallResult try_install_hook(DWORD attempt_id) {
    HookInstallResult result = HookInstallResult::class_registration_failed;
    wchar_t class_name[96] = {};
    HINSTANCE instance = g_module;
    ATOM registered_class = 0;
    HWND window = nullptr;
    IDXGISwapChain* swapchain = nullptr;
    ID3D11Device* device = nullptr;
    ID3D11DeviceContext* context = nullptr;

    WNDCLASSW wc = {};
    wc.lpfnWndProc = dummy_window_proc;
    wc.hInstance = instance;
    if (swprintf(
            class_name,
            ARRAYSIZE(class_name),
            L"FF12P2ColorDummy_%08lx_%08lx",
            GetCurrentProcessId(),
            attempt_id
        ) >= 0) {
        wc.lpszClassName = class_name;
        registered_class = RegisterClassW(&wc);
    }
    if (registered_class) {
        result = HookInstallResult::window_creation_failed;
        window = CreateWindowExW(
            0, wc.lpszClassName, L"", WS_OVERLAPPEDWINDOW,
            0, 0, 16, 16, nullptr, nullptr, wc.hInstance, nullptr
        );
    }

    if (window) {
        result = HookInstallResult::device_creation_failed;
        DXGI_SWAP_CHAIN_DESC desc = {};
        desc.BufferCount = 1;
        desc.BufferDesc.Width = 16;
        desc.BufferDesc.Height = 16;
        desc.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
        desc.OutputWindow = window;
        desc.SampleDesc.Count = 1;
        desc.Windowed = TRUE;
        desc.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;
        D3D_FEATURE_LEVEL level = D3D_FEATURE_LEVEL_11_0;
        const HRESULT hr = D3D11CreateDeviceAndSwapChain(
            nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, nullptr, 0,
            D3D11_SDK_VERSION, &desc, &swapchain, &device, &level, &context
        );
        if (FAILED(hr) || !swapchain) {
            release_ptr(context);
            release_ptr(device);
            release_ptr(swapchain);
        }
    }

    if (swapchain) {
        result = HookInstallResult::hook_publication_failed;
        void** vtable = *reinterpret_cast<void***>(swapchain);
        void** present_slot = &vtable[8];
        bool installed = false;
        for (unsigned int attempt = 0; attempt < 64; ++attempt) {
            void* expected = *reinterpret_cast<void* volatile*>(present_slot);
            if (!expected) break;
            if (expected == reinterpret_cast<void*>(hooked_present)) {
                installed = g_original_present != nullptr;
                break;
            }
            g_original_present = reinterpret_cast<
                HRESULT (STDMETHODCALLTYPE *)(IDXGISwapChain*, UINT, UINT)
            >(expected);
            void* observed = nullptr;
            if (compare_exchange_slot(
                    present_slot,
                    expected,
                    reinterpret_cast<void*>(hooked_present),
                    &observed
                )) {
                installed = true;
                break;
            }
        }
        if (installed) {
            MemoryBarrier();
            g_present_slot = present_slot;
            InterlockedExchange(&g_hook_ready, 1);
            result = HookInstallResult::installed;
        } else {
            g_original_present = nullptr;
        }
    }

    // D3D may return partial COM outputs on failure.  One common tail owns
    // every attempt resource, including the success path, before retrying.
    release_ptr(context);
    release_ptr(device);
    release_ptr(swapchain);
    if (window) DestroyWindow(window);
    if (registered_class) UnregisterClassW(class_name, instance);
    return result;
}

static DWORD WINAPI install_hook(void*) {
    // Once Present points into this image the module must never be unloaded.
    // Pinning from the worker (outside the loader lock) makes process lifetime
    // explicit and also makes an in-flight Present safe during explicit unhook.
    DWORD retry_delay = kHookRetryInitialMilliseconds;
    bool pin_failure_logged = false;
    while (!pin_renderer_module()) {
        if (!pin_failure_logged) {
            log_line("Failed to pin renderer module; retrying");
            pin_failure_logged = true;
        }
        Sleep(retry_delay);
        retry_delay = next_hook_retry_delay(retry_delay);
    }
    retry_delay = kHookRetryInitialMilliseconds;

    HookInstallResult last_failure = HookInstallResult::installed;
    DWORD attempt_id = 0;
    for (;;) {
        const HookInstallResult result = try_install_hook(++attempt_id);
        if (result == HookInstallResult::installed) {
            log_line("Present vtable hook installed");
            return 0;
        }
        if (result != last_failure) {
            log_line(hook_install_failure(result));
            last_failure = result;
        }
        Sleep(retry_delay);
        retry_delay = next_hook_retry_delay(retry_delay);
    }
}

static void restore_every_hook() {
    if (g_present_slot && g_original_present) {
        void* observed = nullptr;
        const bool restored = compare_exchange_slot(
            g_present_slot,
            reinterpret_cast<void*>(hooked_present),
            reinterpret_cast<void*>(g_original_present),
            &observed
        );
        if (!restored && observed != reinterpret_cast<void*>(g_original_present)) {
            log_line("Present unhook skipped because another hook owns the slot");
            return;
        }
        g_present_slot = nullptr;
        // Do not clear the chained target. A Present invocation can already
        // be executing after the slot was restored, and must still be able to
        // tail-call the renderer that preceded us. The DLL is pinned above.
    }
    InterlockedExchange(&g_hook_ready, 0);
}

extern "C" __declspec(dllexport) void FF12CoopUnhook() {
    restore_every_hook();
}

BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID reserved) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_module = module;
        DisableThreadLibraryCalls(module);
        HANDLE thread = CreateThread(nullptr, 0, install_hook, nullptr, 0, nullptr);
        if (!thread) return FALSE;
        CloseHandle(thread);
    } else if (reason == DLL_PROCESS_DETACH) {

        (void)reserved;
    }
    return TRUE;
}
