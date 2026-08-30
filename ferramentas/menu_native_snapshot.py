"""Read-only snapshot of FF12's current native P1 command menu.

This tool deliberately does *not* import ``coop_runtime.RemoteProcess``.
Its process handle is opened with only ``PROCESS_VM_READ`` and
``PROCESS_QUERY_LIMITED_INFORMATION``; the class exposes no write/patch API.

The intended experiment is two independent captures, with the character
switch performed normally by the player between them::

    py menu_native_snapshot.py --label vaan
    # press Left in the game and wait for Penelo's menu to settle
    py menu_native_snapshot.py --label penelo

No clone, hook, remote allocation, remote thread, or synthetic input is used.
"""

from __future__ import annotations

import argparse
import ctypes as ct
import hashlib
import json
import os
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROCESS_NAME = "FFXII_TZA.exe"

# Steam 1.0.4.0, image SHA-256
# 73461D0896204B1C259C52F61AEF4EFBC95A2FFC388CADB36493D5D1D89721F8
UI_MANAGER_GLOBAL = 0x209BE80
P1_MENU_OFFSET = 0xAB30
MENU_CALLBACK = 0x279E20
LIST_OWNER_CALLBACK = 0x2778C0
LIST_CONTROLLER_CALLBACK = 0x2D2DB0
LIST_PAGE_CALLBACK = 0x27AD70
DESCRIPTION_RESOLVER = 0x27B880

PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
READ_ONLY_ACCESS_MASK = PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION

MAX_TREE_NODES = 512
MAX_PARENT_DEPTH = 64
DEFAULT_CONTROLLER_RAW_SIZE = 0x180
DEFAULT_RECEIVER_RAW_SIZE = 0x180
DEFAULT_OWNER_RAW_SIZE = 0x200
DESCRIPTOR_PAGE_STRIDE = 0x1620
DESCRIPTOR_IN_PAGE = 0x4C0
DESCRIPTOR_FROM_OWNER = 0xF0 + DESCRIPTOR_IN_PAGE  # 0x5B0
DESCRIPTOR_ENTRY_OFFSET = 0x50
DESCRIPTOR_ENTRY_STRIDE = 8
MAX_DESCRIPTOR_ITEMS = 128


def hex_address(value: int | None) -> str | None:
    return None if value is None else f"0x{value:x}"


def raw_block(address: int, data: bytes) -> dict[str, Any]:
    """Return a comparison-friendly representation of one exact memory read."""
    return {
        "address": hex_address(address),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "hex": data.hex(),
    }


def unpack_from(fmt: str, data: bytes, offset: int) -> Any:
    return struct.unpack_from(fmt, data, offset)[0]


def read_u64(memory: Any, address: int) -> int:
    return struct.unpack("<Q", memory.read(address, 8))[0]


def read_node_header(memory: Any, address: int) -> dict[str, Any]:
    data = memory.read(address, 0x30)
    callback, parent, child, sibling = (
        unpack_from("<Q", data, offset) for offset in (0x00, 0x10, 0x18, 0x20)
    )
    return {
        "address": hex_address(address),
        "callback": hex_address(callback),
        "parent": hex_address(parent),
        "child": hex_address(child),
        "sibling": hex_address(sibling),
        "raw": raw_block(address, data),
        "_callback": callback,
        "_parent": parent,
        "_child": child,
        "_sibling": sibling,
    }


def walk_ui_tree(memory: Any, root: int) -> tuple[list[dict[str, Any]], list[str]]:
    """Walk child/sibling links in native order with cycle and size guards."""
    nodes: list[dict[str, Any]] = []
    errors: list[str] = []
    pending = [root]
    seen: set[int] = set()
    while pending and len(seen) < MAX_TREE_NODES:
        address = pending.pop()
        if not address or address in seen:
            continue
        seen.add(address)
        try:
            node = read_node_header(memory, address)
        except (OSError, ValueError, struct.error) as error:
            errors.append(f"node {address:#x}: {error}")
            continue
        nodes.append(node)
        # Stack is LIFO: sibling first makes the child get visited first.
        if node["_sibling"]:
            pending.append(node["_sibling"])
        if node["_child"]:
            pending.append(node["_child"])
    if pending:
        errors.append(f"tree stopped at safety limit {MAX_TREE_NODES}")
    return nodes, errors


def public_node(node: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in node.items() if not key.startswith("_")}


