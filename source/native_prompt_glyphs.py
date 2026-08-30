from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image

ASSET_ROOT = Path(__file__).resolve().parent / "runtime_ui_assets_png"


PAD_ATLAS_PATH = ASSET_ROOT / "buttonicon__pic00__pal15.png"
KEY_ATLAS_PATH = ASSET_ROOT / "keyboard__pic00__pal00.png"


PLATE_SQUARE_BOX = (10, 660, 140, 790)
PLATE_WIDE_BOX = PLATE_SQUARE_BOX

PAD_BUTTON_BOXES = {
    "A": (258, 2, 295, 40),
    "B": (298, 2, 335, 40),
    "X": (338, 2, 375, 40),
    "Y": (378, 2, 415, 40),
    "LB": (258, 43, 305, 73),
    "RB": (310, 43, 357, 73),
    "LT": (267, 76, 296, 124),
    "RT": (318, 76, 347, 124),
    "BACK": (370, 43, 412, 82),
    "START": (420, 43, 462, 82),
    "SELECT": (81, 10, 153, 52),
    "LS": (268, 146, 292, 175),
    "RS": (268, 190, 292, 219),
    "L3": (49, 189, 76, 211),
    "R3": (49, 215, 76, 237),
    "DPAD": (0, 57, 34, 93),
    "L": (40, 61, 86, 90),
    "R": (92, 61, 138, 90),
}


KEY_LABEL_ALIASES = {
    "SPC": "Space",
    "SPACE": "Space",
    "ENT": "Enter",
    "ENTER": "Enter",
    "BSP": "Back",
    "BACKSPACE": "Back",
    "ESC": "Esc",
    "<": "Left",
    ">": "Right",
    "UP": "Up",
    "DN": "Down",
    "DEL": "Delete",
    "INS": "Insert",
    "CTRL": "LeftCtrl",
    "SHIFT": "LeftShift",
    "CAPS": "CapsLock",
}

KEY_LABEL_BOXES = {
    "0": (166, 62, 185, 89),
    "1": (719, 14, 731, 37),
    "2": (766, 14, 782, 37),
    "3": (818, 14, 834, 37),
    "4": (866, 14, 884, 37),
    "5": (918, 14, 934, 37),
    "6": (967, 14, 984, 37),
    "7": (17, 62, 33, 89),
    "8": (67, 62, 84, 89),
    "9": (117, 62, 134, 89),
    "?": (218, 157, 233, 192),
    "A": (115, 107, 134, 142),
    "B": (967, 107, 984, 142),
    "C": (865, 107, 883, 142),
    "D": (215, 107, 235, 142),
    "E": (517, 62, 533, 89),
    "F": (267, 107, 282, 142),
    "G": (314, 107, 334, 142),
    "H": (365, 107, 385, 142),
    "I": (770, 62, 780, 89),
    "J": (419, 107, 430, 142),
    "K": (467, 107, 484, 142),
    "L": (518, 107, 533, 142),
    "M": (64, 157, 87, 192),
    "N": (14, 157, 34, 192),
    "O": (813, 62, 835, 89),
    "P": (866, 62, 883, 89),
    "Q": (413, 62, 435, 89),
    "R": (566, 62, 583, 89),
    "S": (167, 107, 184, 142),
    "T": (616, 62, 633, 89),
    "U": (715, 62, 735, 89),
    "V": (914, 107, 934, 142),
    "W": (462, 62, 487, 89),
    "X": (816, 107, 835, 142),
    "Y": (666, 62, 685, 89),
    "Z": (766, 107, 784, 142),
    "[": (918, 62, 930, 89),
    "F1": (63, 14, 85, 37),
    "F2": (112, 14, 136, 37),
    "F3": (162, 14, 186, 37),
    "F4": (212, 14, 237, 37),
    "F5": (262, 14, 286, 37),
    "F6": (312, 14, 337, 37),
    "F7": (362, 14, 386, 37),
    "F8": (413, 14, 438, 37),
    "F9": (463, 14, 488, 37),
    "Up": (166, 210, 184, 240),
    "End": (61, 210, 87, 240),
    "Esc": (10, 14, 37, 37),
    "F10": (507, 14, 542, 37),
    "F11": (559, 14, 591, 37),
    "F12": (608, 14, 642, 37),
    "Tab": (360, 62, 391, 89),
    "Back": (305, 62, 343, 89),
    "Down": (266, 210, 284, 240),
    "Home": (905, 157, 944, 192),
    "Left": (210, 210, 238, 240),
    "Num1": (918, 210, 930, 240),
    "Num2": (967, 210, 983, 240),
    "Num4": (764, 210, 782, 240),
    "Num5": (817, 210, 833, 240),
    "Num6": (865, 210, 882, 240),
    "Num7": (567, 210, 583, 240),
    "Num8": (616, 210, 633, 240),
    "Num9": (666, 210, 683, 240),
    "Enter": (654, 107, 697, 142),
    "Pause": (804, 157, 845, 192),
    "Right": (311, 210, 339, 240),
    "Space": (452, 157, 498, 192),
    "SysRq": (704, 157, 745, 192),
    "Delete": (4, 210, 45, 240),
    "Insert": (856, 157, 893, 192),
    "NumAdd": (717, 210, 732, 240),
    "NumDiv": (419, 210, 432, 240),
    "NumMul": (468, 210, 482, 240),
    "NumSub": (518, 210, 531, 240),
    "PageUp": (959, 157, 992, 192),
    "NumLock": (359, 210, 392, 240),
    "CapsLock": (56, 107, 95, 142),
    "LeftCtrl": (309, 157, 339, 192),
    "PageDown": (106, 210, 145, 240),
    "LeftShift": (707, 107, 744, 142),
    "RightCtrl": (655, 157, 694, 192),
    "RightShift": (256, 157, 295, 192),
    "ScrollLock": (756, 157, 792, 192),
}


