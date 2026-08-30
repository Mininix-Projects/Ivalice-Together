from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

from ff12_battle_pack import BattleTextPack
from ff12_st2e import St2eTable
from headless_menu_model import (
    ActionDefinition,
    MenuCapabilities,
    PageDefinition,
    TargetGroup,
    default_catalog,
)


HERE = Path(__file__).resolve().parent
BINARY_ROOT = HERE / "extracted_ui_assets/ps2data/image/ff12/test_battle/us/binaryfile"
DEFAULT_PACK = BINARY_ROOT / "battle_pack.bin"
DEFAULT_HELP_ACTION = BINARY_ROOT / "help_action.bin"
DEFAULT_HELP_MENU = BINARY_ROOT / "help_menu.bin"


@dataclass(frozen=True)
class CatalogRecord:
    action_id: int
    name: str
    description: str
    category: str
    target_groups: tuple[str, ...]
    description_source: str
    description_confidence: str

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["action_id_hex"] = f"0x{self.action_id:X}"
        return result


class NativeActionCatalog:
    def __init__(
        self,
        battle_pack: Path = DEFAULT_PACK,
        help_action: Path = DEFAULT_HELP_ACTION,
        help_menu: Path = DEFAULT_HELP_MENU,
    ) -> None:
        self.pack = BattleTextPack.read(battle_pack)
        self.help_action = St2eTable.read(help_action)
        self.help_menu = St2eTable.read(help_menu)

    def name(self, action_id: int) -> str:
        return self.pack.action_name(action_id)

    def _help(self, index: int) -> str:
        if 0 <= index < len(self.help_action.entries):
            return self.help_action.entries[index].text
        return ""

    def description(self, action_id: int) -> tuple[str, str, str]:
        if action_id == 0x113:
            return (
                "Dismiss the summoned Esper.",
                "native special command 0x113",
                "verified-command",
            )
        if action_id == 0x96:
            entry = self.help_menu.by_id(0x7530)
            return (
                entry[0].text if entry else "Attack with weapon in hand.",
                "help_menu.bin:0x7530",
                "verified",
            )
        if 0x00 <= action_id <= 0x50:
            return (
                self._help(action_id + 1),
                f"help_action.bin:index:{action_id + 1}",
                "verified-range",
            )
        if 0x52 <= action_id <= 0x6E:
            return (
                self._help(action_id),
                f"help_action.bin:index:{action_id}",
                "verified-range",
            )
        if 0x7C <= action_id <= 0x8E and self.name(action_id).endswith(" Mote"):
            base_name = self.name(action_id)[:-5]
            for base_id in range(0x51):
                if self.name(base_id) == base_name:
                    description, source, _confidence = self.description(base_id)
                    return (
                        description,
                        f"{source}; inherited by {base_name} Mote",
                        "derived-native-name",
                    )
        if 0x9E <= action_id <= 0xB5:
            index = action_id - 31
            return self._help(index), f"help_action.bin:index:{index}", "verified-range"
        if 0xD0 <= action_id <= 0xE1:
            index = action_id - 57
            return self._help(index), f"help_action.bin:index:{index}", "verified-range"
        if 0x106 <= action_id <= 0x112:
            index = action_id - 93
            return self._help(index), f"help_action.bin:index:{index}", "verified-range"
        return "", "unmapped", "unknown"

    def category(self, action_id: int) -> str:
        if action_id == 0x113:
            return "Mist"
        if action_id == 0x96:
            return "Attack"
        if 0x00 <= action_id <= 0x11 or 0x33 <= action_id <= 0x38:
            return "White Magicks"
        if 0x12 <= action_id <= 0x23:
            return "Black Magicks"
        if 0x24 <= action_id <= 0x32:
            return "Time Magicks"
        if 0x39 <= action_id <= 0x50:
            return "Green / Arcane Magicks"
        if 0x52 <= action_id <= 0x91:
            return "Items"
        if 0x9E <= action_id <= 0xB5:
            return "Technicks"
        if 0xD0 <= action_id <= 0xF1:
            return "Mist"
        if 0x106 <= action_id <= 0x112:
            return "Espers"
        return "Other"

    def target_groups(
        self, action_id: int, description: str
    ) -> tuple[TargetGroup, ...]:
        if action_id == 0x96:
            return TargetGroup.FOES, TargetGroup.PARTY
        lower = description.casefold()
        groups: list[TargetGroup] = []
        if "foe" in lower or "enemy" in lower:
            groups.append(TargetGroup.FOES)
        if any(word in lower for word in ("ally", "user", "caster", "party")):
            groups.append(TargetGroup.PARTY)
        return tuple(groups) or (TargetGroup.FOES, TargetGroup.PARTY)

    def record(self, action_id: int) -> CatalogRecord:
        description, source, confidence = self.description(action_id)
        groups = self.target_groups(action_id, description)
        return CatalogRecord(
            action_id=action_id,
            name=self.name(action_id),
            description=description,
            category=self.category(action_id),
            target_groups=tuple(group.value for group in groups),
            description_source=source,
            description_confidence=confidence,
        )

    def definition(self, action_id: int) -> ActionDefinition:
        record = self.record(action_id)
        groups = tuple(TargetGroup(value) for value in record.target_groups)
        default = TargetGroup.PARTY if groups == (TargetGroup.PARTY,) else groups[0]
        return ActionDefinition(
            action_id=record.action_id,
            name=record.name,
            description=record.description,
            target_groups=groups,
            default_group=default,
            category=record.category,
        )

    def records(self, action_ids: Iterable[int]) -> tuple[CatalogRecord, ...]:
        return tuple(self.record(action_id) for action_id in action_ids)

    def player_catalog(self) -> tuple[CatalogRecord, ...]:
        ids = (
            list(range(0x51))
            + list(range(0x52, 0x92))
            + [0x96]
            + list(range(0x9E, 0xB6))
            + list(range(0xD0, 0xE2))
            + list(range(0x106, 0x113))
        )
        return tuple(
            self.record(action_id)
            for action_id in ids
            if not self.name(action_id).casefold().startswith(("reserve", "not used"))
        )

    def build_menu_pages(
        self,
        unlocked: dict[str, Sequence[int]],
        *,
        gambits_unlocked: bool = True,
    ) -> dict[str, PageDefinition]:
        magick_categories = tuple(
            category
            for category in (
                "White Magicks",
                "Black Magicks",
                "Time Magicks",
                "Green Magicks",
                "Arcane Magicks",
            )
            if category in unlocked
        )
        magicks = {
            category: tuple(
                self.definition(action_id) for action_id in unlocked[category]
            )
            for category in magick_categories
        }
        technicks = tuple(
            self.definition(action_id) for action_id in unlocked.get("Technicks", ())
        )
        items = tuple(
            self.definition(action_id) for action_id in unlocked.get("Items", ())
        )
        mist = tuple(
            self.definition(action_id) for action_id in unlocked.get("Mist", ())
        )
        return default_catalog(
            MenuCapabilities(
                magick_pages=magick_categories,
                technicks_unlocked=bool(technicks),
                mist_unlocked=bool(mist),
                gambits_unlocked=gambits_unlocked,
                items_available=bool(items),
            ),
            magicks=magicks,
            technicks=technicks,
            items=items,
            mist=mist,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", type=lambda value: int(value, 0))
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    catalog = NativeActionCatalog()
    records = (
        [catalog.record(args.action)]
        if args.action is not None
        else list(catalog.player_catalog())
    )
    payload = {
        "format": "ff12-native-player-action-catalog-v1",
        "count": len(records),
        "records": [record.as_dict() for record in records],
    }
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            payload if args.action is not None else {"count": len(records)}, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
