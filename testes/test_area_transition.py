"""A stalled follower must reach the next area without a leader swap.

Measured live before this was written: writing target, spawn and the pending
global replaced the room twice in a row, and the engine reset the pending word
to -1 exactly as FUN_00314020 does.  The old design promoted the follower to
native leader instead, which cost a visible delay and Runtime Errors.

The destination is the hard part, not the request: nothing tells us which door
a follower is pushing against.  These tests pin the deliberate answer -- learn
gateways from transitions the game performs by itself, and stay silent when the
spot is unknown or ambiguous, because a wrong guess teleports the whole party.
"""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

import area_transition as at


class FakeProcess:
    def __init__(self):
        self.data = {}
        self.writes = []
        self.readable = True

    def put_u32(self, address, value):
        for offset, byte in enumerate(struct.pack("<I", value)):
            self.data[address + offset] = byte

    def read(self, address, size):
        if not self.readable:
            raise OSError(5, "unreadable")
        return bytes(self.data.get(address + o, 0) for o in range(size))

    def write(self, address, payload):
        payload = bytes(payload)
        self.writes.append((address, payload))
        for offset, byte in enumerate(payload):
            self.data[address + offset] = byte

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]


def idle_process(area=349):
    process = FakeProcess()
    process.put_u32(at.AREA_SLOT_TABLE, area)
    for slot in range(1, at.AREA_SLOT_COUNT):
        process.put_u32(at.AREA_SLOT_TABLE + 4 * slot, at.AREA_SLOT_EMPTY)
    process.put_u32(at.PENDING_AREA_GLOBAL, at.PENDING_AREA_NONE)
    return process


class RequestTransitionTest(unittest.TestCase):
    def test_writes_target_spawn_and_arms_pending_last(self):
        process = idle_process()

        self.assertTrue(at.request_transition(process, 350, 1))

        addresses = [address for address, _ in process.writes]
        self.assertEqual(
            addresses,
            [
                at.JUMP_REQUEST_TARGET,
                at.JUMP_REQUEST_SPAWN,
                at.PENDING_AREA_GLOBAL,
            ],
        )
        # The executor polls the pending word; arming it before target and
        # spawn are in place would run the transition against stale values.
        self.assertEqual(addresses[-1], at.PENDING_AREA_GLOBAL)
        self.assertEqual(process.u32(at.JUMP_REQUEST_TARGET), 350)
        self.assertEqual(process.u32(at.JUMP_REQUEST_SPAWN), 1)
        self.assertEqual(process.u32(at.PENDING_AREA_GLOBAL), 350)

    def test_rejects_the_empty_marker_as_a_destination(self):
        process = idle_process()
        self.assertFalse(at.request_transition(process, at.AREA_SLOT_EMPTY, 1))
        self.assertEqual(process.writes, [])

    def test_rejects_negative_values(self):
        process = idle_process()
        self.assertFalse(at.request_transition(process, -1, 1))
        self.assertFalse(at.request_transition(process, 350, -1))
        self.assertEqual(process.writes, [])

    def test_rejects_values_that_do_not_fit_the_native_mailbox(self):
        process = idle_process()
        self.assertFalse(at.request_transition(process, 1 << 32, 1))
        self.assertFalse(at.request_transition(process, 350, 1 << 32))
        self.assertFalse(at.request_transition(process, float("nan"), 1))
        self.assertFalse(at.request_transition(process, 350.0, 1))
        self.assertFalse(at.request_transition(process, 350, True))
        self.assertEqual(process.writes, [])

    def test_unreadable_process_is_not_a_crash(self):
        process = idle_process()
        self.assertIsNone(at.read_area_slots(_unreadable(process)))
        self.assertIsNone(at.current_area(_unreadable(process)))
        self.assertIsNone(at.pending_area(_unreadable(process)))
        self.assertIsNone(at.read_request(_unreadable(process)))


def _unreadable(process):
    process.readable = False
    return process


