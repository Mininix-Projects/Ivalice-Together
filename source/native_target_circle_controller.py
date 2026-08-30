from __future__ import annotations

from dataclasses import dataclass
import struct
import time

import coop_runtime as coop
from native_target_circle_router import (
    NativeTargetCircleRetargeter,
    TARGET_CIRCLE_ALLOCATOR_OFFSET,
    TARGET_DESIRED_PALETTE_OFFSET,
    TARGET_OWNER_OFFSET,
    TARGET_OWNER_TAGS,
    TARGET_UI_MANAGER_GLOBAL,
    TargetCircleCreateJournal,
    TargetCircleOwnership,
    build_target_dispatcher,
)

TARGET_CIRCLE_CAPACITY_CHECK = 0x291959
TARGET_CIRCLE_CAPACITY_TWO = b"\x83\xf8\x02"
TARGET_CIRCLE_CAPACITY_THREE = b"\x83\xf8\x03"


@dataclass(frozen=True)
class TargetCircleSnapshot:
    manager: int
    allocator: int
    head: int
    current: int
    nodes: tuple[int, ...]
    prev_links: tuple[int, ...]
    next_links: tuple[int, ...]


class NativeTargetCircleCapacity:
    def __init__(self, process: coop.RemoteProcess):
        self.process = process
        self.installed = False
        self.owns_static = False

    def maintain(self) -> None:
        current = self.process.read(
            TARGET_CIRCLE_CAPACITY_CHECK,
            len(TARGET_CIRCLE_CAPACITY_TWO),
        )
        if current == TARGET_CIRCLE_CAPACITY_THREE:
            self.installed = True
            return
        if current != TARGET_CIRCLE_CAPACITY_TWO:
            raise RuntimeError(
                "unexpected native target-circle capacity check "
                f"at {TARGET_CIRCLE_CAPACITY_CHECK:#x}: {current.hex(' ')}"
            )
        self.process.patch_code(
            TARGET_CIRCLE_CAPACITY_CHECK,
            TARGET_CIRCLE_CAPACITY_THREE,
        )
        self.installed = True
        self.owns_static = True

    def restore(self) -> None:
        if not self.installed:
            return
        # This immutable immediate widens native storage to three nodes and has
        # no dynamic ownership to undo.  Keep it process-resident: rewriting an
        # executable instruction while the renderer is live is an avoidable
        # fetch race, and replacement helpers adopt the exact value.
        self.installed = False
        self.owns_static = False


def build_create_dispatcher(cave: int, state: int) -> bytes:
    return build_target_dispatcher(cave, state)


