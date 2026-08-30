#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <cstdint>
#include <cstring>

namespace {

constexpr DWORD kPathCapacity = 32768;
constexpr wchar_t kGameExecutable[] = L"FFXII_TZA.exe";
constexpr wchar_t kRuntimeDirectory[] = L"IvaliceTogetherRuntime";
constexpr wchar_t kRuntimeExecutable[] = L"IvaliceTogetherRuntime.exe";
constexpr wchar_t kAutostartModule[] =
    L"ivalice-together-autostart.dll";
constexpr wchar_t kRuntimeArguments[] =
    L" --ff12-coop-runtime autostart ";
constexpr wchar_t kRuntimeMutexPrefix[] =
    L"Local\\FF12LocalCoopHeadlessLauncher_";
constexpr wchar_t kGameRootEnvironment[] = L"FF12_COOP_GAME_ROOT";
constexpr wchar_t kDataDirectoryEnvironment[] = L"FF12_COOP_DATA_DIR";
constexpr wchar_t kPyInstallerResetKey[] =
    L"PYINSTALLER_RESET_ENVIRONMENT";
constexpr wchar_t kPyInstallerResetEntry[] =
    L"PYINSTALLER_RESET_ENVIRONMENT=1";
constexpr SIZE_T kRel32CaveSize = 0x10000;
constexpr uintptr_t kAllocationGranularity = 0x10000;
constexpr wchar_t kCaveBaseEnvironment[] = L"FF12_COOP_CAVE_BASE";
constexpr wchar_t kDualCaveBaseEnvironment[] =
    L"FF12_COOP_DUAL_CAVE_BASE";
constexpr wchar_t kDamageCaveBaseEnvironment[] =
    L"FF12_COOP_DAMAGE_CAVE_BASE";
constexpr char kCaveMagic[] = "FF12COOP";
constexpr char kDualCaveMagic[] = "FF12INT2";
constexpr char kDamageCaveMagic[] = "FF12DMG1";
constexpr DWORD kRuntimePollMilliseconds = 1000;
constexpr DWORD kRestartInitialMilliseconds = 250;
constexpr DWORD kRestartMaximumMilliseconds = 5000;
constexpr ULONGLONG kStableRuntimeMilliseconds = 10000;

struct Rel32Range {
    uintptr_t start;
    uintptr_t stop;
};

constexpr Rel32Range kRel32Ranges[] = {
    {0x10000000, 0x70000000},
    {0x03000000, 0x10000000},
    {0x70000000, 0x7F000000},
    {0x00010000, 0x03000000},
    {0x7F000000, 0x80000000},
};

HMODULE g_module = nullptr;
HANDLE g_process_guard = nullptr;

bool equals_ignore_case(const wchar_t* left, const wchar_t* right) {
    return left && right &&
           CompareStringOrdinal(left, -1, right, -1, TRUE) == CSTR_EQUAL;
}

wchar_t* filename_part(wchar_t* path) {
    if (!path) {
        return nullptr;
    }
    wchar_t* result = path;
    for (wchar_t* cursor = path; *cursor; ++cursor) {
        if (*cursor == L'\\' || *cursor == L'/') {
            result = cursor + 1;
        }
    }
    return result;
}

bool remove_filename(wchar_t* path) {
    wchar_t* filename = filename_part(path);
    if (!filename || filename == path) {
        return false;
    }
    filename[-1] = L'\0';
    return true;
}

bool append_text(
    wchar_t* destination,
    DWORD capacity,
    const wchar_t* text
) {
    if (!destination || !text || capacity == 0) {
        return false;
    }
    DWORD length = lstrlenW(destination);
    DWORD addition = lstrlenW(text);
    if (length >= capacity || addition >= capacity - length) {
        return false;
    }
    CopyMemory(
        destination + length,
        text,
        static_cast<SIZE_T>(addition + 1) * sizeof(wchar_t)
    );
    return true;
}

bool append_decimal(wchar_t* destination, DWORD capacity, DWORD value) {
    wchar_t reversed[16] = {};
    DWORD count = 0;
    do {
        reversed[count++] = static_cast<wchar_t>(L'0' + value % 10);
        value /= 10;
    } while (value && count < ARRAYSIZE(reversed));

    DWORD length = lstrlenW(destination);
    if (length >= capacity || count >= capacity - length) {
        return false;
    }
    for (DWORD index = 0; index < count; ++index) {
        destination[length + index] = reversed[count - index - 1];
    }
    destination[length + count] = L'\0';
    return true;
}

bool runtime_mutex_active(DWORD game_pid) {
    wchar_t mutex_name[96] = {};
    if (!append_text(
            mutex_name,
            ARRAYSIZE(mutex_name),
            kRuntimeMutexPrefix
        ) ||
        !append_decimal(
            mutex_name,
            ARRAYSIZE(mutex_name),
            game_pid
        )) {
        // An identity that cannot be represented is uncertainty, not proof
        // that it is safe to launch a second supervisor.
        return true;
    }

    SetLastError(ERROR_SUCCESS);
    HANDLE mutex = OpenMutexW(SYNCHRONIZE, FALSE, mutex_name);
    if (mutex) {
        CloseHandle(mutex);
        return true;
    }
    // Only exact absence authorizes a launch.  Access-denied and transient
    // object-manager failures must remain fail-closed, matching launcher_core.
    return GetLastError() != ERROR_FILE_NOT_FOUND;
}

bool pin_autostart_module() {
    HMODULE pinned = nullptr;
    return g_module && GetModuleHandleExW(
        GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
            GET_MODULE_HANDLE_EX_FLAG_PIN,
        reinterpret_cast<LPCWSTR>(g_module),
        &pinned
    );
}

DWORD next_restart_delay(DWORD current) {
    if (current >= kRestartMaximumMilliseconds) {
        return kRestartMaximumMilliseconds;
    }
    const DWORD doubled = current * 2;
    return doubled < current || doubled > kRestartMaximumMilliseconds
        ? kRestartMaximumMilliseconds
        : doubled;
}

bool copy_text(wchar_t* destination, DWORD capacity, const wchar_t* source) {
    if (!destination || capacity == 0) {
        return false;
    }
    destination[0] = L'\0';
    return append_text(destination, capacity, source);
}

bool join_path(
    wchar_t* destination,
    DWORD capacity,
    const wchar_t* directory,
    const wchar_t* leaf
) {
    return copy_text(destination, capacity, directory) &&
           append_text(destination, capacity, L"\\") &&
           append_text(destination, capacity, leaf);
}

bool is_regular_file(const wchar_t* path) {
    const DWORD attributes = GetFileAttributesW(path);
    return attributes != INVALID_FILE_ATTRIBUTES &&
           !(attributes & FILE_ATTRIBUTE_DIRECTORY);
}

bool resolve_runtime_in_root(
    wchar_t* runtime_path,
    wchar_t* data_directory,
    const wchar_t* package_root
) {
    return join_path(
               runtime_path,
               kPathCapacity,
               package_root,
               kRuntimeDirectory
           ) &&
           append_text(runtime_path, kPathCapacity, L"\\") &&
           append_text(runtime_path, kPathCapacity, kRuntimeExecutable) &&
           is_regular_file(runtime_path) &&
           join_path(
               data_directory,
               kPathCapacity,
               package_root,
               L"coop"
           );
}

bool resolve_package_paths(
    wchar_t* runtime_path,
    wchar_t* data_directory,
    const wchar_t* module_x64_root,
    const wchar_t* game_root
) {
    if (resolve_runtime_in_root(
            runtime_path,
            data_directory,
            module_x64_root
        )) {
        return true;
    }

    wchar_t* virtual_x64_root = static_cast<wchar_t*>(HeapAlloc(
        GetProcessHeap(), HEAP_ZERO_MEMORY, kPathCapacity * sizeof(wchar_t)
    ));
    if (!virtual_x64_root) {
        return false;
    }
    const bool resolved =
        join_path(virtual_x64_root, kPathCapacity, game_root, L"x64") &&
        resolve_runtime_in_root(
            runtime_path,
            data_directory,
            virtual_x64_root
        );
    HeapFree(GetProcessHeap(), 0, virtual_x64_root);
    return resolved;
}

bool environment_entry_has_key(
    const wchar_t* entry,
    const wchar_t* key
) {
    if (!entry || !key) {
        return false;
    }
    const int key_length = lstrlenW(key);
    if (lstrlenW(entry) <= key_length) {
        return false;
    }
    return entry[key_length] == L'=' &&
           CompareStringOrdinal(
               entry,
               key_length,
               key,
               key_length,
               TRUE
           ) == CSTR_EQUAL;
}

bool append_environment_entry(
    wchar_t*& destination,
    const wchar_t* entry
) {
    if (!destination || !entry) {
        return false;
    }
    const SIZE_T length = static_cast<SIZE_T>(lstrlenW(entry)) + 1;
    CopyMemory(destination, entry, length * sizeof(wchar_t));
    destination += length;
    return true;
}

wchar_t* build_runtime_environment() {
    wchar_t* inherited = GetEnvironmentStringsW();
    if (!inherited) {
        return nullptr;
    }

    SIZE_T required_characters = 1;
    for (const wchar_t* entry = inherited; *entry;
         entry += lstrlenW(entry) + 1) {
        if (!environment_entry_has_key(entry, kPyInstallerResetKey)) {
            required_characters += static_cast<SIZE_T>(lstrlenW(entry)) + 1;
        }
    }
    required_characters +=
        static_cast<SIZE_T>(lstrlenW(kPyInstallerResetEntry)) + 1;

    wchar_t* block = static_cast<wchar_t*>(HeapAlloc(
        GetProcessHeap(),
        HEAP_ZERO_MEMORY,
        required_characters * sizeof(wchar_t)
    ));
    if (!block) {
        FreeEnvironmentStringsW(inherited);
        return nullptr;
    }

    wchar_t* output = block;
    bool reset_inserted = false;
    for (const wchar_t* entry = inherited; *entry;
         entry += lstrlenW(entry) + 1) {
        if (environment_entry_has_key(entry, kPyInstallerResetKey)) {
            continue;
        }
        if (!reset_inserted &&
            CompareStringOrdinal(
                entry,
                -1,
                kPyInstallerResetEntry,
                -1,
                TRUE
            ) == CSTR_GREATER_THAN) {
            append_environment_entry(output, kPyInstallerResetEntry);
            reset_inserted = true;
        }
        append_environment_entry(output, entry);
    }
    if (!reset_inserted) {
        append_environment_entry(output, kPyInstallerResetEntry);
    }
    *output = L'\0';

    FreeEnvironmentStringsW(inherited);
    return block;
}

void append_log(
    const wchar_t* game_root,
    const wchar_t* message,
    DWORD error_code = ERROR_SUCCESS
) {
    wchar_t* coop_directory = static_cast<wchar_t*>(HeapAlloc(
        GetProcessHeap(), HEAP_ZERO_MEMORY, kPathCapacity * sizeof(wchar_t)
    ));
    wchar_t* log_path = static_cast<wchar_t*>(HeapAlloc(
        GetProcessHeap(), HEAP_ZERO_MEMORY, kPathCapacity * sizeof(wchar_t)
    ));
    if (!coop_directory || !log_path) {
        HeapFree(GetProcessHeap(), 0, coop_directory);
        HeapFree(GetProcessHeap(), 0, log_path);
        return;
    }
    const DWORD local_length = GetEnvironmentVariableW(
        L"LOCALAPPDATA", coop_directory, kPathCapacity
    );
    const bool product_path_ready =
        local_length > 0 && local_length < kPathCapacity &&
        join_path(log_path, kPathCapacity, coop_directory, L"Ivalice Together");
    const bool product_directory_ready =
        product_path_ready &&
        (CreateDirectoryW(log_path, nullptr) != 0 ||
         GetLastError() == ERROR_ALREADY_EXISTS);
    const bool local_runtime = product_path_ready && product_directory_ready;
    if (local_runtime) {
        copy_text(coop_directory, kPathCapacity, log_path);
        if (!join_path(log_path, kPathCapacity, coop_directory, L"Runtime") ||
            (CreateDirectoryW(log_path, nullptr) == 0 &&
             GetLastError() != ERROR_ALREADY_EXISTS) ||
            !copy_text(coop_directory, kPathCapacity, log_path) ||
            !join_path(log_path, kPathCapacity, coop_directory, L"autostart.log")) {
            HeapFree(GetProcessHeap(), 0, coop_directory);
            HeapFree(GetProcessHeap(), 0, log_path);
            return;
        }
    } else if (!join_path(coop_directory, kPathCapacity, game_root, L"coop") ||
               !join_path(
                   log_path,
                   kPathCapacity,
                   coop_directory,
                   L"autostart.log"
               )) {
        HeapFree(GetProcessHeap(), 0, coop_directory);
        HeapFree(GetProcessHeap(), 0, log_path);
        return;
    }
    CreateDirectoryW(coop_directory, nullptr);

    wchar_t wide_line[512] = L"Ivalice Together autostart: ";
    append_text(wide_line, ARRAYSIZE(wide_line), message);
    append_text(wide_line, ARRAYSIZE(wide_line), L" (pid=");
    append_decimal(wide_line, ARRAYSIZE(wide_line), GetCurrentProcessId());
    if (error_code != ERROR_SUCCESS) {
        append_text(wide_line, ARRAYSIZE(wide_line), L", win32=");
        append_decimal(wide_line, ARRAYSIZE(wide_line), error_code);
    }
    append_text(wide_line, ARRAYSIZE(wide_line), L")\r\n");

    int required = WideCharToMultiByte(
        CP_UTF8, 0, wide_line, -1, nullptr, 0, nullptr, nullptr
    );
    if (required > 1) {
        char* utf8 = static_cast<char*>(HeapAlloc(
            GetProcessHeap(), 0, static_cast<SIZE_T>(required)
        ));
        if (utf8 && WideCharToMultiByte(
                CP_UTF8, 0, wide_line, -1, utf8, required, nullptr, nullptr
            )) {
            HANDLE file = CreateFileW(
                log_path,
                FILE_APPEND_DATA,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                nullptr,
                OPEN_ALWAYS,
                FILE_ATTRIBUTE_NORMAL,
                nullptr
            );
            if (file != INVALID_HANDLE_VALUE) {
                DWORD written = 0;
                WriteFile(
                    file,
                    utf8,
                    static_cast<DWORD>(required - 1),
                    &written,
                    nullptr
                );
                CloseHandle(file);
            }
        }
        HeapFree(GetProcessHeap(), 0, utf8);
    }
    HeapFree(GetProcessHeap(), 0, coop_directory);
    HeapFree(GetProcessHeap(), 0, log_path);
}

uintptr_t align_allocation_address(uintptr_t value) {
    return (value + kAllocationGranularity - 1) &
           ~(kAllocationGranularity - 1);
}

bool readable_region(const MEMORY_BASIC_INFORMATION& information) {
    if (information.State != MEM_COMMIT ||
        (information.Protect & (PAGE_GUARD | PAGE_NOACCESS))) {
        return false;
    }
    const DWORD protection = information.Protect & 0xFF;
    return protection == PAGE_READONLY ||
           protection == PAGE_READWRITE ||
           protection == PAGE_WRITECOPY ||
           protection == PAGE_EXECUTE_READ ||
           protection == PAGE_EXECUTE_READWRITE ||
           protection == PAGE_EXECUTE_WRITECOPY;
}

bool memory_has_magic(
    const void* address,
    const char* magic,
    SIZE_T magic_size
) {
    char observed[16] = {};
    SIZE_T transferred = 0;
    if (!address || !magic || !magic_size ||
        magic_size > sizeof(observed)) {
        return false;
    }
    return ReadProcessMemory(
               GetCurrentProcess(),
               address,
               observed,
               magic_size,
               &transferred
           ) &&
           transferred == magic_size &&
           memcmp(observed, magic, magic_size) == 0;
}

void* find_rel32_cave(const char* magic, SIZE_T magic_size) {
    for (const Rel32Range& range : kRel32Ranges) {
        uintptr_t cursor = range.start;
        while (cursor < range.stop) {
            MEMORY_BASIC_INFORMATION information = {};
            if (!VirtualQuery(
                    reinterpret_cast<LPCVOID>(cursor),
                    &information,
                    sizeof(information)
                )) {
                break;
            }
            const uintptr_t base = reinterpret_cast<uintptr_t>(
                information.BaseAddress
            );
            const uintptr_t region_end = base + information.RegionSize;
            if (base >= range.start &&
                base + kRel32CaveSize <= range.stop &&
                base % kAllocationGranularity == 0 &&
                information.RegionSize >= kRel32CaveSize &&
                readable_region(information) &&
                memory_has_magic(
                    information.BaseAddress,
                    magic,
                    magic_size
                )) {
                return information.BaseAddress;
            }
            cursor = region_end > cursor
                ? region_end
                : cursor + kAllocationGranularity;
        }
    }
    return nullptr;
}

void* reserve_rel32_cave(
    const wchar_t* environment_name,
    const char* magic,
    SIZE_T magic_size,
    DWORD* failure
) {
    DWORD last_error = ERROR_NOT_ENOUGH_MEMORY;
    if (void* existing = find_rel32_cave(magic, magic_size)) {
        wchar_t published[16] = {};
        const uintptr_t address = reinterpret_cast<uintptr_t>(existing);
        if (append_decimal(
                published,
                ARRAYSIZE(published),
                static_cast<DWORD>(address)
            ) && SetEnvironmentVariableW(environment_name, published)) {
            if (failure) {
                *failure = ERROR_SUCCESS;
            }
            return existing;
        }
        last_error = GetLastError();
    }
    for (const Rel32Range& range : kRel32Ranges) {
        uintptr_t cursor = range.start;
        while (cursor < range.stop) {
            MEMORY_BASIC_INFORMATION information = {};
            if (!VirtualQuery(
                    reinterpret_cast<LPCVOID>(cursor),
                    &information,
                    sizeof(information)
                )) {
                last_error = GetLastError();
                break;
            }
            const uintptr_t base = reinterpret_cast<uintptr_t>(
                information.BaseAddress
            );
            const uintptr_t region_end = base + information.RegionSize;
            if (information.State == MEM_FREE) {
                uintptr_t candidate = cursor > base ? cursor : base;
                candidate = align_allocation_address(candidate);
                const uintptr_t candidate_stop =
                    region_end < range.stop ? region_end : range.stop;
                while (candidate + kRel32CaveSize <= candidate_stop) {
                    void* allocation = VirtualAlloc(
                        reinterpret_cast<void*>(candidate),
                        kRel32CaveSize,
                        MEM_COMMIT | MEM_RESERVE,
                        PAGE_EXECUTE_READWRITE
                    );
                    if (!allocation) {
                        last_error = GetLastError();
                        allocation = VirtualAlloc(
                            reinterpret_cast<void*>(candidate),
                            kRel32CaveSize,
                            MEM_COMMIT | MEM_RESERVE,
                            PAGE_READWRITE
                        );
                        if (allocation) {
                            DWORD previous = 0;
                            if (!VirtualProtect(
                                    allocation,
                                    kRel32CaveSize,
                                    PAGE_EXECUTE_READWRITE,
                                    &previous
                                )) {
                                last_error = GetLastError();
                                VirtualFree(allocation, 0, MEM_RELEASE);
                                allocation = nullptr;
                            }
                        } else {
                            last_error = GetLastError();
                        }
                    }
                    if (allocation == reinterpret_cast<void*>(candidate)) {
                        CopyMemory(allocation, magic, magic_size);
                        wchar_t published[16] = {};
                        if (append_decimal(
                                published,
                                ARRAYSIZE(published),
                                static_cast<DWORD>(candidate)
                            ) && SetEnvironmentVariableW(
                                environment_name,
                                published
                            )) {
                            if (failure) {
                                *failure = ERROR_SUCCESS;
                            }
                            return allocation;
                        }
                        last_error = GetLastError();
                        VirtualFree(allocation, 0, MEM_RELEASE);
                    } else if (allocation) {
                        VirtualFree(allocation, 0, MEM_RELEASE);
                    }
                    candidate += kAllocationGranularity;
                }
            }
            const uintptr_t next = region_end > cursor
                ? region_end
                : cursor + kAllocationGranularity;
            cursor = next;
        }
    }
    if (failure) {
        *failure = last_error;
    }
    return nullptr;
}

bool reserve_and_publish_runtime_caves(
    const wchar_t* game_root,
    bool log_failures = true
) {
    struct CaveRequest {
        const wchar_t* environment_name;
        const char* magic;
        SIZE_T magic_size;
        const wchar_t* failure_message;
    };
    const CaveRequest requests[] = {
        {
            kCaveBaseEnvironment,
            kCaveMagic,
            sizeof(kCaveMagic) - 1,
            L"could not reserve the primary rel32 cave in-process",
        },
        {
            kDualCaveBaseEnvironment,
            kDualCaveMagic,
            sizeof(kDualCaveMagic) - 1,
            L"could not reserve the interaction rel32 cave in-process",
        },
        {
            kDamageCaveBaseEnvironment,
            kDamageCaveMagic,
            sizeof(kDamageCaveMagic) - 1,
            L"could not reserve the damage rel32 cave in-process",
        },
    };

    bool complete = true;
    for (const CaveRequest& request : requests) {
        DWORD failure = ERROR_SUCCESS;
        if (!reserve_rel32_cave(
                request.environment_name,
                request.magic,
                request.magic_size,
                &failure
            )) {
            complete = false;
            if (log_failures) {
                append_log(game_root, request.failure_message, failure);
            }
        }
    }
    if (complete) {
        append_log(game_root, L"reserved and published native rel32 caves");
    }
    return complete;
}

DWORD WINAPI start_runtime(LPVOID) {
    wchar_t* module_path = static_cast<wchar_t*>(HeapAlloc(
        GetProcessHeap(), HEAP_ZERO_MEMORY, kPathCapacity * sizeof(wchar_t)
    ));
    wchar_t* host_path = static_cast<wchar_t*>(HeapAlloc(
        GetProcessHeap(), HEAP_ZERO_MEMORY, kPathCapacity * sizeof(wchar_t)
    ));
    wchar_t* module_root = static_cast<wchar_t*>(HeapAlloc(
        GetProcessHeap(), HEAP_ZERO_MEMORY, kPathCapacity * sizeof(wchar_t)
    ));
    wchar_t* runtime_path = static_cast<wchar_t*>(HeapAlloc(
        GetProcessHeap(), HEAP_ZERO_MEMORY, kPathCapacity * sizeof(wchar_t)
    ));
    wchar_t* data_directory = static_cast<wchar_t*>(HeapAlloc(
        GetProcessHeap(), HEAP_ZERO_MEMORY, kPathCapacity * sizeof(wchar_t)
    ));
    wchar_t* command_line = static_cast<wchar_t*>(HeapAlloc(
        GetProcessHeap(), HEAP_ZERO_MEMORY, kPathCapacity * sizeof(wchar_t)
    ));
    wchar_t* command_line_template = static_cast<wchar_t*>(HeapAlloc(
        GetProcessHeap(), HEAP_ZERO_MEMORY, kPathCapacity * sizeof(wchar_t)
    ));
    wchar_t* runtime_environment = nullptr;
    bool module_loader_install = false;
    bool caves_ready = false;
    const DWORD game_pid = GetCurrentProcessId();
    DWORD restart_delay = kRestartInitialMilliseconds;
    STARTUPINFOW startup = {};
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION process = {};
    if (!module_path || !host_path || !module_root ||
        !runtime_path || !data_directory || !command_line ||
        !command_line_template) {
        goto cleanup;
    }

    DWORD host_length = GetModuleFileNameW(nullptr, host_path, kPathCapacity);
    if (!host_length || host_length >= kPathCapacity ||
        !equals_ignore_case(filename_part(host_path), kGameExecutable)) {
        goto cleanup;
    }
    if (!remove_filename(host_path) ||
        !equals_ignore_case(filename_part(host_path), L"x64") ||
        !remove_filename(host_path)) {
        goto cleanup;
    }

    DWORD module_length = GetModuleFileNameW(
        g_module, module_path, kPathCapacity
    );
    if (!module_length || module_length >= kPathCapacity ||
        !copy_text(module_root, kPathCapacity, module_path) ||
        !remove_filename(module_root)) {
        goto cleanup;
    }

    module_loader_install =
        equals_ignore_case(filename_part(module_path), kAutostartModule) &&
        equals_ignore_case(filename_part(module_root), L"modules") &&
        remove_filename(module_root) &&
        equals_ignore_case(filename_part(module_root), L"x64");

    caves_ready = reserve_and_publish_runtime_caves(host_path);
    if (!caves_ready) {
        append_log(host_path, L"native rel32 cave bootstrap is incomplete");
    }
    if (!module_loader_install) {
        goto cleanup;
    }

    wchar_t guard_name[96] = L"Local\\IvaliceTogetherAutostart_";
    if (!append_decimal(
            guard_name, ARRAYSIZE(guard_name), game_pid
        )) {
        append_log(host_path, L"could not build the process guard name");
        goto cleanup;
    }
    SetLastError(ERROR_SUCCESS);
    g_process_guard = CreateMutexW(nullptr, FALSE, guard_name);
    if (!g_process_guard) {
        append_log(host_path, L"could not create the process guard", GetLastError());
        goto cleanup;
    }
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        CloseHandle(g_process_guard);
        g_process_guard = nullptr;
        goto cleanup;
    }
    if (!pin_autostart_module()) {
        append_log(
            host_path,
            L"could not pin the process-lifetime autostart sentinel",
            GetLastError()
        );
        CloseHandle(g_process_guard);
        g_process_guard = nullptr;
        goto cleanup;
    }

