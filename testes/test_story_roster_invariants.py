from __future__ import annotations

import itertools
import unittest

import coop_roster
import penelo_story_guard as guard
import story_party_patch as story


class StoryRosterInvariantTests(unittest.TestCase):
    def test_native_slot_partitions_hold_exhaustively(self):
        """Slots 0..2 are main, slot 3 guest, and 4..8 reserve."""

        writable_slots = (0, 1, 2, 4, 5, 6, 7, 8)
        protected = guard.ASHE_ID
        guest = guard.REKS_ID
        for occupied_mask, prefer_active in itertools.product(
            range(1 << len(writable_slots)), (False, True)
        ):
            original = [story.EMPTY] * 9
            original[3] = guest
            for bit, slot in enumerate(writable_slots):
                if occupied_mask & (1 << bit):
                    # Unique non-native sentinels make every preserved slot
                    # observable without introducing accidental duplicates.
                    original[slot] = 0x20 + slot
            result = coop_roster.normalize_party_layout(
                original,
                (protected,),
                empty=story.EMPTY,
                prefer_active=prefer_active,
            )

            self.assertEqual(result[3], guest)
            self.assertEqual(
                [value for value in result if value != story.EMPTY],
                list(dict.fromkeys(value for value in result if value != story.EMPTY)),
            )
            for slot in writable_slots:
                if original[slot] != story.EMPTY:
                    self.assertEqual(result[slot], original[slot])
            self.assertEqual(
                coop_roster.normalize_party_layout(
                    result,
                    (protected,),
                    empty=story.EMPTY,
                    prefer_active=prefer_active,
                ),
                result,
            )

            free_active = [slot for slot in (0, 1, 2) if original[slot] == story.EMPTY]
            free_reserve = [
                slot for slot in range(4, 9) if original[slot] == story.EMPTY
            ]
            destinations = (free_active if prefer_active else []) + free_reserve
            if destinations:
                self.assertEqual(result[destinations[0]], protected)
            else:
                self.assertNotIn(protected, result)

    def test_every_native_guest_id_prefers_slot_three_when_duplicated(self):
        duplicate_slots = (0, 1, 2, 4, 5, 6, 7, 8)
        for guest_id, duplicate_slot in itertools.product(
            range(guard.REKS_ID, 15), duplicate_slots
        ):
            original = [story.EMPTY] * 9
            original[3] = guest_id
            original[duplicate_slot] = guest_id
            result = coop_roster.normalize_party_layout(
                original,
                (),
                empty=story.EMPTY,
                prefer_active=False,
            )
            self.assertEqual(result[3], guest_id)
            self.assertEqual(result[duplicate_slot], story.EMPTY)
            self.assertEqual(result.count(guest_id), 1)

    def test_story_record_collapses_duplicate_guest_without_moving_first(self):
        members = (guard.PENELO_ID, guard.ASHE_ID)
        original = bytes(
            (
                story.VAAN_ID,
                11,
                2,
                story.EMPTY,
                11,
                members[0],
                members[1],
                story.EMPTY,
                story.EMPTY,
            )
        )
        result = story.normalize_story_party(original, members=members)
        self.assertEqual(result.count(11), 1)
        self.assertEqual(result[1], 11)
        self.assertEqual(
            len([value for value in result if value != story.EMPTY]),
            len(set(value for value in result if value != story.EMPTY)),
        )

    def test_deduplication_prefers_the_dedicated_native_guest_slot(self):
        members = (guard.PENELO_ID, guard.ASHE_ID)
        original = (
            story.VAAN_ID,
            11,
            2,
            11,
            11,
            members[0],
            members[1],
            story.EMPTY,
            story.EMPTY,
        )
        result = coop_roster.normalize_party_layout(
            original,
            members,
            empty=story.EMPTY,
            prefer_active=False,
        )
        self.assertEqual(result[1], story.EMPTY)
        self.assertEqual(result[3], 11)
        self.assertEqual(result[4], story.EMPTY)
        self.assertEqual(result.count(11), 1)

    def test_live_roster_normalizer_keeps_every_member_unique(self):
        protected = guard.PROTECTED_MEMBER_IDS
        self.assertTrue(protected)
        guest = 11
        source = (
            guard.VAAN_ID,
            guest,
            2,
            guard.EMPTY,
            guest,
            *protected,
        )
        source = (source + (guard.EMPTY,) * 9)[:9]
        result = guard.normalize_coop_roster(
            tuple(source),
            prefer_active=False,
        )
        members = [value for value in result if value != guard.EMPTY]
        self.assertEqual(len(members), len(set(members)))
        self.assertEqual(result.count(guest), 1)
        for member_id in protected:
            self.assertEqual(result.count(member_id), 1)

    def test_health_rejects_duplicate_guest_in_default_and_menu(self):
        protected = guard.PROTECTED_MEMBER_IDS
        flags = tuple(0x1C for _member_id in protected)
        active = [guard.VAAN_ID, *protected]
        active = active[: guard.COOP_ACTIVE_SLOTS]
        active += [guard.EMPTY] * (guard.ACTIVE_STORAGE_SLOTS - len(active))
        good = tuple(active + [guard.EMPTY] * 5)
        duplicated = list(good)
        duplicated[guard.ACTIVE_STORAGE_SLOTS] = 11
        duplicated[guard.ACTIVE_STORAGE_SLOTS + 1] = 11
        layouts = (
            tuple(duplicated),
            good,
            good,
            tuple(duplicated),
            good,
        )
        self.assertFalse(guard.PeneloStoryGuard.healthy((flags, layouts)))


if __name__ == "__main__":
    unittest.main()