class AreaStateTest(unittest.TestCase):
    def test_current_area_reads_slot_zero(self):
        self.assertEqual(at.current_area(idle_process(305)), 305)

    def test_empty_slot_zero_reads_as_unknown(self):
        process = idle_process()
        process.put_u32(at.AREA_SLOT_TABLE, at.AREA_SLOT_EMPTY)
        self.assertIsNone(at.current_area(process))

    def test_pending_area_is_none_while_idle(self):
        self.assertIsNone(at.pending_area(idle_process()))

    def test_pending_area_reports_a_queued_transition(self):
        process = idle_process()
        process.put_u32(at.PENDING_AREA_GLOBAL, 350)
        self.assertEqual(at.pending_area(process), 350)

    def test_torn_jump_request_is_not_treated_as_a_gateway(self):
        class TornRequestProcess(FakeProcess):
            def __init__(self):
                super().__init__()
                self.request_reads = 0

            def read(self, address, size):
                payload = super().read(address, size)
                if address == at.JUMP_REQUEST_TARGET and size == 12:
                    self.request_reads += 1
                    if self.request_reads == 2:
                        changed = bytearray(payload)
                        struct.pack_into("<I", changed, 4, 99)
                        return bytes(changed)
                return payload

        process = TornRequestProcess()
        process.put_u32(at.JUMP_REQUEST_TARGET, 350)
        process.put_u32(at.JUMP_REQUEST_SPAWN, 1)
        process.put_u32(at.JUMP_REQUEST_PREVIOUS, 349)

        self.assertIsNone(at.read_request(process))


class GatewayMemoryTest(unittest.TestCase):
    def test_remembers_where_a_transition_happened(self):
        memory = at.GatewayMemory()
        memory.observe(349, (10.0, 0.0, 20.0), 350, 1)

        self.assertEqual(memory.lookup(349, (10.0, 0.0, 20.0)), (350, 1))

    def test_matches_a_nearby_approach_to_the_same_doorway(self):
        memory = at.GatewayMemory(match_radius=6.0)
        memory.observe(349, (10.0, 0.0, 20.0), 350, 1)

        self.assertEqual(memory.lookup(349, (13.0, 5.0, 22.0)), (350, 1))

    def test_ignores_a_spot_that_was_never_seen(self):
        memory = at.GatewayMemory(match_radius=6.0)
        memory.observe(349, (10.0, 0.0, 20.0), 350, 1)

        self.assertIsNone(memory.lookup(349, (90.0, 0.0, 90.0)))

    def test_two_exits_from_one_room_stay_separate(self):
        memory = at.GatewayMemory(match_radius=4.0)
        memory.observe(349, (10.0, 0.0, 20.0), 350, 1)
        memory.observe(349, (60.0, 0.0, 20.0), 212, 3)

        self.assertEqual(memory.lookup(349, (10.0, 0.0, 20.0)), (350, 1))
        self.assertEqual(memory.lookup(349, (60.0, 0.0, 20.0)), (212, 3))

    def test_an_ambiguous_spot_answers_nothing(self):
        # Two gateways close enough that both match: guessing here would send
        # the party somewhere the follower never asked for.
        memory = at.GatewayMemory(match_radius=10.0)
        memory.observe(349, (10.0, 0.0, 20.0), 350, 1)
        memory.observe(349, (12.0, 0.0, 20.0), 212, 3)

        self.assertIsNone(memory.lookup(349, (11.0, 0.0, 20.0)))

    def test_knowledge_does_not_leak_between_areas(self):
        memory = at.GatewayMemory()
        memory.observe(349, (10.0, 0.0, 20.0), 350, 1)

        self.assertIsNone(memory.lookup(212, (10.0, 0.0, 20.0)))

    def test_recrossing_refreshes_the_remembered_spot(self):
        memory = at.GatewayMemory(match_radius=6.0)
        memory.observe(349, (10.0, 0.0, 20.0), 350, 1)
        memory.observe(349, (12.0, 0.0, 21.0), 350, 4)

        self.assertEqual(len(memory.routes[349]), 1)
        self.assertEqual(memory.lookup(349, (12.0, 0.0, 21.0)), (350, 4))

    def test_a_transition_onto_itself_is_not_a_gateway(self):
        memory = at.GatewayMemory()
        self.assertFalse(memory.observe(349, (1.0, 0.0, 2.0), 349, 1))

    def test_non_finite_positions_are_refused(self):
        memory = at.GatewayMemory()
        self.assertFalse(memory.observe(349, (float("nan"), 0.0, 2.0), 350, 1))
        self.assertFalse(memory.observe(349, None, 350, 1))
        self.assertIsNone(memory.lookup(349, (float("inf"), 0.0, 2.0)))

    def test_memory_stays_bounded(self):
        memory = at.GatewayMemory(match_radius=0.5, per_area_limit=3)
        for step in range(10):
            memory.observe(349, (step * 50.0, 0.0, 0.0), 400 + step, 1)

        self.assertEqual(len(memory.routes[349]), 3)

    def test_routes_survive_a_runtime_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway_routes.json"
            memory = at.GatewayMemory(storage_path=path)
            memory.observe(349, (10.0, 0.0, 20.0), 350, 1)

            reloaded = at.GatewayMemory(storage_path=path)
            self.assertEqual(
                reloaded.lookup(349, (10.0, 0.0, 20.0)),
                (350, 1),
            )

    def test_a_corrupt_route_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway_routes.json"
            path.write_text("not json", encoding="utf-8")

            memory = at.GatewayMemory(storage_path=path)
            self.assertEqual(memory.routes, {})

    def test_non_object_and_out_of_range_route_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway_routes.json"
            path.write_text("[]", encoding="utf-8")
            self.assertEqual(at.GatewayMemory(storage_path=path).routes, {})

            path.write_text(
                '{"version":1,"routes":{"349":[[1,2,4294967296,0]]}}',
                encoding="utf-8",
            )
            self.assertEqual(at.GatewayMemory(storage_path=path).routes, {})

    def test_observe_rejects_an_unrepresentable_route(self):
        memory = at.GatewayMemory()
        self.assertFalse(
            memory.observe(349, (1.0, 0.0, 2.0), 1 << 32, 0)
        )
        self.assertFalse(
            memory.observe(349, (1.0, 0.0, 2.0), 350, 1 << 32)
        )
        self.assertEqual(memory.routes, {})


