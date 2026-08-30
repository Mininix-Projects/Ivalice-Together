"""Read-only Steam 1.0.4.0 battle-item inventory provider.

The live save layout below was mapped against a running game; the save
base and the 64 packed item counters.  Each counter is a little-endian word:
bits 0..9 are the total quantity and bits 10..15 are the equipped count.
Battle item index 0 maps directly to native action 0x52.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct


SAVE_GAME_BASE_GLOBAL = 0x2EBF190
ITEM_COUNTERS_OFFSET = 0x4748
ITEM_SLOT_COUNT = 0x40
FIRST_ITEM_ACTION_ID = 0x52
TOTAL_COUNT_MASK = 0x03FF


@dataclass(frozen=True)
class LiveInventoryItem:
    index: int
    action_id: int
    quantity: int
    equipped: int = 0


def decode_item_counters(raw: bytes) -> tuple[LiveInventoryItem, ...]:
    expected = ITEM_SLOT_COUNT * 2
    if len(raw) != expected:
        raise ValueError(f"item counter block is {len(raw)} bytes; expected {expected}")
    result: list[LiveInventoryItem] = []
    for index in range(ITEM_SLOT_COUNT):
        packed = struct.unpack_from("<H", raw, index * 2)[0]
        quantity = packed & TOTAL_COUNT_MASK
        if not quantity:
            continue
        result.append(
            LiveInventoryItem(
                index=index,
                action_id=FIRST_ITEM_ACTION_ID + index,
                quantity=quantity,
                equipped=(packed >> 10) & 0x3F,
            )
        )
    return tuple(result)


def read_live_items(process) -> tuple[LiveInventoryItem, ...] | None:
    """Return the current inventory, or ``None`` during an unavailable save."""

    try:
        save_base = process.u64(SAVE_GAME_BASE_GLOBAL)
        if not save_base:
            return None
        raw = process.read(save_base + ITEM_COUNTERS_OFFSET, ITEM_SLOT_COUNT * 2)
    except OSError:
        return None
    return decode_item_counters(raw)


__all__ = [
    "FIRST_ITEM_ACTION_ID",
    "ITEM_COUNTERS_OFFSET",
    "ITEM_SLOT_COUNT",
    "LiveInventoryItem",
    "SAVE_GAME_BASE_GLOBAL",
    "decode_item_counters",
    "read_live_items",
]
