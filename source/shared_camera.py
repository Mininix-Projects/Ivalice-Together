from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


Vec3 = tuple[float, float, float]


@dataclass(frozen=True)
class CameraTuning:
    target_height: float = 1.30

    centre_deadzone: float = 0.03
    vertical_deadzone: float = 0.10

    vertical_upper_bias: float = 0.85

    centre_settle_seconds: float = 0.30
    velocity_filter_seconds: float = 0.12
    lead_seconds: float = 0.12
    max_lead: float = 0.90
    separation_deadzone: float = 4.0
    zoom_per_unit: float = 1.05
    max_extra_distance: float = 24.0
    zoom_out_seconds: float = 0.10
    zoom_in_seconds: float = 0.42
    vertical_separation_cap: float = 6.0
    vertical_separation_weight: float = 0.30
    maximum_sample_speed: float = 30.0
    position_sample_epsilon: float = 0.0005
    velocity_sample_stale_seconds: float = 0.09
    collision_enter_pressure: float = 0.75
    collision_hold_seconds: float = 0.45
    collision_recover_units_per_second: float = 6.0
    native_wall_hold_seconds: float = 0.08
    native_wall_recover_units_per_second: float = 16.0
    native_wall_release_distance: float = 1.5
    collision_active_pressure: float = 0.15
    collision_enter_ratio: float = 0.16
    collision_active_ratio: float = 0.08
    collision_clearance: float = 0.10
    collision_minimum_boom: float = 1.0

    obstruction_enter_ratio: float = 0.22
    obstruction_exit_ratio: float = 0.08
    obstruction_enter_seconds: float = 0.06
    obstruction_exit_seconds: float = 0.32
    orbit_probe_seconds: float = 0.12
    orbit_probe_step: float = 0.13
    orbit_max_radians: float = 0.48
    orbit_settle_seconds: float = 0.14
    orbit_return_seconds: float = 0.28
    orbit_score_epsilon: float = 0.025
    pitch_max_radians: float = 0.16
    pitch_settle_seconds: float = 0.12
    vertical_rise_settle_seconds: float = 0.18
    vertical_fall_settle_seconds: float = 0.08
    terrain_transition_vertical_speed: float = 0.45
    terrain_transition_full_vertical_speed: float = 1.20
    terrain_transition_blend_rise_seconds: float = 0.015
    terrain_transition_blend_fall_seconds: float = 0.24
    terrain_transition_lead_scale: float = 0.10
    terrain_transition_deadzone: float = 0.035
    terrain_transition_horizontal_settle_seconds: float = 0.18
    terrain_transition_vertical_settle_seconds: float = 0.10
    native_boom_collapse_ratio: float = 0.28
    native_boom_recover_ratio: float = 0.92
    native_boom_reference_decay_per_second: float = 1.5
    # Exempt the space between P1 and P2 from camera collision. The game's sweep
    # is a volume rather than a zero-width ray: at 1.8 the recast still began
    # inside Rabanastre's thick wall and reduced the native boom from 17.59 to
    # 2.39. A wider corridor swallows walls just behind the party, precisely the
    # defect this avoids. Scaling it with separation was tested and rejected
    # because it reached walls six units behind the players.
    internal_collision_corridor_radius: float = 5.0
    # Floors and ceilings remain native even when crossing the party segment:
    # only hits near camera height are rejected. This axis can be generous
    # because the camera views the formation from above.
    internal_collision_vertical_radius: float = 3.25
    internal_collision_max_distance: float = 32.0
    # Beyond this length, pull both ends inward so a wide separation does not
    # exempt an entire corridor from geometry.
    internal_collision_corridor_length: float = 16.0
    # Do not publish an internal corridor below this separation.
    internal_collision_min_separation: float = 6.0
    # Radius ceiling as a fraction of the boom achieved by the game. A radius
    # reaching the camera covers the wall behind it, which then stops reporting.
    internal_collision_radius_boom_ratio: float = 0.75
    # Minimum room boom required to publish a corridor. In a long narrow room,
    # any active corridor reported a wall in 8% of frames because the walls run
    # along the same line as the players.
    internal_collision_min_boom: float = 8.0
    # Enabling the corridor lengthens the boom and disabling it shortens it, so
    # a single threshold feeds back and makes the camera oscillate. The boom must
    # fall by this additional amount before the corridor is released.
    internal_collision_boom_release: float = 6.0
    # How far to retreat when the native solver collapses under the extension.
    # It collapses in one frame, so the collapsed distance says nothing about
    # the prior working value. Retreating from the displayed value settles the
    # camera against the wall instead of making it bounce.
    collision_backoff_units: float = 0.75

    internal_latch_recent_seconds: float = 0.16
    internal_latch_max_suppression_seconds: float = 0.45
    internal_latch_collapse_ratio: float = 0.28
    internal_latch_recover_ratio: float = 0.92

    focus_collision_escape_min_actor_distance: float = 1.25
    focus_collision_escape_max_hit_distance: float = 5.30

    native_seed_max_distance: float = 16.0
    # Focus rescue remains disabled because the native signal also fires in open
    # corridors and would move the camera toward one player unnecessarily.
    focus_rescue_max_pull: float = 0.0
    focus_rescue_engage_seconds: float = 0.12
    focus_rescue_release_seconds: float = 0.60
    focus_rescue_min_separation: float = 7.0
    focus_rescue_boom_ratio: float = 0.55

    focus_rescue_hold_seconds: float = 1.2
    focus_rescue_hold_backoff: float = 2.0
    focus_rescue_hold_max_seconds: float = 5.0


@dataclass(frozen=True)
class CameraFrame:
    focus: Vec3
    extra_distance: float
    effective_separation: float
    lead: Vec3
    focus_velocity: Vec3 = (0.0, 0.0, 0.0)
    reset_epoch: int = 0


