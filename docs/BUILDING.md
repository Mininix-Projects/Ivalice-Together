# Building

## Requirements

- Windows 10 or 11, x64
- Python 3.10
- Packages pinned in `requirements-build.txt`
- Visual Studio with the x64 C++ toolchain
- GNU `as.exe` and `objcopy.exe` for the camera-filter assembly
- A legally obtained local installation of FINAL FANTASY XII THE ZODIAC AGE

The repository intentionally excludes extracted game assets, derived game
textures, and third-party loaders. A source checkout runs the explicit test set
listed in `testes/asset_free_tests.txt`; a complete local asset workspace runs
the full suite automatically. Producing the final executable and release
archive requires the local build-only assets declared by
`source/FF12CoopLauncher.spec`.

## Source checks

```powershell
python -m pip install -r requirements-build.txt
powershell -ExecutionPolicy Bypass -File ferramentas/run_source_checks.ps1
```

Tests that compare against an installed `FFXII_TZA.exe` discover a parent game
directory automatically. Set `FF12_GAME_ROOT` when the repository is elsewhere:

```powershell
$env:FF12_GAME_ROOT = "D:\Games\FINAL FANTASY XII THE ZODIAC AGE"
```

The party-HUD asset tests are optional because their source pack cannot be
redistributed. Set `FF12_COOP_MRP_PACK` to a legally extracted
`mrppack_ys.bin` to enable them.

## Native components

```powershell
powershell -ExecutionPolicy Bypass -File fontes_nativos/build_native.ps1 `
  -PublishToSource
```

The native builder compiles in an isolated staging directory, validates the
camera-cave marker contract, and publishes all three artifacts only after every
tool succeeds. It writes
`fontes_nativos/build/native_build_manifest.json` as the final commit marker.
The public release builder rejects missing manifests, changed source, mixed
output, and native payloads that do not match the frozen runtime.

## Local build-only assets

Before running PyInstaller, supply the resources required by the spec under
`source/`. They include launcher artwork and audio, character portraits,
runtime UI sprites, font data, battle help data, and the small per-language
override sections used by the mod. Do not commit extracted or derived game
assets.

The spec fails immediately and names the first missing resource instead of
producing a partial executable.

## Configurator and runtime

Run PyInstaller from `source/` while directing generated files to the ignored
artifact directory:

```powershell
Set-Location source
pyinstaller --noconfirm --clean `
  --workpath ..\artefatos\build `
  --distpath ..\artefatos\dist `
  FF12CoopLauncher.spec
Set-Location ..
```

## Release archive

```powershell
python ferramentas/build_public_release.py
```

The builder verifies the native manifest and frozen source identity, executes
the runtime self-test in clean directories, validates both manual and MO2
layouts, rejects bundled third-party loaders, and emits one installable ZIP
under the ignored `publicacao/` directory.

The repository may live anywhere. No build script depends on a particular
Steam directory or development-workspace layout.
