"""One-shot snapshot tracer for FF12's native command-menu builder.

The native builder at ``0x31EB20`` is frequently called with a descriptor
that lives on a caller's stack.  Recording RCX at function entry and reading
it later is therefore unsafe: the pointer may already be invalid or reused.

This tracer hooks the builder's *common epilogue* instead.  At ``0x31FB65``
RBX still points at the completed descriptor, EAX still contains the native
return value, and the function's stack frame is intact.  The codecave copies
the descriptor header and up to 0x200 eight-byte entries into a private,
fixed-size remote buffer before executing the exact displaced epilogue.

Safety properties:

* the 16 original bytes are validated before installation;
* no game function is called from the codecave;
* records are bounded and excess calls only increment an overflow counter;
* Ctrl+C and ordinary exceptions restore the original bytes in ``finally``;
* the allocation is deliberately leaked rather than freed if restoration
  cannot be proven, preventing a jump into freed memory;
* captured stack pointers are never dereferenced after the hook is removed.

The script is an offline reverse-engineering tool, not a component of the
co-op runtime. Typical use::

    py coop/menu_builder_snapshot_trace.py --duration 15 --label penelo

Arm it while Vaan's native menu is open, then switch once to Penelo.  The
resulting JSON contains every builder event plus exact-content-deduplicated
snapshots suitable for an independent P2 menu model.
"""

from __future__ import annotations

import argparse
import ctypes as ct
import hashlib
import importlib.util
import json
import os
import sys
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


COOP = Path(__file__).resolve().parents[1] / "source"
sys.path.insert(0, str(COOP))  # co-op module dependencies
PROCESS_NAME = "FFXII_TZA.exe"

BUILDER = 0x31EB20
HOOK_SITE = 0x31FB65
HOOK_RETURN = 0x31FB75

# 0x31FB65..0x31FB74, instruction boundaries verified from the Steam 1.0.4.0
# executable (SHA-256 73461D...D89721F8).
ORIGINAL_EPILOGUE = bytes.fromhex(
    "0F 28 74 24 70 "          # movaps xmm6,[rsp+70]
    "48 81 C4 88 00 00 00 "   # add rsp,88
    "5F 5E 5D 5B"             # pop rdi/rsi/rbp/rbx
)
PATCH_SIZE = len(ORIGINAL_EPILOGUE)

MAX_ENTRIES = 0x200
MAX_RECORDS = 512

STATE_COUNT = 0x00
STATE_OVERFLOW = 0x04
STATE_ACTIVE = 0x08
RECORDS_OFFSET = 0x100

RECORD_META_SIZE = 0x40
DESCRIPTOR_HEADER_SIZE = 0x50
RECORD_HEADER_OFFSET = RECORD_META_SIZE
RECORD_ENTRIES_OFFSET = RECORD_HEADER_OFFSET + DESCRIPTOR_HEADER_SIZE
RECORD_SIZE = RECORD_ENTRIES_OFFSET + MAX_ENTRIES * 8


def align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


CAVE_OFFSET = align_up(RECORDS_OFFSET + MAX_RECORDS * RECORD_SIZE, 0x1000)
WORKSPACE_SIZE = CAVE_OFFSET + 0x1000

MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_EXECUTE_READWRITE = 0x40


def hex_address(value: int | None) -> str | None:
    return None if value is None else f"0x{value:x}"


def rel32(source: int, instruction_size: int, target: int) -> bytes:
    displacement = target - (source + instruction_size)
    if not -(1 << 31) <= displacement < (1 << 31):
        raise ValueError(
            f"rel32 out of range: source={source:#x} target={target:#x}"
        )
    return struct.pack("<i", displacement)


def absolute_jump(target: int) -> bytes:
    """RIP-indirect x64 jump that does not clobber a register."""

    return b"\xFF\x25\x00\x00\x00\x00" + struct.pack("<Q", target)


def jump_patch(cave: int) -> bytes:
    patch = absolute_jump(cave)
    return patch + b"\x90" * (PATCH_SIZE - len(patch))