@dataclass(frozen=True)
class CameraObstructionSolution:
    extra_distance: float
    yaw_offset: float
    pitch_offset: float
    blocked_ratio: float
    active: bool


@dataclass(frozen=True)
class CameraCollisionExclusion:
    start_distance: float
    end_distance: float

    @property
    def active(self) -> bool:
        return self.end_distance > self.start_distance + 1e-4


@dataclass(frozen=True)
class InternalCollisionLatchDecision:
    suppress: bool
    restored_native_boom: float | None
    activated: bool = False
    released: bool = False


class InternalCollisionLatchGuard:
    def __init__(self, tuning: CameraTuning | None = None) -> None:
        self.tuning = tuning or CameraTuning()
        self.reset()

    def reset(self) -> None:
        self.previous_counters: tuple[int, int, int, int] | None = None
        self.recent_internal_remaining = 0.0
        self.suppression_remaining = 0.0
        self.reference_native_boom: float | None = None
        self.suppressing = False

    @staticmethod
    def _counter_delta(current: int, previous: int) -> int:
        return (int(current) - int(previous)) & 0xFFFFFFFF

    def observe(
        self,
        counters: Sequence[int],
        native_boom: float,
        native_back_on_wall: bool,
        delta_seconds: float,
    ) -> InternalCollisionLatchDecision:
        dt = min(0.05, max(0.0, float(delta_seconds)))
        current = tuple(int(value) & 0xFFFFFFFF for value in counters[:4])
        native = float(native_boom)
        if len(current) != 4 or not math.isfinite(native):
            self.reset()
            return InternalCollisionLatchDecision(False, None)

        internal_delta = 0
        external_delta = 0
        if self.previous_counters is not None:
            internal_delta = self._counter_delta(current[0], self.previous_counters[0])
            external_delta = self._counter_delta(current[3], self.previous_counters[3])
        self.previous_counters = current

        released = False
        if external_delta:
            self.recent_internal_remaining = 0.0
            released = self.suppressing
            self.suppressing = False
            self.suppression_remaining = 0.0
        elif internal_delta:
            self.recent_internal_remaining = self.tuning.internal_latch_recent_seconds
        else:
            self.recent_internal_remaining = max(
                0.0, self.recent_internal_remaining - dt
            )

        if self.reference_native_boom is None:
            if not native_back_on_wall:
                self.reference_native_boom = native
            return InternalCollisionLatchDecision(False, None, released=released)

        reference = self.reference_native_boom
        collapsed = bool(
            reference > 3.0
            and native < reference * (1.0 - self.tuning.internal_latch_collapse_ratio)
        )
        activated = bool(
            not self.suppressing
            and not external_delta
            and self.recent_internal_remaining > 0.0
            and native_back_on_wall
            and collapsed
        )
        if activated:
            self.suppressing = True
            self.suppression_remaining = (
                self.tuning.internal_latch_max_suppression_seconds
            )

        if self.suppressing:
            if internal_delta:
                self.suppression_remaining = (
                    self.tuning.internal_latch_max_suppression_seconds
                )
            else:
                self.suppression_remaining = max(0.0, self.suppression_remaining - dt)
            recovered = bool(
                not native_back_on_wall
                and native >= reference * self.tuning.internal_latch_recover_ratio
            )
            if recovered or self.suppression_remaining <= 0.0:
                self.suppressing = False
                self.suppression_remaining = 0.0
                self.reference_native_boom = native
                return InternalCollisionLatchDecision(
                    False,
                    None,
                    activated=activated,
                    released=True,
                )
            return InternalCollisionLatchDecision(
                True,
                reference,
                activated=activated,
                released=released,
            )

        if not native_back_on_wall:
            self.reference_native_boom = native
        return InternalCollisionLatchDecision(
            False,
            None,
            activated=activated,
            released=released,
        )


def _horizontal_length(value: Sequence[float]) -> float:
    return math.hypot(float(value[0]), float(value[2]))


def _clamp_horizontal(value: Vec3, maximum: float) -> Vec3:
    length = _horizontal_length(value)
    if length <= maximum or length <= 1e-8:
        return value
    scale = maximum / length
    return value[0] * scale, value[1], value[2] * scale


def _spring_axis(
    current: float,
    velocity: float,
    target: float,
    delta_seconds: float,
    settle_seconds: float,
) -> tuple[float, float]:
    omega = 4.0 / max(0.01, settle_seconds)
    displacement = current - target
    temporary = (velocity + omega * displacement) * delta_seconds
    decay = math.exp(-omega * delta_seconds)
    return (
        target + (displacement + temporary) * decay,
        (velocity - omega * temporary) * decay,
    )


def _linear_interval(
    offset: float,
    slope: float,
    lower: float,
    upper: float,
) -> tuple[float, float] | None:
    if abs(slope) <= 1e-8:
        if lower <= offset <= upper:
            return -math.inf, math.inf
        return None
    first = (lower - offset) / slope
    second = (upper - offset) / slope
    return min(first, second), max(first, second)


