"""Pointer-free catalog snapshots produced from FF12 native menu descriptors.

The native command-menu builder (``0x31EB20`` in Steam 1.0.4.0) emits a
descriptor whose entry array starts at ``+0x50`` and has an eight-byte stride.
Those bytes are valuable evidence, but they are *not* a safe object to retain:
the surrounding native menu is a singleton and is rebuilt/destroyed often.

This module is deliberately offline.  It accepts copied JSON, validates and
normalizes it into immutable, pointer-free records, and provides an explicit
adapter seam for :class:`headless_menu_model.P2MenuModel`.  It does not attach
to FF12, inject code, or retain native addresses.

Important distinctions preserved by the format:

* ``visible`` says whether native exposes the row/category;
* ``enabled`` says whether the exposed row can be selected;
* ``action_id`` is populated only when the trace or proven descriptor semantics
  state it explicitly.  In generic/root entries the first u16 remains only a
  ``dialog_index``; in native type ``0x0B`` lists it is proven to be the final
  action ID consumed by ``0x311CD0``.

Unknown menu types and flag bytes are retained so later RE can reinterpret a
capture without having to repeat it.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # co-op modules


from headless_menu_model import (
    ActionDefinition,
    EntryKind,
    MenuEntry,
    PageDefinition,
)


NORMALIZED_SCHEMA = "ff12-native-menu-catalog-snapshot-v1"
ENTRY_STRIDE = 8

_TYPE_KEYS = ("type", "type_id", "type_u32", "type_i32", "menu_type")
_COUNT_KEYS = (
    "count",
    "declared_count",
    "descriptor_count",
    "descriptor_count_u32_40",
    "ready_u32_40",
)
_ENTRY_KEYS = ("entries", "entries_raw", "raw_entries")
_POINTER_TOKENS = (
    "address",
    "pointer",
    "_ptr",
    "ptr_",
    "receiver",
    "owner",
    "controller",
    "vtable",
)


def _first(mapping: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _integer(value: Any, field_name: str, *, allow_none: bool = False) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer, not bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError as error:
            raise ValueError(f"invalid {field_name}: {value!r}") from error
    raise ValueError(f"invalid {field_name}: {value!r}")


def _optional_bool(mapping: Mapping[str, Any], *keys: str) -> bool | None:
    value = _first(mapping, keys)
    if value is None:
        return None
    if type(value) is not bool:  # Intentionally reject 0/1: provenance matters.
        raise ValueError(f"{keys[0]} must be a JSON boolean or null")
    return value


def _is_pointer_key(key: str) -> bool:
    lowered = key.casefold()
    return any(token in lowered for token in _POINTER_TOKENS)


def _safe_json(value: Any) -> Any:
    """Copy JSON-compatible evidence while removing pointer-labelled fields."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_json(item) for item in value]
    if isinstance(value, Mapping):
        return {
            str(key): _safe_json(item)
            for key, item in value.items()
            if not _is_pointer_key(str(key))
        }
    raise ValueError(f"value is not JSON-compatible: {value!r}")


def _looks_like_unknown_flag(key: str) -> bool:
    lowered = key.casefold()
    if _is_pointer_key(lowered) or lowered.startswith("u64"):
        return False
    return (
        "flag" in lowered
        or "mask" in lowered
        or "unknown" in lowered
        or "reserved" in lowered
        or lowered.startswith(("u8", "u16", "u32", "i8", "i16", "i32"))
        or lowered.startswith(("byte_", "bytes_", "value_", "ready_"))
    )