class CodeBuilder:
    """Tiny label/fixup helper for the handful of rel32 branches below."""

    def __init__(self) -> None:
        self.code = bytearray()
        self.labels: dict[str, int] = {}
        self.fixups: list[tuple[int, str]] = []

    def emit(self, data: bytes) -> None:
        self.code.extend(data)

    def label(self, name: str) -> None:
        if name in self.labels:
            raise ValueError(f"duplicate label {name}")
        self.labels[name] = len(self.code)

    def jcc(self, opcode: int, label: str) -> None:
        self.emit(b"\x0F" + bytes((opcode,)))
        self.fixups.append((len(self.code), label))
        self.emit(b"\0" * 4)

    def jmp(self, label: str) -> None:
        self.emit(b"\xE9")
        self.fixups.append((len(self.code), label))
        self.emit(b"\0" * 4)

    def finish(self, base: int) -> bytes:
        for displacement_offset, label in self.fixups:
            if label not in self.labels:
                raise ValueError(f"undefined label {label}")
            target = base + self.labels[label]
            source_after = base + displacement_offset + 4
            struct.pack_into(
                "<i", self.code, displacement_offset, target - source_after
            )
        return bytes(self.code)


def build_cave(
    cave: int,
    state: int,
    *,
    actor_token: int | None = None,
    caller_filter: int | None = None,
) -> bytes:
    """Build the epilogue logger without calling any game/OS function.

    At the hook point RBX is the completed descriptor.  Ten balanced pushes
    make the original caller return address available at ``[rsp+0xF8]``:
    0x50 bytes from our saves plus 0xA8 bytes from the builder frame.
    """

    c = CodeBuilder()

    # Preserve all registers used by the logger, EAX's native return value,
    # and flags.  Non-volatiles would be restored by the native epilogue too,
    # but balancing them here makes the injected block locally transparent.
    c.emit(
        b"\x9C"                  # pushfq
        b"\x50\x51\x52\x56\x57"  # rax,rcx,rdx,rsi,rdi
        b"\x41\x50\x41\x51\x41\x52\x41\x53"  # r8-r11
    )

    c.emit(b"\x49\xBB" + struct.pack("<Q", state))  # mov r11,state
    c.emit(b"\xF0\x41\xFF\x43\x08")              # lock inc [r11+active]

    # Optional event filters run before reserving a record.  All rejection
    # paths still pass through ``done`` and decrement STATE_ACTIVE.
    if actor_token is not None:
        c.emit(b"\x81\x7B\x08" + struct.pack("<I", actor_token & 0xFFFFFFFF))
        c.jcc(0x85, "done")  # jne
    if caller_filter is not None:
        c.emit(b"\x44\x8B\x94\x24\xF8\x00\x00\x00")  # r10d=[rsp+f8]
        c.emit(b"\x41\x81\xFA" + struct.pack("<I", caller_filter & 0xFFFFFFFF))
        c.jcc(0x85, "done")  # jne

    c.emit(b"\x0F\x31")                              # rdtsc
    c.emit(b"\x48\xC1\xE2\x20")                  # shl rdx,32
    c.emit(b"\x48\x09\xD0")                       # or rax,rdx

    # Reserve one bounded record atomically. EDX receives the old index.
    c.emit(b"\xBA\x01\x00\x00\x00")              # mov edx,1
    c.emit(b"\xF0\x41\x0F\xC1\x13")              # lock xadd [r11],edx
    c.emit(b"\x81\xFA" + struct.pack("<I", MAX_RECORDS))
    c.jcc(0x83, "overflow")                            # jae overflow

    # Preserve old_index+1 before RDX is scaled into a byte offset.
    c.emit(b"\x89\xD1\xFF\xC1")                    # ecx=sequence
    c.emit(
        b"\x48\x69\xD2" + struct.pack("<I", RECORD_SIZE)
    )                                                   # imul rdx,rdx,stride
    c.emit(
        b"\x49\x8D\xBC\x13" + struct.pack("<I", RECORDS_OFFSET)
    )                                                   # lea rdi,[r11+rdx+off]

    # Fixed metadata.
    c.emit(b"\x89\x0F")                              # record.sequence
    c.emit(b"\x44\x8B\x0B")                       # mov r9d,[rbx] type
    c.emit(b"\x44\x89\x4F\x04")                  # record.type
    c.emit(b"\x48\x89\x5F\x08")                  # descriptor pointer
    c.emit(b"\x48\x89\x47\x10")                  # timestamp TSC
    c.emit(b"\x4C\x8B\x84\x24\xF8\x00\x00\x00")  # caller ret
    c.emit(b"\x4C\x89\x47\x18")                  # record.caller
    c.emit(b"\x8B\x4C\x24\x40\x89\x4F\x20") # saved EAX result
    c.emit(b"\x8B\x4B\x40\x89\x4F\x24")      # raw count

    # Clamp copy length to [0,0x200], keeping diagnostics in record.flags.
    c.emit(b"\x45\x33\xC0")                       # xor r8d,r8d
    c.emit(b"\x85\xC9")                            # test ecx,ecx
    c.jcc(0x88, "negative_count")                      # js
    c.emit(b"\x81\xF9" + struct.pack("<I", MAX_ENTRIES))
    c.jcc(0x86, "count_ready")                         # jbe
    c.emit(b"\xB9" + struct.pack("<I", MAX_ENTRIES))
    c.emit(b"\x41\x83\xC8\x01")                  # flags |= clamped
    c.jmp("count_ready")
    c.label("negative_count")
    c.emit(b"\x33\xC9")                             # xor ecx,ecx
    c.emit(b"\x41\x83\xC8\x02")                  # flags |= negative count
    c.label("count_ready")
    c.emit(b"\x89\x4F\x28")                       # copied count
    c.emit(b"\x8B\x57\x20\x85\xD2")            # test return value
    c.jcc(0x89, "return_nonnegative")                  # jns
    c.emit(b"\x41\x83\xC8\x04")                  # flags |= negative result
    c.label("return_nonnegative")
    c.emit(b"\x44\x89\x47\x2C")                  # record.flags
    c.emit(b"\xC7\x47\x30" + struct.pack("<I", DESCRIPTOR_HEADER_SIZE))
    c.emit(b"\xC7\x47\x34\x08\x00\x00\x00") # entry stride

    # Copy header and entries while RBX is unquestionably valid.
    c.emit(b"\x49\x89\xF9")                       # mov r9,rdi record base
    c.emit(b"\x48\x89\xDE")                       # mov rsi,rbx
    c.emit(b"\x49\x8D\x79\x40")                  # lea rdi,[r9+40]
    c.emit(b"\xB9" + struct.pack("<I", DESCRIPTOR_HEADER_SIZE))
    c.emit(b"\xF3\xA4")                             # rep movsb
    c.emit(b"\x48\x8D\x73\x50")                  # lea rsi,[rbx+50]
    c.emit(b"\x49\x8D\xB9" + struct.pack("<I", RECORD_ENTRIES_OFFSET))
    c.emit(b"\x41\x8B\x49\x28\xC1\xE1\x03") # bytes=count*8
    c.emit(b"\xF3\xA4")                             # rep movsb
    c.jmp("done")

    c.label("overflow")
    c.emit(b"\xF0\x41\xFF\x43\x04")              # lock inc [r11+4]

    c.label("done")
    c.emit(b"\xF0\x41\xFF\x4B\x08")              # lock dec [r11+active]
    c.emit(
        b"\x41\x5B\x41\x5A\x41\x59\x41\x58"  # r11-r8
        b"\x5F\x5E\x5A\x59\x58\x9D"              # rdi..rax,popfq
    )
    c.emit(ORIGINAL_EPILOGUE)
    c.emit(absolute_jump(HOOK_RETURN))
    return c.finish(cave)


