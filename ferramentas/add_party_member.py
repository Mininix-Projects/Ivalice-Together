"""One-shot, duplicate-safe insertion of a native member into the live party."""

from __future__ import annotations

import argparse
import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # co-op modules


import coop_runtime as coop
from penelo_story_guard import (
    ADD_PARTY_MEMBER,
    BATTLE_INFO_GLOBAL,
    EMPTY,
    IN_PARTY_FLAG,
    PARTY_ALL_READ,
    PARTY_LAYOUT_COUNT,
    PARTY_LAYOUT_OFFSET,
    PARTY_LAYOUT_STRIDE,
    PARTY_MEMBER_OFFSET,
    PARTY_MEMBER_STRIDE,
    RemoteCaller,
    pack_layout,
    unpack_layout,
)


def insert_active(slots: tuple[int, ...], member_id: int) -> tuple[int, ...]:
    """Place exactly one member after existing active units without eviction."""

    active = [value for value in slots[:4] if value not in (EMPTY, member_id)]
    reserve = [value for value in slots[4:] if value not in (EMPTY, member_id)]
    if len(active) >= 4:
        raise RuntimeError("party ativa ja possui quatro membros")
    active.append(member_id)
    active += [EMPTY] * (4 - len(active))
    reserve += [EMPTY] * (5 - len(reserve))
    return tuple(active + reserve[:5])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("member_id", type=int, nargs="?", default=4)
    args = parser.parse_args()
    if not 0 <= args.member_id <= 5:
        raise ValueError("member_id must be a native main-party ID from 0 to 5")

    process = coop.RemoteProcess(coop.find_pid())
    caller = None
    try:
        if process.read(ADD_PARTY_MEMBER, 5) != b"\x48\x89\x5C\x24\x08":
            raise RuntimeError("unexpected addPartyMember build")
        if process.read(PARTY_ALL_READ, 4) != b"\x48\x83\xEC\x28":
            raise RuntimeError("unexpected partyallread build")
        base = process.u64(BATTLE_INFO_GLOBAL)
        if not base:
            raise RuntimeError("battle info unavailable")

        before = tuple(
            unpack_layout(
                process.read(
                    base + PARTY_LAYOUT_OFFSET + index * PARTY_LAYOUT_STRIDE,
                    PARTY_LAYOUT_STRIDE,
                )
            )
            for index in range(PARTY_LAYOUT_COUNT)
        )
        flags_address = (
            base + PARTY_MEMBER_OFFSET + args.member_id * PARTY_MEMBER_STRIDE
        )
        flags = process.read(flags_address, 1)[0]
        print(f"PARTY_ADD before flags={flags:#04x} layouts={before}", flush=True)

        caller = RemoteCaller(process)
        if not (flags & IN_PARTY_FLAG):
            result = caller.call(ADD_PARTY_MEMBER, args.member_id, 0)
            print(f"PARTY_ADD native result={result:#x}", flush=True)
            time.sleep(0.15)
            base = process.u64(BATTLE_INFO_GLOBAL)
            if not base:
                raise RuntimeError("battle info disappeared after native add")

        for index in range(PARTY_LAYOUT_COUNT):
            address = base + PARTY_LAYOUT_OFFSET + index * PARTY_LAYOUT_STRIDE
            current = unpack_layout(process.read(address, PARTY_LAYOUT_STRIDE))
            desired = insert_active(current, args.member_id)
            if desired != current:
                process.write(address, pack_layout(desired))

        caller.call(PARTY_ALL_READ)
        time.sleep(0.75)
        after = tuple(
            unpack_layout(
                process.read(
                    base + PARTY_LAYOUT_OFFSET + index * PARTY_LAYOUT_STRIDE,
                    PARTY_LAYOUT_STRIDE,
                )
            )
            for index in range(PARTY_LAYOUT_COUNT)
        )
        for index in (1, 2):
            if args.member_id not in after[index][:4] or after[index].count(args.member_id) != 1:
                raise RuntimeError(f"member missing/duplicated in live layout {index}")
        print(f"PARTY_ADD ready member={args.member_id} layouts={after}", flush=True)
        return 0
    finally:
        if caller is not None:
            caller.close()
        process.close()


if __name__ == "__main__":
    raise SystemExit(main())