def _ray_capsule_interval(
    origin: tuple[float, float],
    direction: tuple[float, float],
    segment_start: tuple[float, float],
    segment_end: tuple[float, float],
    radius: float,
) -> tuple[float, float] | None:
    intervals: list[tuple[float, float]] = []

    def add_circle(centre: tuple[float, float]) -> None:
        relative_x = centre[0] - origin[0]
        relative_z = centre[1] - origin[1]
        projection = relative_x * direction[0] + relative_z * direction[1]
        perpendicular_squared = max(
            0.0,
            relative_x * relative_x + relative_z * relative_z - projection * projection,
        )
        discriminant = radius * radius - perpendicular_squared
        if discriminant < 0.0:
            return
        half_width = math.sqrt(max(0.0, discriminant))
        intervals.append((projection - half_width, projection + half_width))

    add_circle(segment_start)
    add_circle(segment_end)

    segment_x = segment_end[0] - segment_start[0]
    segment_z = segment_end[1] - segment_start[1]
    segment_length = math.hypot(segment_x, segment_z)
    if segment_length > 1e-8:
        axis_x = segment_x / segment_length
        axis_z = segment_z / segment_length
        normal_x = -axis_z
        normal_z = axis_x
        relative_x = origin[0] - segment_start[0]
        relative_z = origin[1] - segment_start[1]
        along_interval = _linear_interval(
            relative_x * axis_x + relative_z * axis_z,
            direction[0] * axis_x + direction[1] * axis_z,
            0.0,
            segment_length,
        )
        across_interval = _linear_interval(
            relative_x * normal_x + relative_z * normal_z,
            direction[0] * normal_x + direction[1] * normal_z,
            -radius,
            radius,
        )
        if along_interval is not None and across_interval is not None:
            entry = max(along_interval[0], across_interval[0])
            exit_distance = min(along_interval[1], across_interval[1])
            if entry <= exit_distance:
                intervals.append((entry, exit_distance))

    forward = [(max(0.0, start), end) for start, end in intervals if end >= 0.0]
    if not forward:
        return None

    return min(value[0] for value in forward), max(value[1] for value in forward)


def formation_corridor_radius(
    positions: Sequence[Sequence[float]],
    tuning: CameraTuning,
) -> float:
    del positions
    return max(0.1, float(tuning.internal_collision_corridor_radius))


def _ray_formation_interval(
    origin: tuple[float, float],
    direction: tuple[float, float],
    points: Sequence[tuple[float, float]],
    radius: float,
) -> tuple[float, float] | None:
    if len(points) < 2:
        return None
    intervals = []
    for first in range(len(points)):
        for second in range(first + 1, len(points)):
            interval = _ray_capsule_interval(
                origin, direction, points[first], points[second], radius
            )
            if interval is not None:
                intervals.append(interval)
    if not intervals:
        return None
    intervals.sort()
    start, end = intervals[0]
    for candidate_start, candidate_end in intervals[1:]:
        if candidate_start > end:
            break
        end = max(end, candidate_end)
    return start, end


