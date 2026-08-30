from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


ITEMS_DISCRIMINATOR = 3
MAX_ENUMERATED_ACTION_ID = 0x115
ENTRY_SIZE = 8


@dataclass(frozen=True)
class Type0BEntry:
    raw_hex: str
    action_id: int
    aux_u16: int
    meta_u16: int
    detail_u8: int
    reserved_u8: int
    discriminator: int | None = None

    @property
    def disabled(self) -> bool:
        return bool(self.meta_u16 & 0x0001)

    @property
    def enabled(self) -> bool:
        return not self.disabled

    @property
    def aux_kind(self) -> str:
        if self.discriminator == ITEMS_DISCRIMINATOR:
            return "inventory_quantity"

        if self.meta_u16 & 0x000A:
            return "native_resource_value"
        return "opaque_or_residual"

    @property
    def inventory_quantity(self) -> int | None:
        return self.aux_u16 if self.aux_kind == "inventory_quantity" else None

    @property
    def resource_value(self) -> int | None:
        return self.aux_u16 if self.aux_kind == "native_resource_value" else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw_hex,
            "action_id": self.action_id,
            "action_id_hex": f"0x{self.action_id:X}",
            "aux_u16": self.aux_u16,
            "aux_kind": self.aux_kind,
            "inventory_quantity": self.inventory_quantity,
            "resource_value": self.resource_value,
            "meta_u16": self.meta_u16,
            "meta_hex": f"0x{self.meta_u16:04X}",
            "detail_u8": self.detail_u8,
            "reserved_u8": self.reserved_u8,
            "disabled": self.disabled,
            "enabled": self.enabled,
            "discriminator": self.discriminator,
        }


def _raw_bytes(value: str | bytes | bytearray | Mapping[str, Any]) -> bytes:
    if isinstance(value, Mapping):
        value = value.get("raw", value.get("raw_hex", value.get("bytes")))
    if isinstance(value, str):
        compact = value.removeprefix("0x").replace(" ", "")
        try:
            raw = bytes.fromhex(compact)
        except ValueError as error:
            raise ValueError("invalid type-0x0B entry hex") from error
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        raise TypeError("entry must be raw bytes, hex, or a mapping containing raw")
    if len(raw) != ENTRY_SIZE:
        raise ValueError(f"type-0x0B entry must be {ENTRY_SIZE} bytes, got {len(raw)}")
    return raw


def decode_type0b_entry(
    value: str | bytes | bytearray | Mapping[str, Any],
    *,
    discriminator: int | None = None,
    strict_action_range: bool = True,
) -> Type0BEntry:
    raw = _raw_bytes(value)
    action_id = int.from_bytes(raw[0:2], "little")
    if strict_action_range and not 0 <= action_id <= MAX_ENUMERATED_ACTION_ID:
        raise ValueError(
            f"action ID 0x{action_id:X} is outside the builder's enumerated "
            f"range 0..0x{MAX_ENUMERATED_ACTION_ID:X}"
        )
    return Type0BEntry(
        raw_hex=raw.hex(),
        action_id=action_id,
        aux_u16=int.from_bytes(raw[2:4], "little"),
        meta_u16=int.from_bytes(raw[4:6], "little"),
        detail_u8=raw[6],
        reserved_u8=raw[7],
        discriminator=discriminator,
    )