def find_list_owner(
    memory: Any, controller: int
) -> tuple[int | None, list[dict[str, Any]], list[str]]:
    """Ascend +0x10 until the 0x2778C0 command-list receiver is found."""
    address = controller
    seen: set[int] = set()
    chain: list[dict[str, Any]] = []
    errors: list[str] = []
    while address and address not in seen and len(seen) < MAX_PARENT_DEPTH:
        seen.add(address)
        try:
            node = read_node_header(memory, address)
        except (OSError, ValueError, struct.error) as error:
            errors.append(f"parent node {address:#x}: {error}")
            break
        chain.append(public_node(node))
        if node["_callback"] == LIST_OWNER_CALLBACK:
            return address, chain, errors
        address = node["_parent"]
    if address in seen:
        errors.append("parent chain contains a cycle")
    elif len(seen) >= MAX_PARENT_DEPTH:
        errors.append(f"parent chain stopped at safety limit {MAX_PARENT_DEPTH}")
    else:
        errors.append("0x2778C0 list owner was not found")
    return None, chain, errors


def decode_controller(data: bytes) -> dict[str, Any]:
    if len(data) < 0x108:
        raise ValueError("controller block must include offsets through +0x107")
    count = unpack_from("<H", data, 0xE8)
    columns = data[0xEC]
    row = unpack_from("<b", data, 0xED)
    column = unpack_from("<b", data, 0xEE)
    scroll_row = unpack_from("<h", data, 0xF2)
    scroll_column = unpack_from("<h", data, 0xF4)
    selected = None
    if columns:
        selected = (scroll_row + scroll_column) * columns + row + column
    return {
        "count": count,
        "selected": selected,
        "selected_in_range": bool(
            selected is not None and 0 <= selected < count
        ),
        "geometry": {
            "columns": columns,
            "row": row,
            "column": column,
            "scroll_row": scroll_row,
            "scroll_column": scroll_column,
            "u16_ea": unpack_from("<H", data, 0xEA),
            "u16_f0": unpack_from("<H", data, 0xF0),
            "u16_100": unpack_from("<H", data, 0x100),
            "byte_103": data[0x103],
            "byte_106": data[0x106],
        },
        "flags": {
            "u32_e0": unpack_from("<I", data, 0xE0),
            "u32_e4": unpack_from("<I", data, 0xE4),
        },
        "content": hex_address(unpack_from("<Q", data, 0xC0)),
        "receiver": hex_address(unpack_from("<Q", data, 0xC8)),
        "aux_d0": hex_address(unpack_from("<Q", data, 0xD0)),
        "_receiver": unpack_from("<Q", data, 0xC8),
    }


def descriptor_read_size(count: int) -> int:
    if not 0 < count <= MAX_DESCRIPTOR_ITEMS:
        return 0x100
    return min(0x500, max(0x100, DESCRIPTOR_ENTRY_OFFSET + count * 8))


def decode_descriptor(data: bytes, count: int, selected: int | None) -> dict[str, Any]:
    if len(data) < 0x50:
        raise ValueError("descriptor block is shorter than its 0x50-byte header")
    descriptor_count = unpack_from('<I', data, 0x40)
    result: dict[str, Any] = {
        "type_i32": unpack_from("<i", data, 0x00),
        "type_u32": unpack_from("<I", data, 0x00),
        "u32_08": unpack_from("<I", data, 0x08),
        "u16_1c": unpack_from("<H", data, 0x1C),
        "ready_u32_40": unpack_from("<I", data, 0x40),
        "entry_layout": "+0x50, stride 8",
        "entries": [],
    }
    result.update({
        'descriptor_count_u32_40': descriptor_count,
        'controller_count': count,
        'count_matches_controller': descriptor_count == count,
    })
    available = max(0, (len(data) - DESCRIPTOR_ENTRY_OFFSET) // 8)
    declared_count = descriptor_count if 0 <= descriptor_count <= 128 else count
    item_count = (
        min(declared_count, available, MAX_DESCRIPTOR_ITEMS)
        if declared_count > 0
        else 0
    )
    for index in range(item_count):
        offset = DESCRIPTOR_ENTRY_OFFSET + index * DESCRIPTOR_ENTRY_STRIDE
        entry = data[offset : offset + DESCRIPTOR_ENTRY_STRIDE]
        result["entries"].append({
            "index": index,
            "selected": index == selected,
            "u16_50": unpack_from("<H", entry, 0),
            "u32_50": unpack_from("<I", entry, 0),
            "flags_53": entry[3],
            "value_54": entry[4],
            "value_56": entry[6],
            "raw": entry.hex(),
        })
        decoded_entry = result['entries'][-1]
        decoded_entry.update({
            'u64_50': hex_address(unpack_from('<Q', entry, 0)),
            'bytes_53_57': list(entry[3:8]),
        })
    if selected is not None and 0 <= selected < item_count:
        result["selected_entry"] = result["entries"][selected]
    else:
        result["selected_entry"] = None
    return result


def snapshot_descriptor_candidate(
    memory: Any,
    address: int,
    source: str,
    page: int,
    count: int,
    selected: int | None,
    is_current_page: bool,
) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "address": hex_address(address),
        "source": source,
        "page": page,
        "current_page": is_current_page,
        "resolver": hex_address(DESCRIPTION_RESOLVER),
        "resolver_index_candidate": selected,
        "derivation": (
            "owner + 0x5b0 + page*0x1620; observed at callsite 0x278993"
        ),
    }
    try:
        header = memory.read(address, 0x50)
        live_count = unpack_from('<I', header, 0x40)
        data = memory.read(address, descriptor_read_size(live_count))
        candidate['header_raw'] = raw_block(address, data[:0x50])
        candidate["decoded"] = decode_descriptor(data, count, selected)
        candidate["raw"] = raw_block(address, data)
    except (OSError, ValueError, struct.error) as error:
        candidate["error"] = str(error)
    return candidate