    while (!caves_ready) {
        Sleep(restart_delay);
        caves_ready = reserve_and_publish_runtime_caves(host_path, false);
        restart_delay = next_restart_delay(restart_delay);
    }
    restart_delay = kRestartInitialMilliseconds;

    bool package_failure_logged = false;
    while (!resolve_package_paths(
               runtime_path,
               data_directory,
               module_root,
               host_path
           )) {
        if (!package_failure_logged) {
            append_log(
                host_path,
                L"the co-op runtime package was not found beside the autostart module"
            );
            package_failure_logged = true;
        }
        Sleep(restart_delay);
        restart_delay = next_restart_delay(restart_delay);
    }
    restart_delay = kRestartInitialMilliseconds;

    if (!append_text(command_line_template, kPathCapacity, L"\"") ||
        !append_text(command_line_template, kPathCapacity, runtime_path) ||
        !append_text(command_line_template, kPathCapacity, L"\"") ||
        !append_text(
            command_line_template,
            kPathCapacity,
            kRuntimeArguments
        ) ||
        !append_decimal(
            command_line_template, kPathCapacity, game_pid
        )) {
        append_log(host_path, L"runtime command line is too long");
        goto cleanup;
    }

    bool environment_failure_logged = false;
    while (!SetEnvironmentVariableW(kGameRootEnvironment, host_path) ||
           !SetEnvironmentVariableW(
               kDataDirectoryEnvironment,
               data_directory
           )) {
        if (!environment_failure_logged) {
            append_log(
                host_path,
                L"could not publish the game and configuration directories",
                GetLastError()
            );
            environment_failure_logged = true;
        }
        Sleep(restart_delay);
        restart_delay = next_restart_delay(restart_delay);
    }
    restart_delay = kRestartInitialMilliseconds;

