from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import atomic_files
import runtime_paths


AREA_SLOT_TABLE = 0x022C6D28
AREA_SLOT_COUNT = 5
AREA_SLOT_EMPTY = 0xFFFFFFFF
U32_MAX = 0xFFFFFFFF


JUMP_REQUEST_TARGET = 0x021654C4
JUMP_REQUEST_SPAWN = 0x021654C8
JUMP_REQUEST_PREVIOUS = 0x021654CC


PENDING_AREA_GLOBAL = 0x01EED064
PENDING_AREA_NONE = 0xFFFFFFFF


GATEWAY_MATCH_RADIUS = 6.0

GATEWAY_MEMORY_PER_AREA = 12
GATEWAY_CACHE_VERSION = 1
DEFAULT_GATEWAY_CACHE = (
    runtime_paths.resolve_runtime_directory(__file__)
    / "runtime_status"
    / "gateway_routes.json"
)


def _u32(process, address):
    return struct.unpack("<I", process.read(address, 4))[0]


def read_area_slots(process):
    try:
        raw = process.read(AREA_SLOT_TABLE, 4 * AREA_SLOT_COUNT)
    except OSError:
        return None
    try:
        return struct.unpack("<%dI" % AREA_SLOT_COUNT, raw)
    except struct.error:
        return None


def current_area(process):
    slots = read_area_slots(process)
    if not slots:
        return None
    area = slots[0]
    return None if area == AREA_SLOT_EMPTY else int(area)


def _pending_area_sample(process):
    try:
        value = _u32(process, PENDING_AREA_GLOBAL)
    except (OSError, struct.error):
        return False, None
    return True, (None if value == PENDING_AREA_NONE else int(value))


def pending_area(process):
    _valid, value = _pending_area_sample(process)
    return value


def read_request(process):
    try:
        first = process.read(JUMP_REQUEST_TARGET, 12)
        second = process.read(JUMP_REQUEST_TARGET, 12)
    except OSError:
        return None
    if first != second:
        return None
    try:
        target, spawn, previous = struct.unpack("<3I", first)
    except struct.error:
        return None
    return (int(target), int(spawn), int(previous))


def request_transition(process, target_area, spawn_point):
    if (
        isinstance(target_area, bool)
        or not isinstance(target_area, int)
        or isinstance(spawn_point, bool)
        or not isinstance(spawn_point, int)
    ):
        return False
    if not 0 <= target_area < AREA_SLOT_EMPTY:
        return False
    if not 0 <= spawn_point <= U32_MAX:
        return False
    try:
        process.write(JUMP_REQUEST_TARGET, struct.pack("<I", target_area))
        process.write(JUMP_REQUEST_SPAWN, struct.pack("<I", spawn_point))
        process.write(PENDING_AREA_GLOBAL, struct.pack("<I", target_area))
    except (OSError, struct.error):
        return False
    return True


