"""Permanent-status regressions for custom menu and target eligibility."""

from types import SimpleNamespace
import unittest
from unittest import mock

import headless_menu_core as core
import headless_menu_shell as shell
import session_state


ACTOR = 0x71000000
BATTLE_KEEP = 0x72000000
ENTRY = 0x73000000
BATTLE_KEEP_EXTENDED = 0x74000000
HANDLE = 0x100001


class StatusProcess:
    def __init__(self, *, temporary=0, permanent=0, hp=100):
        self.temporary = temporary
        self.permanent = permanent
        self.hp = hp

    def u64(self, address):
        return {
            ACTOR: core.ACTOR_ACTIVE_FLAG,
            ACTOR + 0x10: ENTRY,
            ACTOR + 0x698: BATTLE_KEEP,
            ACTOR + 0x6A0: BATTLE_KEEP_EXTENDED,
            ENTRY + 0xF8: 0x75000000,
        }.get(address, 0)

    def u32(self, address):
        return {
            ACTOR + 0x08: HANDLE,
            BATTLE_KEEP + 0x48: self.hp,
            BATTLE_KEEP + shell.TEMPORARY_STATUS_EFFECTS_OFFSET: self.temporary,
            BATTLE_KEEP + shell.PERMANENT_STATUS_EFFECTS_OFFSET: self.permanent,
            ENTRY + 0x100: 1,
        }.get(address, 0)

    def read(self, address, size):
        if address == ENTRY + 0x0E and size == 1:
            return b"\x01"
        if address == BATTLE_KEEP_EXTENDED and size == 1:
            return b"\x00"
        return b"\x00" * size


class TornLeaderStatusProcess(StatusProcess):
    def __init__(self, field):
        super().__init__()
        self.field = field
        self.identity_reads = 0
        self.hp_reads = 0
        self.temporary_reads = 0

    def u64(self, address):
        if address == ACTOR + 0x698 and self.field == "identity":
            self.identity_reads += 1
            return BATTLE_KEEP if self.identity_reads % 2 else BATTLE_KEEP + 0x100
        return super().u64(address)

    def u32(self, address):
        if address == BATTLE_KEEP + 0x48 and self.field == "hp":
            self.hp_reads += 1
            return 100 if self.hp_reads % 2 else 99
        if (
            address == BATTLE_KEEP + shell.TEMPORARY_STATUS_EFFECTS_OFFSET
            and self.field == "temporary"
        ):
            self.temporary_reads += 1
            return 0 if self.temporary_reads % 2 else 1 << 4
        return super().u32(address)


