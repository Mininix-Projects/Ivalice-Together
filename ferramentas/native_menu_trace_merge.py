"""Merge one-shot FF12 builder traces into one pointer-free actor catalog.

The native trace files are append-only RE evidence.  This module selects the
newest complete successful output for each stable logical page, strips all
runtime pointer provenance through ``native_menu_catalog_provider``, and
publishes one JSON generation atomically.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Iterable, Sequence

from native_menu_catalog_provider import (
    NativeCatalogSnapshot,
    NativeDescriptorSnapshot,
    load_catalog_snapshot,
)


def _context(descriptor: NativeDescriptorSnapshot) -> dict[str, object]:
    value = descriptor.discriminator
    return dict(value) if isinstance(value, dict) else {}


def stable_page_key(descriptor: NativeDescriptorSnapshot) -> tuple[int, int, int, int]:
    """Key one logical page while ignoring known volatile route residue."""

    if descriptor.type_id is None:
        raise ValueError("descriptor type is unknown")
    context = _context(descriptor)
    variant = int(context.get("variant_u32_14") or 0)
    subcategory = int(context.get("subcategory_u32_18") or 0)
    dynamic_context = int(context.get("context_u32_1c") or 0)
    # Root and Magicks category descriptors can inherit the previous route's
    # +0x14 value (observed 0 then 3 for the same type-8 page).  Their logical
    # identity is the type itself.  Type-0B needs +0x14 to distinguish White,
    # Technicks, Items, and the remaining action lists.
    if descriptor.type_id in (0x06, 0x08):
        variant = subcategory = dynamic_context = 0
    return descriptor.type_id, variant, subcategory, dynamic_context


def descriptor_actor_token(descriptor: NativeDescriptorSnapshot) -> int | None:
    value = _context(descriptor).get("actor_token_u32_08")
    return int(value) if isinstance(value, int) else None


def _successful(descriptor: NativeDescriptorSnapshot) -> bool:
    flags = descriptor.unknown_flags
    result = flags.get("return_value")
    capture_flags = flags.get("capture_flags")
    return (
        descriptor.complete
        and descriptor.type_id is not None
        and (not isinstance(result, int) or result >= 0)
        and (capture_flags in (None, 0))
    )


def merge_catalogs(
    catalogs: Iterable[NativeCatalogSnapshot],
    *,
    actor_token: int,
    generation: int | None = None,
    label: str = "penelo",
) -> NativeCatalogSnapshot:
    """Keep the newest good descriptor for every logical actor page."""

    selected: dict[tuple[int, int, int, int], NativeDescriptorSnapshot] = {}
    latest_utc: str | None = None
    for catalog in catalogs:
        if catalog.captured_utc and (
            latest_utc is None or catalog.captured_utc > latest_utc
        ):
            latest_utc = catalog.captured_utc
        for descriptor in catalog.descriptors:
            if descriptor_actor_token(descriptor) != actor_token:
                continue
            if not _successful(descriptor):
                continue
            selected[stable_page_key(descriptor)] = descriptor
    if not selected:
        raise ValueError(f"no complete descriptors for actor token {actor_token:#x}")
    if not any(key[0] == 0x06 for key in selected):
        raise ValueError("merged generation has no root type-6 descriptor")

    ordered = tuple(selected[key] for key in sorted(selected))
    return NativeCatalogSnapshot(
        descriptors=ordered,
        label=label,
        source_schema="ff12.native-builder-output-snapshot.v1/merged",
        captured_utc=latest_utc,
        generation=generation if generation is not None else time.time_ns(),
    )


def write_catalog_atomic(catalog: NativeCatalogSnapshot, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(catalog.as_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)


def merge_paths(
    paths: Sequence[Path],
    *,
    actor_token: int,
    destination: Path,
    generation: int | None = None,
    label: str = "penelo",
) -> NativeCatalogSnapshot:
    catalogs = tuple(load_catalog_snapshot(path) for path in paths)
    result = merge_catalogs(
        catalogs,
        actor_token=actor_token,
        generation=generation,
        label=label,
    )
    write_catalog_atomic(result, destination)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--actor-token", required=True, type=lambda value: int(value, 0))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--generation", type=int)
    parser.add_argument("--label", default="penelo")
    args = parser.parse_args(argv)
    result = merge_paths(
        args.inputs,
        actor_token=args.actor_token,
        destination=args.output,
        generation=args.generation,
        label=args.label,
    )
    pages = [stable_page_key(descriptor) for descriptor in result.descriptors]
    print(
        f"NATIVE_MENU_CATALOG_MERGED output={args.output} "
        f"generation={result.generation} pages={pages}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
