import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import headless_menu_shell as live


class FakePartyProcess:
    def __init__(self, actor: int, party_slot: int):
        self.manager = 0x5000
        self.bwei = 0x6000
        self.actor = actor
        self.party_slot = party_slot

    def u64(self, address):
        if address == live.legacy.coop.ACTOR_MANAGER_GLOBAL:
            return self.manager
        if address == self.manager + 8:
            return self.bwei
        if address == self.bwei + 0x30:
            return self.actor
        return 0

    def u32(self, address):
        return 1 if address == self.manager else 0

    def read(self, address, size):
        if address == self.bwei + 0x12 and size == 1:
            return bytes((self.party_slot,))
        raise OSError(address)


class FakeHandoffProcess:
    def __init__(self, value):
        self.value = value

    def u32(self, address):
        expected = (
            live.legacy.coop.P2_DEVICE_FILTER_STATE
            + live.legacy.coop.NATIVE_LEADER_HANDOFF_ACTIVE_OFFSET
        )
        if address != expected:
            raise OSError(address)
        return self.value


class CharacterThemeActorIdentityTests(unittest.TestCase):
    def test_live_actor_wins_over_stale_role_or_slot_identity(self):
        active_party = [
            (0, "Vaan", 0x100001, 0xAAA0),
            (3, "Balthier", 0x100002, 0xBBB0),
            (4, "Basch", 0x100003, 0xCCC0),
        ]
        self.assertEqual(
            live.member_id_for_actor(active_party, 0xAAA0, fallback=3),
            0,
        )
        self.assertEqual(
            live.member_id_for_actor(active_party, 0xCCC0, fallback=3),
            4,
        )

    def test_missing_actor_uses_safe_session_fallback(self):
        self.assertEqual(
            live.member_id_for_actor([], 0, fallback=5),
            5,
        )

    def test_native_actor_slot_bypasses_a_stale_active_party_cache(self):
        actor = 0xAAA0
        process = FakePartyProcess(actor, party_slot=1)
        self.assertEqual(
            live.member_id_for_actor_slot(
                process,
                actor,
                (0, 4, live.session_state.EMPTY_PARTY_ID),
            ),
            4,
        )

    def test_transient_cross_role_actor_pointer_cannot_change_menu_identity(self):
        runtime = live.CoopMenuRuntime.__new__(live.CoopMenuRuntime)
        runtime.process = mock.Mock()
        runtime.process.u64.return_value = 0xCCC0
        runtime.base = 0x500000
        runtime.active_party = [
            (0, "Vaan", 0x100001, 0xAAA0),
            (3, "Balthier", 0x100002, 0xBBB0),
            (4, "Basch", 0x100003, 0xCCC0),
        ]
        runtime.session_snapshot = SimpleNamespace(
            identity=live.session_state.SessionIdentity(0, 3, 4),
            p1_owner_party_member_id=0,
            current_layout=(0, 3, 4, live.session_state.EMPTY_PARTY_ID),
        )

        self.assertEqual(runtime._controlled_member_id(2), 3)

    def test_hud_publisher_uses_controlled_actor_identity(self):
        source = inspect.getsource(live.CoopMenuRuntime.sync_presenter_themes)
        self.assertIn("p1_hud_member_id", source)
        self.assertIn("p2_hud_member_id", source)
        self.assertIn("p3_hud_member_id", source)
        self.assertNotIn("identity.p1_party_member_id", source)
        self.assertNotIn("identity.p2_party_member_id", source)

    def test_removed_party_member_cannot_publish_a_stale_hud_actor(self):
        movement_source = (
            Path(__file__).resolve().parents[1] / "source" / "coop_runtime.py"
        ).read_text(encoding="utf-8")
        self.assertIn("p1_removed", movement_source)
        self.assertIn("p2_removed", movement_source)
        self.assertIn("if p1_removed:", movement_source)
        self.assertIn("if p2_removed:", movement_source)
        self.assertIn("p1_actor = 0", movement_source)
        self.assertIn("p2_actor = 0", movement_source)

    def test_nontransactional_native_leader_change_waits_for_status_settle(self):
        snapshot = SimpleNamespace(
            p1_owner_party_member_id=4,
            leader_party_member_id=4,
            identity=live.session_state.SessionIdentity(0, 5),
        )
        replacement = live.session_state.SessionIdentity(4, 5)
        self.assertEqual(
            live.party_identity_reconcile_delay(snapshot, replacement),
            live.PARTY_IDENTITY_RECONCILE_SECONDS,
        )

    def test_transient_area_handoff_suppresses_role_reconciliation(self):
        live.legacy.coop.set_dual_interaction_base(0x51000000)
        self.assertTrue(
            live.transient_native_leader_handoff_active(FakeHandoffProcess(1))
        )
        self.assertFalse(
            live.transient_native_leader_handoff_active(FakeHandoffProcess(0))
        )

    def test_p2_selector_filters_live_and_reserved_p1_owners(self):
        provider_source = inspect.getsource(live.CoopMenuRuntime.target_provider)
        selection_source = inspect.getsource(live.CoopMenuRuntime.select_coop_character)
        self.assertIn("p1_owner_party_member_id", provider_source)
        self.assertIn("p1_owner_party_member_id", selection_source)
        self.assertIn("p1_reserved_party_member_id", provider_source)
        self.assertIn("p1_reserved_party_member_id", selection_source)


if __name__ == "__main__":
    unittest.main()