def snapshot_controller(
    memory: Any,
    address: int,
    controller_raw_size: int = DEFAULT_CONTROLLER_RAW_SIZE,
    receiver_raw_size: int = DEFAULT_RECEIVER_RAW_SIZE,
    owner_raw_size: int = DEFAULT_OWNER_RAW_SIZE,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "address": hex_address(address),
        "callback": hex_address(LIST_CONTROLLER_CALLBACK),
        "errors": [],
    }
    try:
        data = memory.read(address, max(controller_raw_size, 0x108))
        decoded = decode_controller(data)
        record.update({key: value for key, value in decoded.items() if not key.startswith("_")})
        record["raw"] = raw_block(address, data)
    except (OSError, ValueError, struct.error) as error:
        record["errors"].append(f"controller: {error}")
        return record

    receiver = decoded["_receiver"]
    receiver_record: dict[str, Any] = {"address": hex_address(receiver)}
    if receiver:
        try:
            receiver_data = memory.read(receiver, receiver_raw_size)
            receiver_record["callback"] = hex_address(unpack_from("<Q", receiver_data, 0))
            receiver_record["raw"] = raw_block(receiver, receiver_data)
        except (OSError, ValueError, struct.error) as error:
            receiver_record["error"] = str(error)
    record["receiver_object"] = receiver_record

    if receiver:
        try:
            receiver_callback = read_u64(memory, receiver)
            controller_slot = read_u64(memory, receiver + 0x1510)
            receiver_record.update({
                'callback_expected': hex_address(LIST_PAGE_CALLBACK),
                'callback_matches': receiver_callback == LIST_PAGE_CALLBACK,
                'controller_slot_1510': hex_address(controller_slot),
                'controller_slot_matches': controller_slot == address,
            })
        except (OSError, ValueError, struct.error) as error:
            record['errors'].append(f'receiver validation: {error}')

    owner, chain, owner_errors = find_list_owner(memory, address)
    record["owner"] = hex_address(owner)
    record["parent_chain"] = chain
    record["errors"].extend(owner_errors)
    record["descriptor_candidates"] = []
    if receiver:
        primary = snapshot_descriptor_candidate(
            memory,
            receiver + DESCRIPTOR_IN_PAGE,
            'controller+0xc8 receiver + 0x4c0',
            -1,
            decoded['count'],
            decoded['selected'],
            True,
        )
        primary['derivation'] = (
            'controller+0xc8 is active page/context; callsite 0x27898c '
            'passes receiver+0x4c0 to 0x27b880'
        )
        record['descriptor_candidates'].append(primary)

    # Parent ascent is diagnostic only. The primary descriptor route above
    # remains valid when 0x2778c0 is not exposed in the controller ancestry.
    if owner is not None:
        try:
            owner_data = memory.read(owner, max(owner_raw_size, 0xE0))
            page = unpack_from("<h", owner_data, 0xDA)
            record["owner_state"] = {
                "descriptor_page_s16_da": page,
                "raw": raw_block(owner, owner_data),
            }
            if page in (0, 1):
                for candidate_page, source, current in (
                    (page, "0x27b880_current_page", True),
                    (page ^ 1, "double_buffer_peer", False),
                ):
                    descriptor = (
                        owner
                        + DESCRIPTOR_FROM_OWNER
                        + candidate_page * DESCRIPTOR_PAGE_STRIDE
                    )
                    record["descriptor_candidates"].append(
                        snapshot_descriptor_candidate(
                            memory,
                            descriptor,
                            source,
                            candidate_page,
                            decoded["count"],
                            decoded["selected"],
                            current,
                        )
                    )
            else:
                record["errors"].append(
                    f"owner+0xda page is outside proven double buffer: {page}"
                )
        except (OSError, ValueError, struct.error) as error:
            record["errors"].append(f"owner: {error}")
    return record


