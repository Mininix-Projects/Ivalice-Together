import unittest

from add_party_member import EMPTY, insert_active


class AddPartyMemberLiveTests(unittest.TestCase):
    def test_appends_one_active_member_and_removes_reserve_duplicate(self):
        source = (0, 5, EMPTY, EMPTY, 4, EMPTY, EMPTY, EMPTY, EMPTY)
        self.assertEqual(
            insert_active(source, 4),
            (0, 5, 4, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY),
        )

    def test_is_idempotent(self):
        source = (0, 5, 4, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY)
        self.assertEqual(insert_active(source, 4), source)


if __name__ == "__main__":
    unittest.main()
