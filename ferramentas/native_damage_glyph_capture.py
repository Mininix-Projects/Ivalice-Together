"""Short-lived capture of the native battle-number glyph renderer.

The candidate renderer at ``0x366A30`` receives one glyph entry at a time.
The entry contains the glyph index, the loaded atlas, its sprite table and
the packed native palette selector.  This diagnostic copies those flat fields
to a private remote page and always restores the exact function prologue.

It is a reverse-engineering probe, not a resident co-op feature.
"""

from __future__ import annotations

import argparse
import ctypes as ct
import importlib.util
import json
import struct
import sys
import time
from pathlib import Path


COOP = Path(__file__).resolve().parents[1] / "source"
sys.path.insert(0, str(COOP))  # co-op module dependencies
spec = importlib.util.spec_from_file_location(
    "ff12coop_damage_glyph_capture", COOP / "coop_runtime.py"
)
coop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coop)

HOOK = 0x366A30
RETURN = 0x366A3A
ORIGINAL = bytes.fromhex("48 89 5C 24 18 57 48 83 EC 50")
BUFFER_OFFSET = 0x1000
CODE_OFFSET = 0xE000
HEADER_SIZE = 0x20
RECORD_SIZE = 0x100
CAPACITY = 192
ENTRY_SIZE = 0x60
ENTRY_RECORD_OFFSET = 0x40
MEM_RELEASE = 0x8000

coop.kernel32.VirtualFreeEx.argtypes = [
    coop.wt.HANDLE,
    ct.c_void_p,
    ct.c_size_t,
    coop.wt.DWORD,
]
coop.kernel32.VirtualFreeEx.restype = coop.wt.BOOL


def rel32(source: int, size: int, target: int) -> bytes:
    delta = target - (source + size)
    if not -(1 << 31) <= delta < (1 << 31):
        raise RuntimeError("relative branch is outside +/-2 GB")
    return struct.pack("<i", delta)


def _store_qword(code: bytearray, source_offset: int, destination: int) -> None:
    if source_offset == 0:
        code += b"\x48\x8B\x02"  # mov rax,[rdx]
    elif source_offset < 0x80:
        code += b"\x48\x8B\x42" + bytes((source_offset,))
    else:
        code += b"\x48\x8B\x82" + struct.pack("<I", source_offset)
    if destination < 0x80:
        code += b"\x49\x89\x42" + bytes((destination,))
    else:
        code += b"\x49\x89\x82" + struct.pack("<I", destination)


def build_hook(code_address: int, buffer_address: int) -> bytes:
    code = bytearray(b"\x9C\x50\x41\x52\x41\x53")  # flags,rax,r10,r11
    code += b"\x49\xBB" + struct.pack("<Q", buffer_address)
    code += b"\xB8\x01\x00\x00\x00"
    code += b"\xF0\x41\x0F\xC1\x03"  # lock xadd [r11],eax
    code += b"\x3D" + struct.pack("<I", CAPACITY)
    code += b"\x0F\x83\x00\x00\x00\x00"  # jae restore
    skip_disp = len(code) - 4
    code += b"\x48\xC1\xE0\x08"  # RECORD_SIZE = 0x100
    code += b"\x4D\x8D\x54\x03\x20"  # r10 = buffer+header+index*size
    code += b"\x48\x8B\x44\x24\x20\x49\x89\x02"  # caller
    code += b"\x49\x89\x4A\x08"  # submitter/context
    code += b"\x49\x89\x52\x10"  # entry
    code += b"\x4D\x89\x42\x18"  # x / r8
    code += b"\x4D\x89\x4A\x20"  # y / r9
    code += b"\x48\x8B\x41\x58\x49\x89\x42\x28"  # context +0x58
    for source in range(0, ENTRY_SIZE, 8):
        _store_qword(code, source, ENTRY_RECORD_OFFSET + source)
    skip = len(code)
    struct.pack_into(
        "<i", code, skip_disp, code_address + skip - (code_address + skip_disp + 4)
    )
    code += b"\x41\x5B\x41\x5A\x58\x9D"
    code += ORIGINAL
    code += b"\xE9" + rel32(code_address + len(code), 5, RETURN)
    return bytes(code)


