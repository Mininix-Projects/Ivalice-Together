"""Regressions for committed role direction and KO presentation, without game assets."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import sys

from PIL import Image
import pytest

import coop_runtime as coop
import native_ui_assets


def token(member):
    return NS(member_id=member, actor=0x1000 + member * 256,
              phys=0x8000 + member * 256, actor_generation=4, world_epoch=7)


def command(owner, heading):
    return NS(**vars(owner), active=True, heading=heading,
              visual_heading=heading - 0.1, magnitude=2.5)


@pytest.fixture
def orientation(monkeypatch):
    owners = (token(0), token(2))
    publishers = {
        coop.P2_MOVEMENT_MAILBOX: NS(last_command=command(owners[0], 0.25)),
        coop.P3_MOVEMENT_MAILBOX: NS(last_command=command(owners[1], -1.5)),
    }
    monkeypatch.setattr(coop.movement_mailbox, "publisher_for",
                        lambda process, address: publishers[address])
    process = NS(position=(1, 2, 3), camera=0.3)
    transfer = NS(sequence=12, world_epoch=7, native_applied=True,
                  p2_movement_sequence=8, p3_movement_sequence=10)

    def apply(caches=(2.0, 1.9, 0.8, 0.7)):
        return coop.adopt_committed_orientation(
            process, transfer, *owners, 7, *caches
        )

    return NS(apply=apply, process=process, transfer=transfer,
              owners=owners, publishers=publishers)


def test_each_actor_keeps_its_own_direction_once_per_transfer(orientation):
    o = orientation
    payloads = [vars(p.last_command).copy() for p in o.publishers.values()]
    assert o.apply() == pytest.approx((0.25, 0.15, -1.5, -1.6))
    manual = (2.8, 2.7, -2.8, -2.7)
    for _ in range(10):
        assert o.apply(manual) == manual
    assert payloads == [vars(p.last_command) for p in o.publishers.values()]
    assert o.process.position == (1, 2, 3) and o.process.camera == 0.3
    o.transfer.sequence += 1
    o.publishers[coop.P2_MOVEMENT_MAILBOX].last_command.heading = -2
    assert o.apply(manual)[0] == -2


@pytest.mark.parametrize("field,value", [
    ("actor", 999), ("phys", 999), ("member_id", 999),
    ("actor_generation", 999), ("world_epoch", 999),
    ("heading", float("nan")), ("visual_heading", float("inf")),
])
def test_stale_or_invalid_command_does_not_partially_adopt(orientation, field, value):
    o = orientation
    # Fail P3 after P2 has been examined; no partial transaction is retired.
    setattr(o.publishers[coop.P3_MOVEMENT_MAILBOX].last_command, field, value)
    with pytest.raises(OSError):
        o.apply()
    assert not hasattr(o.process, "_orientation_adopted_roles")


@pytest.mark.parametrize("field,value", [("world_epoch", 8), ("native_applied", False)])
def test_cancelled_or_other_world_transfer_is_rejected(orientation, field, value):
    setattr(orientation.transfer, field, value)
    with pytest.raises(OSError):
        orientation.apply()
    assert not hasattr(orientation.process, "_orientation_adopted_roles")


def test_inactive_and_unchanged_roles_keep_existing_heading(orientation):
    o = orientation
    o.publishers[coop.P2_MOVEMENT_MAILBOX].last_command.active = False
    o.transfer.p3_movement_sequence = 0
    assert o.apply() == (2.0, 1.9, 0.8, 0.7)
    o.publishers[coop.P2_MOVEMENT_MAILBOX].last_command.active = True
    assert o.apply((1, 2, 3, 4)) == (1, 2, 3, 4)


def test_missing_publisher_command_is_rejected(orientation):
    orientation.publishers[coop.P2_MOVEMENT_MAILBOX].last_command = None
    with pytest.raises(OSError):
        orientation.apply()


class FixtureFont:
    def render(self, text, scale=1.0, **kwargs):
        return Image.new("RGBA", (max(1, round(len(text) * 8 * scale)),
                                  max(1, round(14 * scale))), (255, 255, 255, 255))


@pytest.fixture
def overlay(monkeypatch):
    name = "_ko_badge_regression_fixture"
    path = Path(__file__).resolve().parents[1] / "source/headless_world_target_overlay.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    with patch.object(native_ui_assets, "BitmapFont", return_value=FixtureFont()):
        spec.loader.exec_module(module)
    monkeypatch.setattr(module, "header_sprite",
                        lambda: Image.new("RGBA", (390, 40)))
    monkeypatch.setattr(module, "_header_sprite",
                        lambda scale: Image.new("RGBA", (390, 40)))
    return module


@pytest.mark.parametrize("scale", [0.3, 0.7, 1.0])
def test_badge_uses_existing_native_skull_pixels(overlay, monkeypatch, scale):
    skull = Image.new("RGBA", (23, 29), (241, 19, 203, 255))
    requested = []
    monkeypatch.setattr(overlay, "trait_sprite",
                        lambda name: requested.append(name) or skull)
    monkeypatch.setattr(overlay, "status_icon_scale", lambda: scale)
    actual = overlay.ko_badge()
    expected = skull.resize((max(1, round(23 * scale)), max(1, round(29 * scale))),
                            Image.Resampling.LANCZOS)
    assert requested == ["status/doom"]
    assert actual.size == expected.size and actual.tobytes() == expected.tobytes()


def test_missing_skull_is_explicit(overlay, monkeypatch):
    monkeypatch.setattr(overlay, "trait_sprite", lambda name: None)
    with pytest.raises(FileNotFoundError):
        overlay.ko_badge()


@pytest.mark.parametrize("style", ["framed", "classic"])
@pytest.mark.parametrize("theme", ["sapphire", "ruby"])
@pytest.mark.parametrize("hp,status,expected", [
    (0, 0, True), (25, 1, True), (0, 1, True),
    (25, 0, False), (25, 64, False), (25, 2, False),
])
def test_real_renderer_displays_skull_only_for_ko(
    overlay, monkeypatch, style, theme, hp, status, expected
):
    monkeypatch.setattr(overlay, "TARGET_PANEL_STYLE", style)
    skull = Image.new("RGBA", (23, 29), (241, 19, 203, 255))
    monkeypatch.setattr(overlay, "trait_sprite", lambda name: skull)
    info = overlay.LiveTargetInfo("Basch", 0x1000, 4, (0, 0, 0), 50, hp, 100,
                                  status_mask=status)
    with patch.object(overlay, "ko_badge", wraps=overlay.ko_badge) as badge:
        rendered = overlay.render_target_info(info, 2, theme)
    assert bool(badge.call_count) is expected
    assert info.current_hp == hp and info.status_mask == status
    if expected:
        colors = rendered.getcolors(rendered.width * rendered.height)
        assert (241, 19, 203, 255) in {color for count, color in colors}