def _unknown_flags(mapping: Mapping[str, Any], known: set[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    nested = mapping.get("unknown_flags")
    if isinstance(nested, Mapping):
        result.update(_safe_json(nested))
    nested = mapping.get("flags")
    if isinstance(nested, Mapping):
        result.setdefault("flags", _safe_json(nested))
    for key, value in mapping.items():
        if key not in known and _looks_like_unknown_flag(key):
            result[key] = _safe_json(value)
    return result


def _raw_entry_bytes(value: Any, ordinal: int) -> bytes:
    if isinstance(value, Mapping):
        value = _first(value, ("raw", "raw_hex", "bytes"))
    if isinstance(value, str):
        compact = value.removeprefix("0x").replace(" ", "")
        try:
            raw = bytes.fromhex(compact)
        except ValueError as error:
            raise ValueError(f"entry {ordinal} has invalid hex bytes") from error
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    elif isinstance(value, list):
        try:
            raw = bytes(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"entry {ordinal} has invalid byte list") from error
    else:
        raise ValueError(f"entry {ordinal} has no raw eight-byte payload")
    if len(raw) != ENTRY_STRIDE:
        raise ValueError(
            f"entry {ordinal} must be exactly {ENTRY_STRIDE} bytes, got {len(raw)}"
        )
    return raw


@dataclass(frozen=True)
class NativeEntrySnapshot:
    ordinal: int
    raw_hex: str
    dialog_index: int
    visible: bool | None = None
    enabled: bool | None = None
    action_id: int | None = None
    discriminator: Any = None
    unknown_flags: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "raw": self.raw_hex,
            "dialog_index": self.dialog_index,
            "visible": self.visible,
            "enabled": self.enabled,
            "action_id": self.action_id,
            "discriminator": _safe_json(self.discriminator),
            "unknown_flags": _safe_json(self.unknown_flags),
        }


@dataclass(frozen=True)
class NativeDescriptorSnapshot:
    type_id: int | None
    mapped_type: str | None
    declared_count: int
    entries: tuple[NativeEntrySnapshot, ...]
    visible: bool | None = None
    enabled: bool | None = None
    discriminator: Any = None
    unknown_flags: Mapping[str, Any] = field(default_factory=dict)
    capture_path: str = "descriptor"

    @property
    def complete(self) -> bool:
        return len(self.entries) == self.declared_count

    @property
    def count_delta(self) -> int:
        return len(self.entries) - self.declared_count

    def as_dict(self) -> dict[str, Any]:
        return {
            "type_id": self.type_id,
            "mapped_type": self.mapped_type,
            "declared_count": self.declared_count,
            "captured_count": len(self.entries),
            "complete": self.complete,
            "count_delta": self.count_delta,
            "visible": self.visible,
            "enabled": self.enabled,
            "discriminator": _safe_json(self.discriminator),
            "unknown_flags": _safe_json(self.unknown_flags),
            "capture_path": self.capture_path,
            "entries": [entry.as_dict() for entry in self.entries],
        }


@dataclass(frozen=True)
class NativeCatalogSnapshot:
    descriptors: tuple[NativeDescriptorSnapshot, ...]
    label: str | None = None
    source_schema: str | None = None
    captured_utc: str | None = None
    generation: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": NORMALIZED_SCHEMA,
            "label": self.label,
            "source_schema": self.source_schema,
            "captured_utc": self.captured_utc,
            "generation": self.generation,
            "descriptor_count": len(self.descriptors),
            "descriptors": [descriptor.as_dict() for descriptor in self.descriptors],
        }


def parse_entry(payload: Any, ordinal: int) -> NativeEntrySnapshot:
    mapping: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
    raw = _raw_entry_bytes(payload, ordinal)
    explicit_action = _first(mapping, ("action_id", "actionId"))
    action_id = _integer(explicit_action, "action_id", allow_none=True)
    known = {
        "index",
        "ordinal",
        "raw",
        "raw_hex",
        "bytes",
        "dialog_index",
        "action_id",
        "actionId",
        "visible",
        "is_visible",
        "enabled",
        "is_enabled",
        "discriminator",
        "unknown_flags",
        "flags",
        "selected",
        "u64_50",
    }
    return NativeEntrySnapshot(
        ordinal=ordinal,
        raw_hex=raw.hex(),
        dialog_index=int.from_bytes(raw[:2], "little"),
        visible=_optional_bool(mapping, "visible", "is_visible"),
        enabled=_optional_bool(mapping, "enabled", "is_enabled"),
        action_id=action_id,
        discriminator=_safe_json(mapping.get("discriminator")),
        unknown_flags=_unknown_flags(mapping, known),
    )


