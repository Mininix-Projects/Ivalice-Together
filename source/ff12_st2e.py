from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


MAGIC = b"st2e"


BYTE_TO_TEXT: dict[int, str] = {
    0x01: "\f",
    0x02: "\n",
    0x04: " ",
    **{0x05 + i: str(i) for i in range(10)},
    **{0x20 + i: chr(ord("A") + i) for i in range(26)},
    **{0x3A + i: chr(ord("a") + i) for i in range(26)},
    0xA2: "/",
    0xA5: "-",
    0xA6: "+",
    0xA7: ",",
    0xA8: ".",
    0xA9: ":",
    0xAA: ";",
    0xAB: "!",
    0xAC: "'",
    0xAD: '"',
    0xAE: "(",
    0xAF: ")",
    0x73: "é",
    0x81: "ú",
    **{0x85 + i: str(i) for i in range(10)},
    0x99: "!",
    0x9A: "?",
    0x9E: "%",
    0xA0: "&",
}


@dataclass(frozen=True)
class TextToken:
    kind: str
    raw: bytes
    text: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "raw": self.raw.hex(), "text": self.text}


def tokenize_ff12_text(raw: bytes) -> Iterator[TextToken]:
    i = 0
    while i < len(raw):
        byte = raw[i]
        if byte == 0x0F:
            command = raw[i : min(i + 4, len(raw))]
            yield TextToken("control", command)
            i += len(command)
            continue
        mapped = BYTE_TO_TEXT.get(byte)
        if mapped is not None:
            kind = "newline" if mapped in {"\n", "\f"} else "text"
            yield TextToken(kind, raw[i : i + 1], mapped)
        else:
            yield TextToken("glyph", raw[i : i + 1], f"<g:{byte:02X}>")
        i += 1


def decode_ff12_text(raw: bytes, *, controls: bool = False) -> str:
    chunks: list[str] = []
    for token in tokenize_ff12_text(raw):
        if token.kind == "control":
            if controls:
                chunks.append(f"<ctl:{token.raw.hex().upper()}>")
        else:
            chunks.append(token.text)
    return "".join(chunks)


@dataclass(frozen=True)
class St2eEntry:
    index: int
    text_id: int
    string_offset: int
    raw: bytes
    record_raw: bytes = b""

    @property
    def text(self) -> str:
        return decode_ff12_text(self.raw)

    @property
    def annotated_text(self) -> str:
        return decode_ff12_text(self.raw, controls=True)

    def as_dict(self, *, source: str | None = None) -> dict[str, object]:
        result: dict[str, object] = {
            "index": self.index,
            "id": self.text_id,
            "id_hex": f"0x{self.text_id:X}",
            "offset": self.string_offset,
            "text": self.text,
            "annotated_text": self.annotated_text,
            "raw_hex": self.raw.hex(),
            "record_hex": self.record_raw.hex(),
        }
        if source is not None:
            result["source"] = source
        return result


@dataclass(frozen=True)
class St2eTable:
    path: Path
    version: int
    entries: tuple[St2eEntry, ...]
    base_offset: int = 0
    entry_stride: int = 4

    @classmethod
    def read(cls, path: str | Path) -> "St2eTable":
        path = Path(path)
        data = path.read_bytes()
        return cls.read_at(path, data, 0)

    @classmethod
    def read_at(cls, path: Path, data: bytes, base_offset: int) -> "St2eTable":
        if base_offset < 0 or base_offset + 0x20 > len(data):
            raise ValueError(f"{path}: invalid st2e base 0x{base_offset:X}")
        if data[base_offset : base_offset + 4] != MAGIC:
            raise ValueError(f"{path}@0x{base_offset:X} is not an st2e table")

        outer_count, entry_stride, outer_header_size = struct.unpack_from(
            "<3I", data, base_offset + 0x04
        )
        section_relative = struct.unpack_from("<I", data, base_offset + 0x14)[0]
        section_offset = base_offset + section_relative
        if not outer_count or outer_count > 1_000_000:
            raise ValueError(f"{path}@0x{base_offset:X}: invalid count {outer_count}")
        if entry_stride < 4 or entry_stride > 0x10000:
            raise ValueError(
                f"{path}@0x{base_offset:X}: invalid entry stride {entry_stride}"
            )
        if outer_header_size < 0x20 or outer_header_size > 0x10000:
            raise ValueError(
                f"{path}@0x{base_offset:X}: invalid header size {outer_header_size}"
            )
        if section_offset + 16 > len(data):
            raise ValueError(f"{path}: invalid string section 0x{section_offset:X}")

        version, header_size, data_size, string_count = struct.unpack_from(
            "<4I", data, section_offset
        )
        if header_size < 12:
            raise ValueError(f"{path}: unexpected string header size {header_size}")
        if string_count != outer_count:
            raise ValueError(
                f"{path}: id count {outer_count} != string count {string_count}"
            )

        records_start = base_offset + outer_header_size
        ids_end = records_start + entry_stride * outer_count
        offsets_start = section_offset + 16
        offsets_end = offsets_start + 4 * string_count
        if ids_end > len(data) or offsets_end > len(data):
            raise ValueError(f"{path}: truncated id/offset table")

        offsets = struct.unpack_from(f"<{string_count}I", data, offsets_start)

        string_base = section_offset + 12
        section_limit = min(len(data), section_offset + header_size + data_size)
        entries: list[St2eEntry] = []
        for index, offset in enumerate(offsets):
            record_start = records_start + index * entry_stride
            record_raw = data[record_start : record_start + entry_stride]
            text_id = struct.unpack_from("<I", record_raw, 0)[0]
            start = string_base + offset
            if start < 0 or start >= len(data) or start >= section_limit:
                raise ValueError(
                    f"{path}: entry {index} points outside section: 0x{offset:X}"
                )
            end = data.find(b"\x00", start, section_limit)
            if end < 0:
                raise ValueError(f"{path}: unterminated string at 0x{start:X}")
            entries.append(
                St2eEntry(index, text_id, offset, data[start:end], record_raw)
            )

        return cls(
            path=path,
            version=version,
            entries=tuple(entries),
            base_offset=base_offset,
            entry_stride=entry_stride,
        )

    @classmethod
    def scan(cls, path: str | Path) -> tuple["St2eTable", ...]:
        path = Path(path)
        data = path.read_bytes()
        tables: list[St2eTable] = []
        cursor = 0
        while True:
            cursor = data.find(MAGIC, cursor)
            if cursor < 0:
                break
            try:
                tables.append(cls.read_at(path, data, cursor))
            except (ValueError, struct.error):
                pass
            cursor += 1
        return tuple(tables)

    def find(self, needle: str) -> list[St2eEntry]:
        folded = needle.casefold()
        return [entry for entry in self.entries if folded in entry.text.casefold()]

    def by_id(self, text_id: int) -> tuple[St2eEntry, ...]:
        return tuple(entry for entry in self.entries if entry.text_id == text_id)

    def as_dict(self) -> dict[str, object]:
        return {
            "source": str(self.path),
            "base_offset": self.base_offset,
            "base_offset_hex": f"0x{self.base_offset:X}",
            "entry_stride": self.entry_stride,
            "version": self.version,
            "count": len(self.entries),
            "entries": [entry.as_dict(source=self.path.name) for entry in self.entries],
        }
