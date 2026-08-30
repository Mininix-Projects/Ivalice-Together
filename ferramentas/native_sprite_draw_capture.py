"""Temporarily log FF12 native UI sprite submissions at 0x2D8D10.

The hook copies only flat call arguments into a private, short-lived remote
allocation.  It deliberately does *not* use the resident FF12COOP allocation:
that page now contains the P2/P3 interaction and target-circle routers, and an
old diagnostic at ``+0x9000/+0xE000`` could overwrite them.  The exact native
prologue is restored before the private page is released in ``finally``.

This is a reverse-engineering capture tool, not a runtime feature.
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
    "ff12coop_sprite_capture", COOP / "coop_runtime.py"
)
coop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coop)

SITES = {
    "sprite": (
        0x2D8D10,
        0x2D8D1A,
        bytes.fromhex("48 83 EC 48 F3 0F 10 44 24 78"),
    ),
    # Lower native quad submit.  Unlike the sprite wrapper, this also sees
    # cached panels and glyph quads.  The ten-byte prologue ends immediately
    # before ``push r15``; executing it in the trampoline preserves r11's
    # required pre-prologue stack value.
    "quad": (
        0x2DB080,
        0x2DB08A,
        bytes.fromhex("4C 8B DC 55 53 56 41 55 41 56"),
    ),
}
BUFFER_OFFSET = 0x1000
CODE_OFFSET = 0xE000
HEADER_SIZE = 0x20
RECORD_SIZE = 0x40
CAPACITY = 304
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


def build_hook(
    code_address: int,
    buffer_address: int,
    original: bytes,
    return_address: int,
    excluded_callers: tuple[int, ...] = (),
) -> bytes:
    code = bytearray()
    code += b"\x9C\x50\x41\x52\x41\x53"  # pushfq, rax, r10, r11
    excluded_disps = []
    for caller in excluded_callers:
        code += b"\x48\xB8" + struct.pack("<Q", caller)
        code += b"\x48\x39\x44\x24\x20"  # cmp [original rsp],rax
        code += b"\x0F\x84\x00\x00\x00\x00"  # je restore
        excluded_disps.append(len(code) - 4)
    code += b"\x49\xBB" + struct.pack("<Q", buffer_address)
    code += b"\xB8\x01\x00\x00\x00"  # eax = 1
    code += b"\xF0\x41\x0F\xC1\x03"  # lock xadd [r11], eax
    code += b"\x3D" + struct.pack("<I", CAPACITY)
    code += b"\x0F\x83\x00\x00\x00\x00"  # jae skip
    skip_disp = len(code) - 4
    code += b"\x48\xC1\xE0\x06"  # rax *= RECORD_SIZE
    code += b"\x4D\x8D\x54\x03\x20"  # r10 = buffer+header+rax
    code += b"\x48\x8B\x44\x24\x20"  # original return address
    code += b"\x49\x89\x02"
    code += b"\x49\x89\x4A\x08"  # rcx
    code += b"\x49\x89\x52\x10"  # rdx
    code += b"\x49\x8B\x00\x49\x89\x42\x18"  # copy [r8] UV
    code += b"\x49\x8B\x01\x49\x89\x42\x20"  # copy [r9] position
    # Windows x64: after the return address, four 8-byte home slots precede
    # the first stack argument.  With our four pushes, caller [rsp+20] is at
    # current [rsp+48].  Keep the pointer value only: it is optional for some
    # sprite paths and must never be dereferenced by a diagnostic hook.
    code += b"\x48\x8B\x44\x24\x48"
    code += b"\x49\x89\x42\x28"
    code += b"\x49\xC7\x42\x30\x00\x00\x00\x00"
    code += b"\x49\xC7\x42\x38\x00\x00\x00\x00"
    skip = len(code)
    for displacement in (skip_disp, *excluded_disps):
        struct.pack_into(
            "<i",
            code,
            displacement,
            code_address + skip - (code_address + displacement + 4),
        )
    code += b"\x41\x5B\x41\x5A\x58\x9D"  # restore r11,r10,rax,flags
    code += original
    code += b"\xE9" + rel32(code_address + len(code), 5, return_address)
    return bytes(code)


def i16x4(value: int) -> list[int]:
    return list(struct.unpack("<4h", struct.pack("<Q", value)))


def allocate_private_page(process: coop.RemoteProcess) -> int:
    """Allocate an unused rel32-reachable page without adopting co-op state."""

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
    raise OSError(ct.get_last_error(), "No private sprite-capture page found")


def free_private_page(process: coop.RemoteProcess, address: int) -> None:
    if address and not coop.kernel32.VirtualFreeEx(
        process.handle,
        ct.c_void_p(address),
        0,
        MEM_RELEASE,
    ):
        raise OSError(
            ct.get_last_error(),
            f"VirtualFreeEx(sprite capture {address:#x})",
        )


def capture(
    pid: int,
    duration: float,
    destination: Path,
    site: str = "sprite",
    excluded_callers: tuple[int, ...] = (),
) -> dict:
    process = coop.RemoteProcess(pid)
    installed = False
    private_base = 0
    capture_error: BaseException | None = None
    hook, return_address, original = SITES[site]
    try:
        private_base = allocate_private_page(process)
        buffer_address = private_base + BUFFER_OFFSET
        code_address = private_base + CODE_OFFSET
        current = process.read(hook, len(original))
        if current != original:
            raise RuntimeError(f"sprite hook is not pristine: {current.hex(' ')}")
        process.write(buffer_address, b"\x00" * (HEADER_SIZE + CAPACITY * RECORD_SIZE))
        hook_code = build_hook(
            code_address,
            buffer_address,
            original,
            return_address,
            excluded_callers=excluded_callers,
        )
        process.write(code_address, hook_code)
        patch = b"\xE9" + rel32(hook, 5, code_address)
        patch += b"\x90" * (len(original) - 5)
        process.patch_code(hook, patch)
        installed = True
        time.sleep(max(0.02, duration))
        count = min(process.u32(buffer_address), CAPACITY)
        raw = process.read(buffer_address + HEADER_SIZE, count * RECORD_SIZE)
        total_submissions = process.u32(buffer_address)
    except BaseException as error:
        capture_error = error
        raise
    finally:
        if installed:
            process.patch_code(hook, original)
        try:
            free_private_page(process, private_base)
        except OSError:
            # Never replace the primary capture/restore failure with cleanup
            # noise.  On success, however, a leaked diagnostic page is an
            # actionable error.
            if capture_error is None:
                process.close()
                raise

    records = []
    for index in range(count):
        values = struct.unpack_from("<8Q", raw, index * RECORD_SIZE)
        caller, rcx, rdx, uv, position, params, param0, param1 = values
        records.append(
            {
                "index": index,
                "caller": hex(caller),
                "rcx": hex(rcx),
                "rdx": hex(rdx),
                "uv": i16x4(uv),
                "position": i16x4(position),
                "params": hex(params),
                "param0": hex(param0),
                "param1": hex(param1),
            }
        )
    result = {
        "pid": pid,
        "site": site,
        "hook": hex(hook),
        "duration_seconds": duration,
        "count": count,
        "total_submissions": total_submissions,
        "capacity_reached": total_submissions >= CAPACITY,
        "excluded_callers": [hex(value) for value in excluded_callers],
        "records": records,
    }
    destination.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    process.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--duration", type=float, default=0.25)
    parser.add_argument("--site", choices=sorted(SITES), default="sprite")
    parser.add_argument(
        "--exclude-caller",
        action="append",
        default=[],
        help="repeatable return address to skip, e.g. 0x2D8D37",
    )
    parser.add_argument("--output", type=Path, default=COOP / "native_sprite_draws.json")
    args = parser.parse_args()
    excluded_callers = tuple(int(value, 0) for value in args.exclude_caller)
    result = capture(
        args.pid,
        args.duration,
        args.output,
        args.site,
        excluded_callers=excluded_callers,
    )
    print(
        f"NATIVE_SPRITE_CAPTURE output={args.output} count={result['count']} "
        f"full={result['capacity_reached']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