def parse_descriptor(
    payload: Mapping[str, Any],
    *,
    capture_path: str = "descriptor",
    type_map: Mapping[int, str] | None = None,
) -> NativeDescriptorSnapshot:
    type_id = _integer(_first(payload, _TYPE_KEYS), "type", allow_none=True)
    count_value = _first(payload, _COUNT_KEYS)
    if count_value is None:
        raise ValueError(f"{capture_path}: descriptor has no count")
    declared_count = _integer(count_value, "count")
    assert declared_count is not None
    if declared_count < 0:
        raise ValueError(f"{capture_path}: count cannot be negative")
    entries_payload = _first(payload, _ENTRY_KEYS)
    if not isinstance(entries_payload, list):
        raise ValueError(f"{capture_path}: descriptor entries must be a list")
    entries = tuple(parse_entry(entry, index) for index, entry in enumerate(entries_payload))
    known = set(_TYPE_KEYS) | set(_COUNT_KEYS) | set(_ENTRY_KEYS) | {
        "visible",
        "is_visible",
        "enabled",
        "is_enabled",
        "discriminator",
        "unknown_flags",
        "flags",
        "entry_layout",
        "controller_count",
        "count_matches_controller",
        "selected_entry",
    }
    return NativeDescriptorSnapshot(
        type_id=type_id,
        mapped_type=(type_map or {}).get(type_id) if type_id is not None else None,
        declared_count=declared_count,
        entries=entries,
        visible=_optional_bool(payload, "visible", "is_visible"),
        enabled=_optional_bool(payload, "enabled", "is_enabled"),
        discriminator=_safe_json(payload.get("discriminator")),
        unknown_flags=_unknown_flags(payload, known),
        capture_path=capture_path,
    )


def _looks_like_descriptor(value: Any) -> bool:
    return bool(
        isinstance(value, Mapping)
        and any(key in value for key in _ENTRY_KEYS)
        and any(key in value for key in _COUNT_KEYS)
    )


def _builder_trace_descriptor(
    snapshot: Mapping[str, Any], index: int
) -> Mapping[str, Any] | None:
    """Adapt one real ``menu_builder_snapshot_trace.py`` output.

    The tracer stores immutable copies under ``snapshots[]``
    rather than embedding descriptors in every event.  This adapter adds only
    semantics proven for the copied eight-byte entries; it never retains the
    ephemeral descriptor pointer from ``events[]``.
    """

    type_id = _integer(_first(snapshot, _TYPE_KEYS), "type", allow_none=True)
    entries_payload = snapshot.get("entries")
    if type_id is None or not isinstance(entries_payload, list):
        return None

    header = snapshot.get("header")
    decoded: Mapping[str, Any] = {}
    if isinstance(header, Mapping) and isinstance(header.get("decoded"), Mapping):
        decoded = header["decoded"]

    normalized_entries: list[dict[str, Any]] = []
    for ordinal, entry_payload in enumerate(entries_payload):
        raw = _raw_entry_bytes(entry_payload, ordinal)
        if type_id in (0x06, 0x08):
            enabled: bool | None = not bool(raw[3] & 0x02)
        elif type_id == 0x0B:
            enabled = not bool(raw[4] & 0x01)
        else:
            enabled = None
        entry = dict(entry_payload) if isinstance(entry_payload, Mapping) else {}
        entry.update({"raw": raw.hex(), "visible": True, "enabled": enabled})
        if type_id == 0x0B:
            # Proven end-to-end: 0x27875A loads this word and 0x278766 passes
            # it to the native submission routine 0x311CD0.
            entry["action_id"] = int.from_bytes(raw[:2], "little")
        normalized_entries.append(entry)

    count = _first(snapshot, ("count_raw", "count_copied", "count"))
    if count is None:
        count = len(normalized_entries)
    discriminator = {
        "snapshot_id": snapshot.get("snapshot_id", index),
        "actor_token_u32_08": decoded.get("actor_or_context_u32_08"),
        "variant_u32_14": decoded.get("discriminator_u32_14"),
        "subcategory_u32_18": decoded.get("u32_18"),
        "context_u32_1c": decoded.get("u32_1c"),
        "caller": snapshot.get("caller"),
    }
    return {
        "type": type_id,
        "count": count,
        "entries": normalized_entries,
        "visible": True,
        "enabled": True,
        "discriminator": discriminator,
        "unknown_flags": {
            "capture_flags": snapshot.get("capture_flags"),
            "return_value": snapshot.get("return_value"),
            "count_copied": snapshot.get("count_copied"),
        },
    }