def allocate_private_page(process: coop.RemoteProcess) -> int:
    for candidate in coop.cave_allocation_candidates():
        allocation = coop.kernel32.VirtualAllocEx(
            process.handle,
            ct.c_void_p(candidate),
            coop.CAVE_ALLOCATION_SIZE,
            coop.MEM_COMMIT | coop.MEM_RESERVE,
            coop.PAGE_EXECUTE_READWRITE,
        )
        if allocation == candidate:
            return int(candidate)
    raise OSError(ct.get_last_error(), "No private damage-glyph page found")


def free_private_page(process: coop.RemoteProcess, address: int) -> None:
    if address and not coop.kernel32.VirtualFreeEx(
        process.handle, ct.c_void_p(address), 0, MEM_RELEASE
    ):
        raise OSError(
            ct.get_last_error(), f"VirtualFreeEx(damage glyph {address:#x})"
        )


def capture(pid: int, duration: float, destination: Path) -> dict:
    process = coop.RemoteProcess(pid)
    private_base = 0
    installed = False
    capture_error: BaseException | None = None
    raw = b""
    count = 0
    total = 0
    try:
        private_base = allocate_private_page(process)
        buffer_address = private_base + BUFFER_OFFSET
        code_address = private_base + CODE_OFFSET
        current = process.read(HOOK, len(ORIGINAL))
        if current != ORIGINAL:
            raise RuntimeError(f"damage-glyph hook is not pristine: {current.hex(' ')}")
        process.write(
            buffer_address, b"\0" * (HEADER_SIZE + CAPACITY * RECORD_SIZE)
        )
        process.write(code_address, build_hook(code_address, buffer_address))
        patch = b"\xE9" + rel32(HOOK, 5, code_address)
        patch += b"\x90" * (len(ORIGINAL) - len(patch))
        process.patch_code(HOOK, patch)
        installed = True
        time.sleep(max(0.02, duration))
        total = process.u32(buffer_address)
        count = min(total, CAPACITY)
        raw = process.read(buffer_address + HEADER_SIZE, count * RECORD_SIZE)
    except BaseException as error:
        capture_error = error
        raise
    finally:
        if installed:
            process.patch_code(HOOK, ORIGINAL)
        try:
            free_private_page(process, private_base)
        except OSError:
            if capture_error is None:
                process.close()
                raise

    records = []
    for index in range(count):
        block = raw[index * RECORD_SIZE : (index + 1) * RECORD_SIZE]
        caller, context, entry, x, y, context_58 = struct.unpack_from(
            "<6Q", block, 0
        )
        payload = block[ENTRY_RECORD_OFFSET : ENTRY_RECORD_OFFSET + ENTRY_SIZE]
        records.append(
            {
                "index": index,
                "caller": hex(caller),
                "context": hex(context),
                "entry": hex(entry),
                "x": struct.unpack("<q", struct.pack("<Q", x))[0],
                "y": struct.unpack("<q", struct.pack("<Q", y))[0],
                "context_58": hex(context_58),
                "context_clut_5c_before": (context_58 >> 32) & 0xFF,
                "glyph_0a": struct.unpack_from("<h", payload, 0x0A)[0],
                "resource_18": hex(struct.unpack_from("<Q", payload, 0x18)[0]),
                "sprite_table_20": hex(
                    struct.unpack_from("<Q", payload, 0x20)[0]
                ),
                "palette_packed_39": payload[0x39],
                "entry_hex": payload.hex(),
            }
        )
    result = {
        "pid": pid,
        "hook": hex(HOOK),
        "duration_seconds": duration,
        "count": count,
        "total_calls": total,
        "capacity_reached": total >= CAPACITY,
        "records": records,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    process.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--duration", type=float, default=0.25)
    parser.add_argument(
        "--output", type=Path, default=COOP / "native_damage_glyphs.json"
    )
    args = parser.parse_args()
    result = capture(args.pid, args.duration, args.output)
    print(
        f"NATIVE_DAMAGE_GLYPH_CAPTURE output={args.output} "
        f"count={result['count']} full={result['capacity_reached']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
