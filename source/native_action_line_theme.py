from __future__ import annotations

import struct


# The game draws the arrow and target line through separate paths. Both hooks
# share handles and replace only the palette, preserving native geometry, depth,
# animation, and fade. Only the arrow preserves native out-of-range red; the
# target line remains on the configured role theme.
ACTION_LINE_RENDER_SITE = 0x3F719C
ACTION_LINE_RENDER_RETURN = 0x3F71A4
ACTION_LINE_RENDER_ORIGINAL = b"\x8b\xd7\x8b\xcb\x46\x8d\x0c\x48"
ACTION_LINE_THEME_CAVE_OFFSET = 0xA000
ACTION_LINE_THEME_CAVE_SIZE = 0x100
ACTION_TARGETLINE_RENDER_SITE = 0x3F54D2
ACTION_TARGETLINE_RENDER_RETURN = 0x3F54DB
ACTION_TARGETLINE_RENDER_ORIGINAL = b"\x44\x8b\xf0\x45\x33\xc0\x44\x8b\xc8"
ACTION_TARGETLINE_THEME_CAVE_OFFSET = 0xA100
ACTION_TARGETLINE_THEME_CAVE_SIZE = 0x100
ACTION_LINE_THEME_STATE_OFFSET = 0xA200
ACTION_LINE_THEME_STATE_MAGIC = 0x314C5441
ACTION_LINE_THEME_MAX_ENTRIES = 3
ACTION_LINE_THEME_STATE_SIZE = 0x20
ACTION_LINE_NATIVE_WARNING_PALETTE = 1


def _rel32(source: int, size: int, target: int) -> bytes:
    return struct.pack("<i", target - (source + size))


def build_action_line_theme_cave(cave: int, state: int) -> bytes:
    code = bytearray()
    labels: dict[str, int] = {}
    fixups: list[tuple[int, str]] = []

    def branch(opcode: bytes, label: str) -> None:
        code.extend(opcode)
        fixups.append((len(code), label))
        code.extend(b"\x00" * 4)

    code += b"\x49\xba" + struct.pack("<Q", state)
    code += b"\x45\x8b\x1a"
    code += b"\x45\x85\xdb"
    branch(b"\x0f\x84", "done")
    code += b"\x8b\x96\xd0\x02\x00\x00"

    code += b"\x41\x3b\x52\x08"
    branch(b"\x0f\x85", "check_1")
    code += b"\x83\xf8" + bytes((ACTION_LINE_NATIVE_WARNING_PALETTE,))
    branch(b"\x0f\x84", "done")
    code += b"\x41\x8b\x42\x0c"
    branch(b"\xe9", "done")

    labels["check_1"] = len(code)
    code += b"\x41\x83\xfb\x02"
    branch(b"\x0f\x82", "done")
    code += b"\x41\x3b\x52\x10"
    branch(b"\x0f\x85", "check_2")
    code += b"\x83\xf8" + bytes((ACTION_LINE_NATIVE_WARNING_PALETTE,))
    branch(b"\x0f\x84", "done")
    code += b"\x41\x8b\x42\x14"
    branch(b"\xe9", "done")

    labels["check_2"] = len(code)
    code += b"\x41\x83\xfb\x03"
    branch(b"\x0f\x82", "done")
    code += b"\x41\x3b\x52\x18"
    branch(b"\x0f\x85", "done")
    code += b"\x83\xf8" + bytes((ACTION_LINE_NATIVE_WARNING_PALETTE,))
    branch(b"\x0f\x84", "done")
    code += b"\x41\x8b\x42\x1c"

    labels["done"] = len(code)
    code += ACTION_LINE_RENDER_ORIGINAL
    jump_source = cave + len(code)
    code += b"\xe9" + _rel32(jump_source, 5, ACTION_LINE_RENDER_RETURN)

    for offset, label in fixups:
        target = cave + labels[label]
        source_after = cave + offset + 4
        struct.pack_into("<i", code, offset, target - source_after)
    return bytes(code)