def _descriptor_payloads(document: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """Recognize normalized, future builder-trace, and old snapshot envelopes."""

    if _looks_like_descriptor(document):
        return [("descriptor", document)]

    result: list[tuple[str, Mapping[str, Any]]] = []
    snapshots = document.get("snapshots")
    if isinstance(snapshots, list):
        for index, snapshot in enumerate(snapshots):
            if not isinstance(snapshot, Mapping):
                continue
            descriptor = _builder_trace_descriptor(snapshot, index)
            if descriptor is not None:
                result.append((f"snapshots[{index}]", descriptor))

    descriptors = document.get("descriptors")
    if isinstance(descriptors, list):
        for index, descriptor in enumerate(descriptors):
            if isinstance(descriptor, Mapping) and _looks_like_descriptor(descriptor):
                result.append((f"descriptors[{index}]", descriptor))

    events = document.get("events")
    if isinstance(events, list):
        for index, event in enumerate(events):
            if not isinstance(event, Mapping):
                continue
            descriptor = event.get("descriptor", event)
            if isinstance(descriptor, Mapping) and _looks_like_descriptor(descriptor):
                merged = dict(descriptor)
                for key in ("discriminator", "visible", "enabled"):
                    if key in event and key not in merged:
                        merged[key] = event[key]
                result.append((f"events[{index}].descriptor", merged))

    # Compatibility with menu_native_snapshot.py.  Dynamic addresses and raw
    # object blocks are not copied into the normalized result.
    controllers = document.get("controllers")
    if isinstance(controllers, list):
        for controller_index, controller in enumerate(controllers):
            if not isinstance(controller, Mapping):
                continue
            candidates = controller.get("descriptor_candidates")
            if not isinstance(candidates, list):
                continue
            for candidate_index, candidate in enumerate(candidates):
                if not isinstance(candidate, Mapping):
                    continue
                decoded = candidate.get("decoded")
                if isinstance(decoded, Mapping) and _looks_like_descriptor(decoded):
                    result.append((
                        "controllers"
                        f"[{controller_index}].descriptor_candidates[{candidate_index}]",
                        decoded,
                    ))
    return result


def parse_catalog_document(
    document: Mapping[str, Any],
    *,
    type_map: Mapping[int, str] | None = None,
) -> NativeCatalogSnapshot:
    payloads = _descriptor_payloads(document)
    if not payloads:
        raise ValueError("document contains no recognizable native descriptor")
    descriptors = tuple(
        parse_descriptor(payload, capture_path=path, type_map=type_map)
        for path, payload in payloads
    )
    generation_value = document.get("generation")
    generation = _integer(generation_value, "generation", allow_none=True)
    return NativeCatalogSnapshot(
        descriptors=descriptors,
        label=str(document["label"]) if document.get("label") is not None else None,
        source_schema=(
            str(document["schema"]) if document.get("schema") is not None else None
        ),
        captured_utc=(
            str(document["captured_utc"])
            if document.get("captured_utc") is not None
            else None
        ),
        generation=generation,
    )


def load_catalog_snapshot(
    path: Path,
    *,
    type_map: Mapping[int, str] | None = None,
) -> NativeCatalogSnapshot:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise ValueError("snapshot root must be a JSON object")
    return parse_catalog_document(document, type_map=type_map)


class ActionDefinitionProvider(Protocol):
    """The small NativeActionCatalog surface needed by the adapter."""

    def definition(self, action_id: int) -> ActionDefinition: ...


@dataclass(frozen=True)
class EntryBinding:
    """Explicit semantic evidence for one descriptor ordinal.

    A binding may come from a confirmed type table, router trace, or confirm
    trace.  It is never synthesized from ``dialog_index``.
    """

    ordinal: int
    kind: EntryKind
    name: str
    description: str = ""
    page_key: str | None = None
    action_id: int | None = None
    visible: bool | None = None
    enabled: bool | None = None


@dataclass(frozen=True)
class UnresolvedEntry:
    ordinal: int
    dialog_index: int
    reason: str


@dataclass(frozen=True)
class PageProjection:
    page: PageDefinition | None
    unresolved: tuple[UnresolvedEntry, ...]
    diagnostics: tuple[str, ...]


def project_page_definition(
    descriptor: NativeDescriptorSnapshot,
    *,
    page_key: str,
    title: str,
    bindings: Mapping[int, EntryBinding],
    action_catalog: ActionDefinitionProvider,
) -> PageProjection:
    """Project a captured descriptor into a P2 model page conservatively.

    Only explicitly bound rows are projected.  An ACTION additionally needs
    an explicit action ID from either the trace entry or the binding.  Unknown
    enabled state is projected as disabled and reported; the pointer-free
    snapshot itself keeps the state as ``None``.
    """

    if descriptor.visible is False:
        return PageProjection(None, (), ("descriptor explicitly hidden",))

    menu_entries: list[MenuEntry] = []
    unresolved: list[UnresolvedEntry] = []
    diagnostics: list[str] = []
    for entry in descriptor.entries:
        binding = bindings.get(entry.ordinal)
        if entry.visible is False or (binding and binding.visible is False):
            continue
        if binding is None:
            unresolved.append(UnresolvedEntry(
                entry.ordinal, entry.dialog_index, "no semantic binding"
            ))
            continue

        enabled_evidence = (
            binding.enabled if binding.enabled is not None else entry.enabled
        )
        enabled = enabled_evidence is True
        if enabled_evidence is None:
            diagnostics.append(
                f"entry {entry.ordinal} enabled state unknown; projected disabled"
            )

        action: ActionDefinition | None = None
        if binding.kind is EntryKind.ACTION:
            explicit_ids = {value for value in (binding.action_id, entry.action_id) if value is not None}
            if len(explicit_ids) > 1:
                raise ValueError(f"entry {entry.ordinal} has conflicting explicit action IDs")
            if not explicit_ids:
                unresolved.append(UnresolvedEntry(
                    entry.ordinal, entry.dialog_index, "action ID not observed"
                ))
                continue
            action = action_catalog.definition(explicit_ids.pop())
            action = replace(
                action,
                name=binding.name or action.name,
                description=binding.description or action.description,
                enabled=enabled,
            )
        elif binding.kind is EntryKind.PAGE and not binding.page_key:
            unresolved.append(UnresolvedEntry(
                entry.ordinal, entry.dialog_index, "page binding has no page_key"
            ))
            continue

        menu_entries.append(MenuEntry(
            name=binding.name,
            description=binding.description,
            kind=binding.kind,
            page_key=binding.page_key,
            action=action,
            enabled=enabled,
        ))

    return PageProjection(
        PageDefinition(page_key, title, tuple(menu_entries)),
        tuple(unresolved),
        tuple(diagnostics),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    snapshot = load_catalog_snapshot(args.snapshot)
    payload = snapshot.as_dict()
    rendered = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ENTRY_STRIDE",
    "NORMALIZED_SCHEMA",
    "ActionDefinitionProvider",
    "EntryBinding",
    "NativeCatalogSnapshot",
    "NativeDescriptorSnapshot",
    "NativeEntrySnapshot",
    "PageProjection",
    "UnresolvedEntry",
    "load_catalog_snapshot",
    "parse_catalog_document",
    "parse_descriptor",
    "parse_entry",
    "project_page_definition",
]