@lru_cache(maxsize=2)
def _atlas(path: str) -> Image.Image:
    with Image.open(path) as source:
        return source.convert("RGBA")


def _cut(path: Path, box) -> Image.Image | None:
    if not path.is_file():
        return None
    return _atlas(str(path)).crop(box).copy()


def pad_glyph(control: str) -> Image.Image | None:
    box = PAD_BUTTON_BOXES.get(str(control).strip().upper())
    return None if box is None else _cut(PAD_ATLAS_PATH, box)


def key_label_glyph(label: str) -> Image.Image | None:
    raw = str(label).strip()
    name = KEY_LABEL_ALIASES.get(raw.upper(), raw)
    box = KEY_LABEL_BOXES.get(name)
    if box is None and len(name) == 1:
        box = KEY_LABEL_BOXES.get(name.upper())
    return None if box is None else _cut(KEY_ATLAS_PATH, box)


def key_plate(wide: bool) -> Image.Image | None:
    return _cut(KEY_ATLAS_PATH, PLATE_WIDE_BOX if wide else PLATE_SQUARE_BOX)


def prompt_badge(device: str, label: str, height: int) -> Image.Image | None:
    height = max(1, int(height))
    if str(device) == "xinput":
        glyph = pad_glyph(label)
        if glyph is None:
            return None
        scale = height / glyph.height
        return glyph.resize(
            (max(1, round(glyph.width * scale)), height),
            Image.Resampling.LANCZOS,
        )

    glyph = key_label_glyph(label)
    if glyph is None:
        return None

    label_height = max(1, round(height * 0.52))
    label_scale = label_height / glyph.height
    glyph = glyph.resize(
        (max(1, round(glyph.width * label_scale)), label_height),
        Image.Resampling.LANCZOS,
    )
    width = max(height, glyph.width + round(height * 0.55))
    plate = key_plate(width > round(height * 1.35))
    if plate is None:
        return None
    plate = plate.resize((width, height), Image.Resampling.LANCZOS)
    badge = Image.new("RGBA", plate.size, (0, 0, 0, 0))
    badge.alpha_composite(plate)
    badge.alpha_composite(
        glyph,
        ((width - glyph.width) // 2, (height - glyph.height) // 2),
    )
    return badge


__all__ = [
    "KEY_LABEL_ALIASES",
    "KEY_LABEL_BOXES",
    "PAD_BUTTON_BOXES",
    "key_label_glyph",
    "key_plate",
    "pad_glyph",
    "prompt_badge",
]
