"""Project a pointer-free native Penelo catalog into P2 menu pages."""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # co-op modules


from headless_menu_model import (
    EntryKind,
    MenuEntry,
    PageDefinition,
)
from native_action_catalog import NativeActionCatalog
from native_menu_catalog_provider import (
    NativeCatalogSnapshot,
    NativeDescriptorSnapshot,
)
from native_type0b_entry import decode_type0b_entry


ROOT_ATTACK = 0x00
ROOT_MAGICKS = 0x12
ROOT_GAMBITS = 0x0D
ROOT_ITEMS = 0x03
ROOT_MIST_IDS = frozenset((0x0B, 0x13))


def _context(descriptor: NativeDescriptorSnapshot) -> Mapping[str, object]:
    return descriptor.discriminator if isinstance(descriptor.discriminator, Mapping) else {}


def _variant(descriptor: NativeDescriptorSnapshot) -> int:
    value = _context(descriptor).get("variant_u32_14", 0)
    return int(value) if isinstance(value, int) else 0


def _descriptor_map(
    catalog: NativeCatalogSnapshot,
) -> dict[tuple[int, int], NativeDescriptorSnapshot]:
    result: dict[tuple[int, int], NativeDescriptorSnapshot] = {}
    for descriptor in catalog.descriptors:
        if descriptor.type_id is None or not descriptor.complete:
            continue
        variant = _variant(descriptor) if descriptor.type_id == 0x0B else 0
        result[(descriptor.type_id, variant)] = descriptor
    return result


def _empty_page(key: str, title: str) -> PageDefinition:
    return PageDefinition(
        key,
        title,
        (MenuEntry("No actions available", "", EntryKind.LABEL, enabled=False),),
    )


def _action_page(
    descriptor: NativeDescriptorSnapshot | None,
    *,
    key: str,
    title: str,
    action_catalog: NativeActionCatalog,
    live_quantities: Mapping[int, int] | None,
    items: bool = False,
) -> PageDefinition:
    if descriptor is None:
        return _empty_page(key, title)
    discriminator = _variant(descriptor)
    entries: list[MenuEntry] = []
    for native_entry in descriptor.entries:
        decoded = decode_type0b_entry(
            native_entry.raw_hex,
            discriminator=discriminator,
        )
        enabled = native_entry.enabled is not False and decoded.enabled
        action = action_catalog.definition(decoded.action_id)
        quantity = None
        if items:
            if live_quantities is None:
                quantity = decoded.inventory_quantity
            else:
                quantity = int(live_quantities.get(decoded.action_id, 0))
                # Native Items pages contain only inventory rows that exist.
                # Once live counters are available they are authoritative;
                # do not resurrect a consumed item from capture-time bytes.
                if quantity <= 0:
                    continue
            enabled = enabled and bool(quantity)
        action = replace(action, enabled=enabled, quantity=quantity)
        entries.append(
            MenuEntry(
                action.name,
                action.description,
                EntryKind.ACTION,
                action=action,
                enabled=enabled,
            )
        )
    if not entries:
        return _empty_page(key, title)
    return PageDefinition(key, title, tuple(entries))


def _category_identity(entry) -> tuple[str, str, str] | None:
    raw = bytes.fromhex(entry.raw_hex)
    category_selector = bool(raw[3] & 0x04)
    category_id = entry.dialog_index
    if not category_selector and category_id == 2:
        return "technicks", "Technicks", "Use technicks."
    if category_selector:
        categories = {
            0: ("magick:white_magicks", "White Magicks"),
            1: ("magick:black_magicks", "Black Magicks"),
            2: ("magick:time_magicks", "Time Magicks"),
            3: ("magick:green_magicks", "Green Magicks"),
            4: ("magick:arcane_magicks", "Arcane Magicks"),
        }
        found = categories.get(category_id)
        if found:
            key, name = found
            return key, name, f"Use {name.casefold()}."
    return None


def build_p2_pages(
    catalog: NativeCatalogSnapshot,
    *,
    action_catalog: NativeActionCatalog,
    live_quantities: Mapping[int, int] | None = None,
) -> dict[str, PageDefinition]:
    """Build the complete candidate tree without mutating a live model."""

    descriptors = _descriptor_map(catalog)
    root_descriptor = descriptors.get((0x06, 0))
    if root_descriptor is None:
        raise ValueError("native catalog has no complete root descriptor")

    pages: dict[str, PageDefinition] = {}
    categories = descriptors.get((0x08, 0))
    category_entries: list[MenuEntry] = []
    if categories is not None:
        for native_entry in categories.entries:
            identity = _category_identity(native_entry)
            enabled = native_entry.enabled is True
            if identity is None:
                category_entries.append(
                    MenuEntry(
                        f"Unknown category 0x{native_entry.dialog_index:X}",
                        "",
                        EntryKind.LABEL,
                        enabled=False,
                    )
                )
                continue
            key, name, description = identity
            if key == "magick:white_magicks":
                page = _action_page(
                    descriptors.get((0x0B, 1)),
                    key=key,
                    title=name,
                    action_catalog=action_catalog,
                    live_quantities=live_quantities,
                )
            elif key == "technicks":
                page = _action_page(
                    descriptors.get((0x0B, 2)),
                    key=key,
                    title=name,
                    action_catalog=action_catalog,
                    live_quantities=live_quantities,
                )
            else:
                page = _empty_page(key, name)
            pages[key] = page
            category_entries.append(
                MenuEntry(
                    name,
                    description,
                    EntryKind.PAGE,
                    page_key=key,
                    enabled=enabled,
                )
            )
        pages["magicks"] = PageDefinition(
            "magicks", "Magicks & Technicks", tuple(category_entries)
        )

    pages["items"] = _action_page(
        descriptors.get((0x0B, 3)),
        key="items",
        title="Items",
        action_catalog=action_catalog,
        live_quantities=live_quantities,
        items=True,
    )

    root_entries: list[MenuEntry] = []
    for native_entry in root_descriptor.entries:
        enabled = native_entry.enabled is not False
        row_id = native_entry.dialog_index
        if row_id == ROOT_ATTACK:
            action = replace(action_catalog.definition(0x96), enabled=enabled)
            root_entries.append(
                MenuEntry(
                    action.name,
                    action.description,
                    EntryKind.ACTION,
                    action=action,
                    enabled=enabled,
                )
            )
        elif row_id == ROOT_MAGICKS and "magicks" in pages:
            root_entries.append(MenuEntry(
                "Magicks & Technicks",
                "Use magicks and technicks.",
                EntryKind.PAGE,
                page_key="magicks",
                enabled=enabled,
            ))
        elif row_id in ROOT_MIST_IDS:
            pages.setdefault("mist", _empty_page("mist", "Mist"))
            root_entries.append(MenuEntry(
                "Mist", "Use Mist abilities.", EntryKind.PAGE,
                page_key="mist", enabled=enabled,
            ))
        elif row_id == ROOT_GAMBITS:
            root_entries.append(MenuEntry(
                "Gambits", "Toggle gambits.", EntryKind.TOGGLE, enabled=enabled
            ))
        elif row_id == ROOT_ITEMS:
            root_entries.append(MenuEntry(
                "Items", "Use items.", EntryKind.PAGE,
                page_key="items", enabled=enabled,
            ))
        else:
            root_entries.append(MenuEntry(
                f"Unknown command 0x{row_id:X}", "", EntryKind.LABEL, enabled=False
            ))
    pages["root"] = PageDefinition("root", "", tuple(root_entries))
    return pages


__all__ = ["build_p2_pages"]
