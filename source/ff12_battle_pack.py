from __future__ import annotations

import argparse
import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ff12_st2e import decode_ff12_text


TEXT_DIRECTORY_INDEX = 61
GROUP_NAMES = (
    "actions",
    "equipment",
    "dummy",
    "abilities",
    "command_categories",
    "statuses",
    "target_rules",
    "reserved",
    "actors",
    "bazaar_goods",
    "passives",
    "inventory",
    "locations",
    "species",
    "families",
)
GLYPH_MARKER = re.compile(r"<g:[0-9A-F]{2}>", re.IGNORECASE)


@dataclass(frozen=True)
class BattleTextRecord:
    group: int
    index: int
    flags: int
    text: str
    raw: bytes
    suffix: bytes

    @property
    def clean_text(self) -> str:
        return GLYPH_MARKER.sub("", self.text).strip()

    def as_dict(self) -> dict[str, object]:
        return {
            "group": self.group,
            "group_name": GROUP_NAMES[self.group],
            "index": self.index,
            "index_hex": f"0x{self.index:X}",
            "flags": self.flags,
            "text": self.text,
            "clean_text": self.clean_text,
            "raw_hex": self.raw.hex(),
            "suffix_hex": self.suffix.hex(),
        }


@dataclass(frozen=True)
class BattleTextPack:
    path: Path
    groups: tuple[tuple[BattleTextRecord, ...], ...]
    directory_offsets: tuple[int, ...]
    text_section_offset: int

    @classmethod
    def read(cls, path: str | Path) -> "BattleTextPack":
        path = Path(path)
        data = path.read_bytes()
        if len(data) < 8:
            raise ValueError(f"truncated battle pack: {path}")
        directory_count = struct.unpack_from("<I", data, 0)[0]
        if directory_count <= TEXT_DIRECTORY_INDEX:
            raise ValueError(
                f"battle pack has only {directory_count} directory entries"
            )
        directory_end = 4 + 4 * directory_count
        if directory_end > len(data):
            raise ValueError("truncated battle-pack directory")
        directory = struct.unpack_from(f"<{directory_count}I", data, 4)
        text_base = directory[TEXT_DIRECTORY_INDEX]
        text_limit = (
            directory[TEXT_DIRECTORY_INDEX + 1]
            if TEXT_DIRECTORY_INDEX + 1 < directory_count
            else len(data)
        )
        if text_base + 8 > len(data) or text_limit > len(data):
            raise ValueError("invalid battle text section")

        group_count, header_size = struct.unpack_from("<II", data, text_base)
        if group_count != len(GROUP_NAMES):
            raise ValueError(
                f"expected {len(GROUP_NAMES)} text groups, got {group_count}"
            )
        if header_size != 8 + group_count * 4:
            raise ValueError(f"unexpected battle text header size 0x{header_size:X}")
        relative_ends = struct.unpack_from(f"<{group_count}I", data, text_base + 8)
        if text_base + relative_ends[-1] > text_limit:
            raise ValueError("battle text groups exceed their directory section")

        groups: list[tuple[BattleTextRecord, ...]] = []
        relative_start = header_size
        for group_index, relative_end in enumerate(relative_ends):
            group_start = text_base + relative_start
            group_end = text_base + relative_end
            if group_start + 4 > group_end:
                raise ValueError(f"truncated text group {group_index}")
            count = struct.unpack_from("<I", data, group_start)[0]
            offset_table_end = group_start + 4 + 4 * (count + 1)
            if offset_table_end > group_end:
                raise ValueError(f"truncated offset table in group {group_index}")
            offsets = struct.unpack_from(f"<{count + 1}I", data, group_start + 4)
            if (
                offsets[0] < 4 + 4 * (count + 1)
                or offsets[-1] != group_end - group_start
            ):
                raise ValueError(f"invalid string offsets in group {group_index}")
            if any(right < left for left, right in zip(offsets, offsets[1:])):
                raise ValueError(f"non-monotonic offsets in group {group_index}")

            records: list[BattleTextRecord] = []
            for index, (left, right) in enumerate(zip(offsets, offsets[1:])):
                raw = data[group_start + left : group_start + right]
                if len(raw) < 3:
                    raise ValueError(f"short text record {group_index}:{index}")
                flags = struct.unpack_from("<H", raw, 0)[0]
                terminator = raw.find(b"\x00", 2)
                if terminator < 0:
                    terminator = len(raw)
                encoded = raw[2:terminator]
                suffix = raw[terminator + 1 :] if terminator < len(raw) else b""
                records.append(
                    BattleTextRecord(
                        group=group_index,
                        index=index,
                        flags=flags,
                        text=decode_ff12_text(encoded),
                        raw=raw,
                        suffix=suffix,
                    )
                )
            groups.append(tuple(records))
            relative_start = relative_end

        return cls(
            path=path,
            groups=tuple(groups),
            directory_offsets=tuple(directory),
            text_section_offset=text_base,
        )

    def group(self, name_or_index: str | int) -> tuple[BattleTextRecord, ...]:
        if isinstance(name_or_index, str):
            try:
                index = GROUP_NAMES.index(name_or_index)
            except ValueError as error:
                raise KeyError(name_or_index) from error
        else:
            index = name_or_index
        return self.groups[index]

    def lookup(self, group: str | int, index: int, *, clean: bool = True) -> str:
        records = self.group(group)
        if index < 0 or index >= len(records):
            return f"Unknown 0x{index:X}"
        record = records[index]
        return record.clean_text if clean else record.text

    def action_name(self, action_id: int) -> str:
        return self.lookup("actions", action_id)

    def actor_name(self, name_index: int, instance_suffix: str = "") -> str:
        base = self.lookup("actors", name_index)
        suffix = instance_suffix.strip()
        return f"{base} {suffix}" if suffix else base

    def find(self, needle: str) -> list[BattleTextRecord]:
        folded = needle.casefold()
        return [
            record
            for group in self.groups
            for record in group
            if folded in record.clean_text.casefold()
        ]

    def as_dict(self) -> dict[str, object]:
        return {
            "format": "ff12-battle-text-pack-v1",
            "source": str(self.path.resolve()),
            "text_section_offset": self.text_section_offset,
            "text_section_offset_hex": f"0x{self.text_section_offset:X}",
            "groups": [
                {
                    "index": index,
                    "name": GROUP_NAMES[index],
                    "count": len(records),
                    "entries": [record.as_dict() for record in records],
                }
                for index, records in enumerate(self.groups)
            ],
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("battle_pack", type=Path)
    parser.add_argument("--find")
    parser.add_argument("--action", type=lambda value: int(value, 0))
    parser.add_argument("--actor", type=lambda value: int(value, 0))
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    pack = BattleTextPack.read(args.battle_pack)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(pack.as_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    if args.action is not None:
        print(pack.action_name(args.action))
    if args.actor is not None:
        print(pack.actor_name(args.actor))
    if args.find:
        for record in pack.find(args.find):
            print(
                f"{GROUP_NAMES[record.group]}\t0x{record.index:X}\t{record.clean_text}"
            )
    if not any((args.json, args.find, args.action is not None, args.actor is not None)):
        for index, records in enumerate(pack.groups):
            print(f"{index:02d} {GROUP_NAMES[index]:20s} {len(records):4d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