def parse_trace_buffer(raw: bytes) -> dict[str, Any]:
    """Parse the fixed remote buffer into immutable Python records."""

    minimum = RECORDS_OFFSET
    if len(raw) < minimum:
        raise ValueError(f"trace buffer too short: {len(raw):#x} < {minimum:#x}")
    attempted, overflow = struct.unpack_from("<II", raw, 0)
    stored = min(attempted, MAX_RECORDS)
    required = RECORDS_OFFSET + stored * RECORD_SIZE
    if len(raw) < required:
        raise ValueError(f"trace buffer truncated: {len(raw):#x} < {required:#x}")

    records: list[dict[str, Any]] = []
    for index in range(stored):
        base = RECORDS_OFFSET + index * RECORD_SIZE
        (
            sequence,
            menu_type,
            descriptor,
            timestamp_tsc,
            caller,
            return_value,
            count_raw,
            count_copied,
            flags,
            header_size,
            entry_stride,
        ) = struct.unpack_from("<IIQQQiIIIII", raw, base)
        if sequence == 0:
            # A record should be complete after the hook is restored and the
            # grace period elapses. Keep diagnostics explicit if it is not.
            continue
        if header_size != DESCRIPTOR_HEADER_SIZE:
            raise ValueError(
                f"record {index} header size {header_size:#x} is unexpected"
            )
        if entry_stride != 8:
            raise ValueError(
                f"record {index} entry stride {entry_stride:#x} is unexpected"
            )
        count_copied = min(count_copied, MAX_ENTRIES)
        header_start = base + RECORD_HEADER_OFFSET
        entries_start = base + RECORD_ENTRIES_OFFSET
        header = bytes(raw[header_start : header_start + DESCRIPTOR_HEADER_SIZE])
        entries = bytes(raw[entries_start : entries_start + count_copied * 8])
        records.append(
            {
                "sequence": sequence,
                "type": menu_type,
                "descriptor": descriptor,
                "timestamp_tsc": timestamp_tsc,
                "caller": caller,
                "return_value": return_value,
                "count_raw": count_raw,
                "count_copied": count_copied,
                "flags": flags,
                "header": header,
                "entries": entries,
            }
        )
    return {
        "attempted": attempted,
        "stored": len(records),
        "overflow": overflow,
        "records": records,
    }


