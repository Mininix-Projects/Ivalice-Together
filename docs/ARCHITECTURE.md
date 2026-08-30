# Architecture

Ivalice Together is one mod with two user-facing entry paths:

1. The configurator edits persistent party, controller, theme, and interface
   settings.
2. The External File Loader loads the native autostart module with the game.
   Autostart selects the matching game process and starts the bundled runtime
   before a save is loaded.

The runtime uses a pre-extracted frozen Python directory so autostart does not
pay one-file extraction cost or depend on a system Python installation.

## Runtime boundaries

- `runtime_entry.py` validates and dispatches frozen worker commands.
- `start_headless_coop.py` owns startup sequencing and the final readiness
  contract.
- `coop_runtime.py` owns remote-memory installation and the live movement loop.
- `runtime_lifecycle.py` contains process-independent movement and camera policy.
- `headless_menu_shell.py` orchestrates the custom battle interface.
- `menu_runtime_state.py` contains menu input and transition state.
- `native_action_line_theme.py` owns action-line and target-line palette hooks.
- `session_state.py` publishes coherent game/session snapshots.
- `controller_bindings.py` normalizes XInput and SDL controller identities.
- `p2_menu_color_dx11.cpp` renders the co-op overlays through D3D11.

Compatibility exports preserve the original module API where responsibilities
have been split. This keeps existing runtime callers and regression tests stable
while allowing isolated modules to be tested directly.

## Native-memory safety

Native detours validate the bytes at each hook site before installation and
restore only patches that still match the mod's payload. Relative branches use
allocations within the x64 `rel32` range; startup refuses an incoherent final
contract instead of continuing with partially installed hooks.

Hardware enumeration and process-memory rollback deliberately retain defensive
exception handling. Those are external failure boundaries, not ordinary calls
between trusted functions.

## Distribution boundary

The release builder accepts only declared payloads, rejects third-party loader
DLLs, verifies the frozen bytecode against current sources, runs a clean-runtime
self-test, checks both manual and MO2 directory layouts, and validates the final
ZIP before replacing the public archive.
