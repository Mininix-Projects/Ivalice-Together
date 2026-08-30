"""Capture short-lived native render-object insertions at ``0x3F1290``.

This diagnostic records only flat fields that are already readable when FF12
links a visual object into one of its native lists.  It uses an independent
remote allocation and restores the exact prologue before freeing that page;
it never borrows the resident FF12COOP allocation.
"""

from __future__ import annotations

import argparse
import json
import struct
import time
from pathlib import Path

import native_sprite_draw_capture as safe_capture


COOP = Path(__file__).resolve().parents[1] / "source"
coop = safe_capture.coop

HOOK = 0x3F1290
RETURN = 0x3F1299
ORIGINAL = bytes.fromhex("33 C0 48 89 82 D0 00 00 00")
BUFFER_OFFSET = 0x1000
CODE_OFFSET = 0xE000
HEADER_SIZE = 0x20
RECORD_SIZE = 0x100
CAPACITY = 192

# (native object offset, flat record offset)
OBJECT_FIELDS = (
    (0x00, 0x20),
    (0x08, 0x28),
    (0x10, 0x30),
    (0x18, 0x38),
    (0x20, 0x40),
    (0x28, 0x48),
    (0x30, 0x50),
    (0x38, 0x58),
    (0x40, 0x60),
    (0x48, 0x68),
    (0x50, 0x70),
    (0x58, 0x78),
    (0x60, 0x80),
    (0x68, 0x88),
    (0x70, 0x90),
    (0x78, 0x98),
    (0x80, 0xA0),
    (0x88, 0xA8),
    (0x90, 0xB0),
    (0x98, 0xB8),
    (0xA0, 0xC0),
    (0xA8, 0xC8),
    (0xB0, 0xD0),
    (0xB8, 0xD8),
    (0xC0, 0xE0),
    (0xC8, 0xE8),
    (0xE0, 0xF0),
)


def _mov_rax_from_rdx(code: bytearray, offset: int) -> None:
    if offset == 0:
        code += b"\x48\x8B\x02"
    elif offset < 0x80:
        code += b"\x48\x8B\x42" + bytes((offset,))
    else:
        code += b"\x48\x8B\x82" + struct.pack("<I", offset)


def _mov_record_from_rax(code: bytearray, offset: int) -> None:
    if offset < 0x80:
        code += b"\x49\x89\x42" + bytes((offset,))
    else:
        code += b"\x49\x89\x82" + struct.pack("<I", offset)


def build_hook(
    code_address: int,
    buffer_address: int,
    return_address: int = RETURN,
    excluded_objects: tuple[int, ...] = (),
) -> bytes:
    code = bytearray()
    code += b"\x9C\x50\x41\x52\x41\x53"  # pushfq,rax,r10,r11
    excluded_fixups: list[int] = []
    for object_pointer in excluded_objects:
        code += b"\x48\xB8" + struct.pack("<Q", object_pointer)
        code += b"\x48\x39\xC2"  # cmp rdx,rax
        code += b"\x0F\x84\x00\x00\x00\x00"  # je skip
        excluded_fixups.append(len(code) - 4)
    code += b"\x49\xBB" + struct.pack("<Q", buffer_address)
    code += b"\xB8\x01\x00\x00\x00"
    code += b"\xF0\x41\x0F\xC1\x03"  # lock xadd [r11],eax
    code += b"\x3D" + struct.pack("<I", CAPACITY)
    code += b"\x0F\x83\x00\x00\x00\x00"  # jae skip
    skip_displacement = len(code) - 4
    code += b"\x48\xC1\xE0\x08"  # index *= RECORD_SIZE
    code += b"\x4D\x8D\x54\x03\x20"  # r10 = buffer+header+index
    code += b"\x48\x8B\x44\x24\x20"  # original return address
    code += b"\x49\x89\x02"
    code += b"\x49\x89\x4A\x08"  # list pointer
    code += b"\x49\x89\x52\x10"  # object pointer
    for source, destination in OBJECT_FIELDS:
        _mov_rax_from_rdx(code, source)
        _mov_record_from_rax(code, destination)
    skip = len(code)
    struct.pack_into(
        "<i",
        code,
        skip_displacement,
        code_address + skip - (code_address + skip_displacement + 4),
    )
    for displacement_at in excluded_fixups:
        struct.pack_into(
            "<i",
            code,
            displacement_at,
            code_address + skip - (code_address + displacement_at + 4),
        )
    code += b"\x41\x5B\x41\x5A\x58\x9D"
    code += ORIGINAL
    code += b"\xE9" + safe_capture.rel32(
        code_address + len(code), 5, return_address
    )
    return bytes(code)


