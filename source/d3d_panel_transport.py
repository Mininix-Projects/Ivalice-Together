from __future__ import annotations

import atexit
import ctypes as ct
import math
import mmap
import os
import struct
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
from PIL import Image


MAGIC = 0x33504955
VERSION = 1
HEADER_SIZE = 192
PANEL_COUNT = 17
SLOT_HEADER_SIZE = 64
MAX_PANEL_WIDTH = 1024
MAX_PANEL_HEIGHT = 768
PIXEL_CAPACITY = MAX_PANEL_WIDTH * MAX_PANEL_HEIGHT * 4
SLOT_SIZE = SLOT_HEADER_SIZE + PIXEL_CAPACITY
MAPPING_SIZE = HEADER_SIZE + PANEL_COUNT * SLOT_SIZE
RENDERER_STALE_MS = 1500
REOPEN_BACKOFF_SECONDS = 1.0

PUBLISHER_READY_OFFSET = 24
RENDERER_READY_OFFSET = 28
PUBLISHER_HEARTBEAT_OFFSET = 32
RENDERER_HEARTBEAT_OFFSET = 40
ACK_GENERATIONS_OFFSET = 64

PANEL_SLOTS = {
    "world_target": 0,
    "interaction_prompt_p2": 1,
    "menu_p2": 2,
    "help_p2": 3,
    "target_info_backdrop": 4,
    "target_info_p2": 5,
    "target_info_p1": 6,
    "menu_p1": 7,
    "help_p1": 8,
    "menu_p3": 9,
    "help_p3": 10,
    "target_info_p3": 11,
    "interaction_prompt_p3": 12,
    "party_target_cursor_p1": 13,
    "party_target_cursor_p2": 14,
    "party_target_cursor_p3": 15,
    "damage_meter": 16,
}


kernel32 = ct.WinDLL("kernel32", use_last_error=True)
kernel32.GetTickCount64.argtypes = []
kernel32.GetTickCount64.restype = ct.c_ulonglong


def tick_ms() -> int:
    return int(kernel32.GetTickCount64())


_PREMULTIPLIED_CACHE: OrderedDict[tuple[int, float], tuple[Image.Image, bytes]] = (
    OrderedDict()
)
_PREMULTIPLIED_CACHE_LIMIT = 16


