# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


here = Path(SPECPATH)


def add_required(collection, source, destination):
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(f"required resource is missing: {source}")
    collection.append((str(source), str(destination)))


datas = []
for filename in (
    "background.png",
    "cancel.wav",
    "confirm.wav",
    "cursol_4_p.png",
    "error.wav",
    "icon.ico",
    "ivalice_together_logo.png",
    "move.wav",
    "open.wav",
):
    add_required(datas, here / "launcher_assets" / filename, "launcher_assets")

for filename in (
    "0-Vaan.png",
    "1-Ashe.png",
    "2-Fran.png",
    "3-Balthier.png",
    "4-Basch.png",
    "5-Penelo.png",
    "6-Reks.png",
    "7-Ashe.png",
    "8-Basch-(Prisoner)-(Variant-1).png",
    "9-Basch-(Fugitive).png",
    "10-Lamont.png",
    "11-Vossler-(Judge).png",
    "12-Vossler-(Knight).png",
    "13-Larsa.png",
    "14-Reddas.png",
):
    add_required(datas, here / "assets" / "portraits" / filename, "assets/portraits")

add_required(
    datas,
    here
    / "game_overrides"
    / "gamedata"
    / "d3d11"
    / "artdata"
    / "menu"
    / "targetline_p_clut_2.dds.phyre",
    "game_overrides/gamedata/d3d11/artdata/menu",
)
add_required(
    datas,
    here
    / "game_overrides"
    / "gamedata"
    / "d3d11"
    / "artdata"
    / "menu"
    / "targetline_p_clut_6.dds.phyre",
    "game_overrides/gamedata/d3d11/artdata/menu",
)
for locale in ("us", "fr", "de", "es", "it", "kr", "cn", "ch", "in"):
    relative = (
        Path("ps2data/image/ff12/myoshiok")
        / locale
        / "packfiles"
        / "tex2pack_ys.bin.dir"
        / "section_000.tm2"
    )
    add_required(
        datas,
        here / "game_overrides" / relative,
        str(Path("game_overrides") / relative.parent),
    )

# Files read directly by the runtime.
for filename in (
    "native_camera_collision_filter_v2.bin",
):
    path = here / filename
    add_required(datas, path, ".")

runtime_png = here / "runtime_ui_assets_png"
for filename in (
    "cursol_4_p__pic00__pal00.png",
    "pause_on_off_gold.png",
    "vaan_command_flag.png",
    "battle_4_p__pic00__pal00.png",
    "buttonicon__pic00__pal15.png",
    "keyboard__pic00__pal00.png",
):
    add_required(datas, runtime_png / filename, "runtime_ui_assets_png")

add_required(datas, runtime_png / "plik_003.png", "runtime_ui_assets_png")

for filename in (
    "dark.png",
    "earth.png",
    "fire.png",
    "flying.png",
    "holy.png",
    "ice.png",
    "lightning.png",
    "water.png",
    "wind.png",
):
    add_required(
        datas,
        runtime_png / "target_traits" / filename,
        "runtime_ui_assets_png/target_traits",
    )

for filename in (
    "berserk.png",
    "blind.png",
    "bravery.png",
    "bubble.png",
    "confuse.png",
    "disable.png",
    "disease.png",
    "doom.png",
    "faith.png",
    "float.png",
    "haste.png",
    "immobilize.png",
    "invisible.png",
    "libra.png",
    "lure.png",
    "oil.png",
    "poison.png",
    "protect.png",
    "reflect.png",
    "regen.png",
    "reverse.png",
    "sap.png",
    "shell.png",
    "silence.png",
    "sleep.png",
    "slow.png",
    "stone.png",
    "stop.png",
):
    add_required(
        datas,
        runtime_png / "target_traits" / "status" / filename,
        "runtime_ui_assets_png/target_traits/status",
    )

for filename in (
    "font00.png",
    "w_win_c__pic00__pal00.png",
):
    add_required(
        datas,
        here / "extracted_ui_assets_png" / filename,
        "extracted_ui_assets_png",
    )

for filename in (
    "border_bl.png",
    "border_bottom.png",
    "border_br.png",
    "border_left.png",
    "border_right.png",
    "border_tl.png",
    "border_top.png",
    "border_tr.png",
    "panel.png",
):
    add_required(datas, here / "imgui_skin_menu" / filename, "imgui_skin_menu")

font_root = (
    here
    / "extracted_ui_assets"
    / "gamedata"
    / "d3d11"
    / "artdata"
    / "font"
    / "us"
)
for filename in ("font00.dat", "font00.ker"):
    add_required(
        datas,
        font_root / filename,
        "extracted_ui_assets/gamedata/d3d11/artdata/font/us",
    )
battle_binary_root = (
    here
    / "extracted_ui_assets"
    / "ps2data"
    / "image"
    / "ff12"
    / "test_battle"
    / "us"
    / "binaryfile"
)
for filename in ("battle_pack.bin", "help_action.bin", "help_menu.bin"):
    add_required(
        datas,
        battle_binary_root / filename,
        "extracted_ui_assets/ps2data/image/ff12/test_battle/us/binaryfile",
    )

binaries = [
    (str(here / "p2_menu_color_dx11_v85.dll"), "."),
    (str(here / "ivalice-together-autostart.dll"), "."),
]

a = Analysis(
    [str(here / "launcher_redesign.py")],
    pathex=[str(here)],
    binaries=binaries,
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "OpenGL",
        "PIL.AvifImagePlugin",
        "PIL.ImageCms",
        "PIL.ImageMath",
        "PIL.ImageQt",
        "PIL.ImageShow",
        "PIL.ImageTk",
        "PIL.WebPImagePlugin",
        "PIL._avif",
        "PIL._imagingcms",
        "PIL._imagingmath",
        "PIL._imagingtk",
        "PIL._webp",
        "jinja2",
        "numpy.testing",
        "numpy._core._multiarray_tests",
        "psutil",
        "pythoncom",
        "pywintypes",
        "tkinter",
        "torch",
        "win32com",
    ],
    noarchive=False,
    optimize=1,
)

# The pygame hook already places its DLLs at the root. Dependency analysis may
# repeat the same files under pygame without changing their contents.
root_binary_sources = {
    Path(source).resolve()
    for target, source, kind in a.binaries
    if len(Path(target).parts) == 1 and Path(source).suffix.casefold() == ".dll"
}
a.binaries = [
    entry
    for entry in a.binaries
    if not (
        Path(entry[0]).parts
        and Path(entry[0]).parts[0].casefold() == "pygame"
        and Path(entry[1]).suffix.casefold() == ".dll"
        and Path(entry[1]).resolve() in root_binary_sources
    )
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="IvaliceTogetherConfig",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(here / "launcher_assets" / "icon.ico"),
    version=str(here / "launcher_version.txt"),
)

# The configurator remains a convenient single file.  Autostart uses this
# pre-extracted companion instead: it avoids the one-file unpack delay before a
# player can load a save, and all worker processes reuse the same directory.
runtime_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="IvaliceTogetherRuntime",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(here / "launcher_assets" / "icon.ico"),
    version=str(here / "launcher_version.txt"),
)

runtime_bundle = COLLECT(
    runtime_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="IvaliceTogetherRuntime",
)