def _hex(value: int) -> str:
    return hex(int(value) & 0xFFFFFFFFFFFFFFFF)


def capture(
    pid: int,
    duration: float,
    destination: Path,
    excluded_objects: tuple[int, ...] = (),
) -> dict:
    process = coop.RemoteProcess(pid)
    private_base = 0
    installed = False
    capture_error: BaseException | None = None
    try:
        private_base = safe_capture.allocate_private_page(process)
        buffer_address = private_base + BUFFER_OFFSET
        code_address = private_base + CODE_OFFSET
        current = process.read(HOOK, len(ORIGINAL))
        if current != ORIGINAL:
            raise RuntimeError(
                f"render-object hook is not pristine: {current.hex(' ')}"
            )
        process.write(
            buffer_address,
            b"\x00" * (HEADER_SIZE + CAPACITY * RECORD_SIZE),
        )
        process.write(
            code_address,
            build_hook(
                code_address,
                buffer_address,
                excluded_objects=excluded_objects,
            ),
        )
        patch = b"\xE9" + safe_capture.rel32(HOOK, 5, code_address)
        patch += b"\x90" * (len(ORIGINAL) - len(patch))
        process.patch_code(HOOK, patch)
        installed = True
        time.sleep(max(0.02, duration))
        total = process.u32(buffer_address)
        count = min(total, CAPACITY)
        raw = process.read(
            buffer_address + HEADER_SIZE,
            count * RECORD_SIZE,
        )
    except BaseException as error:
        capture_error = error
        raise
    finally:
        if installed:
            process.patch_code(HOOK, ORIGINAL)
        try:
            safe_capture.free_private_page(process, private_base)
        except OSError:
            if capture_error is None:
                process.close()
                raise

    records = []
    for index in range(count):
        block = raw[index * RECORD_SIZE : (index + 1) * RECORD_SIZE]
        caller, list_pointer, object_pointer = struct.unpack_from("<3Q", block)
        fields = {
            source: struct.unpack_from("<Q", block, destination)[0]
            for source, destination in OBJECT_FIELDS
        }
        field_10 = fields[0x10]
        color_channels = struct.unpack(
            "<4I", struct.pack("<2Q", fields[0x40], fields[0x48])
        )
        rectangle = struct.unpack(
            "<4f", struct.pack("<2Q", fields[0x50], fields[0x58])
        )
        records.append(
            {
                "index": index,
                "caller": _hex(caller),
                "list": _hex(list_pointer),
                "object": _hex(object_pointer),
                "sprite_index_16": (field_10 >> 48) & 0xFFFF,
                "color_channels_40": list(color_channels),
                "rectangle_50": list(rectangle),
                "resource_e0": _hex(fields[0xE0]),
                "fields": {
                    f"{offset:#04x}": _hex(value)
                    for offset, value in fields.items()
                },
            }
        )
    result = {
        "pid": pid,
        "hook": _hex(HOOK),
        "duration_seconds": duration,
        "count": count,
        "total_insertions": total,
        "capacity_reached": total >= CAPACITY,
        "excluded_objects": [_hex(value) for value in excluded_objects],
        "records": records,
    }
    destination.write_text(
        json.dumps(result, indent=2) + "\n",
        encoding="utf-8",
    )
    process.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--duration", type=float, default=0.25)
    parser.add_argument(
        "--output",
        type=Path,
        default=COOP / "native_render_object_creates.json",
    )
    parser.add_argument(
        "--exclude-object",
        action="append",
        default=[],
        type=lambda value: int(value, 0),
    )
    args = parser.parse_args()
    result = capture(
        args.pid,
        args.duration,
        args.output,
        tuple(args.exclude_object),
    )
    print(
        f"NATIVE_RENDER_OBJECT_CAPTURE output={args.output} "
        f"count={result['count']} full={result['capacity_reached']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