class GatewayMemory:
    def __init__(
        self,
        match_radius=GATEWAY_MATCH_RADIUS,
        per_area_limit=GATEWAY_MEMORY_PER_AREA,
        storage_path=None,
    ):
        self.match_radius_squared = float(match_radius) ** 2
        self.per_area_limit = int(per_area_limit)
        self.storage_path = Path(storage_path) if storage_path is not None else None

        self.routes = {}
        self._load()

    def _load(self):
        if self.storage_path is None:
            return
        try:
            payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(payload, dict):
            return
        if payload.get("version") != GATEWAY_CACHE_VERSION:
            return
        raw_routes = payload.get("routes")
        if not isinstance(raw_routes, dict):
            return

        loaded = {}
        for source_text, rows in raw_routes.items():
            try:
                source = int(source_text)
            except (TypeError, ValueError):
                continue
            if not isinstance(rows, list):
                continue
            valid_rows = []
            for row in rows:
                if not isinstance(row, list) or len(row) != 4:
                    continue
                try:
                    x = float(row[0])
                    z = float(row[1])
                except (TypeError, ValueError, OverflowError):
                    continue
                if (
                    isinstance(row[2], bool)
                    or not isinstance(row[2], int)
                    or isinstance(row[3], bool)
                    or not isinstance(row[3], int)
                ):
                    continue
                target = row[2]
                spawn = row[3]
                if not (math.isfinite(x) and math.isfinite(z)):
                    continue
                if not 0 <= target < AREA_SLOT_EMPTY or not 0 <= spawn <= U32_MAX:
                    continue
                valid_rows.append([x, z, target, spawn])
            if valid_rows:
                loaded[source] = valid_rows[-self.per_area_limit :]
        self.routes = loaded

    def _save(self):
        if self.storage_path is None:
            return
        payload = {
            "version": GATEWAY_CACHE_VERSION,
            "routes": {
                str(source): routes for source, routes in sorted(self.routes.items())
            },
        }
        try:
            atomic_files.write_text(
                self.storage_path,
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    @staticmethod
    def _horizontal(position):
        if position is None:
            return None
        try:
            x = float(position[0])
            z = float(position[2])
        except (TypeError, IndexError, ValueError, OverflowError):
            return None
        if not (math.isfinite(x) and math.isfinite(z)):
            return None
        return (x, z)

    def observe(self, source_area, position, target_area, spawn_point):
        if source_area is None or target_area is None:
            return False
        point = self._horizontal(position)
        if point is None:
            return False
        if (
            isinstance(source_area, bool)
            or not isinstance(source_area, int)
            or isinstance(target_area, bool)
            or not isinstance(target_area, int)
            or isinstance(spawn_point, bool)
            or not isinstance(spawn_point, int)
        ):
            return False
        if (
            not 0 <= source_area < AREA_SLOT_EMPTY
            or not 0 <= target_area < AREA_SLOT_EMPTY
            or not 0 <= spawn_point <= U32_MAX
        ):
            return False
        if source_area == target_area:
            return False

        routes = self.routes.setdefault(source_area, [])
        for route in routes:
            same_place = (route[0] - point[0]) ** 2 + (
                route[1] - point[1]
            ) ** 2 <= self.match_radius_squared
            if same_place and route[2] == target_area:
                route[0] = point[0]
                route[1] = point[1]
                route[3] = spawn_point
                self._save()
                return True

        routes.append([point[0], point[1], target_area, spawn_point])
        if len(routes) > self.per_area_limit:
            routes.pop(0)
        self._save()
        return True

    def lookup(self, source_area, position):
        if source_area is None:
            return None
        point = self._horizontal(position)
        if point is None:
            return None
        routes = self.routes.get(int(source_area))
        if not routes:
            return None

        matches = []
        for route in routes:
            distance_squared = (route[0] - point[0]) ** 2 + (route[1] - point[1]) ** 2
            if distance_squared <= self.match_radius_squared:
                matches.append((distance_squared, route))
        if not matches:
            return None

        destinations = {(route[2], route[3]) for _, route in matches}
        if len(destinations) > 1:
            return None
        return (int(matches[0][1][2]), int(matches[0][1][3]))


class AreaTransitionWatcher:
    def __init__(self, memory=None):
        self.memory = memory if memory is not None else GatewayMemory()
        self.area = None
        self.leader_position = None

    def sample(self, process, leader_position):
        area = current_area(process)
        if area is None:
            return None

        if self.area is None:
            self.area = area
        elif area != self.area:
            request = read_request(process)
            if request is not None:
                target, spawn, previous = request

                if target == area and previous == self.area:
                    self.memory.observe(self.area, self.leader_position, target, spawn)
            self.area = area
            self.leader_position = None

        if leader_position is not None:
            self.leader_position = tuple(leader_position)
        return area

    def route_for(self, position):
        return self.memory.lookup(self.area, position)


class FollowerTransitionController:
    def __init__(
        self,
        watcher=None,
        cooldown_seconds=3.0,
        requester=None,
    ):
        self.watcher = watcher if watcher is not None else AreaTransitionWatcher()
        self.cooldown_seconds = float(cooldown_seconds)
        self.requester = requester if requester is not None else request_transition
        self.blocked_until = 0.0
        self.last_request = None

    def consider(self, process, player, position, triggered, now):
        if not triggered:
            return False
        if float(now) < self.blocked_until:
            return False

        pending_valid, pending = _pending_area_sample(process)
        if not pending_valid or pending is not None:
            return False

        route = self.watcher.route_for(position)
        if route is None:
            return False

        target, spawn = route
        if not self.requester(process, target, spawn):
            return False

        self.blocked_until = float(now) + self.cooldown_seconds
        self.last_request = (int(player), target, spawn)
        print(
            "FOLLOWER_AREA_TRANSITION_REQUEST "
            f"p={int(player)} area={target} spawn={spawn}",
            flush=True,
        )
        return True