    bool runtime_environment_failure_logged = false;
    while (!(runtime_environment = build_runtime_environment())) {
        if (!runtime_environment_failure_logged) {
            append_log(
                host_path,
                L"could not build the isolated runtime environment"
            );
            runtime_environment_failure_logged = true;
        }
        Sleep(restart_delay);
        restart_delay = next_restart_delay(restart_delay);
    }
    restart_delay = kRestartInitialMilliseconds;

    // This process-lifetime sentinel is the final recovery boundary.  Python
    // normally keeps the supervisor alive and reattaches resident helpers, but
    // a bootloader failure or hard process termination must not make co-op a
    // one-shot feature.  An exact live supervisor mutex suppresses duplicates;
    // once it disappears, the same pinned worker requests a replacement.
    for (;;) {
        if (runtime_mutex_active(game_pid)) {
            restart_delay = kRestartInitialMilliseconds;
            Sleep(kRuntimePollMilliseconds);
            continue;
        }

        ZeroMemory(&process, sizeof(process));
        if (!copy_text(
                command_line,
                kPathCapacity,
                command_line_template
            )) {
            append_log(host_path, L"could not refresh the runtime command line");
            Sleep(restart_delay);
            restart_delay = next_restart_delay(restart_delay);
            continue;
        }
        const ULONGLONG started_at = GetTickCount64();
        if (!CreateProcessW(
                runtime_path,
                command_line,
                nullptr,
                nullptr,
                FALSE,
                CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
                runtime_environment,
                host_path,
                &startup,
                &process
            )) {
            append_log(
                host_path,
                L"could not start the co-op runtime; retrying",
                GetLastError()
            );
            Sleep(restart_delay);
            restart_delay = next_restart_delay(restart_delay);
            continue;
        }
        CloseHandle(process.hThread);
        process.hThread = nullptr;
        append_log(host_path, L"runtime requested");

        bool wait_failure_logged = false;
        for (;;) {
            SetLastError(ERROR_SUCCESS);
            const DWORD wait_result = WaitForSingleObject(
                process.hProcess, INFINITE
            );
            if (wait_result == WAIT_OBJECT_0) {
                break;
            }
            // A failed/unknown wait is not proof that the child exited.  Keep
            // its identity handle and fail closed instead of ever starting a
            // duplicate supervisor.  Game-process termination is the only
            // asynchronous interruption of this process-lifetime worker.
            const DWORD wait_error = wait_result == WAIT_FAILED
                ? GetLastError()
                : ERROR_INVALID_STATE;
            if (!wait_failure_logged) {
                append_log(
                    host_path,
                    L"runtime wait is uncertain; retaining the child identity",
                    wait_error
                );
                wait_failure_logged = true;
            }
            Sleep(kRuntimePollMilliseconds);
        }
        const ULONGLONG stopped_at = GetTickCount64();
        CloseHandle(process.hProcess);
        process.hProcess = nullptr;
        append_log(
            host_path,
            L"runtime stopped while the game is alive; restarting"
        );
        if (stopped_at >= started_at &&
            stopped_at - started_at >= kStableRuntimeMilliseconds) {
            restart_delay = kRestartInitialMilliseconds;
        } else {
            restart_delay = next_restart_delay(restart_delay);
        }
        Sleep(restart_delay);
    }

cleanup:
    if (g_process_guard) {
        CloseHandle(g_process_guard);
        g_process_guard = nullptr;
    }
    HeapFree(GetProcessHeap(), 0, module_path);
    HeapFree(GetProcessHeap(), 0, host_path);
    HeapFree(GetProcessHeap(), 0, module_root);
    HeapFree(GetProcessHeap(), 0, runtime_path);
    HeapFree(GetProcessHeap(), 0, data_directory);
    HeapFree(GetProcessHeap(), 0, command_line);
    HeapFree(GetProcessHeap(), 0, command_line_template);
    HeapFree(GetProcessHeap(), 0, runtime_environment);
    return 0;
}

}  // namespace

struct FF12HookVersion {
    DWORD major;
    DWORD minor;
    DWORD patch;
};

extern "C" __declspec(dllexport) const char* FF12HgetName() {
    return "Ivalice Together Automatic Runtime";
}

extern "C" __declspec(dllexport) FF12HookVersion* FF12HgetVer(
    FF12HookVersion* version
) {
    if (version) {
        version->major = 1;
        version->minor = 0;
        version->patch = 13;
    }
    return version;
}

extern "C" BOOL WINAPI DllMain(HMODULE module, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_module = module;
        DisableThreadLibraryCalls(module);
        HANDLE thread = CreateThread(nullptr, 0, start_runtime, nullptr, 0, nullptr);
        if (thread) {
            CloseHandle(thread);
        }
    }
    return TRUE;
}