def capture_snapshot(
    memory: Any,
    pid: int,
    label: str,
    controller_raw_size: int = DEFAULT_CONTROLLER_RAW_SIZE,
    receiver_raw_size: int = DEFAULT_RECEIVER_RAW_SIZE,
    owner_raw_size: int = DEFAULT_OWNER_RAW_SIZE,
) -> dict[str, Any]:
    manager = read_u64(memory, UI_MANAGER_GLOBAL)
    if not manager:
        raise RuntimeError(f"UI manager global {UI_MANAGER_GLOBAL:#x} is null")
    menu = manager + P1_MENU_OFFSET
    callback = read_u64(memory, menu)
    if callback != MENU_CALLBACK:
        raise RuntimeError(
            f"P1 menu {menu:#x} is not live: callback={callback:#x}, "
            f"expected={MENU_CALLBACK:#x}"
        )
    root = read_u64(memory, menu + 0x18)
    if not root:
        raise RuntimeError(f"P1 menu {menu:#x} has no UI tree")

    # Capture the complete native object so Vaan/Penelo can be byte-diffed.
    menu_data = memory.read(menu, 0x3200)
    nodes, tree_errors = walk_ui_tree(memory, root)
    controller_addresses = [
        node["_address"] if "_address" in node else int(node["address"], 16)
        for node in nodes
        if node["_callback"] == LIST_CONTROLLER_CALLBACK
    ]
    controllers = [
        snapshot_controller(
            memory,
            address,
            controller_raw_size,
            receiver_raw_size,
            owner_raw_size,
        )
        for address in controller_addresses
    ]

    signatures = {}
    for name, address, size in (
        ("menu_callback", MENU_CALLBACK, 5),
        ("list_owner_callback", LIST_OWNER_CALLBACK, 5),
        ("list_controller_callback", LIST_CONTROLLER_CALLBACK, 5),
        ("description_resolver", DESCRIPTION_RESOLVER, 5),
    ):
        try:
            signatures[name] = raw_block(address, memory.read(address, size))
        except OSError as error:
            signatures[name] = {"address": hex_address(address), "error": str(error)}

    return {
        "schema": "ff12.native-menu-snapshot.v1",
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "process": {
            "pid": pid,
            "name": PROCESS_NAME,
            "access_mask": hex_address(READ_ONLY_ACCESS_MASK),
            "access": ["PROCESS_VM_READ", "PROCESS_QUERY_LIMITED_INFORMATION"],
            "write_api_exposed": False,
        },
        "build": {
            "image_sha256_expected": (
                "73461d0896204b1c259c52f61aef4efbc95a2ffc388cadb36493d5d1d89721f8"
            ),
            "addresses": {
                "ui_manager_global": hex_address(UI_MANAGER_GLOBAL),
                "p1_menu_offset": hex_address(P1_MENU_OFFSET),
                "menu_callback": hex_address(MENU_CALLBACK),
                "list_owner_callback": hex_address(LIST_OWNER_CALLBACK),
                "list_controller_callback": hex_address(LIST_CONTROLLER_CALLBACK),
                "description_resolver": hex_address(DESCRIPTION_RESOLVER),
            },
            "live_signatures": signatures,
        },
        "manager": {
            "global": hex_address(UI_MANAGER_GLOBAL),
            "address": hex_address(manager),
        },
        "menu": {
            "address": hex_address(menu),
            "callback": hex_address(callback),
            "tree_root": hex_address(root),
            "raw": raw_block(menu, menu_data),
        },
        "tree": {
            "node_count": len(nodes),
            "errors": tree_errors,
            "nodes": [public_node(node) for node in nodes],
        },
        "controllers": controllers,
        "correlation": {
            "status": "static callsite, not a runtime hook",
            "callsite": hex_address(0x278993),
            "resolver": hex_address(DESCRIPTION_RESOLVER),
            "descriptor_argument": "owner+0x5b0+(s16(owner+0xda)*0x1620)",
            "index_argument": "event qword +0x10; normal selection matches controller selected",
            "evidence": "re_root_selection_278302.txt / re_selection_resolve_27b880.txt",
        },
    }


