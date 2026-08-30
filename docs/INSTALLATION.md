# Installation

## Requirements

- FINAL FANTASY XII THE ZODIAC AGE for Steam, version 1.0.4.0, on Windows 10
  or 11
- [FF12 External File Loader 1.5.2](https://www.nexusmods.com/finalfantasy12/mods/170)
- The **module** release of
  [FF12 tkMalloc Fix 0.2.1](https://www.nexusmods.com/finalfantasy12/mods/475?tab=files)
- [Microsoft Visual C++ Redistributable x64](https://aka.ms/vs/17/release/vc_redist.x64.exe)

Ivalice Together does not require FF12 LUA Loader. Third-party loaders are not
included in the Ivalice Together archive.

## Manual installation

1. Install External File Loader and the module edition of tkMalloc Fix in the
   directories specified by their authors.
2. Extract the Ivalice Together release ZIP into the game's main directory so
   its `x64` and `mods` folders merge with the existing folders.
3. Open `x64/IvaliceTogetherConfig.exe` and configure the party and controllers.
4. Start the game through Steam. The co-op runtime loads automatically.

## Mod Organizer 2

1. Install the FFXII game-support plugin for MO2.
2. Install External File Loader, the module edition of tkMalloc Fix, and the
   Ivalice Together release ZIP as three enabled mods.
3. Add `x64/IvaliceTogetherConfig.exe` from the Ivalice Together virtual mod as
   an MO2 executable and run it through MO2.
4. Start **Final Fantasy XII The Zodiac Age (Modded)** through MO2.

Do not install a separate MO2-specific build. Manual installation and MO2 use
the same Ivalice Together ZIP.

## Vortex

Deploy the required loaders and Ivalice Together with Vortex, configure the mod,
and then start the game through Steam. Starting the game directly from Vortex
can crash on some systems.

## Controllers

XInput and SDL GameController devices are supported, including DualSense over
USB or Bluetooth and DS4Windows virtual controllers. Steam Input works when it
exposes the device through XInput or SDL. Controller assignments may be changed
in the configurator while the game is running.

Keyboard and mouse can be assigned to only one local player at a time.

## Troubleshooting

- If nothing changes in game, confirm that External File Loader and the module
  edition of tkMalloc Fix are active and that
  `x64/modules/ivalice-together-autostart.dll` is visible to the game.
- If the runtime executable disappears, inspect Windows Security protection
  history before reinstalling. Do not disable antivirus protection globally.
- If a controller appears in the configurator but not in game, record whether
  it is using USB, Bluetooth, Steam Input, or DS4Windows and attach that detail
  to the bug report.
- The game does not require `FFXII_TZA.vbf` to exist, and Ivalice Together does
  not use that file as a startup requirement.