def build_action_targetline_theme_cave(cave: int, state: int) -> bytes:
    code = bytearray()
    labels: dict[str, int] = {}
    fixups: list[tuple[int, str]] = []

    def branch(opcode: bytes, label: str) -> None:
        code.extend(opcode)
        fixups.append((len(code), label))
        code.extend(b"\x00" * 4)

    code += b"\x49\xba" + struct.pack("<Q", state)
    code += b"\x45\x8b\x1a"
    code += b"\x45\x85\xdb"
    branch(b"\x0f\x84", "done")

    code += b"\x45\x8b\x87\x10\x08\x00\x00"

    code += b"\x45\x3b\x42\x08"
    branch(b"\x0f\x85", "check_1")
    code += b"\x41\x8b\x42\x0c"
    branch(b"\xe9", "done")

    labels["check_1"] = len(code)
    code += b"\x41\x83\xfb\x02"
    branch(b"\x0f\x82", "done")
    code += b"\x45\x3b\x42\x10"
    branch(b"\x0f\x85", "check_2")
    code += b"\x41\x8b\x42\x14"
    branch(b"\xe9", "done")

    labels["check_2"] = len(code)
    code += b"\x41\x83\xfb\x03"
    branch(b"\x0f\x82", "done")
    code += b"\x45\x3b\x42\x18"
    branch(b"\x0f\x85", "done")
    code += b"\x41\x8b\x42\x1c"

    labels["done"] = len(code)
    code += ACTION_TARGETLINE_RENDER_ORIGINAL
    jump_source = cave + len(code)
    code += b"\xe9" + _rel32(jump_source, 5, ACTION_TARGETLINE_RENDER_RETURN)

    for offset, label in fixups:
        target = cave + labels[label]
        source_after = cave + offset + 4
        struct.pack_into("<i", code, offset, target - source_after)
    return bytes(code)