class AreaTransitionWatcherTest(unittest.TestCase):
    def test_learns_a_gateway_from_a_transition_the_game_performed(self):
        watcher = at.AreaTransitionWatcher()
        process = idle_process(349)
        watcher.sample(process, (10.0, 0.0, 20.0))

        # The room is replaced and the request block describes the crossing.
        process.put_u32(at.AREA_SLOT_TABLE, 350)
        process.put_u32(at.JUMP_REQUEST_TARGET, 350)
        process.put_u32(at.JUMP_REQUEST_SPAWN, 1)
        process.put_u32(at.JUMP_REQUEST_PREVIOUS, 349)
        watcher.sample(process, (99.0, 0.0, 99.0))

        self.assertEqual(watcher.memory.lookup(349, (10.0, 0.0, 20.0)), (350, 1))

    def test_uses_the_position_from_before_the_room_changed(self):
        # By the time the new room is up the leader already stands on the spawn
        # point, so sampling then would remember the wrong side of the door.
        watcher = at.AreaTransitionWatcher()
        process = idle_process(349)
        watcher.sample(process, (10.0, 0.0, 20.0))

        process.put_u32(at.AREA_SLOT_TABLE, 350)
        process.put_u32(at.JUMP_REQUEST_TARGET, 350)
        process.put_u32(at.JUMP_REQUEST_SPAWN, 1)
        process.put_u32(at.JUMP_REQUEST_PREVIOUS, 349)
        watcher.sample(process, (500.0, 0.0, 500.0))

        self.assertIsNone(watcher.memory.lookup(349, (500.0, 0.0, 500.0)))

    def test_a_room_change_we_cannot_explain_teaches_nothing(self):
        # Loading a save or a scripted warp changes the room without the block
        # describing a crossing from where we were.
        watcher = at.AreaTransitionWatcher()
        process = idle_process(349)
        watcher.sample(process, (10.0, 0.0, 20.0))

        process.put_u32(at.AREA_SLOT_TABLE, 350)
        process.put_u32(at.JUMP_REQUEST_TARGET, 777)
        process.put_u32(at.JUMP_REQUEST_SPAWN, 1)
        process.put_u32(at.JUMP_REQUEST_PREVIOUS, 111)
        watcher.sample(process, (99.0, 0.0, 99.0))

        self.assertEqual(watcher.memory.routes, {})