class NativeTargetCircleController:
    GOLD_PALETTE = 4
    RETRY_SECONDS = 0.04
    FACTORY_FAILURE_RETRY_SECONDS = 0.50
    MAINTAIN_SECONDS = 0.10

    def __init__(
        self,
        process: coop.RemoteProcess,
        *,
        owner_tag: int,
        palette: int = GOLD_PALETTE,
        allowed_palettes: tuple[int, ...] | None = None,
    ):
        self.process = process
        self.owner_tag = int(owner_tag) & 0xFFFFFFFF
        if self.owner_tag not in TARGET_OWNER_TAGS:
            raise ValueError("target-circle owner must identify P1, P2 or P3")
        self.palette = int(palette)

        self.allowed_palettes = frozenset(
            allowed_palettes if allowed_palettes is not None else (self.palette,)
        )
        if self.palette not in self.allowed_palettes or any(
            not 0 <= value <= 6 for value in self.allowed_palettes
        ):
            raise ValueError("initial target-circle palette is not allowed")
        self.router = NativeTargetCircleRetargeter(process)
        self.node = 0
        self.target = 0
        self.published_palette: int | None = None
        self.ownership: TargetCircleOwnership | None = None
        self.failure_count = 0
        self.next_retry = 0.0
        self.next_maintenance = 0.0
        try:
            self._recover(self._snapshot())
        except (OSError, RuntimeError, struct.error):
            pass

    def _reset_local(self) -> None:
        self.node = 0
        self.target = 0
        self.published_palette = None
        self.ownership = None
        self.next_maintenance = 0.0

    def _snapshot(self) -> TargetCircleSnapshot:
        """Sample one coherent manager, allocator and linked-list generation."""

        for _ in range(3):
            manager = self.process.u64(TARGET_UI_MANAGER_GLOBAL)
            if not manager:
                if not self.process.u64(TARGET_UI_MANAGER_GLOBAL):
                    return TargetCircleSnapshot(0, 0, 0, 0, (), (), ())
                continue
            allocator = self.process.u64(manager + TARGET_CIRCLE_ALLOCATOR_OFFSET)
            head = self.process.u64(manager + 0x10F00)
            current = self.process.u64(manager + 0x10EF0)
            nodes: list[int] = []
            prev_links: list[int] = []
            next_links: list[int] = []
            seen: set[int] = set()
            node = head
            while node:
                if node in seen or len(nodes) >= 8:
                    raise RuntimeError("target-circle native list is cyclic or oversized")
                seen.add(node)
                nodes.append(node)
                previous = self.process.u64(node + 0xD68)
                following = self.process.u64(node + 0xD70)
                prev_links.append(previous)
                next_links.append(following)
                node = following
            if any(
                previous != (nodes[index - 1] if index else 0)
                for index, previous in enumerate(prev_links)
            ):
                raise RuntimeError(
                    "target-circle native list has inconsistent previous links"
                )
            if (
                self.process.u64(TARGET_UI_MANAGER_GLOBAL) != manager
                or self.process.u64(manager + TARGET_CIRCLE_ALLOCATOR_OFFSET)
                != allocator
                or self.process.u64(manager + 0x10F00) != head
                or self.process.u64(manager + 0x10EF0) != current
                or any(
                    self.process.u64(item + 0xD68) != previous
                    for item, previous in zip(nodes, prev_links)
                )
                or any(
                    self.process.u64(item + 0xD70) != following
                    for item, following in zip(nodes, next_links)
                )
            ):
                continue
            return TargetCircleSnapshot(
                manager,
                allocator,
                head,
                current,
                tuple(nodes),
                tuple(prev_links),
                tuple(next_links),
            )
        raise RuntimeError("target-circle manager/list snapshot did not stabilize")

    def _recover(self, snapshot: TargetCircleSnapshot) -> None:
        self._reset_local()
        ownership = self.router.ownership(self.owner_tag)
        if ownership is None:
            return
        if (
            snapshot.manager
            and snapshot.allocator
            and snapshot.allocator != ownership.allocator
        ):
            # A different nonzero allocator is the only proof that a later
            # CREATE cannot consume another slot from the old node's pool.
            self.router.release_ownership(ownership)
            return
        if (
            snapshot.manager != ownership.manager
            or snapshot.allocator != ownership.allocator
            or ownership.node not in snapshot.nodes
        ):
            if (
                snapshot.manager == ownership.manager
                and snapshot.allocator == ownership.allocator
                and ownership.node not in snapshot.nodes
                and self.router.release_quiescent_ownership(ownership)
            ):
                return
            # A load, KO/Stone transition or native lifecycle step can make
            # these fields temporarily disagree.  Keep the resident token as
            # a circuit breaker: discarding it would authorize a second CREATE
            # in the same fixed allocator while the first node may still live.
            raise RuntimeError(
                "owned target-circle identity is ambiguous in the current allocator"
            )
        state = struct.unpack(
            "<H",
            self.process.read(ownership.node + 0xD54, 2),
        )[0]
        live_target = self.process.u32(ownership.node + 0xD2C)
        live_palette = self.process.u32(ownership.node + 0xD7C)
        if state not in (1, 2) or not live_target or not 0 <= live_palette <= 6:
            raise RuntimeError(
                "owned target-circle lifecycle/result is ambiguous"
            )
        if state == 1 and (
            live_target != ownership.target
            or live_palette != ownership.palette
        ):
            # V6 may publish an exact retarget while the node is in the native
            # one-tick lifecycle-1 transition.  Only the already-committed
            # target/palette pair is recoverable in that phase; reconciliation
            # remains restricted to settled lifecycle 2.
            raise RuntimeError(
                "owned target-circle transition does not match its publication"
            )
        if (
            live_target != ownership.target
            or live_palette != ownership.palette
        ):
            # Retarget/palette mutation happens before the sidecar update.  A
            # helper crash in that narrow window is recoverable because the
            # exact token, heap, membership and settled lifecycle still prove
            # that this is our node.  Reconcile metadata; never allocate.
            ownership = self.router.reconcile_ownership(ownership)
            if (
                ownership.target != live_target
                or ownership.palette != live_palette
            ):
                raise RuntimeError(
                    "target-circle game-thread reconciliation changed identity"
                )
        self.node = ownership.node
        self.target = ownership.target
        self.published_palette = ownership.palette
        self.ownership = ownership

    def _claim_created_node(
        self,
        snapshot: TargetCircleSnapshot,
        node: int,
        creation_target: int,
        creation_palette: int,
    ) -> bool:
        if (
            not snapshot.manager
            or not snapshot.allocator
            or not node
            or node not in snapshot.nodes
        ):
            raise RuntimeError(
                "successful target-circle CREATE is not a member of its journaled heap"
            )
        state = struct.unpack("<H", self.process.read(node + 0xD54, 2))[0]
        observed_target = self.process.u32(node + 0xD2C)
        observed_palette = self.process.u32(node + 0xD7C)
        if (
            state == 1
            and observed_target == creation_target
            and observed_palette == creation_palette
        ):
            return False
        if not (
            state == 2
            and observed_target == creation_target
            and observed_palette == creation_palette
        ):
            raise RuntimeError(
                "successful target-circle CREATE has an invalid lifecycle/result"
            )
        ownership = self.router.claim_ownership(
            self.owner_tag,
            snapshot.allocator,
            node,
            observed_target,
            observed_palette,
        )
        if (
            ownership.manager != snapshot.manager
            or ownership.allocator != snapshot.allocator
        ):
            self.router.release_ownership(ownership)
            raise RuntimeError(
                "target-circle manager/allocator changed during ownership claim"
            )
        confirmed = self._snapshot()
        if not (
            confirmed.manager == snapshot.manager
            and confirmed.allocator == snapshot.allocator
            and node in confirmed.nodes
            and struct.unpack("<H", self.process.read(node + 0xD54, 2))[0] == 2
            and self.process.u32(node + 0xD2C) == observed_target
            and self.process.u32(node + 0xD7C) == observed_palette
        ):
            self.router.release_ownership(ownership)
            raise RuntimeError(
                "target-circle identity changed after ownership claim"
            )
        self.node = node
        self.target = observed_target
        self.published_palette = observed_palette
        self.ownership = ownership
        return True

    def _report_failure(self, phase: str, error: BaseException) -> None:
        count = int(getattr(self, "failure_count", 0)) + 1
        self.failure_count = count
        # First failure plus powers of two provide durable evidence without a
        # per-frame log storm during a load/allocator outage.
        if count == 1 or not (count & (count - 1)):
            owner = int(self.owner_tag).to_bytes(4, "little").decode(
                "ascii", errors="replace"
            )
            print(
                "TARGET_CIRCLE_SYNC_FAILURE "
                f"owner={owner} phase={phase} count={count} "
                f"error={type(error).__name__}:{error}",
                flush=True,
            )

    @staticmethod
    def _journal_matches_ownership(
        journal: TargetCircleCreateJournal,
        ownership: TargetCircleOwnership,
    ) -> bool:
        return bool(
            ownership.owner_tag == journal.owner_tag
            and ownership.manager == journal.manager
            and ownership.allocator == journal.allocator
            and ownership.node == journal.node
        )

    def _reconcile_pending_creation(
        self,
        journal: TargetCircleCreateJournal,
    ) -> bool:
        """Resolve this owner's terminal CREATE without ever issuing another."""

        if journal.owner_tag != self.owner_tag:
            return False
        if journal.status == 4:
            if journal.factory_entered:
                if self.router.discard_failed_creation_from_quiescent_node(journal):
                    self._reset_local()
                    self.next_retry = time.monotonic() + self.RETRY_SECONDS
                    return False
                snapshot = self._snapshot()
                if (
                    snapshot.manager
                    and snapshot.allocator
                    and snapshot.allocator != journal.allocator
                    and self.router.discard_creation_from_obsolete_allocator(
                        journal,
                        manager=snapshot.manager,
                        allocator=snapshot.allocator,
                    )
                ):
                    self._reset_local()
                    self.next_retry = time.monotonic() + self.RETRY_SECONDS
                    return False
                raise RuntimeError(
                    "native target-circle factory result is ambiguous; restart the game"
                )
            if not self.router.consume_creation(journal):
                raise RuntimeError(
                    "rejected target-circle CREATE could not be consumed"
                )
            self.next_retry = time.monotonic() + self.FACTORY_FAILURE_RETRY_SECONDS
            return False

        snapshot = self._snapshot()
        if (
            snapshot.manager != journal.manager
            or snapshot.allocator != journal.allocator
        ):
            if (
                snapshot.manager
                and snapshot.allocator
                and snapshot.allocator != journal.allocator
                and self.router.discard_creation_from_obsolete_allocator(
                    journal,
                    manager=snapshot.manager,
                    allocator=snapshot.allocator,
                )
            ):
                self._reset_local()
                self.next_retry = time.monotonic() + self.RETRY_SECONDS
                return False
            raise RuntimeError(
                "terminal target-circle CREATE belongs to another manager/allocator"
            )
        published = self.router.ownership(self.owner_tag)
        if published is not None:
            if not self._journal_matches_ownership(journal, published):
                raise RuntimeError(
                    "target-circle owner has a different resident publication"
                )
            self._recover(snapshot)
            if self.ownership is None or not (
                self.ownership.owner_tag == published.owner_tag
                and self.ownership.token == published.token
                and self.ownership.manager == published.manager
                and self.ownership.allocator == published.allocator
                and self.ownership.node == published.node
            ):
                raise RuntimeError(
                    "terminal target-circle ownership is no longer live"
                )
        elif not self._claim_created_node(
            snapshot,
            journal.node,
            journal.target,
            journal.palette,
        ):
            return False
        if not self.router.consume_creation(journal):
            raise RuntimeError("target-circle CREATE journal consumption failed")
        return True

    def _create(self, target: int, palette: int) -> TargetCircleCreateJournal:
        variant = 1 if (target & 0xF00000) == 0x100000 else 2
        payload = bytearray(0x50)
        struct.pack_into("<I", payload, 0x00, 1)
        struct.pack_into("<I", payload, 0x04, 1)
        struct.pack_into("<I", payload, 0x10, target)
        struct.pack_into("<I", payload, 0x14, variant)
        struct.pack_into("<I", payload, TARGET_OWNER_OFFSET, self.owner_tag)
        struct.pack_into(
            "<I",
            payload,
            TARGET_DESIRED_PALETTE_OFFSET,
            int(palette),
        )
        try:
            sequence = self.router.arm(payload)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                state = self.process.u32(self.router.state)
                if state in (3, 4):
                    if not self.router._result_ready():
                        raise RuntimeError(
                            "circle creator returned a stale acknowledgement"
                        )
                    journal = self.router.pending_creation()
                    if journal is None or not (
                        journal.sequence == sequence
                        and journal.owner_tag == self.owner_tag
                        and journal.target == target
                    ):
                        raise RuntimeError(
                            "circle creator returned a mismatched resident journal"
                        )
                    return journal
                time.sleep(0.001)
            raise RuntimeError("circle creator timed out")
        finally:
            self.router.maintain()

    def sync(self, target: int, *, palette: int | None = None) -> bool:
        if palette is not None:
            palette = int(palette)
            if palette not in self.allowed_palettes:
                raise ValueError(f"unowned target-circle palette {palette}")
            self.palette = palette
        target &= 0xFFFFFFFF
        if getattr(getattr(self, "router", None), "patched", False):
            if not self.router.maintain():
                return False
        if not target:
            self.clear()
            return False
        if not self.router.maintain():
            return False
        if not self.router.resident_exact():
            return False
        phase = "journal"
        try:
            pending = self.router.pending_creation()
            if pending is not None:
                if pending.owner_tag != self.owner_tag:
                    return False
                phase = "verify-create-journal"
                if not self._reconcile_pending_creation(pending):
                    self.next_retry = max(
                        self.next_retry,
                        time.monotonic() + self.RETRY_SECONDS,
                    )
                    return False

            phase = "recover"
            snapshot = self._snapshot()
            self._recover(snapshot)
            if not self.node:
                now = time.monotonic()
                if now < self.next_retry:
                    return False
                if not snapshot.manager or not snapshot.allocator:
                    self.next_retry = now + self.RETRY_SECONDS
                    return False
                if bool(snapshot.head) != bool(snapshot.current):
                    self.next_retry = now + self.RETRY_SECONDS
                    return False
                if snapshot.head and (
                    snapshot.current not in snapshot.nodes
                    or len(snapshot.nodes) >= 3
                ):
                    self.next_retry = now + self.RETRY_SECONDS
                    return False
                if any(
                    struct.unpack("<H", self.process.read(node + 0xD54, 2))[0]
                    != 2
                    for node in snapshot.nodes
                ):
                    self.next_retry = now + self.RETRY_SECONDS
                    return False

                phase = "create"
                journal = self._create(target, self.palette)
                phase = "verify-created"
                if not self._reconcile_pending_creation(journal):
                    # State 1 is a normal one-tick lifecycle after a proven
                    # factory success, not another factory failure.
                    self.next_retry = time.monotonic() + self.RETRY_SECONDS
                    return False
            if self.node and (
                self.target != target or self.published_palette != self.palette
            ):
                phase = "owned-update"
                if self.ownership is None:
                    raise RuntimeError("target-circle ownership was lost")
                self.ownership = self.router.update_ownership(
                    self.ownership,
                    target=target,
                    palette=self.palette,
                )
                if (
                    self.ownership.target != target
                    or self.ownership.palette != self.palette
                ):
                    raise RuntimeError(
                        "target-circle game-thread UPDATE returned a mismatch"
                    )
                self.target = target
            self.published_palette = self.palette
            self.failure_count = 0
            self.next_retry = 0.0
            self.next_maintenance = time.monotonic() + self.MAINTAIN_SECONDS
            return True
        except (OSError, RuntimeError, struct.error) as error:
            self._report_failure(phase, error)
            retry = (
                self.FACTORY_FAILURE_RETRY_SECONDS
                if phase in (
                    "create",
                    "journal",
                    "verify-create-journal",
                    "verify-created",
                )
                else self.RETRY_SECONDS
            )
            self.next_retry = time.monotonic() + retry
            return False

    def clear(self) -> None:
        if not self.router.maintain():
            return
        if not self.router.resident_exact():
            return
        try:
            pending = self.router.pending_creation()
            if pending is not None and pending.owner_tag == self.owner_tag:
                if pending.status == 3 and not self._reconcile_pending_creation(
                    pending
                ):
                    return
                if pending.status == 4:
                    self._reconcile_pending_creation(pending)

            snapshot = self._snapshot()
            ownership = self.ownership
            if not self.node or ownership is None:
                self._recover(snapshot)
                ownership = self.ownership
                if not self.node or ownership is None:
                    self._reset_local()
                    return
            published = self.router.ownership(self.owner_tag)
            if published != ownership:
                self._reset_local()
                return
            if not (
                self.node == ownership.node
                and self.target == ownership.target
                and self.published_palette == ownership.palette
            ):
                self._reset_local()
                return
            if (
                snapshot.manager
                and snapshot.allocator
                and snapshot.allocator != ownership.allocator
            ):
                # The old record cannot govern a different allocator.  Retire
                # only the sidecar without dereferencing an obsolete node.
                self.router.release_ownership(ownership)
                self._reset_local()
                return
            state = struct.unpack("<H", self.process.read(self.node + 0xD54, 2))[0]
            live_target = self.process.u32(self.node + 0xD2C)
            live_palette = self.process.u32(self.node + 0xD7C)
            if not (
                snapshot.manager == ownership.manager
                and snapshot.allocator == ownership.allocator
                and self.node in snapshot.nodes
                and state == 2
                and live_target
                and 0 <= live_palette <= 6
            ):
                # Otherwise retain both the token and local identity.  A
                # transient lifecycle/list mismatch in the same allocator is
                # not permission to forget a potentially allocated node.
                return
            if (
                live_target != ownership.target
                or live_palette != ownership.palette
            ):
                ownership = self.router.reconcile_ownership(ownership)
                if (
                    ownership.target != live_target
                    or ownership.palette != live_palette
                ):
                    return
                self.ownership = ownership
                self.target = live_target
                self.published_palette = live_palette
            if not self.router.retire_ownership(ownership):
                # A concurrent identity change is not authority to discard the
                # circuit breaker.  A later call must re-prove exact retirement.
                return
        except (OSError, RuntimeError, struct.error):
            # A failed proof (or an ambiguous write) retains local ownership so
            # a later call can retry.  No speculative native write is issued.
            return
        self._reset_local()

    def suspend_for_native_frontend(self) -> bool:
        """Retire normally, or release a node reset by a fullscreen native UI."""

        self.clear()
        if not self.router.maintain() or not self.router.resident_exact():
            return False
        try:
            ownership = self.router.ownership(self.owner_tag)
            if ownership is None:
                self._reset_local()
                return True
            if not self.router.release_frontend_reset_ownership(ownership):
                return False
        except (OSError, RuntimeError, struct.error):
            return False
        self._reset_local()
        self.failure_count = 0
        self.next_retry = 0.0
        return True

    def restore(self) -> bool:
        self.clear()
        if self.node or self.ownership is not None:
            return False
        try:
            if self.router.pending_creation() is not None:
                return False
        except (OSError, RuntimeError, struct.error):
            return False
        return self.router.restore()