def _plain_premultiplied_bgra(
    image: Image.Image,
    opacity: float,
) -> bytes:
    rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8).copy()
    opacity = max(0.0, min(1.0, float(opacity)))
    if opacity < 0.999:
        rgba[..., 3] = np.clip(
            rgba[..., 3].astype(np.float32) * opacity, 0, 255
        ).astype(np.uint8)
    alpha = rgba[..., 3:4].astype(np.uint16)
    premul = (rgba[..., :3].astype(np.uint16) * alpha // 255).astype(np.uint8)
    bgra = np.empty_like(rgba)
    bgra[..., 0] = premul[..., 2]
    bgra[..., 1] = premul[..., 1]
    bgra[..., 2] = premul[..., 0]
    bgra[..., 3] = rgba[..., 3]
    return bgra.tobytes()


def _fast_premultiplied_bgra(image: Image.Image) -> bytes:
    return image.convert("RGBA").convert("RGBa").tobytes("raw", "BGRa")


def _cached_plain_premultiplied_bgra(
    image: Image.Image,
    opacity: float,
) -> bytes:
    key = (id(image), round(float(opacity), 5))
    cached = _PREMULTIPLIED_CACHE.pop(key, None)
    if cached is not None and cached[0] is image:
        _PREMULTIPLIED_CACHE[key] = cached
        return cached[1]
    pixels = _plain_premultiplied_bgra(image, opacity)
    _PREMULTIPLIED_CACHE[key] = (image, pixels)
    while len(_PREMULTIPLIED_CACHE) > _PREMULTIPLIED_CACHE_LIMIT:
        _PREMULTIPLIED_CACHE.popitem(last=False)
    return pixels


def _composite_premultiplied_layer(
    destination: np.ndarray,
    layer: Image.Image,
    x: int,
    y: int,
) -> None:
    height, width = destination.shape[:2]
    layer_width, layer_height = layer.size
    left = max(0, int(x))
    top = max(0, int(y))
    right = min(width, int(x) + layer_width)
    bottom = min(height, int(y) + layer_height)
    if left >= right or top >= bottom:
        return
    source_left = left - int(x)
    source_top = top - int(y)
    source = np.frombuffer(
        _cached_plain_premultiplied_bgra(layer, 1.0),
        dtype=np.uint8,
    ).reshape(layer_height, layer_width, 4)[
        source_top : source_top + (bottom - top),
        source_left : source_left + (right - left),
    ]
    source_16 = source.astype(np.uint16)
    destination_slice = destination[top:bottom, left:right]
    destination_16 = destination_slice.astype(np.uint16)
    inverse_alpha = 255 - source_16[..., 3:4]
    composed = source_16 + (destination_16 * inverse_alpha + 127) // 255
    destination_slice[:] = np.minimum(composed, 255).astype(np.uint8)


def premultiplied_bgra(image: Image.Image, opacity: float = 1.0) -> bytes:
    opacity = max(0.0, min(1.0, float(opacity)))
    dynamic = image.info.get("ff12_dynamic_layers")
    if opacity >= 0.999 and dynamic and len(dynamic[1]) > 4:
        return _fast_premultiplied_bgra(image)
    if opacity >= 0.999 and dynamic and len(dynamic[1]) <= 4:
        base, layers = dynamic
        if isinstance(base, Image.Image) and base.size == image.size:
            width, height = image.size
            destination = (
                np.frombuffer(
                    _cached_plain_premultiplied_bgra(base, 1.0),
                    dtype=np.uint8,
                )
                .copy()
                .reshape(height, width, 4)
            )
            for layer, x, y in layers:
                if isinstance(layer, Image.Image):
                    _composite_premultiplied_layer(
                        destination,
                        layer,
                        int(x),
                        int(y),
                    )
            return destination.tobytes()
    return _cached_plain_premultiplied_bgra(image, opacity)


@dataclass
class PublishedPanel:
    generation: int = 0
    sequence: int = 0
    visible: bool = False
    draw_key: tuple | None = None
    pixel_key: tuple | None = None
    pixel_image: Image.Image | None = None
    pixels: bytes = b""


class D3DPanelTransport:
    def __init__(self, game_pid: int) -> None:
        self.game_pid = int(game_pid)
        self.name = f"Local\\FF12CoopPanels_{self.game_pid}"
        self.configured = os.environ.get("FF12_COOP_D3D_PANELS", "1") != "0"
        self.enabled = False
        self.lock = threading.RLock()
        self.mapping: mmap.mmap | None = None
        self.states = [PublishedPanel() for _ in range(PANEL_COUNT)]
        self._closed = False
        self._next_reopen_at = 0.0
        self.mapping_epoch = 0
        if self.configured:
            self.recover(force=True)
        atexit.register(self.close)

    @staticmethod
    def _next_generation(generation: int) -> int:
        generation = (int(generation) + 1) & 0xFFFFFFFF
        return generation or 1

    @classmethod
    def _reconnect_generation(
        cls,
        slot_generation: int,
        acknowledged_generation: int,
    ) -> int:
        """Choose a hidden generation distinct from the renderer's old state.

        A named mapping can outlive the Python view because the in-process
        renderer still owns a handle.  Restarting every slot at generation one
        would let the renderer mistake new pixels for an old GPU texture.  The
        first visible commit may immediately follow this hidden commit, so keep
        both generations distinct from the last renderer acknowledgement.
        """

        generation = cls._next_generation(slot_generation)
        acknowledged_generation = int(acknowledged_generation) & 0xFFFFFFFF
        while acknowledged_generation and (
            generation == acknowledged_generation
            or cls._next_generation(generation) == acknowledged_generation
        ):
            generation = cls._next_generation(generation)
        return generation

    def _reset_states_locked(
        self,
        generations: list[int] | None = None,
    ) -> None:
        if generations is None:
            self.states = [PublishedPanel() for _ in range(PANEL_COUNT)]
            return
        self.states = [
            PublishedPanel(generation=generation, sequence=2)
            for generation in generations
        ]

    def _drop_mapping_locked(self, *, retry: bool) -> None:
        mapping = self.mapping
        self.mapping = None
        self.enabled = False
        self._reset_states_locked()
        if retry and self.configured and not self._closed:
            self._next_reopen_at = time.monotonic() + REOPEN_BACKOFF_SECONDS
        else:
            self._next_reopen_at = math.inf
        if mapping is None:
            return
        try:
            struct.pack_into("<I", mapping, PUBLISHER_READY_OFFSET, 0)
            mapping.flush()
        except (
            AttributeError,
            BufferError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            struct.error,
        ):
            pass
        try:
            mapping.close()
        except (AttributeError, BufferError, OSError, RuntimeError, ValueError):
            pass

    def _open_mapping_locked(self) -> bool:
        if self._closed or not self.configured:
            return False
        candidate = None
        try:
            candidate = mmap.mmap(
                -1,
                MAPPING_SIZE,
                tagname=self.name,
                access=mmap.ACCESS_WRITE,
            )
            # Stop presentation before replacing an existing publisher view.
            # The renderer may keep the named section alive after our old view
            # becomes unusable, so this is also a reconnect path rather than
            # necessarily a brand-new zero-filled mapping.
            struct.pack_into("<I", candidate, PUBLISHER_READY_OFFSET, 0)
            previous_generations = [
                struct.unpack_from(
                    "<I",
                    candidate,
                    self._slot_offset(slot) + 4,
                )[0]
                for slot in range(PANEL_COUNT)
            ]
            acknowledged_generations = list(
                struct.unpack_from(
                    f"<{PANEL_COUNT}I",
                    candidate,
                    ACK_GENERATIONS_OFFSET,
                )
            )
            initial_generations = [
                self._reconnect_generation(previous, acknowledged)
                for previous, acknowledged in zip(
                    previous_generations,
                    acknowledged_generations,
                    strict=True,
                )
            ]

            header = bytearray(HEADER_SIZE)
            struct.pack_into(
                "<IIIIII",
                header,
                0,
                MAGIC,
                VERSION,
                HEADER_SIZE,
                PANEL_COUNT,
                SLOT_SIZE,
                PIXEL_CAPACITY,
            )
            struct.pack_into(
                "<Q",
                header,
                PUBLISHER_HEARTBEAT_OFFSET,
                tick_ms(),
            )
            candidate[:HEADER_SIZE] = header
            for slot, generation in enumerate(initial_generations):
                slot_header = bytearray(SLOT_HEADER_SIZE)
                struct.pack_into("<I", slot_header, 0, 2)
                struct.pack_into(
                    "<IIIIIfffIII",
                    slot_header,
                    4,
                    generation,
                    0,
                    0,
                    0,
                    0,
                    0.0,
                    0.0,
                    1.0,
                    0,
                    slot,
                    0,
                )
                offset = self._slot_offset(slot)
                candidate[offset : offset + SLOT_HEADER_SIZE] = slot_header
            # Publisher readiness is the commit marker.  Until this final
            # aligned store, the renderer must not consume partially reset
            # slots or retain a visible GPU panel from the previous view.
            struct.pack_into("<I", candidate, PUBLISHER_READY_OFFSET, 1)
        except (
            BufferError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            struct.error,
        ):
            if candidate is not None:
                try:
                    candidate.close()
                except (AttributeError, BufferError, OSError, RuntimeError, ValueError):
                    pass
            self.mapping = None
            self.enabled = False
            self._next_reopen_at = time.monotonic() + REOPEN_BACKOFF_SECONDS
            return False
        self.mapping = candidate
        self.enabled = True
        self._next_reopen_at = 0.0
        self.mapping_epoch = self._next_generation(self.mapping_epoch)
        self._reset_states_locked(initial_generations)
        return True

    def recover(self, *, force: bool = False) -> bool:
        """Reopen a transiently failed publisher mapping without changing identity."""

        with self.lock:
            if self.mapping is not None and self.enabled:
                return True
            if self._closed or not self.configured:
                return False
            if not force and time.monotonic() < self._next_reopen_at:
                return False
            return self._open_mapping_locked()

    def invalidate(self) -> None:
        """Quarantine a broken view while allowing a bounded later reconnect."""

        with self.lock:
            self._drop_mapping_locked(retry=True)

    @staticmethod
    def supports(tag: str) -> bool:
        return tag in PANEL_SLOTS

    def touch(self) -> bool:
        with self.lock:
            if not self.recover():
                return False
            try:
                struct.pack_into(
                    "<Q",
                    self.mapping,
                    PUBLISHER_HEARTBEAT_OFFSET,
                    tick_ms(),
                )
            except (
                BufferError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                struct.error,
            ):
                self._drop_mapping_locked(retry=True)
                return False
            return True

    def renderer_available(self) -> bool:
        with self.lock:
            if not self.touch() or self.mapping is None:
                return False
            try:
                ready = struct.unpack_from(
                    "<I", self.mapping, RENDERER_READY_OFFSET
                )[0]
                heartbeat = struct.unpack_from(
                    "<Q", self.mapping, RENDERER_HEARTBEAT_OFFSET
                )[0]
            except (
                BufferError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                struct.error,
            ):
                self._drop_mapping_locked(retry=True)
                return False
            now = tick_ms()
            return bool(
                ready
                and heartbeat
                and now >= heartbeat
                and now - heartbeat <= RENDERER_STALE_MS
            )

    @staticmethod
    def _slot_offset(slot: int) -> int:
        return HEADER_SIZE + slot * SLOT_SIZE

    def _commit(
        self,
        slot: int,
        *,
        visible: bool,
        width: int = 0,
        height: int = 0,
        logical_x: float = 0.0,
        logical_y: float = 0.0,
        opacity: float = 1.0,
        pixels: bytes | None = b"",
        draw_key: tuple | None = None,
    ) -> None:
        if self.mapping is None:
            return
        state = self.states[slot]
        state.generation = (state.generation + 1) & 0xFFFFFFFF
        if not state.generation:
            state.generation = 1
        odd = (state.sequence + 1) | 1
        even = (odd + 1) & 0xFFFFFFFF
        if not even:
            even = 2
        offset = self._slot_offset(slot)
        stride = width * 4 if visible else 0

        pixel_bytes = width * height * 4 if visible else 0
        metadata_only = bool(visible and pixels is None)
        struct.pack_into("<I", self.mapping, offset, odd)
        struct.pack_into(
            "<IIIIIfffIII",
            self.mapping,
            offset + 4,
            state.generation,
            int(visible),
            int(width),
            int(height),
            int(stride),
            float(logical_x),
            float(logical_y),
            float(opacity),
            int(pixel_bytes),
            int(slot),
            int(metadata_only),
        )
        self.mapping[offset + 48 : offset + SLOT_HEADER_SIZE] = b"\x00" * (
            SLOT_HEADER_SIZE - 48
        )
        if visible and pixels is not None:
            pixel_offset = offset + SLOT_HEADER_SIZE
            self.mapping[pixel_offset : pixel_offset + pixel_bytes] = pixels
        struct.pack_into("<I", self.mapping, offset, even)
        state.sequence = even
        state.visible = visible
        state.draw_key = draw_key
        self.touch()

    def publish(
        self,
        tag: str,
        image: Image.Image,
        logical_x: float,
        logical_y: float,
        opacity: float = 1.0,
    ) -> bool:
        slot = PANEL_SLOTS.get(tag)
        if slot is None or not self.recover():
            return False
        try:
            width, height = image.size
            logical_x = float(logical_x)
            logical_y = float(logical_y)
            opacity = float(opacity)
        except (AttributeError, TypeError, ValueError):
            return False
        if (
            width <= 0
            or height <= 0
            or width > MAX_PANEL_WIDTH
            or height > MAX_PANEL_HEIGHT
            or not math.isfinite(logical_x)
            or not math.isfinite(logical_y)
            or not math.isfinite(opacity)
        ):
            self.hide(tag)
            return False
        draw_key = (
            id(image),
            width,
            height,
            round(float(logical_x), 4),
            round(float(logical_y), 4),
            round(float(opacity), 5),
        )
        with self.lock:
            if self.mapping is None:
                return False
            state = self.states[slot]
            try:
                if not state.visible or state.draw_key != draw_key:
                    pixel_key = (
                        id(image),
                        width,
                        height,
                        round(float(opacity), 5),
                    )
                    pixels: bytes | None
                    if (
                        state.visible
                        and state.pixel_image is image
                        and state.pixel_key == pixel_key
                        and len(state.pixels) == width * height * 4
                    ):
                        pixels = None
                    else:
                        pixels = premultiplied_bgra(image, opacity)
                        state.pixel_image = image
                        state.pixel_key = pixel_key
                        state.pixels = pixels
                    if pixels is not None and len(pixels) > PIXEL_CAPACITY:
                        self.hide(tag)
                        return False
                    self._commit(
                        slot,
                        visible=True,
                        width=width,
                        height=height,
                        logical_x=logical_x,
                        logical_y=logical_y,
                        opacity=opacity,
                        pixels=pixels,
                        draw_key=draw_key,
                    )
                else:
                    self.touch()
            except (
                BufferError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                struct.error,
            ):
                self._drop_mapping_locked(retry=True)
                return False
            return self.renderer_available()

    def hide(self, tag: str) -> bool:
        slot = PANEL_SLOTS.get(tag)
        if slot is None or not self.recover():
            return False
        with self.lock:
            try:
                if self.states[slot].visible:
                    self._commit(slot, visible=False)
                else:
                    self.touch()
            except (
                BufferError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                struct.error,
            ):
                self._drop_mapping_locked(retry=True)
                return False
            return self.renderer_available()

    def close(self) -> None:
        with self.lock:
            if self._closed:
                return
            self._closed = True
            if self.mapping is None:
                self.enabled = False
                self._next_reopen_at = math.inf
                return
            try:
                for tag in PANEL_SLOTS:
                    self.hide(tag)
                struct.pack_into("<I", self.mapping, PUBLISHER_READY_OFFSET, 0)
                self.touch()
                self.mapping.flush()
            except (
                BufferError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                struct.error,
            ):
                pass
            self._drop_mapping_locked(retry=False)


_transports: dict[int, D3DPanelTransport] = {}
_transport_lock = threading.Lock()


def panel_transport(game_pid: int, tag: str) -> D3DPanelTransport | None:
    if not D3DPanelTransport.supports(tag):
        return None
    with _transport_lock:
        transport = _transports.get(int(game_pid))
        if transport is None or transport._closed:
            transport = D3DPanelTransport(game_pid)
            _transports[int(game_pid)] = transport
        return transport if transport.configured else None


__all__ = [
    "ACK_GENERATIONS_OFFSET",
    "D3DPanelTransport",
    "HEADER_SIZE",
    "MAGIC",
    "MAPPING_SIZE",
    "MAX_PANEL_HEIGHT",
    "MAX_PANEL_WIDTH",
    "PANEL_COUNT",
    "PANEL_SLOTS",
    "PIXEL_CAPACITY",
    "SLOT_HEADER_SIZE",
    "SLOT_SIZE",
    "VERSION",
    "panel_transport",
    "premultiplied_bgra",
]