class FollowerTransitionControllerTest(unittest.TestCase):
    def _armed(self, area=349, spot=(10.0, 0.0, 20.0)):
        controller = at.FollowerTransitionController()
        controller.watcher.area = area
        controller.watcher.memory.observe(area, spot, 350, 1)
        return controller

    def test_a_stalled_follower_on_a_known_gateway_requests_it(self):
        controller = self._armed()
        process = idle_process(349)

        self.assertTrue(controller.consider(process, 2, (10.0, 0.0, 20.0), True, 100.0))
        self.assertEqual(process.u32(at.PENDING_AREA_GLOBAL), 350)
        self.assertEqual(controller.last_request, (2, 350, 1))

    def test_nothing_happens_without_a_stall(self):
        controller = self._armed()
        process = idle_process(349)

        self.assertFalse(
            controller.consider(process, 2, (10.0, 0.0, 20.0), False, 100.0)
        )
        self.assertEqual(process.writes, [])

    def test_an_unknown_gateway_is_left_alone(self):
        controller = self._armed()
        process = idle_process(349)

        self.assertFalse(
            controller.consider(process, 2, (90.0, 0.0, 90.0), True, 100.0)
        )
        self.assertEqual(process.writes, [])

    def test_never_stacks_onto_a_transition_already_running(self):
        controller = self._armed()
        process = idle_process(349)
        process.put_u32(at.PENDING_AREA_GLOBAL, 212)

        self.assertFalse(
            controller.consider(process, 2, (10.0, 0.0, 20.0), True, 100.0)
        )
        self.assertEqual(process.writes, [])

    def test_unreadable_pending_mailbox_fails_closed(self):
        calls = []

        def requester(*args):
            calls.append(args)
            return True

        controller = self._armed()
        controller.requester = requester
        process = _unreadable(idle_process(349))

        self.assertFalse(
            controller.consider(process, 2, (10.0, 0.0, 20.0), True, 100.0)
        )
        self.assertEqual(calls, [])
        self.assertEqual(process.writes, [])

    def test_cooldown_absorbs_a_held_stick_across_the_reload(self):
        # The world takes seconds to rebuild and the stall detector keeps
        # firing meanwhile; without this the party would jump repeatedly.
        controller = self._armed()
        process = idle_process(349)
        self.assertTrue(controller.consider(process, 2, (10.0, 0.0, 20.0), True, 100.0))
        process.put_u32(at.PENDING_AREA_GLOBAL, at.PENDING_AREA_NONE)

        self.assertFalse(
            controller.consider(process, 2, (10.0, 0.0, 20.0), True, 101.0)
        )
        self.assertTrue(controller.consider(process, 2, (10.0, 0.0, 20.0), True, 104.0))

    def test_p3_uses_the_same_route_as_p2(self):
        controller = self._armed()
        process = idle_process(349)

        self.assertTrue(controller.consider(process, 3, (10.0, 0.0, 20.0), True, 100.0))
        self.assertEqual(controller.last_request, (3, 350, 1))

    def test_production_can_inject_a_native_requester(self):
        calls = []

        def requester(process, target, spawn):
            calls.append((process, target, spawn))
            return 7

        controller = at.FollowerTransitionController(requester=requester)
        controller.watcher.area = 349
        controller.watcher.memory.observe(
            349,
            (10.0, 0.0, 20.0),
            350,
            1,
        )
        process = idle_process(349)

        self.assertTrue(
            controller.consider(
                process,
                2,
                (10.0, 0.0, 20.0),
                True,
                100.0,
            )
        )
        self.assertEqual(calls, [(process, 350, 1)])
        self.assertEqual(process.writes, [])


class NoLeaderSwapTest(unittest.TestCase):
    def test_the_module_never_touches_party_leadership(self):
        # The whole point of this route: the old controller promoted the
        # follower to native leader and restored P1 afterwards.  Nothing here
        # may reintroduce that.
        import inspect

        source = inspect.getsource(at)
        for forbidden in (
            "native_leader",
            "leader_refresh",
            "party_member_id",
            "BATTLE_LEADER",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