def decode_header(header: bytes) -> dict[str, Any]:
    if len(header) != DESCRIPTOR_HEADER_SIZE:
        raise ValueError("descriptor header must be exactly 0x50 bytes")
    fields = {
        f"u32_{offset:02x}": struct.unpack_from("<I", header, offset)[0]
        for offset in range(0, DESCRIPTOR_HEADER_SIZE, 4)
    }
    fields.update(
        {
            "type_i32_00": struct.unpack_from("<i", header, 0x00)[0],
            "type_u32_00": struct.unpack_from("<I", header, 0x00)[0],
            "actor_or_context_u32_08": struct.unpack_from("<I", header, 0x08)[0],
            "discriminator_u32_14": struct.unpack_from("<I", header, 0x14)[0],
            "count_u32_40": struct.unpack_from("<I", header, 0x40)[0],
        }
    )
    return fields


def decode_entries(entries: bytes) -> list[dict[str, Any]]:
    if len(entries) % 8:
        raise ValueError("entry buffer size is not a multiple of 8")
    result = []
    for index in range(len(entries) // 8):
        entry = entries[index * 8 : index * 8 + 8]
        result.append(
            {
                "index": index,
                "dialog_id_u16_50": struct.unpack_from("<H", entry, 0)[0],
                "u16_52": struct.unpack_from("<H", entry, 2)[0],
                "flags_53": entry[3],
                "u16_54": struct.unpack_from("<H", entry, 4)[0],
                "value_54": entry[4],
                "value_56": entry[6],
                "value_57": entry[7],
                "u64": hex_address(struct.unpack_from("<Q", entry, 0)[0]),
                "raw": entry.hex(),
            }
        )
    return result


def snapshot_digest(record: dict[str, Any]) -> str:
    """Content identity excluding ephemeral pointer/sequence/timestamp."""

    digest = hashlib.sha256()
    digest.update(
        struct.pack(
            "<IQiII",
            record["type"],
            record["caller"],
            record["return_value"],
            record["count_raw"],
            record["count_copied"],
        )
    )
    digest.update(record["header"])
    digest.update(record["entries"])
    return digest.hexdigest()


def deduplicate_records(
    records: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Deduplicate only exact completed outputs, never pointer/type alone.

    This is important because the builder can emit many type-0x0B variants in
    one burst from the same stack address.  Pointer-only or type-only dedup
    would silently discard real Magicks/Foecraft/Items pages.
    """

    snapshots: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    by_digest: dict[str, int] = {}
    for record in records:
        digest = snapshot_digest(record)
        snapshot_id = by_digest.get(digest)
        if snapshot_id is None:
            snapshot_id = len(snapshots)
            by_digest[digest] = snapshot_id
            header = record["header"]
            entries = record["entries"]
            snapshots.append(
                {
                    "snapshot_id": snapshot_id,
                    "sha256": digest,
                    "repeat_count": 0,
                    "type": record["type"],
                    "type_hex": hex_address(record["type"]),
                    "caller": hex_address(record["caller"]),
                    "return_value": record["return_value"],
                    "count_raw": record["count_raw"],
                    "count_copied": record["count_copied"],
                    "capture_flags": record["flags"],
                    "header": {
                        "size": len(header),
                        "sha256": hashlib.sha256(header).hexdigest(),
                        "hex": header.hex(),
                        "decoded": decode_header(header),
                    },
                    "entries": decode_entries(entries),
                }
            )
        snapshots[snapshot_id]["repeat_count"] += 1
        events.append(
            {
                "sequence": record["sequence"],
                "timestamp_tsc": record["timestamp_tsc"],
                "descriptor": hex_address(record["descriptor"]),
                "type": record["type"],
                "type_hex": hex_address(record["type"]),
                "caller": hex_address(record["caller"]),
                "return_value": record["return_value"],
                "count_raw": record["count_raw"],
                "snapshot_id": snapshot_id,
            }
        )
    return snapshots, events


def default_output(label: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    return COOP / "native_menu_snapshots" / f"builder_{safe}_{stamp}.json"


def load_coop_module() -> Any:
    path = COOP / "coop_runtime.py"
    spec = importlib.util.spec_from_file_location("ff12coop_builder_trace", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def allocation_candidates() -> Iterable[int]:
    """Yield 64-KiB-aligned low addresses whose cave fits rel32."""

    anchors = (0x30000000, 0x40000000, 0x50000000, 0x60000000, 0x20000000)
    seen: set[int] = set()
    for anchor in anchors:
        for delta in range(0, 0x02000000, 0x10000):
            for candidate in (anchor + delta, anchor - delta):
                if candidate in seen or candidate < 0x10000:
                    continue
                seen.add(candidate)
                cave = candidate + CAVE_OFFSET
                try:
                    rel32(HOOK_SITE, 5, cave)
                except ValueError:
                    continue
                yield candidate


def allocate_workspace(process: Any, coop: Any) -> int:
    """Allocate a dedicated workspace anywhere in the x64 address space."""

    kernel32 = coop.kernel32
    kernel32.VirtualFreeEx.argtypes = [
        ct.c_void_p,
        ct.c_void_p,
        ct.c_size_t,
        ct.c_ulong,
    ]
    kernel32.VirtualFreeEx.restype = ct.c_int

    result = kernel32.VirtualAllocEx(
        process.handle,
        None,
        WORKSPACE_SIZE,
        MEM_COMMIT | MEM_RESERVE,
        PAGE_EXECUTE_READWRITE,
    )
    if not result:
        raise OSError(ct.get_last_error(), "VirtualAllocEx(trace workspace)")
    return int(result)


def free_workspace(process: Any, coop: Any, address: int) -> None:
    if not address:
        return
    ok = coop.kernel32.VirtualFreeEx(
        process.handle, ct.c_void_p(address), 0, MEM_RELEASE
    )
    if not ok:
        raise OSError(ct.get_last_error(), f"VirtualFreeEx({address:#x})")


class InstalledTrace:
    """Owns one hook and proves restoration before allowing memory release."""

    def __init__(
        self,
        process: Any,
        workspace: int,
        *,
        actor_token: int | None = None,
        caller_filter: int | None = None,
    ):
        self.process = process
        self.workspace = workspace
        self.cave = workspace + CAVE_OFFSET
        self.cave_code = build_cave(
            self.cave,
            workspace,
            actor_token=actor_token,
            caller_filter=caller_filter,
        )
        self.patch = jump_patch(self.cave)
        self.install_attempted = False
        self.installed = False
        self.restored = False

    def install(self) -> None:
        current = self.process.read(HOOK_SITE, PATCH_SIZE)
        if current != ORIGINAL_EPILOGUE:
            raise RuntimeError(
                "builder epilogue is not pristine; refusing to stack hooks: "
                f"{current.hex(' ')}"
            )
        self.process.write(self.workspace, b"\0" * WORKSPACE_SIZE)
        self.process.write(self.cave, self.cave_code)
        if self.process.read(self.cave, len(self.cave_code)) != self.cave_code:
            raise RuntimeError("codecave readback verification failed")
        self.install_attempted = True
        self.process.patch_code(HOOK_SITE, self.patch)
        if self.process.read(HOOK_SITE, PATCH_SIZE) != self.patch:
            raise RuntimeError("hook readback verification failed")
        self.installed = True

    def restore(self) -> None:
        current = self.process.read(HOOK_SITE, PATCH_SIZE)
        if current == ORIGINAL_EPILOGUE:
            self.installed = False
            self.restored = True
            return
        if current != self.patch:
            raise RuntimeError(
                "builder hook site changed by another writer; workspace retained: "
                f"{current.hex(' ')}"
            )
        self.process.patch_code(HOOK_SITE, ORIGINAL_EPILOGUE)
        verified = self.process.read(HOOK_SITE, PATCH_SIZE)
        if verified != ORIGINAL_EPILOGUE:
            raise RuntimeError(
                "critical: builder epilogue restoration did not verify; "
                "workspace retained"
            )
        self.installed = False
        self.restored = True

    def raw_buffer(self) -> bytes:
        return self.process.read(self.workspace, CAVE_OFFSET)


def wait_for_quiescence(
    process: Any,
    state: int,
    timeout: float = 2.0,
    stable_seconds: float = 0.050,
    poll_seconds: float = 0.002,
) -> None:
    """Wait until no thread is executing the restored codecave hook."""

    deadline = time.monotonic() + timeout
    zero_since: float | None = None
    last_active = -1
    while time.monotonic() < deadline:
        last_active = struct.unpack(
            "<I", process.read(state + STATE_ACTIVE, 4)
        )[0]
        now = time.monotonic()
        if last_active == 0:
            if zero_since is None:
                zero_since = now
            if now - zero_since >= stable_seconds:
                return
        else:
            zero_since = None
        time.sleep(poll_seconds)
    raise TimeoutError(
        f"builder codecave did not quiesce within {timeout:.3f}s "
        f"(active={last_active}); workspace must remain allocated"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Temporarily capture completed outputs of FF12's native 0x31EB20 "
            "command-menu builder."
        )
    )
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--label", default="penelo")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--pid", type=int)
    parser.add_argument(
        "--actor-token",
        type=lambda value: int(value, 0),
        help="only record descriptors whose +0x08 u32 matches this value",
    )
    parser.add_argument(
        "--caller",
        type=lambda value: int(value, 0),
        help="only record this builder return address (for example 0x27e317)",
    )
    parser.add_argument(
        "--retain-workspace",
        action="store_true",
        help="retain the private capture buffer after the hook is restored",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if os.name != "nt":
        raise OSError("live builder tracing requires Windows")
    args = parse_args(argv)
    if args.duration <= 0:
        raise ValueError("--duration must be positive")

    coop = load_coop_module()
    pid = args.pid if args.pid is not None else coop.find_pid()
    process = coop.RemoteProcess(pid)
    workspace = 0
    trace: InstalledTrace | None = None
    safe_to_free = False
    retain_reason = "hook restoration/quiescence was not proven"
    parsed: dict[str, Any] | None = None
    destination = args.output or default_output(args.label)
    try:
        # Validate before allocating so a permanent/foreign hook is never
        # disturbed just because the tracer was started.
        pristine = process.read(HOOK_SITE, PATCH_SIZE)
        if pristine != ORIGINAL_EPILOGUE:
            raise RuntimeError(
                f"unexpected bytes at {HOOK_SITE:#x}: {pristine.hex(' ')}"
            )
        workspace = allocate_workspace(process, coop)
        trace = InstalledTrace(
            process,
            workspace,
            actor_token=args.actor_token,
            caller_filter=args.caller,
        )
        trace.install()
        print(
            "BUILDER_SNAPSHOT_TRACE_ARMED "
            f"pid={pid} duration={args.duration:.1f}s workspace={workspace:#x}\n"
            "Switch the native command menu from Vaan to Penelo now."
        )

        deadline = time.monotonic() + args.duration
        while time.monotonic() < deadline:
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

        # Stop future entries, then wait on the cave's real in-flight counter.
        trace.restore()
        wait_for_quiescence(process, workspace)
        parsed = parse_trace_buffer(trace.raw_buffer())
        safe_to_free = not args.retain_workspace
        if args.retain_workspace:
            retain_reason = "--retain-workspace was requested"
    finally:
        if trace is not None and not trace.restored:
            try:
                trace.restore()
                wait_for_quiescence(process, workspace)
                if parsed is None:
                    parsed = parse_trace_buffer(trace.raw_buffer())
                safe_to_free = not args.retain_workspace
                if args.retain_workspace:
                    retain_reason = "--retain-workspace was requested"
            except Exception as restore_error:
                safe_to_free = False
                print(f"CRITICAL_ROLLBACK_WARNING {restore_error}")
        if workspace and safe_to_free:
            free_workspace(process, coop, workspace)
        elif workspace:
            print(
                f"WORKSPACE_RETAINED address={workspace:#x}: {retain_reason}. "
                "Do not free it while the game is running."
            )
        process.close()

    if parsed is None:
        raise RuntimeError("trace completed without a readable buffer")
    snapshots, events = deduplicate_records(parsed["records"])
    document = {
        "schema": "ff12.native-builder-output-snapshot.v1",
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "label": args.label,
        "process": {"pid": pid, "name": PROCESS_NAME},
        "build": {
            "image_sha256_expected": (
                "73461d0896204b1c259c52f61aef4efbc95a2ffc388cadb36493d5d1d89721f8"
            ),
            "builder": hex_address(BUILDER),
            "hook_site": hex_address(HOOK_SITE),
            "hook_return": hex_address(HOOK_RETURN),
            "original_epilogue": ORIGINAL_EPILOGUE.hex(),
        },
        "capture": {
            "duration_seconds": args.duration,
            "actor_token_filter": hex_address(args.actor_token),
            "caller_filter": hex_address(args.caller),
            "attempted_records": parsed["attempted"],
            "stored_records": parsed["stored"],
            "overflow_records": parsed["overflow"],
            "max_records": MAX_RECORDS,
            "max_entries_per_record": MAX_ENTRIES,
            "deduplication": (
                "exact completed output: type+caller+return+count+header+entries; "
                "descriptor pointer/timestamp excluded"
            ),
        },
        "events": events,
        "snapshots": snapshots,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    print(
        "BUILDER_SNAPSHOT_TRACE_COMPLETE "
        f"output={destination} events={len(events)} "
        f"unique_snapshots={len(snapshots)} overflow={parsed['overflow']}"
    )
    print("Original builder epilogue restored and verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