class PermanentStatusGateTests(unittest.TestCase):
    def test_status_helpers_union_the_two_native_words(self):
        process = StatusProcess(temporary=1 << 4, permanent=1 << 13)
        expected = (1 << 4) | (1 << 13)
        self.assertEqual(shell.combined_status_effects(process, BATTLE_KEEP), expected)
        self.assertEqual(core.combined_status_effects(process, BATTLE_KEEP), expected)

    def test_leader_control_gate_rejects_the_complete_effective_status_set(self):
        unavailable_bits = (0, 1, 3, 4, 5, 13, 31)
        for word in ("temporary", "permanent"):
            for bit in unavailable_bits:
                with self.subTest(word=word, bit=bit):
                    process = StatusProcess(**{word: 1 << bit})
                    self.assertFalse(
                        shell.leader_control_availability(process, ACTOR)
                    )

    def test_leader_control_gate_keeps_berserk_and_immobilize_available(self):
        # Berserk(27) is the resident manual-command exception; Immobilize(14)
        # restricts locomotion, not command/leader ownership.
        for word in ("temporary", "permanent"):
            for bit in (14, 27):
                with self.subTest(word=word, bit=bit):
                    process = StatusProcess(**{word: 1 << bit})
                    self.assertTrue(
                        shell.leader_control_availability(process, ACTOR)
                    )
        self.assertFalse(
            shell.leader_control_availability(StatusProcess(hp=0), ACTOR)
        )

    def test_leader_control_gate_returns_unknown_for_every_torn_bracket(self):
        for field in ("identity", "hp", "temporary"):
            with self.subTest(field=field):
                self.assertIsNone(
                    shell.leader_control_availability(
                        TornLeaderStatusProcess(field),
                        ACTOR,
                    )
                )

    def test_unknown_availability_preserves_promotion_latch_until_clear_sample(self):
        identity = session_state.SessionIdentity(0, 2, 1)
        self.assertEqual(
            shell.ko_promoted_role_for_availability(
                identity,
                2,
                None,
                p1_present=True,
                previous_role="p2",
            ),
            "p2",
        )
        self.assertEqual(
            shell.ko_promoted_role_for_availability(
                identity,
                2,
                False,
                p1_present=True,
                previous_role=None,
            ),
            "p2",
        )
        self.assertIsNone(
            shell.ko_promoted_role_for_availability(
                identity,
                2,
                True,
                p1_present=True,
                previous_role="p2",
            )
        )
        self.assertIsNone(
            shell.ko_promoted_role_for_availability(
                identity,
                2,
                None,
                p1_present=False,
                previous_role="p2",
            )
        )

    def test_unknown_p1_sample_blocks_follower_leader_identity_reconcile(self):
        identity = session_state.SessionIdentity(0, 2, 1)
        self.assertTrue(
            shell.identity_reconcile_has_uncertain_follower_leader(
                identity,
                2,
                None,
                p1_present=True,
            )
        )
        self.assertFalse(
            shell.identity_reconcile_has_uncertain_follower_leader(
                identity,
                2,
                True,
                p1_present=True,
            )
        )
        self.assertFalse(
            shell.identity_reconcile_has_uncertain_follower_leader(
                identity,
                2,
                None,
                p1_present=False,
            )
        )

    def test_status_change_between_menu_poll_and_submit_fails_closed(self):
        process = StatusProcess(temporary=1 << 1)
        runtime = shell.CoopMenuRuntime.__new__(shell.CoopMenuRuntime)
        runtime.process = process
        runtime.p3_model = SimpleNamespace(issuer=0x200028)
        runtime.party = [("Basch", 0x200028, ACTOR)]
        runtime.native_submitter = SimpleNamespace(submit=mock.Mock(return_value=0))
        request = SimpleNamespace(
            issuer=0x200028,
            target=0x300040,
            action_id=0x51,
        )

        self.assertEqual(runtime.submit_request(request, player=3), 0)
        runtime.native_submitter.submit.assert_not_called()

    def test_live_target_catalog_rejects_permanent_ko(self):
        process = StatusProcess(permanent=1)
        actors = ((1, ACTOR, 0x76000000, (1.0, 2.0, 3.0)),)

        with mock.patch.object(core, "decode_ff12_text", return_value="Foe"):
            self.assertEqual(
                core.live_target_candidates(process, (), active_actors=actors),
                [],
            )

            process.permanent = 0
            self.assertEqual(
                len(core.live_target_candidates(process, (), active_actors=actors)),
                1,
            )

    def test_character_transfer_rejects_permanent_ko(self):
        runtime = shell.CoopMenuRuntime.__new__(shell.CoopMenuRuntime)
        runtime.process = StatusProcess(permanent=1)
        runtime.active_party = [(4, "Fran", HANDLE, ACTOR)]
        runtime.session_snapshot = SimpleNamespace(
            identity=session_state.SessionIdentity(0, 5, 3),
            p1_owner_party_member_id=0,
            p1_reserved_party_member_id=0,
        )

        with mock.patch("builtins.print"):
            self.assertEqual(runtime.select_coop_character(2, HANDLE), 0)

    def test_character_selector_presentation_matches_status_commit_gate(self):
        runtime = shell.CoopMenuRuntime.__new__(shell.CoopMenuRuntime)
        runtime.process = StatusProcess(permanent=1 << 1)
        runtime.active_party = [(4, "Fran", HANDLE, ACTOR)]
        runtime.refresh_actors = mock.Mock()
        runtime.model = SimpleNamespace(
            pending_action=shell.P2_CHANGE_CHARACTER_ACTION,
            preserve_candidate_on_refresh=True,
        )
        runtime.p3_model = SimpleNamespace(pending_action=None)
        runtime.p1_model = SimpleNamespace(pending_action=None)
        runtime.session_snapshot = SimpleNamespace(
            identity=session_state.SessionIdentity(0, 5, 3),
            p1_owner_party_member_id=0,
            p1_reserved_party_member_id=0,
        )

        candidate = runtime.target_provider(runtime.model)[0]
        self.assertFalse(candidate.selectable)
        with mock.patch("builtins.print"):
            self.assertEqual(runtime.select_coop_character(2, HANDLE), 0)

        runtime.process.permanent = 1 << 27
        candidate = runtime.target_provider(runtime.model)[0]
        self.assertTrue(candidate.selectable)

    def test_leader_transfer_rejects_permanent_ko(self):
        runtime = shell.CoopMenuRuntime.__new__(shell.CoopMenuRuntime)
        runtime.process = StatusProcess(permanent=1)
        runtime.active_party = [(1, "Ashe", HANDLE, ACTOR)]

        with (
            mock.patch.object(shell.legacy.coop, "native_party_leader", return_value=0),
            mock.patch.object(shell.legacy.coop, "request_native_leader_refresh") as request,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(runtime.select_p1_leader(HANDLE), 0)

        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