class SharedCameraController:
    def __init__(self, tuning: CameraTuning | None = None) -> None:
        self.tuning = tuning or CameraTuning()
        self.reset_epoch = 0
        self.reset()

    def reset(self) -> None:
        self.reset_epoch = (self.reset_epoch + 1) & 0xFFFFFFFF
        if self.reset_epoch == 0:
            self.reset_epoch = 1
        self.focus: Vec3 | None = None
        self.focus_velocity: Vec3 = (0.0, 0.0, 0.0)
        self.frame_advanced = False
        self.frame_delta_seconds = 0.0
        self._last_present_tick_us: int | None = None
        self._last_camera_frame: CameraFrame | None = None

        self.previous_positions: list[Vec3 | None] = [None, None]
        self.velocity_sample_ticks_us: list[int | None] = [None, None]
        self.velocity_sample_ages: list[float] = [0.0, 0.0]
        self.sampled_velocities: list[Vec3] = [
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0),
        ]
        self.filtered_group_velocity: Vec3 = (0.0, 0.0, 0.0)
        self.extra_distance = 0.0
        self.collision_boom_cap: float | None = None
        self.collision_zoom_correction = 0.0
        self.collision_hold_remaining = 0.0
        self.collision_active = False
        self.native_wall_latched = False
        self.native_wall_extra = 0.0
        self.native_wall_clear_age = 0.0
        self.native_wall_anchor_focus: Vec3 | None = None
        self.last_output_extra = 0.0
        self.orbit_offset = 0.0
        self.orbit_velocity = 0.0
        self.orbit_target = 0.0
        self.orbit_direction = 1.0
        self.orbit_probe_age = 0.0
        self.orbit_previous_score: float | None = None
        self.pitch_offset = 0.0
        self.pitch_velocity = 0.0
        self.pitch_target = 0.0
        self.obstruction_enter_age = 0.0
        self.obstruction_exit_age = 0.0
        self.obstruction_active = False
        self.previous_native_boom: float | None = None
        self.native_boom_reference: float | None = None
        self.native_collapse_latched = False
        self.actors_are_moving = False
        self.terrain_transition_blend = 0.0
        self.focus_rescue_blend = 0.0
        self.focus_rescue_offset = 0.0
        self.focus_rescue_hold_remaining = 0.0
        self.focus_rescue_hold_length = 0.0
        self.focus_rescue_anchor: Vec3 | None = None

    def _ensure_player_slots(self, count: int) -> None:
        while len(self.previous_positions) < count:
            self.previous_positions.append(None)
            self.velocity_sample_ticks_us.append(None)
            self.velocity_sample_ages.append(0.0)
            self.sampled_velocities.append((0.0, 0.0, 0.0))

    @property
    def previous_p1(self) -> Vec3 | None:
        return self.previous_positions[0]

    @previous_p1.setter
    def previous_p1(self, value: Vec3 | None) -> None:
        self.previous_positions[0] = value

    @property
    def previous_p2(self) -> Vec3 | None:
        return self.previous_positions[1]

    @previous_p2.setter
    def previous_p2(self, value: Vec3 | None) -> None:
        self.previous_positions[1] = value

    def _shared_focus_target(self, *positions: Vec3) -> Vec3:
        count = len(positions)
        if not count:
            return (0.0, self.tuning.target_height, 0.0)
        vertical = [position[1] for position in positions]
        average_y = sum(vertical) / count
        upper_half_separation = max(vertical) - average_y
        return (
            sum(position[0] for position in positions) / count,
            average_y
            + upper_half_separation * self.tuning.vertical_upper_bias
            + self.tuning.target_height,
            sum(position[2] for position in positions) / count,
        )

    def note_focus_burial(
        self,
        actual_boom: float,
        native_boom: float,
        native_back_on_wall: bool,
        delta_seconds: float,
    ) -> float:
        dt = min(0.05, max(0.0, float(delta_seconds)))
        collapsed = (
            math.isfinite(actual_boom)
            and math.isfinite(native_boom)
            and actual_boom > 0.5
            and native_boom < actual_boom * self.tuning.focus_rescue_boom_ratio
        )

        known = [
            position for position in self.previous_positions if position is not None
        ]
        separation = self._widest_horizontal_separation(known)
        wide_enough = separation >= self.tuning.focus_rescue_min_separation
        buried = wide_enough and (bool(native_back_on_wall) or collapsed)
        if buried:
            if self.focus_rescue_blend > 0.05:
                self.focus_rescue_hold_length = min(
                    self.tuning.focus_rescue_hold_max_seconds,
                    max(
                        self.tuning.focus_rescue_hold_seconds,
                        self.focus_rescue_hold_length
                        * self.tuning.focus_rescue_hold_backoff,
                    ),
                )
            else:
                self.focus_rescue_hold_length = self.tuning.focus_rescue_hold_seconds
            self.focus_rescue_hold_remaining = self.focus_rescue_hold_length
        else:
            self.focus_rescue_hold_remaining = max(
                0.0, self.focus_rescue_hold_remaining - dt
            )
        target = 1.0 if self.focus_rescue_hold_remaining > 0.0 else 0.0
        seconds = (
            self.tuning.focus_rescue_engage_seconds
            if target > self.focus_rescue_blend
            else self.tuning.focus_rescue_release_seconds
        )
        alpha = 1.0 - math.exp(-dt / max(0.01, seconds))
        self.focus_rescue_blend += (target - self.focus_rescue_blend) * alpha
        if self.focus_rescue_blend < 1.0e-4 and target == 0.0:
            self.focus_rescue_blend = 0.0

            self.focus_rescue_anchor = None
            self.focus_rescue_hold_length = 0.0
        return self.focus_rescue_blend

    def _rescued_focus(self, centre: Vec3, positions: Sequence[Vec3]) -> Vec3:
        self.focus_rescue_offset = 0.0
        blend = self.focus_rescue_blend
        if blend <= 0.0 or self.tuning.focus_rescue_max_pull <= 0.0:
            return centre
        if len(positions) < 2:
            return centre
        if (
            self._widest_horizontal_separation(positions)
            < self.tuning.focus_rescue_min_separation
        ):
            return centre

        if self.focus_rescue_anchor is None:
            self.focus_rescue_anchor = min(
                positions,
                key=lambda position: math.hypot(
                    position[0] - centre[0], position[2] - centre[2]
                ),
            )
        else:
            self.focus_rescue_anchor = min(
                positions,
                key=lambda position: math.hypot(
                    position[0] - self.focus_rescue_anchor[0],
                    position[2] - self.focus_rescue_anchor[2],
                ),
            )
        nearest = self.focus_rescue_anchor
        amount = min(1.0, blend * self.tuning.focus_rescue_max_pull)
        rescued = (
            centre[0] + (nearest[0] - centre[0]) * amount,
            centre[1],
            centre[2] + (nearest[2] - centre[2]) * amount,
        )
        self.focus_rescue_offset = math.hypot(
            rescued[0] - centre[0], rescued[2] - centre[2]
        )
        return rescued

    @staticmethod
    def _widest_horizontal_separation(positions: Sequence[Vec3]) -> float:
        widest = 0.0
        for first in range(len(positions)):
            for second in range(first + 1, len(positions)):
                widest = max(
                    widest,
                    math.hypot(
                        positions[second][0] - positions[first][0],
                        positions[second][2] - positions[first][2],
                    ),
                )
        return widest

    def internal_collision_exclusion_group(
        self,
        positions: Sequence[Sequence[float]],
        camera_focus: Sequence[float],
        camera_direction: Sequence[float],
    ) -> CameraCollisionExclusion:
        try:
            party = [
                tuple(float(value) for value in position[:3]) for position in positions
            ]
            focus = tuple(float(value) for value in camera_focus[:3])
            direction_x = float(camera_direction[0])
            direction_z = float(camera_direction[2])
        except (TypeError, ValueError, IndexError):
            return CameraCollisionExclusion(0.0, 0.0)
        values = [*focus, direction_x, direction_z]
        for member in party:
            values.extend(member)
        if (
            len(party) < 2
            or any(len(member) != 3 for member in party)
            or len(focus) != 3
            or not all(math.isfinite(value) for value in values)
        ):
            return CameraCollisionExclusion(0.0, 0.0)
        direction_length = math.hypot(direction_x, direction_z)
        if direction_length <= 1e-5:
            return CameraCollisionExclusion(0.0, 0.0)
        direction = (
            direction_x / direction_length,
            direction_z / direction_length,
        )
        interval = _ray_formation_interval(
            (focus[0], focus[2]),
            direction,
            [(member[0], member[2]) for member in party],
            formation_corridor_radius(party, self.tuning),
        )
        if interval is None:
            return CameraCollisionExclusion(0.0, 0.0)
        maximum = max(0.0, self.tuning.internal_collision_max_distance)
        start = min(maximum, max(0.0, interval[0]))
        end = min(maximum, max(start, interval[1]))
        return CameraCollisionExclusion(start, end)

    def native_focus_distance_to_party(
        self,
        native_focus: Sequence[float],
        *positions: Sequence[float],
    ) -> float | None:
        try:
            native = tuple(float(value) for value in native_focus[:3])
            party = [
                tuple(float(value) for value in position[:3]) for position in positions
            ]
        except (TypeError, ValueError):
            return None
        if len(native) != 3 or not party or any(len(member) != 3 for member in party):
            return None
        target = self._shared_focus_target(*party)
        if not all(math.isfinite(value) for value in (*native, *target)):
            return None
        return math.sqrt(
            sum((native[index] - target[index]) ** 2 for index in range(3))
        )

    def solve_obstruction(
        self,
        actual_boom: float,
        native_boom: float,
        delta_seconds: float,
        *,
        new_native_sample: bool = True,
        allow_auto_orbit: bool = True,
        allow_collision_limiter: bool = False,
        native_back_on_wall: bool = False,
        wall_release_requested: bool = False,
    ) -> CameraObstructionSolution:
        self.note_focus_burial(
            actual_boom, native_boom, native_back_on_wall, delta_seconds
        )

        dt = min(0.05, max(0.0, float(delta_seconds)))
        actual = max(0.0, float(actual_boom))
        native = max(0.0, float(native_boom))
        previous_native = self.previous_native_boom
        catastrophic_collapse = bool(
            new_native_sample
            and previous_native is not None
            and previous_native > 3.0
            and native
            < previous_native * (1.0 - self.tuning.native_boom_collapse_ratio)
        )
        if self.native_boom_reference is None:
            self.native_boom_reference = native
        if native >= self.native_boom_reference:
            self.native_boom_reference = native
        elif not self.native_collapse_latched and not catastrophic_collapse:
            self.native_boom_reference = max(
                native,
                self.native_boom_reference
                - self.tuning.native_boom_reference_decay_per_second * dt,
            )
        if catastrophic_collapse:
            self.native_collapse_latched = True
            self.native_boom_reference = max(
                self.native_boom_reference,
                previous_native if previous_native is not None else native,
            )

        direct_desired_total = max(
            self.tuning.collision_minimum_boom,
            native + max(0.0, float(self.extra_distance)),
        )
        desired_total = direct_desired_total
        if self.native_collapse_latched:
            desired_total = max(
                desired_total,
                self.native_boom_reference + max(0.0, float(self.extra_distance)),
            )
        blocked_ratio = max(0.0, min(1.0, (desired_total - actual) / desired_total))

        if not allow_auto_orbit:
            self.obstruction_active = False
            self.obstruction_enter_age = 0.0
            self.obstruction_exit_age = 0.0
            self.orbit_target = 0.0
            self.orbit_offset = 0.0
            self.orbit_velocity = 0.0
            self.pitch_target = 0.0
            self.pitch_offset = 0.0
            self.pitch_velocity = 0.0
            self.orbit_previous_score = None
            self.orbit_probe_age = 0.0

            if (
                native_back_on_wall
                or self.native_wall_latched
                or allow_collision_limiter
            ):
                extra = self.collision_safe_extra_distance(
                    actual,
                    native,
                    dt,
                    new_native_sample=new_native_sample,
                    native_back_on_wall=native_back_on_wall,
                    wall_release_requested=wall_release_requested,
                )
            else:
                self.collision_boom_cap = None
                self.collision_zoom_correction = 0.0
                self.collision_hold_remaining = 0.0
                self.collision_active = False
                self.native_wall_latched = False
                self.native_collapse_latched = False
                self.native_boom_reference = native
                extra = max(0.0, float(self.extra_distance))
                self.last_output_extra = extra
            if new_native_sample:
                self.previous_native_boom = native
            return CameraObstructionSolution(extra, 0.0, 0.0, blocked_ratio, False)

        if catastrophic_collapse and not self.obstruction_active:
            self.obstruction_active = True
            self.obstruction_enter_age = self.tuning.obstruction_enter_seconds
            self.obstruction_exit_age = 0.0
            self.orbit_probe_age = 0.0
            self.orbit_previous_score = actual / desired_total
            self.orbit_direction *= -1.0
            self.orbit_target = self.orbit_direction * min(
                0.18, self.tuning.orbit_max_radians
            )

        if blocked_ratio >= self.tuning.obstruction_enter_ratio:
            self.obstruction_enter_age += dt
            self.obstruction_exit_age = 0.0
        elif blocked_ratio <= self.tuning.obstruction_exit_ratio:
            self.obstruction_exit_age += dt
            self.obstruction_enter_age = 0.0
        else:
            self.obstruction_enter_age = max(0.0, self.obstruction_enter_age - dt)
            self.obstruction_exit_age = 0.0

        if (
            not self.obstruction_active
            and self.obstruction_enter_age >= self.tuning.obstruction_enter_seconds
        ):
            self.obstruction_active = True
            self.orbit_probe_age = 0.0
            self.orbit_previous_score = actual / desired_total
            self.orbit_direction *= -1.0

        if (
            self.obstruction_active
            and self.obstruction_exit_age >= self.tuning.obstruction_exit_seconds
        ):
            self.obstruction_active = False
            self.orbit_target = 0.0
            self.pitch_target = 0.0
            self.orbit_previous_score = None
            self.orbit_probe_age = 0.0

        if (
            self.native_collapse_latched
            and self.native_boom_reference is not None
            and native
            >= self.native_boom_reference * self.tuning.native_boom_recover_ratio
        ):
            self.native_collapse_latched = False
            self.native_boom_reference = native

        if self.obstruction_active and new_native_sample:
            self.orbit_probe_age += dt
            if self.orbit_probe_age >= self.tuning.orbit_probe_seconds:
                score = actual / desired_total
                previous = self.orbit_previous_score
                if (
                    previous is not None
                    and score < previous - self.tuning.orbit_score_epsilon
                ):
                    self.orbit_direction *= -1.0
                elif abs(
                    self.orbit_target
                ) >= self.tuning.orbit_max_radians - 1e-4 and score <= (
                    previous if previous is not None else score
                ):
                    self.orbit_direction *= -1.0
                severity = 0.55 + 0.45 * blocked_ratio
                self.orbit_target += (
                    self.orbit_direction * self.tuning.orbit_probe_step * severity
                )
                self.orbit_target = max(
                    -self.tuning.orbit_max_radians,
                    min(self.tuning.orbit_max_radians, self.orbit_target),
                )
                self.orbit_previous_score = score
                self.orbit_probe_age = 0.0
            self.pitch_target = self.tuning.pitch_max_radians * blocked_ratio
        elif not self.obstruction_active:
            self.orbit_target = 0.0
            self.pitch_target = 0.0

        orbit_settle = (
            self.tuning.orbit_settle_seconds
            if self.obstruction_active
            else self.tuning.orbit_return_seconds
        )
        self.orbit_offset, self.orbit_velocity = _spring_axis(
            self.orbit_offset,
            self.orbit_velocity,
            self.orbit_target,
            dt,
            orbit_settle,
        )
        self.pitch_offset, self.pitch_velocity = _spring_axis(
            self.pitch_offset,
            self.pitch_velocity,
            self.pitch_target,
            dt,
            self.tuning.pitch_settle_seconds,
        )

        extra = self.collision_safe_extra_distance(
            actual,
            native,
            dt,
            new_native_sample=new_native_sample,
            native_back_on_wall=native_back_on_wall,
            wall_release_requested=wall_release_requested,
        )
        if new_native_sample:
            self.previous_native_boom = native
        return CameraObstructionSolution(
            extra,
            self.orbit_offset,
            self.pitch_offset,
            blocked_ratio,
            self.obstruction_active,
        )

    def collision_safe_extra_distance(
        self,
        actual_boom: float,
        native_boom: float,
        delta_seconds: float,
        *,
        new_native_sample: bool = True,
        native_back_on_wall: bool = False,
        wall_release_requested: bool = False,
    ) -> float:
        desired = max(0.0, float(self.extra_distance))
        dt = min(0.05, max(0.0, float(delta_seconds)))
        actual = max(0.0, float(actual_boom))
        native = max(0.0, float(native_boom))
        minimum_boom = self.tuning.collision_minimum_boom

        if desired <= 1e-4:
            self.collision_boom_cap = native
            self.collision_zoom_correction = 0.0
            self.collision_hold_remaining = 0.0
            self.collision_active = False
            self.native_wall_latched = False
            self.native_wall_extra = 0.0
            self.native_wall_clear_age = 0.0
            self.native_wall_anchor_focus = None
            self.last_output_extra = 0.0
            return 0.0

        if native_back_on_wall:
            if not self.native_wall_latched:
                previous = max(0.0, float(self.last_output_extra))
                self.native_wall_extra = min(
                    desired,
                    previous if previous > 1e-4 else desired,
                )
            else:
                self.native_wall_extra = min(desired, self.native_wall_extra)
            frozen = self.native_wall_extra
            self.collision_boom_cap = None
            self.collision_zoom_correction = frozen
            self.collision_hold_remaining = 0.0
            self.native_wall_clear_age = 0.0
            self.native_wall_anchor_focus = self.focus
            self.collision_active = True
            self.native_wall_latched = True
            self.last_output_extra = frozen
            return frozen

        if self.native_wall_latched:
            anchor_distance = 0.0
            if self.native_wall_anchor_focus is not None and self.focus is not None:
                anchor_distance = math.hypot(
                    self.focus[0] - self.native_wall_anchor_focus[0],
                    self.focus[2] - self.native_wall_anchor_focus[2],
                )
            can_release = bool(
                wall_release_requested
                or (
                    self.actors_are_moving
                    and anchor_distance >= self.tuning.native_wall_release_distance
                )
            )
            if not can_release:
                frozen = min(desired, self.native_wall_extra)
                self.native_wall_clear_age = 0.0
                self.collision_zoom_correction = frozen
                self.last_output_extra = frozen
                return frozen
            self.native_wall_clear_age += dt
            frozen = min(desired, self.native_wall_extra)
            if self.native_wall_clear_age > self.tuning.native_wall_hold_seconds:
                frozen = min(
                    desired,
                    frozen + self.tuning.native_wall_recover_units_per_second * dt,
                )
                self.native_wall_extra = frozen
            self.collision_zoom_correction = frozen
            self.last_output_extra = frozen
            if frozen >= desired - 0.05:
                self.native_wall_latched = False
                self.native_wall_extra = 0.0
                self.native_wall_clear_age = 0.0
                self.native_wall_anchor_focus = None
                self.collision_active = False
                self.collision_boom_cap = None
            return frozen

        desired_total = max(minimum_boom, native + desired)
        previous_total = max(minimum_boom, native + self.collision_zoom_correction)
        if self.collision_boom_cap is None:
            self.collision_boom_cap = desired_total

        self.collision_boom_cap = min(self.collision_boom_cap, desired_total)
        pressure = previous_total - actual
        absolute_threshold = (
            self.tuning.collision_active_pressure
            if self.collision_active
            else self.tuning.collision_enter_pressure
        )
        ratio_threshold = (
            self.tuning.collision_active_ratio
            if self.collision_active
            else self.tuning.collision_enter_ratio
        )
        pressure_ratio = max(0.0, pressure) / max(previous_total, 1.0)

        if (
            new_native_sample
            and pressure > absolute_threshold
            and pressure_ratio > ratio_threshold
        ):
            if not self.collision_active:
                safe_total = max(
                    minimum_boom,
                    previous_total - self.tuning.collision_backoff_units,
                )
                self.collision_boom_cap = min(self.collision_boom_cap, safe_total)
            self.collision_active = True
            self.collision_hold_remaining = self.tuning.collision_hold_seconds
        elif self.collision_active:
            self.collision_hold_remaining = max(0.0, self.collision_hold_remaining - dt)
            if self.collision_hold_remaining <= 0.0:
                recovery_rate = (
                    self.tuning.native_wall_recover_units_per_second
                    if self.native_wall_latched
                    else self.tuning.collision_recover_units_per_second
                )
                self.collision_boom_cap = min(
                    desired_total,
                    self.collision_boom_cap + recovery_rate * dt,
                )
            if (
                self.collision_boom_cap >= desired_total - 0.05
                and pressure_ratio <= self.tuning.collision_active_ratio
            ):
                self.collision_active = False
                self.native_wall_latched = False
        else:
            self.collision_boom_cap = desired_total
            self.native_wall_latched = False

        correction = max(0.0, min(desired, self.collision_boom_cap - native))
        self.collision_zoom_correction = correction
        self.last_output_extra = correction
        return correction

    def _update_actor_velocity_sample(
        self,
        current: Vec3,
        previous: Vec3,
        age: float,
        sampled: Vec3,
        *,
        sample_seconds: float | None = None,
    ) -> tuple[Vec3, Vec3, float]:
        delta = tuple(current[index] - previous[index] for index in range(3))
        changed = math.sqrt(sum(component * component for component in delta))
        if changed > self.tuning.position_sample_epsilon:
            # Production passes the exact elapsed QPC time between the game
            # Presents that committed the two positions.  ``age`` is only the
            # startup/test fallback before the renderer clock becomes visible;
            # neither path assumes a particular game frame rate.
            interval = age if sample_seconds is None else float(sample_seconds)
            interval = max(1.0e-6, interval)
            velocity = tuple(component / interval for component in delta)
            return (
                current,
                _clamp_horizontal(velocity, self.tuning.maximum_sample_speed),
                0.0,
            )
        if age >= self.tuning.velocity_sample_stale_seconds:
            sampled = (0.0, 0.0, 0.0)
        return previous, sampled, age

    def _coherent_group_velocity(
        self,
        positions: Sequence[Vec3],
        dt: float,
        present_tick_us: int | None = None,
    ) -> Vec3:
        self._ensure_player_slots(len(positions))
        clock_tick = (
            int(present_tick_us)
            if present_tick_us is not None and int(present_tick_us) > 0
            else None
        )
        if any(
            self.previous_positions[index] is None for index in range(len(positions))
        ):
            for index, position in enumerate(positions):
                self.previous_positions[index] = position
                self.velocity_sample_ticks_us[index] = clock_tick
            return 0.0, 0.0, 0.0
        for index, position in enumerate(positions):
            self.velocity_sample_ages[index] += dt
            previous = self.previous_positions[index]
            delta = tuple(
                position[axis] - previous[axis]  # type: ignore[index]
                for axis in range(3)
            )
            changed = math.sqrt(sum(component * component for component in delta))
            changed = changed > self.tuning.position_sample_epsilon
            sample_seconds = None
            if clock_tick is not None:
                previous_tick = self.velocity_sample_ticks_us[index]
                if previous_tick is None:
                    # Establish one position/timestamp pair before deriving a
                    # rate.  Guessing its age would reintroduce poll cadence.
                    self.previous_positions[index] = position
                    self.velocity_sample_ticks_us[index] = clock_tick
                    self.velocity_sample_ages[index] = 0.0
                    self.sampled_velocities[index] = (0.0, 0.0, 0.0)
                    continue
                if changed and clock_tick <= previous_tick:
                    # A position crossed the Python read while the renderer's
                    # committed Present did not. Retain the old anchor and
                    # consume the change on the next coherent Present.
                    continue
                if changed:
                    sample_seconds = (clock_tick - previous_tick) / 1_000_000.0
            (
                self.previous_positions[index],
                self.sampled_velocities[index],
                self.velocity_sample_ages[index],
            ) = self._update_actor_velocity_sample(
                position,
                previous,  # type: ignore[arg-type]
                self.velocity_sample_ages[index],
                self.sampled_velocities[index],
                sample_seconds=sample_seconds,
            )
            if changed and clock_tick is not None:
                self.velocity_sample_ticks_us[index] = clock_tick
        velocities = [self.sampled_velocities[index] for index in range(len(positions))]
        if all(_horizontal_length(velocity) < 0.05 for velocity in velocities):
            return 0.0, 0.0, 0.0

        count = len(velocities)
        average = (
            sum(velocity[0] for velocity in velocities) / count,
            0.0,
            sum(velocity[2] for velocity in velocities) / count,
        )
        if _horizontal_length(average) < 1e-6:
            return 0.0, 0.0, 0.0
        return average

    def update(
        self,
        p1_position: Sequence[float],
        p2_position: Sequence[float],
        delta_seconds: float,
        *,
        present_tick_us: int | None = None,
    ) -> CameraFrame:
        return self.update_group(
            (p1_position, p2_position),
            delta_seconds,
            present_tick_us=present_tick_us,
        )

    def update_group(
        self,
        positions: Sequence[Sequence[float]],
        delta_seconds: float,
        *,
        present_tick_us: int | None = None,
    ) -> CameraFrame:
        requested_dt = min(0.05, max(1.0 / 1000.0, float(delta_seconds)))
        clock_tick = (
            int(present_tick_us)
            if present_tick_us is not None and int(present_tick_us) > 0
            else None
        )
        if clock_tick is not None and self._last_present_tick_us is not None:
            if clock_tick <= self._last_present_tick_us:
                if self._last_camera_frame is not None:
                    self.frame_advanced = False
                    self.frame_delta_seconds = 0.0
                    return self._last_camera_frame
            else:
                requested_dt = min(
                    0.05,
                    max(
                        1.0 / 1000.0,
                        (clock_tick - self._last_present_tick_us) / 1_000_000.0,
                    ),
                )
        dt = requested_dt
        self.frame_advanced = True
        self.frame_delta_seconds = dt
        party: list[Vec3] = [
            tuple(float(value) for value in position[:3])  # type: ignore[misc]
            for position in positions
        ]
        if not party:
            frame = CameraFrame(
                self.focus or (0.0, 0.0, 0.0),
                self.extra_distance,
                0.0,
                (0.0, 0.0, 0.0),
                self.focus_velocity,
                self.reset_epoch,
            )
            if clock_tick is not None:
                self._last_present_tick_us = clock_tick
            self._last_camera_frame = frame
            return frame
        self._ensure_player_slots(len(party))
        horizontal_separation = self._widest_horizontal_separation(party)
        centre = self._rescued_focus(self._shared_focus_target(*party), party)

        group_velocity = self._coherent_group_velocity(
            party,
            dt,
            present_tick_us,
        )
        self.actors_are_moving = any(
            math.sqrt(sum(component * component for component in velocity)) > 0.10
            for velocity in self.sampled_velocities[: len(party)]
        )
        velocity_alpha = 1.0 - math.exp(
            -dt / max(0.01, self.tuning.velocity_filter_seconds)
        )
        self.filtered_group_velocity = tuple(
            self.filtered_group_velocity[index]
            + (group_velocity[index] - self.filtered_group_velocity[index])
            * velocity_alpha
            for index in range(3)
        )  # type: ignore[assignment]
        lead = _clamp_horizontal(
            (
                self.filtered_group_velocity[0] * self.tuning.lead_seconds,
                0.0,
                self.filtered_group_velocity[2] * self.tuning.lead_seconds,
            ),
            self.tuning.max_lead,
        )

        vertical_motion = max(
            abs(velocity[1]) for velocity in self.sampled_velocities[: len(party)]
        )
        transition_range = max(
            1.0e-4,
            self.tuning.terrain_transition_full_vertical_speed
            - self.tuning.terrain_transition_vertical_speed,
        )
        terrain_target = min(
            1.0,
            max(
                0.0,
                (vertical_motion - self.tuning.terrain_transition_vertical_speed)
                / transition_range,
            ),
        )

        terrain_blend_seconds = (
            self.tuning.terrain_transition_blend_rise_seconds
            if terrain_target > self.terrain_transition_blend
            else self.tuning.terrain_transition_blend_fall_seconds
        )
        terrain_alpha = 1.0 - math.exp(-dt / max(0.01, terrain_blend_seconds))
        self.terrain_transition_blend += (
            terrain_target - self.terrain_transition_blend
        ) * terrain_alpha
        if self.terrain_transition_blend < 1.0e-4 and terrain_target == 0.0:
            self.terrain_transition_blend = 0.0
        terrain_blend = self.terrain_transition_blend
        if terrain_blend > 0.0:
            lead_scale = (
                1.0 + (self.tuning.terrain_transition_lead_scale - 1.0) * terrain_blend
            )
            lead = (
                lead[0] * lead_scale,
                0.0,
                lead[2] * lead_scale,
            )
        if self.focus is None:
            self.focus = centre
            self.focus_velocity = (0.0, 0.0, 0.0)
        else:
            dx = centre[0] - self.focus[0]
            dz = centre[2] - self.focus[2]
            distance = math.hypot(dx, dz)
            moving_centre = _horizontal_length(self.filtered_group_velocity)
            active_deadzone = max(
                0.01,
                self.tuning.centre_deadzone
                - max(0.0, horizontal_separation - 2.0) * 0.025,
            )
            if moving_centre > 0.20 and horizontal_separation > 3.0:
                active_deadzone = min(active_deadzone, 0.08)
            if terrain_blend > 0.0:
                active_deadzone += (
                    min(
                        active_deadzone,
                        self.tuning.terrain_transition_deadzone,
                    )
                    - active_deadzone
                ) * terrain_blend
            if distance <= active_deadzone:
                target_x, target_z = self.focus[0], self.focus[2]
            else:
                amount = (distance - active_deadzone) / distance
                target_x = self.focus[0] + dx * amount + lead[0]
                target_z = self.focus[2] + dz * amount + lead[2]

            dy = centre[1] - self.focus[1]
            if abs(dy) <= self.tuning.vertical_deadzone:
                target_y = self.focus[1]
            else:
                target_y = centre[1] - math.copysign(self.tuning.vertical_deadzone, dy)
            targets = target_x, target_y, target_z
            next_focus = []
            next_velocity = []
            for axis in range(3):
                settle_seconds = self.tuning.centre_settle_seconds
                if terrain_blend > 0.0 and axis != 1:
                    settle_seconds += (
                        self.tuning.terrain_transition_horizontal_settle_seconds
                        - settle_seconds
                    ) * terrain_blend
                if axis == 1:
                    settle_seconds = (
                        self.tuning.vertical_fall_settle_seconds
                        if targets[axis] < self.focus[axis]
                        else self.tuning.vertical_rise_settle_seconds
                    )
                    settle_seconds += (
                        self.tuning.terrain_transition_vertical_settle_seconds
                        - settle_seconds
                    ) * terrain_blend
                position, velocity = _spring_axis(
                    self.focus[axis],
                    self.focus_velocity[axis],
                    targets[axis],
                    dt,
                    settle_seconds,
                )
                next_focus.append(position)
                next_velocity.append(velocity)
            self.focus = tuple(next_focus)  # type: ignore[assignment]
            self.focus_velocity = tuple(next_velocity)  # type: ignore[assignment]

        vertical_spread = max(member[1] for member in party) - min(
            member[1] for member in party
        )
        vertical = (
            min(vertical_spread, self.tuning.vertical_separation_cap)
            * self.tuning.vertical_separation_weight
        )
        effective_separation = math.hypot(horizontal_separation, vertical)
        desired_extra = min(
            self.tuning.max_extra_distance,
            self.tuning.zoom_per_unit
            * max(0.0, effective_separation - self.tuning.separation_deadzone),
        )
        zoom_seconds = (
            self.tuning.zoom_out_seconds
            if desired_extra > self.extra_distance
            else self.tuning.zoom_in_seconds
        )
        zoom_alpha = 1.0 - math.exp(-dt / max(0.01, zoom_seconds))
        self.extra_distance += (desired_extra - self.extra_distance) * zoom_alpha

        frame = CameraFrame(
            self.focus,
            self.extra_distance,
            effective_separation,
            lead,
            self.focus_velocity,
            self.reset_epoch,
        )
        if clock_tick is not None:
            self._last_present_tick_us = clock_tick
        self._last_camera_frame = frame
        return frame