class ReadOnlyProcess:
    """Minimal Win32 process reader with no mutation methods or rights."""

    def __init__(self, pid: int):
        if os.name != "nt":
            raise OSError("live snapshots require Windows")
        from ctypes import wintypes as wt

        self.pid = pid
        self._kernel32 = ct.WinDLL("kernel32", use_last_error=True)
        self._kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        self._kernel32.OpenProcess.restype = wt.HANDLE
        self._kernel32.ReadProcessMemory.argtypes = [
            wt.HANDLE,
            ct.c_void_p,
            ct.c_void_p,
            ct.c_size_t,
            ct.POINTER(ct.c_size_t),
        ]
        self._kernel32.ReadProcessMemory.restype = wt.BOOL
        self._kernel32.CloseHandle.argtypes = [wt.HANDLE]
        self.handle = self._kernel32.OpenProcess(READ_ONLY_ACCESS_MASK, False, pid)
        if not self.handle:
            raise OSError(ct.get_last_error(), f"OpenProcess({pid})")

    def read(self, address: int, size: int) -> bytes:
        if size < 0:
            raise ValueError("negative read size")
        buffer = (ct.c_ubyte * size)()
        transferred = ct.c_size_t()
        ok = self._kernel32.ReadProcessMemory(
            self.handle,
            ct.c_void_p(address),
            buffer,
            size,
            ct.byref(transferred),
        )
        if not ok or transferred.value != size:
            raise OSError(
                ct.get_last_error(),
                f"ReadProcessMemory({address:#x}, {size:#x})",
            )
        return bytes(buffer)

    def close(self) -> None:
        if self.handle:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> "ReadOnlyProcess":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def find_pid(process_name: str = PROCESS_NAME) -> int:
    if os.name != "nt":
        raise OSError("process discovery requires Windows")
    from ctypes import wintypes as wt

    kernel32 = ct.WinDLL("kernel32", use_last_error=True)
    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ct.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD),
            ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ct.c_size_t),
            ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", ct.c_long),
            ("dwFlags", wt.DWORD),
            ("szExeFile", wt.WCHAR * 260),
        ]

    kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    kernel32.Process32FirstW.argtypes = [wt.HANDLE, ct.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wt.BOOL
    kernel32.Process32NextW.argtypes = [wt.HANDLE, ct.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wt.BOOL
    kernel32.CloseHandle.argtypes = [wt.HANDLE]

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == wt.HANDLE(-1).value:
        raise OSError(ct.get_last_error(), "CreateToolhelp32Snapshot")
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ct.sizeof(entry)
        ok = kernel32.Process32FirstW(snapshot, ct.byref(entry))
        while ok:
            if entry.szExeFile.lower() == process_name.lower():
                return int(entry.th32ProcessID)
            ok = kernel32.Process32NextW(snapshot, ct.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    raise RuntimeError(f"{process_name} is not running")


def default_output(label: str) -> Path:
    safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    return Path(__file__).resolve().parents[1] / f"native_menu_{safe_label}_{stamp}.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Snapshot FF12's current native P1 menu using reads only."
    )
    parser.add_argument("--label", default="current", help="capture label, e.g. vaan/penelo")
    parser.add_argument("--pid", type=int, help="FFXII_TZA PID (auto-detected by default)")
    parser.add_argument("--output", type=Path, help="JSON destination")
    parser.add_argument(
        "--controller-raw-size",
        type=lambda value: int(value, 0),
        default=DEFAULT_CONTROLLER_RAW_SIZE,
    )
    parser.add_argument(
        "--receiver-raw-size",
        type=lambda value: int(value, 0),
        default=DEFAULT_RECEIVER_RAW_SIZE,
    )
    parser.add_argument(
        "--owner-raw-size",
        type=lambda value: int(value, 0),
        default=DEFAULT_OWNER_RAW_SIZE,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pid = args.pid if args.pid is not None else find_pid()
    destination = args.output or default_output(args.label)
    with ReadOnlyProcess(pid) as process:
        snapshot = capture_snapshot(
            process,
            pid,
            args.label,
            args.controller_raw_size,
            args.receiver_raw_size,
            args.owner_raw_size,
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(
        f"READ_ONLY_NATIVE_MENU_SNAPSHOT output={destination} "
        f"controllers={len(snapshot['controllers'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
