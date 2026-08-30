"""Resolve native interaction objects from game handles."""

from __future__ import annotations

import struct

import coop_runtime as coop


REGISTRY_BASE = 0x2098E10
INTERACTION_NAME_KEY_OFFSET = 0x102
TREASURE_NAME_KEY = 434


def resolve_handle(process: coop.RemoteProcess, handle: int) -> int:
    registry_index = (handle >> 16) & 0xF
    object_index = handle & 0xFFFF
    generation = (handle >> 20) & 0x7FF
    registry = REGISTRY_BASE + registry_index * 0x288
    objects = process.u64(registry + 8)
    if not objects or object_index >= process.u32(objects):
        return 0
    obj = process.u64(objects + 8 + object_index * 8)
    if not obj:
        return 0
    live_generation = struct.unpack("<H", process.read(obj + 0x16, 2))[0] & 0x7FF
    return obj if live_generation == generation else 0


def is_treasure_object(process: coop.RemoteProcess, obj: int) -> bool:
    if not obj:
        return False
    name_key = struct.unpack(
        "<h",
        process.read(obj + INTERACTION_NAME_KEY_OFFSET, 2),
    )[0]
    return name_key == TREASURE_NAME_KEY


def is_treasure_handle(process: coop.RemoteProcess, handle: int) -> bool:
    return is_treasure_object(process, resolve_handle(process, handle))