class NativeActionLineThemeRouter:
    def __init__(self, process, base: int) -> None:
        self.process = process
        self.cave = base + ACTION_LINE_THEME_CAVE_OFFSET
        self.state = base + ACTION_LINE_THEME_STATE_OFFSET
        self.targetline_cave = base + ACTION_TARGETLINE_THEME_CAVE_OFFSET
        self.patch = (
            b"\xe9"
            + _rel32(ACTION_LINE_RENDER_SITE, 5, self.cave)
            + b"\x90" * (len(ACTION_LINE_RENDER_ORIGINAL) - 5)
        )
        self.targetline_patch = (
            b"\xe9"
            + _rel32(ACTION_TARGETLINE_RENDER_SITE, 5, self.targetline_cave)
            + b"\x90" * (len(ACTION_TARGETLINE_RENDER_ORIGINAL) - 5)
        )
        self.published_entries: tuple[tuple[int, int], ...] | None = None
        self.installed = False
        self.owns_static = False

    def install(self) -> None:
        current = self.process.read(
            ACTION_LINE_RENDER_SITE, len(ACTION_LINE_RENDER_ORIGINAL)
        )
        if current not in (ACTION_LINE_RENDER_ORIGINAL, self.patch):
            raise RuntimeError(
                "unexpected native action-line render bytes: " + current.hex(" ")
            )
        targetline_current = self.process.read(
            ACTION_TARGETLINE_RENDER_SITE,
            len(ACTION_TARGETLINE_RENDER_ORIGINAL),
        )
        if targetline_current not in (
            ACTION_TARGETLINE_RENDER_ORIGINAL,
            self.targetline_patch,
        ):
            raise RuntimeError(
                "unexpected native target-line render bytes: "
                + targetline_current.hex(" ")
            )
        cave = build_action_line_theme_cave(self.cave, self.state)
        targetline_cave = build_action_targetline_theme_cave(
            self.targetline_cave, self.state
        )
        if len(cave) > ACTION_LINE_THEME_CAVE_SIZE:
            raise RuntimeError("native action-line cave exceeds its reserved slot")
        if len(targetline_cave) > ACTION_TARGETLINE_THEME_CAVE_SIZE:
            raise RuntimeError("native target-line cave exceeds its reserved slot")
        cave_image = cave + b"\x00" * (ACTION_LINE_THEME_CAVE_SIZE - len(cave))
        targetline_cave_image = targetline_cave + b"\x00" * (
            ACTION_TARGETLINE_THEME_CAVE_SIZE - len(targetline_cave)
        )
        current_cave = self.process.read(self.cave, ACTION_LINE_THEME_CAVE_SIZE)
        current_targetline_cave = self.process.read(
            self.targetline_cave,
            ACTION_TARGETLINE_THEME_CAVE_SIZE,
        )
        installed = int(current == self.patch) + int(
            targetline_current == self.targetline_patch
        )
        if installed:
            if installed != 2:
                raise RuntimeError("partial native action-line theme installation")
            if current_cave != cave_image:
                raise RuntimeError("active native action-line cave mismatch")
            if current_targetline_cave != targetline_cave_image:
                raise RuntimeError("active native target-line cave mismatch")
            self.installed = True
            return
        if current_cave != b"\x00" * ACTION_LINE_THEME_CAVE_SIZE:
            raise RuntimeError(
                "non-empty native action-line cave with stock roots; restart the game"
            )
        if current_targetline_cave != b"\x00" * ACTION_TARGETLINE_THEME_CAVE_SIZE:
            raise RuntimeError(
                "non-empty native target-line cave with stock roots; restart the game"
            )
        self.process.write(self.state, b"\x00" * ACTION_LINE_THEME_STATE_SIZE)
        self.process.write(
            self.state + 4, struct.pack("<I", ACTION_LINE_THEME_STATE_MAGIC)
        )
        self.process.write(
            self.cave, cave_image
        )
        self.process.write(
            self.targetline_cave,
            targetline_cave_image,
        )
        self.process.patch_code(ACTION_TARGETLINE_RENDER_SITE, self.targetline_patch)
        # Never roll the first root back if the second publication is
        # ambiguous: the game thread may already be executing either cave.
        # A partial topology is detected read-only by the next helper and
        # requires a game restart instead of live repair.
        self.process.patch_code(ACTION_LINE_RENDER_SITE, self.patch)
        self.installed = True
        self.owns_static = True

    def publish(self, entries) -> bool:
        normalized: list[tuple[int, int]] = []
        seen: set[int] = set()
        for raw_handle, raw_palette in entries:
            handle = int(raw_handle) & 0xFFFFFFFF
            palette = int(raw_palette)
            if not handle or handle in seen or not 0 <= palette <= 6:
                continue
            normalized.append((handle, palette))
            seen.add(handle)
            if len(normalized) >= ACTION_LINE_THEME_MAX_ENTRIES:
                break
        signature = tuple(normalized)
        if signature == self.published_entries:
            return False

        self.process.write(self.state, b"\x00\x00\x00\x00")
        body = bytearray(ACTION_LINE_THEME_STATE_SIZE - 4)
        struct.pack_into("<I", body, 0, ACTION_LINE_THEME_STATE_MAGIC)
        for index, (handle, palette) in enumerate(signature):
            struct.pack_into("<II", body, 4 + index * 8, handle, palette)
        self.process.write(self.state + 4, body)
        self.process.write(self.state, struct.pack("<I", len(signature)))
        self.published_entries = signature
        return True

    def restore(self) -> None:
        if not self.installed:
            return
        # Count zero makes both resident hooks exact native pass-throughs.  The
        # immutable roots/caves remain process-lifetime so a replacement helper
        # can adopt them without an executable-code race.
        self.process.write(self.state, b"\x00\x00\x00\x00")
        self.installed = False
        self.owns_static = False
