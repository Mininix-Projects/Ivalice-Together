# Ivalice Together

Ivalice Together adds local co-op for up to three players to FINAL FANTASY XII
THE ZODIAC AGE on Windows.

The mod gives each local player independent movement, targeting, battle menus,
controller bindings, interaction routing, and character-themed interface
elements while keeping the original game responsible for combat and world
simulation.

## Repository contents

- `source/` - Python configurator and runtime modules.
- `fontes_nativos/` - D3D11 renderer, autostart module, and camera filter.
- `testes/` - offline regression, emulation, and contract tests.
- `ferramentas/` - portable source audit and release build entry points.
- `docs/` - architecture, installation, and build documentation.

Generated executables, release archives, reverse-engineering captures, local
configuration, third-party loaders, and assets extracted from the game are not
stored in this repository.

## Player installation

Download the ready-to-install ZIP from the GitHub Releases page. Do not use the
repository's **Code > Download ZIP** archive as a mod package. The same release
ZIP supports manual installation and supported mod managers.

See [docs/INSTALLATION.md](docs/INSTALLATION.md) for loader requirements and
installation steps.

## Development checks

The supported development environment is Python 3.10 on Windows:

```powershell
python -m pip install -r requirements-build.txt
powershell -ExecutionPolicy Bypass -File ferramentas/run_source_checks.ps1
```

The check entry point runs Ruff, compiles every Python module into the ignored
artifact directory, audits the publishable source tree, and executes pytest. A
clean checkout runs the explicit asset-free regression set; a complete local
build workspace automatically runs the full suite.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the runtime boundaries and
[docs/BUILDING.md](docs/BUILDING.md) for native and frozen-runtime builds.

## Project status

Ivalice Together is under active development. Bug reports should include the
game version, installation method, controller model and connection method, and
the exact steps needed to reproduce the problem.

## Legal notice

Ivalice Together is an unofficial fan project and is not affiliated with or
endorsed by Square Enix. FINAL FANTASY and related names and assets belong to
their respective owners. This repository does not distribute the game or
third-party loader binaries.
